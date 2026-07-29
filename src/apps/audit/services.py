from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import InitVar, dataclass
from typing import Any

from django.db import connections
from django.utils import timezone

from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)

from .models import AuditEvent, _allow_audit_event_insert
from .redaction import (
    POLICY_VERSION,
    redaction_policy_hash,
    sanitize_audit_key,
    sanitize_metadata,
    sanitize_reason,
    validate_stored_metadata,
)

AUDIT_MATERIAL_ENVELOPE_VERSION = "audit-material-envelope-v1"
AUDIT_IDENTITY_VERSION = "audit-event-identity-v1"
_AUDIT_EVENT_NAMESPACE = uuid.UUID("882d0be7-a2c4-4cf4-91b4-0f9033e0abf1")
_AUDIT_CONTEXT_FACTORY_TOKEN = object()
SUPPORTED_AUDIT_DATABASE_ALIAS = "default"


class AuditContextError(ValueError):
    pass


class AuditTransactionError(RuntimeError):
    pass


class AuditIdentityConflict(AuditContextError):
    pass


def _uuid(value: Any, *, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AuditContextError(f"{field_name} must be a UUID") from exc


def _context_key(value: Any, *, field_name: str, required: bool) -> str | None:
    if value is None:
        if required:
            raise AuditContextError(f"{field_name} is required")
        return None
    if not isinstance(value, str):
        raise AuditContextError(f"{field_name} must be a string")
    try:
        return sanitize_audit_key(value, field_name=field_name)
    except ValueError as exc:
        raise AuditContextError(f"{field_name} is invalid") from exc


def _database_alias(using: str) -> str:
    if not isinstance(using, str) or not using.strip() or using != using.strip():
        raise AuditContextError("database alias must be an explicit non-blank string")
    if using not in connections:
        raise AuditContextError(f"database alias {using!r} is not configured")
    if using != SUPPORTED_AUDIT_DATABASE_ALIAS:
        raise AuditContextError(
            "audited mutations currently support only the default database alias"
        )
    return using


@dataclass(frozen=True, slots=True)
class AuditContext:
    correlation_id: uuid.UUID
    actor_type: str
    actor_id: uuid.UUID | None
    database_alias: str
    reason_code: str | None = None
    request_key: str | None = None
    event_key: str | None = None
    operation_key: str | None = None
    worker_consumer_name: str | None = None
    worker_lease_token: uuid.UUID | None = None
    worker_lease_generation: int | None = None
    _factory_token: InitVar[object | None] = None

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _AUDIT_CONTEXT_FACTORY_TOKEN:
            raise AuditContextError(
                "AuditContext must be created by an explicit provenance factory"
            )
        alias = _database_alias(self.database_alias)
        correlation = _uuid(self.correlation_id, field_name="correlation_id")
        reason = sanitize_reason(self.reason_code)
        actor_id = (
            _uuid(self.actor_id, field_name="actor_id")
            if self.actor_id is not None
            else None
        )
        request_key = _context_key(
            self.request_key, field_name="request_key", required=False
        )
        event_key = _context_key(
            self.event_key,
            field_name="event_key",
            required=self.actor_type == AuditEvent.ActorType.WORKER,
        )
        operation_key = _context_key(
            self.operation_key,
            field_name="operation_key",
            required=self.actor_type == AuditEvent.ActorType.SYSTEM,
        )
        worker_consumer_name = _context_key(
            self.worker_consumer_name,
            field_name="worker_consumer_name",
            required=self.actor_type == AuditEvent.ActorType.WORKER,
        )
        worker_lease_token = (
            _uuid(self.worker_lease_token, field_name="worker_lease_token")
            if self.worker_lease_token is not None
            else None
        )
        worker_lease_generation = self.worker_lease_generation
        if self.actor_type == AuditEvent.ActorType.ADMIN:
            if (
                actor_id is None
                or event_key is not None
                or operation_key is not None
                or worker_consumer_name is not None
                or worker_lease_token is not None
                or worker_lease_generation is not None
            ):
                raise AuditContextError("admin audit provenance is invalid")
        elif self.actor_type == AuditEvent.ActorType.WORKER:
            if (
                actor_id is not None
                or request_key is not None
                or operation_key is not None
                or worker_lease_token is None
                or type(worker_lease_generation) is not int
                or worker_lease_generation < 1
            ):
                raise AuditContextError("worker audit provenance is invalid")
            event_key = str(_uuid(event_key, field_name="event_key"))
        elif self.actor_type == AuditEvent.ActorType.SYSTEM:
            if (
                actor_id is not None
                or request_key is not None
                or event_key is not None
                or worker_consumer_name is not None
                or worker_lease_token is not None
                or worker_lease_generation is not None
            ):
                raise AuditContextError("system audit provenance is invalid")
        else:
            raise AuditContextError("audit actor_type is invalid")
        object.__setattr__(self, "database_alias", alias)
        object.__setattr__(self, "correlation_id", correlation)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "reason_code", reason)
        object.__setattr__(self, "request_key", request_key)
        object.__setattr__(self, "event_key", event_key)
        object.__setattr__(self, "operation_key", operation_key)
        object.__setattr__(self, "worker_consumer_name", worker_consumer_name)
        object.__setattr__(self, "worker_lease_token", worker_lease_token)
        object.__setattr__(
            self,
            "worker_lease_generation",
            worker_lease_generation,
        )

    @classmethod
    def for_admin(
        cls,
        *,
        request,
        reason_code: str | None = None,
        request_key: str | None = None,
        using: str = "default",
    ) -> AuditContext:
        user = getattr(request, "user", None)
        if (
            user is None
            or not getattr(user, "is_authenticated", False)
            or not getattr(user, "is_active", False)
            or not getattr(user, "is_staff", False)
            or getattr(user, "pk", None) is None
        ):
            raise AuditContextError("an active authenticated staff administrator is required")
        alias = _database_alias(using)
        actor_alias = getattr(getattr(user, "_state", None), "db", None)
        if actor_alias is not None and actor_alias != alias:
            raise AuditContextError("administrator and audit database aliases differ")
        correlation = getattr(request, "correlation_id", None)
        if correlation is None:
            raise AuditContextError("request.correlation_id is required")
        return cls(
            correlation_id=_uuid(correlation, field_name="request.correlation_id"),
            actor_type=AuditEvent.ActorType.ADMIN,
            actor_id=_uuid(user.pk, field_name="administrator ID"),
            database_alias=alias,
            reason_code=sanitize_reason(reason_code, required=True),
            request_key=_context_key(
                request_key, field_name="request_key", required=True
            ),
            _factory_token=_AUDIT_CONTEXT_FACTORY_TOKEN,
        )

    @classmethod
    def for_worker(
        cls,
        *,
        correlation_id: uuid.UUID | str,
        event_key: str,
        consumer_name: str,
        lease_token: uuid.UUID | str,
        lease_generation: int,
        reason_code: str | None = None,
        using: str = "default",
    ) -> AuditContext:
        normalized_event_id = str(_uuid(event_key, field_name="event_key"))
        return cls(
            correlation_id=_uuid(correlation_id, field_name="worker correlation_id"),
            actor_type=AuditEvent.ActorType.WORKER,
            actor_id=None,
            database_alias=_database_alias(using),
            reason_code=sanitize_reason(reason_code),
            event_key=normalized_event_id,
            worker_consumer_name=consumer_name,
            worker_lease_token=_uuid(
                lease_token,
                field_name="worker lease_token",
            ),
            worker_lease_generation=lease_generation,
            _factory_token=_AUDIT_CONTEXT_FACTORY_TOKEN,
        )

    @classmethod
    def for_admin_continuation(
        cls,
        *,
        request,
        correlation_id: uuid.UUID | str,
        reason_code: str,
        request_key: str,
        using: str = "default",
    ) -> AuditContext:
        """Restore signed/durable provenance for a multi-request admin operation."""

        user = getattr(request, "user", None)
        if (
            user is None
            or not getattr(user, "is_authenticated", False)
            or not getattr(user, "is_active", False)
            or not getattr(user, "is_staff", False)
            or getattr(user, "pk", None) is None
        ):
            raise AuditContextError(
                "an active authenticated staff administrator is required"
            )
        alias = _database_alias(using)
        actor_alias = getattr(getattr(user, "_state", None), "db", None)
        if actor_alias is not None and actor_alias != alias:
            raise AuditContextError(
                "administrator and audit database aliases differ"
            )
        return cls(
            correlation_id=_uuid(
                correlation_id,
                field_name="continued admin correlation_id",
            ),
            actor_type=AuditEvent.ActorType.ADMIN,
            actor_id=_uuid(user.pk, field_name="administrator ID"),
            database_alias=alias,
            reason_code=sanitize_reason(reason_code, required=True),
            request_key=_context_key(
                request_key,
                field_name="request_key",
                required=True,
            ),
            _factory_token=_AUDIT_CONTEXT_FACTORY_TOKEN,
        )

    @classmethod
    def for_system(
        cls,
        *,
        correlation_id: uuid.UUID | str,
        operation_key: str,
        reason_code: str | None = None,
        using: str = "default",
    ) -> AuditContext:
        return cls(
            correlation_id=_uuid(correlation_id, field_name="system correlation_id"),
            actor_type=AuditEvent.ActorType.SYSTEM,
            actor_id=None,
            database_alias=_database_alias(using),
            reason_code=sanitize_reason(reason_code),
            operation_key=_context_key(
                operation_key, field_name="operation_key", required=True
            ),
            _factory_token=_AUDIT_CONTEXT_FACTORY_TOKEN,
        )

    def provenance_metadata(self) -> dict[str, str]:
        values = (
            ("request_key", self.request_key),
            ("event_key", self.event_key),
            ("operation_key", self.operation_key),
        )
        return {key: value for key, value in values if value is not None}


