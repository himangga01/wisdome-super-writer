import uuid
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import F
from django.db.models import Q
from django.utils import timezone

from wisdome_writer.observability import current_correlation_id

from .models import OutboxMessage


def enqueue_event(
    *,
    topic: str | None = None,
    aggregate_type: str | None = None,
    aggregate_id: uuid.UUID | str | None = None,
    payload: dict[str, Any],
    message_key: str | None = None,
    event_type: str | None = None,
    dedupe_key: str | None = None,
    correlation_id: uuid.UUID | str | None = None,
    available_at: datetime | None = None,
) -> OutboxMessage:
    """Enqueue inside the caller's transaction; duplicate message keys return the first row."""

    topic = topic or event_type
    if not topic:
        raise ValueError("outbox topic is required")
    message_key = message_key or dedupe_key
    if aggregate_id is None:
        candidate = next(
            (value for key, value in payload.items() if key.endswith("Id") and value),
            None,
        )
        try:
            aggregate_id = uuid.UUID(str(candidate))
        except (ValueError, TypeError, AttributeError):
            aggregate_id = uuid.uuid5(uuid.NAMESPACE_URL, message_key or f"{topic}:{payload!r}")
    else:
        aggregate_id = uuid.UUID(str(aggregate_id))
    aggregate_type = aggregate_type or topic.split(".", 1)[0]
    resolved_correlation_id = correlation_id or current_correlation_id()
    try:
        resolved_correlation_id = uuid.UUID(str(resolved_correlation_id))
    except (ValueError, TypeError, AttributeError):
        resolved_correlation_id = uuid.uuid4()
    message_key = message_key or f"{topic}:{aggregate_id}:{uuid.uuid4()}"
    with transaction.atomic():
        message, _ = OutboxMessage.objects.get_or_create(
            message_key=message_key,
            defaults={
                "topic": topic,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "payload": payload,
                "correlation_id": resolved_correlation_id,
                "available_at": available_at or timezone.now(),
            },
        )
    return message


@transaction.atomic
def claim_events(*, limit: int = 100, lease_seconds: int = 60) -> list[OutboxMessage]:
    now = timezone.now()
    messages = list(
        OutboxMessage.objects.select_for_update(skip_locked=True)
        .filter(published_at__isnull=True, available_at__lte=now)
        .filter(Q(claimed_until__isnull=True) | Q(claimed_until__lte=now))
        .order_by("available_at", "created_at")[:limit]
    )
    lease_until = now + timedelta(seconds=max(1, lease_seconds))
    OutboxMessage.objects.filter(pk__in=[message.pk for message in messages]).update(
        claimed_at=now,
        claimed_until=lease_until,
    )
    for message in messages:
        message.claimed_at = now
        message.claimed_until = lease_until
    return messages


def mark_published(message_id: uuid.UUID) -> None:
    OutboxMessage.objects.filter(pk=message_id, published_at__isnull=True).update(
        published_at=timezone.now(),
        claimed_at=None,
        claimed_until=None,
        attempts=F("attempts") + 1,
        last_error_code=None,
    )


def mark_failed(message_id: uuid.UUID, *, error_code: str, retry_at: datetime) -> None:
    OutboxMessage.objects.filter(pk=message_id, published_at__isnull=True).update(
        attempts=F("attempts") + 1,
        last_error_code=error_code[:120],
        available_at=retry_at,
        claimed_at=None,
        claimed_until=None,
    )
