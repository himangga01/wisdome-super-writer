from celery import shared_task

from apps.audit.services import AuditContext
from wisdome_writer.observability import current_correlation_uuid

from .services import dispatch_due_schedules, release_queued_dispatch


@shared_task
def dispatch_due_schedules_task():
    audit_context = AuditContext.for_system(
        correlation_id=current_correlation_uuid(
            fallback=dispatch_due_schedules_task.request.id
        ),
        operation_key="schedule-due-scan",
        reason_code="scheduled due scan",
    )
    rows = dispatch_due_schedules(audit_context=audit_context)
    return [str(row.id) for row in rows if row]


@shared_task
def dispatch_queued_schedule(schedule_id: str):
    audit_context = AuditContext.for_system(
        correlation_id=current_correlation_uuid(
            fallback=dispatch_queued_schedule.request.id
        ),
        operation_key="schedule-queued-release",
        reason_code="queued schedule release",
    )
    row = release_queued_dispatch(
        schedule_id,
        audit_context=audit_context,
    )
    return str(row.id) if row else None
