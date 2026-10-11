"""Read only validated schema-v2 funding runs, preserving mode and history."""

from __future__ import annotations

import json
import re
import stat
from datetime import date, datetime
from pathlib import Path

from apps.local_content.bundles import ArticleBundleWriter
from apps.policy_funding.catalog import source_by_id
from apps.policy_funding.contracts import FundingNotice, content_hash

RUN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})(?:--run-[a-f0-9]{12})?")


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate policy JSON key")
        result[key] = value
    return result


def _json(payload: bytes) -> dict:
    def invalid(value):
        raise ValueError("Non-finite policy JSON")

    try:
        result = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_strict_object, parse_constant=invalid
        )
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid policy JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("Policy metadata must be an object")
    return result


def _safe_root(root: Path) -> Path:
    root = Path(root).absolute()
    for entry in (root, *root.parents):
        if not entry.exists() and not entry.is_symlink():
            continue
        metadata = entry.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("Policy root has a linked or non-directory ancestor")
    return root


def read_run(root: Path, name: str) -> dict:
    if not isinstance(name, str) or not (match := RUN_NAME.fullmatch(name)):
        raise ValueError("Invalid policy run name")
    run_date = date.fromisoformat(match[1])
    writer = ArticleBundleWriter(_safe_root(root))
    records = _json(writer.read_validated_run_file(run_date, "notices.json", run_directory=name))
    observation = _json(
        writer.read_validated_run_file(run_date, "raw-observations.json", run_directory=name)
    )
    if (
        set(records) != {"schema_version", "topic", "run_name", "mode", "notices"}
        or type(records["schema_version"]) is not int
        or records["schema_version"] != 1
        or records["topic"] != "policy_funding"
        or records["run_name"] != name
        or not isinstance(records["mode"], str)
        or records["mode"] not in {"live", "fixture"}
        or not isinstance(records["notices"], list)
        or len(records["notices"]) > 300
    ):
        raise ValueError("Invalid policy notice inventory")
    expected = {
        "schema_version",
        "run_name",
        "run_date",
        "mode",
        "captured_at",
        "records_hash",
        "source_outcomes",
        "changes",
        "case_verification",
        "coverage_limited",
    }
    if (
        not expected <= set(observation)
        or set(observation) - expected - {"selection"}
        or type(observation["schema_version"]) is not int
        or observation["schema_version"] != 1
        or observation["run_name"] != name
        or observation["run_date"] != run_date.isoformat()
        or observation["mode"] != records["mode"]
        or observation["records_hash"] != content_hash(records)
        or observation["coverage_limited"] is not True
    ):
        raise ValueError("Invalid policy observation identity")
    if "selection" in observation:
        from apps.policy_funding.catalog import REGIONS

        selection = observation["selection"]
        if (
            not isinstance(selection, dict)
            or set(selection) != {"region", "include_national"}
            or selection["region"] is not None
            and not isinstance(selection["region"], str)
            or selection["region"] not in {None, "national", "unspecified", *REGIONS}
            or type(selection["include_national"]) is not bool
            or selection["include_national"] != (selection["region"] in REGIONS)
        ):
            raise ValueError("Invalid policy region selection")
    if not isinstance(observation["captured_at"], str):
        raise ValueError("Invalid policy capture time")
    stamp = datetime.fromisoformat(observation["captured_at"])
    if stamp.utcoffset() is None:
        raise ValueError("Policy run lacks an aware capture time")
    notices = tuple(FundingNotice.from_dict(row) for row in records["notices"])
    if len({row.stable_id for row in notices}) != len(notices):
        raise ValueError("Duplicate notice in policy run")
    if any((row.provenance == "fixture") != (records["mode"] == "fixture") for row in notices):
        raise ValueError("Policy fixture/live provenance mismatch")
    for key in ("source_outcomes", "changes"):
        if not isinstance(observation[key], list) or len(observation[key]) > 600:
            raise ValueError("Policy observation exceeds bounds")
    for row in observation["source_outcomes"]:
        if (
            not isinstance(row, dict)
            or set(row)
            != {"source_id", "status", "errors", "request_count", "count", "coverage_limited"}
            or not isinstance(row["source_id"], str)
            or not isinstance(row["status"], str)
            or row["status"] not in {"completed", "partial", "failed", "discovery_only"}
            or type(row["request_count"]) is not int
            or not 0 <= row["request_count"] <= 150
            or type(row["count"]) is not int
            or not 0 <= row["count"] <= 100
            or row["coverage_limited"] is not True
            or not isinstance(row["errors"], list)
            or len(row["errors"]) > 150
            or any(not isinstance(error, str) or len(error) > 250 for error in row["errors"])
        ):
            raise ValueError("Invalid policy source outcome")
        source_by_id(row["source_id"])
    for row in observation["changes"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"notice_id", "status", "fields"}
            or not isinstance(row["notice_id"], str)
            or row["notice_id"] not in {n.stable_id for n in notices}
            or not isinstance(row["status"], str)
            or row["status"] not in {"added", "changed", "unchanged"}
            or not isinstance(row["fields"], list)
            or len(row["fields"]) > 30
            or any(
                not isinstance(field, str) or field not in FundingNotice.__dataclass_fields__
                for field in row["fields"]
            )
        ):
            raise ValueError("Invalid policy change projection")
    _validate_case_report(observation["case_verification"], mode=records["mode"])
    return {**observation, "notices": notices}


