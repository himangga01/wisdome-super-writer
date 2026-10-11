"""Bounded projections of committed housing runs; caller enforces local access."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

from django.http import Http404
from django.urls import reverse

from apps.local_content.bundles import (
    ArticleBundleWriter,
    BundleValidationError,
    _parse_strict_json_object,
)
from apps.local_content.contracts import SEOUL
from apps.local_content.sources.applyhome import _subscription_kind
from apps.local_content.views import (
    _article_root,
    _confined_existing,
    _final_articles,
    _official_https_url,
    _parse_run_name,
    _reject_links_and_reparse,
    _run_manifest,
)
from wisdome_writer.portal.periods import PeriodSelection

_MAX_ROOT_ENTRIES = 512
_MAX_RUNS = 128
_MAX_NOTICES = 1000
_VISIBLE_RUNS = 12
_VISIBLE_NOTICES = 100
_METADATA_FILES = {"notices.json", "raw-observations.json"}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SOURCE_DATE = re.compile(
    r"(?<!\d)(?P<y>\d{4})\s*[./-]\s*(?P<m>\d{1,2})\s*[./-]\s*(?P<d>\d{1,2})(?!\d)"
)
_READ_ERRORS = (BundleValidationError, Http404, OSError, ValueError)


class _ReadOnlyBundleWriter(ArticleBundleWriter):
    """Keep the writer's validation rules without its root-creation side effect."""

    def _safe_root(self) -> Path:
        root = Path(os.path.abspath(self.root))
        _reject_links_and_reparse(root)
        if not root.is_dir():
            raise BundleValidationError("housing root is unavailable")
        return root


