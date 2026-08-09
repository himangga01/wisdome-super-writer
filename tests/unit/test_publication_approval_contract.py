import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase
from jsonschema.exceptions import ValidationError

from apps.publishing import api
from wisdome_writer.api.openapi import _compile_schema, load_openapi_contract
from wisdome_writer.domain.errors import Conflict, InvalidInput


ARTICLE_ID = "11111111-1111-4111-8111-111111111111"
INTENT_ID = "22222222-2222-4222-8222-222222222222"
TARGET_ID = "33333333-3333-4333-8333-333333333333"
SNAPSHOT_ID = "44444444-4444-4444-8444-444444444444"
RENDER_ID = "55555555-5555-4555-8555-555555555555"
APPROVAL_ID = "66666666-6666-4666-8666-666666666666"
CURRENT_APPROVAL_ID = "77777777-7777-4777-8777-777777777777"
REAUTH_ID = "88888888-8888-4888-8888-888888888888"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _validator(schema_name: str):
    contract = load_openapi_contract()
    _, validator = _compile_schema(
        contract["components"]["schemas"][schema_name],
        document=contract,
        subject=schema_name,
    )
    return validator


def _content_subject() -> dict:
    return {
        "kind": "content_preview",
        "action": "create",
        "renderId": RENDER_ID,
        "targetId": TARGET_ID,
        "targetSnapshotId": SNAPSHOT_ID,
        "targetConfigHash": HASH_A,
        "templateHash": HASH_B,
        "sourceManifestHash": HASH_C,
    }


def _unpublish_subject() -> dict:
    return {
        "kind": "unpublish_command",
        "action": "unpublish",
        "targetId": TARGET_ID,
        "targetSnapshotId": SNAPSHOT_ID,
        "targetConfigHash": HASH_A,
        "remotePostId": "remote-post-1",
        "observedRemoteState": "published",
        "reason": "Corrections require removal.",
        "affectedTargetIds": [TARGET_ID],
        "correctionEvidenceManifestHash": HASH_B,
    }


def _request_payload(*, decision="approved", subject=None, reauth_proof_id=None):
    return {
        "revisionNo": 3,
        "publicationIntentId": INTENT_ID,
        "expectedLatestApprovalId": None,
        "expectedHeadVersion": 0,
        "requestKey": "approval-request-0001",
        "reauthProofId": reauth_proof_id,
        "actionSubject": subject or _content_subject(),
        "decision": decision,
        "decisionReason": "Reviewed against the frozen publication material.",
    }


