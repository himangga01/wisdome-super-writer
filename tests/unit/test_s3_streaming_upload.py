from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

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
    def test_failed_download_preserves_an_existing_destination(self) -> None:
        for response_version, response_size, exception in (
            ("frozen-v1", 5, FileExistsError),
            ("wrong-v2", 5, ValueError),
            ("frozen-v1", 99, ValueError),
        ):
            with self.subTest(version=response_version, size=response_size):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    destination = Path(temporary_directory) / "input.bin"
                    destination.write_bytes(b"caller-owned")
                    body = BytesIO(b"value")
                    client = Mock()
                    client.get_object.return_value = {
                        "Body": body,
                        "VersionId": response_version,
                        "ContentLength": response_size,
                    }
                    with self.assertRaises(exception):
                        S3ObjectStorage(bucket="test", client=client).get_file(
                            key="evidence/raw/input.bin",
                            version_id="frozen-v1",
                            destination=destination,
                            expected_checksum_sha256=hashlib.sha256(b"value").hexdigest(),
                            expected_size=5,
                        )
                    self.assertTrue(destination.exists())
                    self.assertEqual(destination.read_bytes(), b"caller-owned")
                    self.assertTrue(body.closed)

    def test_download_rejects_a_substituted_destination_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "input.bin"
            moved = Path(temporary_directory) / "download.bin"

            class ReplacingBody(BytesIO):
                def close(self):
                    if not self.closed:
                        destination.replace(moved)
                        destination.write_bytes(b"replacement-owned-by-another-writer")
                    super().close()

            body = ReplacingBody(b"value")
            client = Mock()
            client.get_object.return_value = {
                "Body": body,
                "VersionId": "frozen-v1",
                "ContentLength": 5,
            }
            with self.assertRaisesRegex(ValueError, "destination.*changed"):
                S3ObjectStorage(bucket="test", client=client).get_file(
                    key="evidence/raw/input.bin",
                    version_id="frozen-v1",
                    destination=destination,
                    expected_checksum_sha256=hashlib.sha256(b"value").hexdigest(),
                    expected_size=5,
                )
            self.assertEqual(destination.read_bytes(), b"replacement-owned-by-another-writer")
            self.assertEqual(moved.read_bytes(), b"value")

    def test_checksum_failure_removes_only_the_new_download(self) -> None:
        client = _StreamingClient()
        client.uploaded = b"value"
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "input.bin"
            with self.assertRaisesRegex(ValueError, "identity"):
                S3ObjectStorage(bucket="test", client=client).get_file(
                    key="evidence/raw/input.bin",
                    version_id="stream-version",
                    destination=destination,
                    expected_checksum_sha256="0" * 64,
                    expected_size=5,
                )
            self.assertFalse(destination.exists())

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