def load_housing_snapshot(
    query: str = "", period: PeriodSelection | None = None, stage: str = "all"
) -> dict[str, object]:
    """Project a latest snapshot or confirmed subscription dates across history.

    Views must authorize local loopback access before calling this function.
    Empty storage is distinct from invalid, legacy-only or over-limit storage.
    Search covers the complete selected snapshot before the display limit.
    """
    if not isinstance(query, str) or len(query.strip()) > 200:
        raise ValueError("query must be a string of at most 200 characters")
    if not isinstance(stage, str) or stage not in {"all", "special", "first"}:
        raise ValueError("stage must be all, special or first")
    if period is not None and period.basis != "schedule":
        raise ValueError("housing period must use the schedule basis")
    active_period = period is not None and period.active
    query = query.strip().casefold()
    result: dict[str, object] = {
        "available": True,
        "message": "아직 저장된 주택 공고 수집 결과가 없습니다.",
        "snapshot_name": None,
        "observed_at": None,
        "notices": [],
        "runs": [],
        "total": 0,
        "matching_total": 0,
        "unknown_schedule_count": 0,
        "history_total": 0,
        "available_years": [],
    }
    try:
        root = _article_root(required=False)
        if root is None:
            return result
        candidates = _run_candidates(root)
    except _READ_ERRORS:
        return _unavailable(result)

    writer = _ReadOnlyBundleWriter(root)
    runs: list[dict[str, object]] = []
    snapshots: list[tuple[datetime, str, list[dict[str, object]], bool]] = []
    observed: dict[str, datetime] = {}
    invalid = False
    for run_name, run_date in candidates:
        try:
            run_root = writer.validate_run(run_date, run_directory=run_name)
            _confined_existing(root, run_name)
            manifest = _run_manifest(run_root)
            final_articles = _final_articles(run_root, writer, manifest)
            run = {
                "name": run_name,
                "url": reverse("local-article-run", kwargs={"run_name": run_name}),
                "article_count": len(final_articles),
                "date": run_date.isoformat(),
            }
            if manifest.get("schema_version") != 2:
                runs.append(run)
                continue
            run_files = manifest.get("run_files")
            if not isinstance(run_files, dict) or set(run_files) != _METADATA_FILES:
                raise ValueError("unbound metadata")
            payload = writer.read_validated_run_file(
                run_date, "notices.json", run_directory=run_name
            )
            if hashlib.sha256(payload).hexdigest() != run_files["notices.json"]:
                raise ValueError("metadata changed after manifest selection")
            document = _parse_strict_json_object(payload, "housing portal notices")
            timestamp, notices, complete = _project_document(document, run_date)
            for notice in notices:
                notice.update(captured_at=timestamp, run_name=run_name)
            runs.append(run)
            observed[run_name] = timestamp
            snapshots.append((timestamp, run_name, notices, complete))
        except _READ_ERRORS:
            invalid = True

    minimum = datetime.min.replace(tzinfo=SEOUL)
    runs.sort(
        key=lambda run: (observed.get(str(run["name"]), minimum), str(run["name"])),
        reverse=True,
    )
    result["runs"] = runs[:_VISIBLE_RUNS]
    if not snapshots:
        return _unavailable(result) if candidates or invalid else result

    timestamp, run_name, notices, complete = max(snapshots, key=lambda item: item[:2])
    history = [row for _time, _name, rows, _complete in snapshots for row in rows]
    result["available_years"] = sorted(
        {date.fromisoformat(value).year for row in history for value in _stage_dates(row, stage)},
        reverse=True,
    )
    latest_history = _latest_identities(history)
    result["history_total"] = len(latest_history)
    unknown_rows = latest_history if active_period else _latest_identities(notices)
    result["unknown_schedule_count"] = sum(not _stage_dates(row, stage) for row in unknown_rows)
    if active_period:
        assert period is not None
        qualified = [
            row
            for row in history
            if any(period.matches(value) for value in _stage_dates(row, stage))
        ]
        notices = _latest_identities(qualified)
        notices.sort(
            key=lambda row: (
                min(value for value in _stage_dates(row, stage) if period.matches(value)),
                str(row["title"]),
                str(row["source_key"]),
                str(row["external_id"]),
            )
        )
        complete = all(snapshot[3] for snapshot in snapshots)
    elif stage != "all":
        notices = [row for row in notices if _stage_dates(row, stage)]
    matching = [
        notice
        for notice in notices
        if not query
        or any(
            query in str(notice[field] or "").casefold()
            for field in ("title", "publisher", "region")
        )
    ]
    message = "저장된 수집 시점의 공고입니다. 현재 신청 조건은 공식 공고에서 확인해 주세요."
    if active_period:
        message = (
            "검증된 저장 이력의 특별공급·1순위 청약일 기준입니다. "
            "청약일이 확인되지 않은 공고는 월별 결과에 포함하지 않습니다. "
            "이전 게시 공고의 수집 범위 밖 일정은 누락될 수 있습니다."
        )
    if result["unknown_schedule_count"]:
        message += f" 선택한 청약 단계의 날짜 미확인 공고: {result['unknown_schedule_count']}개."
    if not complete:
        message += " 이 실행은 일부 수집 결과가 누락되었을 수 있습니다."
    if invalid:
        message += " 검증할 수 없는 일부 저장 결과는 제외했습니다."
    if len(matching) > _VISIBLE_NOTICES:
        message += f" 공고 목록은 최대 {_VISIBLE_NOTICES}개까지 표시합니다."
    result.update(
        message=message,
        snapshot_name=run_name,
        observed_at=timestamp,
        notices=matching[:_VISIBLE_NOTICES],
        total=len(notices),
        matching_total=len(matching),
    )
    return result


def _unavailable(result: dict[str, object]) -> dict[str, object]:
    result.update(
        available=False,
        total=None,
        matching_total=None,
        unknown_schedule_count=None,
        history_total=None,
        message="저장된 공고 목록을 확인할 수 없습니다. 검증된 실행 기록에서 확인해 주세요.",
    )
    return result


def _run_candidates(root: Path) -> list[tuple[str, date]]:
    candidates: list[tuple[str, date]] = []
    with os.scandir(root) as entries:
        for count, entry in enumerate(entries, start=1):
            if count > _MAX_ROOT_ENTRIES:
                raise ValueError("root entry limit")
            run_date = _parse_run_name(entry.name)
            if run_date is not None:
                candidates.append((entry.name, run_date))
                if len(candidates) > _MAX_RUNS:
                    raise ValueError("run history limit")
    return candidates


def _project_document(
    document: dict[str, object], run_date: date
) -> tuple[datetime, list[dict[str, object]], bool]:
    window = document.get("window")
    notices = document.get("notices")
    if (
        type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or not isinstance(window, dict)
        or not isinstance(notices, list)
        or len(notices) > _MAX_NOTICES
        or not isinstance(document.get("complete"), bool)
        or not isinstance(document.get("detail_failures"), list)
        or not isinstance(document.get("conflicts"), list)
    ):
        raise ValueError("invalid notice document")
    start = _timestamp(window.get("start"))
    end = _timestamp(window.get("end"))
    if start > end or end.date() != run_date:
        raise ValueError("invalid collection window")
    rows = [_project_notice(notice) for notice in notices]
    if len({(row["source_key"], row["external_id"]) for row in rows}) != len(rows):
        raise ValueError("duplicate notice identity in saved run")
    rows.sort(key=lambda row: (str(row["published_on"] or ""), str(row["title"])), reverse=True)
    return (
        end,
        rows,
        bool(document["complete"])
        and not document["detail_failures"]
        and not document["conflicts"],
    )