class PublicationApprovalContractTests(SimpleTestCase):
    maxDiff = None

    def test_publication_intent_serializer_omits_unbounded_render_and_approval_history(self):
        created_at = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)
        empty_related_manager = SimpleNamespace(all=lambda: ())
        row = SimpleNamespace(
            id=uuid.UUID(INTENT_ID),
            article_id=uuid.UUID(ARTICLE_ID),
            revision_no=3,
            revision_content_hash=HASH_A,
            generation_attempt_id=None,
            input_evidence_manifest_hash=HASH_B,
            generation_pipeline_manifest_hash=None,
            quality_gate_manifest_hash=HASH_C,
            quality_report_hash="d" * 64,
            correction_case_id=None,
            target_snapshot_refs=[
                {
                    "targetId": TARGET_ID,
                    "targetSnapshotId": SNAPSHOT_ID,
                    "targetConfigHash": HASH_A,
                }
            ],
            target_commands=[
                {
                    "targetId": TARGET_ID,
                    "targetSnapshotId": SNAPSHOT_ID,
                    "targetConfigHash": HASH_A,
                    "resolvedAction": "create",
                    "canonicalDependencyTargetId": None,
                }
            ],
            target_snapshot_manifest_hash=HASH_B,
            approval_mode="manual",
            auto_publish_validation_refs=[],
            auto_publish_activation_refs=[],
            auto_activation_manifest_hash=None,
            supersedes_intent_id=None,
            intent_hash=HASH_C,
            request_key="intent-request-0001",
            state="awaiting_approval",
            created_at=created_at,
            renders=empty_related_manager,
            approvals=empty_related_manager,
        )

        payload = api.intent_json(row)

        self.assertNotIn("renders", payload)
        self.assertNotIn("approvals", payload)
        _validator("PublicationIntent").validate(payload)

    def test_request_uses_exact_cas_and_decision_reason_fields(self):
        validator = _validator("ApprovalRequest")
        payload = _request_payload()

        validator.validate(payload)

        legacy = dict(payload)
        legacy["reason"] = legacy.pop("decisionReason")
        with self.assertRaises(ValidationError):
            validator.validate(legacy)

        null_id_with_noninitial_version = dict(payload)
        null_id_with_noninitial_version["expectedHeadVersion"] = 1
        with self.assertRaises(ValidationError):
            validator.validate(null_id_with_noninitial_version)

        existing_id_with_initial_version = dict(payload)
        existing_id_with_initial_version["expectedLatestApprovalId"] = APPROVAL_ID
        with self.assertRaises(ValidationError):
            validator.validate(existing_id_with_initial_version)

    def test_request_rejects_unsafe_or_untrimmed_audit_fields(self):
        validator = _validator("ApprovalRequest")
        rejected = (
            {**_request_payload(), "requestKey": "        "},
            {**_request_payload(), "decisionReason": "   "},
            {**_request_payload(), "decisionReason": " Reviewed material"},
            {**_request_payload(), "decisionReason": "Reviewed material "},
        )

        for payload in rejected:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    validator.validate(payload)

    def test_request_reauthentication_is_conditional_on_decision_and_action(self):
        validator = _validator("ApprovalRequest")
        accepted = (
            _request_payload(decision="approved", reauth_proof_id=None),
            _request_payload(decision="rejected", subject=_unpublish_subject(), reauth_proof_id=None),
            _request_payload(decision="revoked", reauth_proof_id=REAUTH_ID),
            _request_payload(
                decision="approved",
                subject=_unpublish_subject(),
                reauth_proof_id=REAUTH_ID,
            ),
        )
        rejected = (
            _request_payload(decision="approved", reauth_proof_id=REAUTH_ID),
            _request_payload(decision="rejected", reauth_proof_id=REAUTH_ID),
            _request_payload(decision="revoked", reauth_proof_id=None),
            _request_payload(
                decision="approved",
                subject=_unpublish_subject(),
                reauth_proof_id=None,
            ),
        )

        for payload in accepted:
            with self.subTest(accepted=payload):
                validator.validate(payload)
        for payload in rejected:
            with self.subTest(rejected=payload):
                with self.assertRaises(ValidationError):
                    validator.validate(payload)

    def test_approval_audit_context_uses_decision_reason(self):
        request = RequestFactory().post(f"/api/v1/articles/{ARTICLE_ID}/approvals")
        request.user = SimpleNamespace(
            pk=uuid.UUID("99999999-9999-4999-8999-999999999999"),
            is_authenticated=True,
            is_active=True,
            is_staff=True,
            _state=SimpleNamespace(db="default"),
        )
        request.correlation_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

        context = api._approval_audit_context(request, _request_payload())

        self.assertEqual(
            context.reason_code,
            "Reviewed against the frozen publication material.",
        )
        self.assertEqual(context.request_key, "approval-request-0001")

    def test_approval_audit_context_maps_sanitizer_failures_to_invalid_input(self):
        request = RequestFactory().post(f"/api/v1/articles/{ARTICLE_ID}/approvals")
        request.user = SimpleNamespace(
            pk=uuid.UUID("99999999-9999-4999-8999-999999999999"),
            is_authenticated=True,
            is_active=True,
            is_staff=True,
            _state=SimpleNamespace(db="default"),
        )
        request.correlation_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        rejected = (
            {**_request_payload(), "requestKey": "unsafe key"},
            {
                **_request_payload(),
                "decisionReason": "api_key=must-not-enter-audit",
            },
        )

        for payload in rejected:
            with self.subTest(payload=payload):
                try:
                    api._approval_audit_context(request, payload)
                except Exception as exc:
                    self.assertIsInstance(exc, InvalidInput)
                else:
                    self.fail("unsafe audit input was accepted")

    def test_approval_serializer_projects_the_current_head_not_row_inference(self):
        decided_at = datetime(2026, 8, 9, 4, 0, tzinfo=timezone.utc)
        head_updated_at = datetime(2026, 8, 9, 4, 5, tzinfo=timezone.utc)
        row = SimpleNamespace(
            id=uuid.UUID(APPROVAL_ID),
            revision_no=3,
            publication_intent_id=uuid.UUID(INTENT_ID),
            target_id=uuid.UUID(TARGET_ID),
            approval_subject_hash=HASH_A,
            decision_hash=HASH_B,
            approval_material_version="approval-subject-v3",
            head_version=1,
            supersedes_approval_id=None,
            request_key="approval-request-0001",
            reauth_proof_id=None,
            action_subject=_content_subject(),
            mode="manual",
            decision="approved",
            decision_reason="Reviewed against the frozen publication material.",
            admin_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
            decision_actor_type="admin",
            decision_actor_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
            decided_at=decided_at,
        )
        projection = SimpleNamespace(
            current_head_approval_id=uuid.UUID(CURRENT_APPROVAL_ID),
            current_head_version=2,
            current_head_decision="revoked",
            current_head_subject_hash=HASH_C,
            current_head_decision_hash="d" * 64,
            current_head_updated_at=head_updated_at,
            is_current=False,
            dispatch_eligible=False,
        )

        payload = api.approval_json(row, projection=projection)

        self.assertEqual(
            payload,
            {
                "id": APPROVAL_ID,
                "revisionNo": 3,
                "publicationIntentId": INTENT_ID,
                "targetId": TARGET_ID,
                "approvalSubjectHash": HASH_A,
                "decisionHash": HASH_B,
                "approvalMaterialVersion": "approval-subject-v3",
                "headVersion": 1,
                "supersedesApprovalId": None,
                "requestKey": "approval-request-0001",
                "reauthProofId": None,
                "actionSubject": _content_subject(),
                "mode": "manual",
                "decision": "approved",
                "decisionReason": "Reviewed against the frozen publication material.",
                "decidedBy": "99999999-9999-4999-8999-999999999999",
                "decisionActorType": "admin",
                "decisionActorId": "99999999-9999-4999-8999-999999999999",
                "currentHead": {
                    "latestApprovalId": CURRENT_APPROVAL_ID,
                    "version": 2,
                    "decision": "revoked",
                    "approvalSubjectHash": HASH_C,
                    "decisionHash": "d" * 64,
                    "updatedAt": head_updated_at.isoformat(),
                },
                "isCurrent": False,
                "dispatchEligible": False,
                "decidedAt": decided_at.isoformat(),
            },
        )
        validator = _validator("Approval")
        validator.validate(payload)
        worker_actor = dict(payload)
        worker_actor["decisionActorType"] = "worker"
        worker_actor["decisionActorId"] = None
        validator.validate(worker_actor)
        worker_row = SimpleNamespace(
            **{
                **vars(row),
                "decision_actor_type": "worker",
                "decision_actor_id": None,
            }
        )
        self.assertEqual(
            api.approval_json(worker_row, projection=projection),
            worker_actor,
        )
        admin_without_actor_id = dict(payload)
        admin_without_actor_id["decisionActorId"] = None
        with self.assertRaises(ValidationError):
            validator.validate(admin_without_actor_id)
        worker_with_actor_id = dict(worker_actor)
        worker_with_actor_id["decisionActorId"] = APPROVAL_ID
        with self.assertRaises(ValidationError):
            validator.validate(worker_with_actor_id)
        missing_owner = SimpleNamespace(**{**vars(row), "admin_id": None})
        with self.assertRaises(Conflict):
            api.approval_json(missing_owner, projection=projection)
        legacy_version = dict(payload)
        legacy_version["approvalMaterialVersion"] = "approval-subject-v2"
        with self.assertRaises(ValidationError):
            validator.validate(legacy_version)
        extra_member = dict(payload)
        extra_member["subjectHash"] = HASH_A
        with self.assertRaises(ValidationError):
            validator.validate(extra_member)
        with patch.object(
            api,
            "evaluate_approval_decision_readonly",
            return_value=projection,
        ) as evaluate:
            self.assertEqual(api.approval_json(row), payload)
        evaluate.assert_called_once_with(approval_id=row.id)

    def test_approval_endpoint_forwards_exact_body_and_distinguishes_create_from_replay(self):
        article_id = uuid.UUID(ARTICLE_ID)
        payload = _request_payload()
        row = SimpleNamespace(id=uuid.UUID(APPROVAL_ID))
        audit_context = object()

        def request():
            value = RequestFactory().post(
                f"/api/v1/articles/{ARTICLE_ID}/approvals",
                data=payload,
                content_type="application/json",
                HTTP_X_CSRFTOKEN="test-token",
            )
            value._dont_enforce_csrf_checks = True
            value.correlation_id = uuid.UUID(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            )
            value.user = SimpleNamespace(
                pk=uuid.UUID("99999999-9999-4999-8999-999999999999"),
                is_authenticated=True,
                is_active=True,
                is_staff=True,
            )
            return value

        serialized = {"id": APPROVAL_ID}
        with (
            patch.object(
                api.AuditContext,
                "for_admin",
                return_value=audit_context,
            ),
            patch.object(
                api,
                "decide_approval",
                side_effect=((row, True), (row, False)),
            ) as decide,
            patch.object(api, "approval_json", return_value=serialized),
        ):
            created = api.approvals(request(), article_id=article_id)
            replayed = api.approvals(request(), article_id=article_id)

        self.assertEqual(created.status_code, 201)
        self.assertEqual(replayed.status_code, 200)
        self.assertEqual(decide.call_count, 2)
        for call in decide.call_args_list:
            self.assertEqual(call.args[:3], (article_id, TARGET_ID, payload))
            self.assertEqual(call.kwargs["audit_context"], audit_context)
            self.assertEqual(
                call.kwargs["user"].pk,
                uuid.UUID("99999999-9999-4999-8999-999999999999"),
            )

    def test_approval_endpoint_status_contract_is_explicit(self):
        responses = load_openapi_contract()["paths"][
            "/articles/{articleId}/approvals"
        ]["post"]["responses"]

        self.assertEqual(
            {"200", "201", "403", "409", "422"},
            {status for status in responses if status != "default"},
        )
