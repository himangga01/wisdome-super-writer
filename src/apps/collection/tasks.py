from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import CollectionRun, RecoveryState, RunState, RunStep
from .services import (
    collect_run,
    project_run_terminal_observation,
    project_step_terminal_observation,
)


@shared_task(name="apps.collection.tasks.execute_collection_run")
def execute_collection_run(run_id: str):
    run = CollectionRun.objects.select_related("source_registry").get(id=run_id)
    if (
        run.state == RunState.STOPPING
        and run.stop_requested_at is not None
    ):
        return _finalize_collection_run_delivery_failure(
            run_id,
            "stop_requested",
        )
    if run.state not in {RunState.QUEUED, RunState.COLLECTING}:
        return {"runId": str(run.id), "state": run.state}
    run = collect_run(run)
    return {"runId": str(run.id), "state": run.state}


def _finalize_collection_run_delivery_failure(
    run_id: str,
    error_code: str,
):
    now = timezone.now()
    redacted_code = str(error_code)[:100]
    with transaction.atomic():
        run = (
            CollectionRun.objects.select_for_update()
            .filter(pk=run_id)
            .first()
        )
        if run is None:
            return {"runId": run_id, "state": "missing"}
        if run.state in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.STOPPED,
        }:
            return {"runId": str(run.id), "state": run.state}

        active = run.state in {RunState.QUEUED, RunState.COLLECTING}
        if run.state == RunState.STOPPING:
            if run.stop_requested_at is None:
                return {"runId": str(run.id), "state": run.state}
            if RunStep.objects.filter(run=run).exclude(
                name="collect"
            ).exists():
                return {"runId": str(run.id), "state": run.state}
            step = (
                RunStep.objects.select_for_update()
                .filter(
                    run=run,
                    name="collect",
                    attempt_no=1,
                )
                .first()
            )
            stopping = True
            update_stopping_step = (
                step is not None
                and step.state in {"queued", "running"}
            )
        elif active:
            step, _ = RunStep.objects.select_for_update().get_or_create(
                run=run,
                name="collect",
                attempt_no=1,
            )
            stopping = run.stop_requested_at is not None
            update_stopping_step = stopping
        else:
            return {"runId": str(run.id), "state": run.state}

        if stopping:
            if update_stopping_step:
                step.state = "stopped"
                step.error_code = "stop_requested"
                step.error_detail_redacted = (
                    "collection delivery stopped by request"
                )
                project_step_terminal_observation(
                    step,
                    run,
                    finished_at=now,
                    final_state=step.state,
                    affected_count=step.input_count,
                    error_code=step.error_code,
                    recovery_state=RecoveryState.STOPPED,
                )
                step.save(
                    update_fields=(
                        "correlation_id",
                        "worker_task_id",
                        "state",
                        "error_code",
                        "error_detail_redacted",
                        "finished_at",
                        "duration_ms",
                        "retry_count",
                        "retry_at",
                        "terminal_impact",
                        "recovery_state",
                    )
                )
            run.state = RunState.STOPPED
            run.error_summary = None
        else:
            step.state = "failed"
            step.error_code = redacted_code
            step.error_detail_redacted = (
                "collection delivery exhausted before completion"
            )
            run.state = RunState.FAILED
            run.error_summary = {
                "stage": "collect",
                "code": redacted_code,
            }
            project_step_terminal_observation(
                step,
                run,
                finished_at=now,
                final_state=step.state,
                affected_count=step.input_count,
                error_code=step.error_code,
                recovery_state=RecoveryState.MANUAL_REQUIRED,
            )
            step.save(
                update_fields=(
                    "correlation_id",
                    "worker_task_id",
                    "state",
                    "error_code",
                    "error_detail_redacted",
                    "finished_at",
                    "duration_ms",
                    "retry_count",
                    "retry_at",
                    "terminal_impact",
                    "recovery_state",
                )
            )
        project_run_terminal_observation(
            run,
            finished_at=now,
            stage="collect",
            final_state=run.state,
            error_code=(
                "stop_requested"
                if stopping
                else redacted_code
            ),
            recovery_state=(
                RecoveryState.STOPPED
                if stopping
                else RecoveryState.MANUAL_REQUIRED
            ),
        )
        run.save(
            update_fields=(
                "state",
                "error_summary",
                "completed_at",
                "duration_ms",
                "terminal_impact",
                "recovery_state",
                "next_recovery_at",
            )
        )
        return {
            "runId": str(run.id),
            "state": run.state,
            "code": (
                step.error_code
                if step is not None
                else "stop_requested"
            ),
        }


@shared_task(
    name="apps.collection.tasks.finalize_collection_run_delivery_failure"
)
def finalize_collection_run_delivery_failure(
    run_id: str,
    error_code: str,
):
    return _finalize_collection_run_delivery_failure(
        run_id,
        error_code,
    )
