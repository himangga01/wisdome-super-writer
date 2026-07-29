import uuid

from django.db import migrations, models


TERMINAL_RUN_STATES = frozenset({"completed", "failed", "stopped"})
TERMINAL_STEP_STATES = frozenset({"succeeded", "failed", "stopped"})


def _duration_ms(started_at, finished_at):
    if started_at is None or finished_at is None:
        return None
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    return duration_ms if duration_ms >= 0 else None


def _safe_code(value, *, max_length):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_length:
        return None
    return value


def _bounded_count(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, 0), 2147483647)


def _run_correlation(OutboxMessage, db_alias, run):
    events = OutboxMessage.objects.using(db_alias).filter(
        aggregate_type="collection_run",
        aggregate_id=run.id,
    )
    event = (
        events.filter(topic="run.requested")
        .order_by("occurred_at", "id")
        .first()
    )
    if event is None:
        event = events.order_by("occurred_at", "id").first()
    return event.correlation_id if event is not None else run.id


def backfill_collection_observability(apps, schema_editor):
    CollectionRun = apps.get_model("collection", "CollectionRun")
    RunStep = apps.get_model("collection", "RunStep")
    OutboxMessage = apps.get_model("infrastructure", "OutboxMessage")
    db_alias = schema_editor.connection.alias

    runs = CollectionRun.objects.using(db_alias).order_by("id")
    for run in runs.iterator():
        correlation_id = _run_correlation(
            OutboxMessage,
            db_alias,
            run,
        )
        terminal = run.state in TERMINAL_RUN_STATES
        summary = run.error_summary if isinstance(run.error_summary, dict) else {}
        stage = _safe_code(summary.get("stage"), max_length=64) or "run"
        error_code = _safe_code(summary.get("code"), max_length=100)
        if run.state == "stopped" and error_code is None:
            error_code = "stop_requested"
        terminal_impact = {}
        if terminal:
            terminal_impact = {
                "scope": "run",
                "stage": stage,
                "final_state": run.state,
                "affected_count": 0,
                "error_code": error_code,
            }
        CollectionRun.objects.using(db_alias).filter(pk=run.pk).update(
            correlation_id=correlation_id,
            duration_ms=_duration_ms(run.started_at, run.completed_at),
            retry_count=1 if run.trigger == "retry" else 0,
            terminal_impact=terminal_impact,
            recovery_state=(
                "manual_required"
                if run.state == "failed"
                else (
                    "stopped"
                    if run.state == "stopped"
                    else (
                        "not_required"
                        if run.state == "completed"
                        else "in_progress"
                    )
                )
            ),
            next_recovery_at=None,
        )

    steps = RunStep.objects.using(db_alias).select_related("run").order_by("id")
    for step in steps.iterator():
        terminal = step.state in TERMINAL_STEP_STATES
        error_code = _safe_code(step.error_code, max_length=100)
        affected_count = 0
        if step.state in {"failed", "stopped"}:
            affected_count = _bounded_count(
                max(step.input_count - step.output_count, 0)
            )
        terminal_impact = {}
        if terminal:
            terminal_impact = {
                "scope": "step",
                "stage": (
                    _safe_code(step.name, max_length=64)
                    or "unknown"
                ),
                "final_state": step.state,
                "affected_count": affected_count,
                "error_code": error_code,
            }
        RunStep.objects.using(db_alias).filter(pk=step.pk).update(
            correlation_id=step.run.correlation_id,
            duration_ms=_duration_ms(
                step.started_at,
                step.finished_at,
            ),
            retry_count=max(step.attempt_no - 1, 0),
            retry_at=None,
            terminal_impact=terminal_impact,
            recovery_state=(
                "manual_required"
                if step.state == "failed"
                else (
                    "stopped"
                    if step.state == "stopped"
                    else (
                        "not_required"
                        if step.state == "succeeded"
                        else "in_progress"
                    )
                )
            ),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("collection", "0005_converge_legacy_terminal_recovery"),
        ("infrastructure", "0002_outboxconsumerreceipt_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="collectionrun",
            name="correlation_id",
            field=models.UUIDField(
                db_index=True,
                editable=False,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="collectionrun",
            name="duration_ms",
            field=models.PositiveBigIntegerField(
                blank=True,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="collectionrun",
            name="next_recovery_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="collectionrun",
            name="recovery_state",
            field=models.CharField(
                choices=[
                    ("not_required", "Not required"),
                    ("in_progress", "In progress"),
                    ("automatic_retry", "Automatic retry"),
                    ("reconciling", "Reconciling"),
                    ("manual_required", "Manual required"),
                    ("stopped", "Stopped"),
                ],
                db_index=True,
                default="in_progress",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="collectionrun",
            name="retry_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="collectionrun",
            name="terminal_impact",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="runstep",
            name="correlation_id",
            field=models.UUIDField(
                db_index=True,
                editable=False,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="runstep",
            name="duration_ms",
            field=models.PositiveBigIntegerField(
                blank=True,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="runstep",
            name="recovery_state",
            field=models.CharField(
                choices=[
                    ("not_required", "Not required"),
                    ("in_progress", "In progress"),
                    ("automatic_retry", "Automatic retry"),
                    ("reconciling", "Reconciling"),
                    ("manual_required", "Manual required"),
                    ("stopped", "Stopped"),
                ],
                db_index=True,
                default="in_progress",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="runstep",
            name="retry_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="runstep",
            name="retry_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="runstep",
            name="terminal_impact",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="runstep",
            name="worker_task_id",
            field=models.CharField(
                blank=True,
                max_length=255,
                null=True,
            ),
        ),
        migrations.RunPython(
            backfill_collection_observability,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="collectionrun",
            name="correlation_id",
            field=models.UUIDField(
                db_index=True,
                default=uuid.uuid4,
                editable=False,
            ),
        ),
        migrations.AlterField(
            model_name="runstep",
            name="correlation_id",
            field=models.UUIDField(
                db_index=True,
                editable=False,
            ),
        ),
    ]
