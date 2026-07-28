from __future__ import annotations

import logging
import socket
from datetime import datetime

from celery import current_app, shared_task
from django.utils import timezone

from .event_routes import queue_for, route_for
from .models import OutboxMessage
from .outbox import (
    ForbiddenEventPayload,
    OutboxConflict,
    claim_events,
    consume_event,
    dead_letter_consumer_event,
    event_envelope,
    mark_failed,
    mark_published,
    validate_persisted_event,
)

logger = logging.getLogger(__name__)
CONSUMER_MAX_ATTEMPTS = 5


@shared_task(name="wisdome_writer.infrastructure.tasks.acknowledge_domain_event")
def acknowledge_domain_event():
    return {"state": "acknowledged"}


@shared_task(name="wisdome_writer.infrastructure.tasks.dispatch_outbox", acks_late=True)
def dispatch_outbox(limit: int = 100):
    # Celery beat only publishes this infrastructure task. The due-schedule scan
    # runs here and creates its run + outbox event in the service transaction.
    try:
        from apps.scheduling.services import dispatch_due_schedules

        dispatch_due_schedules()
    except Exception:
        logger.exception("due schedule scan failed")

    owner = f"{socket.gethostname()}:{dispatch_outbox.request.id or 'manual'}"
    dispatched = dead_lettered = failed = 0
    for message in claim_events(limit=limit, lease_owner=owner):
        envelope = event_envelope(message)
        route = route_for(message.topic)
        if route is None:
            mark_failed(message, error_code="unknown_event_type", permanent=True)
            dead_lettered += 1
            continue
        try:
            validate_persisted_event(message)
            current_app.send_task(
                "wisdome_writer.infrastructure.tasks.consume_outbox_event",
                args=[envelope],
                queue=queue_for(route, envelope),
                task_id=f"outbox:{message.id}:{message.attempts}",
            )
        except Exception as exc:
            mark_failed(
                message,
                error_code=str(getattr(exc, "code", exc.__class__.__name__)),
                retry_after=getattr(exc, "retry_after_seconds", None),
                permanent=isinstance(exc, (ForbiddenEventPayload, OutboxConflict)),
            )
            failed += 1
        else:
            mark_published(message)
            dispatched += 1
    return {
        "dispatched": dispatched,
        "deadLettered": dead_lettered,
        "failed": failed,
    }


@shared_task(
    bind=True,
    name="wisdome_writer.infrastructure.tasks.consume_outbox_event",
    max_retries=None,
    acks_late=True,
    reject_on_worker_lost=True,
)
def consume_outbox_event(self, envelope: dict):
    try:
        persisted_type = OutboxMessage.objects.values_list("topic", flat=True).get(
            pk=envelope.get("event_id")
        )
    except (OutboxMessage.DoesNotExist, ValueError, TypeError):
        return {"state": "dead_letter", "code": "event_not_found"}
    route = route_for(persisted_type)
    if route is None:
        return dead_letter_consumer_event(
            envelope["event_id"],
            consumer_name="unknown-event",
            error_code="unknown_event_type",
        )

    def handler(*args):
        task = current_app.tasks.get(route.task_name)
        if task is None:
            raise LookupError(f"event handler task is not registered: {route.task_name}")
        # Direct Task.__call__ pushes the handler's own Celery request context
        # without publishing another broker message.
        return task(*args)

    result = consume_event(
        envelope=envelope,
        consumer_name=route.consumer_name,
        handler=handler,
        argument_keys=route.argument_keys,
        max_attempts=CONSUMER_MAX_ATTEMPTS,
    )
    if result["state"] == "retry":
        retry_at = datetime.fromisoformat(result["retry_at"])
        countdown = max(1, int((retry_at - timezone.now()).total_seconds()))
        raise self.retry(countdown=countdown)
    return result
