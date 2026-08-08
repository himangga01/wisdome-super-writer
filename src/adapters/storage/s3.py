import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

import boto3
from botocore.config import Config
from django.conf import settings

from .base import ObjectInfo


def _validate_key(key: str) -> str:
    path = PurePosixPath(key)
    if not key or key.startswith("/") or ".." in path.parts:
        raise ValueError("object key must be a non-empty relative path without parent traversal")
    return str(path)


class S3ObjectStorage:
    def __init__(self, *, bucket: str | None = None, client=None):
        self.bucket = bucket or settings.AWS_STORAGE_BUCKET_NAME
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=settings.AWS_S3_ENDPOINT_URL,
                region_name=settings.AWS_S3_REGION_NAME,
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
                config=Config(s3={"addressing_style": settings.AWS_S3_ADDRESSING_STYLE}),
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
        params = {"Bucket": self.bucket, "Key": _validate_key(key)}
        if version_id:
            params["VersionId"] = version_id
        response = self.client.get_object(**params)
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        try:
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
            or response.get("ContentLength", size) != size
            or (
                version_id
                and response.get("VersionId")
                and response.get("VersionId") != version_id
            )
        ):
            destination.unlink(missing_ok=True)
            raise ValueError("downloaded object identity differs from provenance")
        destination.chmod(0o400)
        return ObjectInfo(
            key=params["Key"],
            version_id=response.get("VersionId") or version_id,
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

