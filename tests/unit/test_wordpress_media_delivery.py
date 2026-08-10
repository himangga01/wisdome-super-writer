from __future__ import annotations

import hashlib
from unittest import TestCase
from unittest.mock import patch

import httpx

from adapters.publishers.wordpress.client import WordPressPublisher


class WordPressExactMediaTests(TestCase):
    def _publisher(self, handler):
        return WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_zero_match_is_explicit_not_found_without_mutation(self):
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(200, json=[])

        result = self._publisher(handler).find_media(
            remote_lookup_key="ww-media-1",
            description_marker="wisdome-media-v1:abc",
            expected_alt_text="검증된 이미지",
            expected_caption="출처 표기",
            expected_checksum="a" * 64,
            expected_mime_type="image/png",
        )

        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.error_code, "remote_match_not_found")
        self.assertEqual(methods, ["GET"])

    def test_one_match_requires_exact_metadata_and_downloaded_bytes(self):
        content = b"exact-image-bytes"
        checksum = hashlib.sha256(content).hexdigest()

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "slug": "ww-media-42",
                        "source_url": "https://wordpress.example/media/42.png",
                        "mime_type": "image/png",
                        "alt_text": "검증된 이미지",
                        "caption": {"raw": "출처 표기"},
                        "description": {"raw": "wisdome-media-v1:exact"},
                    }
                ],
            )

        with patch(
            "adapters.publishers.wordpress.client.safe_get",
            return_value=httpx.Response(
                200,
                content=content,
                headers={"Content-Type": "image/png"},
            ),
        ):
            result = self._publisher(handler).find_media(
                remote_lookup_key="ww-media-42",
                description_marker="wisdome-media-v1:exact",
                expected_alt_text="검증된 이미지",
                expected_caption="출처 표기",
                expected_checksum=checksum,
                expected_mime_type="image/png",
            )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.remote_post_id, "42")

    def test_marker_preserving_body_tamper_is_manual_required(self):
        expected = b"expected-image"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "slug": "ww-media-42",
                        "source_url": "https://wordpress.example/media/42.png",
                        "mime_type": "image/png",
                        "alt_text": "검증된 이미지",
                        "caption": {"raw": "출처 표기"},
                        "description": {"raw": "wisdome-media-v1:exact"},
                    }
                ],
            )

        with patch(
            "adapters.publishers.wordpress.client.safe_get",
            return_value=httpx.Response(
                200,
                content=b"tampered-image",
                headers={"Content-Type": "image/png"},
            ),
        ):
            result = self._publisher(handler).find_media(
                remote_lookup_key="ww-media-42",
                description_marker="wisdome-media-v1:exact",
                expected_alt_text="검증된 이미지",
                expected_caption="출처 표기",
                expected_checksum=hashlib.sha256(expected).hexdigest(),
                expected_mime_type="image/png",
            )

        self.assertEqual(result.status, "manual_required")
        self.assertEqual(result.error_code, "remote_media_material_mismatch")

    def test_upload_is_available_only_after_authenticated_and_public_proof(self):
        content = b"exact-image-bytes"
        checksum = hashlib.sha256(content).hexdigest()
        methods: list[str] = []
        list_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal list_calls
            methods.append(request.method)
            if request.method == "GET" and request.url.path.endswith("/media"):
                list_calls += 1
                if list_calls == 1:
                    return httpx.Response(200, json=[])
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 42,
                            "slug": "ww-media-42",
                            "source_url": "https://wordpress.example/media/42.png",
                            "mime_type": "image/png",
                            "alt_text": "검증된 이미지",
                            "caption": {"raw": "출처 표기"},
                            "description": {"raw": "wisdome-media-v1:exact"},
                        }
                    ],
                )
            if request.method == "POST" and request.url.path.endswith("/media"):
                return httpx.Response(
                    201,
                    json={
                        "id": 42,
                        "source_url": "https://wordpress.example/media/42.png",
                    },
                )
            return httpx.Response(200, json={"id": 42})

        writes: list[str] = []
        publisher = WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            write_guard=lambda: writes.append("write"),
        )
        with patch(
            "adapters.publishers.wordpress.client.safe_get",
            return_value=httpx.Response(
                200,
                content=content,
                headers={"Content-Type": "image/png"},
            ),
        ):
            result = publisher.upload_media(
                content=content,
                filename="image.png",
                mime_type="image/png",
                remote_lookup_key="ww-media-42",
                alt_text="검증된 이미지",
                caption="출처 표기",
                description_marker="wisdome-media-v1:exact",
            )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.remote_post_id, "42")
        self.assertEqual(writes, ["write", "write"])
        self.assertEqual(methods, ["GET", "POST", "POST", "GET"])
        self.assertEqual(checksum, hashlib.sha256(content).hexdigest())


if __name__ == "__main__":
    import unittest

    unittest.main()
