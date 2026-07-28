import hashlib
import hmac
import unicodedata
import uuid

import rfc8785
from django.db import migrations
from django.utils import timezone


def _normalize_json(value):
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            normalized_key = unicodedata.normalize("NFC", str(key))
            if normalized_key in normalized:
                raise ValueError(
                    "canonical JSON keys collide after NFC normalization"
                )
            normalized[normalized_key] = _normalize_json(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(
        f"unsupported canonical JSON type: {type(value).__name__}"
    )


def _material_hash(
    *,
    event_id,
    event_type,
    occurred_at,
    correlation_id,
    job_id,
    entity_type,
    entity_id,
    dedupe_key,
    not_before,
    payload,
):
    material = {
        "event_id": str(event_id),
        "event_type": event_type,
        "event_version": 1,
        "occurred_at": occurred_at.isoformat(),
        "correlation_id": str(correlation_id),
        "causation_id": None,
        "job_id": str(job_id),
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "operation": "process",
        "dedupe_key": dedupe_key,
        "policy_versions": {},
        "not_before": not_before.isoformat(),
        "payload": payload,
    }
    return hashlib.sha256(
        rfc8785.dumps(_normalize_json(material))
    ).hexdigest()


def _enqueue_recovery_event(
    OutboxMessage,
    db_alias,
    *,
    event_type,
    run_id,
    dedupe_key,
):
    payload = {"run_id": str(run_id)}
    existing = (
        OutboxMessage.objects.using(db_alias)
        .filter(message_key=dedupe_key)
        .first()
    )
    if existing is not None:
        expected_hash = _material_hash(
            event_id=existing.id,
            event_type=event_type,
            occurred_at=existing.occurred_at,
            correlation_id=run_id,
            job_id=run_id,
            entity_type="collection_run",
            entity_id=run_id,
            dedupe_key=dedupe_key,
            not_before=existing.not_before,
            payload=payload,
        )
        if not hmac.compare_digest(
            existing.immutable_material_hash,
            expected_hash,
        ):
            raise RuntimeError(
                "recovery outbox dedupe key has conflicting immutable material"
            )
        return existing

    event_id = uuid.uuid4()
    occurred_at = timezone.now()
    material_hash = _material_hash(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        correlation_id=run_id,
        job_id=run_id,
        entity_type="collection_run",
        entity_id=run_id,
        dedupe_key=dedupe_key,
        not_before=occurred_at,
        payload=payload,
    )
    return OutboxMessage.objects.using(db_alias).create(
        id=event_id,
        message_key=dedupe_key,
        topic=event_type,
        event_version=1,
        occurred_at=occurred_at,
        aggregate_type="collection_run",
        aggregate_id=run_id,
        payload=payload,
        correlation_id=run_id,
        causation_id=None,
        job_id=run_id,
        operation="process",
        policy_versions={},
        immutable_material_hash=material_hash,
        not_before=occurred_at,
        available_at=occurred_at,
        status="pending",
        max_attempts=5,
    )


def enqueue_recovery_events(apps, schema_editor):
    RunStep = apps.get_model("collection", "RunStep")
    OutboxMessage = apps.get_model("infrastructure", "OutboxMessage")
    db_alias = schema_editor.connection.alias
    steps = (
        RunStep.objects.using(db_alias)
        .select_related("run")
        .filter(
            run__state="extracting",
            name="extract",
            attempt_no=1,
            state="running",
            started_at__isnull=False,
            fanout_completed_at__isnull=True,
        )
        .order_by("run_id")
    )
    for step in steps.iterator():
        run_id = step.run_id
        counters = (
            step.run.counters
            if isinstance(step.run.counters, dict)
            else {}
        )
        if "evidence" in counters and "extractionFailures" in counters:
            step.fanout_completed_at = (
                step.finished_at
                or step.started_at
                or timezone.now()
            )
            step.save(
                using=db_alias,
                update_fields=("fanout_completed_at",),
            )
            _enqueue_recovery_event(
                OutboxMessage,
                db_alias,
                event_type="evidence.finalize_requested",
                run_id=run_id,
                dedupe_key=(
                    "evidence.finalize_requested:recovery:"
                    f"fanout-marker-v1:{run_id}"
                ),
            )
        else:
            _enqueue_recovery_event(
                OutboxMessage,
                db_alias,
                event_type="run.evidence_requested",
                run_id=run_id,
                dedupe_key=(
                    "run.evidence_requested:recovery:"
                    f"fanout-marker-v1:{run_id}"
                ),
            )


class Migration(migrations.Migration):
    dependencies = [
        ("collection", "0002_runstep_fanout_completed_at"),
        ("evidence", "0002_documentextraction_input_fingerprint"),
        ("infrastructure", "0002_outboxconsumerreceipt_and_more"),
    ]

    operations = [
        migrations.RunPython(
            enqueue_recovery_events,
            migrations.RunPython.noop,
        ),
    ]
