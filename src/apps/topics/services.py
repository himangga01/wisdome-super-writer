from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.text import slugify

from apps.accounts.services import consume_reauthentication_proof
from apps.audit.models import AuditEvent
from apps.audit.redaction import AuditRedactionError, sanitize_audit_key
from apps.audit.services import (
    AuditContext,
    audit_event_id,
    record_audit_event,
    require_audit_replay,
)
from wisdome_writer.domain.concurrency import require_idempotent_match
from wisdome_writer.domain.errors import (
    Conflict,
    InvalidInput,
    RequestKeyConflict,
    StaleVersion,
    StateConflict,
)
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)
from wisdome_writer.infrastructure.secrets import SecretRef

from .models import (
    SourceDefinition,
    SourceDefinitionSnapshot,
    SourceRegistryDecision,
    SourceRegistryMembership,
    SourceRegistryMutation,
    SourceRegistrySnapshot,
    TopicCode,
    TopicPolicy,
    TopicRegistryHead,
)


SOURCE_SNAPSHOT_SCHEMA_V2 = "source-definition-snapshot-v2"
SOURCE_REGISTRY_MANIFEST_SCHEMA_V2 = "source-registry-manifest-v2"
SOURCE_AUDIT_SCHEMA_V2 = "source-definition-audit-v2"
REGISTRY_AUDIT_SCHEMA_V2 = "source-registry-audit-v2"

SUPPORTED_ADAPTER_KEYS = frozenset(
    {
        "housing_applyhome",
        "housing_lh",
        "open_data_json",
        "public_html",
        "rss",
    }
)
_ADAPTER_ACCESS_METHODS = {
    "housing_applyhome": frozenset({"public_html"}),
    "housing_lh": frozenset({"public_html"}),
    "open_data_json": frozenset({"open_data_api", "public_api"}),
    "public_html": frozenset({"public_file", "public_html"}),
    "rss": frozenset({"rss_atom"}),
}
_ADAPTER_CONFIG_KEYS = {
    adapter_key: frozenset({"entrypoints"})
    for adapter_key in SUPPORTED_ADAPTER_KEYS
}
_ADAPTER_KEY_PATTERN = re.compile(r"^[a-z0-9_.-]+$")
_SECRET_KEY_PATTERN = re.compile(
    r"(?:authorization|cookie|token|password|passwd|secret|api.?key|"
    r"credential|access.?key|private.?key|client.?secret|session)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(
        r"(?:authorization|cookie|token|password|passwd|api.?key|"
        r"credential|client.?secret)\s*(?:=|:|\s)\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+",
        re.IGNORECASE,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\btop[-_]?secret\b", re.IGNORECASE),
)
_SECRET_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "key",
        "password",
        "secret",
        "service_key",
        "servicekey",
        "session",
        "sig",
        "signature",
        "subscription_key",
        "subscriptionkey",
        "token",
        "x_auth",
        "x_amz_signature",
        "x_goog_signature",
    }
)
_SECRET_COMPACT_KEYS = frozenset(
    re.sub(r"[^a-z0-9]+", "", key)
    for key in _SECRET_QUERY_KEYS
)
_SOURCE_CONTRACT_FIELDS = (
    "topic",
    "name",
    "publisher",
    "authorityTier",
    "independenceGroupId",
    "ownerName",
    "editorialControlName",
    "baseUrl",
    "accessMethod",
    "adapterKey",
    "externalConfig",
    "secretRef",
    "allowedMimeTypes",
    "defaultRightsStatus",
    "termsUrl",
    "robotsUrl",
    "licenseUrl",
    "pollIntervalSeconds",
    "rateLimitPolicy",
    "enabled",
)


@dataclass(frozen=True)
class RegistryImportResult:
    topic_code: str
    registry_id: str
    source_count: int
    created: bool


def _hash(material: Any) -> str:
    try:
        return canonical_hash(
            material,
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        )
    except (OverflowError, TypeError, UnicodeError, ValueError) as exc:
        raise InvalidInput("Canonical source material is invalid.") from exc


def source_registry_audit_request_key(request_key: str) -> str:
    """Preserve legacy-safe keys and hash only contract-valid unsafe keys."""

    try:
        return sanitize_audit_key(
            request_key,
            field_name="source registry request key",
        )
    except AuditRedactionError:
        return _hash(
            {
                "schemaVersion": "source-registry-audit-request-key-v1",
                "requestKey": request_key,
            }
        )


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


def _require_admin_context(
    *,
    audit_context: AuditContext,
    admin: Any,
    request_key: str,
    reason: str,
) -> None:
    if (
        audit_context.actor_type != AuditEvent.ActorType.ADMIN
        or _id(audit_context.actor_id) != _id(admin.pk)
        or audit_context.request_key
        != source_registry_audit_request_key(request_key)
        or audit_context.reason_code != reason
    ):
        raise ValueError(
            "Audit provenance does not match the source registry administrator."
        )


def _require_admin_audit_replay(
    *,
    context: AuditContext,
    action: str,
    entity: Any,
    identity_key: str,
    request_hash: str,
    admin: Any,
) -> None:
    normalized_identity_key = source_registry_audit_request_key(
        identity_key
    )
    expected_event_id = audit_event_id(
        action=action,
        entity=entity,
        identity_key=normalized_identity_key,
    )
    stored = (
        AuditEvent.objects.using(context.database_alias)
        .only("actor_type", "actor_id")
        .filter(pk=expected_event_id)
        .first()
    )
    if stored is not None and (
        stored.actor_type != AuditEvent.ActorType.ADMIN
        or stored.actor_id != admin.pk
    ):
        raise RequestKeyConflict(
            "The request key belongs to another administrator."
        )
    require_audit_replay(
        context=context,
        action=action,
        entity=entity,
        identity_key=normalized_identity_key,
        request_hash=request_hash,
    )


def _require_request_hash(request_hash: str) -> str:
    if not isinstance(request_hash, str) or not re.fullmatch(
        r"[a-f0-9]{64}",
        request_hash,
    ):
        raise ValueError("A canonical OpenAPI request hash is required.")
    return request_hash


