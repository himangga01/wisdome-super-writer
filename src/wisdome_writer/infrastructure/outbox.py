from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlsplit

from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash
from wisdome_writer.observability import correlation_context, current_correlation_id

from .event_routes import payload_schema_for
from .models import OutboxConsumerReceipt, OutboxMessage

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
MAX_DEDUPE_KEY_LENGTH = 200
SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")
SAFE_DOMAIN_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
SAFE_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,119}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SECRET_VALUE_PATTERNS = (
    re.compile(
        r"(?i)(?:access[_-]?token|refresh[_-]?token|api[_-]?key|password|passwd|"
        r"authorization|cookie|client[_-]?secret|credential)\s*(?:=|:|\s)\s*\S+"
    ),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)\btop[-_]?secret\b"),
)


class OutboxConflict(ValueError):
    pass


class ForbiddenEventPayload(ValueError):
    pass


class LostOutboxLease(RuntimeError):
    pass


class PermanentEventError(RuntimeError):
    permanent = True

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


class ConsumerInfrastructureError(DatabaseError):
    def __init__(
        self,
        cause: DatabaseError,
        *,
        lease_token: uuid.UUID,
        lease_generation: int,
    ):
        super().__init__(str(cause))
        self.lease_token = lease_token
        self.lease_generation = lease_generation


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
            if not isinstance(key, str):
                raise ForbiddenEventPayload(f"event payload key must be a string: {path}")
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
    if isinstance(value, str) and any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS):
        raise ForbiddenEventPayload(f"secret-looking event payload value is forbidden: {path}")


def _validate_formatted_value(*, key: str, value: Any, value_format: str) -> None:
    if value is None:
        return
    if value_format == "uuid":
        try:
            uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ForbiddenEventPayload(f"event payload field is not a UUID: {key}") from exc
        return
    if value_format == "sha256":
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            raise ForbiddenEventPayload(
                f"event payload field is not a lowercase SHA-256 digest: {key}"
            )
        return
    if value_format == "version":
        if not isinstance(value, str) or SAFE_VERSION_RE.fullmatch(value) is None:
            raise ForbiddenEventPayload(f"event payload field is not a safe version: {key}")
        return
    if value_format == "domain_key":
        if not isinstance(value, str) or SAFE_DOMAIN_KEY_RE.fullmatch(value) is None:
            raise ForbiddenEventPayload(f"event payload field is not a safe domain key: {key}")
        return
    if value_format == "positive_integer":
        if type(value) is not int or value < 1:
            raise ForbiddenEventPayload(
                f"event payload field is not a positive integer: {key}"
            )
        return
    if value_format == "url":
        if not isinstance(value, str):
            raise ForbiddenEventPayload(f"event payload URL field must be a string: {key}")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
        ):
            raise ForbiddenEventPayload(
                f"event payload URL cannot contain credentials, query, or fragment: {key}"
            )
        return
    raise ForbiddenEventPayload(f"unknown event payload field format: {key}")


def _validate_event_payload(
    event_type: str,
    event_version: int,
    payload: dict[str, Any],
) -> None:
    _validate_payload(payload)
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ForbiddenEventPayload("event payload exceeds the 64 KiB limit")
    schema = payload_schema_for(event_type, event_version)
    if schema is None:
        raise ForbiddenEventPayload(
            f"no payload schema is registered for event: {event_type}@{event_version}"
        )
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
        if key in schema.formats:
            _validate_formatted_value(
                key=key,
                value=value,
                value_format=schema.formats[key],
            )
        if key in schema.choices and value not in schema.choices[key]:
            raise ForbiddenEventPayload(f"event payload field has unsupported value: {key}")


