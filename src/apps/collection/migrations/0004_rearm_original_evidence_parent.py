import hashlib
import hmac
import unicodedata

import rfc8785
from django.db import migrations
from django.utils import timezone


RECOVERY_REASON = "deployment_recovery_original_parent_rearmed"
SUPERSEDED_REASON = "deployment_recovery_parent_superseded"


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


def _persisted_material_hash(event):
    material = {
        "event_id": str(event.id),
        "event_type": event.topic,
        "event_version": event.event_version,
        "occurred_at": event.occurred_at.isoformat(),
        "correlation_id": str(event.correlation_id),
        "causation_id": (
            str(event.causation_id) if event.causation_id else None
        ),
        "job_id": str(event.job_id),
        "entity_type": event.aggregate_type,
        "entity_id": str(event.aggregate_id),
        "operation": event.operation,
        "dedupe_key": event.message_key,
        "policy_versions": event.policy_versions,
        "not_before": event.not_before.isoformat(),
        "payload": event.payload,
    }
    return hashlib.sha256(
        rfc8785.dumps(_normalize_json(material))
    ).hexdigest()


def _validate_event(event, run_id):
    if (
        event.topic != "run.evidence_requested"
        or event.event_version != 1
        or event.aggregate_type != "collection_run"
        or event.aggregate_id != run_id
        or event.payload != {"run_id": str(run_id)}
        or not hmac.compare_digest(
            event.immutable_material_hash,
            _persisted_material_hash(event),
        )
    ):
        raise RuntimeError(
            "evidence parent recovery found conflicting immutable material "
            f"for event {event.id}"
        )


def _original_events(OutboxMessage, db_alias, run_id):
    return list(
        OutboxMessage.objects.using(db_alias)
        .select_for_update()
        .filter(message_key=f"run.evidence_requested:{run_id}")
    )


def _receipt(
    OutboxConsumerReceipt,
    db_alias,
    event_id,
):
    receipt, _ = (
        OutboxConsumerReceipt.objects.using(db_alias)
        .select_for_update()
        .get_or_create(
            event_id=event_id,
            consumer_name="run-evidence",
        )
    )
    return receipt


def _existing_receipt(
    OutboxConsumerReceipt,
    db_alias,
    event_id,
):
    return (
        OutboxConsumerReceipt.objects.using(db_alias)
        .select_for_update()
        .filter(
            event_id=event_id,
            consumer_name="run-evidence",
        )
        .first()
    )


def _assert_quiesced(
    OutboxConsumerReceipt,
    db_alias,
    event,
):
    now = timezone.now()
    receipt = _existing_receipt(
        OutboxConsumerReceipt,
        db_alias,
        event.id,
    )
    active_dispatch = bool(
        event.status == "dispatching"
        and event.claimed_until is not None
        and event.claimed_until > now
    )
    active_consumer = bool(
        receipt is not None
        and receipt.state == "processing"
        and receipt.claimed_until is not None
        and receipt.claimed_until > now
    )
    if active_dispatch or active_consumer:
        raise RuntimeError(
            "evidence parent recovery requires quiesced dispatchers and "
            f"consumers for event {event.id}"
        )
    return receipt


def _fence_event(
    OutboxConsumerReceipt,
    db_alias,
    event,
):
    now = timezone.now()
    receipt = _receipt(
        OutboxConsumerReceipt,
        db_alias,
        event.id,
    )
    if receipt.state == "succeeded":
        event.status = "published"
        event.published_at = (
            event.published_at
            or receipt.completed_at
            or now
        )
        event.claimed_at = None
        event.claimed_until = None
        event.lease_owner = ""
        event.lease_token = None
        event.last_error_code = None
        event.last_error_at = None
        event.dead_lettered_at = None
        event.save(
            using=db_alias,
            update_fields=(
                "status",
                "published_at",
                "claimed_at",
                "claimed_until",
                "lease_owner",
                "lease_token",
                "last_error_code",
                "last_error_at",
                "dead_lettered_at",
            ),
        )
        return
    if receipt.state == "dead_letter":
        event.status = "dead_letter"
        event.published_at = None
        event.last_error_code = (
            receipt.last_error_code
            or event.last_error_code
            or SUPERSEDED_REASON
        )
        event.last_error_at = event.last_error_at or now
        event.dead_lettered_at = (
            event.dead_lettered_at
            or receipt.dead_lettered_at
            or now
        )
        event.claimed_at = None
        event.claimed_until = None
        event.lease_owner = ""
        event.lease_token = None
        event.save(
            using=db_alias,
            update_fields=(
                "status",
                "published_at",
                "last_error_code",
                "last_error_at",
                "dead_lettered_at",
                "claimed_at",
                "claimed_until",
                "lease_owner",
                "lease_token",
            ),
        )
        return
    receipt.state = "dead_letter"
    receipt.attempts = max(receipt.attempts, 1)
    receipt.last_error_code = SUPERSEDED_REASON
    receipt.next_retry_at = None
    receipt.completed_at = None
    receipt.dead_lettered_at = now
    receipt.claimed_at = None
    receipt.claimed_until = None
    receipt.lease_token = None
    receipt.save(
        using=db_alias,
        update_fields=(
            "state",
            "attempts",
            "last_error_code",
            "next_retry_at",
            "completed_at",
            "dead_lettered_at",
            "claimed_at",
            "claimed_until",
            "lease_token",
        ),
    )
    event.status = "dead_letter"
    event.published_at = None
    event.last_error_code = SUPERSEDED_REASON
    event.last_error_at = now
    event.dead_lettered_at = now
    event.claimed_at = None
    event.claimed_until = None
    event.lease_owner = ""
    event.lease_token = None
    event.save(
        using=db_alias,
        update_fields=(
            "status",
            "published_at",
            "last_error_code",
            "last_error_at",
            "dead_lettered_at",
            "claimed_at",
            "claimed_until",
            "lease_owner",
            "lease_token",
        ),
    )


