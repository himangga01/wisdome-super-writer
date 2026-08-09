import json
import shutil
import subprocess
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from apps.editorial import api
from wisdome_writer.domain.errors import RequestValidationError, StaleVersion
from wisdome_writer.api.middleware import AdminApiSecurityMiddleware
from wisdome_writer.api.openapi import _compile_schema, load_openapi_contract
from wisdome_writer.infrastructure.event_routes import (
    EVENT_PAYLOAD_SCHEMAS,
    EVENT_ROUTES,
)


class Rows:
    def __init__(self, *rows):
        self.rows = list(rows)

    def all(self):
        return self

    def order_by(self, *args):
        return self

    def prefetch_related(self, *args):
        return self

    def select_related(self, *args):
        return self

    def __iter__(self):
        return iter(self.rows)


class EditorialAdminRuntimeTests(SimpleTestCase):
    maxDiff = None

    def test_editorial_api_rejects_authenticated_non_staff_before_view(self):
        request = RequestFactory().get("/api/v1/articles")
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_staff=False,
        )
        middleware = AdminApiSecurityMiddleware(
            lambda _request: self.fail("non-staff request reached editorial view")
        )

        response = middleware(request)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)["code"], "staff_required")

    def test_revise_route_forwards_canonical_material_and_returns_revalidation_identity(self):
        article_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
        revision_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        body_blocks = [
            {"id": "fact-1", "type": "fact", "content": "공급 일정입니다. [S1]"}
        ]
        claim_bindings = [
            {
                "claimRef": "fact-schedule",
                "blockId": "fact-1",
                "statement": "공급 일정입니다.",
                "claimType": "fact",
                "evidenceIds": ["33333333-3333-4333-8333-333333333333"],
                "citationMarker": "S1",
                "sourceSpans": {
                    "33333333-3333-4333-8333-333333333333": "공급 일정"
                },
                "semanticKey": None,
                "actor": None,
                "attribution": None,
                "horizon": None,
                "uncertaintyNote": None,
                "derivedFromClaimRefs": [],
            }
        ]
        request = RequestFactory().post(
            f"/api/v1/articles/{article_id}/revisions",
            data=json.dumps(
                {
                    "baseRevisionNo": 1,
                    "requestKey": "manual-edit-0001",
                    "title": "수정 제목",
                    "summary": "수정 요약",
                    "bodyBlocks": body_blocks,
                    "claimBindings": claim_bindings,
                    "editReason": "근거 연결 보완",
                },
                ensure_ascii=False,
            ),
            content_type="application/json",
        )
        request.user = SimpleNamespace(is_authenticated=True, pk=uuid.uuid4())
        revision = SimpleNamespace(
            id=revision_id,
            revision_no=2,
            claim_graph_state="queued",
            quality_state="pending",
            content_hash="a" * 64,
            evidence_manifest_hash="b" * 64,
            editorial_policy_hash="c" * 64,
            verification_manifest_hash="d" * 64,
            exclusion_manifest_hash="e" * 64,
        )
        audit_context = object()

        with (
            patch.object(api, "get_object_or_404"),
            patch.object(api.AuditContext, "for_admin", return_value=audit_context),
            patch.object(api, "create_manual_revision", return_value=(revision, True)) as create,
        ):
            response = api.revise_article(request, article_id=article_id)

        self.assertEqual(response.status_code, 201)
        response_payload = json.loads(response.content)
        self.assertEqual(
            response_payload,
            {
                "articleId": str(article_id),
                "revisionId": str(revision_id),
                "revisionNo": 2,
                "revalidationState": "queued",
                "qualityState": "pending",
                "revisionContentHash": "a" * 64,
                "inputEvidenceManifestHash": "b" * 64,
                "editorialPolicyHash": "c" * 64,
                "verificationManifestHash": "d" * 64,
                "excludedMaterialManifestHash": "e" * 64,
            },
        )
        contract = load_openapi_contract()
        _, validator = _compile_schema(
            contract["components"]["schemas"]["CreateRevisionResult"],
            document=contract,
            subject="CreateRevisionResult response",
        )
        validator.validate(response_payload)
        create.assert_called_once_with(
            article_id=article_id,
            base_revision_no=1,
            title="수정 제목",
            summary="수정 요약",
            body_blocks=body_blocks,
            claim_bindings=claim_bindings,
            user=request.user,
            audit_context=audit_context,
        )

    def test_revise_route_rejects_members_outside_exact_openapi_request(self):
        article_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
        request = self._manual_revision_request(article_id)
        request_payload = json.loads(request.body)
        request_payload["bodyMarkdown"] = "계약에 없는 파생 입력"
        request = self._manual_revision_request(article_id, **request_payload)

        revision = SimpleNamespace(
            id=uuid.uuid4(),
            revision_no=2,
            claim_graph_state="queued",
            quality_state="pending",
            content_hash="a" * 64,
            evidence_manifest_hash="b" * 64,
            editorial_policy_hash="c" * 64,
            verification_manifest_hash="d" * 64,
            exclusion_manifest_hash="e" * 64,
        )
        with (
            patch.object(api, "get_object_or_404"),
            patch.object(api.AuditContext, "for_admin", return_value=object()),
            patch.object(api, "create_manual_revision", return_value=(revision, True)),
            self.assertRaises(RequestValidationError),
        ):
            api.revise_article(request, article_id=article_id)

    def test_revise_route_maps_stale_base_revision_to_conflict(self):
        article_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
        request = self._manual_revision_request(article_id)

        with (
            patch.object(api, "get_object_or_404"),
            patch.object(api.AuditContext, "for_admin", return_value=object()),
            patch.object(
                api,
                "create_manual_revision",
                side_effect=ValueError("base_revision_no is stale"),
            ),
            self.assertRaises(StaleVersion),
        ):
            api.revise_article(request, article_id=article_id)

    def test_revise_route_returns_200_for_identical_request_key_replay(self):
        article_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
        request = self._manual_revision_request(article_id)
        revision = SimpleNamespace(
            id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
            revision_no=2,
            claim_graph_state="queued",
            quality_state="pending",
            content_hash="a" * 64,
            evidence_manifest_hash="b" * 64,
            editorial_policy_hash="c" * 64,
            verification_manifest_hash="d" * 64,
            exclusion_manifest_hash="e" * 64,
        )

        with (
            patch.object(api, "get_object_or_404"),
            patch.object(api.AuditContext, "for_admin", return_value=object()),
            patch.object(api, "create_manual_revision", return_value=(revision, False)),
        ):
            response = api.revise_article(request, article_id=article_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["revisionId"], str(revision.id))

    def test_policy_payload_never_fabricates_legacy_release_identity_hashes(self):
        snapshot = SimpleNamespace(
            id=uuid.UUID("77777777-7777-4777-8777-777777777777"),
            policy_key="legacy-unverified",
            topic_code="legacy",
            material_hash="8" * 64,
            release_document_hash="1" * 64,
            config_hash="2" * 64,
            implementation_manifest_hash="3" * 64,
            document={
                "schemaVersion": "editorial-policy-legacy-quarantine-v1",
                "policyKey": "legacy-unverified",
                "policyVersion": "legacy",
                "topicCode": "legacy",
                "runtimeEligible": False,
            },
        )

        payload = api._policy_payload(
            SimpleNamespace(
                editorial_policy_snapshot=snapshot,
                editorial_policy_version="legacy",
                editorial_policy_hash="8" * 64,
            )
        )

        self.assertIsNone(payload["releaseDocumentHash"])
        self.assertIsNone(payload["configHash"])
        self.assertIsNone(payload["implementationManifestHash"])
        contract = load_openapi_contract()
        _, validator = _compile_schema(
            contract["components"]["schemas"]["EditorialPolicyRef"],
            document=contract,
            subject="legacy EditorialPolicyRef response",
        )
        validator.validate(payload)

    def test_policy_payload_uses_exact_snapshot_identity_columns(self):
        snapshot = SimpleNamespace(
            id=uuid.UUID("77777777-7777-4777-8777-777777777777"),
            policy_key="housing-editorial",
            topic_code="housing_subscription",
            material_hash="8" * 64,
            release_document_hash="1" * 64,
            config_hash="2" * 64,
            implementation_manifest_hash="3" * 64,
            document={
                "policyKey": "housing-editorial",
                "policyVersion": "2.0.0",
                "topicCode": "housing_subscription",
            },
        )

        payload = api._policy_payload(
            SimpleNamespace(
                editorial_policy_snapshot=snapshot,
                editorial_policy_version="2.0.0",
                editorial_policy_hash="8" * 64,
            )
        )

        self.assertEqual(payload["releaseDocumentHash"], "1" * 64)
        self.assertEqual(payload["configHash"], "2" * 64)
        self.assertEqual(payload["implementationManifestHash"], "3" * 64)

    def _manual_revision_request(self, article_id, **overrides):
        payload = {
            "baseRevisionNo": 1,
            "requestKey": "manual-edit-0001",
            "title": "수정 제목",
            "summary": "수정 요약",
            "bodyBlocks": [
                {"id": "fact-1", "type": "fact", "content": "공급 일정입니다. [S1]"}
            ],
            "claimBindings": [
                {
                    "claimRef": "fact-schedule",
                    "blockId": "fact-1",
                    "statement": "공급 일정입니다.",
                    "claimType": "fact",
                    "evidenceIds": ["33333333-3333-4333-8333-333333333333"],
                    "citationMarker": "S1",
                    "sourceSpans": {
                        "33333333-3333-4333-8333-333333333333": "공급 일정"
                    },
                    "semanticKey": None,
                    "actor": None,
                    "attribution": None,
                    "horizon": None,
                    "uncertaintyNote": None,
                    "derivedFromClaimRefs": [],
                }
            ],
            "editReason": "근거 연결 보완",
        }
        payload.update(overrides)
        request = RequestFactory().post(
            f"/api/v1/articles/{article_id}/revisions",
            data=json.dumps(payload, ensure_ascii=False),
            content_type="application/json",
        )
        request.user = SimpleNamespace(is_authenticated=True, pk=uuid.uuid4())
        return request

    def test_article_detail_exposes_bounded_provenance_without_raw_source_text(self):
        now = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)
        article_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
        revision_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        evidence_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
        claim_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
        verification_id = uuid.UUID("55555555-5555-4555-8555-555555555555")
        cluster_id = uuid.UUID("66666666-6666-4666-8666-666666666666")
        policy_id = uuid.UUID("77777777-7777-4777-8777-777777777777")
        source_item_id = uuid.UUID("88888888-8888-4888-8888-888888888888")
        run_source_item_id = uuid.UUID("99999999-9999-4999-8999-999999999999")
        secret_source_text = "원문 비공개 " * 5000
        locator = {
            "locator_type": "structured_path",
            "path_type": "json_pointer",
            "path": "/supply/date",
        }
        source_item = SimpleNamespace(
            id=source_item_id,
            canonical_url="https://example.test/source",
            title="<img src=x onerror=alert(1)>",
            publisher="공식 기관 <script>alert(1)</script>",
            published_at=now,
            modified_at=None,
            first_collected_at=now,
            content_hash="1" * 64,
            source_version_hash="2" * 64,
            status="active",
        )
        evidence = SimpleNamespace(
            id=evidence_id,
            source_item_id=source_item_id,
            source_item=source_item,
            origin_run_source_item_id=run_source_item_id,
            origin_run_source_item=SimpleNamespace(
                run_id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                source_snapshot_id=uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            ),
            derivation_type="raw",
            kind="text",
            evidence_content_hash="3" * 64,
            checksum="4" * 64,
            locator_type="structured_path",
            locator=locator,
            extracted_text=secret_source_text,
            rights_status="attribution_required",
            rights_basis_url="https://example.test/rights",
            attribution_text="출처 표시",
            alt_text=None,
            confidence=None,
            confidence_detail=None,
            low_confidence_reasons=[],
            review_subject_schema_version="v1",
            review_subject_hash="5" * 64,
            latest_review_decision=None,
            manual_review_required=False,
            publishable=True,
            review_state="passed",
            document_extraction_id=None,
            extraction_run_id=None,
            generic_extraction_attempt_id=None,
        )
        frozen_evidence = {
            "evidenceId": str(evidence_id),
            "sourceItemId": str(source_item_id),
            "runSourceItemId": str(run_source_item_id),
            "sourceVersionHash": "f" * 64,
            "contentHash": "3" * 64,
            "checksum": "4" * 64,
            "reviewSubjectHash": "5" * 64,
            "locatorType": "structured_path",
            "locator": locator,
            "authorityTier": "primary_government",
            "independenceGroup": "official-source",
            "originIdentityHash": "6" * 64,
            "sourceTitle": source_item.title,
            "sourceUrl": source_item.canonical_url,
            "publisher": source_item.publisher,
            "publishedAt": now.isoformat(),
            "modifiedAt": "2026-08-08T03:00:00+00:00",
            "retrievedAt": "2026-08-07T03:00:00+00:00",
            "freshnessCutoff": "2026-08-08T03:00:00+00:00",
            "rightsStatus": "attribution_required",
            "rightsBasisUrl": "https://example.test/rights",
            "attributionText": "출처 표시",
            "altText": "고정된 대체 텍스트",
            "publishable": True,
            "sourceText": secret_source_text,
        }
        link = SimpleNamespace(
            evidence_id=evidence_id,
            evidence=evidence,
            relation="contradicts",
            source_span="공급 일정은 8월 10일입니다.",
            source_span_hash="7" * 64,
            verification_strength="direct",
            checked_at=now,
        )
        claim = SimpleNamespace(
            id=claim_id,
            block_id="fact-1",
            text="공급 일정은 8월 11일입니다.",
            claim_type="fact",
            risk_level="high",
            verification_state="conflicted",
            citation_marker="S1",
            evidence_links=Rows(link),
        )
        policy_document = {
            "policyKey": "housing-editorial",
            "policyVersion": "2.0.0",
            "topicCode": "housing_subscription",
        }
        policy_snapshot = SimpleNamespace(
            id=policy_id,
            policy_key="housing-editorial",
            policy_version="2.0.0",
            topic_code="housing_subscription",
            document=policy_document,
            material_hash="8" * 64,
            release_document_hash="1" * 64,
            config_hash="2" * 64,
            implementation_manifest_hash="3" * 64,
        )
        verification = SimpleNamespace(
            id=verification_id,
            cluster_id=cluster_id,
            decision="verified_notice",
            article_type="housing_notice",
            category="notice",
            policy_version="1",
            policy_hash="9" * 64,
            evidence_manifest_hash="a" * 64,
            rule_manifest_hash="b" * 64,
            result_manifest_hash="c" * 64,
            local_event_date=now.date(),
        )
        membership = SimpleNamespace(
            event_cluster_id=cluster_id,
            verification=verification,
            role="lead",
            display_order=0,
            inclusion_reason="주요 공고",
            cluster_snapshot_hash="a" * 64,
        )
        quality_check = SimpleNamespace(
            code="high_risk_verification_satisfied",
            check_version="1",
            result="failed",
            score=None,
            blocking=True,
            details={"claimIds": [str(claim_id)], "reason": "독립 근거 부족"},
            created_at=now,
        )
        visual = SimpleNamespace(
            id=uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            block_id="fact-1",
            source_evidence_id=evidence_id,
            visualization_id=None,
            display_order=0,
            locator_snapshot=locator,
            rights_status_snapshot="attribution_required",
            attribution_snapshot="출처 표시",
            alt_text_snapshot="공급 일정 표",
            caption="공급 일정",
            presentation_hash="d" * 64,
        )
        revision = SimpleNamespace(
            id=revision_id,
            revision_no=2,
            title="검토 대상 제목",
            summary="검토 대상 요약",
            body_blocks=[
                {"id": "fact-1", "type": "fact", "content": "공급 일정은 8월 11일입니다. [S1]"}
            ],
            body_markdown="공급 일정은 8월 11일입니다. [S1]",
            content_hash="e" * 64,
            provenance_kind="admin_edit",
            generation_attempt_id=None,
            generation_attempt=None,
            editorial_policy_snapshot=policy_snapshot,
            editorial_policy_snapshot_id=policy_id,
            editorial_policy_version="2.0.0",
            editorial_policy_hash="8" * 64,
            verification_manifest=[
                {
                    "verificationId": str(verification_id),
                    "role": "supporting",
                    "decision": "verified_notice",
                    "articleType": "housing_notice",
                    "category": "notice",
                    "evidenceManifestHash": "a" * 64,
                    "ruleManifestHash": "b" * 64,
                    "resultManifestHash": "c" * 64,
                    "policyVersion": "1",
                    "policyHash": "9" * 64,
                    "localEventDate": "2026-08-08",
                }
            ],
            verification_manifest_hash="f" * 64,
            evidence_manifest=[frozen_evidence],
            evidence_manifest_hash="0" * 64,
            exclusion_manifest=[
                {
                    "verificationId": str(verification_id),
                    "runSourceItemId": str(run_source_item_id),
                    "sourceItemId": str(source_item_id),
                    "evidenceIds": [str(evidence_id)],
                    "selectionState": "conflicting",
                    "sourceStatus": "active",
                    "sourceTitle": "상충 공고",
                    "sourceUrl": "https://example.test/conflict",
                    "publisher": "다른 기관",
                    "reason": "날짜가 다름",
                }
            ],
            exclusion_manifest_hash="1" * 64,
            claim_graph_state="blocked",
            quality_gate_manifest_hash="2" * 64,
            quality_report_hash="3" * 64,
            quality_state="failed",
            created_at=now,
            claims=Rows(claim),
            quality_checks=Rows(quality_check),
            visual_placements=Rows(visual),
        )
        article = SimpleNamespace(
            id=article_id,
            article_identity_key="housing:2026-08-09",
            topic_code="housing_subscription",
            article_type="housing_notice",
            state="blocked",
            source_run_id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            source_verification=verification,
            current_revision_id=revision_id,
            current_revision=revision,
            event_clusters=Rows(membership),
            updated_at=now,
        )

        decision = SimpleNamespace(
            publishable=False,
            blocking_codes=("high_risk_verification_satisfied",),
            policy_current=True,
            evidence_current=True,
            current_policy_snapshot_id=policy_id,
            current_policy_material_hash="8" * 64,
        )
        payload = api._article_payload(
            article,
            detail=True,
            publishability_decision=decision,
        )

        self.assertIn("revision", payload)
        self.assertEqual(payload["revision"]["bodyBlocks"], revision.body_blocks)
        self.assertEqual(payload["revision"]["editorialPolicy"]["snapshotId"], str(policy_id))
        self.assertEqual(payload["verificationSnapshots"][0]["role"], "supporting")
        self.assertEqual(
            payload["verificationSnapshots"][0]["localEventDate"], "2026-08-08"
        )
        self.assertEqual(payload["evidenceSnapshots"][0]["locator"], locator)
        self.assertEqual(payload["evidenceSnapshots"][0]["sourceVersionHash"], "f" * 64)
        self.assertEqual(
            payload["evidenceSnapshots"][0]["sourceTitle"], source_item.title
        )
        self.assertEqual(
            payload["evidenceSnapshots"][0]["freshnessCutoff"],
            "2026-08-08T03:00:00+00:00",
        )
        self.assertEqual(
            payload["evidenceSnapshots"][0]["modifiedAt"],
            "2026-08-08T03:00:00+00:00",
        )
        self.assertEqual(
            payload["evidenceSnapshots"][0]["retrievedAt"],
            "2026-08-07T03:00:00+00:00",
        )
        self.assertEqual(
            payload["evidenceSnapshots"][0]["altText"],
            "고정된 대체 텍스트",
        )
        self.assertEqual(payload["excludedMaterials"][0]["classification"], "conflicting")
        self.assertEqual(payload["claims"][0]["type"], "fact")
        self.assertEqual(payload["claims"][0]["evidenceLinks"][0]["relation"], "contradicts")
        self.assertEqual(payload["evidence"][0]["sourceUrl"], source_item.canonical_url)
        self.assertEqual(payload["evidence"][0]["publisher"], source_item.publisher)
        self.assertEqual(payload["evidence"][0]["rightsStatus"], "attribution_required")
        self.assertEqual(payload["evidence"][0]["excerpt"], link.source_span)
        self.assertTrue(payload["qualityChecks"][0]["blocking"])
        self.assertEqual(payload["visualPlacements"][0]["altText"], "공급 일정 표")
        self.assertFalse(payload["runtimeEligibility"]["publishEligible"])
        self.assertIn(
            "high_risk_verification_satisfied",
            payload["runtimeEligibility"]["blockingCodes"],
        )
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("sourceText", encoded)
        self.assertNotIn(secret_source_text, encoded)
        contract = load_openapi_contract()
        _, validator = _compile_schema(
            contract["components"]["schemas"]["ArticleDetail"],
            document=contract,
            subject="ArticleDetail response",
        )
        validator.validate(payload)

    def test_legacy_evidence_snapshot_is_an_honest_quarantine_projection(self):
        evidence_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
        revision = SimpleNamespace(
            evidence_manifest=[
                {"evidenceId": str(evidence_id), "legacyQuarantine": True}
            ],
            created_at=datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc),
        )

        payload = api._evidence_snapshot_payloads(revision)

        self.assertEqual(
            payload,
            [{"evidenceId": str(evidence_id), "legacyQuarantine": True}],
        )
        contract = load_openapi_contract()
        _, validator = _compile_schema(
            contract["components"]["schemas"]["RevisionEvidenceSnapshot"],
            document=contract,
            subject="legacy RevisionEvidenceSnapshot response",
        )
        validator.validate(payload[0])

    def test_exclusion_payload_preserves_frozen_classification_and_evidence_rights(self):
        evidence_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
        verification_id = uuid.UUID("55555555-5555-4555-8555-555555555555")
        revision = SimpleNamespace(
            exclusion_manifest=[
                {
                    "verificationId": str(verification_id),
                    "runSourceItemId": None,
                    "sourceItemId": None,
                    "evidenceIds": [str(evidence_id)],
                    "selectionState": "excluded",
                    "classification": "duplicate",
                    "sourceStatus": "active",
                    "sourceTitle": "중복 원문",
                    "sourceUrl": "https://example.test/duplicate",
                    "publisher": "공식 기관",
                    "reason": "동일 공고의 중복 수집본",
                    "evidenceMaterial": [
                        {
                            "evidenceId": str(evidence_id),
                            "rightsStatus": "prohibited",
                        }
                    ],
                }
            ]
        )

        payload = api._exclusion_payloads(revision)

        self.assertEqual(payload[0]["classification"], "duplicate")
        self.assertEqual(payload[0]["rightsStatus"], "prohibited")
        contract = load_openapi_contract()
        _, validator = _compile_schema(
            contract["components"]["schemas"]["RevisionExcludedMaterial"],
            document=contract,
            subject="RevisionExcludedMaterial prohibited rights response",
        )
        validator.validate(payload[0])

    def test_manual_claim_binding_uses_request_local_references(self):
        contract = load_openapi_contract()
        _, validator = _compile_schema(
            contract["components"]["schemas"]["CreateRevisionRequest"],
            document=contract,
            subject="CreateRevisionRequest request-local claim references",
        )
        fact_evidence_id = "33333333-3333-4333-8333-333333333333"
        interpretation_evidence_id = "44444444-4444-4444-8444-444444444444"
        payload = {
            "baseRevisionNo": 1,
            "requestKey": "manual-edit-0001",
            "title": "수정 제목",
            "summary": "수정 요약",
            "bodyBlocks": [
                {"id": "fact-1", "type": "fact", "content": "확인된 사실 [S1]"},
                {
                    "id": "interpretation-1",
                    "type": "interpretation",
                    "content": "사실에서 도출한 해석 [S2]",
                },
            ],
            "claimBindings": [
                {
                    "claimRef": "fact-1-ref",
                    "blockId": "fact-1",
                    "statement": "확인된 사실",
                    "claimType": "fact",
                    "evidenceIds": [fact_evidence_id],
                    "citationMarker": "S1",
                    "sourceSpans": {fact_evidence_id: "확인된 사실"},
                    "semanticKey": None,
                    "actor": None,
                    "attribution": None,
                    "horizon": None,
                    "uncertaintyNote": None,
                    "derivedFromClaimRefs": [],
                },
                {
                    "claimRef": "interpretation-1-ref",
                    "blockId": "interpretation-1",
                    "statement": "사실에서 도출한 해석",
                    "claimType": "interpretation",
                    "evidenceIds": [interpretation_evidence_id],
                    "citationMarker": "S2",
                    "sourceSpans": {
                        interpretation_evidence_id: "해석 근거 원문"
                    },
                    "semanticKey": None,
                    "actor": None,
                    "attribution": None,
                    "horizon": None,
                    "uncertaintyNote": "해석에는 불확실성이 있음",
                    "derivedFromClaimRefs": ["fact-1-ref"],
                },
            ],
            "editReason": "해석의 사실 계보를 명시",
        }

        validator.validate(payload)

    def test_revalidation_event_registry_uses_exact_seven_field_abi(self):
        expected = (
            "article_id",
            "article_revision_id",
            "editorial_policy_snapshot_id",
            "editorial_policy_material_hash",
            "verification_manifest_hash",
            "input_evidence_manifest_hash",
            "excluded_material_manifest_hash",
        )

        route = EVENT_ROUTES[("editorial.revalidate_requested", 1)]
        schema = EVENT_PAYLOAD_SCHEMAS[("editorial.revalidate_requested", 1)]

        self.assertEqual(route.argument_keys, expected)
        self.assertEqual(schema.required, frozenset(expected))
        self.assertEqual(set(schema.fields), set(expected))

    def test_evidence_payload_includes_derived_provenance_without_raw_text(self):
        evidence_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
        source_item_id = uuid.UUID("88888888-8888-4888-8888-888888888888")
        run_source_item_id = uuid.UUID("99999999-9999-4999-8999-999999999999")
        origin = SimpleNamespace(
            id=run_source_item_id,
            run_id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            source_snapshot_id=uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        )
        source = SimpleNamespace(
            title="원문",
            canonical_url="https://example.test/source",
            publisher="공식 기관",
            published_at=None,
            content_hash="1" * 64,
        )
        attempt = SimpleNamespace(
            id=uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            run_source_item_id=run_source_item_id,
            source_item_id=source_item_id,
            input_asset=SimpleNamespace(checksum="2" * 64),
            extraction_profile_snapshot_id=uuid.UUID(
                "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            ),
            profile_material_hash="3" * 64,
            fingerprint_schema_version="generic-extraction-fingerprint-v1",
            extraction_fingerprint="4" * 64,
            state="succeeded",
            engine="structured_parser",
            extractor_version="1.1.0",
            config_hash="5" * 64,
            validation_mode="deterministic",
            result_checksum="6" * 64,
            low_confidence_reasons_hash="7" * 64,
            calibration_profile_key=None,
            calibration_profile_version=None,
            calibration_profile_hash=None,
        )
        evidence = SimpleNamespace(
            id=evidence_id,
            source_item_id=source_item_id,
            source_item=source,
            origin_run_source_item_id=run_source_item_id,
            origin_run_source_item=origin,
            derivation_type="other_derived",
            kind="structured_data",
            evidence_content_hash="8" * 64,
            checksum="2" * 64,
            locator={},
            extracted_text="비노출 원문" * 1000,
            generic_extraction_attempt=attempt,
            rights_status="allowed",
            rights_basis_url="https://example.test/rights",
            attribution_text=None,
            alt_text=None,
            confidence=None,
            confidence_detail=None,
            low_confidence_reasons=[],
            review_subject_schema_version="v1",
            review_subject_hash="9" * 64,
            latest_review_decision=None,
            manual_review_required=False,
            publishable=True,
            review_state="passed",
        )

        payload = api._evidence_payloads({str(evidence_id): evidence}, [])[0]

        self.assertEqual(payload["extraction"]["attemptId"], str(attempt.id))
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("비노출 원문", encoded)

    def test_runtime_payload_serializes_shared_fail_closed_decision(self):
        evaluated_snapshot_id = uuid.UUID("77777777-7777-4777-8777-777777777777")
        current_snapshot_id = uuid.UUID("88888888-8888-4888-8888-888888888888")
        evaluated_at = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)
        revision = SimpleNamespace(
            editorial_policy_snapshot=SimpleNamespace(
                id=evaluated_snapshot_id,
                material_hash="1" * 64,
            )
        )
        decision = SimpleNamespace(
            publishable=False,
            blocking_codes=("evidence_latest_review_stale",),
            policy_current=True,
            evidence_current=False,
            current_policy_snapshot_id=current_snapshot_id,
            current_policy_material_hash="2" * 64,
        )

        payload = api._runtime_payload(
            revision,
            evaluated_at=evaluated_at,
            publishability_decision=decision,
        )

        self.assertFalse(payload["publishEligible"])
        self.assertTrue(payload["policyCurrent"])
        self.assertFalse(payload["evidenceCurrent"])
        self.assertEqual(
            payload["currentReleasePolicySnapshotId"], str(current_snapshot_id)
        )
        self.assertEqual(payload["blockingCodes"], ["evidence_latest_review_stale"])

    def test_article_console_uses_text_nodes_and_hides_publish_until_passed(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the admin UI contract test")
        script_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "static"
            / "admin_console"
            / "articles.js"
        )
        scenario = textwrap.dedent(
            f"""
            class Node {{
              constructor(tag = 'div') {{
                this.tagName = tag.toUpperCase();
                this.children = [];
                this.dataset = {{}};
                this.className = '';
                this._text = '';
                this.classList = {{ add() {{}}, remove() {{}} }};
              }}
              set innerHTML(value) {{ throw new Error('unsafe innerHTML assignment'); }}
              set textContent(value) {{ this._text = String(value ?? ''); }}
              get textContent() {{
                return this._text + this.children.map(child => child.textContent || '').join('');
              }}
              append(...children) {{ this.children.push(...children.filter(Boolean)); }}
              replaceChildren(...children) {{ this.children = children.filter(Boolean); }}
              addEventListener() {{}}
            }}
            const detail = new Node('section');
            global.window = {{
              csrfToken: '',
              WISDOME_DISABLE_ARTICLE_AUTOLOAD: true,
            }};
            global.document = {{
              createElement: tag => new Node(tag),
              querySelector: selector => selector === '#article-detail' ? detail : null,
              querySelectorAll: () => [],
            }};
            global.fetch = () => new Promise(() => {{}});
            require({json.dumps(str(script_path))});
            const ui = window.WisdomeArticleAdmin;
            if (!ui || typeof ui.renderArticle !== 'function') {{
              throw new Error('article renderer is not exposed');
            }}
            const row = {{
              id: 'article-1',
              title: '<img src=x onerror=alert(1)>',
              qualityState: 'failed',
              revalidationState: 'blocked',
              revision: {{
                summary: '<script>alert(1)</script>',
                bodyBlocks: [{{ id: 'b1', type: 'fact', content: '본문' }}],
              }},
              runtimeEligibility: {{ publishEligible: false, blockingCodes: ['quality'] }},
              claims: [{{
                id: 'claim-1', blockId: 'b1', statement: '<svg onload=alert(1)>',
                type: 'outlook', riskLevel: 'high', verificationState: 'conflicted',
                actor: '기업<script>', attribution: '보도자료', horizon: '2027년',
                uncertaintyNote: '불확실', derivedFromClaimIds: [],
                evidenceLinks: [{{
                  evidenceId: 'evidence-1', relation: 'context',
                  sourceSpan: '<img src=x onerror=alert(1)>', verificationStrength: 'context',
                  independenceGroup: 'corporate',
                }}],
              }}],
              evidence: [{{
                id: 'evidence-1', publisher: '기관<script>', rightsStatus: 'allowed',
                sourceUrl: 'javascript:alert(1)', locator: {{ path: '/x' }},
              }}],
              evidenceSnapshots: [{{
                evidenceId: 'evidence-1', publisher: '고정 기관', rightsStatus: 'attribution_required',
                sourceUrl: 'https://safe.example/source', publishedAt: '2026-08-08T00:00:00Z',
                retrievedAt: '2026-08-09T00:00:00Z', freshnessCutoff: '2026-08-07T00:00:00Z',
                locator: {{ path: '/frozen' }},
              }}],
              excludedMaterials: [{{
                sourceTitle: '<img src=x>', reason: '<script>reason</script>',
                publisher: '제외 기관', rightsStatus: 'unknown', classification: 'excluded',
                sourceUrl: 'data:text/html,<script>alert(1)</script>',
              }}],
              qualityChecks: [{{
                code: 'high_risk_verification_satisfied', result: 'failed', blocking: true,
                details: {{ reason: '<img src=x onerror=alert(1)>' }},
              }}],
              visualPlacements: [],
            }};
            ui.renderArticle(detail, row);
            const walk = node => [node, ...node.children.flatMap(walk)];
            const failedNodes = walk(detail);
            if (!detail.textContent.includes('<img src=x onerror=alert(1)>')) {{
              throw new Error('source-derived title was not rendered as literal text');
            }}
            if (!detail.textContent.includes('<script>alert(1)</script>')) {{
              throw new Error('revision summary was not rendered as literal text');
            }}
            if (!detail.textContent.includes('기업<script>') || !detail.textContent.includes('2027년')) {{
              throw new Error('claim semantic fields are missing from the drilldown');
            }}
            if (!detail.textContent.includes('고정 기관') || detail.textContent.includes('기관<script>')) {{
              throw new Error('claim evidence must render frozen provenance, not live source fields');
            }}
            if (failedNodes.some(node => node.tagName === 'A' && !String(node.href).startsWith('https://'))) {{
              throw new Error('unsafe evidence URLs must not create links');
            }}
            if (failedNodes.some(node => node.tagName === 'A' && node.className.includes('publish'))) {{
              throw new Error('publish link must be absent while blocked');
            }}
            row.qualityState = 'passed';
            row.revalidationState = 'passed';
            row.runtimeEligibility = {{
              publishEligible: true, policyCurrent: true, evidenceCurrent: true, blockingCodes: []
            }};
            ui.renderArticle(detail, row);
            if (!walk(detail).some(node => node.tagName === 'A' && node.className.includes('publish'))) {{
              throw new Error('publish link is required after every gate passes');
            }}
            """
        )
        completed = subprocess.run(
            [node, "-e", scenario],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_shared_external_url_helper_accepts_only_http_schemes(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the URL safety test")
        script_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "static"
            / "admin_console"
            / "url_safety.js"
        )
        scenario = textwrap.dedent(
            f"""
            global.window = {{}};
            require({json.dumps(str(script_path))});
            const safe = window.WisdomeUrlSafety?.safeHttpUrl;
            if (typeof safe !== 'function') throw new Error('safeHttpUrl is not exposed');
            if (safe('javascript:alert(1)') !== null) throw new Error('javascript scheme accepted');
            if (safe('data:text/html,<script>alert(1)</script>') !== null) throw new Error('data scheme accepted');
            if (safe('https://safe.example/post') !== 'https://safe.example/post') throw new Error('https rejected');
            if (safe('http://safe.example/post') !== 'http://safe.example/post') throw new Error('http rejected');
            """
        )
        completed = subprocess.run(
            [node, "-e", scenario],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
