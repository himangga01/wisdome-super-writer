import hashlib
import hmac
import unicodedata

import rfc8785
from django.db import migrations
from django.utils import timezone


RECOVERY_ERROR_CODE = "deployment_recovery_parent_ambiguous"
REARM_REASON = "deployment_recovery_draft_redelivery"
DRAFT_TOPIC = "run.draft_requested"
DRAFT_CONSUMER = "article-draft"
SCHEDULED_TOPIC = "publication.scheduled_run_requested"
SCHEDULED_CONSUMER = "scheduled-publication"


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


def _event_for_run(
    OutboxMessage,
    db_alias,
    run_id,
    *,
    topic,
):
    message_key = f"{topic}:{run_id}"
    event = (
        OutboxMessage.objects.using(db_alias)
        .select_for_update()
        .filter(message_key=message_key)
        .first()
    )
    if event is None:
        return None
    try:
        material_matches = hmac.compare_digest(
            event.immutable_material_hash,
            _persisted_material_hash(event),
        )
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        event.topic != topic
        or event.event_version != 1
        or event.aggregate_type != "collection_run"
        or event.aggregate_id != run_id
        or event.message_key != message_key
        or event.payload != {"run_id": str(run_id)}
        or not material_matches
    ):
        return None
    return event


def _event_receipt(
    OutboxConsumerReceipt,
    db_alias,
    event,
    *,
    consumer_name,
):
    return (
        OutboxConsumerReceipt.objects.using(db_alias)
        .select_for_update()
        .filter(
            event_id=event.id,
            consumer_name=consumer_name,
        )
        .first()
    )


def _has_active_delivery(event, receipt):
    now = timezone.now()
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
    return active_dispatch or active_consumer


def _project_succeeded_event(
    OutboxConsumerReceipt,
    event,
    receipt,
    *,
    db_alias,
    consumer_name,
):
    if _has_active_delivery(event, receipt):
        return False
    now = timezone.now()
    if receipt is None:
        receipt = OutboxConsumerReceipt.objects.using(db_alias).create(
            event_id=event.id,
            consumer_name=consumer_name,
            state="succeeded",
            attempts=1,
            started_at=now,
            completed_at=now,
        )
    else:
        receipt.state = "succeeded"
        receipt.attempts = max(receipt.attempts, 1)
        receipt.last_error_code = ""
        receipt.next_retry_at = None
        receipt.started_at = receipt.started_at or now
        receipt.completed_at = receipt.completed_at or now
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
                "started_at",
                "completed_at",
                "dead_lettered_at",
                "claimed_at",
                "claimed_until",
                "lease_token",
            ),
        )
    event.status = "published"
    event.published_at = (
        event.published_at
        or receipt.completed_at
        or now
    )
    event.last_error_code = None
    event.last_error_at = None
    event.dead_lettered_at = None
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
    return True


def _rearm_event(
    OutboxConsumerReceipt,
    db_alias,
    event,
    *,
    consumer_name,
):
    if event.attempts >= 32767:
        return False
    now = timezone.now()
    receipt, _ = (
        OutboxConsumerReceipt.objects.using(db_alias)
        .select_for_update()
        .get_or_create(
            event_id=event.id,
            consumer_name=consumer_name,
        )
    )
    receipt.state = "retry"
    receipt.attempts = 0
    receipt.last_error_code = REARM_REASON
    receipt.next_retry_at = None
    receipt.completed_at = None
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
    event.status = "pending"
    event.max_attempts = max(
        event.max_attempts,
        event.attempts + 1,
    )
    event.published_at = None
    event.available_at = now
    event.last_error_code = REARM_REASON
    event.last_error_at = now
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
    return True


def _has_durable_fanout_counters(counters):
    if not isinstance(counters, dict):
        return False
    for key in ("evidence", "extractionFailures"):
        value = counters.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if value < 0 or value > 2147483647:
            return False
    return True


