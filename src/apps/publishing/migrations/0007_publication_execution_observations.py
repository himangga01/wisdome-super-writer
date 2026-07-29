import django.db.models.deletion
import uuid

from django.db import migrations, models


RECOVERY_CHOICES = [
    ("not_required", "Not required"),
    ("in_progress", "In progress"),
    ("automatic_retry", "Automatic retry"),
    ("reconciling", "Reconciling"),
    ("manual_required", "Manual required"),
    ("stopped", "Stopped"),
]
DIRECT_EXECUTION_RESULT_STATES = frozenset(
    {
        "succeeded",
        "retryable_failed",
        "permanent_failed",
        "unknown_outcome",
    }
)
ATTEMPT_TERMINAL_STATES = frozenset(
    {
        "succeeded",
        "permanent_failed",
        "manual_required",
        "stale",
    }
)


def _duration_ms(started_at, finished_at):
    if (
        started_at is None
        or finished_at is None
        or finished_at < started_at
    ):
        return None
    return int((finished_at - started_at).total_seconds() * 1000)


def _recovery_state(state):
    if state == "succeeded":
        return "not_required"
    if state in {"queued", "running"}:
        return "in_progress"
    if state == "retryable_failed":
        return "automatic_retry"
    if state in {"unknown_outcome", "reconciling"}:
        return "reconciling"
    if state in {"permanent_failed", "manual_required"}:
        return "manual_required"
    return "stopped"


def _terminal_impact(
    stage,
    state,
    error_code,
    *,
    scope="publication_channel",
):
    return {
        "scope": scope,
        "stage": stage,
        "final_state": state,
        "affected_count": 1,
        "error_code": (error_code or "")[:100],
    }


def _matching_publication_event(Event, attempt, execution_attempt_no, db_alias):
    events = Event.objects.using(db_alias).filter(
        topic="publication.requested",
        aggregate_id=attempt.id,
    )
    exact = events.filter(
        message_key=(
            f"publication.requested:{attempt.id}:{execution_attempt_no}"
        )
    ).first()
    if exact is not None:
        return exact
    if attempt.started_at is not None:
        prior = (
            events.filter(occurred_at__lte=attempt.started_at)
            .order_by("-occurred_at", "-id")
            .first()
        )
        if prior is not None:
            return prior
    return events.order_by("occurred_at", "id").first()


