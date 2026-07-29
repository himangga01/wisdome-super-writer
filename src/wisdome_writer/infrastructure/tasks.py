from __future__ import annotations

import logging
import socket
import uuid
from datetime import datetime

from celery import current_app, shared_task
from celery.exceptions import Reject
from django.db import DatabaseError
from django.utils import timezone

from .event_routes import EventRoutingError, queue_for, route_for
from .models import OutboxMessage
from .outbox import (
    ForbiddenEventPayload,
    OutboxConflict,
    claim_exhausted_events,
    claim_events,
    consume_event,
    dead_letter_consumer_event,
    event_envelope,
    mark_failed,
    mark_published,
    mark_terminal_retry,
    validate_persisted_event,
)

logger = logging.getLogger(__name__)


@shared_task(name="wisdome_writer.infrastructure.tasks.acknowledge_domain_event")
def acknowledge_domain_event():
    return {"state": "acknowledged"}


def _registered_task(task_name: str):
    task = current_app.tasks.get(task_name)
    if task is None:
        raise LookupError(f"event handler task is not registered: {task_name}")
    return task


def _terminal_handler_for(route):
    if route.terminal_task_name is None:
        return None

    def terminal_handler(*args):
        return _registered_task(route.terminal_task_name)(*args)

    return terminal_handler


def _terminalize_routed_dispatch(message, route, error_code: str):
    try:
        result = dead_letter_consumer_event(
            message.id,
            consumer_name=route.consumer_name,
            error_code=error_code,
            terminal_handler=_terminal_handler_for(route),
            terminal_argument_keys=route.terminal_argument_keys,
            expected_dispatch_lease_token=message.lease_token,
            expected_dispatch_lease_generation=message.lease_generation,
        )
    except Exception as exc:
        mark_terminal_retry(
            message,
            error_code=(
                f"terminal_callback_{getattr(exc, 'code', exc.__class__.__name__)}"
            ),
        )
        logger.exception(
            "routed outbox terminal callback failed; event returned to terminal retry",
            exc_info=exc,
        )
        return False
    return result["state"] in {"accepted", "duplicate", "dead_letter"}


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
    exhausted = claim_exhausted_events(limit=limit, lease_owner=owner)
    for message in exhausted:
        route = route_for(message.topic, message.event_version)
        if route is None:
            mark_failed(
                message,
                error_code="dispatch_attempts_exhausted",
                permanent=True,
            )
            dead_lettered += 1
        elif _terminalize_routed_dispatch(
            message,
            route,
            "dispatch_attempts_exhausted",
        ):
            dead_lettered += 1
        else:
            failed += 1

    remaining = max(limit - len(exhausted), 0)
    for message in claim_events(limit=remaining, lease_owner=owner) if remaining else ():
        envelope = event_envelope(message)
        route = route_for(message.topic, message.event_version)
        if route is None:
            mark_failed(message, error_code="unsupported_event_type_version", permanent=True)
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
            terminal = isinstance(
                exc,
                (ForbiddenEventPayload, OutboxConflict, EventRoutingError),
            ) or message.attempts >= message.max_attempts
            if terminal:
                if _terminalize_routed_dispatch(
                    message,
                    route,
                    str(getattr(exc, "code", exc.__class__.__name__)),
                ):
                    dead_lettered += 1
                else:
                    failed += 1
            else:
                mark_failed(
                    message,
                    error_code=str(
                        getattr(exc, "code", exc.__class__.__name__)
                    ),
                    retry_after=getattr(exc, "retry_after_seconds", None),
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
    acks_on_failure_or_timeout=False,
    reject_on_worker_lost=True,
    soft_time_limit=28 * 60,
    time_limit=30 * 60,
)
def consume_outbox_event(
    self,
    envelope: dict,
    infra_retry_count: int = 0,
    infrastructure_lease_token: str | None = None,
    infrastructure_lease_generation: int | None = None,
):
    def retry_infrastructure(exc: DatabaseError):
        retry_lease_token = getattr(
            exc,
            "lease_token",
            infrastructure_lease_token,
        )
        retry_lease_generation = getattr(
            exc,
            "lease_generation",
            infrastructure_lease_generation,
        )
        if infra_retry_count >= 3:
            logger.exception(
                "outbox consumer infrastructure remained unavailable; requeueing",
                exc_info=exc,
            )
            raise Reject(exc, requeue=True)
        raise self.retry(
            args=(envelope,),
            kwargs={
                "infra_retry_count": infra_retry_count + 1,
                "infrastructure_lease_token": (
                    str(retry_lease_token) if retry_lease_token else None
                ),
                "infrastructure_lease_generation": retry_lease_generation,
            },
            exc=exc,
            countdown=2**infra_retry_count,
        )

    def unaddressable(code: str):
        # Without a valid persisted event_id there is no row on which to store a
        # durable receipt. Bound broker redelivery and terminate after three tries.
        if self.request.retries < 2:
            raise self.retry(
                args=(envelope,),
                kwargs={"infra_retry_count": 0},
                countdown=2**self.request.retries,
            )
        logger.error("unaddressable outbox envelope discarded after bounded retries: %s", code)
        return {"state": "dead_letter", "code": code, "attempt": self.request.retries + 1}

    if not isinstance(envelope, dict):
        return unaddressable("envelope_not_object")
    try:
        event_id = uuid.UUID(str(envelope.get("event_id")))
    except (ValueError, TypeError, AttributeError):
        return unaddressable("event_id_invalid")
    try:
        try:
            persisted_type, persisted_version = OutboxMessage.objects.values_list(
                "topic", "event_version"
            ).get(
                pk=event_id
            )
        except OutboxMessage.DoesNotExist:
            return unaddressable("event_not_found")
    except DatabaseError as exc:
        return retry_infrastructure(exc)
    route = route_for(persisted_type, persisted_version)
    if route is None:
        try:
            return dead_letter_consumer_event(
                event_id,
                consumer_name="unknown-event",
                error_code="unsupported_event_type_version",
            )
        except DatabaseError as exc:
            return retry_infrastructure(exc)

    def handler(*args):
        # Direct Task.__call__ pushes the handler's own Celery request context
        # without publishing another broker message.
        return _registered_task(route.task_name)(*args)

    terminal_handler = _terminal_handler_for(route)

    try:
        result = consume_event(
            envelope=envelope,
            consumer_name=route.consumer_name,
            handler=handler,
            argument_keys=route.argument_keys,
            terminal_handler=terminal_handler,
            terminal_argument_keys=route.terminal_argument_keys,
            max_attempts=route.max_attempts,
            infrastructure_lease_token=infrastructure_lease_token,
            infrastructure_lease_generation=infrastructure_lease_generation,
        )
    except DatabaseError as exc:
        return retry_infrastructure(exc)
    if result["state"] == "retry":
        retry_at = datetime.fromisoformat(result["retry_at"])
        countdown = max(1, int((retry_at - timezone.now()).total_seconds()))
        raise self.retry(
            args=(envelope,),
            kwargs={"infra_retry_count": 0},
            countdown=countdown,
        )
    return result
