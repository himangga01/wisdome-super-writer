from django.db import migrations
from django.utils import timezone


LEGACY_AMBIGUOUS_CODES = {
    "legacy_reconcile_event_missing",
    "legacy_reconcile_generation_ambiguous",
}
TERMINAL_ATTEMPT_STATES = {
    "succeeded",
    "permanent_failed",
    "manual_required",
    "stale",
}
TERMINAL_RECEIPT_STATES = {"succeeded", "dead_letter"}
SUCCESS_PUBLICATION_STATE_BY_ACTION = {
    "create": "published",
    "update": "published",
    "mark_withdrawn": "marked_withdrawn",
    "unpublish": "withdrawn",
}


def _event_generation(event, attempt_id, expected_generation):
    payload = event.payload if isinstance(event.payload, dict) else {}
    if payload.get("publication_attempt_id") != str(attempt_id):
        return None
    if event.event_version == 1:
        generation = expected_generation
    elif event.event_version == 2:
        generation = payload.get("reconcile_attempt_no")
    else:
        return None
    if (
        type(generation) is not int
        or generation != expected_generation
        or generation < 1
        or generation > 5
    ):
        return None
    return generation


def _event_order(event):
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_version == 1:
        return (0, 0, event.occurred_at, str(event.id))
    generation = payload.get("reconcile_attempt_no")
    if event.event_version == 2 and type(generation) is int:
        return (1, generation, event.occurred_at, str(event.id))
    return (2, 0, event.occurred_at, str(event.id))


