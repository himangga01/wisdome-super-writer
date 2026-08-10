import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

import boto3
from botocore.config import Config
from django.conf import settings

from wisdome_writer.infrastructure.http_safety import safe_get

from .base import ObjectInfo


def _validate_key(key: str) -> str:
    path = PurePosixPath(key)
    if not key or key.startswith("/") or ".." in path.parts:
        raise ValueError("object key must be a non-empty relative path without parent traversal")
    return str(path)


class S3ObjectStorage:
    def __init__(
        self,
        *,
        bucket: str | None = None,
        client=None,
        public_base_url: str | None = None,
    ):
        self.bucket = bucket or settings.AWS_STORAGE_BUCKET_NAME
        self._client = client
        self.public_base_url = (
            public_base_url
            if public_base_url is not None
            else getattr(settings, "AWS_S3_PUBLIC_BASE_URL", "")
        ).rstrip("/")

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=settings.AWS_S3_ENDPOINT_URL,
                region_name=settings.AWS_S3_REGION_NAME,
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
                config=Config(
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"mode": "standard", "max_attempts": 3},
                    s3={"addressing_style": settings.AWS_S3_ADDRESSING_STYLE},
                ),
            )
        return self._client

    def put_bytes(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        checksum_sha256: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectInfo:
        key = _validate_key(key)
        actual_checksum = hashlib.sha256(data).hexdigest()
        if checksum_sha256 and checksum_sha256 != actual_checksum:
            raise ValueError("object checksum does not match supplied checksum")
        object_metadata = {str(k): str(v) for k, v in (metadata or {}).items()}
        object_metadata["sha256"] = actual_checksum
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata=object_metadata,
        )
        return ObjectInfo(
            key=key,
            version_id=response.get("VersionId"),
            checksum_sha256=actual_checksum,
            size=len(data),
            content_type=content_type,
            etag=(response.get("ETag") or "").strip('"') or None,
        )

    def put_file(
        self,
        *,
        key: str,
        path: Path,
        content_type: str,
        checksum_sha256: str,
        expected_size: int,
        metadata: dict[str, str] | None = None,
    ) -> ObjectInfo:
        """Upload a verified regular file without materializing its bytes in memory."""
        key = _validate_key(key)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        response = None
        try:
            file_metadata = os.fstat(descriptor)
            file_identity = (
                file_metadata.st_dev,
                file_metadata.st_ino,
                file_metadata.st_size,
                file_metadata.st_mtime_ns,
                file_metadata.st_ctime_ns,
            )
            if (
                not stat.S_ISREG(file_metadata.st_mode)
                or file_metadata.st_nlink != 1
                or file_metadata.st_size != expected_size
            ):
                raise ValueError("upload source is not the expected unique regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                before = hashlib.file_digest(source, "sha256").hexdigest()
                if before != checksum_sha256:
                    raise ValueError("object checksum does not match supplied checksum")
                source.seek(0)
                object_metadata = {
                    str(k): str(v) for k, v in (metadata or {}).items()
                }
                object_metadata["sha256"] = checksum_sha256
                response = self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=source,
                    ContentLength=expected_size,
                    ContentType=content_type,
                    Metadata=object_metadata,
                )
                source.seek(0)
                after = hashlib.file_digest(source, "sha256").hexdigest()
            observed = os.fstat(descriptor)
            observed_identity = (
                observed.st_dev,
                observed.st_ino,
                observed.st_size,
                observed.st_mtime_ns,
                observed.st_ctime_ns,
            )
            if before != after or observed_identity != file_identity:
                delete_params = {"Bucket": self.bucket, "Key": key}
                if response.get("VersionId"):
                    delete_params["VersionId"] = response["VersionId"]
                self.client.delete_object(**delete_params)
                raise ValueError("upload source changed while it was streamed")
        finally:
            os.close(descriptor)
        return ObjectInfo(
            key=key,
            version_id=response.get("VersionId"),
            checksum_sha256=checksum_sha256,
            size=expected_size,
            content_type=content_type,
            etag=(response.get("ETag") or "").strip('"') or None,
        )

    def get_bytes(self, *, key: str, version_id: str | None = None) -> bytes:
        params = {"Bucket": self.bucket, "Key": _validate_key(key)}
        if version_id:
            params["VersionId"] = version_id
        return self.client.get_object(**params)["Body"].read()

    def get_bounded_bytes(
        self,
        *,
        key: str,
        version_id: str,
        max_bytes: int,
        chunk_size: int = 1024 * 1024,
    ) -> bytes:
        """Read one immutable object version without trusting the response stream size."""
        if not isinstance(version_id, str) or not version_id.strip():
            raise ValueError("a frozen object version is required")
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if not isinstance(chunk_size, int) or chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        params = {
            "Bucket": self.bucket,
            "Key": _validate_key(key),
            "VersionId": version_id,
        }
        response = self.client.get_object(**params)
        body = response["Body"]
        content_length = response.get("ContentLength")
        try:
            if type(content_length) is not int or content_length < 0:
                raise ValueError("object ContentLength is missing or invalid")
            if content_length > max_bytes:
                raise ValueError("object exceeds the configured byte limit")
            if response.get("VersionId") != version_id:
                raise ValueError("object version differs from frozen provenance")
            chunks: list[bytes] = []
            observed = 0
            while True:
                chunk = body.read(min(chunk_size, max_bytes - observed + 1))
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ValueError("object body did not return bytes")
                observed += len(chunk)
                if observed > max_bytes:
                    raise ValueError("object exceeds the configured byte limit")
                chunks.append(chunk)
            if observed != content_length:
                raise ValueError("object ContentLength differs from streamed bytes")
            return b"".join(chunks)
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()

    def get_file(
        self,
        *,
        key: str,
        version_id: str | None,
        destination: Path,
        expected_checksum_sha256: str,
        expected_size: int,
    ) -> ObjectInfo:
        """Download an exact object to a new file using bounded chunks."""
        if not isinstance(version_id, str) or not version_id.strip():
            raise ValueError("a frozen object version is required")
        params = {"Bucket": self.bucket, "Key": _validate_key(key)}
        params["VersionId"] = version_id
        response = self.client.get_object(**params)
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        try:
            content_length = response.get("ContentLength")
            if type(content_length) is not int or content_length < 0:
                raise ValueError("object ContentLength is missing or invalid")
            if content_length != expected_size:
                raise ValueError("object ContentLength differs from frozen provenance")
            if response.get("VersionId") != version_id:
                raise ValueError("object version differs from frozen provenance")
            with destination.open("xb") as output:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > expected_size:
                        raise ValueError("downloaded object exceeds its expected size")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        observed_checksum = digest.hexdigest()
        if (
            size != expected_size
            or observed_checksum != expected_checksum_sha256
            or response["ContentLength"] != size
        ):
            destination.unlink(missing_ok=True)
            raise ValueError("downloaded object identity differs from provenance")
        destination.chmod(0o400)
        return ObjectInfo(
            key=params["Key"],
            version_id=response["VersionId"],
            checksum_sha256=observed_checksum,
            size=size,
            content_type=response.get("ContentType", "application/octet-stream"),
            etag=(response.get("ETag") or "").strip('"') or None,
        )

    def head(self, *, key: str, version_id: str | None = None) -> ObjectInfo:
        params = {"Bucket": self.bucket, "Key": _validate_key(key)}
        if version_id:
            params["VersionId"] = version_id
        response = self.client.head_object(**params)
        metadata = response.get("Metadata", {})
        return ObjectInfo(
            key=key,
            version_id=response.get("VersionId") or version_id,
            checksum_sha256=metadata.get("sha256", ""),
            size=response["ContentLength"],
            content_type=response.get("ContentType", "application/octet-stream"),
            etag=(response.get("ETag") or "").strip('"') or None,
        )

    def delete(self, *, key: str, version_id: str | None = None) -> None:
        params = {"Bucket": self.bucket, "Key": _validate_key(key)}
        if version_id:
            params["VersionId"] = version_id
        self.client.delete_object(**params)

    def delete_exact_version(self, *, key: str, version_id: str) -> None:
        if not isinstance(version_id, str) or not version_id.strip():
            raise ValueError("exact object version is required")
        self.client.delete_object(
            Bucket=self.bucket,
            Key=_validate_key(key),
            VersionId=version_id,
        )

    def public_url(self, *, key: str) -> str:
        if not self.public_base_url:
            raise ValueError("public object base URL is not configured")
        parsed = urlparse(self.public_base_url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("public object base URL must be HTTPS")
        encoded_key = quote(_validate_key(key), safe="/")
        return f"{self.public_base_url}/{encoded_key}"

    def put_public_bytes_exact(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        checksum_sha256: str,
    ) -> tuple[ObjectInfo, str]:
        info = self.put_bytes(
            key=key,
            data=data,
            content_type=content_type,
            checksum_sha256=checksum_sha256,
            metadata={"delivery": "public"},
        )
        if not info.version_id:
            raise ValueError("public delivery requires an exact object version")
        verified, public_url = self.verify_public_bytes_exact(
            key=key,
            version_id=info.version_id,
            checksum_sha256=checksum_sha256,
            expected_size=len(data),
            content_type=content_type,
        )
        return verified, public_url

    def verify_public_bytes_exact(
        self,
        *,
        key: str,
        version_id: str,
        checksum_sha256: str,
        expected_size: int,
        content_type: str,
    ) -> tuple[ObjectInfo, str]:
        if not isinstance(version_id, str) or not version_id.strip():
            raise ValueError("public delivery requires an exact object version")
        verified = self.head(key=key, version_id=version_id)
        if (
            verified.version_id != version_id
            or verified.checksum_sha256 != checksum_sha256
            or verified.size != expected_size
            or verified.content_type.split(";", 1)[0].strip().lower()
            != content_type.lower()
        ):
            raise ValueError("public delivery object identity is not exact")
        public_url = self.public_url(key=key)
        host = urlparse(public_url).hostname
        response = safe_get(
            public_url,
            max_bytes=expected_size + 1,
            timeout=30.0,
            allowed_hosts={host} if host else set(),
            headers={"User-Agent": "WisdomeWriter/1.0"},
            max_elapsed_seconds=30.0,
        )
        observed_type = (
            response.headers.get("Content-Type", "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        if (
            response.status_code != 200
            or len(response.content) != expected_size
            or observed_type != content_type.lower()
            or hashlib.sha256(response.content).hexdigest()
            != checksum_sha256
        ):
            raise ValueError("public delivery URL does not expose the exact object")
        return verified, public_url

    def presign_get(
        self, *, key: str, version_id: str | None = None, expires_seconds: int | None = None
    ) -> str:
        params = {"Bucket": self.bucket, "Key": _validate_key(key)}
        if version_id:
            params["VersionId"] = version_id
        return self.client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_seconds or settings.OBJECT_STORAGE_PRESIGN_TTL_SECONDS,
        )


def content_addressed_key(*, namespace: str, checksum_sha256: str, filename: str) -> str:
    if len(checksum_sha256) != 64:
        raise ValueError("checksum must be a SHA-256 hex digest")
    safe_filename = PurePosixPath(filename).name
    return _validate_key(f"{namespace}/{checksum_sha256[:2]}/{checksum_sha256}/{safe_filename}")

