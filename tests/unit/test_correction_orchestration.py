from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from django.apps import apps
from django.conf import settings
from django.db import connection
from django.test import TestCase as DjangoTestCase
from django.urls import resolve
from jsonschema.exceptions import ValidationError

from apps.editorial import corrections as editorial_corrections
from apps.publishing import corrections as publishing_corrections
from wisdome_writer.api.openapi import _compile_schema, load_openapi_contract


SHA_A = "a" * 64
SHA_B = "b" * 64


class CorrectionDomainContractTests(TestCase):
    def test_correction_decision_and_case_head_fields_exist(self):
        decision = apps.get_model("editorial", "CorrectionDecision")
        decision_fields = {field.name for field in decision._meta.fields}
        self.assertTrue(
            {
                "correction_case",
                "decision",
                "subject_hash",
                "diff_manifest_hash",
                "corrected_revision",
                "supersedes",
                "head_version",
                "request_key",
                "request_hash",
                "reauth_proof_id",
                "decision_reason",
                "decided_by",
                "decided_at",
            }
            <= decision_fields
        )
        case = apps.get_model("editorial", "CorrectionCase")
        case_fields = {field.name for field in case._meta.fields}
        self.assertTrue(
            {
                "corrected_revision",
                "latest_decision",
                "decision_version",
                "verified_at",
                "dispatched_at",
                "failure_summary",
            }
            <= case_fields
        )

    def test_decision_request_hash_binds_cas_actor_reason_and_revision(self):
        helper = editorial_corrections.correction_decision_request_hash
        material = {
            "case_id": uuid.UUID("00000000-0000-4000-8000-000000000101"),
            "decision": "verified",
            "subject_hash": SHA_A,
            "diff_manifest_hash": SHA_B,
            "corrected_revision_id": uuid.UUID(
                "00000000-0000-4000-8000-000000000102"
            ),
            "expected_latest_decision_id": None,
            "expected_decision_version": 0,
            "request_key": "correction-decision-001",
            "reason": "공식 정정 원문과 새 개정을 검증했습니다.",
            "actor_id": uuid.UUID("00000000-0000-4000-8000-000000000103"),
            "reauth_proof_id": uuid.UUID(
                "00000000-0000-4000-8000-000000000104"
            ),
        }
        baseline = helper(**material)
        for key in (
            "decision",
            "subject_hash",
            "diff_manifest_hash",
            "corrected_revision_id",
            "expected_decision_version",
            "request_key",
            "reason",
            "actor_id",
            "reauth_proof_id",
        ):
            changed = dict(material)
            if key.endswith("_id"):
                changed[key] = uuid.UUID("00000000-0000-4000-8000-000000000999")
            elif key == "expected_decision_version":
                changed[key] = 1
            else:
                changed[key] = f"changed-{material[key]}"
            self.assertNotEqual(baseline, helper(**changed), key)

    def test_corrected_revision_must_be_current_passed_and_include_changed_source(self):
        case = SimpleNamespace(
            article_id=uuid.UUID("00000000-0000-4000-8000-000000000201"),
            source_item_id=uuid.UUID("00000000-0000-4000-8000-000000000202"),
            article=SimpleNamespace(
                current_revision_id=uuid.UUID(
                    "00000000-0000-4000-8000-000000000203"
                )
            ),
        )
        revision = SimpleNamespace(
            id=case.article.current_revision_id,
            article_id=case.article_id,
            claim_graph_state="passed",
            quality_state="passed",
            evidence_manifest=[{"sourceItemId": str(case.source_item_id)}],
        )
        with patch(
            "apps.editorial.services.require_revision_publishable"
        ) as publishable:
            editorial_corrections.validate_corrected_revision(
                case=case,
                revision=revision,
            )
        publishable.assert_called_once_with(revision)

        revision.evidence_manifest = []
        with self.assertRaisesRegex(ValueError, "source"):
            editorial_corrections.validate_corrected_revision(
                case=case,
                revision=revision,
            )

    def test_change_detection_returns_existing_case_for_exact_subject(self):
        item = SimpleNamespace(
            source_id=uuid.UUID("00000000-0000-4000-8000-000000000301"),
            external_id="notice-1",
            source_version_hash=SHA_B,
            content_hash=SHA_B,
            status="corrected",
            canonical_url="https://example.test/current",
        )
        prior = SimpleNamespace(
            source_version_hash=SHA_A,
            content_hash=SHA_A,
            canonical_url="https://example.test/prior",
        )
        existing = SimpleNamespace(id=uuid.uuid4())
        prior_query = MagicMock()
        prior_query.exclude.return_value.order_by.return_value.first.return_value = None
        with patch.object(
            editorial_corrections.CorrectionCase.objects,
            "get_or_create",
            return_value=(existing, False),
        ), patch.object(
            editorial_corrections.CorrectionCase.objects,
            "filter",
            return_value=prior_query,
        ):
            observed = editorial_corrections._create_cases_for_change(
                item=item,
                prior_item=prior,
                article_ids=[uuid.uuid4()],
            )
        self.assertEqual(observed, [existing])

    def test_exact_decision_replay_precedes_live_case_state_and_proof(self):
        case_id = uuid.UUID("00000000-0000-4000-8000-000000000351")
        actor_id = uuid.UUID("00000000-0000-4000-8000-000000000352")
        proof_id = uuid.UUID("00000000-0000-4000-8000-000000000353")
        request_key = "correction-decision-351"
        reason = "공식 정정 원문을 검증했습니다."
        expected_hash = editorial_corrections.correction_decision_request_hash(
            case_id=case_id,
            decision="rejected",
            subject_hash=SHA_A,
            diff_manifest_hash=SHA_B,
            corrected_revision_id=None,
            expected_latest_decision_id=None,
            expected_decision_version=0,
            request_key=request_key,
            reason=reason,
            actor_id=actor_id,
            reauth_proof_id=proof_id,
        )
        existing = SimpleNamespace(
            id=uuid.uuid4(),
            request_hash=expected_hash,
            decided_by_id=actor_id,
            reauth_proof_id=proof_id,
        )
        case = SimpleNamespace(id=case_id, state="applying")
        case_query = MagicMock()
        case_query.select_related.return_value.get.return_value = case
        decision_query = MagicMock()
        decision_query.select_related.return_value.first.return_value = existing
        context = SimpleNamespace(
            request_key=request_key,
            reason_code=reason,
        )
        user = SimpleNamespace(
            pk=actor_id,
            is_authenticated=True,
            is_active=True,
            is_staff=True,
        )
        with (
            patch.object(
                editorial_corrections.CorrectionCase.objects,
                "select_for_update",
                return_value=case_query,
            ),
            patch.object(
                editorial_corrections.CorrectionDecision.objects,
                "filter",
                return_value=decision_query,
            ),
            patch.object(
                editorial_corrections,
                "require_audit_replay",
            ) as audit_replay,
            patch.object(
                editorial_corrections,
                "consume_reauthentication_proof",
                side_effect=AssertionError("proof must not be consumed on replay"),
            ),
        ):
            observed, created = editorial_corrections.decide_correction_case.__wrapped__(
                case_id=case_id,
                decision="rejected",
                expected_subject_hash=SHA_A,
                expected_diff_manifest_hash=SHA_B,
                corrected_revision_id=None,
                expected_latest_decision_id=None,
                expected_decision_version=0,
                request_key=request_key,
                reason=reason,
                reauth_proof_id=proof_id,
                user=user,
                request=SimpleNamespace(),
                audit_context=context,
            )
        self.assertIs(observed, existing)
        self.assertFalse(created)
        audit_replay.assert_called_once()