def _settle_event(Receipt, event, *, db_alias, code):
    now = timezone.now()
    receipt = (
        Receipt.objects.using(db_alias)
        .select_for_update()
        .filter(
            event_id=event.id,
            consumer_name="publication-reconcile",
        )
        .first()
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
            "reconcile generation repair requires quiesced dispatchers "
            f"and consumers for event {event.id}"
        )
    if receipt is not None and receipt.state == "succeeded":
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
        return "succeeded", receipt.completed_at or event.published_at
    if receipt is not None and receipt.state == "dead_letter":
        event.status = "dead_letter"
        event.published_at = None
        event.last_error_code = (
            receipt.last_error_code
            or event.last_error_code
            or code
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
        return "dead_letter", receipt.dead_lettered_at or event.dead_lettered_at
    if receipt is None:
        receipt = Receipt.objects.using(db_alias).create(
            event_id=event.id,
            consumer_name="publication-reconcile",
            state="dead_letter",
            attempts=1,
            last_error_code=code,
            dead_lettered_at=now,
        )
    else:
        updated = Receipt.objects.using(db_alias).filter(
            pk=receipt.pk,
            state__in=("processing", "retry"),
        ).update(
            state="dead_letter",
            attempts=max(receipt.attempts, 1),
            last_error_code=code,
            next_retry_at=None,
            completed_at=None,
            dead_lettered_at=now,
            claimed_at=None,
            claimed_until=None,
            lease_token=None,
        )
        if updated != 1:
            receipt.refresh_from_db(using=db_alias)
            return _settle_event(
                Receipt,
                event,
                db_alias=db_alias,
                code=code,
            )
    event.status = "dead_letter"
    event.published_at = None
    event.last_error_code = code
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
    return "dead_letter", now


def _complete_delivery_generation(
    Generation,
    generation,
    *,
    db_alias,
    completed_at,
):
    updates = {
        "state": "completed",
        "completed_at": completed_at or timezone.now(),
    }
    if not generation.result_identity:
        updates["result_state"] = ""
    Generation.objects.using(db_alias).filter(pk=generation.pk).update(
        **updates
    )


def _has_durable_success_proof(
    Intent,
    attempt,
    publication,
    generations,
    *,
    db_alias,
):
    if any(
        generation.state == "completed"
        and generation.result_state == "succeeded"
        and bool(generation.result_identity)
        for generation in generations
    ):
        return True
    if (
        attempt.finished_at is None
        or publication.last_success_at is None
        or attempt.finished_at != publication.last_success_at
    ):
        return False
    revision_no = (
        Intent.objects.using(db_alias)
        .filter(pk=attempt.publication_intent_id)
        .values_list("revision_no", flat=True)
        .first()
    )
    if (
        revision_no is None
        or publication.published_revision_no != revision_no
    ):
        return False
    if attempt.resolved_action == "unpublish":
        return bool(publication.remote_post_id) or (
            publication.remote_state in {"withdrawn", "deleted"}
        )
    return bool(publication.remote_post_id)


def _is_latest_attempt(Attempt, attempt, *, db_alias):
    latest_id = (
        Attempt.objects.using(db_alias)
        .filter(publication_id=attempt.publication_id)
        .order_by("-created_at", "-id")
        .values_list("id", flat=True)
        .first()
    )
    return latest_id == attempt.id


def _restore_success_projection(
    Attempt,
    Publication,
    attempt,
    publication,
    *,
    db_alias,
):
    Attempt.objects.using(db_alias).filter(pk=attempt.id).update(
        state="succeeded",
        error_code="",
        error_detail_redacted="",
    )
    if (
        not _is_latest_attempt(Attempt, attempt, db_alias=db_alias)
        or publication.state != "manual_required"
        or publication.last_error_code not in LEGACY_AMBIGUOUS_CODES
    ):
        return
    publication_state = SUCCESS_PUBLICATION_STATE_BY_ACTION.get(
        attempt.resolved_action
    )
    if publication_state is None:
        return
    Publication.objects.using(db_alias).filter(pk=publication.id).update(
        state=publication_state,
        last_error_code="",
    )


def repair_ambiguous_generations(apps, schema_editor):
    Attempt = apps.get_model("publishing", "PublicationAttempt")
    Publication = apps.get_model("publishing", "Publication")
    Intent = apps.get_model("publishing", "PublicationIntent")
    Generation = apps.get_model(
        "publishing",
        "PublicationReconcileGeneration",
    )
    Event = apps.get_model("infrastructure", "OutboxMessage")
    Receipt = apps.get_model("infrastructure", "OutboxConsumerReceipt")
    db_alias = schema_editor.connection.alias
    attempts = (
        Attempt.objects.using(db_alias)
        .select_for_update()
        .order_by(
            "created_at",
            "id",
        )
    )
    for attempt in attempts.iterator():
        publication = (
            Publication.objects.using(db_alias)
            .select_for_update()
            .get(pk=attempt.publication_id)
        )
        events = list(
            Event.objects.using(db_alias)
            .select_for_update()
            .filter(
                topic="publication.reconcile_requested",
                aggregate_id=attempt.id,
            )
            .order_by("occurred_at", "id")
        )
        events.sort(key=_event_order)
        existing = list(
            Generation.objects.using(db_alias)
            .select_for_update()
            .select_related("source_event")
            .filter(publication_attempt_id=attempt.id)
            .order_by("generation")
        )
        existing_by_event = {
            generation.source_event_id: generation
            for generation in existing
        }
        existing_by_number = {
            generation.generation: generation
            for generation in existing
        }
        prefix = []
        invalid_events = []
        expected_generation = 1
        prefix_broken = False
        for event in events:
            bound = existing_by_event.get(event.id)
            generation_number = _event_generation(
                event,
                attempt.id,
                expected_generation,
            )
            if (
                prefix_broken
                or generation_number is None
                or (
                    bound is not None
                    and bound.generation != expected_generation
                )
                or (
                    bound is None
                    and expected_generation in existing_by_number
                )
            ):
                prefix_broken = True
                invalid_events.append(event)
                continue
            prefix.append((generation_number, event, bound))
            expected_generation += 1

        prefix_existing_ids = {
            generation.id
            for _, _, generation in prefix
            if generation is not None
        }
        orphan_generations = [
            generation
            for generation in existing
            if generation.id not in prefix_existing_ids
        ]
        new_bindings = [
            (number, event)
            for number, event, generation in prefix
            if generation is None
        ]
        prefix_count = len(prefix)
        receipt_states = {
            receipt.event_id: receipt.state
            for receipt in (
                Receipt.objects.using(db_alias)
                .select_for_update()
                .filter(
                    event_id__in=[event.id for _, event, _ in prefix],
                    consumer_name="publication-reconcile",
                )
                .only("event_id", "state")
            )
        }
        prefix_has_dead_letter = any(
            event.status == "dead_letter"
            or receipt_states.get(event.id) == "dead_letter"
            for _, event, _ in prefix
        )
        incomplete_terminal_generations = any(
            generation is not None
            and generation.state != "completed"
            and (
                event.status == "dead_letter"
                or receipt_states.get(event.id)
                in TERMINAL_RECEIPT_STATES
            )
            for _, event, generation in prefix
        )
        synthetic_results = [
            generation
            for generation in existing
            if not generation.result_identity
            and bool(generation.result_state)
        ]
        legacy_manual = (
            attempt.state == "manual_required"
            and attempt.error_code in LEGACY_AMBIGUOUS_CODES
        )
        restored_success = bool(
            legacy_manual
            and _has_durable_success_proof(
                Intent,
                attempt,
                publication,
                existing,
                db_alias=db_alias,
            )
        )
        effective_attempt_state = (
            "succeeded" if restored_success else attempt.state
        )
        success_projection_needs_restore = bool(
            effective_attempt_state == "succeeded"
            and publication.state == "manual_required"
            and publication.last_error_code in LEGACY_AMBIGUOUS_CODES
            and _is_latest_attempt(
                Attempt,
                attempt,
                db_alias=db_alias,
            )
        )
        ambiguous_repair = bool(
            invalid_events
            or orphan_generations
            or attempt.reconcile_attempt_no > prefix_count
            or legacy_manual
            or prefix_has_dead_letter
        )
        requires_manual = bool(
            ambiguous_repair
            and effective_attempt_state not in TERMINAL_ATTEMPT_STATES
        )
        requires_repair = bool(
            new_bindings
            or invalid_events
            or orphan_generations
            or attempt.reconcile_attempt_no != prefix_count
            or legacy_manual
            or incomplete_terminal_generations
            or synthetic_results
            or prefix_has_dead_letter
            or success_projection_needs_restore
        )
        if not requires_repair:
            continue

        for generation_number, event in new_bindings:
            generation = Generation.objects.using(db_alias).create(
                publication_attempt_id=attempt.id,
                generation=generation_number,
                source_event_id=event.id,
                state="started",
                result_identity="",
                result_state="",
                not_before=event.not_before,
                started_at=event.occurred_at,
            )
            for index, item in enumerate(prefix):
                if item[1].id == event.id:
                    prefix[index] = (
                        item[0],
                        item[1],
                        generation,
                    )
                    break

        for generation in synthetic_results:
            Generation.objects.using(db_alias).filter(
                pk=generation.pk
            ).update(result_state="")

        if ambiguous_repair:
            for _, event, generation in prefix:
                _, completed_at = _settle_event(
                    Receipt,
                    event,
                    db_alias=db_alias,
                    code="legacy_reconcile_generation_ambiguous",
                )
                _complete_delivery_generation(
                    Generation,
                    generation,
                    db_alias=db_alias,
                    completed_at=completed_at,
                )
        else:
            for _, event, generation in prefix:
                receipt_state = receipt_states.get(event.id)
                if (
                    event.status == "dead_letter"
                    or receipt_state in TERMINAL_RECEIPT_STATES
                ):
                    _, completed_at = _settle_event(
                        Receipt,
                        event,
                        db_alias=db_alias,
                        code="legacy_reconcile_delivery_terminal",
                    )
                    _complete_delivery_generation(
                        Generation,
                        generation,
                        db_alias=db_alias,
                        completed_at=completed_at,
                    )
        for event in invalid_events:
            _settle_event(
                Receipt,
                event,
                db_alias=db_alias,
                code="legacy_reconcile_event_unbound",
            )
        for generation in orphan_generations:
            _, completed_at = _settle_event(
                Receipt,
                generation.source_event,
                db_alias=db_alias,
                code="legacy_reconcile_generation_orphaned",
            )
            _complete_delivery_generation(
                Generation,
                generation,
                db_alias=db_alias,
                completed_at=completed_at,
            )

        highest_bound = max(
            (generation.generation for generation in existing),
            default=0,
        )
        highest_bound = max(highest_bound, prefix_count)
        update_fields = {"reconcile_attempt_no": highest_bound}
        if restored_success:
            update_fields.update(
                state="succeeded",
                error_code="",
                error_detail_redacted="",
            )
        elif requires_manual:
            completed_at = attempt.finished_at or timezone.now()
            update_fields.update(
                state="manual_required",
                error_code="legacy_reconcile_generation_ambiguous",
                finished_at=completed_at,
            )
            Publication.objects.using(db_alias).filter(
                pk=attempt.publication_id
            ).update(
                state="manual_required",
                last_error_code="legacy_reconcile_generation_ambiguous",
            )
        Attempt.objects.using(db_alias).filter(pk=attempt.id).update(
            **update_fields
        )
        if restored_success or success_projection_needs_restore:
            _restore_success_projection(
                Attempt,
                Publication,
                attempt,
                publication,
                db_alias=db_alias,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0004_publicationreconcilegeneration"),
        ("infrastructure", "0002_outboxconsumerreceipt_and_more"),
    ]

    operations = [
        migrations.RunPython(
            repair_ambiguous_generations,
            migrations.RunPython.noop,
        ),
    ]
