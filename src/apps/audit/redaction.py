from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
    sha256_hex,
)

MAX_DEPTH = 4
MAX_BYTES = 8 * 1024
MAX_SCALAR_LENGTH = 512
MAX_CONTAINER_ITEMS = 50
MAX_REASON_LENGTH = 500
POLICY_VERSION = "audit-redaction-v2"
LEGACY_POLICY_VERSION = "audit-redaction-v1"

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_CLASSIFICATION_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DOMAIN_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:+/@~-]{0,199}$"
)
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|token|password|passwd|secret|api[-_]?key|"
    r"credential|session|csrf|body|content|raw|html|extracted[_-]?text)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(
        r"(?i)(?:access[_-]?token|refresh[_-]?token|api[_-]?key|password|passwd|"
        r"authorization|cookie|client[_-]?secret|credential)\s*(?:=|:)\s*\S+"
    ),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)\btop[-_]?secret\b"),
)
_RAW_CONTENT_VALUE_PATTERNS = (
    re.compile(
        r"(?i)(?:raw[_ -]?(?:body|content|text)|body[_ -]?(?:text|html)|"
        r"extracted[_ -]?text)\s*(?:=|:)"
    ),
    re.compile(r"(?is)<(?:html|body|article|script|style|div|p|h[1-6])(?:\s|>)"),
)


class AuditRedactionError(ValueError):
    pass


@dataclass(frozen=True)
class AuditMetadataPolicy:
    allowed_keys: frozenset[str]


_PROVENANCE_KEYS = frozenset(
    {
        "request_key",
        "event_key",
        "operation_key",
        "reauth_proof_id",
    }
)
_DOMAIN_KEY_KEYS = frozenset(
    {
        "event_key",
        "operation_key",
        "request_key",
        "tick_key",
    }
)
_CLASSIFICATION_CODE_KEYS = frozenset(
    {
        "action",
        "channel",
        "decision",
        "error_code",
        "policy_version",
        "reason_code",
        "result",
        "source_type",
        "stage",
        "state",
        "status",
        "target_type",
        "worker_name",
    }
)
_CLASSIFICATION_INTEGER_KEYS = frozenset(
    {
        "attempt",
        "count",
        "version",
    }
)
_CLASSIFICATION_KEYS = (
    _CLASSIFICATION_CODE_KEYS
    | _CLASSIFICATION_INTEGER_KEYS
    | frozenset({"duration_ms", "enabled"})
)
_HASH_KEYS = frozenset(
    {
        "approval_hash",
        "checksum",
        "decision_hash",
        "intent_hash",
        "manifest_hash",
        "material_hash",
        "outcome_hash",
        "profile_material_hash",
        "request_hash",
        "result_hash",
        "subject_hash",
        "target_config_hash",
    }
)
_IDENTITY_KEYS = frozenset(
    {
        "activation_id",
        "article_id",
        "coalesced_into_id",
        "collection_run_id",
        "decision_id",
        "dispatch_id",
        "evidence_id",
        "intent_id",
        "profile_id",
        "publication_attempt_id",
        "remote_media_id",
        "registry_snapshot_id",
        "revision_id",
        "schedule_id",
        "snapshot_id",
        "source_event_id",
        "target_id",
        "validation_id",
    }
)
_VERSION_KEYS = frozenset(
    {
        "reconcile_attempt_no",
        "revision_no",
        "row_version",
        "schedule_version",
        "snapshot_version",
    }
)


def _policy(*extra_keys: str) -> AuditMetadataPolicy:
    return AuditMetadataPolicy(
        allowed_keys=_PROVENANCE_KEYS | frozenset(extra_keys)
    )