def _converge_stopping_runs(Run, RunStep, db_alias):
    # Deployment recovery runs with workers quiesced. Terminal step rows are
    # deliberately excluded so their historical outcome remains intact.
    runs = (
        Run.objects.using(db_alias)
        .select_for_update()
        .filter(
            state="stopping",
            stop_requested_at__isnull=False,
        )
        .order_by("id")
    )
    for run in runs.iterator():
        now = timezone.now()
        active_steps = (
            RunStep.objects.using(db_alias)
            .select_for_update()
            .filter(
                run_id=run.id,
                state__in=("queued", "running"),
            )
            .order_by("id")
        )
        for step in active_steps.iterator():
            step.state = "stopped"
            step.error_code = "stop_requested"
            step.error_detail_redacted = (
                "step stopped during deployment recovery"
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


def _is_exact_legacy_recovery_failure(run, step):
    if step is None or run.stop_requested_at is not None:
        return False
    summary = run.error_summary
    if not isinstance(summary, dict):
        return False
    return bool(
        set(summary) == {"stage", "code"}
        and summary.get("stage") == "extract"
        and summary.get("code") == RECOVERY_ERROR_CODE
        and run.completed_at is not None
        and step.state == "failed"
        and step.error_code == RECOVERY_ERROR_CODE
        and step.error_detail_redacted
        == "evidence parent recovery state cannot be proven safe"
        and step.finished_at is not None
        and step.fanout_completed_at is None
    )


def _restore_extract_projection(
    run,
    step,
    db_alias,
    *,
    run_state,
):
    now = timezone.now()
    evidence_count = run.counters["evidence"]
    failure_count = run.counters["extractionFailures"]
    fanout_completed_at = (
        step.finished_at
        or step.started_at
        or now
    )
    step.state = "failed" if evidence_count == 0 else "succeeded"
    step.output_count = evidence_count
    step.error_code = (
        "partial_extraction_failure" if failure_count else None
    )
    step.error_detail_redacted = (
        f"failed document/generic attempts: {failure_count}"
        if failure_count
        else None
    )
    step.started_at = step.started_at or fanout_completed_at
    step.fanout_completed_at = fanout_completed_at
    step.finished_at = step.finished_at or now
    step.save(
        using=db_alias,
        update_fields=(
            "state",
            "output_count",
            "error_code",
            "error_detail_redacted",
            "started_at",
            "fanout_completed_at",
            "finished_at",
        ),
    )
    run.state = run_state
    run.error_summary = None
    run.completed_at = None
    run.save(
        using=db_alias,
        update_fields=(
            "state",
            "error_summary",
            "completed_at",
        ),
    )


def converge_legacy_terminal_recovery(apps, schema_editor):
    Run = apps.get_model("collection", "CollectionRun")
    RunStep = apps.get_model("collection", "RunStep")
    DraftArticle = apps.get_model("editorial", "DraftArticle")
    PublicationAttempt = apps.get_model(
        "publishing",
        "PublicationAttempt",
    )
    OutboxMessage = apps.get_model("infrastructure", "OutboxMessage")
    OutboxConsumerReceipt = apps.get_model(
        "infrastructure",
        "OutboxConsumerReceipt",
    )
    db_alias = schema_editor.connection.alias

    _converge_stopping_runs(Run, RunStep, db_alias)

    # This pass repairs only the exact parent-ambiguous projection left by
    # an already-applied 0004. It is not a general retry of that migration.
    failed_runs = (
        Run.objects.using(db_alias)
        .select_for_update()
        .filter(state="failed")
        .order_by("id")
    )
    for run in failed_runs.iterator():
        step = (
            RunStep.objects.using(db_alias)
            .select_for_update()
            .filter(
                run_id=run.id,
                name="extract",
                attempt_no=1,
            )
            .first()
        )
        if not _is_exact_legacy_recovery_failure(run, step):
            continue
        if not _has_durable_fanout_counters(run.counters):
            # Without durable fan-out completion evidence, keep the
            # migration failure terminal and fail closed.
            continue
        draft_event = _event_for_run(
            OutboxMessage,
            db_alias,
            run.id,
            topic=DRAFT_TOPIC,
        )
        if draft_event is None:
            continue
        draft_receipt = _event_receipt(
            OutboxConsumerReceipt,
            db_alias,
            draft_event,
            consumer_name=DRAFT_CONSUMER,
        )
        article_id = (
            DraftArticle.objects.using(db_alias)
            .select_for_update()
            .filter(
                source_run_id=run.id,
                current_revision_id__isnull=False,
            )
            .values_list("id", flat=True)
            .first()
        )

        scheduled_event = _event_for_run(
            OutboxMessage,
            db_alias,
            run.id,
            topic=SCHEDULED_TOPIC,
        )
        scheduled_receipt = None
        if scheduled_event is not None:
            scheduled_receipt = _event_receipt(
                OutboxConsumerReceipt,
                db_alias,
                scheduled_event,
                consumer_name=SCHEDULED_CONSUMER,
            )
        dispatched_attempt_id = (
            PublicationAttempt.objects.using(db_alias)
            .select_for_update()
            .filter(
                publication_intent__origin_collection_run_id=run.id,
                publication_intent__state="dispatched",
            )
            .values_list("id", flat=True)
            .first()
        )

        if article_id is None:
            if dispatched_attempt_id is not None:
                continue
            if _has_active_delivery(draft_event, draft_receipt):
                continue
            if not _rearm_event(
                OutboxConsumerReceipt,
                db_alias,
                draft_event,
                consumer_name=DRAFT_CONSUMER,
            ):
                continue
            run_state = "validating"
        else:
            if _has_active_delivery(draft_event, draft_receipt):
                continue
            if dispatched_attempt_id is not None:
                if (
                    run.trigger != "schedule"
                    or run.approval_mode != "validated_auto"
                    or scheduled_event is None
                    or _has_active_delivery(
                        scheduled_event,
                        scheduled_receipt,
                    )
                ):
                    continue
                run_state = "publishing"
            else:
                if (
                    scheduled_event is not None
                    and _has_active_delivery(
                        scheduled_event,
                        scheduled_receipt,
                    )
                ):
                    continue
                run_state = "awaiting_approval"

            if not _project_succeeded_event(
                OutboxConsumerReceipt,
                draft_event,
                draft_receipt,
                db_alias=db_alias,
                consumer_name=DRAFT_CONSUMER,
            ):
                continue
            if run_state == "publishing" and not _project_succeeded_event(
                OutboxConsumerReceipt,
                scheduled_event,
                scheduled_receipt,
                db_alias=db_alias,
                consumer_name=SCHEDULED_CONSUMER,
            ):
                continue

        _restore_extract_projection(
            run,
            step,
            db_alias,
            run_state=run_state,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("collection", "0004_rearm_original_evidence_parent"),
        ("editorial", "0001_initial"),
        ("publishing", "0005_repair_ambiguous_reconcile_generations"),
    ]

    operations = [
        migrations.RunPython(
            converge_legacy_terminal_recovery,
            migrations.RunPython.noop,
        ),
    ]
