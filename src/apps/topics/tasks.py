from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from adapters.sources import build_source_adapter

from .models import SourceDefinitionSnapshot
from .services import record_source_check_result


@shared_task(name="apps.topics.tasks.check_source_snapshot")
def check_source_snapshot(
    source_id: str,
    source_snapshot_id: str,
    source_config_hash: str,
    check_id: str,
):
    del check_id
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
        adapter = build_source_adapter(
            snapshot,
            runtime_mode="source_check",
        )
        external_config = snapshot.frozen_config.get(
            "externalConfig",
            {},
        )
        source_check_days = int(
            external_config.get("sourceCheckDays", 1)
        )
        if source_check_days < 1 or source_check_days > 365:
            raise ValueError("sourceCheckDays is outside its approved range.")
        records = adapter.collect(
            since=now - timedelta(days=source_check_days),
            until=now,
        )
        if not records:
            raise ValueError(
                "Source check returned no contract-valid records."
            )
    except Exception as exc:
        return record_source_check_result(
            source_id=source_id,
            source_snapshot_id=source_snapshot_id,
            source_config_hash=source_config_hash,
            status="failed",
            record_count=0,
            error_code=exc.__class__.__name__,
        )
    return record_source_check_result(
        source_id=source_id,
        source_snapshot_id=source_snapshot_id,
        source_config_hash=source_config_hash,
        status="passed",
        record_count=len(records),
        error_code=None,
    )
