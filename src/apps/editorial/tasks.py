from celery import shared_task
from django.db import transaction

from apps.collection.models import CollectionRun, RunState
from wisdome_writer.infrastructure.outbox import enqueue_event

from .services import build_source_grounded_draft


@shared_task
def generate_run_draft(run_id: str):
    with transaction.atomic():
        run = CollectionRun.objects.get(id=run_id)
        if run.state not in {RunState.VALIDATING, RunState.DRAFTING}:
            return {"runId": str(run.id), "state": run.state}
        run.state = RunState.DRAFTING
        run.save(update_fields=["state"])
        article = build_source_grounded_draft(run)
        if run.trigger == "schedule" and run.approval_mode == "validated_auto":
            enqueue_event(
                event_type="publication.scheduled_run_requested",
                aggregate_type="collection_run",
                aggregate_id=run.id,
                job_id=run.id,
                dedupe_key=f"publication.scheduled_run_requested:{run.id}",
                payload={"run_id": str(run.id)},
            )
    return {"runId": str(run.id), "articleId": str(article.id), "state": article.state}
