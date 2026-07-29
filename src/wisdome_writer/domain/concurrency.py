from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Self

from wisdome_writer.domain.errors import RequestKeyConflict, StaleVersion
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)


_OPERATION_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,199}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUBJECT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9 _.-]{0,99}$")


def _operation_id(value: object) -> str:
    if not isinstance(value, str) or not _OPERATION_ID_PATTERN.fullmatch(value):
        raise ValueError("operation_id must be a stable OpenAPI operation identifier")
    return value


def _path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or not value.startswith("/")
        or "?" in value
        or "#" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("path must be a normalized path identity")
    return value


def canonical_request_hash(
    *,
    operation_id: str,
    path: str,
    payload: Mapping[str, Any],
) -> str:
    normalized_operation_id = _operation_id(operation_id)
    normalized_path = _path(path)
    if not isinstance(payload, Mapping):
        raise TypeError("request identity payload must be an object")
    return canonical_hash(
        {
            "schema_version": "canonical-request-identity-v1",
            "operation_id": normalized_operation_id,
            "path": normalized_path,
            "payload": payload,
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            key: _freeze(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    operation_id: str
    path: str
    payload: Mapping[str, Any]
    request_hash: str = ""

    def __post_init__(self) -> None:
        frozen_payload = _freeze(self.payload)
        if not isinstance(frozen_payload, Mapping):
            raise TypeError("request identity payload must be an object")
        object.__setattr__(self, "operation_id", _operation_id(self.operation_id))
        object.__setattr__(self, "path", _path(self.path))
        object.__setattr__(self, "payload", frozen_payload)
        computed = canonical_request_hash(
            operation_id=self.operation_id,
            path=self.path,
            payload=frozen_payload,
        )
        if self.request_hash and self.request_hash != computed:
            raise ValueError("request_hash does not match the request identity")
        object.__setattr__(self, "request_hash", computed)

    @classmethod
    def build(
        cls,
        *,
        operation_id: str,
        path: str,
        payload: Mapping[str, Any],
    ) -> Self:
        return cls(
            operation_id=operation_id,
            path=path,
            payload=payload,
        )

    @property
    def hash(self) -> str:
        return self.request_hash


def require_idempotent_match(
    *,
    stored_hash: str,
    expected_hash: str,
) -> None:
    if (
        not isinstance(stored_hash, str)
        or not _HASH_PATTERN.fullmatch(stored_hash)
        or not isinstance(expected_hash, str)
        or not _HASH_PATTERN.fullmatch(expected_hash)
        or not hmac.compare_digest(stored_hash, expected_hash)
    ):
        raise RequestKeyConflict(
            "The request key is already bound to different request material"
        )


def require_expected_version(
    *,
    actual: Any,
    expected: Any,
    subject: str,
) -> None:
    if type(actual) is type(expected) and actual == expected:
        return
    public_subject = (
        subject
        if isinstance(subject, str) and _SUBJECT_PATTERN.fullmatch(subject)
        else "Resource"
    )
    raise StaleVersion(f"{public_subject} version does not match the current state")