def _validate_public_url(
    value: Any,
    *,
    field_name: str,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > 500:
        raise InvalidInput(f"{field_name} must be a public HTTP URL.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise InvalidInput(f"{field_name} is invalid.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise InvalidInput(
            f"{field_name} must not contain credentials or an invalid target."
        )
    decoded_value = unquote(value)
    if any(ord(character) < 32 for character in decoded_value) or any(
        pattern.search(decoded_value)
        for pattern in _SECRET_VALUE_PATTERNS
    ):
        raise InvalidInput(
            f"{field_name} must not contain credential material."
        )
    del port
    for key, query_value in parse_qsl(
        parsed.query,
        keep_blank_values=True,
    ):
        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        if normalized_key in _SECRET_QUERY_KEYS or _SECRET_KEY_PATTERN.search(
            normalized_key
        ):
            raise InvalidInput(
                f"{field_name} must not contain credential query parameters."
            )
        if any(
            pattern.search(query_value)
            for pattern in _SECRET_VALUE_PATTERNS
        ):
            raise InvalidInput(
                f"{field_name} must not contain credential query values."
            )
    return value


def _validate_external_config(
    value: Any,
    *,
    base_url: str,
    depth: int = 0,
    path: str = "externalConfig",
) -> Any:
    if depth > 8:
        raise InvalidInput("externalConfig is nested too deeply.")
    if isinstance(value, Mapping):
        if len(value) > 200:
            raise InvalidInput("externalConfig contains too many fields.")
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 200:
                raise InvalidInput("externalConfig contains an invalid field name.")
            normalized_key = re.sub(
                r"[^a-z0-9]+",
                "",
                key.lower(),
            )
            if (
                normalized_key in _SECRET_COMPACT_KEYS
                or _SECRET_KEY_PATTERN.search(normalized_key)
            ):
                raise InvalidInput(
                    "externalConfig must not contain credential fields."
                )
            normalized[key] = _validate_external_config(
                item,
                base_url=base_url,
                depth=depth + 1,
                path=f"{path}.{key}",
            )
        return normalized
    if isinstance(value, list):
        if len(value) > 500:
            raise InvalidInput("externalConfig contains too many list items.")
        return [
            _validate_external_config(
                item,
                base_url=base_url,
                depth=depth + 1,
                path=f"{path}[]",
            )
            for item in value
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str) or len(value) > 4000:
        raise InvalidInput("externalConfig contains an invalid value.")
    if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise InvalidInput("externalConfig must not contain credential values.")
    if "://" in value:
        target = _validate_public_url(value, field_name=path)
        base_host = (urlsplit(base_url).hostname or "").rstrip(".").lower()
        target_host = (urlsplit(target).hostname or "").rstrip(".").lower()
        if target_host != base_host:
            raise InvalidInput(
                "externalConfig URL hosts must match the approved baseUrl host."
            )
    return value


def _validate_secret_ref(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 300
        or any(character.isspace() or ord(character) < 32 for character in value)
        or any(character in value for character in ("?", "#", "@"))
    ):
        raise InvalidInput("secretRef is invalid.")
    try:
        reference = SecretRef.parse(value)
    except ValueError as exc:
        raise InvalidInput("secretRef is invalid.") from exc
    if (
        not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", reference.provider)
        or not reference.locator
        or len(reference.locator) > 240
    ):
        raise InvalidInput("secretRef is invalid.")
    return value


def _source_projection_material(source: SourceDefinition) -> dict[str, Any]:
    return {
        "topic": source.topic_code,
        "name": source.display_name,
        "publisher": source.publisher,
        "authorityTier": source.authority_tier,
        "independenceGroupId": source.independence_group,
        "ownerName": source.owner_name,
        "editorialControlName": source.editorial_control_name,
        "baseUrl": source.base_url,
        "accessMethod": source.access_method,
        "adapterKey": source.adapter_key,
        "externalConfig": source.external_config,
        "secretRef": source.secret_ref,
        "allowedMimeTypes": source.allowed_mime_types,
        "defaultRightsStatus": source.default_rights_status,
        "termsUrl": source.terms_url,
        "robotsUrl": source.robots_url,
        "licenseUrl": source.license_url,
        "pollIntervalSeconds": source.poll_interval_seconds,
        "rateLimitPolicy": source.rate_limit_policy,
        "enabled": source.enabled,
    }


def _normalize_source_material(
    data: Mapping[str, Any],
    *,
    current: SourceDefinition | None = None,
) -> dict[str, Any]:
    if current is None:
        missing = [
            field for field in _SOURCE_CONTRACT_FIELDS if field not in data
        ]
        if missing:
            raise InvalidInput("The complete source definition is required.")
        material = {field: data[field] for field in _SOURCE_CONTRACT_FIELDS}
    else:
        if "topic" in data:
            raise InvalidInput("Source topic is immutable.")
        material = _source_projection_material(current)
        for field in _SOURCE_CONTRACT_FIELDS:
            if field != "topic" and field in data:
                material[field] = data[field]

    if material["topic"] not in TopicCode.values:
        raise InvalidInput("Unsupported source topic.")
    for field in (
        "name",
        "publisher",
        "independenceGroupId",
        "ownerName",
        "editorialControlName",
    ):
        value = material[field]
        if not isinstance(value, str) or not value.strip():
            raise InvalidInput(f"{field} must not be empty.")
        material[field] = value.strip()

    base_url = _validate_public_url(
        material["baseUrl"],
        field_name="baseUrl",
    )
    material["baseUrl"] = base_url
    for field in ("termsUrl", "robotsUrl", "licenseUrl"):
        material[field] = _validate_public_url(
            material[field],
            field_name=field,
            nullable=True,
        )

    adapter_key = material["adapterKey"]
    if (
        not isinstance(adapter_key, str)
        or not _ADAPTER_KEY_PATTERN.fullmatch(adapter_key)
        or adapter_key not in SUPPORTED_ADAPTER_KEYS
    ):
        raise InvalidInput("adapterKey is not registered.")
    external_config = _validate_external_config(
        material["externalConfig"],
        base_url=base_url,
    )
    if not isinstance(external_config, dict):
        raise InvalidInput("externalConfig must be an object.")
    if material["accessMethod"] not in _ADAPTER_ACCESS_METHODS[adapter_key]:
        raise InvalidInput(
            "accessMethod is incompatible with adapterKey."
        )
    unknown_config_keys = (
        set(external_config) - _ADAPTER_CONFIG_KEYS[adapter_key]
    )
    if unknown_config_keys:
        raise InvalidInput(
            "externalConfig contains fields unsupported by adapterKey."
        )
    entrypoints = external_config.get("entrypoints", [])
    if not isinstance(entrypoints, list) or any(
        not isinstance(item, str) for item in entrypoints
    ):
        raise InvalidInput("externalConfig.entrypoints must be a URL array.")
    normalized_entrypoints: list[str] = []
    base_host = (urlsplit(base_url).hostname or "").rstrip(".").lower()
    for index, entrypoint in enumerate(entrypoints):
        normalized_entrypoint = _validate_public_url(
            entrypoint,
            field_name=f"externalConfig.entrypoints[{index}]",
        )
        entrypoint_host = (
            urlsplit(normalized_entrypoint).hostname or ""
        ).rstrip(".").lower()
        if entrypoint_host != base_host:
            raise InvalidInput(
                "externalConfig URL hosts must match the approved baseUrl host."
            )
        normalized_entrypoints.append(normalized_entrypoint)
    if len(set(normalized_entrypoints)) != len(normalized_entrypoints):
        raise InvalidInput(
            "externalConfig.entrypoints must not contain duplicates."
        )
    if material["enabled"] is True and not normalized_entrypoints:
        raise InvalidInput(
            "An enabled source requires a probe entrypoint."
        )
    if "entrypoints" in external_config:
        external_config["entrypoints"] = normalized_entrypoints
    material["externalConfig"] = external_config
    material["secretRef"] = _validate_secret_ref(material["secretRef"])

    allowed_mime_types = material["allowedMimeTypes"]
    if (
        not isinstance(allowed_mime_types, list)
        or len(allowed_mime_types) > 100
        or any(
            not isinstance(value, str)
            or not value
            or len(value) > 160
            or "/" not in value
            for value in allowed_mime_types
        )
    ):
        raise InvalidInput("allowedMimeTypes is invalid.")
    material["allowedMimeTypes"] = sorted(set(allowed_mime_types))

    if material["authorityTier"] not in SourceDefinition.AuthorityTier.values:
        raise InvalidInput("authorityTier is invalid.")
    if material["accessMethod"] not in SourceDefinition.AccessMethod.values:
        raise InvalidInput("accessMethod is invalid.")
    if (
        material["defaultRightsStatus"]
        not in SourceDefinition.RightsStatus.values
    ):
        raise InvalidInput("defaultRightsStatus is invalid.")

    poll_interval = material["pollIntervalSeconds"]
    if (
        isinstance(poll_interval, bool)
        or not isinstance(poll_interval, int)
        or poll_interval < 60
        or poll_interval > 9_007_199_254_740_991
    ):
        raise InvalidInput("pollIntervalSeconds is invalid.")

    rate_policy = material["rateLimitPolicy"]
    if not isinstance(rate_policy, Mapping) or set(rate_policy) != {
        "maxConcurrency",
        "requestsPerMinute",
        "burst",
    }:
        raise InvalidInput("rateLimitPolicy is invalid.")
    limits = {
        "maxConcurrency": (1, 50),
        "requestsPerMinute": (1, 10000),
        "burst": (1, 1000),
    }
    normalized_rate_policy: dict[str, int] = {}
    for field, (minimum, maximum) in limits.items():
        value = rate_policy[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise InvalidInput("rateLimitPolicy is invalid.")
        normalized_rate_policy[field] = value
    material["rateLimitPolicy"] = normalized_rate_policy
    if not isinstance(material["enabled"], bool):
        raise InvalidInput("enabled must be boolean.")

    # Ensure the exact material is canonicalizable before any row is changed.
    _hash(source_snapshot_material(material))
    return material


def source_snapshot_material(
    source_material: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schemaVersion": SOURCE_SNAPSHOT_SCHEMA_V2,
        **{field: source_material[field] for field in _SOURCE_CONTRACT_FIELDS},
    }


def source_snapshot_hash(source_material: Mapping[str, Any]) -> str:
    return _hash(source_snapshot_material(source_material))


def _is_verifiable_v2_source_snapshot(
    snapshot: SourceDefinitionSnapshot,
    *,
    expected_config_hash: str | None = None,
) -> bool:
    return (
        isinstance(snapshot.frozen_config, dict)
        and snapshot.frozen_config.get("schemaVersion")
        == SOURCE_SNAPSHOT_SCHEMA_V2
        and (
            expected_config_hash is None
            or snapshot.config_hash == expected_config_hash
        )
        and _hash(snapshot.frozen_config)
        == snapshot.frozen_config_hash
        and snapshot.frozen_config_hash == snapshot.config_hash
    )


def _apply_source_projection(
    source: SourceDefinition,
    material: Mapping[str, Any],
) -> None:
    source.topic_code = material["topic"]
    source.display_name = material["name"]
    source.publisher = material["publisher"]
    source.authority_tier = material["authorityTier"]
    source.independence_group = material["independenceGroupId"]
    source.owner_name = material["ownerName"]
    source.editorial_control_name = material["editorialControlName"]
    source.base_url = material["baseUrl"]
    source.access_method = material["accessMethod"]
    source.adapter_key = material["adapterKey"]
    source.external_config = material["externalConfig"]
    source.secret_ref = material["secretRef"]
    source.allowed_mime_types = material["allowedMimeTypes"]
    source.default_rights_status = material["defaultRightsStatus"]
    source.terms_url = material["termsUrl"]
    source.robots_url = material["robotsUrl"]
    source.license_url = material["licenseUrl"]
    source.poll_interval_seconds = material["pollIntervalSeconds"]
    source.rate_limit_policy = material["rateLimitPolicy"]
    source.enabled = material["enabled"]


def _source_audit_material(source: SourceDefinition) -> dict[str, Any]:
    projection_hash = _hash(source_snapshot_material(
        _source_projection_material(source)
    ))
    return {
        "schema_version": SOURCE_AUDIT_SCHEMA_V2,
        "source_id": str(source.id),
        "topic_code": source.topic_code,
        "projection_hash": projection_hash,
        "latest_approved_snapshot_version": (
            source.latest_approved_snapshot_version
        ),
        "latest_draft_snapshot_id": _id(source.latest_draft_snapshot_id),
        "latest_draft_snapshot_version": (
            source.latest_draft_snapshot_version
        ),
        "latest_draft_config_hash": source.latest_draft_config_hash,
    }


def _next_source_snapshot_version(
    source: SourceDefinition,
    *,
    using: str,
) -> int:
    latest = (
        SourceDefinitionSnapshot.objects.using(using)
        .filter(source=source)
        .aggregate(value=Max("version"))["value"]
    )
    return int(latest or 0) + 1


def _create_draft_snapshot(
    *,
    source: SourceDefinition,
    material: Mapping[str, Any],
    version: int,
    request_key: str | None,
    request_hash: str | None,
    using: str,
) -> SourceDefinitionSnapshot:
    frozen_config = source_snapshot_material(material)
    frozen_config_hash = source_snapshot_hash(material)
    snapshot = SourceDefinitionSnapshot(
        source=source,
        topic_code=source.topic_code,
        version=version,
        state=SourceDefinitionSnapshot.State.DRAFT,
        config=frozen_config,
        config_hash=frozen_config_hash,
        frozen_config=frozen_config,
        frozen_config_hash=frozen_config_hash,
        independence_group=material["independenceGroupId"],
        owner_name=material["ownerName"],
        editorial_control_name=material["editorialControlName"],
        request_key=request_key,
        request_hash=request_hash,
    )
    snapshot.full_clean()
    snapshot.save(using=using)
    return snapshot


def _retire_current_draft(
    source: SourceDefinition,
    *,
    using: str,
) -> None:
    if source.latest_draft_snapshot_id is None:
        return
    snapshot = (
        SourceDefinitionSnapshot.objects.using(using)
        .select_for_update()
        .get(pk=source.latest_draft_snapshot_id)
    )
    if snapshot.state == SourceDefinitionSnapshot.State.DRAFT:
        snapshot.state = SourceDefinitionSnapshot.State.RETIRED
        snapshot.retired_at = timezone.now()
        snapshot.save(
            update_fields=["state", "retired_at"],
            using=using,
        )


def _source_creation_replay(
    *,
    request_key: str,
    request_hash: str,
    admin: Any,
    audit_context: AuditContext,
) -> SourceDefinition | None:
    existing = SourceDefinition.objects.using(
        audit_context.database_alias
    ).filter(
        creation_request_key=request_key
    ).first()
    if existing is None:
        return None
    require_idempotent_match(
        stored_hash=existing.creation_request_hash or "",
        expected_hash=request_hash,
    )
    _require_admin_audit_replay(
        context=audit_context,
        action="source_definition.created",
        entity=existing,
        identity_key=request_key,
        request_hash=request_hash,
        admin=admin,
    )
    return existing


def create_source_definition(
    data: Mapping[str, Any],
    *,
    admin: Any,
    audit_context: AuditContext,
    request_hash: str,
) -> tuple[SourceDefinition, bool]:
    request_key = str(data["requestKey"])
    reason = "source definition create"
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    request_hash = _require_request_hash(request_hash)
    alias = audit_context.database_alias

    existing = _source_creation_replay(
        request_key=request_key,
        request_hash=request_hash,
        admin=admin,
        audit_context=audit_context,
    )
    if existing is not None:
        return existing, False

    material = _normalize_source_material(data)
    with transaction.atomic(using=alias):
        _lock_topic_head(material["topic"], using=alias)
        existing = _source_creation_replay(
            request_key=request_key,
            request_hash=request_hash,
            admin=admin,
            audit_context=audit_context,
        )
        if existing is not None:
            return existing, False

        source_id = uuid.uuid4()
        generated_key = (
            slugify(material["name"])[:70].strip("-") or "source"
        )
        source = SourceDefinition(
            id=source_id,
            key=f"{generated_key}-{str(source_id)[:8]}",
            creation_request_key=request_key,
            creation_request_hash=request_hash,
        )
        _apply_source_projection(source, material)
        try:
            with transaction.atomic(using=alias):
                source.save(using=alias)
        except IntegrityError:
            existing = _source_creation_replay(
                request_key=request_key,
                request_hash=request_hash,
                admin=admin,
                audit_context=audit_context,
            )
            if existing is None:
                raise
            return existing, False
        snapshot = _create_draft_snapshot(
            source=source,
            material=material,
            version=1,
            request_key=request_key,
            request_hash=request_hash,
            using=alias,
        )
        source.current_snapshot_version = snapshot.version
        source.latest_draft_snapshot = snapshot
        source.latest_draft_snapshot_version = snapshot.version
        source.latest_draft_config_hash = snapshot.config_hash
        source.save(
            update_fields=[
                "current_snapshot_version",
                "latest_draft_snapshot",
                "latest_draft_snapshot_version",
                "latest_draft_config_hash",
                "updated_at",
            ],
            using=alias,
        )
        record_audit_event(
            context=audit_context,
            action="source_definition.created",
            entity=source,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=SOURCE_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            after_material=_source_audit_material(source),
            metadata={
                "request_hash": request_hash,
                "result": "created",
                "source_id": str(source.id),
                "snapshot_id": str(snapshot.id),
                "snapshot_version": snapshot.version,
                "config_hash": snapshot.config_hash,
            },
        )
        return source, True


def update_source_definition(
    source_id: Any,
    data: Mapping[str, Any],
    *,
    admin: Any,
    audit_context: AuditContext,
    request_hash: str,
) -> SourceDefinition:
    request_key = str(data["requestKey"])
    reason = "source definition update"
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    request_hash = _require_request_hash(request_hash)
    alias = audit_context.database_alias

    with transaction.atomic(using=alias):
        source = (
            SourceDefinition.objects.using(alias)
            .select_for_update()
            .get(pk=source_id)
        )
        existing = (
            SourceDefinitionSnapshot.objects.using(alias)
            .filter(source=source, request_key=request_key)
            .first()
        )
        if existing is not None:
            require_idempotent_match(
                stored_hash=existing.request_hash or "",
                expected_hash=request_hash,
            )
            _require_admin_audit_replay(
                context=audit_context,
                action="source_definition.updated",
                entity=source,
                identity_key=request_key,
                request_hash=request_hash,
                admin=admin,
            )
            return source

        expected = _id(data.get("expectedLatestDraftSnapshotId"))
        actual = _id(source.latest_draft_snapshot_id)
        if expected != actual:
            raise StaleVersion(
                "The latest source draft snapshot changed."
            )
        material = _normalize_source_material(data, current=source)
        before_material = _source_audit_material(source)
        _retire_current_draft(source, using=alias)
        version = _next_source_snapshot_version(source, using=alias)
        snapshot = _create_draft_snapshot(
            source=source,
            material=material,
            version=version,
            request_key=request_key,
            request_hash=request_hash,
            using=alias,
        )
        _apply_source_projection(source, material)
        source.current_snapshot_version = version
        source.latest_draft_snapshot = snapshot
        source.latest_draft_snapshot_version = version
        source.latest_draft_config_hash = snapshot.config_hash
        source.save(using=alias)
        record_audit_event(
            context=audit_context,
            action="source_definition.updated",
            entity=source,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=SOURCE_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            before_material=before_material,
            after_material=_source_audit_material(source),
            metadata={
                "request_hash": request_hash,
                "result": "updated",
                "source_id": str(source.id),
                "snapshot_id": str(snapshot.id),
                "snapshot_version": snapshot.version,
                "config_hash": snapshot.config_hash,
            },
        )
        return source


def _membership_material(
    memberships: Iterable[
        SourceRegistryMembership | Mapping[str, Any]
    ],
) -> list[dict[str, Any]]:
    material: list[dict[str, Any]] = []
    for membership in memberships:
        if isinstance(membership, Mapping):
            source_id = membership["source_definition_id"]
            snapshot_id = membership["source_snapshot_id"]
            config_hash = membership["config_hash"]
            enabled = membership["enabled"]
            display_order = membership["display_order"]
        else:
            source_id = membership.source_definition_id
            snapshot_id = membership.source_snapshot_id
            config_hash = membership.source_snapshot.config_hash
            enabled = membership.enabled
            display_order = membership.display_order
        material.append(
            {
                "sourceDefinitionId": str(source_id),
                "sourceDefinitionSnapshotId": str(snapshot_id),
                "sourceDefinitionConfigHash": str(config_hash),
                "enabled": bool(enabled),
                "displayOrder": int(display_order),
            }
        )
    return sorted(
        material,
        key=lambda item: item["sourceDefinitionId"],
    )


def registry_manifest_hash_for_memberships(
    memberships: Iterable[
        SourceRegistryMembership | Mapping[str, Any]
    ],
) -> str:
    return _hash(
        {
            "schemaVersion": SOURCE_REGISTRY_MANIFEST_SCHEMA_V2,
            "memberships": _membership_material(memberships),
        }
    )


def registry_manifest_hash(
    registry: SourceRegistrySnapshot,
    *,
    using: str | None = None,
) -> str:
    alias = using or registry._state.db or "default"
    memberships = list(
        SourceRegistryMembership.objects.using(alias)
        .select_related("source_snapshot")
        .filter(registry=registry)
        .order_by("source_definition_id")
    )
    return registry_manifest_hash_for_memberships(memberships)


def _registry_audit_material(
    registry: SourceRegistrySnapshot,
) -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_AUDIT_SCHEMA_V2,
        "registry_id": str(registry.id),
        "topic_code": registry.topic_code,
        "version": registry.version,
        "state": registry.state,
        "row_version": registry.row_version,
        "manifest_hash": registry.manifest_hash,
        "base_approved_registry_id": _id(
            registry.base_approved_registry_id
        ),
        "base_approved_version": registry.base_approved_version,
        "base_approved_manifest_hash": (
            registry.base_approved_manifest_hash
        ),
        "latest_decision_id": _id(registry.latest_decision_id),
    }


def _lock_topic_head(
    topic_code: str,
    *,
    using: str,
) -> TopicRegistryHead:
    TopicRegistryHead.objects.using(using).get_or_create(
        topic_code=topic_code
    )
    return (
        TopicRegistryHead.objects.using(using)
        .select_for_update()
        .get(topic_code=topic_code)
    )


def _head_tuple(
    head: TopicRegistryHead,
) -> tuple[str | None, int | None, str | None]:
    return (
        _id(head.current_approved_registry_id),
        head.current_approved_version,
        head.current_approved_manifest_hash,
    )


def _request_head_tuple(
    data: Mapping[str, Any],
) -> tuple[str | None, int | None, str | None]:
    return (
        _id(data.get("expectedCurrentHeadRegistryId")),
        data.get("expectedCurrentHeadVersion"),
        data.get("expectedCurrentHeadManifestHash"),
    )


def _next_registry_version(topic_code: str, *, using: str) -> int:
    latest = (
        SourceRegistrySnapshot.objects.using(using)
        .filter(topic_code=topic_code)
        .aggregate(value=Max("version"))["value"]
    )
    return int(latest or 0) + 1


def create_registry_draft(
    data: Mapping[str, Any],
    *,
    admin: Any,
    audit_context: AuditContext,
    request_hash: str,
) -> tuple[SourceRegistrySnapshot, bool]:
    request_key = str(data["requestKey"])
    reason = "source registry draft create"
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    request_hash = _require_request_hash(request_hash)
    topic_code = str(data["topic"])
    if topic_code not in TopicCode.values:
        raise InvalidInput("Unsupported registry topic.")
    alias = audit_context.database_alias

    existing = SourceRegistrySnapshot.objects.using(alias).filter(
        topic_code=topic_code,
        draft_request_key=request_key,
    ).first()
    if existing is not None:
        require_idempotent_match(
            stored_hash=existing.draft_request_hash or "",
            expected_hash=request_hash,
        )
        _require_admin_audit_replay(
            context=audit_context,
            action="source_registry.draft_created",
            entity=existing,
            identity_key=request_key,
            request_hash=request_hash,
            admin=admin,
        )
        return existing, False

    with transaction.atomic(using=alias):
        head = _lock_topic_head(topic_code, using=alias)
        existing = SourceRegistrySnapshot.objects.using(alias).filter(
            topic_code=topic_code,
            draft_request_key=request_key,
        ).first()
        if existing is not None:
            require_idempotent_match(
                stored_hash=existing.draft_request_hash or "",
                expected_hash=request_hash,
            )
            _require_admin_audit_replay(
                context=audit_context,
                action="source_registry.draft_created",
                entity=existing,
                identity_key=request_key,
                request_hash=request_hash,
                admin=admin,
            )
            return existing, False

        expected_latest = (
            _id(data.get("baseRegistryId")),
            data.get("expectedLatestRegistryVersion"),
            data.get("expectedLatestRegistryManifestHash"),
        )
        if expected_latest != _head_tuple(head):
            raise StaleVersion(
                "The approved source registry head changed."
            )

        base = None
        carried: list[SourceRegistryMembership] = []
        if head.current_approved_registry_id is not None:
            base = (
                SourceRegistrySnapshot.objects.using(alias)
                .select_for_update()
                .get(pk=head.current_approved_registry_id)
            )
            if (
                base.state != SourceRegistrySnapshot.State.APPROVED
                or base.version != head.current_approved_version
                or base.manifest_hash
                != head.current_approved_manifest_hash
            ):
                raise Conflict("The source registry head is inconsistent.")
            carried = list(
                SourceRegistryMembership.objects.using(alias)
                .select_related("source_snapshot")
                .filter(registry=base)
                .order_by("source_definition_id")
            )

        manifest_hash = registry_manifest_hash_for_memberships(carried)
        registry = SourceRegistrySnapshot.objects.using(alias).create(
            topic_code=topic_code,
            version=_next_registry_version(topic_code, using=alias),
            state=SourceRegistrySnapshot.State.DRAFT,
            manifest_hash=manifest_hash,
            row_version=1,
            base_approved_registry=base,
            base_approved_version=(base.version if base else None),
            base_approved_manifest_hash=(
                base.manifest_hash if base else None
            ),
            draft_request_key=request_key,
            draft_request_hash=request_hash,
        )
        if carried:
            SourceRegistryMembership.objects.using(alias).bulk_create(
                [
                    SourceRegistryMembership(
                        registry=registry,
                        source_definition_id=member.source_definition_id,
                        source_snapshot_id=member.source_snapshot_id,
                        enabled=member.enabled,
                        display_order=member.display_order,
                    )
                    for member in carried
                ]
            )
        record_audit_event(
            context=audit_context,
            action="source_registry.draft_created",
            entity=registry,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=REGISTRY_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            after_material=_registry_audit_material(registry),
            metadata={
                "request_hash": request_hash,
                "result": "created",
                "registry_id": str(registry.id),
                "manifest_hash": registry.manifest_hash,
                "version": registry.version,
                "membership_count": len(carried),
            },
        )
        return registry, True


def update_registry_membership(
    registry_id: Any,
    source_id: Any,
    data: Mapping[str, Any],
    *,
    admin: Any,
    audit_context: AuditContext,
    request_hash: str,
) -> tuple[SourceRegistrySnapshot, bool]:
    request_key = str(data["requestKey"])
    reason = "source registry membership update"
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    request_hash = _require_request_hash(request_hash)
    alias = audit_context.database_alias

    existing = SourceRegistryMutation.objects.using(alias).filter(
        registry_id=registry_id,
        request_key=request_key,
    ).first()
    if existing is not None:
        require_idempotent_match(
            stored_hash=existing.request_hash,
            expected_hash=request_hash,
        )
        registry = SourceRegistrySnapshot.objects.using(alias).get(
            pk=registry_id
        )
        _require_admin_audit_replay(
            context=audit_context,
            action="source_registry.membership_updated",
            entity=registry,
            identity_key=request_key,
            request_hash=request_hash,
            admin=admin,
        )
        return registry, False

    with transaction.atomic(using=alias):
        registry = (
            SourceRegistrySnapshot.objects.using(alias)
            .select_for_update()
            .get(pk=registry_id)
        )
        existing = SourceRegistryMutation.objects.using(alias).filter(
            registry=registry,
            request_key=request_key,
        ).first()
        if existing is not None:
            require_idempotent_match(
                stored_hash=existing.request_hash,
                expected_hash=request_hash,
            )
            _require_admin_audit_replay(
                context=audit_context,
                action="source_registry.membership_updated",
                entity=registry,
                identity_key=request_key,
                request_hash=request_hash,
                admin=admin,
            )
            return registry, False
        if registry.state != SourceRegistrySnapshot.State.DRAFT:
            raise StateConflict(
                "Only a draft source registry can be changed."
            )
        if (
            registry.row_version != data["expectedRowVersion"]
            or registry.manifest_hash != data["expectedManifestHash"]
        ):
            raise StaleVersion(
                "The source registry draft changed."
            )

        source = (
            SourceDefinition.objects.using(alias)
            .select_for_update()
            .get(pk=source_id)
        )
        snapshot = (
            SourceDefinitionSnapshot.objects.using(alias)
            .select_for_update()
            .get(pk=data["sourceDefinitionSnapshotId"])
        )
        if (
            source.topic_code != registry.topic_code
            or snapshot.source_id != source.id
            or snapshot.topic_code != registry.topic_code
        ):
            raise InvalidInput(
                "The membership source, snapshot and registry must share one topic."
            )
        if snapshot.state == SourceDefinitionSnapshot.State.RETIRED:
            raise StateConflict(
                "A retired source snapshot cannot be selected."
            )
        if (
            snapshot.state == SourceDefinitionSnapshot.State.DRAFT
            and source.latest_draft_snapshot_id != snapshot.id
        ):
            raise StaleVersion(
                "The selected source draft is no longer current."
            )
        if data["enabled"] and not bool(
            snapshot.frozen_config.get("enabled", True)
        ):
            raise InvalidInput(
                "A disabled source snapshot cannot be enabled in a registry."
            )

        before_material = _registry_audit_material(registry)
        before_hash = registry.manifest_hash
        member = (
            SourceRegistryMembership.objects.using(alias)
            .select_for_update()
            .filter(
                registry=registry,
                source_definition=source,
            )
            .first()
        )
        if member is None:
            member = SourceRegistryMembership(
                registry=registry,
                source_definition=source,
            )
        member.source_snapshot = snapshot
        member.enabled = data["enabled"]
        member.display_order = data["displayOrder"]
        member.full_clean()
        member.save(using=alias)

        after_hash = registry_manifest_hash(registry, using=alias)
        registry.manifest_hash = after_hash
        registry.row_version += 1
        registry.save(
            update_fields=["manifest_hash", "row_version"],
            using=alias,
        )
        mutation = SourceRegistryMutation(
            registry=registry,
            source_definition=source,
            request_key=request_key,
            request_hash=request_hash,
            before_manifest_hash=before_hash,
            after_manifest_hash=after_hash,
            resulting_row_version=registry.row_version,
        )
        mutation.full_clean()
        mutation.save(using=alias)
        record_audit_event(
            context=audit_context,
            action="source_registry.membership_updated",
            entity=registry,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=REGISTRY_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            before_material=before_material,
            after_material=_registry_audit_material(registry),
            metadata={
                "request_hash": request_hash,
                "result": "updated",
                "registry_id": str(registry.id),
                "source_id": str(source.id),
                "source_snapshot_id": str(snapshot.id),
                "before_manifest_hash": before_hash,
                "after_manifest_hash": after_hash,
                "row_version": registry.row_version,
                "mutation_id": str(mutation.id),
            },
        )
        return registry, True


def _verify_registry_manifest(
    registry: SourceRegistrySnapshot,
    *,
    using: str,
) -> list[SourceRegistryMembership]:
    memberships = list(
        SourceRegistryMembership.objects.using(using)
        .select_related("source_definition", "source_snapshot")
        .filter(registry=registry)
        .order_by("source_definition_id")
    )
    if registry_manifest_hash_for_memberships(
        memberships
    ) != registry.manifest_hash:
        raise Conflict("The source registry manifest is inconsistent.")
    for member in memberships:
        if (
            member.source_definition_id != member.source_snapshot.source_id
            or member.source_definition.topic_code != registry.topic_code
            or member.source_snapshot.topic_code != registry.topic_code
        ):
            raise Conflict(
                "The source registry membership identity is inconsistent."
            )
    return memberships


def _lock_and_validate_approval_sources(
    memberships: Iterable[SourceRegistryMembership],
    *,
    topic_code: str,
    using: str,
) -> tuple[
    dict[Any, SourceDefinition],
    dict[Any, SourceDefinitionSnapshot],
]:
    enabled_memberships = [
        membership for membership in memberships if membership.enabled
    ]
    source_ids = sorted(
        {
            membership.source_definition_id
            for membership in enabled_memberships
        },
        key=str,
    )
    snapshot_ids = sorted(
        {
            membership.source_snapshot_id
            for membership in enabled_memberships
        },
        key=str,
    )
    locked_sources = {
        source.id: source
        for source in SourceDefinition.objects.using(using)
        .select_for_update()
        .filter(id__in=source_ids)
        .order_by("id")
    }
    locked_snapshots = {
        snapshot.id: snapshot
        for snapshot in SourceDefinitionSnapshot.objects.using(using)
        .select_for_update()
        .filter(id__in=snapshot_ids)
        .order_by("id")
    }
    if (
        len(locked_sources) != len(source_ids)
        or len(locked_snapshots) != len(snapshot_ids)
    ):
        raise Conflict(
            "The source registry membership target no longer exists."
        )

    for member in enabled_memberships:
        source = locked_sources[member.source_definition_id]
        snapshot = locked_snapshots[member.source_snapshot_id]
        if (
            source.topic_code != topic_code
            or snapshot.topic_code != topic_code
            or snapshot.source_id != source.id
        ):
            raise Conflict(
                "The locked source registry membership identity is inconsistent."
            )
        if snapshot.state == SourceDefinitionSnapshot.State.RETIRED:
            raise StateConflict(
                "A retired source snapshot cannot be approved."
            )
        if (
            snapshot.state == SourceDefinitionSnapshot.State.DRAFT
            and source.latest_draft_snapshot_id != snapshot.id
        ):
            raise StaleVersion(
                "A selected source draft is no longer current."
            )
        if snapshot.state not in {
            SourceDefinitionSnapshot.State.DRAFT,
            SourceDefinitionSnapshot.State.APPROVED,
        }:
            raise StateConflict(
                "The selected source snapshot state is invalid."
            )
        if not bool(snapshot.frozen_config.get("enabled", True)):
            raise InvalidInput(
                "An enabled membership selected a disabled source snapshot."
            )
        if not _is_verifiable_v2_source_snapshot(snapshot):
            raise Conflict(
                "A selected source snapshot is not a verifiable v2 snapshot."
            )
    return locked_sources, locked_snapshots


def decide_source_registry(
    registry_id: Any,
    data: Mapping[str, Any],
    *,
    admin: Any,
    request: Any,
    audit_context: AuditContext,
    request_hash: str,
) -> tuple[SourceRegistryDecision, bool]:
    request_key = str(data["requestKey"])
    reason = str(data["reason"])
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    request_hash = _require_request_hash(request_hash)
    alias = audit_context.database_alias

    existing = SourceRegistryDecision.objects.using(alias).filter(
        registry_id=registry_id,
        request_key=request_key,
    ).first()
    if existing is not None:
        if existing.decided_by_id != admin.pk:
            raise RequestKeyConflict(
                "The request key belongs to another administrator."
            )
        require_idempotent_match(
            stored_hash=existing.request_hash,
            expected_hash=request_hash,
        )
        require_audit_replay(
            context=audit_context,
            action=f"source_registry.{existing.decision}",
            entity=existing.registry,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            request_hash=request_hash,
        )
        return existing, False

    registry_topic = (
        SourceRegistrySnapshot.objects.using(alias)
        .only("topic_code")
        .get(pk=registry_id)
        .topic_code
    )
    with transaction.atomic(using=alias):
        # Topic head is always locked before a registry row. This serializes
        # every approval/retirement for one topic and avoids cross-draft races.
        head = _lock_topic_head(registry_topic, using=alias)
        registry = (
            SourceRegistrySnapshot.objects.using(alias)
            .select_for_update()
            .get(pk=registry_id)
        )
        existing = SourceRegistryDecision.objects.using(alias).filter(
            registry=registry,
            request_key=request_key,
        ).first()
        if existing is not None:
            if existing.decided_by_id != admin.pk:
                raise RequestKeyConflict(
                    "The request key belongs to another administrator."
                )
            require_idempotent_match(
                stored_hash=existing.request_hash,
                expected_hash=request_hash,
            )
            require_audit_replay(
                context=audit_context,
                action=f"source_registry.{existing.decision}",
                entity=registry,
                identity_key=source_registry_audit_request_key(
                    request_key
                ),
                request_hash=request_hash,
            )
            return existing, False

        if (
            registry.row_version != data["expectedRowVersion"]
            or registry.manifest_hash != data["expectedManifestHash"]
        ):
            raise StaleVersion("The source registry changed.")
        if _head_tuple(head) != _request_head_tuple(data):
            raise StaleVersion(
                "The approved source registry head changed."
            )
        if _id(registry.latest_decision_id) != _id(
            data.get("expectedLatestDecisionId")
        ):
            raise StaleVersion(
                "The source registry decision projection changed."
            )
        latest_decision = (
            SourceRegistryDecision.objects.using(alias).get(
                pk=registry.latest_decision_id
            )
            if registry.latest_decision_id
            else None
        )

        decision_value = data["decision"]
        if decision_value not in SourceRegistryDecision.Decision.values:
            raise InvalidInput("Unsupported source registry decision.")
        memberships = _verify_registry_manifest(registry, using=alias)
        before_material = _registry_audit_material(registry)
        prior_head: SourceRegistrySnapshot | None = None
        locked_sources: dict[Any, SourceDefinition] = {}
        locked_snapshots: dict[Any, SourceDefinitionSnapshot] = {}

        if decision_value == SourceRegistryDecision.Decision.APPROVED:
            if registry.state != SourceRegistrySnapshot.State.DRAFT:
                raise StateConflict(
                    "Only a draft source registry can be approved."
                )
            base_tuple = (
                _id(registry.base_approved_registry_id),
                registry.base_approved_version,
                registry.base_approved_manifest_hash,
            )
            if base_tuple != _head_tuple(head):
                raise StaleVersion(
                    "The registry draft is based on a stale approved head."
                )
            enabled_memberships = [
                member for member in memberships if member.enabled
            ]
            if not enabled_memberships:
                raise InvalidInput(
                    "An approved registry requires an enabled source."
                )
            if head.current_approved_registry_id is not None:
                prior_head = (
                    SourceRegistrySnapshot.objects.using(alias)
                    .select_for_update()
                    .get(pk=head.current_approved_registry_id)
                )
                if (
                    prior_head.state
                    != SourceRegistrySnapshot.State.APPROVED
                ):
                    raise Conflict(
                        "The current source registry head is inconsistent."
                    )
            else:
                # Legacy approved rows are intentionally not promoted into a
                # trusted head by migration. An explicit re-authenticated v2
                # approval may retire the single legacy row while establishing
                # the first authoritative head.
                prior_head = (
                    SourceRegistrySnapshot.objects.using(alias)
                    .select_for_update()
                    .filter(
                        topic_code=registry.topic_code,
                        state=SourceRegistrySnapshot.State.APPROVED,
                    )
                    .exclude(pk=registry.pk)
                    .first()
                )
            (
                locked_sources,
                locked_snapshots,
            ) = _lock_and_validate_approval_sources(
                enabled_memberships,
                topic_code=registry.topic_code,
                using=alias,
            )
        else:
            if (
                registry.state != SourceRegistrySnapshot.State.APPROVED
                or head.current_approved_registry_id != registry.id
            ):
                raise StateConflict(
                    "Only the current approved registry can be retired."
                )

        consume_reauthentication_proof(
            request=request,
            proof_id=data["reauthProofId"],
            action_scope="registry_decision",
            entity_type="source_registry_snapshot",
            entity_id=registry.id,
        )
        now = timezone.now()
        version = (
            latest_decision.version + 1
            if latest_decision is not None
            else 1
        )
        decision_id = uuid.uuid4()
        decision_hash = _hash(
            {
                "schemaVersion": "source-registry-decision-v2",
                "id": str(decision_id),
                "registryId": str(registry.id),
                "version": version,
                "decision": decision_value,
                "expectedRowVersion": data["expectedRowVersion"],
                "expectedManifestHash": data["expectedManifestHash"],
                "expectedCurrentHeadRegistryId": _id(
                    data.get("expectedCurrentHeadRegistryId")
                ),
                "expectedCurrentHeadVersion": data.get(
                    "expectedCurrentHeadVersion"
                ),
                "expectedCurrentHeadManifestHash": data.get(
                    "expectedCurrentHeadManifestHash"
                ),
                "supersedesDecisionId": _id(
                    registry.latest_decision_id
                ),
                "requestKey": request_key,
                "requestHash": request_hash,
                "decidedBy": str(admin.pk),
                "decidedAt": now.isoformat(),
                "reason": reason,
            }
        )
        decision = SourceRegistryDecision(
            id=decision_id,
            registry=registry,
            version=version,
            decision=decision_value,
            expected_row_version=data["expectedRowVersion"],
            expected_manifest_hash=data["expectedManifestHash"],
            expected_current_head_registry_id=(
                data.get("expectedCurrentHeadRegistryId")
            ),
            expected_current_head_version=data.get(
                "expectedCurrentHeadVersion"
            ),
            expected_current_head_manifest_hash=data.get(
                "expectedCurrentHeadManifestHash"
            ),
            supersedes_decision=latest_decision,
            request_key=request_key,
            request_hash=request_hash,
            decision_hash=decision_hash,
            decided_by=admin,
            decided_at=now,
            reason=reason,
        )
        decision.full_clean()
        decision.save(using=alias)

        if decision_value == SourceRegistryDecision.Decision.APPROVED:
            for member in memberships:
                if not member.enabled:
                    continue
                source = locked_sources[member.source_definition_id]
                snapshot = locked_snapshots[member.source_snapshot_id]
                if snapshot.state == SourceDefinitionSnapshot.State.DRAFT:
                    previous_approved = list(
                        SourceDefinitionSnapshot.objects.using(alias)
                        .select_for_update()
                        .filter(
                            source=source,
                            state=SourceDefinitionSnapshot.State.APPROVED,
                        )
                        .exclude(pk=snapshot.pk)
                    )
                    for previous in previous_approved:
                        previous.state = (
                            SourceDefinitionSnapshot.State.RETIRED
                        )
                        previous.retired_at = now
                        previous.save(
                            update_fields=["state", "retired_at"],
                            using=alias,
                        )
                    snapshot.state = SourceDefinitionSnapshot.State.APPROVED
                    snapshot.approved_by = admin
                    snapshot.approved_at = now
                    snapshot.retired_at = None
                    snapshot.save(
                        update_fields=[
                            "state",
                            "approved_by",
                            "approved_at",
                            "retired_at",
                        ],
                        using=alias,
                    )
                source.latest_approved_snapshot_version = snapshot.version
                if source.latest_draft_snapshot_id == snapshot.id:
                    source.latest_draft_snapshot = None
                    source.latest_draft_snapshot_version = None
                    source.latest_draft_config_hash = None
                source.save(
                    update_fields=[
                        "latest_approved_snapshot_version",
                        "latest_draft_snapshot",
                        "latest_draft_snapshot_version",
                        "latest_draft_config_hash",
                        "updated_at",
                    ],
                    using=alias,
                )

            if prior_head is not None:
                prior_before = _registry_audit_material(prior_head)
                prior_head.state = SourceRegistrySnapshot.State.RETIRED
                prior_head.retired_at = now
                prior_head.row_version += 1
                prior_head.save(
                    update_fields=[
                        "state",
                        "retired_at",
                        "row_version",
                    ],
                    using=alias,
                )
                record_audit_event(
                    context=audit_context,
                    action="source_registry.superseded",
                    entity=prior_head,
                    identity_key=_hash(
                        {
                            "requestKey": request_key,
                            "priorRegistryId": str(prior_head.id),
                            "newRegistryId": str(registry.id),
                        }
                    ),
                    material_schema_version=REGISTRY_AUDIT_SCHEMA_V2,
                    metadata_schema_version="2",
                    before_material=prior_before,
                    after_material=_registry_audit_material(prior_head),
                    metadata={
                        "request_hash": request_hash,
                        "result": "superseded",
                        "registry_id": str(prior_head.id),
                        "replacement_registry_id": str(registry.id),
                    },
                )

            registry.state = SourceRegistrySnapshot.State.APPROVED
            registry.approved_by = admin
            registry.approved_at = now
            registry.retired_at = None
            head.current_approved_registry = registry
            head.current_approved_version = registry.version
            head.current_approved_manifest_hash = registry.manifest_hash
        else:
            registry.state = SourceRegistrySnapshot.State.RETIRED
            registry.retired_at = now
            head.current_approved_registry = None
            head.current_approved_version = None
            head.current_approved_manifest_hash = None

        registry.latest_decision = decision
        registry.row_version += 1
        registry.save(
            update_fields=[
                "state",
                "row_version",
                "latest_decision",
                "approved_by",
                "approved_at",
                "retired_at",
            ],
            using=alias,
        )
        head.row_version += 1
        head.save(using=alias)
        record_audit_event(
            context=audit_context,
            action=f"source_registry.{decision_value}",
            entity=registry,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=REGISTRY_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            before_material=before_material,
            after_material=_registry_audit_material(registry),
            metadata={
                "request_hash": request_hash,
                "result": "decided",
                "decision": decision_value,
                "decision_hash": decision.decision_hash,
                "registry_id": str(registry.id),
                "manifest_hash": registry.manifest_hash,
                "version": decision.version,
                "reauth_proof_id": str(data["reauthProofId"]),
                "prior_head_registry_id": _id(
                    prior_head.id if prior_head else None
                ),
            },
        )
        return decision, True


def normalize_registry_import(data: Mapping[str, Any]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    for raw in data["sources"]:
        if not isinstance(raw, Mapping):
            raise InvalidInput(
                "Repository sources must be objects."
            )
        source_key = raw.get("key")
        if (
            not isinstance(source_key, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", source_key)
            is None
        ):
            raise InvalidInput(
                "Repository source keys must be lowercase slugs."
            )
        material = _normalize_source_material(
            {
                "topic": data["topicCode"],
                "name": raw["displayName"],
                "publisher": raw.get("publisher", raw["ownerName"]),
                "authorityTier": raw["authorityTier"],
                "independenceGroupId": raw["independenceGroup"],
                "ownerName": raw["ownerName"],
                "editorialControlName": raw.get(
                    "editorialControlName",
                    raw["ownerName"],
                ),
                "baseUrl": raw["baseUrl"],
                "accessMethod": raw["accessMethod"],
                "adapterKey": raw.get("adapter", "public_html"),
                "externalConfig": {
                    "entrypoints": raw.get("entrypoints", []),
                },
                "secretRef": raw.get("secretRef"),
                "allowedMimeTypes": raw.get(
                    "allowedContentTypes",
                    [],
                ),
                "defaultRightsStatus": raw.get(
                    "rightsStatus",
                    "internal_analysis_only",
                ),
                "termsUrl": raw.get("termsUrl"),
                "robotsUrl": raw.get("robotsUrl"),
                "licenseUrl": raw.get("licenseUrl"),
                "pollIntervalSeconds": int(
                    raw.get("pollMinutes", 60)
                )
                * 60,
                "rateLimitPolicy": {
                    "maxConcurrency": int(
                        raw.get("maxConcurrency", 1)
                    ),
                    "requestsPerMinute": int(
                        raw.get("rateLimitPerMinute", 10)
                    ),
                    "burst": int(raw.get("burst", 1)),
                },
                "enabled": raw.get("enabled", True),
            }
        )
        sources.append({"key": source_key, "material": material})
    source_keys = [item["key"] for item in sources]
    if len(set(source_keys)) != len(source_keys):
        raise InvalidInput(
            "Repository source keys must be unique within one topic."
        )
    return {
        "topic_code": data["topicCode"],
        "title": data["title"],
        "policy_version": int(data.get("policyVersion", 1)),
        "freshness_minutes": int(data.get("freshnessMinutes", 1440)),
        "policy": data.get("policy", {}),
        "sources": sources,
    }


def _approved_registry_matches_import(
    registry: SourceRegistrySnapshot,
    normalized: Mapping[str, Any],
    *,
    using: str,
) -> bool:
    if (
        registry.state != SourceRegistrySnapshot.State.APPROVED
        or registry.topic_code != normalized["topic_code"]
    ):
        return False
    memberships = list(
        SourceRegistryMembership.objects.using(using)
        .select_related("source_definition", "source_snapshot")
        .filter(registry=registry)
        .order_by("source_definition_id")
    )
    if (
        registry_manifest_hash_for_memberships(memberships)
        != registry.manifest_hash
    ):
        return False
    expected = {
        item["key"]: {
            "config_hash": source_snapshot_hash(item["material"]),
            "enabled": bool(item["material"]["enabled"]),
            "display_order": display_order,
        }
        for display_order, item in enumerate(normalized["sources"])
    }
    if len(expected) != len(normalized["sources"]):
        raise InvalidInput(
            "Repository source keys must be unique within one topic."
        )
    if {
        membership.source_definition.key
        for membership in memberships
    } != set(expected):
        return False
    for membership in memberships:
        source = membership.source_definition
        snapshot = membership.source_snapshot
        target = expected[source.key]
        if (
            source.topic_code != registry.topic_code
            or snapshot.source_id != source.id
            or snapshot.topic_code != registry.topic_code
            or (
                membership.enabled
                and snapshot.state
                != SourceDefinitionSnapshot.State.APPROVED
            )
            or membership.enabled != target["enabled"]
            or membership.display_order != target["display_order"]
            or not _is_verifiable_v2_source_snapshot(
                snapshot,
                expected_config_hash=target["config_hash"],
            )
        ):
            return False
    return True


def _topic_policy_matches_import(
    normalized: Mapping[str, Any],
    *,
    using: str,
) -> bool:
    policy = (
        TopicPolicy.objects.using(using)
        .select_for_update()
        .filter(
            code=normalized["topic_code"],
            version=normalized["policy_version"],
        )
        .first()
    )
    expected_hash = _hash(normalized["policy"])
    return policy is not None and all(
        (
            policy.active,
            policy.title == normalized["title"],
            policy.freshness_minutes
            == normalized["freshness_minutes"],
            policy.policy == normalized["policy"],
            policy.policy_hash == expected_hash,
        )
    )


def import_registry_manifest(
    path: str | Path,
    *,
    audit_context: AuditContext,
) -> RegistryImportResult:
    if audit_context.actor_type != AuditEvent.ActorType.SYSTEM:
        raise ValueError(
            "Source registry import requires explicit system provenance."
        )
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    normalized = normalize_registry_import(data)
    topic_code = normalized["topic_code"]
    if topic_code not in TopicCode.values:
        raise InvalidInput("Unsupported registry topic.")
    request_material = {
        "schemaVersion": "source-registry-import-v2",
        **normalized,
    }
    repository_hash = _hash(request_material)
    alias = audit_context.database_alias

    with transaction.atomic(using=alias):
        head = _lock_topic_head(topic_code, using=alias)
        if head.current_approved_registry_id is not None:
            current = (
                SourceRegistrySnapshot.objects.using(alias)
                .select_for_update()
                .get(pk=head.current_approved_registry_id)
            )
            if (
                current.version == head.current_approved_version
                and current.manifest_hash
                == head.current_approved_manifest_hash
                and _approved_registry_matches_import(
                    current,
                    normalized,
                    using=alias,
                )
                and _topic_policy_matches_import(
                    normalized,
                    using=alias,
                )
            ):
                return RegistryImportResult(
                    topic_code=topic_code,
                    registry_id=str(current.id),
                    source_count=len(normalized["sources"]),
                    created=False,
                )
        request_hash = _hash(
            {
                **request_material,
                "baseRegistryId": _id(
                    head.current_approved_registry_id
                ),
                "baseVersion": head.current_approved_version,
                "baseManifestHash": (
                    head.current_approved_manifest_hash
                ),
            }
        )
        legacy_request_key = f"seed:{request_hash}"
        request_key = "seed:" + _hash(
            {
                "schemaVersion": (
                    "source-registry-import-generation-v1"
                ),
                "requestHash": request_hash,
                "headRowVersion": head.row_version,
            }
        )
        existing = SourceRegistrySnapshot.objects.using(alias).filter(
            topic_code=topic_code,
            state=SourceRegistrySnapshot.State.DRAFT,
            draft_request_key__in=(
                request_key,
                legacy_request_key,
            ),
        ).first()
        if existing is not None:
            require_idempotent_match(
                stored_hash=existing.draft_request_hash or "",
                expected_hash=request_hash,
            )
            require_audit_replay(
                context=audit_context,
                action="source_registry.imported",
                entity=existing,
                identity_key=existing.draft_request_key,
                request_hash=request_hash,
            )
            return RegistryImportResult(
                topic_code=topic_code,
                registry_id=str(existing.id),
                source_count=existing.memberships.count(),
                created=False,
            )

        policy_hash = _hash(normalized["policy"])
        policy, policy_created = TopicPolicy.objects.using(alias).get_or_create(
            code=topic_code,
            version=normalized["policy_version"],
            defaults={
                "title": normalized["title"],
                "freshness_minutes": normalized["freshness_minutes"],
                "policy": normalized["policy"],
                "policy_hash": policy_hash,
                "active": True,
            },
        )
        if not policy_created and any(
            (
                not policy.active,
                policy.title != normalized["title"],
                policy.freshness_minutes
                != normalized["freshness_minutes"],
                policy.policy != normalized["policy"],
                policy.policy_hash != policy_hash,
            )
        ):
            raise Conflict(
                "The topic policy version already has different material."
            )

        selected: list[
            tuple[SourceDefinition, SourceDefinitionSnapshot, bool, int]
        ] = []
        for display_order, item in enumerate(normalized["sources"]):
            source = (
                SourceDefinition.objects.using(alias)
                .select_for_update()
                .filter(topic_code=topic_code, key=item["key"])
                .first()
            )
            if source is None:
                source = SourceDefinition(
                    topic_code=topic_code,
                    key=item["key"],
                )
                _apply_source_projection(source, item["material"])
                source.save(using=alias)
            target_hash = source_snapshot_hash(item["material"])
            snapshot = None
            if (
                source.latest_draft_snapshot_id
                and source.latest_draft_config_hash == target_hash
            ):
                snapshot = (
                    SourceDefinitionSnapshot.objects.using(alias)
                    .select_for_update()
                    .get(pk=source.latest_draft_snapshot_id)
                )
                if (
                    snapshot.state
                    != SourceDefinitionSnapshot.State.DRAFT
                    or not _is_verifiable_v2_source_snapshot(
                        snapshot,
                        expected_config_hash=target_hash,
                    )
                ):
                    snapshot = None
            if snapshot is None:
                snapshot = (
                    SourceDefinitionSnapshot.objects.using(alias)
                    .select_for_update()
                    .filter(
                        source=source,
                        state=SourceDefinitionSnapshot.State.APPROVED,
                        config_hash=target_hash,
                    )
                    .order_by("-version")
                    .first()
                )
                if (
                    snapshot is not None
                    and not _is_verifiable_v2_source_snapshot(
                        snapshot,
                        expected_config_hash=target_hash,
                    )
                ):
                    snapshot = None
            if snapshot is None:
                _retire_current_draft(source, using=alias)
                version = _next_source_snapshot_version(
                    source,
                    using=alias,
                )
                snapshot = _create_draft_snapshot(
                    source=source,
                    material=item["material"],
                    version=version,
                    request_key=None,
                    request_hash=None,
                    using=alias,
                )
            elif snapshot.state == SourceDefinitionSnapshot.State.APPROVED:
                _retire_current_draft(source, using=alias)
            _apply_source_projection(source, item["material"])
            source.current_snapshot_version = snapshot.version
            if snapshot.state == SourceDefinitionSnapshot.State.DRAFT:
                source.latest_draft_snapshot = snapshot
                source.latest_draft_snapshot_version = snapshot.version
                source.latest_draft_config_hash = snapshot.config_hash
            else:
                source.latest_draft_snapshot = None
                source.latest_draft_snapshot_version = None
                source.latest_draft_config_hash = None
            source.save(using=alias)
            selected.append(
                (
                    source,
                    snapshot,
                    bool(item["material"]["enabled"]),
                    display_order,
                )
            )

        membership_material = [
            {
                "source_definition_id": source.id,
                "source_snapshot_id": snapshot.id,
                "config_hash": snapshot.config_hash,
                "enabled": enabled,
                "display_order": display_order,
            }
            for source, snapshot, enabled, display_order in selected
        ]
        manifest_hash = registry_manifest_hash_for_memberships(
            membership_material
        )
        base = None
        if head.current_approved_registry_id:
            base = SourceRegistrySnapshot.objects.using(alias).get(
                pk=head.current_approved_registry_id
            )
        registry = SourceRegistrySnapshot.objects.using(alias).create(
            topic_code=topic_code,
            version=_next_registry_version(topic_code, using=alias),
            state=SourceRegistrySnapshot.State.DRAFT,
            manifest_hash=manifest_hash,
            row_version=1,
            base_approved_registry=base,
            base_approved_version=head.current_approved_version,
            base_approved_manifest_hash=(
                head.current_approved_manifest_hash
            ),
            draft_request_key=request_key,
            draft_request_hash=request_hash,
        )
        SourceRegistryMembership.objects.using(alias).bulk_create(
            [
                SourceRegistryMembership(
                    registry=registry,
                    source_definition=source,
                    source_snapshot=snapshot,
                    enabled=enabled,
                    display_order=display_order,
                )
                for source, snapshot, enabled, display_order in selected
            ]
        )
        record_audit_event(
            context=audit_context,
            action="source_registry.imported",
            entity=registry,
            identity_key=request_key,
            material_schema_version=REGISTRY_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            after_material=_registry_audit_material(registry),
            metadata={
                "request_hash": request_hash,
                "result": "imported",
                "repository_hash": repository_hash,
                "registry_id": str(registry.id),
                "manifest_hash": registry.manifest_hash,
                "version": registry.version,
                "membership_count": len(selected),
            },
        )
        return RegistryImportResult(
            topic_code=topic_code,
            registry_id=str(registry.id),
            source_count=len(selected),
            created=True,
        )


def _source_check_snapshot(
    source: SourceDefinition,
    *,
    using: str,
) -> SourceDefinitionSnapshot:
    if source.latest_draft_snapshot_id is not None:
        return SourceDefinitionSnapshot.objects.using(using).get(
            pk=source.latest_draft_snapshot_id
        )
    if source.latest_approved_snapshot_version is not None:
        return SourceDefinitionSnapshot.objects.using(using).get(
            source=source,
            version=source.latest_approved_snapshot_version,
            state=SourceDefinitionSnapshot.State.APPROVED,
        )
    raise StateConflict(
        "The source has no current draft or approved snapshot to check."
    )


def request_source_check(
    source_id: Any,
    *,
    admin: Any,
    request_key: str,
    audit_context: AuditContext,
) -> Any:
    from wisdome_writer.infrastructure.outbox import enqueue_event

    if not request_key:
        raise ValueError("A source check request key is required.")
    reason = "source access check"
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        source = (
            SourceDefinition.objects.using(alias)
            .select_for_update()
            .get(pk=source_id)
        )
        snapshot = _source_check_snapshot(source, using=alias)
        check_id = uuid.uuid4()
        event = enqueue_event(
            topic="source.check_requested",
            aggregate_type="SourceDefinition",
            aggregate_id=source.id,
            message_key=f"source.check_requested:{source.id}:{check_id}",
            correlation_id=audit_context.correlation_id,
            job_id=check_id,
            operation="check",
            payload={
                "source_id": str(source.id),
                "source_snapshot_id": str(snapshot.id),
                "source_config_hash": snapshot.config_hash,
                "check_id": str(check_id),
            },
            max_attempts=3,
        )
        record_audit_event(
            context=audit_context,
            action="source_definition.check_requested",
            entity=source,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=SOURCE_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            before_material=_source_audit_material(source),
            after_material=_source_audit_material(source),
            metadata={
                "result": "accepted",
                "source_id": str(source.id),
                "snapshot_id": str(snapshot.id),
                "config_hash": snapshot.config_hash,
                "job_id": str(event.job_id),
            },
        )
        return event


def record_source_check_result(
    *,
    source_id: Any,
    source_snapshot_id: Any,
    source_config_hash: str,
    status: str,
    record_count: int,
    error_code: str | None,
) -> dict[str, Any]:
    if status not in {"passed", "failed"}:
        raise ValueError("Unsupported source check status.")
    safe_error_code = (
        re.sub(r"[^a-z0-9_.-]+", "_", str(error_code).lower())[:120]
        if error_code
        else None
    )
    with transaction.atomic():
        source = (
            SourceDefinition.objects.select_for_update().get(pk=source_id)
        )
        snapshot = SourceDefinitionSnapshot.objects.get(
            pk=source_snapshot_id,
            source=source,
        )
        if snapshot.config_hash != source_config_hash:
            raise Conflict("The source check snapshot hash is inconsistent.")
        current_snapshot = _source_check_snapshot(source, using="default")
        result = {
            "snapshotId": str(snapshot.id),
            "configHash": snapshot.config_hash,
            "status": status,
            "checkedAt": timezone.now().isoformat(),
            "recordCount": max(0, min(int(record_count), 1_000_000)),
            "errorCode": safe_error_code,
        }
        if current_snapshot.id == snapshot.id:
            source.last_health = result
            source.save(
                update_fields=["last_health", "updated_at"],
            )
        return result


def current_registry(
    topic_code: str,
    *,
    for_update: bool = False,
) -> SourceRegistrySnapshot:
    if (
        for_update
        and not transaction.get_connection().in_atomic_block
    ):
        raise RuntimeError(
            "A locked registry lookup requires an active transaction."
        )
    head_queryset = TopicRegistryHead.objects
    if for_update:
        head_queryset = head_queryset.select_for_update()
    try:
        head = head_queryset.get(topic_code=topic_code)
    except TopicRegistryHead.DoesNotExist as exc:
        raise StateConflict(
            "No approved source registry head exists for this topic."
        ) from exc
    if head.current_approved_registry_id is None:
        raise StateConflict(
            "No consistent approved source registry exists for this topic."
        )
    registry_queryset = SourceRegistrySnapshot.objects
    if for_update:
        registry_queryset = registry_queryset.select_for_update()
    registry = registry_queryset.get(
        pk=head.current_approved_registry_id
    )
    if (
        registry.state != SourceRegistrySnapshot.State.APPROVED
        or registry.version != head.current_approved_version
        or registry.manifest_hash
        != head.current_approved_manifest_hash
    ):
        raise StateConflict(
            "No consistent approved source registry exists for this topic."
        )
    memberships = _verify_registry_manifest(
        registry,
        using=registry._state.db or "default",
    )
    for member in memberships:
        if not member.enabled:
            continue
        snapshot = member.source_snapshot
        if (
            snapshot.state != SourceDefinitionSnapshot.State.APPROVED
            or not _is_verifiable_v2_source_snapshot(snapshot)
        ):
            raise Conflict(
                "An approved source registry contains an unverifiable snapshot."
            )
    return (
        SourceRegistrySnapshot.objects.prefetch_related(
            "memberships__source_definition",
            "memberships__source_snapshot",
        )
        .select_related("latest_decision", "base_approved_registry")
        .get(pk=registry.pk)
    )
