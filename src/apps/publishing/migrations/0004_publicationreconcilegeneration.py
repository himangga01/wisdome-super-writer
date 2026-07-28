import django.db.models.deletion
import uuid

from django.db import migrations, models


TERMINAL_ATTEMPT_STATES = {
    "succeeded",
    "permanent_failed",
    "manual_required",
    "stale",
}
TERMINAL_RECEIPT_STATES = {
    "succeeded",
    "dead_letter",
}


def _manual_required(
    Attempt,
    Publication,
    attempt,
    code,
    *,
    db_alias,
    reconcile_attempt_no=None,
):
    persisted_counter = (
        attempt.reconcile_attempt_no
        if reconcile_attempt_no is None
        else reconcile_attempt_no
    )
    Attempt.objects.using(db_alias).filter(pk=attempt.pk).update(
        state="manual_required",
        reconcile_attempt_no=min(max(persisted_counter, 1), 5),
        error_code=code,
    )
    Publication.objects.using(db_alias).filter(
        pk=attempt.publication_id
    ).update(
        state="manual_required",
        last_error_code=code,
    )


def bind_existing_reconcile_events(apps, schema_editor):
    Attempt = apps.get_model("publishing", "PublicationAttempt")
    Publication = apps.get_model("publishing", "Publication")
    Generation = apps.get_model(
        "publishing",
        "PublicationReconcileGeneration",
    )
    Event = apps.get_model("infrastructure", "OutboxMessage")
    Receipt = apps.get_model("infrastructure", "OutboxConsumerReceipt")
    db_alias = schema_editor.connection.alias

    attempts = Attempt.objects.using(db_alias).order_by("created_at", "id")
    for attempt in attempts.iterator():
        events = list(
            Event.objects.using(db_alias).filter(
                topic="publication.reconcile_requested",
                aggregate_id=attempt.id,
            ).order_by("occurred_at", "id")
        )
        if not events:
            if attempt.reconcile_attempt_no:
                _manual_required(
                    Attempt,
                    Publication,
                    attempt,
                    "legacy_reconcile_event_missing",
                    db_alias=db_alias,
                )
            continue

        assignments = []
        invalid = False
        next_generation = 1
        for event in events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            if payload.get("publication_attempt_id") != str(attempt.id):
                invalid = True
                break
            if event.event_version == 1:
                generation = next_generation
            elif event.event_version == 2:
                generation = payload.get("reconcile_attempt_no")
                if type(generation) is not int:
                    invalid = True
                    break
            else:
                invalid = True
                break
            if generation != next_generation or generation > 5:
                invalid = True
                break
            assignments.append((generation, event))
            next_generation += 1

        if invalid or attempt.reconcile_attempt_no > len(assignments):
            _manual_required(
                Attempt,
                Publication,
                attempt,
                "legacy_reconcile_generation_ambiguous",
                db_alias=db_alias,
            )
            continue

        latest_delivery_dead_lettered = False
        latest_generation_completed = False
        for index, (generation, event) in enumerate(assignments):
            receipt = (
                Receipt.objects.using(db_alias).filter(
                    event_id=event.id,
                    consumer_name="publication-reconcile",
                )
                .first()
            )
            delivery_dead_lettered = (
                event.status == "dead_letter"
                or (
                    receipt is not None
                    and receipt.state == "dead_letter"
                )
            )
            is_latest = index == len(assignments) - 1
            if is_latest:
                latest_delivery_dead_lettered = delivery_dead_lettered
            completed = (
                index < len(assignments) - 1
                or attempt.state in TERMINAL_ATTEMPT_STATES
                or delivery_dead_lettered
                or (
                    receipt is not None
                    and receipt.state in TERMINAL_RECEIPT_STATES
                )
            )
            completed_at = None
            if is_latest:
                latest_generation_completed = completed
            if completed:
                if receipt is not None:
                    completed_at = (
                        receipt.completed_at
                        or receipt.dead_lettered_at
                    )
                if completed_at is None and delivery_dead_lettered:
                    completed_at = event.dead_lettered_at
                if completed_at is None and index < len(assignments) - 1:
                    completed_at = assignments[index + 1][1].occurred_at
                completed_at = (
                    completed_at
                    or attempt.finished_at
                    or event.occurred_at
                )
            result_state = attempt.state if completed else ""
            if (
                is_latest
                and delivery_dead_lettered
                and attempt.state not in TERMINAL_ATTEMPT_STATES
            ):
                result_state = "manual_required"
            Generation.objects.using(db_alias).create(
                publication_attempt_id=attempt.id,
                generation=generation,
                source_event_id=event.id,
                state="completed" if completed else "started",
                result_state=result_state,
                not_before=event.not_before,
                started_at=event.occurred_at,
                completed_at=completed_at,
            )
        Attempt.objects.using(db_alias).filter(pk=attempt.pk).update(
            reconcile_attempt_no=len(assignments)
        )
        if (
            latest_delivery_dead_lettered
            and attempt.state not in TERMINAL_ATTEMPT_STATES
        ):
            _manual_required(
                Attempt,
                Publication,
                attempt,
                "legacy_reconcile_delivery_dead_letter",
                db_alias=db_alias,
                reconcile_attempt_no=len(assignments),
            )
        elif (
            not latest_generation_completed
            and attempt.state not in TERMINAL_ATTEMPT_STATES
        ):
            Attempt.objects.using(db_alias).filter(
                pk=attempt.pk
            ).update(state="reconciling")
            Publication.objects.using(db_alias).filter(
                pk=attempt.publication_id
            ).update(state="reconciling")


class Migration(migrations.Migration):
    dependencies = [
        ("infrastructure", "0002_outboxconsumerreceipt_and_more"),
        ("publishing", "0003_publicationattempt_reconcile_attempt_no"),
    ]

    operations = [
        migrations.CreateModel(
            name="PublicationReconcileGeneration",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("generation", models.PositiveSmallIntegerField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("started", "시작"),
                            ("completed", "완료"),
                        ],
                        default="started",
                        max_length=16,
                    ),
                ),
                ("result_identity", models.CharField(blank=True, max_length=64)),
                ("result_state", models.CharField(blank=True, max_length=24)),
                ("not_before", models.DateTimeField()),
                ("started_at", models.DateTimeField()),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "publication_attempt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reconcile_generations",
                        to="publishing.publicationattempt",
                    ),
                ),
                (
                    "source_event",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="publication_reconcile_generation",
                        to="infrastructure.outboxmessage",
                    ),
                ),
            ],
            options={
                "ordering": [
                    "publication_attempt_id",
                    "generation",
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "publication_attempt",
                            "generation",
                        ),
                        name="uq_publication_reconcile_generation",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("generation__gte", 1),
                            ("generation__lte", 5),
                        ),
                        name="ck_publication_reconcile_generation_1_5",
                    ),
                ],
            },
        ),
        migrations.RunPython(
            bind_existing_reconcile_events,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="publicationattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("reconcile_attempt_no__lte", 5),
                ),
                name="ck_publication_reconcile_attempt_no_lte_5",
            ),
        ),
    ]