class CorrectionPublishingContractTests(TestCase):
    def test_public_history_binds_case_decision_diff_and_source_links(self):
        case = SimpleNamespace(
            id=uuid.UUID("00000000-0000-4000-8000-000000000401"),
            kind="correction",
            subject_hash=SHA_A,
            diff_summary={"changedFields": ["applicationEndAt"]},
            source_item=SimpleNamespace(
                id=uuid.uuid4(),
                canonical_url="https://example.test/current",
                source_version_hash=SHA_B,
            ),
            prior_source_item=SimpleNamespace(
                id=uuid.uuid4(),
                canonical_url="https://example.test/prior",
                source_version_hash=SHA_A,
            ),
            corrected_revision_id=uuid.UUID(
                "00000000-0000-4000-8000-000000000499"
            ),
            latest_decision=SimpleNamespace(
                id=uuid.uuid4(),
                decision="verified",
                diff_manifest_hash=SHA_B,
                decided_at=SimpleNamespace(isoformat=lambda: "2026-08-11T00:00:00+00:00"),
                corrected_revision_id=uuid.UUID(
                    "00000000-0000-4000-8000-000000000499"
                ),
                subject_hash=SHA_A,
            ),
        )
        history = publishing_corrections.build_public_correction_history(case)
        self.assertEqual(history[0]["correctionCaseId"], str(case.id))
        self.assertEqual(history[0]["decisionId"], str(case.latest_decision.id))
        self.assertEqual(history[0]["sourceLinks"], [
            "https://example.test/current",
            "https://example.test/prior",
        ])

    def test_prepare_uses_frozen_corrected_revision_not_article_current_guess(self):
        corrected = SimpleNamespace(
            id=uuid.UUID("00000000-0000-4000-8000-000000000501"),
            revision_no=4,
            content_hash=SHA_A,
        )
        case = SimpleNamespace(
            id=uuid.UUID("00000000-0000-4000-8000-000000000502"),
            article_id=uuid.UUID("00000000-0000-4000-8000-000000000503"),
            state="verified",
            kind="correction",
            corrected_revision=corrected,
            corrected_revision_id=corrected.id,
            latest_decision_id=uuid.uuid4(),
            latest_decision=SimpleNamespace(
                decision="verified",
                corrected_revision_id=corrected.id,
            ),
            dispatched_at=None,
            save=MagicMock(),
        )
        case_query = MagicMock()
        case_query.get.return_value = case
        intent_query = MagicMock()
        intent_query.filter.return_value.first.return_value = None
        article = SimpleNamespace(
            id=case.article_id,
            current_revision=corrected,
            current_revision_id=corrected.id,
        )
        article_query = MagicMock()
        article_query.get.return_value = article
        locked_article_query = MagicMock()
        locked_article_query.select_related.return_value.get.return_value = article
        plan = publishing_corrections.CorrectionPlan([], [])

        with (
            patch.object(
                publishing_corrections.CorrectionCase.objects,
                "select_for_update",
                return_value=case_query,
            ),
            patch.object(
                publishing_corrections.PublicationIntent.objects,
                "select_related",
                return_value=intent_query,
            ),
            patch.object(
                publishing_corrections.DraftArticle.objects,
                "select_related",
                return_value=article_query,
            ),
            patch.object(
                publishing_corrections.DraftArticle.objects,
                "select_for_update",
                return_value=locked_article_query,
            ),
            patch.object(
                publishing_corrections,
                "build_correction_plan",
                return_value=plan,
            ),
            patch.object(
                publishing_corrections,
                "resolve_current_publication_intent",
                return_value=None,
            ),
            patch.object(
                publishing_corrections,
                "create_publication_intent",
                return_value=(SimpleNamespace(id=uuid.uuid4()), True),
            ) as create_intent,
            patch.object(
                publishing_corrections,
                "_require_revision_for_commands",
            ),
        ):
            publishing_corrections._prepare_verified_correction_atomic.__wrapped__(
                str(case.id),
                user=SimpleNamespace(pk=uuid.uuid4()),
                request_key="correction-publish-001",
                audit_context=SimpleNamespace(reason_code="검증된 정정 반영"),
            )

        payload = create_intent.call_args.args[1]
        self.assertEqual(payload["revisionNo"], corrected.revision_no)
        self.assertEqual(
            payload["expectedRevisionContentHash"], corrected.content_hash
        )


