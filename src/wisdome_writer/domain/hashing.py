import hashlib
import unicodedata
from collections.abc import Mapping
from typing import Any

import rfc8785


CANONICAL_HASH_SCHEMA_VERSION = "nfc-rfc8785-sha256-v1"


def _normalize_json(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object member names must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError("canonical JSON object member names collide after NFC normalization")
            normalized[normalized_key] = _normalize_json(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(f"unsupported canonical JSON value type: {type(value).__name__}")


def canonical_json_bytes(
    value: Any,
    *,
    schema_version: str = CANONICAL_HASH_SCHEMA_VERSION,
) -> bytes:
    if schema_version != CANONICAL_HASH_SCHEMA_VERSION:
        raise ValueError(f"unsupported canonical hash schema version: {schema_version}")
    return rfc8785.dumps(_normalize_json(value))


def canonical_hash(value: Any, *, schema_version: str) -> str:
    return hashlib.sha256(
        canonical_json_bytes(value, schema_version=schema_version)
    ).hexdigest()


def sha256_hex(value: bytes | str | Any) -> str:
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = canonical_json_bytes(value)
    return hashlib.sha256(payload).hexdigest()

