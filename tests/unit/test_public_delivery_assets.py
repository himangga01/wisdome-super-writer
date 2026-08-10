from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import httpx

from adapters.storage import S3ObjectStorage
from adapters.storage.base import ObjectInfo
from apps.publishing import media_delivery
from apps.publishing.models import MediaDeliveryOperation
from apps.publishing.services import MediaDeliveryOperationFence


class _VersionedS3Client:
    def __init__(self, content: bytes):
        self.content = content
        self.deleted = None

    def put_object(self, **kwargs):
        self.content = bytes(kwargs["Body"])
        return {"VersionId": "version-7", "ETag": '"etag-7"'}

    def head_object(self, **kwargs):
        return {
            "VersionId": kwargs.get("VersionId") or "version-7",
            "ContentLength": len(self.content),
            "ContentType": "image/png",
            "Metadata": {"sha256": hashlib.sha256(self.content).hexdigest()},
            "ETag": '"etag-7"',
        }

    def delete_object(self, **kwargs):
        self.deleted = kwargs
        return {}


class PublicDeliveryAssetAdapterTests(TestCase):
    def test_prepare_requires_exact_version_head_and_anonymous_bytes(self):
        content = b"public-image-bytes"
        checksum = hashlib.sha256(content).hexdigest()
        storage = S3ObjectStorage(
            bucket="published",
            client=_VersionedS3Client(content),
            public_base_url="https://assets.example",
        )
        with patch(
            "adapters.storage.s3.safe_get",
            return_value=httpx.Response(
                200,
                content=content,
                headers={"Content-Type": "image/png"},
            ),
        ):
            info, public_url = storage.put_public_bytes_exact(
                key="published-assets/aa/image.png",
                data=content,
                content_type="image/png",
                checksum_sha256=checksum,
            )

        self.assertEqual(info.version_id, "version-7")
        self.assertEqual(info.checksum_sha256, checksum)
        self.assertEqual(public_url, "https://assets.example/published-assets/aa/image.png")

    def test_delete_exact_version_rejects_empty_version_and_passes_exact_id(self):
        client = _VersionedS3Client(b"data")
        storage = S3ObjectStorage(bucket="published", client=client)

        with self.assertRaises(ValueError):
            storage.delete_exact_version(
                key="published-assets/aa/image.png",
                version_id="",
            )
        storage.delete_exact_version(
            key="published-assets/aa/image.png",
            version_id="version-7",
        )

        self.assertEqual(
            client.deleted,
            {
                "Bucket": "published",
                "Key": "published-assets/aa/image.png",
                "VersionId": "version-7",
            },
        )

    def test_reconcile_rejects_public_body_that_differs_from_exact_version(self):
        content = b"public-image-bytes"
        checksum = hashlib.sha256(content).hexdigest()
        storage = S3ObjectStorage(
            bucket="published",
            client=_VersionedS3Client(content),
            public_base_url="https://assets.example",
        )
        with patch(
            "adapters.storage.s3.safe_get",
            return_value=httpx.Response(
                200,
                content=b"tampered-public-image",
                headers={"Content-Type": "image/png"},
            ),
        ):
            with self.assertRaises(ValueError):
                storage.verify_public_bytes_exact(
                    key="published-assets/aa/image.png",
                    version_id="version-7",
                    checksum_sha256=checksum,
                    expected_size=len(content),
                    content_type="image/png",
                )

    def test_prepare_operation_uses_frozen_bytes_and_marks_write_once(self):
        content = b"public-image-bytes"
        checksum = hashlib.sha256(content).hexdigest()
        operation_id = uuid.uuid4()
        mapping_id = uuid.uuid4()
        event_id = uuid.uuid4()
        material = media_delivery.FrozenMediaMaterial(
            object_key="source/image.png",
            object_version="source-v1",
            checksum=checksum,
            presentation_hash="b" * 64,
            mime_type="image/png",
            byte_size=len(content),
            alt_text="검증된 이미지",
            caption="출처 표기",
        )
        mapping = SimpleNamespace(
            id=mapping_id,
            delivery_object_key="published/image.png",
            delivery_object_version="",
        )
        operation = SimpleNamespace(
            id=operation_id,
            generation=1,
            action=MediaDeliveryOperation.Action.PREPARE,
            remote_media_id=None,
            public_delivery_asset_id=mapping_id,
            public_delivery_asset=mapping,
        )
        fence = MediaDeliveryOperationFence(
            operation_id=operation_id,
            mapping_kind=MediaDeliveryOperation.MappingKind.PUBLIC_DELIVERY_ASSET,
            mapping_id=mapping_id,
            action=MediaDeliveryOperation.Action.PREPARE,
            generation=1,
            source_event_id=event_id,
            consumer_name="delivery-prepare-v2",
            consumer_lease_generation=1,
            lease_token_hash="c" * 64,
            lease_expires_at=None,
        )

        class Storage:
            def get_bounded_bytes(self, **_kwargs):
                return content

            def put_public_bytes_exact(self, **kwargs):
                self.put_kwargs = kwargs
                return (
                    ObjectInfo(
                        key=kwargs["key"],
                        version_id="delivery-v1",
                        checksum_sha256=checksum,
                        size=len(content),
                        content_type="image/png",
                    ),
                    "https://assets.example/published/image.png",
                )

        writes: list[str] = []
        storage = Storage()
        with patch.object(
            media_delivery,
            "_binding_material",
            return_value=material,
        ):
            result = media_delivery.perform_media_delivery_operation(
                operation,
                fence=fence,
                write_guard=lambda: writes.append("authorized"),
                storage=storage,
            )

        self.assertEqual(writes, ["authorized"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["object_version"], "delivery-v1")
        self.assertEqual(result["asset_checksum"], checksum)


if __name__ == "__main__":
    import unittest

    unittest.main()
