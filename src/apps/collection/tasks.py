from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import CollectionRun, RunState, RunStep
from .services import collect_run


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
                    state="running",
                )
                .first()
            )
            if step is None:
                return {"runId": str(run.id), "state": run.state}
            stopping = True
        elif active:
            step, _ = RunStep.objects.select_for_update().get_or_create(
                run=run,
                name="collect",
                attempt_no=1,
            )
            stopping = run.stop_requested_at is not None
        else:
            return {"runId": str(run.id), "state": run.state}

        if stopping:
            step.state = "stopped"
            step.error_code = "stop_requested"
            step.error_detail_redacted = (
                "collection delivery stopped by request"
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
        step.finished_at = step.finished_at or now
        step.save(
            update_fields=(
                "state",
                "error_code",
                "error_detail_redacted",
                "finished_at",
            )
        )
        run.completed_at = run.completed_at or now
        run.save(
            update_fields=(
                "state",
                "error_summary",
                "completed_at",
            )
        )
        return {
            "runId": str(run.id),
            "state": run.state,
            "code": step.error_code,
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
