"""Read-only, topic-scoped projections for the semiconductor operator portal."""

import json
from urllib.parse import urlsplit

from django.conf import settings
from django.db import DatabaseError
from django.db.models import F, Max, Q

from apps.collection.models import CollectionRun, SourceItem, SourceItemStatus
from apps.editorial.models import DraftArticle
from apps.local_content.bundles import BundleValidationError
from apps.topics.models import SourceDefinition

TOPIC = "semiconductor_news"


def _https_host(value: str) -> str | None:
    if not isinstance(value, str) or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\" for char in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            return None
        return parsed.hostname.lower()
    except ValueError:
        return None


def _source_url(value: str, base_url: str) -> str | None:
    host = _https_host(base_url)
    return value if host and _https_host(value) == host else None


def _rights_label(value: str) -> str:
    return dict(SourceDefinition.RightsStatus.choices).get(value, "불명")


def _source_row(*, name, publisher, url, enabled, rights):
    return {
        "display_name": name,
        "publisher": publisher,
        "url": _source_url(url, url),
        "enabled": enabled is True,
        "rights_label": _rights_label(rights),
    }


def _configured_sources() -> list[dict]:
    """Project the repository catalog, without importing or approving it."""
    path = settings.REPOSITORY_ROOT / "config" / "source-registry" / "semiconductor_news.json"
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(catalog, dict) or catalog.get("topicCode") != TOPIC:
        return []
    sources = catalog.get("sources")
    if not isinstance(sources, list):
        return []
    return [
        _source_row(
            name=row["displayName"],
            publisher=row.get("publisher") or row.get("ownerName") or row["displayName"],
            url=row.get("baseUrl", ""),
            enabled=row.get("enabled", False),
            rights=row.get("rightsStatus", "unknown"),
        )
        for row in sources
        if isinstance(row, dict) and isinstance(row.get("displayName"), str)
    ]


def _local_archive(query, period):
    if not settings.IS_LOCAL_RUNTIME:
        return None
    from .news_archive import load_news_archive

    root = getattr(
        settings, "SEMICONDUCTOR_ARCHIVE_ROOT", settings.REPOSITORY_ROOT / "output/semiconductor"
    )
    try:
        data = load_news_archive(root, query=query, period=period)
    except (ValueError, OSError, BundleValidationError):
        data = {
            "available": False,
            "total": None,
            "news": [],
            "drafts": [],
            "runs": [],
            "last_seen": None,
            "message": "저장된 반도체 메타데이터를 확인할 수 없습니다.",
            "inventory_label": "로컬 원문 메타데이터",
        }
    if data is not None:
        data["inventory_kind"] = "local_metadata"
        data["sources"] = _configured_sources()
    return data


def _missing_local_inventory():
    return {
        "available": False,
        "total": None,
        "news": [],
        "drafts": [],
        "runs": [],
        "last_seen": None,
        "last_attempt_at": None,
        "unknown_date_count": None,
        "inventory_kind": "local_metadata",
        "inventory_label": "로컬 원문 메타데이터",
        "sources": _configured_sources(),
        "message": "이 로컬 원문 메타데이터 저장소에서 확인할 자료가 없습니다.",
    }


