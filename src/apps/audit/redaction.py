import json
import re
from collections.abc import Mapping
from typing import Any

from django.conf import settings

from wisdome_writer.domain.hashing import sha256_hex

MAX_DEPTH = 4
MAX_BYTES = 8 * 1024
MAX_SCALAR_LENGTH = 512
POLICY_VERSION = "audit-redaction-v1"

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|token|password|passwd|secret|api[-_]?key|body|content|raw|text)",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?:bearer\s+[a-z0-9._~+/=-]+|basic\s+[a-z0-9+/=]+)", re.IGNORECASE)

DEFAULT_SAFE_KEYS = frozenset(
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


class AuditRedactionError(ValueError):
    pass


def _allowed_keys(action: str) -> frozenset[str]:
    configured = getattr(settings, "AUDIT_METADATA_ALLOWLISTS", {})
    return DEFAULT_SAFE_KEYS | frozenset(configured.get(action, ()))


def _sanitize(value: Any, *, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise AuditRedactionError("audit metadata exceeds maximum depth")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_SCALAR_LENGTH:
            raise AuditRedactionError("audit metadata scalar is too long")
        if _BEARER_VALUE.search(value):
            raise AuditRedactionError("credential-like value is forbidden in audit metadata")
        return value
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if _SENSITIVE_KEY.search(normalized_key):
                raise AuditRedactionError("sensitive key is forbidden in audit metadata")
            result[normalized_key] = _sanitize(item, depth=depth + 1)
        return result
    raise AuditRedactionError(f"unsupported audit metadata type: {type(value).__name__}")


def sanitize_metadata(action: str, metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    allowed = _allowed_keys(action)
    unknown = set(metadata) - allowed
    if unknown:
        raise AuditRedactionError("audit metadata contains keys not allowed for this action")
    sanitized = _sanitize(dict(metadata), depth=1)
    encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise AuditRedactionError("audit metadata exceeds maximum encoded size")
    return sanitized


def redaction_policy_hash(action: str) -> str:
    return sha256_hex(
        {
            "version": POLICY_VERSION,
            "action": action,
            "allowed_keys": sorted(_allowed_keys(action)),
            "max_depth": MAX_DEPTH,
            "max_bytes": MAX_BYTES,
            "max_scalar_length": MAX_SCALAR_LENGTH,
        }
    )

