import hashlib
import json
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from adapters.sources import build_source_adapter
from adapters.sources.errors import SourceAccessError

from .models import SourceDefinitionSnapshot
from .services import record_source_check_result


@shared_task(name="apps.topics.tasks.check_source_snapshot")
def check_source_snapshot(
    source_id: str,
    source_snapshot_id: str,
    source_config_hash: str,
    check_id: str,
):
    snapshot = SourceDefinitionSnapshot.objects.select_related("source").get(
        pk=source_snapshot_id,
        source_id=source_id,
    )
    if snapshot.config_hash != source_config_hash:
        return record_source_check_result(
            source_id=source_id,
            source_snapshot_id=source_snapshot_id,
            source_config_hash=source_config_hash,
            status="failed",
            record_count=0,
            error_code="snapshot_hash_mismatch",
        )

    now = timezone.now()
    try:
        frozen_config = snapshot.frozen_config
        if (
            isinstance(frozen_config, dict)
            and frozen_config.get("defaultRightsStatus")
            == "prohibited"
        ):
            return record_source_check_result(
                source_id=source_id,
                source_snapshot_id=source_snapshot_id,
                source_config_hash=source_config_hash,
                status="failed",
                record_count=0,
                error_code="source_rights_prohibited",
                rights_decision="prohibited",
            )
        adapter = build_source_adapter(
            snapshot,
            runtime_mode="source_check",
            operation_id=check_id,
        )
        external_config = snapshot.frozen_config.get(
            "externalConfig",
            {},
        )
        source_check_days = int(
            external_config.get("sourceCheckDays", 1)
        )
        if source_check_days < 1 or source_check_days > 365:
            raise SourceAccessError(
                code="source_check_window_invalid",
                category="policy",
                detail="Frozen source check window is invalid.",
                remediation=(
                    "Approve a sourceCheckDays value between 1 and 365."
                ),
            )
        try:
            records = adapter.collect(
                since=now - timedelta(days=source_check_days),
                until=now,
            )
        except BaseException:
            try:
                adapter.close()
            except SourceAccessError:
                pass
            raise
        else:
            adapter.close()
        if not records:
            raise SourceAccessError(
                code="source_check_no_records",
                category="schema",
                detail=(
                    "Source check returned no contract-valid records."
                ),
                remediation=(
                    "Review the frozen response contract and check window."
                ),
            )
    except SourceAccessError as exc:
        result = record_source_check_result(
            source_id=source_id,
            source_snapshot_id=source_snapshot_id,
            source_config_hash=source_config_hash,
            status="failed",
            record_count=0,
            error_code=exc.code,
            rights_decision=snapshot.frozen_config.get(
                "defaultRightsStatus"
            ),
        )
        if exc.retryable:
            raise
        return result
    http_metadata = [
        record.http_metadata
        for record in records
        if isinstance(record.http_metadata, dict)
    ]

    def first_http_value(key: str):
        pending = list(http_metadata)
        while pending:
            value = pending.pop(0)
            if key in value and isinstance(value[key], str):
                return value[key][:500]
            pending.extend(
                item
                for item in value.values()
                if isinstance(item, dict)
            )
        return None

    content_hash = hashlib.sha256(
        json.dumps(
            sorted(
                (
                    record.external_id,
                    record.content_hash,
                    record.raw_checksum,
                )
                for record in records
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return record_source_check_result(
        source_id=source_id,
        source_snapshot_id=source_snapshot_id,
        source_config_hash=source_config_hash,
        status="passed",
        record_count=len(records),
        error_code=None,
        etag=first_http_value("etag"),
        last_modified=first_http_value("lastModified"),
        content_hash=content_hash,
        rights_decision=snapshot.frozen_config.get(
            "defaultRightsStatus"
        ),
    )


@shared_task(
    name="apps.topics.tasks.finalize_source_check_delivery_failure"
)
def finalize_source_check_delivery_failure(
    source_id: str,
    source_snapshot_id: str,
    source_config_hash: str,
    check_id: str,
    error_code: str,
):
    del check_id
    return record_source_check_result(
        source_id=source_id,
        source_snapshot_id=source_snapshot_id,
        source_config_hash=source_config_hash,
        status="failed",
        record_count=0,
        error_code=str(error_code)[:120],
    )