class CorrectionWorkerContractTests(TestCase):
    def test_periodic_detector_task_and_beat_are_registered(self):
        from apps.editorial import tasks

        self.assertTrue(callable(tasks.detect_source_corrections))
        beat = settings.CELERY_BEAT_SCHEDULE["detect-source-corrections"]
        self.assertEqual(
            beat["task"], "apps.editorial.tasks.detect_source_corrections"
        )
        self.assertLessEqual(float(beat["schedule"]), 300.0)


class CorrectionDatabaseContractTests(DjangoTestCase):
    def test_decision_append_only_guards_are_installed(self):
        model = apps.get_model("editorial", "CorrectionDecision")
        with self.assertRaisesRegex(TypeError, "append-only"):
            model.objects.update(decision="rejected")
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                names = {
                    row[0]
                    for row in cursor.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='trigger' AND name LIKE "
                        "'editorial_correction_%_guard'"
                    ).fetchall()
                }
            self.assertTrue(
                {
                    "editorial_correction_decision_insert_guard",
                    "editorial_correction_decision_update_guard",
                    "editorial_correction_decision_delete_guard",
                    "editorial_correction_case_head_guard",
                }
                <= names
            )


class CorrectionApiContractTests(TestCase):
    def test_decision_route_and_openapi_operation_are_exact(self):
        contract = load_openapi_contract()
        operation = contract["paths"]["/corrections/{correctionId}/decisions"][
            "post"
        ]
        self.assertEqual(operation["operationId"], "decideCorrection")
        self.assertEqual(
            resolve(
                "/api/v1/corrections/"
                "00000000-0000-4000-8000-000000000701/decisions"
            ).url_name,
            "correction-decisions",
        )

    def test_decision_request_is_closed_and_requires_dual_cas_reauth(self):
        contract = load_openapi_contract()
        _, validator = _compile_schema(
            contract["components"]["schemas"]["CorrectionDecisionRequest"],
            document=contract,
            subject="CorrectionDecisionRequest",
        )
        body = {
            "decision": "verified",
            "expectedSubjectHash": SHA_A,
            "expectedDiffManifestHash": SHA_B,
            "correctedRevisionId": "00000000-0000-4000-8000-000000000702",
            "expectedLatestDecisionId": None,
            "expectedDecisionVersion": 0,
            "requestKey": "correction-decision-702",
            "reauthProofId": "00000000-0000-4000-8000-000000000703",
            "reason": "공식 정정 원문과 새 개정을 검증했습니다.",
        }
        validator.validate(body)
        with self.assertRaises(ValidationError):
            validator.validate({**body, "unexpected": True})
        with self.assertRaises(ValidationError):
            missing = dict(body)
            missing.pop("expectedDecisionVersion")
            validator.validate(missing)

        scopes = contract["components"]["schemas"]["ReauthenticationRequest"][
            "properties"
        ]["actionScopes"]["items"]["enum"]
        self.assertIn("correction_decision", scopes)
