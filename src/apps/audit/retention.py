from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_VERSION, canonical_hash

from .models import RetentionBatch, RetentionBatchItem, RetentionHold


def load_retention_policy() -> dict:
    path = settings.BASE_DIR.parent / "config" / "retention" / "default.json"
    if not path.exists():
        path = Path("config/retention/default.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _is_held(entity_type: str, entity_id) -> bool:
    now = timezone.now()
    return (
        RetentionHold.objects.filter(scope_type=entity_type, active=True)
        .filter(Q(scope_id__isnull=True) | Q(scope_id=entity_id))
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .exists()
    )


@transaction.atomic
def create_retention_preview(*, request_key: str, user) -> RetentionBatch:
    existing = RetentionBatch.objects.filter(request_key=request_key).first()
    if existing:
        return existing
    policy = load_retention_policy()
    now = timezone.now()
    raw_cutoff = now - timedelta(days=int(policy["rawEvidenceDays"]))
    candidates: list[tuple[str, object, str | None]] = []

    from apps.collection.models import SourceItem
    from apps.evidence.models import EvidenceAsset

    candidates.extend(
        ("source_item", row.id, None)
        for row in SourceItem.objects.filter(first_collected_at__lt=raw_cutoff).exclude(body_text="")
    )
    candidates.extend(
        ("evidence_asset", row.id, row.object_key)
        for row in EvidenceAsset.objects.filter(created_at__lt=raw_cutoff).exclude(
            extracted_text__isnull=True, object_key__isnull=True
        )
    )
    manifest = [
        {"entityType": kind, "entityId": str(entity_id), "objectKey": key}
        for kind, entity_id, key in candidates
    ]
    batch = RetentionBatch.objects.create(
        policy_version=int(policy["version"]),
        policy_hash=canonical_hash(
            policy, schema_version=CANONICAL_HASH_SCHEMA_VERSION
        ),
        cutoff_at=raw_cutoff,
        request_key=request_key,
        preview_manifest_hash=canonical_hash(
            manifest, schema_version=CANONICAL_HASH_SCHEMA_VERSION
        ),
        counters={"candidate": len(candidates), "held": 0, "purged": 0},
        requested_by=user,
    )
    RetentionBatchItem.objects.bulk_create(
        [
            RetentionBatchItem(batch=batch, entity_type=kind, entity_id=entity_id, object_key=key)
            for kind, entity_id, key in candidates
        ]
    )
    return batch


@transaction.atomic
def approve_retention_batch(batch_id, *, expected_version: int) -> RetentionBatch:
    batch = RetentionBatch.objects.select_for_update().get(id=batch_id)
    if batch.row_version != expected_version or batch.state != RetentionBatch.State.PREVIEW:
        raise ValueError("stale_retention_batch")
    batch.state = RetentionBatch.State.APPROVED
    batch.row_version += 1
    batch.approved_at = timezone.now()
    batch.save(update_fields=["state", "row_version", "approved_at"])
    return batch


@transaction.atomic
def execute_retention_batch(batch_id) -> RetentionBatch:
    from apps.collection.models import SourceItem
    from apps.evidence.models import EvidenceAsset

    batch = RetentionBatch.objects.select_for_update().get(id=batch_id)
    if batch.state not in {RetentionBatch.State.APPROVED, RetentionBatch.State.RUNNING}:
        return batch
    batch.state = RetentionBatch.State.RUNNING
    batch.started_at = batch.started_at or timezone.now()
    batch.save(update_fields=["state", "started_at"])
    held = purged = failed = 0
    for item in batch.items.select_for_update().filter(state=RetentionBatchItem.State.CANDIDATE):
        if _is_held(item.entity_type, item.entity_id):
            item.state = RetentionBatchItem.State.HELD
            item.reason_code = "active_hold"
            held += 1
        else:
            try:
                if item.entity_type == "source_item":
                    SourceItem.objects.filter(id=item.entity_id).update(body_text="")
                elif item.entity_type == "evidence_asset":
                    EvidenceAsset.objects.filter(id=item.entity_id).update(
                        extracted_text=None, structured_data=None, object_key=None, object_version=None
                    )
                item.state = RetentionBatchItem.State.PURGED
                purged += 1
            except Exception:
                item.state = RetentionBatchItem.State.FAILED
                item.reason_code = "purge_failed"
                failed += 1
        item.processed_at = timezone.now()
        item.save(update_fields=["state", "reason_code", "processed_at"])
    batch.counters = {
        "candidate": batch.items.count(),
        "held": held,
        "purged": purged,
        "failed": failed,
    }
    batch.state = RetentionBatch.State.FAILED if failed else RetentionBatch.State.COMPLETED
    batch.completed_at = timezone.now()
    batch.save(update_fields=["counters", "state", "completed_at"])
    return batch


@shared_task
def execute_retention_batch_task(batch_id: str):
    batch = execute_retention_batch(batch_id)
    return {"batchId": str(batch.id), "state": batch.state, "counters": batch.counters}
