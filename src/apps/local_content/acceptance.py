"""Machine-readable, production-backed audit for one local housing run."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from apps.local_content.bundles import ArticleBundleWriter, BundlePublishError
from apps.local_content.contracts import HousingNotice
from apps.local_content.humanizer import (
    HumanizationVerificationError,
    verify_humanization_audit,
)
from apps.local_content.selection import is_residential

_SHA256 = re.compile(r"[0-9a-f]{64}")
_OFFICIAL_LINK = re.compile(r"\]\(<(?P<url>https://[^>\r\n]+)>\)")
_DETAIL_LINK = re.compile(r"\]\((?P<path>\./[^)\r\n]+/article\.md)\)")
_ALLOWED_SOURCE_HOSTS = frozenset({"www.applyhome.co.kr", "apply.lh.or.kr"})
_EXPECTED_ARTICLE_FILES = frozenset(
    {
        "article.draft.md",
        "article.md",
        "sources.json",
        "verification.json",
        "humanize/input.md",
        "humanize/output.md",
        "humanize/events.ndjson",
        "humanize/verification.json",
        "humanize/audit.json",
        "assets/hero.png",
        "assets/summary-card.webp",
        "assets/timeline.webp",
    }
)
_RAW_OBSERVATION_KEYS = frozenset(
    {
        "source_key",
        "external_id",
        "category",
        "published_at",
        "status",
        "source_checksum",
        "detail_code",
    }
)
_MAX_JSON_BYTES = 16 * 1024 * 1024
_PREVIEW_CSP = (
    "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
    "object-src 'none'; img-src 'self'; style-src 'self'"
)
_DETERMINISTIC_COMMAND_NAMES = frozenset(
    {
        "setup_local",
        "whole_repository_ruff",
        "django_check",
        "migration_check",
        "focused_pytest",
        "full_pytest",
        "changed_file_ruff",
        "ci_material",
        "diff_check",
    }
)


@dataclass(frozen=True)
class AcceptanceExpectations:
    raw_observations: int
    excluded_notices: int
    indexed_notices: int
    detailed_articles: int
    source_keys: tuple[str, ...] = ("applyhome", "lh")


def preview_headers_match(
    headers: dict[str, str],
    *,
    cache_control: str,
) -> bool:
    """Require exact preview security/cache header values."""

    normalized = {key.casefold(): value for key, value in headers.items()}
    return all(
        normalized.get(key) == value
        for key, value in {
            "content-security-policy": _PREVIEW_CSP,
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "cache-control": cache_control,
        }.items()
    )


def derive_task12_acceptance(report: dict[str, object]) -> dict[str, object]:
    """Recompute final acceptance from workflow, artifact, browser, and command facts."""

    section = report.get("task12_acceptance")
    if not isinstance(section, dict):
        section = {}
        report["task12_acceptance"] = section
    artifact = section.get("artifact_audit")
    browser = section.get("browser")
    deterministic = section.get("deterministic_evidence")
    workflow_passed = (
        report.get("live_success") is True
        and report.get("complete") is True
        and report.get("blocked") is False
        and report.get("error_codes") == []
    )
    artifact_passed = (
        isinstance(artifact, dict) and artifact.get("overall_passed") is True
    )
    browser_passed = isinstance(browser, dict) and browser.get("passed") is True
    deterministic_passed, deterministic_evidence = _derive_deterministic_gate(
        deterministic
    )
    requirements = [
        _verdict("workflow_live_success", workflow_passed, {}),
        _verdict("artifact_audit", artifact_passed, {}),
        _verdict("django_brave_all_pages", browser_passed, {}),
        _verdict(
            "deterministic_commands_and_ruff_ruling",
            deterministic_passed,
            deterministic_evidence,
        ),
    ]
    section["requirements"] = requirements
    section["overall_passed"] = all(
        requirement["passed"] is True for requirement in requirements
    )
    return section


def _derive_deterministic_gate(
    deterministic: object,
) -> tuple[bool, dict[str, object]]:
    if not isinstance(deterministic, dict):
        return False, {"reason": "MISSING_DETERMINISTIC_EVIDENCE"}
    commands = deterministic.get("commands")
    ruling = deterministic.get("legacy_ruling")
    if not isinstance(commands, list) or not isinstance(ruling, dict):
        return False, {"reason": "INVALID_DETERMINISTIC_SCHEMA"}
    names: list[str] = []
    gates_valid = True
    whole_ruff_valid = False
    for row in commands:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            gates_valid = False
            continue
        names.append(row["name"])
        exit_code = row.get("exit_code")
        passed = row.get("passed")
        gate = row.get("gate")
        if row["name"] == "whole_repository_ruff":
            if gate is not False or not isinstance(exit_code, int):
                continue
            if exit_code == 0:
                whole_ruff_valid = (
                    passed is True and row.get("disposition") == "non_gate_clean"
                )
            else:
                text = ruling.get("text")
                ruling_hash = ruling.get("sha256")
                whole_ruff_valid = (
                    passed is False
                    and row.get("disposition") == "legacy_debt_not_gate"
                    and ruling.get("applied") is True
                    and isinstance(ruling.get("id"), str)
                    and bool(ruling["id"])
                    and isinstance(text, str)
                    and bool(text)
                    and isinstance(ruling_hash, str)
                    and hashlib.sha256(text.encode("utf-8")).hexdigest()
                    == ruling_hash
                )
        elif (
            gate is not True
            or exit_code != 0
            or passed is not True
            or row.get("disposition") != "gate_passed"
        ):
            gates_valid = False
    exact_names = (
        len(names) == len(set(names))
        and set(names) == _DETERMINISTIC_COMMAND_NAMES
        and deterministic.get("exact_command_plan_executed") is True
    )
    passed = (
        exact_names
        and gates_valid
        and whole_ruff_valid
        and deterministic.get("failed_gates") == []
        and deterministic.get("overall_passed") is True
    )
    return passed, {
        "exact_command_count": len(names),
        "exact_command_names": exact_names,
        "binding_gates_valid": gates_valid,
        "whole_repository_ruff_truth_and_ruling_valid": whole_ruff_valid,
    }


def audit_live_run(
    run_root: Path,
    *,
    window_start: datetime,
    expectations: AcceptanceExpectations,
) -> dict[str, object]:
    """Audit one committed run without trusting workflow report booleans."""

    root = Path(run_root).resolve()
    run_date = _run_date(root.name)
    requirements: list[dict[str, object]] = []

    try:
        validated = ArticleBundleWriter(root.parent).validate_run(
            run_date,
            run_directory=root.name,
        )
        production_validated = validated == root
        validation_error = None
    except (BundlePublishError, OSError, ValueError) as exc:
        production_validated = False
        validation_error = exc.__class__.__name__
    requirements.append(
        _verdict(
            "production_bundle_and_exact_inventory_validation",
            production_validated,
            {
                "run_name": root.name,
                "validator": "ArticleBundleWriter.validate_run",
                "error_type": validation_error,
            },
        )
    )
    if not production_validated:
        return _audit_document(root, requirements, {}, (), ())

    raw = _read_json_object(root / "raw-observations.json")
    notices = _read_json_object(root / "notices.json")
    index_text = (root / "index.md").read_text(encoding="utf-8", errors="strict")
    manifest = _read_json_object(root / "manifest.json")

    raw_result = _audit_raw_observations(
        raw,
        window_start=window_start,
        run_date=run_date,
        expectations=expectations,
    )
    requirements.append(raw_result["verdict"])
    indexed_result = _audit_index(
        notices,
        raw_result=raw_result,
        index_text=index_text,
        root=root,
        expectations=expectations,
    )
    requirements.append(indexed_result["verdict"])
    article_result = _audit_articles(
        root,
        manifest=manifest,
        expected_detail_paths=indexed_result["detail_paths"],
        expectations=expectations,
    )
    requirements.append(article_result["verdict"])
    counts = {
        "raw_observations": raw_result["raw_count"],
        "excluded_notices": raw_result["excluded_count"],
        "indexed_notices": indexed_result["indexed_count"],
        "detailed_articles": article_result["article_count"],
        "images": article_result["image_count"],
    }
    return _audit_document(
        root,
        requirements,
        counts,
        article_result["job_hashes"],
        article_result["verification_hashes"],
    )


def merge_artifact_audit(report_path: Path, audit: dict[str, object]) -> None:
    """Atomically merge body-free artifact evidence into a workflow report."""

    path = Path(report_path)
    report = _read_json_object(path)
    section = report.get("task12_acceptance")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("task12 acceptance report section must be an object")
    section["artifact_audit"] = audit
    report["task12_acceptance"] = section
    payload = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    temporary = path.with_name(f".{path.name}.artifact-audit.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _audit_raw_observations(
    raw: dict[str, object],
    *,
    window_start: datetime,
    run_date: date,
    expectations: AcceptanceExpectations,
) -> dict[str, object]:
    schema_valid = set(raw) == {"schema_version", "window", "sources", "observations"}
    window = raw.get("window")
    sources = raw.get("sources")
    observations = raw.get("observations")
    schema_valid = (
        schema_valid
        and raw.get("schema_version") == 1
        and isinstance(window, dict)
        and set(window) == {"start", "end"}
        and isinstance(sources, list)
        and isinstance(observations, list)
    )
    parsed_end: datetime | None = None
    rows: list[dict[str, object]] = []
    stable_ids: list[tuple[str, str]] = []
    residential_ids: set[tuple[str, str]] = set()
    excluded_count = 0
    dates_valid = False
    if schema_valid:
        try:
            parsed_start = datetime.fromisoformat(str(window["start"]))
            parsed_end = datetime.fromisoformat(str(window["end"]))
            dates_valid = (
                parsed_start == window_start
                and parsed_end.date() == run_date
                and parsed_end >= parsed_start
            )
            for value in observations:
                if not isinstance(value, dict) or set(value) != _RAW_OBSERVATION_KEYS:
                    raise ValueError
                source_key = value.get("source_key")
                external_id = value.get("external_id")
                category = value.get("category")
                published_at = datetime.fromisoformat(str(value.get("published_at")))
                if (
                    not isinstance(source_key, str)
                    or not source_key
                    or not isinstance(external_id, str)
                    or not external_id
                    or not isinstance(category, str)
                    or not isinstance(value.get("status"), str)
                    or not isinstance(value.get("source_checksum"), str)
                    or _SHA256.fullmatch(value["source_checksum"]) is None
                    or value.get("detail_code") not in {"OK", "DETAIL_COLLECTION_FAILED"}
                    or not parsed_start <= published_at <= parsed_end
                ):
                    raise ValueError
                rows.append(value)
                stable_id = (source_key, external_id)
                stable_ids.append(stable_id)
                probe = HousingNotice(
                    source_key=source_key,
                    external_id=external_id,
                    canonical_url="https://invalid.local/audit-only",
                    title="audit-only",
                    publisher=source_key,
                    category=category,
                    region=None,
                    status=str(value["status"]),
                    published_at=published_at,
                    source_checksum=str(value["source_checksum"]),
                )
                if is_residential(probe):
                    residential_ids.add(stable_id)
                else:
                    excluded_count += 1
        except (TypeError, ValueError):
            schema_valid = False
            dates_valid = False
    source_counts = Counter(str(row.get("source_key")) for row in rows)
    source_complete = _raw_sources_complete(
        sources if isinstance(sources, list) else [],
        source_counts,
        rows,
        expectations.source_keys,
    )
    passed = (
        schema_valid
        and dates_valid
        and len(rows) == expectations.raw_observations
        and len(stable_ids) == len(set(stable_ids))
        and excluded_count == expectations.excluded_notices
        and len(residential_ids) == expectations.indexed_notices
        and source_complete
    )
    return {
        "verdict": _verdict(
            "raw_official_observations_and_production_selection",
            passed,
            {
                "raw_count": len(rows),
                "unique_stable_ids": len(set(stable_ids)),
                "excluded_by_is_residential": excluded_count,
                "residential_count": len(residential_ids),
                "source_counts": dict(sorted(source_counts.items())),
                "source_complete": source_complete,
                "window_start": window.get("start") if isinstance(window, dict) else None,
                "window_end": window.get("end") if isinstance(window, dict) else None,
            },
        ),
        "raw_count": len(rows),
        "excluded_count": excluded_count,
        "residential_ids": residential_ids,
        "rows": rows,
    }


def _raw_sources_complete(
    sources: list[object],
    source_counts: Counter[str],
    observations: list[dict[str, object]],
    expected_keys: tuple[str, ...],
) -> bool:
    if len(sources) != len(expected_keys):
        return False
    seen: set[str] = set()
    for value in sources:
        if not isinstance(value, dict) or set(value) != {
            "source_key",
            "collection_code",
            "observation_count",
            "detail_failure_count",
        }:
            return False
        source_key = value.get("source_key")
        if not isinstance(source_key, str) or source_key in seen:
            return False
        seen.add(source_key)
        detail_failures = sum(
            row["source_key"] == source_key
            and row["detail_code"] == "DETAIL_COLLECTION_FAILED"
            for row in observations
        )
        if (
            value.get("collection_code") != "OK"
            or value.get("observation_count") != source_counts[source_key]
            or value.get("detail_failure_count") != detail_failures
            or detail_failures != 0
        ):
            return False
    return seen == set(expected_keys)


def _audit_index(
    notices: dict[str, object],
    *,
    raw_result: dict[str, object],
    index_text: str,
    root: Path,
    expectations: AcceptanceExpectations,
) -> dict[str, object]:
    notice_rows = notices.get("notices")
    rows = notice_rows if isinstance(notice_rows, list) else []
    indexed_ids: list[tuple[str, str]] = []
    canonical_urls: list[str] = []
    rows_valid = set(notices) == {
        "schema_version",
        "window",
        "complete",
        "detail_failures",
        "notices",
        "conflicts",
    }
    for row in rows:
        if not isinstance(row, dict):
            rows_valid = False
            continue
        source_key = row.get("source_key")
        external_id = row.get("external_id")
        canonical_url = row.get("canonical_url")
        if not all(isinstance(value, str) and value for value in (source_key, external_id)):
            rows_valid = False
            continue
        if not isinstance(canonical_url, str) or not _allowed_official_url(canonical_url):
            rows_valid = False
            continue
        indexed_ids.append((source_key, external_id))
        canonical_urls.append(canonical_url)
    official_links = [match.group("url") for match in _OFFICIAL_LINK.finditer(index_text)]
    detail_paths = [match.group("path") for match in _DETAIL_LINK.finditer(index_text)]
    resolved_paths: list[str] = []
    links_safe = True
    for value in detail_paths:
        target = (root / value).resolve()
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError:
            links_safe = False
            continue
        if not target.is_file():
            links_safe = False
            continue
        resolved_paths.append(relative)
    residential_ids = raw_result["residential_ids"]
    passed = (
        rows_valid
        and notices.get("schema_version") == 1
        and notices.get("complete") is True
        and notices.get("detail_failures") == []
        and notices.get("conflicts") == []
        and len(rows) == expectations.indexed_notices
        and len(indexed_ids) == len(set(indexed_ids))
        and set(indexed_ids) == residential_ids
        and len(official_links) == len(rows)
        and official_links == canonical_urls
        and all(_allowed_official_url(url) for url in official_links)
        and len(detail_paths) == expectations.detailed_articles
        and len(detail_paths) == len(set(detail_paths))
        and links_safe
    )
    return {
        "verdict": _verdict(
            "weekly_index_exact_rows_ids_and_links",
            passed,
            {
                "indexed_rows": len(rows),
                "unique_stable_ids": len(set(indexed_ids)),
                "official_links": len(official_links),
                "detail_links": len(detail_paths),
                "resolved_detail_paths": sorted(resolved_paths),
            },
        ),
        "indexed_count": len(rows),
        "detail_paths": tuple(sorted(resolved_paths)),
    }


def _audit_articles(
    root: Path,
    *,
    manifest: dict[str, object],
    expected_detail_paths: tuple[str, ...],
    expectations: AcceptanceExpectations,
) -> dict[str, object]:
    articles = manifest.get("articles")
    article_names = sorted(articles) if isinstance(articles, dict) else []
    failures: list[dict[str, str]] = []
    job_hashes: list[str] = []
    verification_hashes: list[str] = []
    image_count = 0
    observed_detail_paths: list[str] = []
    for name in article_names:
        bundle = root / name
        try:
            bundle_manifest = _read_json_object(bundle / "manifest.json")
            files = bundle_manifest.get("files")
            if not isinstance(files, dict) or set(files) != _EXPECTED_ARTICLE_FILES:
                raise ValueError("inventory")
            observed_detail_paths.append(f"{name}/article.md")
            images = {
                path: (bundle / path).read_bytes()
                for path in sorted(_EXPECTED_ARTICLE_FILES)
                if path.startswith("assets/")
            }
            image_count += len(images)
            audit = _read_json_object(bundle / "humanize" / "audit.json")
            result = verify_humanization_audit(
                audit,
                draft_markdown=(bundle / "article.draft.md").read_text("utf-8"),
                final_markdown=(bundle / "article.md").read_text("utf-8"),
                input_document=(bundle / "humanize" / "input.md").read_text("utf-8"),
                candidate=(bundle / "humanize" / "output.md").read_text("utf-8"),
                sources=(bundle / "sources.json").read_bytes(),
                images=images,
            )
            _require_article_status(bundle)
            _require_official_sources(bundle / "sources.json")
            job_hashes.append(result.job_hash)
            verification_hashes.append(result.verification_hash)
        except (
            BundlePublishError,
            HumanizationVerificationError,
            OSError,
            UnicodeError,
            ValueError,
        ) as exc:
            failures.append({"article": name, "error_type": exc.__class__.__name__})
    expected = tuple(
        sorted(path.removeprefix("./") for path in expected_detail_paths)
    )
    passed = (
        manifest.get("schema_version") == 2
        and len(article_names) == expectations.detailed_articles
        and len(observed_detail_paths) == expectations.detailed_articles
        and tuple(sorted(observed_detail_paths)) == expected
        and len(job_hashes) == expectations.detailed_articles
        and len(job_hashes) == len(set(job_hashes))
        and len(verification_hashes) == expectations.detailed_articles
        and image_count == expectations.detailed_articles * 3
        and not failures
    )
    return {
        "verdict": _verdict(
            "article_humanization_sources_images_and_closed_inventory",
            passed,
            {
                "article_count": len(article_names),
                "humanizer_jobs_reverified": len(job_hashes),
                "unique_job_hashes": len(set(job_hashes)),
                "image_count": image_count,
                "failures": failures,
                "article_paths": [f"{name}/article.md" for name in article_names],
            },
        ),
        "article_count": len(article_names),
        "image_count": image_count,
        "job_hashes": tuple(sorted(job_hashes)),
        "verification_hashes": tuple(sorted(verification_hashes)),
    }


def _require_article_status(bundle: Path) -> None:
    verification = _read_json_object(bundle / "verification.json")
    humanization = _read_json_object(bundle / "humanize" / "verification.json")
    if (
        set(verification)
        != {
            "status",
            "source_key",
            "external_id",
            "source_checksum",
            "humanization_status",
        }
        or verification.get("status") != "verified"
        or verification.get("humanization_status") != "verified"
        or humanization.get("status") != "verified"
        or humanization.get("error_codes") != []
    ):
        raise ValueError("article verification status")
    events = [
        _strict_json_loads(line.encode("utf-8"))
        for line in (bundle / "humanize" / "events.ndjson")
        .read_text("utf-8")
        .splitlines()
        if line
    ]
    if [event.get("status") for event in events if isinstance(event, dict)] != [
        "completed",
        "verified",
    ]:
        raise ValueError("humanization event status")


def _require_official_sources(path: Path) -> None:
    value = _strict_json_loads(path.read_bytes())
    if not isinstance(value, list) or not value:
        raise ValueError("article sources")
    for row in value:
        if (
            not isinstance(row, dict)
            or set(row) != {"source_key", "title", "publisher", "url", "checksum"}
            or not _allowed_official_url(row.get("url"))
            or not isinstance(row.get("checksum"), str)
            or _SHA256.fullmatch(row["checksum"]) is None
        ):
            raise ValueError("article sources")


def _allowed_official_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in _ALLOWED_SOURCE_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed.fragment == ""
    )


def _run_date(name: str) -> date:
    match = re.fullmatch(r"(?P<date>\d{4}-\d{2}-\d{2})(?:--run-[0-9a-f]{12})?", name)
    if match is None:
        raise ValueError("run directory name is invalid")
    return date.fromisoformat(match.group("date"))


def _read_json_object(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    if not payload or len(payload) > _MAX_JSON_BYTES:
        raise ValueError("JSON file size is invalid")
    value = _strict_json_loads(payload)
    if not isinstance(value, dict):
        raise ValueError("JSON file must contain an object")
    return value


def _strict_json_loads(payload: bytes) -> object:
    def reject_duplicate(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=reject_duplicate,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("JSON constant")),
    )


def _verdict(
    name: str,
    passed: bool,
    evidence: dict[str, object],
) -> dict[str, object]:
    return {"name": name, "passed": bool(passed), "evidence": evidence}


def _audit_document(
    root: Path,
    requirements: list[dict[str, object]],
    counts: dict[str, object],
    job_hashes: tuple[str, ...],
    verification_hashes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_name": root.name,
        "requirements": requirements,
        "counts": counts,
        "humanizer_job_hashes": list(job_hashes),
        "humanizer_verification_hashes": list(verification_hashes),
        "overall_passed": bool(requirements)
        and all(row.get("passed") is True for row in requirements),
    }
