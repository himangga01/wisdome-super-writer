from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Callable

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash
from wisdome_writer.observability import correlation_context, current_correlation_id

from .models import OutboxConsumerReceipt, OutboxMessage
from .event_routes import payload_schema_for

CURRENT_EVENT_ID: ContextVar[str | None] = ContextVar("outbox_event_id", default=None)
CURRENT_EVENT_CORRELATION_ID: ContextVar[str | None] = ContextVar(
    "outbox_event_correlation_id", default=None
)

FORBIDDEN_KEY_PARTS = {
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "cookie",
    "set_cookie",
    "csrf",
    "authorization",
    "credential",
    "session",
    "raw_body",
    "body_text",
    "body_html",
    "binary",
    "secret",
}
FORBIDDEN_EXACT_KEYS = {
    "body",
    "content",
    "html",
    "text",
    "headers",
    "request_headers",
    "response_headers",
}
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_POLICY_BYTES = 8 * 1024
MAX_PAYLOAD_DEPTH = 8
MAX_CONTAINER_ITEMS = 100
MAX_STRING_LENGTH = 4096


class OutboxConflict(ValueError):
    pass


class ForbiddenEventPayload(ValueError):
    pass


class LostOutboxLease(RuntimeError):
    pass


def _uuid(value: uuid.UUID | str | None, *, fallback: uuid.UUID | None = None) -> uuid.UUID:
    if value is None and fallback is not None:
        return fallback
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        if fallback is not None:
            return fallback
        raise ValueError("a valid UUID is required") from exc