def canonical_audit_material_hash(
    material: Any,
    *,
    material_schema_version: str,
) -> str:
    schema_version = _context_key(
        material_schema_version,
        field_name="material_schema_version",
        required=True,
    )
    return canonical_hash(
        {
            "envelope_version": AUDIT_MATERIAL_ENVELOPE_VERSION,
            "material_schema_version": schema_version,
            "material": material,
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _event_id(
    *,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    identity_key: str,
) -> uuid.UUID:
    identity_hash = canonical_hash(
        {
            "identity_version": AUDIT_IDENTITY_VERSION,
            "action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "identity_key": identity_key,
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    return uuid.uuid5(_AUDIT_EVENT_NAMESPACE, identity_hash)


def audit_event_id(
    *,
    action: str,
    entity,
    identity_key: str,
) -> uuid.UUID:
    """Return the deterministic primary key for one immutable audit identity."""

    if getattr(entity, "pk", None) is None:
        raise AuditContextError("audit replay entity must have a primary key")
    return _event_id(
        action=_context_key(action, field_name="action", required=True),
        entity_type=entity._meta.label_lower,
        entity_id=_uuid(entity.pk, field_name="entity ID"),
        identity_key=_context_key(
            identity_key,
            field_name="identity_key",
            required=True,
        ),
    )


def _merge_metadata(
    context: AuditContext,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if metadata is None:
        merged: dict[str, Any] = {}
    elif isinstance(metadata, Mapping):
        merged = dict(metadata)
    else:
        raise AuditContextError("audit metadata must be an object")
    for key, value in context.provenance_metadata().items():
        existing = merged.get(key)
        if existing is not None and existing != value:
            raise AuditIdentityConflict(f"audit metadata {key} conflicts with context")
        merged[key] = value
    return merged


def _immutable_values_match(event: AuditEvent, expected: Mapping[str, Any]) -> bool:
    # A later idempotent HTTP replay may have a new request correlation. The first
    # committed correlation remains authoritative; all other immutable values must
    # still match exactly.
    compared_fields = (
        "actor_type",
        "actor_id",
        "action",
        "entity_type",
        "entity_id",
        "before_hash",
        "after_hash",
        "reason_code",
        "metadata_schema_version",
        "redaction_policy_version",
        "redaction_policy_hash",
        "metadata_redacted",
    )
    return all(getattr(event, field) == expected[field] for field in compared_fields)


def require_audit_replay(
    *,
    context: AuditContext,
    action: str,
    entity,
    identity_key: str | None = None,
    event_id: uuid.UUID | str | None = None,
    request_hash: str | None = None,
    metadata_expected: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """Fail closed unless an existing business result has its original audit row."""

    provenance = {
        AuditEvent.ActorType.ADMIN: ("request_key", context.request_key),
        AuditEvent.ActorType.WORKER: ("event_key", context.event_key),
        AuditEvent.ActorType.SYSTEM: ("operation_key", context.operation_key),
    }.get(context.actor_type)
    if provenance is None or not provenance[1]:
        raise AuditIdentityConflict(
            "explicit provenance is required to verify this audit replay"
        )
    if (identity_key is None) == (event_id is None):
        raise AuditIdentityConflict(
            "exactly one audit identity_key or event_id is required"
        )
    entity_alias = getattr(getattr(entity, "_state", None), "db", None)
    if entity_alias != context.database_alias:
        raise AuditIdentityConflict(
            "audit replay entity and context database aliases differ"
        )
    provenance_key, provenance_value = provenance
    expected_event_id = (
        audit_event_id(
            action=action,
            entity=entity,
            identity_key=identity_key,
        )
        if identity_key is not None
        else _uuid(event_id, field_name="audit event_id")
    )
    event = AuditEvent.objects.using(context.database_alias).filter(
        id=expected_event_id,
        action=action,
        entity_type=entity._meta.label_lower,
        entity_id=entity.pk,
    ).first()
    if event is None:
        raise AuditIdentityConflict(
            "existing business result has no matching immutable audit event"
        )
    validated_metadata = validate_stored_metadata(
        action=event.action,
        metadata_schema_version=event.metadata_schema_version,
        redaction_policy_version=event.redaction_policy_version,
        redaction_policy_hash_value=event.redaction_policy_hash,
        metadata=event.metadata_redacted,
    )
    if validated_metadata.get(provenance_key) != provenance_value:
        raise AuditIdentityConflict(
            "stored audit provenance failed validation"
        )
    if any(
        validated_metadata.get(key) != value
        for key, value in (metadata_expected or {}).items()
    ):
        raise AuditIdentityConflict(
            "stored audit replay metadata conflicts with the expected result"
        )
    if (
        event.actor_type != context.actor_type
        or event.actor_id != context.actor_id
        or event.reason_code != context.reason_code
    ):
        raise AuditIdentityConflict(
            "existing audit provenance conflicts with this replay"
        )
    if (
        request_hash is not None
        and validated_metadata.get("request_hash") != request_hash
    ):
        raise AuditIdentityConflict(
            "request key was already used for different immutable material"
        )
    return event


def require_worker_event(
    *,
    context: AuditContext,
    topic: str,
    aggregate_id,
    payload_identity: Mapping[str, Any],
):
    """Bind worker provenance to the persisted event and active consumer receipt."""

    if (
        context.actor_type != AuditEvent.ActorType.WORKER
        or context.event_key is None
    ):
        raise AuditContextError("worker event provenance is required")

    from wisdome_writer.infrastructure.event_routes import route_for
    from wisdome_writer.infrastructure.models import (
        OutboxConsumerReceipt,
        OutboxMessage,
    )

    event = (
        OutboxMessage.objects.using(context.database_alias)
        .select_for_update()
        .filter(
            id=context.event_key,
            topic=topic,
            aggregate_id=aggregate_id,
            correlation_id=context.correlation_id,
        )
        .first()
    )
    if event is None:
        raise AuditIdentityConflict(
            "worker event provenance does not match the persisted event"
        )
    payload = event.payload if isinstance(event.payload, dict) else {}
    if any(
        str(payload.get(key)) != str(value)
        for key, value in payload_identity.items()
    ):
        raise AuditIdentityConflict(
            "worker event payload does not match the audited mutation"
        )
    route = route_for(event.topic, event.event_version)
    if (
        route is None
        or route.consumer_name != context.worker_consumer_name
        or not OutboxConsumerReceipt.objects.using(
            context.database_alias
        ).filter(
            event=event,
            consumer_name=context.worker_consumer_name,
            state=OutboxConsumerReceipt.State.PROCESSING,
            claimed_until__gt=timezone.now(),
            lease_token=context.worker_lease_token,
            lease_generation=context.worker_lease_generation,
        ).exists()
    ):
        raise AuditIdentityConflict(
            "worker event is not owned by the active routed consumer"
        )
    return event


def record_audit_event(
    *,
    context: AuditContext,
    action: str,
    entity,
    identity_key: str,
    material_schema_version: str,
    before_material: Any | None = None,
    after_material: Any | None = None,
    metadata: Mapping[str, Any] | None = None,
    metadata_schema_version: str = "1",
) -> tuple[AuditEvent, bool]:
    alias = context.database_alias
    connection = connections[alias]
    if not connection.in_atomic_block:
        raise AuditTransactionError(
            "AuditEvent insertion requires an existing outer transaction"
        )
    if getattr(entity, "pk", None) is None or getattr(
        getattr(entity, "_state", None), "adding", True
    ):
        raise AuditTransactionError("the audited business entity must already be saved")
    entity_alias = getattr(entity._state, "db", None)
    if entity_alias != alias:
        raise AuditTransactionError("business entity and audit database aliases differ")

    normalized_identity = _context_key(
        identity_key, field_name="identity_key", required=True
    )
    normalized_action = _context_key(
        action, field_name="action", required=True
    )
    normalized_entity_id = _uuid(entity.pk, field_name="entity ID")
    entity_type = entity._meta.label_lower
    normalized_metadata = sanitize_metadata(
        normalized_action,
        _merge_metadata(context, metadata),
        metadata_schema_version=metadata_schema_version,
    )
    before_hash = (
        canonical_audit_material_hash(
            before_material,
            material_schema_version=material_schema_version,
        )
        if before_material is not None
        else None
    )
    after_hash = (
        canonical_audit_material_hash(
            after_material,
            material_schema_version=material_schema_version,
        )
        if after_material is not None
        else None
    )
    policy_hash = redaction_policy_hash(
        normalized_action,
        metadata_schema_version=metadata_schema_version,
    )
    event_id = _event_id(
        action=normalized_action,
        entity_type=entity_type,
        entity_id=normalized_entity_id,
        identity_key=normalized_identity,
    )
    immutable_values = {
        "correlation_id": context.correlation_id,
        "actor_type": context.actor_type,
        "actor_id": context.actor_id,
        "action": normalized_action,
        "entity_type": entity_type,
        "entity_id": normalized_entity_id,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "reason_code": context.reason_code,
        "metadata_schema_version": metadata_schema_version,
        "redaction_policy_version": POLICY_VERSION,
        "redaction_policy_hash": policy_hash,
        "metadata_redacted": normalized_metadata,
    }
    manager = AuditEvent.objects.db_manager(alias)
    with _allow_audit_event_insert():
        event, created = manager.get_or_create(
            id=event_id,
            defaults=immutable_values,
        )
    if not created and not _immutable_values_match(event, immutable_values):
        raise AuditIdentityConflict(
            "audit identity was already used for different immutable content"
        )
    return event, created