# Every accepted action/schema pair is explicit. Unknown actions and schema versions
# fail closed even when the caller supplies empty metadata.
_TARGET_KEYS = (
    "decision",
    "decision_id",
    "enabled",
    "error_code",
    "outcome_hash",
    "policy_version",
    "request_hash",
    "result",
    "result_hash",
    "snapshot_id",
    "snapshot_version",
    "stage",
    "state",
    "status",
    "target_config_hash",
    "target_id",
    "target_type",
    "version",
)
_VALIDATION_KEYS = (
    "activation_id",
    "count",
    "decision",
    "decision_hash",
    "decision_id",
    "enabled",
    "error_code",
    "manifest_hash",
    "material_hash",
    "request_hash",
    "result",
    "state",
    "status",
    "target_id",
    "validation_id",
    "version",
)
_INTENT_KEYS = (
    "action",
    "count",
    "error_code",
    "intent_hash",
    "intent_id",
    "manifest_hash",
    "request_hash",
    "result",
    "revision_id",
    "revision_no",
    "state",
    "status",
    "target_id",
    "target_type",
)
_APPROVAL_KEYS = (
    "action",
    "approval_hash",
    "decision",
    "decision_id",
    "error_code",
    "intent_id",
    "request_hash",
    "result",
    "state",
    "status",
    "subject_hash",
    "target_id",
)
_ATTEMPT_KEYS = (
    "action",
    "attempt",
    "channel",
    "duration_ms",
    "error_code",
    "intent_id",
    "outcome_hash",
    "publication_attempt_id",
    "reconcile_attempt_no",
    "request_hash",
    "result",
    "result_hash",
    "source_event_id",
    "state",
    "status",
    "target_id",
    "worker_name",
)
_REGISTRY_KEYS = (
    "count",
    "decision",
    "decision_hash",
    "error_code",
    "manifest_hash",
    "material_hash",
    "registry_snapshot_id",
    "request_hash",
    "result",
    "source_type",
    "state",
    "status",
    "version",
)
_SCHEDULE_KEYS = (
    "coalesced_into_id",
    "collection_run_id",
    "decision",
    "decision_id",
    "dispatch_id",
    "enabled",
    "error_code",
    "result",
    "reason_code",
    "request_hash",
    "row_version",
    "schedule_id",
    "schedule_version",
    "state",
    "status",
    "tick_key",
    "version",
)
_ARTICLE_KEYS = (
    "article_id",
    "collection_run_id",
    "count",
    "error_code",
    "intent_id",
    "manifest_hash",
    "material_hash",
    "result",
    "result_hash",
    "request_hash",
    "revision_id",
    "revision_no",
    "state",
    "status",
    "subject_hash",
)
_EVIDENCE_KEYS = (
    "decision",
    "decision_hash",
    "decision_id",
    "error_code",
    "evidence_id",
    "profile_id",
    "profile_material_hash",
    "request_hash",
    "result",
    "state",
    "status",
    "subject_hash",
    "version",
)
_REMOTE_MEDIA_KEYS = (
    "error_code",
    "intent_id",
    "publication_attempt_id",
    "remote_media_id",
    "result",
    "result_hash",
    "state",
    "status",
    "target_id",
)

ACTION_METADATA_POLICIES: dict[tuple[str, str], AuditMetadataPolicy] = {
    **{
        (action, "1"): _policy(*_TARGET_KEYS)
        for action in (
        "publication_target.created",
        "publication_target.updated",
        "publication_target.oauth_connected",
        "publication_target.preflight_requested",
        "publication_target.preflight",
        "publication_target.preflight_stale_before_call",
        "publication_target.preflight_stale_after_call",
        "publication_target.canary_requested",
        "publication_target.canary_started",
        "publication_target.canary_completed",
        "publication_target.disconnected",
        "publication_target.credential_revoke_started",
        "publication_target.credentials_revoked",
        "publication_target.credential_revoke_failed",
        )
    },
    **{
        (action, "1"): _policy(*_VALIDATION_KEYS)
        for action in (
        "auto_publish_validation.created",
        "auto_publish_validation.decided",
        "auto_publish_activation.decided",
        )
    },
    ("publication_intent.created", "1"): _policy(*_INTENT_KEYS),
    ("publication_approval.decided", "1"): _policy(*_APPROVAL_KEYS),
    ("publication.dispatched", "1"): _policy(
        "count",
        "error_code",
        "intent_id",
        "request_hash",
        "result",
        "result_hash",
        "state",
        "status",
        "target_id",
    ),
    **{
        (action, "1"): _policy(*_ATTEMPT_KEYS)
        for action in (
            "publication_attempt.started",
            "publication_attempt.finished",
            "publication_attempt.reconciled",
            "publication_attempt.retry_requested",
            "publication_attempt.retry_scheduled",
            "publication_attempt.retry_exhausted",
            "publication_attempt.reconcile_started",
            "publication_attempt.reconcile_delivery_failed",
        )
    },
    **{
        (action, "1"): _policy(*_REGISTRY_KEYS)
        for action in (
            "source_registry.imported",
            "source_registry.approved",
        )
    },
    **{
        (action, "1"): _policy(*_SCHEDULE_KEYS)
        for action in (
            "schedule.created",
            "schedule.updated",
            "schedule.disabled",
            "schedule_dispatch.skipped",
            "schedule_dispatch.queued",
            "schedule_dispatch.coalesced",
            "schedule_dispatch.dispatched",
            "schedule_dispatch.released",
            "kill_switch.decided",
        )
    },
    **{
        (action, "1"): _policy(*_ARTICLE_KEYS)
        for action in (
            "article.draft_generated",
            "article.revision.created",
            "collection_run.publication_started",
        )
    },
    **{
        (action, "1"): _policy(*_EVIDENCE_KEYS)
        for action in (
            "extraction_profile.decided",
            "evidence.profile_decided",
            "evidence.review_decided",
        )
    },
    **{
        (action, "1"): _policy(*_REMOTE_MEDIA_KEYS)
        for action in (
            "remote_media.reconcile_started",
            "remote_media.reconciled",
        )
    },
}


