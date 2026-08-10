from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import httpx

from adapters.publishers.blogger.client import BloggerPublisher
from adapters.publishers.wordpress.client import WordPressPublisher
from apps.publishing import services
from apps.publishing.contracts import (
    PublishCommand,
    PublisherError,
    RenderedArticle,
    publication_content_marker,
)
from apps.publishing.models import Approval, PublicationAction


SHA_A = "a" * 64
SHA_B = "b" * 64


class PublicationMediaExecutionFenceTests(TestCase):
    def test_attempt_gate_requires_exact_ready_media_before_external_write(self):
        target = SimpleNamespace(
            id=uuid.uuid4(),
            current_snapshot_id=uuid.uuid4(),
            current_config_hash=SHA_A,
            publisher_adapter_manifest_hash=services.ADAPTER_MANIFESTS["wordpress"],
            channel="wordpress",
            connection_state="verified",
            environment="test",
        )
        command = {
            "targetId": str(target.id),
            "targetSnapshotId": str(target.current_snapshot_id),
            "targetConfigHash": target.current_config_hash,
            "resolvedAction": PublicationAction.CREATE,
            "targetCommandHash": SHA_B,
        }
        intent = SimpleNamespace(
            id=uuid.uuid4(),
            article_id=uuid.uuid4(),
            state="dispatched",
            approval_mode="manual",
            target_commands=[command],
        )
        approval = SimpleNamespace(
            id=uuid.uuid4(),
            decision=Approval.Decision.APPROVED,
            approval_subject_hash="c" * 64,
            target_action=PublicationAction.CREATE,
        )
        attempt = SimpleNamespace(
            publication_intent=intent,
            publication=SimpleNamespace(
                target=target,
                remote_lookup_key="ww-current",
            ),
            remote_lookup_key="ww-current",
            target_snapshot_id=target.current_snapshot_id,
            target_config_hash=target.current_config_hash,
            publisher_adapter_manifest_hash=target.publisher_adapter_manifest_hash,
            approval=approval,
            approval_subject_hash=approval.approval_subject_hash,
            resolved_action=PublicationAction.CREATE,
        )

        with (
            patch.object(services, "_require_attempt_origin_run_active"),
            patch.object(services, "_kill_switch_enabled", return_value=False),
            patch.object(
                services,
                "resolve_current_publication_intent",
                return_value=intent,
            ),
            patch.object(
                services,
                "_latest_approval_locked",
                return_value=approval,
            ),
            patch.object(
                services,
                "_approval_matches_frozen_subject",
                return_value=True,
            ),
            patch.object(
                services,
                "_validated_auto_live_eligible",
                return_value=True,
            ),
            patch.object(
                services,
                "require_publication_media_ready_locked",
            ) as require_media,
        ):
            services.validate_attempt_gate(attempt)

        require_media.assert_called_once_with(attempt=attempt)


def _rendered_article(*, channel_role: str = "primary_canonical") -> RenderedArticle:
    return RenderedArticle(
        article_id=str(uuid.UUID(int=1)),
        revision_no=3,
        channel_role=channel_role,
        render_stage="final",
        title="검증된 제목",
        body_html="<p>검증된 본문</p>",
        source_links=("https://source.example/item",),
        included_claim_ids=(str(uuid.UUID(int=2)),),
        canonical_link_state=(
            "resolved" if channel_role == "secondary_summary" else "not_applicable"
        ),
        canonical_source_url=(
            "https://primary.example/post" if channel_role == "secondary_summary" else None
        ),
        template_hash=SHA_A,
        content_hash=SHA_B,
        source_manifest_hash="c" * 64,
        labels=("policy",),
    )


def _command(
    *,
    action: str,
    remote_post_id: str | None = None,
    channel_role: str = "primary_canonical",
) -> PublishCommand:
    return PublishCommand(
        publication_attempt_id=str(uuid.UUID(int=10)),
        action=action,
        target_command_hash="d" * 64,
        idempotency_key="publication-attempt-10",
        remote_lookup_key="wisdome-publication-10",
        target_id=str(uuid.UUID(int=11)),
        publication_intent_id=str(uuid.UUID(int=12)),
        approval_id=str(uuid.UUID(int=13)),
        approval_subject_hash="e" * 64,
        target_snapshot_id=str(uuid.UUID(int=14)),
        target_config_hash="f" * 64,
        publisher_contract_version="publisher-v1",
        publisher_adapter_manifest_hash="1" * 64,
        remote_post_id=remote_post_id,
        rendered_article=(
            None
            if action == "unpublish"
            else _rendered_article(channel_role=channel_role)
        ),
    )


