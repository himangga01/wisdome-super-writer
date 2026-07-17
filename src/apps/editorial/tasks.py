from celery import shared_task
from django.db import transaction

from apps.collection.models import CollectionRun, RunState

from .services import build_source_grounded_draft


@shared_task
def generate_run_draft(run_id: str):
    run = CollectionRun.objects.get(id=run_id)
    if run.state not in {RunState.VALIDATING, RunState.DRAFTING}:
        return {"runId": str(run.id), "state": run.state}
    run.state = RunState.DRAFTING
    run.save(update_fields=["state"])
    article = build_source_grounded_draft(run)
    if run.trigger == "schedule" and run.approval_mode == "validated_auto":
        from apps.publishing.tasks import dispatch_scheduled_run_publication

        transaction.on_commit(lambda: dispatch_scheduled_run_publication.delay(str(run.id)))
    return {"runId": str(run.id), "articleId": str(article.id), "state": article.state}