def _current_policy(action: str, metadata_schema_version: str) -> AuditMetadataPolicy:
    try:
        return ACTION_METADATA_POLICIES[(action, metadata_schema_version)]
    except KeyError as exc:
        raise AuditRedactionError(
            f"no audit metadata policy is registered for {action}@{metadata_schema_version}"
        ) from exc


def _reject_forbidden_value(value: str) -> None:
    if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise AuditRedactionError("credential-like value is forbidden in audit data")
    if any(pattern.search(value) for pattern in _RAW_CONTENT_VALUE_PATTERNS):
        raise AuditRedactionError("raw or extracted content is forbidden in audit data")


def sanitize_reason(reason: str | None, *, required: bool = False) -> str | None:
    if reason is None:
        if required:
            raise AuditRedactionError("an audit reason is required")
        return None
    if not isinstance(reason, str):
        raise AuditRedactionError("audit reason must be a string")
    if not reason.strip():
        raise AuditRedactionError("audit reason must not be blank")
    if len(reason) > MAX_REASON_LENGTH:
        raise AuditRedactionError("audit reason exceeds 500 characters")
    if any(ord(character) < 32 for character in reason):
        raise AuditRedactionError("audit reason contains control characters")
    _reject_forbidden_value(reason)
    return reason


def sanitize_audit_key(
    value: str,
    *,
    field_name: str = "audit key",
) -> str:
    if not isinstance(value, str):
        raise AuditRedactionError(f"{field_name} must be a string")
    if _DOMAIN_KEY_RE.fullmatch(value) is None:
        raise AuditRedactionError(
            f"{field_name} must be a safe audit identifier"
        )
    _reject_forbidden_value(value)
    return value


def _sanitize_scalar(value: Any) -> Any:
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise AuditRedactionError("non-finite numbers are forbidden in audit metadata")
        return value
    if isinstance(value, str):
        if len(value) > MAX_SCALAR_LENGTH:
            raise AuditRedactionError("audit metadata scalar is too long")
        _reject_forbidden_value(value)
        return value
    raise AuditRedactionError(f"unsupported audit metadata type: {type(value).__name__}")


