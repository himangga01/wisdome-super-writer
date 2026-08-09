from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from apps.publishing import api, automation, corrections, services, tasks
from apps.publishing.models import Approval, PublicationAction
from wisdome_writer.domain.errors import Conflict, InvalidInput
from wisdome_writer.infrastructure.models import (
    OutboxConsumerReceipt,
    OutboxMessage,
)
from wisdome_writer.infrastructure.event_routes import route_for


def _revision(**overrides):
    values = {
        "id": "revision-1",
        "revision_no": 3,
        "content_hash": "content-v1",
        "evidence_manifest_hash": "evidence-v1",
        "editorial_policy_hash": "policy-v1",
        "verification_manifest_hash": "verification-v1",
        "exclusion_manifest_hash": "exclusion-v1",
        "claim_manifest_hash": "claims-v1",
        "quality_gate_manifest_hash": "quality-gate-v1",
        "quality_report_hash": "quality-report-v1",
        "revalidation_generation": 7,
        "generation_attempt_id": "attempt-1",
        "generation_attempt": SimpleNamespace(
            generation_pipeline_manifest_hash="pipeline-v1"
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _commands(*actions):
    return [{"resolvedAction": action} for action in actions]


class PublicationEligibilityPolicyTests(TestCase):
    def test_intent_frozen_hash_covers_every_editorial_eligibility_dimension(self):
        revision = _revision()
        base = services._revision_publication_material(revision)

        self.assertEqual(
            base,
            {
                "revisionContentHash": "content-v1",
                "generationAttemptId": "attempt-1",
                "inputEvidenceManifestHash": "evidence-v1",
                "generationPipelineManifestHash": "pipeline-v1",
                "qualityGateManifestHash": "quality-gate-v1",
                "qualityReportHash": "quality-report-v1",
                "editorialPolicyHash": "policy-v1",
                "verificationManifestHash": "verification-v1",
                "exclusionManifestHash": "exclusion-v1",
                "claimManifestHash": "claims-v1",
                "revalidationGeneration": 7,
            },
        )
        intent_data = {
            **base,
            "approvalMode": "manual",
            "autoPublishValidationRefs": [],
            "autoPublishActivationRefs": [],
            "correctionCaseId": None,
        }
        refs = {
            "target-1": {
                "targetId": "target-1",
                "targetSnapshotId": "snapshot-1",
                "targetConfigHash": "config-v1",
            }
        }
        commands = {
            "target-1": {
                "targetId": "target-1",
                "targetSnapshotId": "snapshot-1",
                "targetConfigHash": "config-v1",
                "resolvedAction": PublicationAction.CREATE,
                "canonicalDependencyTargetId": None,
                "targetCommandHash": "command-v1",
            }
        }
        original_hash = services._intent_hash(intent_data, revision, refs, commands)
        changed_hash = services._intent_hash(
            {**intent_data, "editorialPolicyHash": "policy-v2"},
            revision,
            refs,
            commands,
        )

        self.assertNotEqual(original_hash, changed_hash)

    def test_intent_replay_and_preview_fail_closed_when_core_revalidation_is_stale(self):
        with patch.object(
            services,
            "require_revision_publishable",
            side_effect=ValueError("editorial policy changed"),
        ):
            with self.assertRaisesRegex(Conflict, "editorial policy changed"):
                services._require_revision_for_commands(
                    _revision(), _commands(PublicationAction.CREATE)
                )

    def test_approval_reject_and_revoke_are_allowed_for_stale_content_revision(self):
        with patch.object(services, "require_revision_publishable") as gate:
            for decision in (Approval.Decision.REJECTED, Approval.Decision.REVOKED):
                services._require_revision_for_commands(
                    _revision(),
                    _commands(PublicationAction.UPDATE),
                    approval_decision=decision,
                )

        gate.assert_not_called()

    def test_dispatch_content_actions_fail_before_attempt_or_outbox_creation(self):
        with patch.object(
            services,
            "require_revision_publishable",
            side_effect=ValueError("quality report is stale"),
        ):
            with self.assertRaisesRegex(Conflict, "quality report is stale"):
                services._require_revision_for_commands(
                    _revision(), _commands(PublicationAction.UPDATE)
                )

    def test_begin_attempt_content_action_fails_before_adapter_command(self):
        with patch.object(
            services,
            "require_revision_publishable",
            side_effect=ValueError("claim graph changed"),
        ):
            with self.assertRaisesRegex(Conflict, "claim graph changed"):
                services._require_revision_for_commands(
                    _revision(), _commands(PublicationAction.CREATE)
                )

    def test_automation_existing_intent_is_revalidated(self):
        with patch.object(services, "require_revision_publishable") as gate:
            services._require_revision_for_commands(
                _revision(), _commands(PublicationAction.CREATE)
            )

        self.assertEqual(gate.call_args.args[0].id, "revision-1")
        self.assertTrue(callable(automation.dispatch_validated_schedule_run))

    def test_correction_withdrawal_actions_remain_allowed_when_revision_is_stale(self):
        with patch.object(
            services,
            "require_revision_publishable",
            side_effect=AssertionError(
                "withdrawal safety actions must not run the content gate"
            ),
        ):
            services._require_revision_for_commands(
                _revision(),
                _commands(
                    PublicationAction.UNPUBLISH,
                    PublicationAction.MARK_WITHDRAWN,
                ),
            )
        self.assertTrue(callable(corrections.prepare_verified_correction))
        self.assertTrue(callable(api.article_preview))

    def test_latest_revoke_or_reject_blocks_dispatch(self):
        target_ids = {"target-1", "target-2"}

        self.assertFalse(
            services._latest_approvals_allow_dispatch(
                target_ids,
                {
                    "target-1": Approval.Decision.APPROVED,
                    "target-2": Approval.Decision.REVOKED,
                },
            )
        )
        self.assertFalse(
            services._latest_approvals_allow_dispatch(
                target_ids,
                {
                    "target-1": Approval.Decision.REJECTED,
                    "target-2": Approval.Decision.APPROVED,
                },
            )
        )

    def test_approval_subject_hash_binds_decision_and_head_version(self):
        intent = SimpleNamespace(
            id="intent-1",
            article_revision_id="revision-1",
            quality_report_hash="quality-1",
        )
        command = {
            "targetSnapshotId": "snapshot-1",
            "targetConfigHash": "config-1",
        }
        approved = services._approval_subject_hash(
            intent=intent,
            target_id="target-1",
            action=PublicationAction.CREATE,
            command=command,
            subject={"kind": "content_preview"},
            decision=Approval.Decision.APPROVED,
            head_version=1,
            supersedes_approval_id=None,
        )
        revoked = services._approval_subject_hash(
            intent=intent,
            target_id="target-1",
            action=PublicationAction.CREATE,
            command=command,
            subject={"kind": "content_preview"},
            decision=Approval.Decision.REVOKED,
            head_version=2,
            supersedes_approval_id="approval-1",
        )

        self.assertNotEqual(approved, revoked)

    def test_dispatch_requires_the_exact_frozen_target_set(self):
        with self.assertRaises(InvalidInput):
            services._require_exact_dispatch_targets(
                requested_ids=["target-1"],
                expected_refs={"target-1": {}},
                intent_commands={"target-1": {}, "target-2": {}},
                intent_refs={"target-1": {}, "target-2": {}},
            )

    def test_dispatched_intent_is_not_overwritten_by_stale_projection(self):
        self.assertFalse(
            services._intent_state_can_be_marked_stale("dispatched")
        )
        self.assertTrue(
            services._intent_state_can_be_marked_stale("approved")
        )

    def test_reject_and_revoke_do_not_depend_on_current_target_snapshot(self):
        self.assertFalse(
            services._approval_requires_current_target_snapshot(
                Approval.Decision.REJECTED,
                PublicationAction.UPDATE,
            )
        )
        self.assertFalse(
            services._approval_requires_current_target_snapshot(
                Approval.Decision.REVOKED,
                PublicationAction.CREATE,
            )
        )
        self.assertTrue(
            services._approval_requires_current_target_snapshot(
                Approval.Decision.APPROVED,
                PublicationAction.UPDATE,
            )
        )

    def test_content_attempt_uses_article_external_write_fence(self):
        self.assertTrue(
            services._requires_article_external_write_fence(
                PublicationAction.CREATE
            )
        )
        self.assertTrue(
            services._requires_article_external_write_fence(
                PublicationAction.MARK_WITHDRAWN
            )
        )
        self.assertFalse(
            services._requires_article_external_write_fence(
                PublicationAction.UNPUBLISH
            )
        )

    def test_admin_edit_requires_exact_revalidation_event_and_receipt(self):
        revision = _revision(
            provenance_kind="admin_edit",
            article_id="article-1",
            article=SimpleNamespace(source_run_id="run-1"),
            editorial_policy_version="policy-1",
            editorial_policy_snapshot_id="policy-snapshot-1",
            revalidation_event_key="event-1",
            revalidation_lease_token="lease-1",
            _state=SimpleNamespace(db="default"),
        )
        event = SimpleNamespace(
            payload={
                "article_id": "article-1",
                "article_revision_id": "revision-1",
                "editorial_policy_snapshot_id": "policy-snapshot-1",
                "editorial_policy_material_hash": "policy-v1",
                "verification_manifest_hash": "verification-v1",
                "input_evidence_manifest_hash": "evidence-v1",
                "excluded_material_manifest_hash": "exclusion-v1",
            },
            policy_versions={
                "editorialPolicyVersion": "policy-1",
                "editorialPolicyHash": "policy-v1",
            },
            immutable_material_hash="event-material-v1",
        )
        receipt = SimpleNamespace(
            state=OutboxConsumerReceipt.State.SUCCEEDED,
            lease_generation=revision.revalidation_generation,
        )
        event_db = MagicMock()
        event_db.filter.return_value.first.return_value = event
        receipt_db = MagicMock()
        receipt_db.filter.return_value.first.return_value = receipt

        with (
            patch.object(
                OutboxMessage.objects,
                "using",
                return_value=event_db,
            ),
            patch.object(
                OutboxConsumerReceipt.objects,
                "using",
                return_value=receipt_db,
            ),
            patch(
                "wisdome_writer.infrastructure.outbox.compute_material_hash",
                return_value="event-material-v1",
            ),
            patch.object(services, "require_revision_publishable") as gate,
        ):
            services._require_revision_for_commands(
                revision, _commands(PublicationAction.CREATE)
            )
            receipt.lease_generation += 1
            services._require_revision_for_commands(
                revision, _commands(PublicationAction.CREATE)
            )

        self.assertEqual(gate.call_count, 2)

    def test_approval_is_append_only_through_the_orm(self):
        with self.assertRaisesRegex(TypeError, "append-only"):
            Approval.objects.none().update(
                decision=Approval.Decision.REVOKED
            )
        with self.assertRaisesRegex(TypeError, "append-only"):
            Approval.objects.none().delete()

    def test_open_intent_reference_lookup_is_sqlite_safe(self):
        query = MagicMock()
        query.filter.return_value = query
        query.order_by.return_value = [
            SimpleNamespace(
                target_snapshot_refs=[{"targetId": "target-1"}],
            ),
            SimpleNamespace(
                target_snapshot_refs=[{"targetId": "target-2"}],
            ),
        ]
        with patch.object(
            services.PublicationIntent.objects,
            "select_for_update",
            return_value=query,
        ):
            rows = services._lock_open_intents_referencing(
                field_name="target_snapshot_refs",
                reference={"targetId": "target-1"},
            )

        query.filter.assert_called_once_with(
            state__in=services._STALEABLE_INTENT_STATES,
        )
        self.assertEqual(len(rows), 1)

    def test_remote_url_persistence_accepts_only_http_or_https(self):
        self.assertEqual(
            services._validated_remote_url("https://example.test/post/1"),
            "https://example.test/post/1",
        )
        for value in (
            "javascript:alert(1)",
            "ftp://example.test/post/1",
            "https://user:secret@example.test/post/1",
            "https:///missing-host",
        ):
            with self.subTest(value=value), self.assertRaises(Conflict):
                services._validated_remote_url(value)

    def test_publication_attempts_project_the_origin_run_terminal_state(self):
        self.assertEqual(
            services._publication_run_terminal_state(
                ["succeeded", "succeeded"]
            ),
            ("completed", "not_required", None),
        )
        self.assertEqual(
            services._publication_run_terminal_state(
                ["succeeded", "manual_required"]
            ),
            ("failed", "manual_required", "publication_terminal_failure"),
        )
        self.assertIsNone(
            services._publication_run_terminal_state(
                ["succeeded", "reconciling"]
            )
        )

    def test_publication_delivery_dlq_has_a_domain_terminalizer(self):
        route = route_for("publication.requested", 1)

        self.assertEqual(
            route.terminal_task_name,
            "apps.publishing.tasks.finalize_publication_delivery_failure",
        )
        self.assertEqual(
            route.terminal_argument_keys,
            ("publication_attempt_id",),
        )
        self.assertTrue(callable(tasks.finalize_publication_delivery_failure))