def load_semiconductor_data(query: str = "", period=None, inventory="auto") -> dict:
    """Return saved versions and drafts; never collect or mutate on a page read.

    ``total`` counts matching non-tombstoned source versions. ``last_seen`` is
    the latest saved collection time for this topic, independent of search.
    Directory entries are configuration, not proven collection coverage.
    """
    query = query.strip()[:200]
    if inventory not in {"auto", "local", "database"}:
        raise ValueError("invalid semiconductor inventory")
    if inventory == "local" and not settings.IS_LOCAL_RUNTIME:
        data = _missing_local_inventory()
        data["message"] = "로컬 원문 메타데이터는 로컬 실행에서만 확인할 수 있습니다."
        return data
    archive = (
        _local_archive(query, period)
        if inventory != "database" and settings.IS_LOCAL_RUNTIME
        else None
    )
    try:
        records = SourceItem.objects.filter(
            source__topic_code=TOPIC,
            retention_tombstoned_at__isnull=True,
        )
        unknown_date_count = records.filter(published_at__isnull=True).count()
        drafts = DraftArticle.objects.filter(
            topic_code=TOPIC,
            current_revision__isnull=False,
            current_revision__article_id=F("id"),
        )
        runs_query = CollectionRun.objects.filter(topic_code=TOPIC)
        if period is not None and period.active:
            field = "first_collected_at" if period.basis == "collected" else "published_at"
            records = records.filter(**{f"{field}__gte": period.start, f"{field}__lt": period.end})
            drafts = drafts.filter(updated_at__gte=period.start, updated_at__lt=period.end)
            runs_query = runs_query.filter(window_start__lt=period.end, window_end__gt=period.start)
        last_seen = records.aggregate(value=Max("first_collected_at"))["value"]
        if query:
            records = records.filter(Q(title__icontains=query) | Q(publisher__icontains=query))
            drafts = drafts.filter(
                Q(current_revision__title__icontains=query)
                | Q(current_revision__summary__icontains=query)
            )
        total = records.count()
        news_rows = list(
            records.order_by(
                F("published_at").desc(nulls_last=True), "-first_collected_at", "-id"
            ).values(
                "title",
                "publisher",
                "canonical_url",
                "published_at",
                "first_collected_at",
                "status",
                "source__base_url",
                "source__default_rights_status",
            )[:60]
        )
        draft_rows = list(
            drafts.order_by("-updated_at", "-id").values(
                "id", "current_revision__title", "current_revision__summary", "state"
            )[:30]
        )
        source_rows = list(
            SourceDefinition.objects.filter(topic_code=TOPIC)
            .order_by("display_name", "id")
            .values("display_name", "publisher", "base_url", "enabled", "default_rights_status")
        )
        runs = list(
            runs_query.order_by("-created_at", "-id").values(
                "id", "display_id", "state", "created_at", "completed_at"
            )[:5]
        )
    except DatabaseError:
        if archive is not None:
            archive["drafts_available"] = False
            return archive
        if inventory == "local":
            return _missing_local_inventory()
        return {
            "available": False,
            "message": (
                "반도체 수집 데이터를 아직 불러올 수 없습니다. "
                "아래 출처는 저장소의 설정 목록이며, 실제 수집 상태를 뜻하지 않습니다."
            ),
            "total": None,
            "news": [],
            "drafts": [],
            "sources": _configured_sources(),
            "runs": [],
            "last_seen": None,
            "inventory_kind": "database_versions",
            "inventory_label": "저장된 뉴스 버전",
            "unknown_date_count": None,
        }

    status_labels = dict(SourceItemStatus.choices)
    result = {
        "available": True,
        "message": (
            "저장된 원문 버전 목록입니다. 같은 소식의 이전 버전이 포함될 수 있습니다."
            if total
            else "조건에 맞는 저장된 원문 버전이 없습니다."
        ),
        "total": total,
        "news": [
            {
                "title": row["title"],
                "publisher": row["publisher"],
                "url": _source_url(row["canonical_url"], row["source__base_url"]),
                "published_at": row["published_at"],
                "first_collected_at": row["first_collected_at"],
                "status": row["status"],
                "status_label": status_labels.get(row["status"], "상태 미상"),
                "rights_label": _rights_label(row["source__default_rights_status"]),
            }
            for row in news_rows
        ],
        "drafts": [
            {
                "id": str(row["id"]),
                "title": row["current_revision__title"],
                "summary": row["current_revision__summary"],
                "state": row["state"],
            }
            for row in draft_rows
        ],
        "sources": [
            _source_row(
                name=row["display_name"],
                publisher=row["publisher"],
                url=row["base_url"],
                enabled=row["enabled"],
                rights=row["default_rights_status"],
            )
            for row in source_rows
        ],
        "runs": [{**row, "id": str(row["id"])} for row in runs],
        "last_seen": last_seen,
        "inventory_kind": "database_versions",
        "inventory_label": "저장된 뉴스 버전",
        "unknown_date_count": unknown_date_count,
        "drafts_available": True,
    }
    if period is not None and period.active and period.basis == "published" and unknown_date_count:
        result["message"] += (
            f" 발표일 미상인 저장 버전 {unknown_date_count}건은 기간에 배정하지 않았습니다."
        )
    if inventory == "local" and archive is None:
        archive = _missing_local_inventory()
    if archive is not None:
        if inventory == "local" or archive["available"] or total == 0:
            archive["drafts"] = result["drafts"]
            archive["runs"] = result["runs"]
            archive["drafts_available"] = True
            return archive
        # An unavailable local refresh is evidence of failure, not another news
        # inventory to merge into database version counts.
        result["message"] += (
            " 로컬 수집은 실패·검증 불가 상태여서 저장된 데이터베이스 버전을 표시합니다."
        )
        for field in ("source_outcomes", "outcome_summary", "last_attempt_at"):
            if field in archive:
                result[field] = archive[field]
    return result