def _validate_case_report(report, *, mode):
    required = {
        "schema_version",
        "as_of",
        "input_provenance",
        "loan_approval",
        "claims",
        "review_note",
    }
    if (
        not isinstance(report, dict)
        or set(report) != required
        or type(report["schema_version"]) is not int
        or report["schema_version"] != 1
        or report["input_provenance"] != "user_provided"
        or report["loan_approval"] is not False
        or not isinstance(report["as_of"], str)
        or not isinstance(report["review_note"], str)
        or len(report["review_note"]) > 2000
        or not isinstance(report["claims"], list)
        or len(report["claims"]) > 30
    ):
        raise ValueError("Invalid reference-case report")
    date.fromisoformat(report["as_of"])
    statuses = {
        "unverified",
        "fixture_only",
        "supported_by_collected_notice",
        "supported_by_dated_review",
        "supported_with_conditions",
        "different_program_date",
    }
    from apps.policy_funding.contracts import validate_reference_url

    for claim in report["claims"]:
        fields = {"id", "claim", "status", "sources", "explanation"}
        if (
            not isinstance(claim, dict)
            or not fields <= set(claim)
            or set(claim) - fields - {"evidence"}
            or any(
                not isinstance(claim[key], str) or len(claim[key]) > 2000
                for key in fields - {"sources"}
            )
            or claim["status"] not in statuses
            or not isinstance(claim["sources"], list)
            or len(claim["sources"]) > 10
            or mode == "fixture"
            and claim["status"] == "supported_by_collected_notice"
        ):
            raise ValueError("Invalid reference claim")
        for url in claim["sources"]:
            validate_reference_url(url)
        if "evidence" in claim:
            if not isinstance(claim["evidence"], list) or len(claim["evidence"]) > 10:
                raise ValueError("Invalid reference evidence")
            for item in claim["evidence"]:
                required = {
                    "checked_on",
                    "method",
                    "attachment_sha256",
                    "source_date",
                    "issuer",
                    "url",
                    "locator",
                    "excerpt",
                    "scope",
                }
                optional = {"attachment_url", "filename_qualifier"}
                if (
                    not isinstance(item, dict)
                    or not required <= set(item)
                    or set(item) - required - optional
                    or any(
                        value is not None and (not isinstance(value, str) or len(value) > 3000)
                        for value in item.values()
                    )
                    or any(
                        not isinstance(item[key], str)
                        for key in required - {"source_date", "attachment_sha256"}
                    )
                ):
                    raise ValueError("Invalid reference evidence fields")
                validate_reference_url(item["url"])
                date.fromisoformat(item["checked_on"])
                if item["source_date"] is not None:
                    date.fromisoformat(item["source_date"])
                checksum = item["attachment_sha256"]
                if checksum is not None and not re.fullmatch(r"[a-f0-9]{64}", checksum):
                    raise ValueError("Invalid reference attachment hash")
                if item.get("attachment_url"):
                    validate_reference_url(item["attachment_url"])


def list_runs(root: Path, *, limit: int = 30) -> list[dict]:
    root = _safe_root(root)
    if not root.exists():
        return []
    if not 1 <= limit <= 100:
        raise ValueError("Invalid policy history limit")
    names = sorted(
        (path.name for path in root.iterdir() if RUN_NAME.fullmatch(path.name)), reverse=True
    )
    results = []
    for name in names[:100]:
        try:
            results.append(read_run(root, name))
        except (ValueError, OSError):
            continue
    results.sort(key=lambda row: row["captured_at"], reverse=True)
    return results[:limit]


def latest_notices(root: Path, *, mode: str = "live") -> tuple[FundingNotice, ...]:
    results = {}
    for run in list_runs(root):
        if run["mode"] == mode:
            for notice in run["notices"]:
                results.setdefault(notice.stable_id, notice)
    return tuple(results.values())


def period_notices(root: Path, period, *, mode: str = "live") -> dict:
    """Filter saved observations before deduplication; keep older dated versions."""
    from wisdome_writer.portal.periods import as_local_date

    if mode not in {"live", "fixture"} or period.basis not in {"published", "collected"}:
        raise ValueError("Invalid policy date selection")
    results, notice_runs, unknown, known, years = {}, {}, set(), set(), set()
    runs = [run for run in list_runs(root, limit=100) if run["mode"] == mode]
    for run in runs:
        for notice in run["notices"]:
            value = notice.published_on if period.basis == "published" else notice.fetched_at
            day = as_local_date(value)
            if day is None:
                unknown.add(notice.stable_id)
            else:
                known.add(notice.stable_id)
                years.add(day.year)
            if period.matches(value):
                if notice.stable_id not in results:
                    results[notice.stable_id] = notice
                    notice_runs[notice.stable_id] = run["run_name"]
    return {
        "notices": tuple(results.values()),
        "unknown_date_count": len(unknown - known),
        "available_years": sorted(years),
        "coverage_run_count": len(runs),
        "notice_runs": notice_runs,
    }
