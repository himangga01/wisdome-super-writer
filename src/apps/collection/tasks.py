from celery import shared_task

from .models import CollectionRun, RunState
from .services import collect_run


@shared_task(bind=True, autoretry_for=(TimeoutError,), retry_backoff=True, max_retries=3)
def execute_collection_run(self, run_id: str):
    run = CollectionRun.objects.select_related("source_registry").get(id=run_id)
    if run.state not in {RunState.QUEUED, RunState.COLLECTING}:
        return {"runId": str(run.id), "state": run.state}
    run = collect_run(run)
    if run.state == RunState.EXTRACTING:
        from apps.evidence.tasks import process_run_evidence

        process_run_evidence.delay(str(run.id))
    return {"runId": str(run.id), "state": run.state}
