from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve
from jsonschema.exceptions import ValidationError

from apps.publishing import api, services
from apps.publishing.models import PublicationAttempt
from wisdome_writer.api.openapi import _compile_schema, load_openapi_contract


ARTICLE_ID = "11111111-1111-4111-8111-111111111111"
TARGET_ID = "22222222-2222-4222-8222-222222222222"
INTENT_ID = "33333333-3333-4333-8333-333333333333"
CORRELATION_ID = "44444444-4444-4444-8444-444444444444"
SNAPSHOT_ID = "55555555-5555-4555-8555-555555555555"
PUBLICATION_ID = "66666666-6666-4666-8666-666666666666"
HASH_A = "a" * 64


def _validator(schema_name: str):
    contract = load_openapi_contract()
    _, validator = _compile_schema(
        contract["components"]["schemas"][schema_name],
        document=contract,
        subject=schema_name,
    )
    return validator


def _staff_request(path: str):
    request = RequestFactory().get(path, HTTP_X_CSRFTOKEN="test-token")
    request._dont_enforce_csrf_checks = True
    request.correlation_id = uuid.UUID(CORRELATION_ID)
    request.user = SimpleNamespace(
        pk=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        is_authenticated=True,
        is_active=True,
        is_staff=True,
    )
    return request