def _validate_policy_versions(policy_versions: Any) -> dict[str, int | str]:
    if not isinstance(policy_versions, dict):
        raise ForbiddenEventPayload("policy_versions must be a JSON object")
    _validate_payload(policy_versions, path="policy_versions")
    encoded = json.dumps(
        policy_versions,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_POLICY_BYTES:
        raise ForbiddenEventPayload("policy_versions exceeds the 8 KiB limit")
    for key, value in policy_versions.items():
        if SAFE_DOMAIN_KEY_RE.fullmatch(key) is None:
            raise ForbiddenEventPayload("policy_versions contains an invalid policy name")
        if type(value) is int:
            if value < 1:
                raise ForbiddenEventPayload("policy_versions integers must be positive")
            continue
        if type(value) is str:
            if (
                SAFE_VERSION_RE.fullmatch(value) is None
                or (
                    not any(character.isdigit() for character in value)
                    and not SHA256_RE.fullmatch(value)
                )
            ):
                raise ForbiddenEventPayload("policy_versions contains an invalid version")
            continue
        raise ForbiddenEventPayload("policy_versions values must be positive integers or versions")
    return policy_versions


def _material(
    *,
    event_id: uuid.UUID,
    event_type: str,
    event_version: int,
    occurred_at: datetime,
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
        "event_id": str(event_id),
        "event_type": event_type,
        "event_version": event_version,
        "occurred_at": occurred_at.isoformat(),
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


def _material_hash(
    *,
    event_id: uuid.UUID,
    event_type: str,
    event_version: int,
    occurred_at: datetime,
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
) -> str:
    return canonical_hash(
        _material(
            event_id=event_id,
            event_type=event_type,
            event_version=event_version,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            job_id=job_id,
            entity_type=entity_type,
            entity_id=entity_id,
            operation=operation,
            dedupe_key=dedupe_key,
            policy_versions=policy_versions,
            not_before=not_before,
            payload=payload,
        ),
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _validate_material_identity(
    *,
    event_type: Any,
    event_version: Any,
    occurred_at: Any,
    correlation_id: Any,
    causation_id: Any,
    job_id: Any,
    entity_type: Any,
    entity_id: Any,
    operation: Any,
    dedupe_key: Any,
) -> None:
    if not isinstance(event_type, str) or SAFE_EVENT_TYPE_RE.fullmatch(event_type) is None:
        raise ForbiddenEventPayload("event_type is invalid")
    if type(event_version) is not int or event_version < 1:
        raise ForbiddenEventPayload("event_version must be a positive integer")
    if not isinstance(occurred_at, datetime) or timezone.is_naive(occurred_at):
        raise ForbiddenEventPayload("occurred_at must be an aware datetime")
    for key, value, nullable in (
        ("correlation_id", correlation_id, False),
        ("causation_id", causation_id, True),
        ("job_id", job_id, False),
        ("entity_id", entity_id, False),
    ):
        if value is None and nullable:
            continue
        try:
            uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ForbiddenEventPayload(f"{key} must be a UUID") from exc
    for key, value in (("entity_type", entity_type), ("operation", operation)):
        if not isinstance(value, str) or SAFE_NAME_RE.fullmatch(value) is None:
            raise ForbiddenEventPayload(f"{key} is invalid")
    if (
        not isinstance(dedupe_key, str)
        or not dedupe_key
        or len(dedupe_key) > MAX_DEDUPE_KEY_LENGTH
        or any(pattern.search(dedupe_key) for pattern in SECRET_VALUE_PATTERNS)
    ):
        raise ForbiddenEventPayload("dedupe_key is invalid")


def compute_material_hash(message: OutboxMessage) -> str:
    return _material_hash(
        event_id=message.id,
        event_type=message.topic,
        event_version=message.event_version,
        occurred_at=message.occurred_at,
        correlation_id=message.correlation_id,
        causation_id=message.causation_id,
        job_id=message.job_id,
        entity_type=message.aggregate_type,
        entity_id=message.aggregate_id,
        operation=message.operation,
        dedupe_key=message.message_key,
        policy_versions=message.policy_versions,
        not_before=message.not_before,
        payload=message.payload,
    )


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
    _validate_event_payload(event_type, event_version, payload)
    policy_versions = _validate_policy_versions(
        {} if policy_versions is None else policy_versions
    )
    entity_id = _uuid(aggregate_id)
    entity_type = aggregate_type or event_type.split(".", 1)[0]
    dedupe_key = dedupe_key or message_key
    if not dedupe_key:
        raise ValueError("outbox dedupe_key is required")
    requested_not_before = available_at
    not_before = requested_not_before or timezone.now()
    if not isinstance(not_before, datetime) or timezone.is_naive(not_before):
        raise ForbiddenEventPayload("not_before must be an aware datetime")
    inherited_correlation = CURRENT_EVENT_CORRELATION_ID.get()
    correlation_is_implicit = correlation_id is None and inherited_correlation is None
    raw_correlation = correlation_id or inherited_correlation
    if raw_correlation is not None:
        resolved_correlation = _uuid(raw_correlation)
    else:
        try:
            resolved_correlation = _uuid(current_correlation_id())
        except ValueError:
            resolved_correlation = uuid.uuid4()
    inherited_causation = CURRENT_EVENT_ID.get()
    resolved_causation = (
        _uuid(causation_id or inherited_causation)
        if causation_id or inherited_causation
        else None
    )
    resolved_job_id = _uuid(job_id) if job_id is not None else entity_id
    event_id = uuid.uuid4()
    occurred_at = timezone.now()
    _validate_material_identity(
        event_type=event_type,
        event_version=event_version,
        occurred_at=occurred_at,
        correlation_id=resolved_correlation,
        causation_id=resolved_causation,
        job_id=resolved_job_id,
        entity_type=entity_type,
        entity_id=entity_id,
        operation=operation,
        dedupe_key=dedupe_key,
    )
    material_hash = _material_hash(
        event_id=event_id,
        event_type=event_type,
        event_version=event_version,
        occurred_at=occurred_at,
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
    defaults = {
        "id": event_id,
        "topic": event_type,
        "event_version": event_version,
        "occurred_at": occurred_at,
        "aggregate_type": entity_type,
        "aggregate_id": entity_id,
        "payload": payload,
        "correlation_id": resolved_correlation,
        "causation_id": resolved_causation,
        "job_id": resolved_job_id,
        "operation": operation,
        "policy_versions": policy_versions,
        "immutable_material_hash": material_hash,
        "not_before": not_before,
        "available_at": not_before,
        "max_attempts": min(max(int(max_attempts), 1), 5),
    }

    def comparison_hash(existing: OutboxMessage) -> str:
        validate_persisted_event(existing)
        return _material_hash(
            event_id=existing.id,
            event_type=event_type,
            event_version=event_version,
            occurred_at=existing.occurred_at,
            correlation_id=(
                existing.correlation_id
                if correlation_is_implicit
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
                existing.not_before
                if requested_not_before is None
                else requested_not_before
            ),
            payload=payload,
        )

    with transaction.atomic():
        existing = OutboxMessage.objects.select_for_update().filter(message_key=dedupe_key).first()
        if existing is not None:
            candidate_hash = comparison_hash(existing)
            if not hmac.compare_digest(existing.immutable_material_hash, candidate_hash):
                raise OutboxConflict("dedupe key already exists with different immutable material")
            return existing
        try:
            with transaction.atomic():
                return OutboxMessage.objects.create(message_key=dedupe_key, **defaults)
        except IntegrityError:
            existing = OutboxMessage.objects.select_for_update().get(message_key=dedupe_key)
            candidate_hash = comparison_hash(existing)
            if not hmac.compare_digest(existing.immutable_material_hash, candidate_hash):
                raise OutboxConflict(
                    "dedupe key concurrently created with different immutable material"
                )
            return existing


@transaction.atomic
def claim_exhausted_events(
    *,
    limit: int = 100,
    lease_seconds: int = 60,
    lease_owner: str = "outbox-dispatcher",
) -> list[OutboxMessage]:
    now = timezone.now()
    messages = list(
        OutboxMessage.objects.select_for_update(skip_locked=True)
        .filter(
            status=OutboxMessage.Status.PENDING,
            available_at__lte=now,
            attempts__gte=F("max_attempts"),
        )
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
        message.save(
            update_fields=(
                "status",
                "claimed_at",
                "claimed_until",
                "lease_owner",
                "lease_token",
                "lease_generation",
            )
        )
    return messages


@transaction.atomic
def claim_events(
    *,
    limit: int = 100,
    lease_seconds: int = 60,
    lease_owner: str = "outbox-dispatcher",
) -> list[OutboxMessage]:
    now = timezone.now()
    claimable = Q(
        status=OutboxMessage.Status.PENDING,
        attempts__lt=F("max_attempts"),
    ) | Q(
        status=OutboxMessage.Status.DISPATCHING,
        claimed_until__lte=now,
    )
    messages = list(
        OutboxMessage.objects.select_for_update(skip_locked=True)
        .filter(claimable, available_at__lte=now)
        .filter(Q(claimed_until__isnull=True) | Q(claimed_until__lte=now))
        .order_by("available_at", "created_at", "id")[: max(1, limit)]
    )
    lease_until = now + timedelta(seconds=max(1, lease_seconds))
    for message in messages:
        starts_new_attempt = message.status == OutboxMessage.Status.PENDING
        message.status = OutboxMessage.Status.DISPATCHING
        message.claimed_at = now
        message.claimed_until = lease_until
        message.lease_owner = lease_owner[:160]
        message.lease_token = uuid.uuid4()
        message.lease_generation += 1
        update_fields = [
            "status",
            "claimed_at",
            "claimed_until",
            "lease_owner",
            "lease_token",
            "lease_generation",
        ]
        if starts_new_attempt:
            message.attempts += 1
            update_fields.append("attempts")
        message.save(update_fields=update_fields)
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
        "not_before": message.not_before.isoformat(),
        "payload": message.payload,
    }


def validate_persisted_event(message: OutboxMessage) -> None:
    if not isinstance(message.payload, dict):
        raise ForbiddenEventPayload("persisted event payload must be a JSON object")
    _validate_material_identity(
        event_type=message.topic,
        event_version=message.event_version,
        occurred_at=message.occurred_at,
        correlation_id=message.correlation_id,
        causation_id=message.causation_id,
        job_id=message.job_id,
        entity_type=message.aggregate_type,
        entity_id=message.aggregate_id,
        operation=message.operation,
        dedupe_key=message.message_key,
    )
    if not isinstance(message.not_before, datetime) or timezone.is_naive(
        message.not_before
    ):
        raise ForbiddenEventPayload("persisted not_before must be an aware datetime")
    _validate_event_payload(message.topic, message.event_version, message.payload)
    _validate_policy_versions(message.policy_versions)
    if SHA256_RE.fullmatch(message.immutable_material_hash or "") is None:
        raise OutboxConflict("persisted immutable material hash is malformed")
    recomputed = compute_material_hash(message)
    if not hmac.compare_digest(message.immutable_material_hash, recomputed):
        raise OutboxConflict("persisted immutable material hash does not match the event")


@transaction.atomic
def dead_letter_consumer_event(
    event_id: uuid.UUID | str,
    *,
    consumer_name: str,
    error_code: str,
    terminal_handler: Callable[..., Any] | None = None,
    terminal_argument_keys: tuple[str, ...] = (),
    increment_attempt: bool = True,
    expected_dispatch_lease_token: uuid.UUID | str | None = None,
    expected_dispatch_lease_generation: int | None = None,
    expected_consumer_lease_token: uuid.UUID | str | None = None,
    expected_consumer_lease_generation: int | None = None,
) -> dict[str, Any]:
    now = timezone.now()
    event = OutboxMessage.objects.select_for_update().get(pk=event_id)
    receipt, receipt_created = (
        OutboxConsumerReceipt.objects.select_for_update().get_or_create(
        event=event,
        consumer_name=consumer_name,
        )
    )
    dispatch_fenced = expected_dispatch_lease_token is not None
    if dispatch_fenced and (
        event.status != OutboxMessage.Status.DISPATCHING
        or event.lease_token != _uuid(expected_dispatch_lease_token)
        or event.lease_generation != expected_dispatch_lease_generation
        or event.claimed_until is None
        or event.claimed_until <= now
    ):
        return {"state": "stale_fenced", "attempt": receipt.attempts}
    succeeded = (
        receipt.state == OutboxConsumerReceipt.State.SUCCEEDED
        or event.consumer_receipts.filter(
            state=OutboxConsumerReceipt.State.SUCCEEDED
        ).exists()
    )
    if dispatch_fenced and (
        succeeded
        or (
            not receipt_created
            and receipt.state
            in {
                OutboxConsumerReceipt.State.PROCESSING,
                OutboxConsumerReceipt.State.RETRY,
            }
        )
    ):
        event.status = OutboxMessage.Status.PUBLISHED
        event.published_at = event.published_at or now
        event.claimed_at = None
        event.claimed_until = None
        event.lease_owner = ""
        event.lease_token = None
        event.last_error_code = None
        event.last_error_at = None
        event.save(
            update_fields=(
                "status",
                "published_at",
                "claimed_at",
                "claimed_until",
                "lease_owner",
                "lease_token",
                "last_error_code",
                "last_error_at",
            )
        )
        return {"state": "accepted", "attempt": receipt.attempts}
    if succeeded:
        return {"state": "duplicate", "attempt": receipt.attempts}
    if receipt.state == OutboxConsumerReceipt.State.DEAD_LETTER:
        if event.status != OutboxMessage.Status.DEAD_LETTER:
            event.status = OutboxMessage.Status.DEAD_LETTER
            event.dead_lettered_at = receipt.dead_lettered_at or now
            event.last_error_at = now
            event.last_error_code = (
                receipt.last_error_code or error_code[:120]
            )
            event.claimed_at = None
            event.claimed_until = None
            event.lease_owner = ""
            event.lease_token = None
            event.save(
                update_fields=(
                    "status",
                    "dead_lettered_at",
                    "last_error_at",
                    "last_error_code",
                    "claimed_at",
                    "claimed_until",
                    "lease_owner",
                    "lease_token",
                )
            )
        return {"state": "dead_letter", "attempt": receipt.attempts}
    if expected_consumer_lease_token is not None and (
        receipt.state != OutboxConsumerReceipt.State.PROCESSING
        or receipt.lease_token != _uuid(expected_consumer_lease_token)
        or receipt.lease_generation != expected_consumer_lease_generation
    ):
        return {"state": "stale_fenced", "attempt": receipt.attempts}
    if terminal_handler is not None:
        if len(terminal_argument_keys) == 1:
            terminal_args = [str(event.aggregate_id)]
        else:
            payload = event.payload if isinstance(event.payload, dict) else {}
            terminal_args = [payload[key] for key in terminal_argument_keys]
        with event_context(event_envelope(event)):
            terminal_handler(*terminal_args, error_code[:120])
    receipt.state = OutboxConsumerReceipt.State.DEAD_LETTER
    if increment_attempt:
        receipt.attempts += 1
    receipt.last_error_code = error_code[:120]
    receipt.dead_lettered_at = now
    receipt.next_retry_at = None
    receipt.claimed_at = None
    receipt.claimed_until = None
    receipt.lease_token = None
    receipt.save(
        update_fields=(
            "state",
            "attempts",
            "last_error_code",
            "dead_lettered_at",
            "next_retry_at",
            "claimed_at",
            "claimed_until",
            "lease_token",
        )
    )
    event.status = OutboxMessage.Status.DEAD_LETTER
    event.dead_lettered_at = now
    event.last_error_at = now
    event.last_error_code = error_code[:120]
    event.claimed_at = None
    event.claimed_until = None
    event.lease_owner = ""
    event.lease_token = None
    event.save(
        update_fields=(
            "status",
            "dead_lettered_at",
            "last_error_at",
            "last_error_code",
            "claimed_at",
            "claimed_until",
            "lease_owner",
            "lease_token",
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


def mark_terminal_retry(
    message: OutboxMessage,
    *,
    error_code: str,
) -> None:
    now = timezone.now()
    updates = {
        "status": OutboxMessage.Status.PENDING,
        "available_at": now
        + timedelta(seconds=_backoff_seconds(message.id, message.attempts)),
        "last_error_code": error_code[:120],
        "last_error_at": now,
        "claimed_at": None,
        "claimed_until": None,
        "lease_owner": "",
        "lease_token": None,
    }
    if _lease_filter(message).update(**updates) != 1:
        raise LostOutboxLease(
            "stale dispatcher cannot release failed terminalization"
        )


def _verify_received_envelope(event: OutboxMessage, envelope: dict[str, Any]) -> None:
    if not isinstance(envelope, dict):
        raise ForbiddenEventPayload("received event envelope must be an object")
    expected = event_envelope(event)
    received_keys = set(envelope)
    expected_keys = set(expected)
    if received_keys != expected_keys:
        missing = ",".join(sorted(expected_keys - received_keys))
        extra = ",".join(sorted(received_keys - expected_keys))
        raise OutboxConflict(
            f"received envelope fields do not match contract: missing={missing}; extra={extra}"
        )
    if type(envelope["attempt"]) is not int or envelope["attempt"] < 1:
        raise ForbiddenEventPayload("received event attempt must be a positive integer")
    if envelope["attempt"] > event.attempts:
        raise OutboxConflict("received event attempt is from a future dispatch generation")
    for key, expected_value in expected.items():
        if key == "attempt":
            continue
        if envelope[key] != expected_value:
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


def _lock_owned_consumer_receipt(
    *,
    event_id: uuid.UUID,
    consumer_name: str,
    lease_token: uuid.UUID,
    lease_generation: int,
) -> OutboxConsumerReceipt | None:
    receipt = (
        OutboxConsumerReceipt.objects.select_for_update()
        .filter(
            event_id=event_id,
            consumer_name=consumer_name,
            state=OutboxConsumerReceipt.State.PROCESSING,
        )
        .first()
    )
    if (
        receipt is None
        or receipt.lease_token != lease_token
        or receipt.lease_generation != lease_generation
    ):
        return None
    return receipt


def consume_event(
    *,
    envelope: dict[str, Any],
    consumer_name: str,
    handler: Callable[..., Any],
    argument_keys: tuple[str, ...],
    terminal_handler: Callable[..., Any] | None = None,
    terminal_argument_keys: tuple[str, ...] = (),
    max_attempts: int = 5,
    infrastructure_lease_token: uuid.UUID | str | None = None,
    infrastructure_lease_generation: int | None = None,
) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise ForbiddenEventPayload("received event envelope must be an object")
    payload = envelope.get("payload")
    event_id = _uuid(envelope.get("event_id"))
    now = timezone.now()
    consumer_attempt_limit = min(max(max_attempts, 1), 5)
    with transaction.atomic():
        event = OutboxMessage.objects.select_for_update().get(pk=event_id)
        receipt, _ = OutboxConsumerReceipt.objects.select_for_update().get_or_create(
            event=event,
            consumer_name=consumer_name,
        )
        if receipt.state == OutboxConsumerReceipt.State.SUCCEEDED:
            return {"state": "duplicate", "attempt": receipt.attempts}
        if receipt.state == OutboxConsumerReceipt.State.DEAD_LETTER:
            return {"state": "dead_letter", "attempt": receipt.attempts}
        if event.status == OutboxMessage.Status.DEAD_LETTER:
            return dead_letter_consumer_event(
                event_id,
                consumer_name=consumer_name,
                error_code=(
                    event.last_error_code
                    or "persisted_event_dead_letter"
                ),
                terminal_handler=terminal_handler,
                terminal_argument_keys=terminal_argument_keys,
                increment_attempt=False,
            )
        if (
            receipt.state == OutboxConsumerReceipt.State.PROCESSING
            and infrastructure_lease_token is not None
            and receipt.lease_token == _uuid(infrastructure_lease_token)
            and receipt.lease_generation == infrastructure_lease_generation
        ):
            receipt.state = OutboxConsumerReceipt.State.RETRY
            receipt.attempts = max(receipt.attempts - 1, 0)
            receipt.next_retry_at = None
            receipt.claimed_at = None
            receipt.claimed_until = None
            receipt.lease_token = None
        try:
            if not isinstance(payload, dict):
                raise ForbiddenEventPayload("received event payload must be an object")
            _verify_received_envelope(event, envelope)
            validate_persisted_event(event)
        except (ForbiddenEventPayload, OutboxConflict) as exc:
            return dead_letter_consumer_event(
                event_id,
                consumer_name=consumer_name,
                error_code=str(
                    getattr(exc, "code", exc.__class__.__name__)
                ),
                terminal_handler=terminal_handler,
                terminal_argument_keys=terminal_argument_keys,
            )
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
        if receipt.attempts >= consumer_attempt_limit:
            return dead_letter_consumer_event(
                event_id,
                consumer_name=consumer_name,
                error_code="consumer_attempts_exhausted",
                terminal_handler=terminal_handler,
                terminal_argument_keys=terminal_argument_keys,
                increment_attempt=False,
            )
        receipt.state = OutboxConsumerReceipt.State.PROCESSING
        receipt.attempts += 1
        receipt.started_at = now
        receipt.next_retry_at = None
        receipt.claimed_at = now
        receipt.claimed_until = now + timedelta(
            seconds=max(int(settings.OUTBOX_CONSUMER_LEASE_SECONDS), 1)
        )
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

    def consumer_infrastructure_error(
        exc: DatabaseError,
    ) -> ConsumerInfrastructureError:
        try:
            with transaction.atomic():
                owned_receipt = _lock_owned_consumer_receipt(
                    event_id=event_id,
                    consumer_name=consumer_name,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                )
                if owned_receipt is not None:
                    owned_receipt.state = OutboxConsumerReceipt.State.RETRY
                    owned_receipt.attempts = max(
                        owned_receipt.attempts - 1,
                        0,
                    )
                    owned_receipt.last_error_code = ""
                    owned_receipt.next_retry_at = None
                    owned_receipt.claimed_at = None
                    owned_receipt.claimed_until = None
                    owned_receipt.lease_token = None
                    owned_receipt.save(
                        update_fields=(
                            "state",
                            "attempts",
                            "last_error_code",
                            "next_retry_at",
                            "claimed_at",
                            "claimed_until",
                            "lease_token",
                        )
                    )
        except DatabaseError as release_exc:
            return ConsumerInfrastructureError(
                release_exc,
                lease_token=lease_token,
                lease_generation=lease_generation,
            )
        return ConsumerInfrastructureError(
            exc,
            lease_token=lease_token,
            lease_generation=lease_generation,
        )

    try:
        args = [payload[key] for key in argument_keys]
    except KeyError as exc:
        try:
            return dead_letter_consumer_event(
                event_id,
                consumer_name=consumer_name,
                error_code=f"missing_payload_{exc.args[0]}",
                terminal_handler=terminal_handler,
                terminal_argument_keys=terminal_argument_keys,
                increment_attempt=False,
                expected_consumer_lease_token=lease_token,
                expected_consumer_lease_generation=lease_generation,
            )
        except DatabaseError as settlement_exc:
            raise consumer_infrastructure_error(
                settlement_exc
            ) from settlement_exc

    try:
        # Domain tasks own their transaction boundaries. In particular, publisher
        # tasks commit the pre-call fingerprint before external I/O and use
        # reconcile on ambiguous outcomes.
        with event_context(envelope):
            result = handler(*args)
    except DatabaseError as exc:
        raise consumer_infrastructure_error(exc) from exc
    except Exception as exc:
        try:
            with transaction.atomic():
                event = OutboxMessage.objects.select_for_update().get(
                    pk=event_id
                )
                receipt = _lock_owned_consumer_receipt(
                    event_id=event_id,
                    consumer_name=consumer_name,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                )
                if receipt is None:
                    return {"state": "stale_fenced", "attempt": None}
                terminal = bool(getattr(exc, "permanent", False)) or (
                    receipt.attempts >= consumer_attempt_limit
                )
                error_code = str(
                    getattr(exc, "code", exc.__class__.__name__)
                )[:120]
                if terminal:
                    return dead_letter_consumer_event(
                        event_id,
                        consumer_name=consumer_name,
                        error_code=error_code,
                        terminal_handler=terminal_handler,
                        terminal_argument_keys=terminal_argument_keys,
                        increment_attempt=False,
                        expected_consumer_lease_token=lease_token,
                        expected_consumer_lease_generation=lease_generation,
                    )
                receipt.state = (
                    OutboxConsumerReceipt.State.RETRY
                )
                receipt.last_error_code = error_code
                retry_after = getattr(
                    exc,
                    "retry_after_seconds",
                    None,
                )
                receipt.next_retry_at = timezone.now() + timedelta(
                    seconds=_backoff_seconds(
                        event_id,
                        receipt.attempts,
                        retry_after,
                    )
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
                        receipt.next_retry_at.isoformat()
                        if receipt.next_retry_at
                        else None
                    ),
                }
        except DatabaseError as settlement_exc:
            raise consumer_infrastructure_error(
                settlement_exc
            ) from settlement_exc

    try:
        with transaction.atomic():
            receipt = _lock_owned_consumer_receipt(
                event_id=event_id,
                consumer_name=consumer_name,
                lease_token=lease_token,
                lease_generation=lease_generation,
            )
            if receipt is None:
                return {"state": "stale_fenced", "attempt": None}
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
            return {
                "state": "succeeded",
                "attempt": receipt.attempts,
                "result": result,
            }
    except DatabaseError as settlement_exc:
        raise consumer_infrastructure_error(
            settlement_exc
        ) from settlement_exc