def backfill_publication_observations(apps, schema_editor):
    Attempt = apps.get_model("publishing", "PublicationAttempt")
    Execution = apps.get_model(
        "publishing",
        "PublicationExecutionObservation",
    )
    Generation = apps.get_model(
        "publishing",
        "PublicationReconcileGeneration",
    )
    Event = apps.get_model("infrastructure", "OutboxMessage")
    db_alias = schema_editor.connection.alias

    generations = Generation.objects.using(db_alias).select_related(
        "source_event"
    )
    for generation in generations.iterator():
        executed = (
            generation.state == "completed"
            and bool(generation.result_identity)
            and bool(generation.result_state)
        )
        duration_ms = (
            _duration_ms(
                generation.started_at,
                generation.completed_at,
            )
            if executed
            else None
        )
        error_code = (
            generation.source_event.last_error_code or ""
        )[:100]
        delivery_failed = (
            generation.state == "completed"
            and not executed
            and (
                generation.source_event.status == "dead_letter"
                or bool(error_code)
            )
        )
        if executed:
            recovery_state = _recovery_state(generation.result_state)
            impact = _terminal_impact(
                "reconcile",
                generation.result_state,
                error_code,
            )
        elif generation.state != "completed":
            recovery_state = "reconciling"
            impact = {}
        elif delivery_failed:
            recovery_state = "manual_required"
            impact = _terminal_impact(
                "reconcile_delivery",
                "delivery_failed",
                error_code,
                scope="publication_delivery",
            )
        else:
            recovery_state = "stopped"
            impact = {}
        Generation.objects.using(db_alias).filter(
            pk=generation.pk
        ).update(
            correlation_id=generation.source_event.correlation_id,
            duration_ms=duration_ms,
            error_code=error_code,
            terminal_impact=impact,
            recovery_state=recovery_state,
            next_recovery_at=(
                generation.not_before
                if generation.state == "started"
                else None
            ),
        )

    attempts = (
        Attempt.objects.using(db_alias)
        .select_related("publication_intent")
        .order_by("created_at", "id")
    )
    for attempt in attempts.iterator():
        first_event = (
            Event.objects.using(db_alias)
            .filter(
                aggregate_id=attempt.id,
                topic__in=(
                    "publication.requested",
                    "publication.reconcile_requested",
                ),
            )
            .order_by("occurred_at", "id")
            .first()
        )
        intent = attempt.publication_intent
        correlation_id = (
            first_event.correlation_id
            if first_event is not None
            else (
                intent.origin_collection_run_id
                or intent.correction_case_id
                or intent.id
            )
        )
        duration_ms = _duration_ms(
            attempt.started_at,
            attempt.finished_at,
        )
        completed_execution_retries = max(attempt.attempt_no - 1, 0)
        if attempt.state in {
            "queued",
            "running",
            "retryable_failed",
            "stale",
        }:
            completed_execution_retries = max(
                attempt.attempt_no - 2,
                0,
            )
        attempt_generations = list(
            Generation.objects.using(db_alias)
            .filter(publication_attempt_id=attempt.id)
            .order_by("generation")
        )
        executed_generations = [
            generation
            for generation in attempt_generations
            if (
                generation.state == "completed"
                and bool(generation.result_identity)
                and bool(generation.result_state)
            )
        ]
        completed_reconciles = len(executed_generations)
        recovery_state = _recovery_state(attempt.state)
        direct_execution_terminal = (
            attempt.state in DIRECT_EXECUTION_RESULT_STATES
            and duration_ms is not None
            and not executed_generations
        )
        known_durations = [
            generation.duration_ms
            for generation in executed_generations
            if generation.duration_ms is not None
        ]
        if direct_execution_terminal:
            known_durations.insert(0, duration_ms)
        aggregate_duration_ms = (
            sum(known_durations) if known_durations else None
        )
        if executed_generations:
            latest_execution = executed_generations[-1]
            impact = _terminal_impact(
                "reconcile",
                latest_execution.result_state,
                latest_execution.error_code,
            )
        elif direct_execution_terminal:
            impact = _terminal_impact(
                "execution",
                attempt.state,
                attempt.error_code,
            )
        elif (
            attempt.state in ATTEMPT_TERMINAL_STATES
            and duration_ms is not None
        ):
            impact = _terminal_impact(
                "aggregate",
                attempt.state,
                attempt.error_code,
            )
        else:
            impact = {}
        Attempt.objects.using(db_alias).filter(pk=attempt.pk).update(
            correlation_id=correlation_id,
            duration_ms=aggregate_duration_ms,
            retry_count=(
                completed_execution_retries + completed_reconciles
            ),
            terminal_impact=impact,
            recovery_state=recovery_state,
            next_recovery_at=(
                attempt.next_retry_at
                if recovery_state
                in {"automatic_retry", "reconciling"}
                else None
            ),
        )

        if attempt.started_at is None:
            continue
        open_execution = attempt.state == "running"
        if not open_execution and not direct_execution_terminal:
            continue
        execution_attempt_no = attempt.attempt_no
        if attempt.state == "retryable_failed" and execution_attempt_no > 1:
            execution_attempt_no -= 1
        source_event = _matching_publication_event(
            Event,
            attempt,
            execution_attempt_no,
            db_alias,
        )
        Execution.objects.using(db_alias).create(
            publication_attempt_id=attempt.id,
            execution_attempt_no=execution_attempt_no,
            correlation_id=(
                source_event.correlation_id
                if source_event is not None
                else correlation_id
            ),
            source_event_id=(
                source_event.id if source_event is not None else None
            ),
            started_at=attempt.started_at,
            finished_at=(
                None if open_execution else attempt.finished_at
            ),
            duration_ms=(
                None if open_execution else duration_ms
            ),
            result_state=(
                "" if open_execution else attempt.state
            ),
            error_code=(
                "" if open_execution else attempt.error_code[:100]
            ),
            retry_at=(
                None if open_execution else attempt.next_retry_at
            ),
            terminal_impact=(
                {}
                if open_execution
                else _terminal_impact(
                    "execution",
                    attempt.state,
                    attempt.error_code,
                )
            ),
            recovery_state=(
                "in_progress" if open_execution else recovery_state
            ),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("infrastructure", "0002_outboxconsumerreceipt_and_more"),
        ("publishing", "0006_restore_legacy_terminal_projection"),
    ]

    operations = [
        migrations.AddField(
            model_name="publicationattempt",
            name="correlation_id",
            field=models.UUIDField(db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="publicationattempt",
            name="duration_ms",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="publicationattempt",
            name="next_recovery_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="publicationattempt",
            name="recovery_state",
            field=models.CharField(
                choices=RECOVERY_CHOICES,
                default="in_progress",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="publicationattempt",
            name="retry_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="publicationattempt",
            name="terminal_impact",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="publicationreconcilegeneration",
            name="correlation_id",
            field=models.UUIDField(db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="publicationreconcilegeneration",
            name="duration_ms",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="publicationreconcilegeneration",
            name="error_code",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="publicationreconcilegeneration",
            name="next_recovery_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="publicationreconcilegeneration",
            name="recovery_state",
            field=models.CharField(
                choices=RECOVERY_CHOICES,
                default="in_progress",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="publicationreconcilegeneration",
            name="terminal_impact",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="publicationreconcilegeneration",
            name="worker_task_id",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.CreateModel(
            name="PublicationExecutionObservation",
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
                ("execution_attempt_no", models.PositiveIntegerField()),
                ("correlation_id", models.UUIDField(db_index=True)),
                ("worker_task_id", models.CharField(blank=True, max_length=255)),
                ("started_at", models.DateTimeField()),
                (
                    "finished_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "duration_ms",
                    models.PositiveBigIntegerField(blank=True, null=True),
                ),
                ("result_state", models.CharField(blank=True, max_length=24)),
                ("error_code", models.CharField(blank=True, max_length=100)),
                (
                    "retry_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "terminal_impact",
                    models.JSONField(blank=True, default=dict),
                ),
                (
                    "recovery_state",
                    models.CharField(
                        choices=RECOVERY_CHOICES,
                        default="in_progress",
                        max_length=32,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "publication_attempt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="execution_observations",
                        to="publishing.publicationattempt",
                    ),
                ),
                (
                    "source_event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="publication_execution_observations",
                        to="infrastructure.outboxmessage",
                    ),
                ),
            ],
            options={
                "ordering": [
                    "publication_attempt_id",
                    "execution_attempt_no",
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "publication_attempt",
                            "execution_attempt_no",
                        ),
                        name="uq_publication_execution_observation",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("execution_attempt_no__gte", 1)
                        ),
                        name="ck_publication_execution_attempt_no_gte_1",
                    ),
                ],
            },
        ),
        migrations.RunPython(
            backfill_publication_observations,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="publicationattempt",
            name="correlation_id",
            field=models.UUIDField(db_index=True),
        ),
        migrations.AlterField(
            model_name="publicationreconcilegeneration",
            name="correlation_id",
            field=models.UUIDField(db_index=True),
        ),
    ]