class PublishingAdminContractTests(SimpleTestCase):
    maxDiff = None

    def test_openapi_covers_every_publishing_console_api_operation(self):
        contract = load_openapi_contract()
        expected = {
            ("/targets/{targetId}", "get"): "getPublicationTarget",
            ("/articles/{articleId}/publication-intents", "get"): "getCurrentPublicationIntent",
            ("/articles/{articleId}/preview", "get"): "previewArticle",
            ("/articles/{articleId}/approvals", "get"): "listArticleApprovals",
            ("/articles/{articleId}/publications", "get"): "listArticlePublications",
            ("/publications/{publicationId}/attempts", "get"): "listPublicationAttempts",
            ("/publication-attempts/{attemptId}/retry", "post"): "retryPublicationAttempt",
            ("/corrections/{correctionId}/prepare-publication", "post"): "prepareCorrectionPublication",
        }

        for (path, method), operation_id in expected.items():
            with self.subTest(path=path, method=method):
                self.assertEqual(
                    contract["paths"][path][method]["operationId"],
                    operation_id,
                )

        schemas = contract["components"]["schemas"]
        for name in (
            "CurrentPublicationIntentResult",
            "ApprovalPage",
            "PublicationPage",
            "PublicationAttemptPage",
            "PublicationAttemptActionResult",
            "PublicationAttemptRetryRequest",
        ):
            with self.subTest(schema=name):
                self.assertFalse(schemas[name]["additionalProperties"])

    def test_current_intent_get_is_openapi_bound(self):
        request = _staff_request(
            f"/api/v1/articles/{ARTICLE_ID}/publication-intents"
        )
        row = SimpleNamespace(id=uuid.UUID(INTENT_ID))
        with (
            patch.object(api, "resolve_current_publication_intent", return_value=row),
            patch.object(api, "intent_json", return_value={"id": INTENT_ID}),
        ):
            response = api.publication_intents(request, article_id=ARTICLE_ID)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.openapi_operation_id, "getCurrentPublicationIntent")
        self.assertEqual(json.loads(response.content), {"item": {"id": INTENT_ID}})

    def test_preview_get_is_openapi_bound(self):
        request = _staff_request(
            f"/api/v1/articles/{ARTICLE_ID}/preview?targetId={TARGET_ID}"
        )
        with (
            patch.object(api, "get_article_preview", return_value=SimpleNamespace()),
            patch.object(api, "render_json", return_value={"id": "render"}),
        ):
            response = api.article_preview(request, article_id=ARTICLE_ID)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.openapi_operation_id, "previewArticle")

    def test_route_names_cover_history_and_retry_endpoints(self):
        routes = {
            f"/api/v1/articles/{ARTICLE_ID}/approvals": "approvals",
            f"/api/v1/articles/{ARTICLE_ID}/publications": "article-publications",
            f"/api/v1/publications/{TARGET_ID}/attempts": "publication-attempts",
            f"/api/v1/publication-attempts/{TARGET_ID}/retry": "retry-publication-attempt",
        }
        for path, expected_name in routes.items():
            with self.subTest(path=path):
                self.assertEqual(resolve(path).url_name, expected_name)

    def test_accepted_job_response_is_exact_and_traceable(self):
        row = SimpleNamespace(
            id=uuid.UUID(TARGET_ID),
            correlation_id=uuid.UUID(CORRELATION_ID),
            created_at=SimpleNamespace(isoformat=lambda: "2026-08-11T00:00:00+00:00"),
        )
        payload = api.accepted_job_json(row)
        self.assertEqual(
            payload,
            {
                "jobId": TARGET_ID,
                "correlationId": CORRELATION_ID,
                "acceptedAt": "2026-08-11T00:00:00+00:00",
            },
        )
        _validator("AcceptedJob").validate(payload)

    def test_preview_publication_and_attempt_serializers_match_openapi(self):
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        preview = SimpleNamespace(
            id=uuid.UUID(INTENT_ID),
            target_id=uuid.UUID(TARGET_ID),
            target_snapshot_id=uuid.UUID(SNAPSHOT_ID),
            target_config_hash=HASH_A,
            channel_role="primary_canonical",
            render_stage="preview",
            title="검증된 미리보기",
            body_html="<p>본문</p>",
            source_links=["https://example.com/source"],
            included_claim_ids=[ARTICLE_ID],
            canonical_source_url=None,
            canonical_link_state="not_applicable",
            template_hash=HASH_A,
            content_hash=HASH_A,
            source_manifest_hash=HASH_A,
        )
        publication = SimpleNamespace(
            id=uuid.UUID(PUBLICATION_ID),
            article_id=uuid.UUID(ARTICLE_ID),
            target_id=uuid.UUID(TARGET_ID),
            target=SimpleNamespace(channel="wordpress", role="primary_canonical"),
            state="published",
            remote_state="published",
            remote_post_id="post-1",
            remote_url="https://example.com/post-1",
            canonical_source_url=None,
            scheduled_for=None,
            canonical_ready_at=now,
            published_at=now,
            published_revision_no=3,
            last_error_code="",
        )
        attempt = SimpleNamespace(
            id=uuid.UUID(INTENT_ID),
            publication_id=uuid.UUID(PUBLICATION_ID),
            publication_intent_id=uuid.UUID(INTENT_ID),
            publication=SimpleNamespace(target_id=uuid.UUID(TARGET_ID)),
            state="succeeded",
            resolved_action="create",
            attempt_no=1,
            execution_generation=1,
            reconcile_attempt_no=0,
            recovery_state="not_required",
            error_code="",
            http_status=201,
            started_at=now,
            finished_at=now,
            created_at=now,
        )

        _validator("ChannelPreview").validate(api.render_json(preview))
        _validator("Publication").validate(api.publication_json(publication))
        _validator("PublicationAttempt").validate(
            api.publication_attempt_json(attempt)
        )

    def test_reauthentication_contract_includes_approval_revoke(self):
        contract = load_openapi_contract()
        scopes = contract["components"]["schemas"]["ReauthenticationRequest"][
            "properties"
        ]["actionScopes"]["items"]["enum"]
        self.assertIn("approval_revoke", scopes)

    def test_manual_attempt_retry_requires_purpose_bound_reauthentication(self):
        validator = _validator("PublicationAttemptRetryRequest")
        payload = {
            "requestKey": "attempt-retry-0001",
            "reauthProofId": TARGET_ID,
            "reason": "Retry the existing logical attempt safely.",
        }
        validator.validate(payload)
        without_proof = dict(payload)
        without_proof.pop("reauthProofId")
        with self.assertRaises(ValidationError):
            validator.validate(without_proof)

    def test_retry_service_consumes_bulk_retry_proof_before_mutation(self):
        attempt = SimpleNamespace(
            id=uuid.UUID(INTENT_ID),
            state=PublicationAttempt.State.RETRYABLE_FAILED,
            attempt_no=1,
            publication=SimpleNamespace(target_id=uuid.UUID(TARGET_ID)),
            correlation_id=uuid.UUID(CORRELATION_ID),
            started_at=None,
            finished_at=None,
            next_retry_at=None,
            recovery_state="automatic_retry",
            next_recovery_at=None,
            error_detail_redacted="",
            save=MagicMock(),
        )
        query = MagicMock()
        query.select_related.return_value.get.return_value = attempt
        audit_context = SimpleNamespace(
            actor_type="admin",
            request_key="attempt-retry-0001",
            correlation_id=uuid.UUID(CORRELATION_ID),
        )
        request = SimpleNamespace(user=SimpleNamespace(pk=uuid.UUID(TARGET_ID)))

        with (
            patch.object(
                services.PublicationAttempt.objects,
                "select_for_update",
                return_value=query,
            ),
            patch.object(services, "_require_audit_actor"),
            patch.object(services, "_admin_request_hash", return_value=HASH_A),
            patch.object(services, "_has_request_audit", return_value=False),
            patch.object(services, "_has_valid_retry_reservation", return_value=True),
            patch.object(services, "_audit_state", return_value={}),
            patch.object(services, "_enqueue_event"),
            patch.object(services, "_record_publishing_audit"),
            patch.object(services, "consume_reauthentication_proof") as consume,
        ):
            observed, action = services.retry_publication_attempt.__wrapped__(
                str(attempt.id),
                request=request,
                reauth_proof_id=TARGET_ID,
                audit_context=audit_context,
            )

        self.assertIs(observed, attempt)
        self.assertEqual(action, "retry")
        consume.assert_called_once_with(
            request=request,
            proof_id=TARGET_ID,
            action_scope="bulk_retry",
            entity_type="publication_attempt",
            entity_id=attempt.id,
        )

    def test_console_scripts_use_current_head_and_server_owned_canary_material(self):
        root = Path(settings.REPOSITORY_ROOT)
        target_template = (
            root / "src/templates/admin_console/publishing/target_detail.html"
        ).read_text(encoding="utf-8")
        article_template = (
            root / "src/templates/admin_console/publishing/article_publish.html"
        ).read_text(encoding="utf-8")
        target_script = (
            root / "src/static/admin_console/publishing_target.js"
        ).read_text(encoding="utf-8")
        article_script = (
            root / "src/static/admin_console/publishing_article.js"
        ).read_text(encoding="utf-8")

        self.assertNotIn("policyVersion", target_template + target_script)
        self.assertNotIn("intent.approvals", article_template + article_script)
        self.assertNotIn("innerHTML", target_script + article_script)
        self.assertIn("expectedHeadVersion", article_script)
        self.assertIn("decisionReason", article_script)
        self.assertIn("approval_revoke", article_script)
        self.assertIn("bulk_retry", article_script)
        self.assertIn("/auth/reauth", target_script)
        self.assertIn("/auth/reauth", article_script)