def _project_original(
    OutboxConsumerReceipt,
    db_alias,
    event,
    *,
    completed,
):
    now = timezone.now()
    receipt = _receipt(
        OutboxConsumerReceipt,
        db_alias,
        event.id,
    )
    if completed:
        _fence_event(
            OutboxConsumerReceipt,
            db_alias,
            event,
        )
        return
    if event.attempts >= 32767:
        raise RuntimeError(
            "evidence parent recovery cannot extend an exhausted delivery "
            f"generation for event {event.id}"
        )
    receipt.state = "retry"
    receipt.attempts = 0
    receipt.completed_at = None
    receipt.last_error_code = RECOVERY_REASON
    event.status = "pending"
    event.published_at = None
    event.max_attempts = max(event.max_attempts, event.attempts + 1)
    event.available_at = now
    event.last_error_code = RECOVERY_REASON
    event.last_error_at = now
    receipt.next_retry_at = None
    receipt.dead_lettered_at = None
    receipt.claimed_at = None
    receipt.claimed_until = None
    receipt.lease_token = None
    receipt.save(
        using=db_alias,
        update_fields=(
            "state",
            "attempts",
            "last_error_code",
            "next_retry_at",
            "completed_at",
            "dead_lettered_at",
            "claimed_at",
            "claimed_until",
            "lease_token",
        ),
    )
    event.dead_lettered_at = None
    event.claimed_at = None
    event.claimed_until = None
    event.lease_owner = ""
    event.lease_token = None
    event.save(
        using=db_alias,
        update_fields=(
            "status",
            "max_attempts",
            "published_at",
            "available_at",
            "last_error_code",
            "last_error_at",
            "dead_lettered_at",
            "claimed_at",
            "claimed_until",
            "lease_owner",
            "lease_token",
        ),
    )


def _fail_parent_recovery(
    run,
    step,
    *,
    db_alias,
    error_code,
    detail,
):
    now = timezone.now()
    step.state = "failed"
    step.error_code = error_code
    step.error_detail_redacted = detail
    step.finished_at = now
    step.save(
        using=db_alias,
        update_fields=(
            "state",
            "error_code",
            "error_detail_redacted",
            "finished_at",
        ),
    )
    run.state = "failed"
    run.error_summary = {
        "stage": "extract",
        "code": error_code,
    }
    run.completed_at = now
    run.save(
        using=db_alias,
        update_fields=(
            "state",
            "error_summary",
            "completed_at",
        ),
    )


def _stop_parent_recovery(run, step, *, db_alias):
    now = timezone.now()
    if step is not None:
        step.state = "stopped"
        step.error_code = "stop_requested"
        step.error_detail_redacted = (
            "evidence recovery stopped by request"
        )
        step.finished_at = step.finished_at or now
        step.save(
            using=db_alias,
            update_fields=(
                "state",
                "error_code",
                "error_detail_redacted",
                "finished_at",
            ),
        )
    run.state = "stopped"
    run.error_summary = None
    run.completed_at = run.completed_at or now
    run.save(
        using=db_alias,
        update_fields=(
            "state",
            "error_summary",
            "completed_at",
        ),
    )


def _has_child_events(OutboxMessage, db_alias, event):
    return OutboxMessage.objects.using(db_alias).filter(
        causation_id=event.id
    ).exists()