def _project_notice(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid notice")
    url = _text(value.get("canonical_url"), maximum=4096, required=True, allow_controls=True)
    safe_url = None
    if (
        url
        and "\\" not in url
        and not any(
            unicodedata.category(character) in {"Cc", "Cf"} or character.isspace()
            for character in url
        )
    ):
        safe_url = _official_https_url(url)
    published = _timestamp(value.get("published_at"))
    checksum = _text(value.get("source_checksum"), maximum=64, required=True)
    if checksum is None or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise ValueError("invalid source identity checksum")
    return {
        "title": _text(value.get("title"), maximum=1024, required=True),
        "publisher": _text(value.get("publisher"), maximum=256, required=True),
        "url": safe_url,
        "published_on": published.date().isoformat(),
        "application_start": _optional_date(value.get("application_start")),
        "application_end": _optional_date(value.get("application_end")),
        "deadline": _optional_date(value.get("deadline")),
        "region": _text(value.get("region"), maximum=128),
        "category": _text(value.get("category"), maximum=128, required=True),
        "status": _text(value.get("status"), maximum=128, required=True),
        "source_key": _text(value.get("source_key"), maximum=128, required=True),
        "external_id": _text(value.get("external_id"), maximum=512, required=True),
        "source_checksum": checksum,
        "subscription_events": _subscription_events(value.get("facts")),
    }


def _subscription_events(facts: object) -> list[dict[str, object]]:
    if not isinstance(facts, list) or len(facts) > 64:
        raise ValueError("invalid subscription facts")
    events: list[dict[str, object]] = []
    for pair in facts:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("invalid subscription fact")
        label = _text(pair[0], maximum=256, required=True)
        text = _text(pair[1], maximum=2048, required=False)
        if text is None:
            raise ValueError("invalid subscription fact text")
        kind = _subscription_kind(label or "")
        if kind is None:
            continue
        dates: list[str] = []
        try:
            for match in _SOURCE_DATE.finditer(text):
                parsed = date(int(match["y"]), int(match["m"]), int(match["d"])).isoformat()
                if parsed not in dates:
                    dates.append(parsed)
        except ValueError:
            # A malformed explicit date cannot establish any date for this event.
            dates = []
        remaining = _SOURCE_DATE.sub("", unicodedata.normalize("NFKC", text))
        if re.search(r"\d+\s*[./-]\s*\d+\s*[./-]\s*\d+", remaining):
            # Skipped malformed date tokens invalidate the whole stage event,
            # including an otherwise valid second endpoint in the same text.
            dates = []
        if (
            dates
            and re.fullmatch(
                r"(?:[\s\d.():/,~∼–—-]|[월화수목금토일](?:요일)?|오전|오후|시|분|부터|까지)*",
                remaining,
            )
            is None
        ):
            dates = []
        event = {"kind": kind, "label": label, "text": text, "dates": dates}
        if event not in events:
            events.append(event)
    return events


def _stage_dates(row: dict[str, object], stage: str) -> list[str]:
    return [
        value
        for event in row["subscription_events"]
        if stage == "all" or event["kind"] == stage
        for value in event["dates"]
    ]


def _latest_identities(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    latest: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        identity = (str(row["source_key"]), str(row["external_id"]))
        previous = latest.get(identity)
        if previous is None or (row["captured_at"], row["run_name"]) > (
            previous["captured_at"],
            previous["run_name"],
        ):
            latest[identity] = row
    return list(latest.values())


def _text(
    value: object, *, maximum: int, required: bool = False, allow_controls: bool = False
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError("invalid text")
    if not allow_controls and any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise ValueError("invalid control character")
    return value


def _optional_date(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("invalid date")
    return date.fromisoformat(value).isoformat()


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or "T" not in value:
        raise ValueError("invalid timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp requires timezone")
    try:
        return result.astimezone(SEOUL)
    except OverflowError as exc:
        raise ValueError("timestamp is outside the supported date range") from exc
