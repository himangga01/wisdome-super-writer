from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from wisdome_writer.api.pagination import (
    CursorState,
    canonical_filter_hash,
    decode_cursor as decode_signed_cursor,
    encode_cursor as encode_signed_cursor,
)
from wisdome_writer.domain.errors import InvalidCursor


RESOURCE = "audit.events"
ORDER = ("-occurred_at", "-id")
_POSITION_KEYS = frozenset({"occurred_at", "id"})


def filter_hash(filters: dict[str, Any]) -> str:
    return canonical_filter_hash(filters)


def _key(value: object) -> tuple[datetime, UUID]:
    if not isinstance(value, dict) and not hasattr(value, "keys"):
        raise InvalidCursor("The audit cursor position is invalid")
    if set(value.keys()) != _POSITION_KEYS:
        raise InvalidCursor("The audit cursor position is invalid")
    occurred_at_value = value["occurred_at"]
    identifier_value = value["id"]
    if not isinstance(occurred_at_value, str) or not isinstance(identifier_value, str):
        raise InvalidCursor("The audit cursor position is invalid")
    try:
        occurred_at = datetime.fromisoformat(occurred_at_value)
        identifier = UUID(identifier_value)
    except (TypeError, ValueError) as exc:
        raise InvalidCursor("The audit cursor position is invalid") from exc
    if (
        occurred_at.tzinfo is None
        or occurred_at.utcoffset() is None
        or occurred_at.isoformat() != occurred_at_value
        or str(identifier) != identifier_value
    ):
        raise InvalidCursor("The audit cursor position is invalid")
    return occurred_at, identifier


def encode_cursor(
    *,
    filters: dict[str, Any],
    watermark: tuple[datetime, str],
    last: tuple[datetime, str],
    limit: int,
) -> str:
    return encode_signed_cursor(
        resource=RESOURCE,
        filters=filters,
        order=ORDER,
        limit=limit,
        position={
            "occurred_at": last[0].isoformat(),
            "id": str(last[1]),
        },
        watermark={
            "occurred_at": watermark[0].isoformat(),
            "id": str(watermark[1]),
        },
    )


def decode_cursor(
    value: str,
    *,
    filters: dict[str, Any],
    limit: int,
) -> CursorState:
    state = decode_signed_cursor(
        value,
        resource=RESOURCE,
        filters=filters,
        order=ORDER,
        limit=limit,
    )
    if state.watermark is None:
        raise InvalidCursor("The audit cursor watermark is missing")
    _key(state.position)
    _key(state.watermark)
    return state


def cursor_key(value: object) -> tuple[datetime, UUID]:
    return _key(value)
