from celery import shared_task
from django.db import transaction

from apps.audit.services import AuditContext
from apps.collection.models import CollectionRun, RunState
from wisdome_writer.infrastructure.outbox import (
    CURRENT_EVENT_CONSUMER_LEASE_GENERATION,
    CURRENT_EVENT_CONSUMER_LEASE_TOKEN,
    CURRENT_EVENT_CONSUMER_NAME,
    CURRENT_EVENT_CORRELATION_ID,
    CURRENT_EVENT_ID,
    enqueue_event,
)

from .models import DraftArticle
from .services import build_source_grounded_draft


@shared_task
def generate_run_draft(run_id: str):
    audit_context = AuditContext.for_worker(
        correlation_id=CURRENT_EVENT_CORRELATION_ID.get(),
        event_key=CURRENT_EVENT_ID.get(),
        consumer_name=CURRENT_EVENT_CONSUMER_NAME.get(),
        lease_token=CURRENT_EVENT_CONSUMER_LEASE_TOKEN.get(),
        lease_generation=CURRENT_EVENT_CONSUMER_LEASE_GENERATION.get(),
        reason_code="source grounded draft generation",
    )
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        run = (
            CollectionRun.objects.using(alias)
            .select_for_update()
            .get(id=run_id)
        )
        has_generated_article = DraftArticle.objects.using(alias).filter(
            source_run=run,
            current_revision__isnull=False,
        ).exists()
        if (
            run.state not in {RunState.VALIDATING, RunState.DRAFTING}
            and not has_generated_article
        ):
            return {"runId": str(run.id), "state": run.state}
        article = build_source_grounded_draft(
            run,
            audit_context=audit_context,
        )
        if run.trigger == "schedule" and run.approval_mode == "validated_auto":
            enqueue_event(
                event_type="publication.scheduled_run_requested",
                aggregate_type="collection_run",
                aggregate_id=run.id,
                job_id=run.id,
                dedupe_key=f"publication.scheduled_run_requested:{run.id}",
                correlation_id=audit_context.correlation_id,
                payload={"run_id": str(run.id)},
            )
    return {"runId": str(run.id), "articleId": str(article.id), "state": article.state}
