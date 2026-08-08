from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    version_id: str | None
    checksum_sha256: str
    size: int
    content_type: str
    etag: str | None = None


@runtime_checkable
class ObjectStorage(Protocol):
    def put_bytes(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        checksum_sha256: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectInfo: ...

    def put_file(
        self,
        *,
        key: str,
        path: Path,
        content_type: str,
        checksum_sha256: str,
        expected_size: int,
        metadata: dict[str, str] | None = None,
    ) -> ObjectInfo: ...

    def get_bytes(self, *, key: str, version_id: str | None = None) -> bytes: ...

    def get_file(
        self,
        *,
        key: str,
        version_id: str | None,
        destination: Path,
        expected_checksum_sha256: str,
        expected_size: int,
    ) -> ObjectInfo: ...

    def head(self, *, key: str, version_id: str | None = None) -> ObjectInfo: ...

    def delete(self, *, key: str, version_id: str | None = None) -> None: ...

    def presign_get(
        self, *, key: str, version_id: str | None = None, expires_seconds: int | None = None
    ) -> str: ...

