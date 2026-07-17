from celery import shared_task

from .services import dispatch_due_schedules, release_queued_dispatch


@shared_task
def dispatch_due_schedules_task():
    rows = dispatch_due_schedules()
    return [str(row.id) for row in rows if row]


@shared_task
def dispatch_queued_schedule(schedule_id: str):
    row = release_queued_dispatch(schedule_id)
    return str(row.id) if row else None