def _validate_payload(value: Any, *, path: str = "payload", depth: int = 0) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise ForbiddenEventPayload(f"event payload is too deeply nested: {path}")
    if isinstance(value, dict):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ForbiddenEventPayload(f"event payload object is too large: {path}")
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in FORBIDDEN_EXACT_KEYS or any(
                part in normalized for part in FORBIDDEN_KEY_PARTS
            ):
                raise ForbiddenEventPayload(f"forbidden event payload field: {path}.{key}")
            _validate_payload(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ForbiddenEventPayload(f"event payload list is too large: {path}")
        for index, child in enumerate(value):
            _validate_payload(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ForbiddenEventPayload(f"binary event payload is forbidden: {path}")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ForbiddenEventPayload(f"non-JSON event payload value is forbidden: {path}")
    if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
        raise ForbiddenEventPayload(f"event payload string is too large: {path}")


def _validate_event_payload(event_type: str, payload: dict[str, Any]) -> None:
    _validate_payload(payload)
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ForbiddenEventPayload("event payload exceeds the 64 KiB limit")
    schema = payload_schema_for(event_type)
    if schema is None:
        raise ForbiddenEventPayload(f"no payload schema is registered for event type: {event_type}")
    keys = set(payload)
    missing = schema.required - keys
    extra = keys - set(schema.fields)
    if missing:
        raise ForbiddenEventPayload(
            f"event payload is missing required fields: {', '.join(sorted(missing))}"
        )
    if extra:
        raise ForbiddenEventPayload(
            f"event payload contains undeclared fields: {', '.join(sorted(extra))}"
        )
    for key, value in payload.items():
        allowed = schema.fields[key]
        if type(value) not in allowed:
            expected = "/".join(value_type.__name__ for value_type in allowed)
            raise ForbiddenEventPayload(
                f"event payload field has invalid type: {key} (expected {expected})"
            )


def _material(
    *,
    event_type: str,
    event_version: int,
    correlation_id: uuid.UUID,
    causation_id: uuid.UUID | None,
    job_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    operation: str,
    dedupe_key: str,
    policy_versions: dict[str, Any],
    not_before: datetime,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "event_version": event_version,
        "correlation_id": str(correlation_id),
        "causation_id": str(causation_id) if causation_id else None,
        "job_id": str(job_id),
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "operation": operation,
        "dedupe_key": dedupe_key,
        "policy_versions": policy_versions,
        "not_before": not_before.isoformat(),
        "payload": payload,
    }


def enqueue_event(
    *,
    topic: str | None = None,
    aggregate_type: str | None = None,
    aggregate_id: uuid.UUID | str | None = None,
    payload: dict[str, Any],
    message_key: str | None = None,
    event_type: str | None = None,
    event_version: int = 1,
    dedupe_key: str | None = None,
    correlation_id: uuid.UUID | str | None = None,
    causation_id: uuid.UUID | str | None = None,
    job_id: uuid.UUID | str | None = None,
    operation: str = "process",
    policy_versions: dict[str, Any] | None = None,
    available_at: datetime | None = None,
    max_attempts: int = 5,
) -> OutboxMessage:
    """Persist one immutable event. Producers must call this in their business transaction."""

    event_type = event_type or topic
    if not event_type:
        raise ValueError("outbox event_type is required")
    if not isinstance(payload, dict):
        raise ForbiddenEventPayload("event payload must be a JSON object")
    _validate_event_payload(event_type, payload)
    policy_versions = policy_versions or {}
    _validate_payload(policy_versions, path="policy_versions")
    if len(
        json.dumps(
            policy_versions, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ) > MAX_POLICY_BYTES:
        raise ForbiddenEventPayload("policy_versions exceeds the 8 KiB limit")
    entity_id = _uuid(aggregate_id)
    entity_type = aggregate_type or event_type.split(".", 1)[0]
    dedupe_key = dedupe_key or message_key
    if not dedupe_key:
        raise ValueError("outbox dedupe_key is required")
    requested_not_before = available_at
    not_before = requested_not_before or timezone.now()
    inherited_correlation = CURRENT_EVENT_CORRELATION_ID.get()
    raw_correlation = correlation_id or inherited_correlation or current_correlation_id()
    correlation_was_generated = False
    try:
        resolved_correlation = _uuid(raw_correlation)
    except ValueError:
        resolved_correlation = uuid.uuid4()
        correlation_was_generated = True
    inherited_causation = CURRENT_EVENT_ID.get()
    resolved_causation = (
        _uuid(causation_id or inherited_causation)
        if causation_id or inherited_causation
        else None
    )
    resolved_job_id = _uuid(job_id, fallback=entity_id)
    material = _material(
        event_type=event_type,
        event_version=event_version,
        correlation_id=resolved_correlation,
        causation_id=resolved_causation,
        job_id=resolved_job_id,
        entity_type=entity_type,
        entity_id=entity_id,
        operation=operation,
        dedupe_key=dedupe_key,
        policy_versions=policy_versions,
        not_before=not_before,
        payload=payload,
    )
    material_hash = canonical_hash(material, schema_version=CANONICAL_HASH_SCHEMA_V1)
    defaults = {
        "topic": event_type,
        "event_version": event_version,
        "occurred_at": timezone.now(),
        "aggregate_type": entity_type,
        "aggregate_id": entity_id,
        "payload": payload,
        "correlation_id": resolved_correlation,
        "causation_id": resolved_causation,
        "job_id": resolved_job_id,
        "operation": operation,
        "policy_versions": policy_versions,
        "immutable_material_hash": material_hash,
        "available_at": not_before,
        "max_attempts": min(max(int(max_attempts), 1), 5),
    }
    with transaction.atomic():
        existing = OutboxMessage.objects.select_for_update().filter(message_key=dedupe_key).first()
        if existing is not None:
            comparison_hash = material_hash
            if requested_not_before is None or correlation_was_generated:
                comparison_hash = canonical_hash(
                    _material(
                        event_type=event_type,
                        event_version=event_version,
                        correlation_id=(
                            existing.correlation_id
                            if correlation_was_generated
                            else resolved_correlation
                        ),
                        causation_id=resolved_causation,
                        job_id=resolved_job_id,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        operation=operation,
                        dedupe_key=dedupe_key,
                        policy_versions=policy_versions,
                        not_before=(
                            existing.available_at
                            if requested_not_before is None
                            else requested_not_before
                        ),
                        payload=payload,
                    ),
                    schema_version=CANONICAL_HASH_SCHEMA_V1,
                )
            if existing.immutable_material_hash != comparison_hash:
                raise OutboxConflict("dedupe key already exists with different immutable material")
            return existing
        try:
            with transaction.atomic():
                return OutboxMessage.objects.create(message_key=dedupe_key, **defaults)
        except IntegrityError:
            existing = OutboxMessage.objects.select_for_update().get(message_key=dedupe_key)
            comparison_hash = material_hash
            if requested_not_before is None or correlation_was_generated:
                comparison_hash = canonical_hash(
                    _material(
                        event_type=event_type,
                        event_version=event_version,
                        correlation_id=(
                            existing.correlation_id
                            if correlation_was_generated
                            else resolved_correlation
                        ),
                        causation_id=resolved_causation,
                        job_id=resolved_job_id,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        operation=operation,
                        dedupe_key=dedupe_key,
                        policy_versions=policy_versions,
                        not_before=(
                            existing.available_at
                            if requested_not_before is None
                            else requested_not_before
                        ),
                        payload=payload,
                    ),
                    schema_version=CANONICAL_HASH_SCHEMA_V1,
                )
            if existing.immutable_material_hash != comparison_hash:
                raise OutboxConflict(
                    "dedupe key concurrently created with different immutable material"
                )
            return existing


@transaction.atomic
def claim_events(
    *,
    limit: int = 100,
    lease_seconds: int = 60,
    lease_owner: str = "outbox-dispatcher",
) -> list[OutboxMessage]:
    now = timezone.now()
    claimable = Q(status=OutboxMessage.Status.PENDING) | Q(
        status=OutboxMessage.Status.DISPATCHING, claimed_until__lte=now
    )
    exhausted = (
        OutboxMessage.objects.select_for_update(skip_locked=True)
        .filter(claimable, attempts__gte=F("max_attempts"))
        .filter(Q(claimed_until__isnull=True) | Q(claimed_until__lte=now))
    )
    exhausted.update(
        status=OutboxMessage.Status.DEAD_LETTER,
        dead_lettered_at=now,
        last_error_at=now,
        last_error_code="dispatch_attempts_exhausted",
        lease_owner="",
        lease_token=None,
        claimed_at=None,
        claimed_until=None,
    )
    messages = list(
        OutboxMessage.objects.select_for_update(skip_locked=True)
        .filter(claimable, available_at__lte=now, attempts__lt=F("max_attempts"))
        .filter(Q(claimed_until__isnull=True) | Q(claimed_until__lte=now))
        .order_by("available_at", "created_at", "id")[: max(1, limit)]
    )
    lease_until = now + timedelta(seconds=max(1, lease_seconds))
    for message in messages:
        message.status = OutboxMessage.Status.DISPATCHING
        message.claimed_at = now
        message.claimed_until = lease_until
        message.lease_owner = lease_owner[:160]
        message.lease_token = uuid.uuid4()
        message.lease_generation += 1
        message.attempts += 1
        message.save(
            update_fields=(
                "status",
                "claimed_at",
                "claimed_until",
                "lease_owner",
                "lease_token",
                "lease_generation",
                "attempts",
            )
        )
    return messages


def event_envelope(message: OutboxMessage) -> dict[str, Any]:
    return {
        "event_id": str(message.id),
        "event_type": message.topic,
        "event_version": message.event_version,
        "occurred_at": message.occurred_at.isoformat(),
        "correlation_id": str(message.correlation_id),
        "causation_id": str(message.causation_id) if message.causation_id else None,
        "job_id": str(message.job_id),
        "entity_type": message.aggregate_type,
        "entity_id": str(message.aggregate_id),
        "operation": message.operation,
        "attempt": message.attempts,
        "dedupe_key": message.message_key,
        "policy_versions": message.policy_versions,
        "not_before": message.available_at.isoformat(),
        "payload": message.payload,
    }


def validate_persisted_event(message: OutboxMessage) -> None:
    _validate_event_payload(message.topic, message.payload)
    _validate_payload(message.policy_versions, path="policy_versions")


@transaction.atomic
def dead_letter_consumer_event(
    event_id: uuid.UUID | str,
    *,
    consumer_name: str,
    error_code: str,
) -> dict[str, Any]:
    now = timezone.now()
    event = OutboxMessage.objects.select_for_update().get(pk=event_id)
    receipt, _ = OutboxConsumerReceipt.objects.select_for_update().get_or_create(
        event=event,
        consumer_name=consumer_name,
    )
    receipt.state = OutboxConsumerReceipt.State.DEAD_LETTER
    receipt.attempts += 1
    receipt.last_error_code = error_code[:120]
    receipt.dead_lettered_at = now
    receipt.claimed_at = None
    receipt.claimed_until = None
    receipt.lease_token = None
    receipt.save()
    event.status = OutboxMessage.Status.DEAD_LETTER
    event.dead_lettered_at = now
    event.last_error_at = now
    event.last_error_code = error_code[:120]
    event.save(
        update_fields=(
            "status",
            "dead_lettered_at",
            "last_error_at",
            "last_error_code",
        )
    )
    return {"state": "dead_letter", "attempt": receipt.attempts}


def _lease_filter(message: OutboxMessage):
    return OutboxMessage.objects.filter(
        pk=message.pk,
        status=OutboxMessage.Status.DISPATCHING,
        lease_token=message.lease_token,
        lease_generation=message.lease_generation,
        claimed_until__gt=timezone.now(),
    )


def mark_published(message: OutboxMessage) -> None:
    updated = _lease_filter(message).update(
        status=OutboxMessage.Status.PUBLISHED,
        published_at=timezone.now(),
        claimed_at=None,
        claimed_until=None,
        lease_owner="",
        lease_token=None,
        last_error_code=None,
        last_error_at=None,
    )
    if updated != 1:
        raise LostOutboxLease("stale dispatcher cannot ACK this event")


def _backoff_seconds(event_id: uuid.UUID, attempt: int, retry_after: int | None = None) -> int:
    if retry_after is not None:
        return min(max(int(retry_after), 1), 3600)
    base = min(15 * (2 ** max(attempt - 1, 0)), 1800)
    digest = hashlib.sha256(f"{event_id}:{attempt}".encode()).digest()
    return min(base + int.from_bytes(digest[:2], "big") % max(2, base // 4 + 1), 3600)


def mark_failed(
    message: OutboxMessage,
    *,
    error_code: str,
    retry_after: int | None = None,
    permanent: bool = False,
) -> None:
    now = timezone.now()
    terminal = permanent or message.attempts >= message.max_attempts
    updates: dict[str, Any] = {
        "status": (
            OutboxMessage.Status.DEAD_LETTER if terminal else OutboxMessage.Status.PENDING
        ),
        "last_error_code": error_code[:120],
        "last_error_at": now,
        "claimed_at": None,
        "claimed_until": None,
        "lease_owner": "",
        "lease_token": None,
    }
    if terminal:
        updates["dead_lettered_at"] = now
    else:
        updates["available_at"] = now + timedelta(
            seconds=_backoff_seconds(message.id, message.attempts, retry_after)
        )
    if _lease_filter(message).update(**updates) != 1:
        raise LostOutboxLease("stale dispatcher cannot NACK this event")


def _verify_received_envelope(event: OutboxMessage, envelope: dict[str, Any]) -> None:
    expected = event_envelope(event)
    for key in expected:
        if key == "attempt":
            continue
        if envelope.get(key) != expected[key]:
            raise OutboxConflict(f"received envelope does not match persisted event: {key}")


@contextmanager
def event_context(envelope: dict[str, Any]):
    event_token = CURRENT_EVENT_ID.set(envelope["event_id"])
    correlation_token = CURRENT_EVENT_CORRELATION_ID.set(envelope["correlation_id"])
    try:
        with correlation_context(envelope["correlation_id"]):
            yield
    finally:
        CURRENT_EVENT_CORRELATION_ID.reset(correlation_token)
        CURRENT_EVENT_ID.reset(event_token)


def consume_event(
    *,
    envelope: dict[str, Any],
    consumer_name: str,
    handler: Callable[..., Any],
    argument_keys: tuple[str, ...],
    max_attempts: int = 5,
) -> dict[str, Any]:
    payload = envelope.get("payload")
    event_id = _uuid(envelope.get("event_id"))
    now = timezone.now()
    with transaction.atomic():
        event = OutboxMessage.objects.select_for_update().get(pk=event_id)
        try:
            if not isinstance(payload, dict):
                raise ForbiddenEventPayload("received event payload must be an object")
            _verify_received_envelope(event, envelope)
            validate_persisted_event(event)
        except (ForbiddenEventPayload, OutboxConflict) as exc:
            receipt, _ = OutboxConsumerReceipt.objects.select_for_update().get_or_create(
                event=event,
                consumer_name=consumer_name,
            )
            receipt.state = OutboxConsumerReceipt.State.DEAD_LETTER
            receipt.attempts += 1
            receipt.last_error_code = str(
                getattr(exc, "code", exc.__class__.__name__)
            )[:120]
            receipt.dead_lettered_at = now
            receipt.claimed_at = None
            receipt.claimed_until = None
            receipt.lease_token = None
            receipt.save(
                update_fields=(
                    "state",
                    "attempts",
                    "last_error_code",
                    "dead_lettered_at",
                    "claimed_at",
                    "claimed_until",
                    "lease_token",
                )
            )
            event.status = OutboxMessage.Status.DEAD_LETTER
            event.dead_lettered_at = now
            event.last_error_at = now
            event.last_error_code = receipt.last_error_code
            event.save(
                update_fields=(
                    "status",
                    "dead_lettered_at",
                    "last_error_at",
                    "last_error_code",
                )
            )
            return {"state": "dead_letter", "attempt": receipt.attempts}
        receipt, _ = OutboxConsumerReceipt.objects.select_for_update().get_or_create(
            event=event,
            consumer_name=consumer_name,
        )
        if receipt.state == OutboxConsumerReceipt.State.SUCCEEDED:
            return {"state": "duplicate", "attempt": receipt.attempts}
        if receipt.state == OutboxConsumerReceipt.State.DEAD_LETTER:
            return {"state": "dead_letter", "attempt": receipt.attempts}
        if (
            receipt.state == OutboxConsumerReceipt.State.PROCESSING
            and receipt.claimed_until
            and receipt.claimed_until > now
        ):
            return {
                "state": "retry",
                "attempt": receipt.attempts,
                "retry_at": receipt.claimed_until.isoformat(),
            }
        if (
            receipt.state == OutboxConsumerReceipt.State.RETRY
            and receipt.next_retry_at
            and receipt.next_retry_at > now
        ):
            return {
                "state": "retry",
                "attempt": receipt.attempts,
                "retry_at": receipt.next_retry_at.isoformat(),
            }
        receipt.state = OutboxConsumerReceipt.State.PROCESSING
        receipt.attempts += 1
        receipt.started_at = now
        receipt.next_retry_at = None
        receipt.claimed_at = now
        receipt.claimed_until = now + timedelta(minutes=30)
        receipt.lease_token = uuid.uuid4()
        receipt.lease_generation += 1
        lease_token = receipt.lease_token
        lease_generation = receipt.lease_generation
        receipt.save(
            update_fields=(
                "state",
                "attempts",
                "started_at",
                "next_retry_at",
                "claimed_at",
                "claimed_until",
                "lease_token",
                "lease_generation",
            )
        )

    try:
        args = [payload[key] for key in argument_keys]
    except KeyError as exc:
        with transaction.atomic():
            receipt = OutboxConsumerReceipt.objects.select_for_update().get(
                event_id=event_id,
                consumer_name=consumer_name,
                lease_token=lease_token,
                lease_generation=lease_generation,
                state=OutboxConsumerReceipt.State.PROCESSING,
                claimed_until__gt=timezone.now(),
            )
            receipt.state = OutboxConsumerReceipt.State.DEAD_LETTER
            receipt.last_error_code = f"missing_payload_{exc.args[0]}"[:120]
            receipt.dead_lettered_at = timezone.now()
            receipt.claimed_at = None
            receipt.claimed_until = None
            receipt.lease_token = None
            receipt.save(
                update_fields=(
                    "state",
                    "last_error_code",
                    "dead_lettered_at",
                    "claimed_at",
                    "claimed_until",
                    "lease_token",
                )
            )
            return {"state": "dead_letter", "attempt": receipt.attempts}

    try:
        # Domain tasks own their transaction boundaries. In particular, publisher
        # tasks commit the pre-call fingerprint before external I/O and use
        # reconcile on ambiguous outcomes.
        with event_context(envelope):
            result = handler(*args)
    except Exception as exc:
        with transaction.atomic():
            receipt = OutboxConsumerReceipt.objects.select_for_update().get(
                event_id=event_id,
                consumer_name=consumer_name,
                lease_token=lease_token,
                lease_generation=lease_generation,
                state=OutboxConsumerReceipt.State.PROCESSING,
                claimed_until__gt=timezone.now(),
            )
            terminal = receipt.attempts >= min(max(max_attempts, 1), 5)
            receipt.state = (
                OutboxConsumerReceipt.State.DEAD_LETTER
                if terminal
                else OutboxConsumerReceipt.State.RETRY
            )
            receipt.last_error_code = str(
                getattr(exc, "code", exc.__class__.__name__)
            )[:120]
            if terminal:
                receipt.dead_lettered_at = timezone.now()
                receipt.next_retry_at = None
            else:
                retry_after = getattr(exc, "retry_after_seconds", None)
                receipt.next_retry_at = timezone.now() + timedelta(
                    seconds=_backoff_seconds(event_id, receipt.attempts, retry_after)
                )
            receipt.claimed_at = None
            receipt.claimed_until = None
            receipt.lease_token = None
            receipt.save(
                update_fields=(
                    "state",
                    "last_error_code",
                    "next_retry_at",
                    "dead_lettered_at",
                    "claimed_at",
                    "claimed_until",
                    "lease_token",
                )
            )
            return {
                "state": receipt.state,
                "attempt": receipt.attempts,
                "retry_at": (
                    receipt.next_retry_at.isoformat() if receipt.next_retry_at else None
                ),
            }

    with transaction.atomic():
        receipt = OutboxConsumerReceipt.objects.select_for_update().get(
            event_id=event_id,
            consumer_name=consumer_name,
            lease_token=lease_token,
            lease_generation=lease_generation,
            state=OutboxConsumerReceipt.State.PROCESSING,
            claimed_until__gt=timezone.now(),
        )
        receipt.state = OutboxConsumerReceipt.State.SUCCEEDED
        receipt.last_error_code = ""
        receipt.completed_at = timezone.now()
        receipt.next_retry_at = None
        receipt.claimed_at = None
        receipt.claimed_until = None
        receipt.lease_token = None
        receipt.save(
            update_fields=(
                "state",
                "last_error_code",
                "completed_at",
                "next_retry_at",
                "claimed_at",
                "claimed_until",
                "lease_token",
            )
        )
        return {"state": "succeeded", "attempt": receipt.attempts, "result": result}