def _sanitize_value(value: Any, *, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise AuditRedactionError("audit metadata exceeds maximum depth")
    if isinstance(value, Mapping):
        raise AuditRedactionError(
            "nested audit metadata objects require an explicitly versioned schema"
        )
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise AuditRedactionError("audit metadata container is too large")
        return [_sanitize_value(item, depth=depth + 1) for item in value]
    return _sanitize_scalar(value)


def _validate_key_value(key: str, value: Any) -> None:
    if key in _DOMAIN_KEY_KEYS and value is not None:
        sanitize_audit_key(value, field_name=key)
    if key in _CLASSIFICATION_CODE_KEYS and value is not None:
        if not isinstance(value, str):
            raise AuditRedactionError(f"{key} must be a scalar code string")
        if value == "":
            if key != "error_code":
                raise AuditRedactionError(f"{key} must not be an empty code")
        elif _CLASSIFICATION_CODE_RE.fullmatch(value) is None:
            raise AuditRedactionError(f"{key} contains an invalid scalar code")
    if key in _CLASSIFICATION_INTEGER_KEYS and value is not None:
        if type(value) is not int or value < 0:
            raise AuditRedactionError(f"{key} must be a non-negative integer")
    if key in _HASH_KEYS and value is not None:
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise AuditRedactionError(f"{key} must be a lowercase SHA-256 digest")
    if key in (_IDENTITY_KEYS | {"reauth_proof_id"}) and value is not None:
        from uuid import UUID

        try:
            UUID(str(value))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AuditRedactionError(f"{key} must be a UUID") from exc
    if key in _VERSION_KEYS and value is not None:
        if type(value) is not int or value < 0:
            raise AuditRedactionError(f"{key} must be a non-negative integer")
    if key == "duration_ms" and value is not None:
        if (
            type(value) not in {int, float}
            or value < 0
            or (type(value) is float and not math.isfinite(value))
        ):
            raise AuditRedactionError("duration_ms must be non-negative")
    if key == "enabled" and value is not None and type(value) is not bool:
        raise AuditRedactionError("enabled must be a boolean")


def sanitize_metadata(
    action: str,
    metadata: Mapping[str, Any] | None,
    *,
    metadata_schema_version: str = "1",
) -> dict[str, Any]:
    policy = _current_policy(action, metadata_schema_version)
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise AuditRedactionError("audit metadata must be an object")
    if len(metadata) > MAX_CONTAINER_ITEMS:
        raise AuditRedactionError("audit metadata contains too many properties")

    normalized: dict[str, Any] = {}
    for raw_key, value in metadata.items():
        if not isinstance(raw_key, str):
            raise AuditRedactionError("audit metadata keys must be strings")
        key = unicodedata.normalize("NFC", raw_key)
        if key in normalized:
            raise AuditRedactionError("audit metadata keys collide after normalization")
        if _KEY_RE.fullmatch(key) is None or _SENSITIVE_KEY.search(key):
            raise AuditRedactionError("sensitive or invalid key is forbidden in audit metadata")
        if key not in policy.allowed_keys:
            raise AuditRedactionError(
                f"audit metadata key {key!r} is not allowed for {action}@{metadata_schema_version}"
            )
        sanitized = _sanitize_value(value, depth=1)
        _validate_key_value(key, sanitized)
        normalized[key] = sanitized

    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise AuditRedactionError("audit metadata exceeds maximum encoded size")
    return normalized


def _regex_semantics(
    pattern: re.Pattern[str],
    *,
    operation: str,
) -> dict[str, int | str]:
    return {
        "pattern": pattern.pattern,
        "flags": int(pattern.flags),
        "operation": operation,
    }


def redaction_policy_hash(
    action: str,
    *,
    metadata_schema_version: str = "1",
) -> str:
    policy = _current_policy(action, metadata_schema_version)
    return canonical_hash(
        {
            "version": POLICY_VERSION,
            "action": action,
            "metadata_schema_version": metadata_schema_version,
            "allowed_keys": sorted(policy.allowed_keys),
            "metadata_object_rules": {
                "accepted_type": "mapping_or_subclass",
                "none_becomes_empty_object": True,
                "max_properties": MAX_CONTAINER_ITEMS,
                "key_rules": {
                    "accepted_type": "string_or_subclass",
                    "normalization": "NFC",
                    "normalized_collisions": "forbidden",
                    "syntax": _regex_semantics(
                        _KEY_RE,
                        operation="fullmatch",
                    ),
                    "sensitive": _regex_semantics(
                        _SENSITIVE_KEY,
                        operation="search",
                    ),
                    "registered_allowlist_required": True,
                },
                "encoded_size": {
                    "encoding": "utf-8",
                    "json_ensure_ascii": False,
                    "json_separators": [",", ":"],
                    "json_allow_nan": False,
                    "json_sort_keys": False,
                    "max_bytes": MAX_BYTES,
                },
            },
            "value_rules": {
                "root_depth": 1,
                "max_depth": MAX_DEPTH,
                "scalar_types": {
                    "null": "allowed",
                    "boolean": "exact_bool",
                    "integer": "exact_int",
                    "float": "exact_finite_float",
                    "string": "string_or_subclass",
                },
                "max_scalar_length": MAX_SCALAR_LENGTH,
                "scalar_length_measure": "python_len",
                "nested_objects": "forbidden",
                "containers": {
                    "accepted_types": ["list_or_subclass", "tuple_or_subclass"],
                    "normalized_type": "list",
                    "max_items": MAX_CONTAINER_ITEMS,
                },
                "secret_patterns": [
                    _regex_semantics(pattern, operation="search")
                    for pattern in _SECRET_VALUE_PATTERNS
                ],
                "raw_content_patterns": [
                    _regex_semantics(pattern, operation="search")
                    for pattern in _RAW_CONTENT_VALUE_PATTERNS
                ],
            },
            "field_rules": {
                "classification": {
                    "keys": sorted(_CLASSIFICATION_KEYS),
                    "code_keys": sorted(_CLASSIFICATION_CODE_KEYS),
                    "code_type": "string_or_subclass",
                    "code_syntax": _regex_semantics(
                        _CLASSIFICATION_CODE_RE,
                        operation="fullmatch",
                    ),
                    "empty_code_allowed_for": ["error_code"],
                    "integer_keys": sorted(_CLASSIFICATION_INTEGER_KEYS),
                    "integer_rule": "exact_non_negative_integer",
                    "duration_ms": "exact_non_negative_finite_integer_or_float",
                    "enabled": "exact_boolean",
                    "null": "allowed",
                },
                "hash": {
                    "keys": sorted(_HASH_KEYS),
                    "type": "string_or_subclass",
                    "syntax": _regex_semantics(
                        _SHA256_RE,
                        operation="fullmatch",
                    ),
                    "null": "allowed",
                },
                "identity": {
                    "keys": sorted(_IDENTITY_KEYS | {"reauth_proof_id"}),
                    "rule": "python_uuid_parseable_string",
                    "null": "allowed",
                },
                "domain_key": {
                    "keys": sorted(_DOMAIN_KEY_KEYS),
                    "type": "string_or_subclass",
                    "syntax": _regex_semantics(
                        _DOMAIN_KEY_RE,
                        operation="fullmatch",
                    ),
                    "secret_and_raw_value_rules": "current_value_rules",
                    "null": "allowed",
                },
                "version": {
                    "keys": sorted(_VERSION_KEYS),
                    "rule": "exact_non_negative_integer",
                    "null": "allowed",
                },
            },
            "reason_rules": {
                "accepted_type": "string_or_subclass",
                "none": "allowed_unless_required_by_caller",
                "blank_rule": "python_str_strip_must_be_nonempty",
                "max_length": MAX_REASON_LENGTH,
                "length_measure": "python_len",
                "control_characters_below_u0020": "forbidden",
                "secret_patterns": [
                    _regex_semantics(pattern, operation="search")
                    for pattern in _SECRET_VALUE_PATTERNS
                ],
                "raw_content_patterns": [
                    _regex_semantics(pattern, operation="search")
                    for pattern in _RAW_CONTENT_VALUE_PATTERNS
                ],
            },
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


# v1 is retained only so historical rows can be revalidated against the exact
# policy family that created them. New inserts can never select it.
_LEGACY_MAX_DEPTH = 4
_LEGACY_MAX_BYTES = 8 * 1024
_LEGACY_MAX_SCALAR_LENGTH = 512
_LEGACY_DEFAULT_SAFE_KEYS = frozenset(
    {
        "request_key",
        "state",
        "status",
        "action",
        "reason_code",
        "error_code",
        "count",
        "attempt",
        "version",
        "manifest_hash",
        "material_hash",
        "checksum",
        "source_type",
        "target_type",
        "queue",
        "duration_ms",
    }
)
_LEGACY_ACTION_ALLOWLISTS: Mapping[str, frozenset[str]] = MappingProxyType({})
_LEGACY_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|token|password|passwd|secret|api[-_]?key|body|content|raw|text)",
    re.IGNORECASE,
)
_LEGACY_BEARER_VALUE = re.compile(
    r"(?:bearer\s+[a-z0-9._~+/=-]+|basic\s+[a-z0-9+/=]+)",
    re.IGNORECASE,
)


def _legacy_allowed_keys(action: str) -> frozenset[str]:
    return _LEGACY_DEFAULT_SAFE_KEYS | _LEGACY_ACTION_ALLOWLISTS.get(
        action, frozenset()
    )


def _legacy_sanitize(value: Any, *, depth: int) -> Any:
    if depth > _LEGACY_MAX_DEPTH:
        raise AuditRedactionError("audit metadata exceeds maximum depth")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _LEGACY_MAX_SCALAR_LENGTH:
            raise AuditRedactionError("audit metadata scalar is too long")
        if _LEGACY_BEARER_VALUE.search(value):
            raise AuditRedactionError("credential-like value is forbidden in audit metadata")
        return value
    if isinstance(value, (list, tuple)):
        return [_legacy_sanitize(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if _LEGACY_SENSITIVE_KEY.search(normalized_key):
                raise AuditRedactionError("sensitive key is forbidden in audit metadata")
            result[normalized_key] = _legacy_sanitize(item, depth=depth + 1)
        return result
    raise AuditRedactionError(f"unsupported audit metadata type: {type(value).__name__}")


def _legacy_sanitize_metadata(
    action: str, metadata: Any
) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise AuditRedactionError("stored legacy audit metadata must be an object")
    if not metadata:
        return {}
    allowed = _legacy_allowed_keys(action)
    unknown = set(metadata) - allowed
    if unknown:
        raise AuditRedactionError("audit metadata contains keys not allowed for this action")
    sanitized = _legacy_sanitize(dict(metadata), depth=1)
    encoded = json.dumps(
        sanitized, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > _LEGACY_MAX_BYTES:
        raise AuditRedactionError("audit metadata exceeds maximum encoded size")
    return sanitized


def _validate_current_output_safety(value: Any, *, depth: int = 1) -> None:
    """Apply current secret/raw checks after validating a historical policy."""

    if depth > MAX_DEPTH:
        raise AuditRedactionError("stored audit metadata exceeds maximum depth")
    if isinstance(value, Mapping):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise AuditRedactionError("stored audit metadata object is too large")
        for key, child in value.items():
            normalized_key = unicodedata.normalize("NFC", str(key))
            if _SENSITIVE_KEY.search(normalized_key):
                raise AuditRedactionError(
                    "stored audit metadata contains a currently forbidden key"
                )
            _validate_current_output_safety(child, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise AuditRedactionError("stored audit metadata list is too large")
        for child in value:
            _validate_current_output_safety(child, depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value) > MAX_SCALAR_LENGTH:
            raise AuditRedactionError("stored audit metadata scalar is too long")
        _reject_forbidden_value(value)
        return
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float and math.isfinite(value):
        return
    raise AuditRedactionError("stored audit metadata contains an unsafe value")


def _legacy_policy_hash(action: str) -> str:
    return sha256_hex(
        {
            "version": LEGACY_POLICY_VERSION,
            "action": action,
            "allowed_keys": sorted(_legacy_allowed_keys(action)),
            "max_depth": _LEGACY_MAX_DEPTH,
            "max_bytes": _LEGACY_MAX_BYTES,
            "max_scalar_length": _LEGACY_MAX_SCALAR_LENGTH,
        }
    )


def validate_stored_metadata(
    *,
    action: str,
    metadata_schema_version: str,
    redaction_policy_version: str,
    redaction_policy_hash_value: str,
    metadata: Any,
) -> dict[str, Any]:
    if redaction_policy_version == POLICY_VERSION:
        expected = redaction_policy_hash(
            action, metadata_schema_version=metadata_schema_version
        )
        if redaction_policy_hash_value != expected:
            raise AuditRedactionError("stored audit redaction policy hash is invalid")
        sanitized = sanitize_metadata(
            action,
            metadata,
            metadata_schema_version=metadata_schema_version,
        )
        _validate_current_output_safety(sanitized)
        return sanitized
    if redaction_policy_version == LEGACY_POLICY_VERSION:
        if redaction_policy_hash_value != _legacy_policy_hash(action):
            raise AuditRedactionError("stored legacy redaction policy hash is invalid")
        sanitized = _legacy_sanitize_metadata(action, metadata)
        _validate_current_output_safety(sanitized)
        return sanitized
    raise AuditRedactionError("stored audit redaction policy version is unsupported")
