import hashlib
import json
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core import signing

from wisdome_writer.domain.hashing import canonical_json_bytes

SALT = "wisdome-writer.audit-cursor.v1"


def filter_hash(filters: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(filters)).hexdigest()


def encode_cursor(
    *, filters: dict[str, Any], watermark: tuple[datetime, str], last: tuple[datetime, str], limit: int
) -> str:
    payload = {
        "filter_hash": filter_hash(filters),
        "watermark": [watermark[0].isoformat(), watermark[1]],
        "last": [last[0].isoformat(), last[1]],
        "limit": limit,
    }
    return signing.dumps(payload, key=settings.AUDIT_CURSOR_SIGNING_KEY, salt=SALT, compress=True)


def decode_cursor(value: str, *, filters: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = signing.loads(value, key=settings.AUDIT_CURSOR_SIGNING_KEY, salt=SALT)
    except signing.BadSignature as exc:
        raise ValueError("invalid audit cursor") from exc
    if payload.get("filter_hash") != filter_hash(filters):
        raise ValueError("audit cursor does not match filters")
    return payload

