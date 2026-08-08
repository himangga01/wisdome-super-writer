from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from adapters.storage import S3ObjectStorage


class _StreamingClient:
    def __init__(self) -> None:
        self.body_type = None
        self.uploaded = b""

    def put_object(self, **kwargs):
        body = kwargs["Body"]
        self.body_type = type(body)
        chunks = []
        while True:
            chunk = body.read(7)
            if not chunk:
                break
            chunks.append(chunk)
        self.uploaded = b"".join(chunks)
        return {"VersionId": "stream-version", "ETag": '"stream-etag"'}

    def get_object(self, **kwargs):
        del kwargs
        return {
            "Body": BytesIO(self.uploaded),
            "VersionId": "stream-version",
            "ContentType": "application/pdf",
            "ContentLength": len(self.uploaded),
            "Metadata": {"sha256": hashlib.sha256(self.uploaded).hexdigest()},
            "ETag": '"stream-etag"',
        }


class S3StreamingUploadTests(unittest.TestCase):
    def test_put_file_hashes_and_uploads_from_a_bounded_file_stream(self) -> None:
        client = _StreamingClient()
        data = b"%PDF-1.7\n" + b"x" * 4096
        checksum = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "converted.pdf"
            path.write_bytes(data)
            info = S3ObjectStorage(bucket="test", client=client).put_file(
                key="evidence/converted/test.pdf",
                path=path,
                content_type="application/pdf",
                checksum_sha256=checksum,
                expected_size=len(data),
            )

        self.assertNotEqual(client.body_type, bytes)
        self.assertEqual(client.uploaded, data)
        self.assertEqual(info.checksum_sha256, checksum)
        self.assertEqual(info.size, len(data))
        self.assertEqual(info.version_id, "stream-version")

    def test_get_file_streams_and_binds_downloaded_checksum_and_size(self) -> None:
        client = _StreamingClient()
        client.uploaded = b"legacy-hwp-input" * 256
        checksum = hashlib.sha256(client.uploaded).hexdigest()
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "input.hwp"
            info = S3ObjectStorage(bucket="test", client=client).get_file(
                key="evidence/raw/input.hwp",
                version_id="stream-version",
                destination=destination,
                expected_checksum_sha256=checksum,
                expected_size=len(client.uploaded),
            )
            self.assertEqual(destination.read_bytes(), client.uploaded)
        self.assertEqual(info.checksum_sha256, checksum)
        self.assertEqual(info.size, len(client.uploaded))


if __name__ == "__main__":
    unittest.main()