def converge_parent_recovery(apps, schema_editor):
    Run = apps.get_model("collection", "CollectionRun")
    RunStep = apps.get_model("collection", "RunStep")
    OutboxMessage = apps.get_model("infrastructure", "OutboxMessage")
    OutboxConsumerReceipt = apps.get_model(
        "infrastructure",
        "OutboxConsumerReceipt",
    )
    db_alias = schema_editor.connection.alias
    recovery_by_run = {}
    recovery_events = (
        OutboxMessage.objects.using(db_alias)
        .select_for_update()
        .filter(
            topic="run.evidence_requested",
            message_key__startswith=(
                "run.evidence_requested:recovery:fanout-marker-v1:"
            ),
        )
        .order_by("occurred_at", "id")
    )
    for event in recovery_events.iterator():
        payload = event.payload if isinstance(event.payload, dict) else {}
        run_id = event.aggregate_id
        if payload.get("run_id") != str(run_id):
            raise RuntimeError(
                "superseded evidence recovery event payload is ambiguous"
            )
        _validate_event(event, run_id)
        expected_key = (
            "run.evidence_requested:recovery:fanout-marker-v1:"
            f"{run_id}"
        )
        if event.message_key != expected_key:
            raise RuntimeError(
                "superseded evidence recovery event key is ambiguous"
            )
        if run_id in recovery_by_run:
            raise RuntimeError(
                "multiple evidence parent recovery events exist for "
                f"run {run_id}"
            )
        recovery_by_run[run_id] = event

    steps = (
        RunStep.objects.using(db_alias)
        .select_for_update()
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
    by_run = {step.run_id: step for step in steps.iterator()}
    all_run_ids = set(recovery_by_run) | set(by_run)
    for run_id in sorted(all_run_ids, key=str):
        run = (
            Run.objects.using(db_alias)
            .select_for_update()
            .filter(pk=run_id)
            .first()
        )
        if run is None:
            raise RuntimeError(
                f"evidence parent recovery references missing run {run_id}"
            )
        step = (
            RunStep.objects.using(db_alias)
            .select_for_update()
            .filter(
                run_id=run_id,
                name="extract",
                attempt_no=1,
            )
            .first()
        )
        originals = _original_events(
            OutboxMessage,
            db_alias,
            run_id,
        )
        if len(originals) > 1:
            raise RuntimeError(
                "multiple original evidence parent events exist for "
                f"run {run_id}"
            )
        if originals:
            _validate_event(originals[0], run_id)
        recovery = recovery_by_run.get(run_id)
        parents = [*originals]
        if recovery is not None:
            parents.append(recovery)
        receipts = {
            parent.id: _assert_quiesced(
                OutboxConsumerReceipt,
                db_alias,
                parent,
            )
            for parent in parents
        }
        incomplete = bool(
            run.state == "extracting"
            and step is not None
            and step.state == "running"
            and step.started_at is not None
            and step.fanout_completed_at is None
        )
        if run.stop_requested_at is not None or run.state == "stopping":
            for parent in parents:
                _fence_event(
                    OutboxConsumerReceipt,
                    db_alias,
                    parent,
                )
            if step is None:
                step = RunStep.objects.using(db_alias).create(
                    run_id=run.id,
                    name="extract",
                    attempt_no=1,
                    state="queued",
                )
            _stop_parent_recovery(run, step, db_alias=db_alias)
            continue
        if not incomplete:
            for parent in parents:
                _fence_event(
                    OutboxConsumerReceipt,
                    db_alias,
                    parent,
                )
            if run.state in {"completed", "failed", "stopped"}:
                continue
            if (
                step is not None
                and step.fanout_completed_at is not None
            ):
                continue
            if step is None:
                step = RunStep.objects.using(db_alias).create(
                    run_id=run.id,
                    name="extract",
                    attempt_no=1,
                    state="running",
                    started_at=timezone.now(),
                )
            _fail_parent_recovery(
                run,
                step,
                db_alias=db_alias,
                error_code="deployment_recovery_parent_ambiguous",
                detail=(
                    "evidence parent recovery state cannot be proven safe"
                ),
            )
            continue
        if originals and recovery is not None:
            original = originals[0]
            original_has_children = _has_child_events(
                OutboxMessage,
                db_alias,
                original,
            )
            recovery_has_children = _has_child_events(
                OutboxMessage,
                db_alias,
                recovery,
            )
            if original_has_children and recovery_has_children:
                raise RuntimeError(
                    "evidence parent recovery found mixed child causation "
                    f"for run {run_id}"
                )
            recovery_receipt = receipts.get(recovery.id)
            if recovery_has_children or (
                not original_has_children
                and recovery_receipt is not None
                and recovery_receipt.state == "succeeded"
            ):
                canonical = recovery
                noncanonical = original
            else:
                canonical = original
                noncanonical = recovery
            _fence_event(
                OutboxConsumerReceipt,
                db_alias,
                noncanonical,
            )
        elif originals:
            canonical = originals[0]
        elif recovery is not None:
            canonical = recovery
        else:
            _fail_parent_recovery(
                run,
                step,
                db_alias=db_alias,
                error_code="deployment_recovery_parent_missing",
                detail=(
                    "in-flight evidence fan-out has no recoverable "
                    "parent event"
                ),
            )
            continue
        _project_original(
            OutboxConsumerReceipt,
            db_alias,
            canonical,
            completed=False,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("collection", "0003_recover_inflight_evidence_fanout"),
        ("evidence", "0003_evidenceasset_raw_input_fingerprint"),
        ("infrastructure", "0002_outboxconsumerreceipt_and_more"),
    ]

    operations = [
        migrations.RunPython(
            converge_parent_recovery,
            migrations.RunPython.noop,
        ),
    ]
