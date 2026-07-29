from __future__ import annotations

import hashlib
import math
import re
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from django.conf import settings
from django.core import signing

from wisdome_writer.domain.errors import InvalidCursor
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_json_bytes,
)


CURSOR_VERSION = 1
MAX_CURSOR_LENGTH = 4096
_MAX_FILTER_BYTES = 16 * 1024
_MAX_FILTER_NODES = 256
_MAX_FILTER_DEPTH = 6
_MAX_POSITION_FIELDS = 16
_MAX_PAGE_LIMIT = 200
_MAX_SAFE_INTEGER = 2**53 - 1
_RESOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_ORDER_FIELD_PATTERN = re.compile(r"^-?[A-Za-z][A-Za-z0-9_.]{0,63}$")
_POSITION_FIELD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_FILTER_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SALT_PREFIX = "wisdome-writer.cursor.v1"
_PAYLOAD_KEYS = frozenset({"v", "r", "f", "o", "l", "p", "w"})


CursorScalar = str | int | None


@dataclass(frozen=True, slots=True)
class CursorState:
    version: int
    resource: str
    filter_hash: str
    order: tuple[str, ...]
    limit: int
    position: Mapping[str, CursorScalar]
    watermark: Mapping[str, CursorScalar] | None


def _invalid_cursor() -> InvalidCursor:
    return InvalidCursor("The cursor is invalid or does not match this query")


def _resource(value: object) -> str:
    if not isinstance(value, str) or not _RESOURCE_PATTERN.fullmatch(value):
        raise _invalid_cursor()
    return value


def _limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_PAGE_LIMIT:
        raise _invalid_cursor()
    return value


def _order(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _invalid_cursor()
    normalized = tuple(value)
    if not 1 <= len(normalized) <= 8:
        raise _invalid_cursor()
    if any(
        not isinstance(item, str)
        or not _ORDER_FIELD_PATTERN.fullmatch(item)
        for item in normalized
    ):
        raise _invalid_cursor()
    if len(set(normalized)) != len(normalized):
        raise _invalid_cursor()
    return normalized


def _position(
    value: object,
    *,
    optional: bool,
) -> Mapping[str, CursorScalar] | None:
    if value is None and optional:
        return None
    if not isinstance(value, Mapping) or not 1 <= len(value) <= _MAX_POSITION_FIELDS:
        raise _invalid_cursor()

    normalized: dict[str, CursorScalar] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not _POSITION_FIELD_PATTERN.fullmatch(key)
            or key in normalized
        ):
            raise _invalid_cursor()
        if item is None:
            normalized[key] = None
        elif isinstance(item, str):
            if not 1 <= len(item) <= 512 or any(ord(char) < 32 for char in item):
                raise _invalid_cursor()
            normalized[key] = item
        elif type(item) is int and -_MAX_SAFE_INTEGER <= item <= _MAX_SAFE_INTEGER:
            normalized[key] = item
        else:
            raise _invalid_cursor()
    return MappingProxyType(normalized)


def _validate_filter_value(
    value: Any,
    *,
    depth: int,
    counter: list[int],
) -> None:
    counter[0] += 1
    if counter[0] > _MAX_FILTER_NODES or depth > _MAX_FILTER_DEPTH:
        raise _invalid_cursor()
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) > 2048 or any(ord(char) < 32 for char in value):
            raise _invalid_cursor()
        return
    if type(value) is int:
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise _invalid_cursor()
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid_cursor()
        return
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise _invalid_cursor()
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or len(key) > 128
                or any(ord(char) < 32 for char in key)
            ):
                raise _invalid_cursor()
            _validate_filter_value(item, depth=depth + 1, counter=counter)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise _invalid_cursor()
        for item in value:
            _validate_filter_value(item, depth=depth + 1, counter=counter)
        return
    raise _invalid_cursor()


def canonical_filter_hash(filters: Mapping[str, Any]) -> str:
    if not isinstance(filters, Mapping):
        raise _invalid_cursor()
    _validate_filter_value(filters, depth=0, counter=[0])
    try:
        payload = canonical_json_bytes(
            {
                "schema_version": "signed-cursor-filter-v1",
                "filters": filters,
            },
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_cursor() from exc
    if len(payload) > _MAX_FILTER_BYTES:
        raise _invalid_cursor()
    return hashlib.sha256(payload).hexdigest()


def _salt(resource: str) -> str:
    return f"{_SALT_PREFIX}:{resource}"


def encode_cursor(
    *,
    resource: str,
    filters: Mapping[str, Any],
    order: Sequence[str],
    limit: int,
    position: Mapping[str, CursorScalar],
    watermark: Mapping[str, CursorScalar] | None = None,
) -> str:
    normalized_resource = _resource(resource)
    normalized_order = _order(order)
    normalized_limit = _limit(limit)
    normalized_position = _position(position, optional=False)
    normalized_watermark = _position(watermark, optional=True)
    payload = {
        "v": CURSOR_VERSION,
        "r": normalized_resource,
        "f": canonical_filter_hash(filters),
        "o": list(normalized_order),
        "l": normalized_limit,
        "p": dict(normalized_position or {}),
        "w": (
            dict(normalized_watermark)
            if normalized_watermark is not None
            else None
        ),
    }
    value = signing.dumps(
        payload,
        key=settings.AUDIT_CURSOR_SIGNING_KEY,
        salt=_salt(normalized_resource),
        compress=True,
    )
    if len(value) > MAX_CURSOR_LENGTH:
        raise _invalid_cursor()
    return value


def decode_cursor(
    value: str,
    *,
    resource: str,
    filters: Mapping[str, Any],
    order: Sequence[str],
    limit: int,
) -> CursorState:
    normalized_resource = _resource(resource)
    normalized_order = _order(order)
    normalized_limit = _limit(limit)
    expected_filter_hash = canonical_filter_hash(filters)
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_CURSOR_LENGTH
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise _invalid_cursor()
    try:
        payload = signing.loads(
            value,
            key=settings.AUDIT_CURSOR_SIGNING_KEY,
            salt=_salt(normalized_resource),
        )
    except (
        signing.BadSignature,
        TypeError,
        ValueError,
        UnicodeError,
        zlib.error,
    ) as exc:
        raise _invalid_cursor() from exc
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        raise _invalid_cursor()
    if type(payload["v"]) is not int or payload["v"] != CURSOR_VERSION:
        raise _invalid_cursor()
    if payload["r"] != normalized_resource:
        raise _invalid_cursor()
    if (
        not isinstance(payload["f"], str)
        or not _FILTER_HASH_PATTERN.fullmatch(payload["f"])
        or payload["f"] != expected_filter_hash
    ):
        raise _invalid_cursor()
    stored_order = _order(payload["o"])
    stored_limit = _limit(payload["l"])
    if stored_order != normalized_order or stored_limit != normalized_limit:
        raise _invalid_cursor()
    position = _position(payload["p"], optional=False)
    watermark = _position(payload["w"], optional=True)
    return CursorState(
        version=CURSOR_VERSION,
        resource=normalized_resource,
        filter_hash=expected_filter_hash,
        order=normalized_order,
        limit=normalized_limit,
        position=position or MappingProxyType({}),
        watermark=watermark,
    )
