from celery import shared_task

from .models import CollectionRun, RunState
from .services import collect_run


@shared_task(name="apps.collection.tasks.execute_collection_run")
def execute_collection_run(run_id: str):
    run = CollectionRun.objects.select_related("source_registry").get(id=run_id)
    if run.state not in {RunState.QUEUED, RunState.COLLECTING}:
        return {"runId": str(run.id), "state": run.state}
    run = collect_run(run)
    return {"runId": str(run.id), "state": run.state}
