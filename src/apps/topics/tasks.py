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
        records = build_source_adapter(snapshot).collect(
            since=now - timedelta(minutes=5),
            until=now,
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