class MutatingFailureClassificationTests(TestCase):
    def test_wordpress_mutating_5xx_is_unknown_outcome_not_direct_retry(self):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(503, json={"code": "busy"})
            )
        )
        publisher = WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=client,
        )

        with self.assertRaises(PublisherError) as caught:
            publisher.execute(_command(action="update", remote_post_id="42"))

        self.assertEqual(caught.exception.category, "unknown_outcome")

    def test_blogger_mutating_5xx_is_unknown_outcome_not_direct_retry(self):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(503, json={"error": {"code": 503}})
            )
        )
        publisher = BloggerPublisher(
            blog_id="blog-7",
            access_token="not-a-secret",
            client=client,
        )

        with self.assertRaises(PublisherError) as caught:
            publisher.execute(
                _command(
                    action="update",
                    remote_post_id="post-42",
                    channel_role="secondary_summary",
                )
            )

        self.assertEqual(caught.exception.category, "unknown_outcome")


class RemoteReconcileContractTests(TestCase):
    def test_wordpress_create_projection_mismatch_never_posts(self):
        command = _command(action="create")
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "slug": command.remote_lookup_key,
                        "status": "draft",
                        "title": {"raw": command.rendered_article.title},
                        "content": {
                            "raw": (
                                f"{command.rendered_article.body_html}\n"
                                f"{publication_content_marker(command)}"
                            )
                        },
                    }
                ],
            )

        publisher = WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = publisher.execute(command)

        self.assertEqual(result.status, "manual_required")
        self.assertEqual(result.error_code, "remote_projection_mismatch")
        self.assertEqual(methods, ["GET"])

    def test_blogger_create_bounded_scan_failure_never_posts(self):
        command = _command(action="create", channel_role="secondary_summary")
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(200, json={"items": [], "nextPageToken": "more"})

        publisher = BloggerPublisher(
            blog_id="blog-7",
            access_token="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = publisher.execute(command)

        self.assertEqual(result.status, "manual_required")
        self.assertEqual(result.error_code, "remote_reconcile_page_limit_exceeded")
        self.assertNotIn("POST", methods)

    def test_wordpress_create_2xx_requires_exact_followup_projection(self):
        command = _command(action="create")
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET" and len(calls) == 1:
                return httpx.Response(200, json=[])
            if request.method == "POST":
                return httpx.Response(
                    201,
                    json={"id": 42, "status": "publish"},
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "status": "publish",
                        "title": {"raw": command.rendered_article.title},
                        "content": {
                            "raw": (
                                "<p>tampered</p>\n"
                                f"{publication_content_marker(command)}"
                            )
                        },
                    }
                ],
            )

        publisher = WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = publisher.execute(command)

        self.assertEqual(calls, ["GET", "POST", "GET"])
        self.assertNotEqual(result.status, "succeeded")
        self.assertEqual(result.error_code, "remote_projection_mismatch")

    def test_wordpress_mutation_2xx_invalid_json_is_unknown_not_success(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(
                201,
                content=b"not-json",
                headers={"Content-Type": "application/json"},
            )

        publisher = WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = publisher.execute(_command(action="create"))

        self.assertEqual(calls, ["GET", "POST"])
        self.assertEqual(result.status, "unknown_outcome")
        self.assertEqual(result.error_code, "remote_mutation_response_invalid")

    def test_blogger_update_2xx_requires_exact_followup_blog_and_body(self):
        command = _command(
            action="update",
            remote_post_id="post-42",
            channel_role="secondary_summary",
        )
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "PATCH":
                return httpx.Response(
                    200,
                    json={
                        "id": "post-42",
                        "blog": {"id": "blog-7"},
                        "status": "LIVE",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "post-42",
                    "blog": {"id": "another-blog"},
                    "status": "LIVE",
                    "title": command.rendered_article.title,
                    "content": (
                        f"{command.rendered_article.body_html}\n"
                        f"{publication_content_marker(command)}"
                    ),
                },
            )

        publisher = BloggerPublisher(
            blog_id="blog-7",
            access_token="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = publisher.execute(command)

        self.assertEqual(calls, ["PATCH", "GET"])
        self.assertNotEqual(result.status, "succeeded")
        self.assertEqual(result.error_code, "remote_projection_mismatch")

    def test_retry_after_imf_fixdate_reaches_publisher_error(self):
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=300)
        response = httpx.Response(
            429,
            headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
        )
        publisher = WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=httpx.Client(
                transport=httpx.MockTransport(lambda _request: response)
            ),
        )

        with self.assertRaises(PublisherError) as caught:
            publisher.reconcile(_command(action="create"))

        self.assertIsNotNone(caught.exception.retry_after_seconds)
        self.assertGreaterEqual(caught.exception.retry_after_seconds, 295)
        self.assertLessEqual(caught.exception.retry_after_seconds, 300)

    def test_wordpress_update_reconcile_reads_the_exact_remote_post_id(self):
        observed_paths: list[str] = []
        command = _command(action="update", remote_post_id="42")

        def handler(request: httpx.Request) -> httpx.Response:
            observed_paths.append(request.url.path)
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "slug": "wisdome-publication-10",
                    "status": "publish",
                    "title": {"raw": "검증된 제목"},
                    "content": {
                        "raw": (
                            "<p>검증된 본문</p>\n"
                            f"{publication_content_marker(command)}"
                        )
                    },
                    "link": "https://wordpress.example/post",
                    "modified_gmt": "2026-08-09T00:00:00",
                },
            )

        publisher = WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = publisher.reconcile(command)

        self.assertEqual(observed_paths, ["/wp-json/wp/v2/posts/42"])
        self.assertEqual(result.status, "succeeded")

    def test_blogger_unpublish_reconcile_requires_exact_post_draft_state(self):
        observed_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed_paths.append(request.url.path)
            return httpx.Response(
                200,
                json={
                    "kind": "blogger#post",
                    "id": "post-42",
                    "blog": {"id": "blog-7"},
                    "status": "DRAFT",
                    "content": "<p>old body</p>",
                    "labels": ["wisdome-wisdome-publication-10"],
                    "updated": "2026-08-09T00:00:00Z",
                },
            )

        publisher = BloggerPublisher(
            blog_id="blog-7",
            access_token="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = publisher.reconcile(
            _command(
                action="unpublish",
                remote_post_id="post-42",
                channel_role="secondary_summary",
            )
        )

        self.assertEqual(
            observed_paths,
            ["/blogger/v3/blogs/blog-7/posts/post-42"],
        )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.remote_state, "draft")

    def test_wordpress_create_persists_a_versioned_content_marker(self):
        posted_content: list[str] = []
        command = _command(action="create")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                if not posted_content:
                    return httpx.Response(200, json=[])
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 42,
                            "slug": "wisdome-publication-10",
                            "status": "publish",
                            "title": {"raw": command.rendered_article.title},
                            "content": {"raw": posted_content[0]},
                            "link": "https://wordpress.example/post",
                        }
                    ],
                )
            posted_content.append(str(httpx.QueryParams(request.content.decode())))
            payload = __import__("json").loads(request.content)
            posted_content[-1] = payload["content"]
            return httpx.Response(
                201,
                json={
                    "id": 42,
                    "slug": "wisdome-publication-10",
                    "status": "publish",
                    "link": "https://wordpress.example/post",
                },
            )

        publisher = WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = publisher.execute(command)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(posted_content), 1)
        self.assertIn("<!--wisdome-publication-v1:", posted_content[0])
        self.assertIn("wisdome-publication-10", posted_content[0])

    def test_create_reconcile_not_found_is_retryable_before_bounded_manualization(self):
        wordpress = WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json=[])
                )
            ),
        )

        result = wordpress.reconcile(_command(action="create"))

        self.assertEqual(result.status, "retryable_failed")
        self.assertEqual(result.error_code, "remote_match_not_found")

    def test_wordpress_create_reconcile_rejects_a_draft_even_with_the_exact_marker(self):
        command = _command(action="create")
        expected_body = (
            f"{command.rendered_article.body_html}\n"
            f"{publication_content_marker(command)}"
        )
        publisher = WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200,
                        json=[
                            {
                                "id": 42,
                                "status": "draft",
                                "title": {"raw": command.rendered_article.title},
                                "content": {"raw": expected_body},
                                "link": "https://wordpress.example/post",
                            }
                        ],
                    )
                )
            ),
        )

        result = publisher.reconcile(command)

        self.assertEqual(result.status, "manual_required")
        self.assertEqual(result.error_code, "remote_projection_mismatch")

    def test_wordpress_update_reconcile_rejects_tampered_body_with_valid_marker(self):
        command = _command(action="update", remote_post_id="42")
        publisher = WordPressPublisher(
            base_url="https://wordpress.example",
            username="worker",
            application_password="not-a-secret",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200,
                        json={
                            "id": 42,
                            "status": "publish",
                            "title": {"raw": command.rendered_article.title},
                            "content": {
                                "raw": (
                                    "<p>tampered body</p>\n"
                                    f"{publication_content_marker(command)}"
                                )
                            },
                            "link": "https://wordpress.example/post",
                        },
                    )
                )
            ),
        )

        result = publisher.reconcile(command)

        self.assertEqual(result.status, "manual_required")
        self.assertEqual(result.error_code, "remote_projection_mismatch")

    def test_blogger_create_reconcile_requires_live_exact_body_and_blog_identity(self):
        command = _command(action="create", channel_role="secondary_summary")
        marker = publication_content_marker(command)
        responses = (
            {
                "items": [
                    {
                        "id": "post-42",
                        "blog": {"id": "another-blog"},
                        "status": "DRAFT",
                        "title": command.rendered_article.title,
                        "content": f"<p>tampered</p>\n{marker}",
                        "labels": ["wisdome-wisdome-publication-10"],
                    }
                ]
            },
            {"items": []},
            {"items": []},
        )
        response_iter = iter(responses)
        publisher = BloggerPublisher(
            blog_id="blog-7",
            access_token="not-a-secret",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json=next(response_iter))
                )
            ),
        )

        result = publisher.reconcile(command)

        self.assertEqual(result.status, "manual_required")
        self.assertEqual(result.error_code, "remote_projection_mismatch")

    def test_blogger_reconcile_wall_deadline_is_fail_closed_across_all_states(self):
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            return httpx.Response(200, json={"items": []})

        publisher = BloggerPublisher(
            blog_id="blog-7",
            access_token="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            monotonic_clock=MagicMock(side_effect=(0.0, 31.0)),
        )

        result = publisher.reconcile(
            _command(action="create", channel_role="secondary_summary")
        )

        self.assertEqual(result.status, "manual_required")
        self.assertEqual(result.error_code, "remote_reconcile_deadline_exceeded")
        self.assertEqual(requests, [])

    def test_blogger_reconcile_checks_the_deadline_after_the_final_response(self):
        command = _command(action="create", channel_role="secondary_summary")
        expected_body = (
            f"{command.rendered_article.body_html}\n"
            f"{publication_content_marker(command)}"
        )
        now = [0.0]

        def handler(request: httpx.Request) -> httpx.Response:
            state = request.url.params["status"]
            if state != "scheduled":
                return httpx.Response(200, json={"items": []})
            now[0] = 31.0
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "post-42",
                            "blog": {"id": "blog-7"},
                            "status": "LIVE",
                            "title": command.rendered_article.title,
                            "content": expected_body,
                            "labels": ["wisdome-wisdome-publication-10"],
                        }
                    ]
                },
            )

        publisher = BloggerPublisher(
            blog_id="blog-7",
            access_token="not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            monotonic_clock=lambda: now[0],
        )

        result = publisher.reconcile(command)

        self.assertEqual(result.status, "manual_required")
        self.assertEqual(result.error_code, "remote_reconcile_deadline_exceeded")
