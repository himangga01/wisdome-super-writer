from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection, connections

from apps.publishing import services
from apps.publishing.models import Publication, PublicationIntent
from tests.unit import test_publication_approval_db
from tests.unit.test_publication_intent_idempotency_db import (
    PublicationIdentityGuardedTransactionTestCase,
)


class PublicationIntentServiceConcurrencyTests(
    PublicationIdentityGuardedTransactionTestCase
):
    def setUp(self):
        if connection.vendor != "sqlite":
            self.skipTest("SQLite busy convergence is SQLite-specific")
        fixture_case = test_publication_approval_db.PublicationApprovalSQLiteTriggerTests
        fixture_case.setUpTestData()
        self.fixture = fixture_case.fixture
        target = self.fixture.target
        target.current_snapshot_id = self.fixture.snapshot.id
        target.current_config_hash = self.fixture.snapshot.config_hash
        target.capabilities = {"create": True}
        target.publisher_contract_version = "publisher-v1"
        target.publisher_adapter_manifest_hash = (
            self.fixture.snapshot.publisher_adapter_manifest_hash
        )
        target.save(
            update_fields=(
                "current_snapshot_id",
                "current_config_hash",
                "capabilities",
                "publisher_contract_version",
                "publisher_adapter_manifest_hash",
                "updated_at",
            )
        )
        self.fixture.intent.state = PublicationIntent.State.APPROVED
        self.fixture.intent.save(update_fields=("state",))

    def test_same_intent_request_converges_for_two_sqlite_callers(self):
        root = self.fixture.intent
        revision = self.fixture.revision
        article = revision.article
        reason = "Concurrent publication intent request."
        request_key = "intent-concurrent-same-request"
        data = {
            "revisionNo": revision.revision_no,
            "expectedRevisionContentHash": revision.content_hash,
            "correctionCaseId": None,
            "targetSnapshots": [
                {
                    "targetId": str(self.fixture.target.id),
                    "targetSnapshotId": str(self.fixture.snapshot.id),
                    "targetConfigHash": self.fixture.snapshot.config_hash,
                }
            ],
            "targetCommands": [
                {
                    "targetId": str(self.fixture.target.id),
                    "targetSnapshotId": str(self.fixture.snapshot.id),
                    "targetConfigHash": self.fixture.snapshot.config_hash,
                    "resolvedAction": "create",
                    "canonicalDependencyTargetId": None,
                }
            ],
            "approvalMode": "manual",
            "autoPublishValidationRefs": [],
            "autoPublishActivationRefs": [],
            "expectedLatestIntentId": str(root.id),
            "requestKey": request_key,
            "reason": reason,
        }
        context = SimpleNamespace(
            actor_type="admin",
            actor_id=self.fixture.user.id,
            event_key=None,
            request_key=request_key,
            reason_code=reason,
            correlation_id=uuid.uuid4(),
        )
        material = {
            "revisionContentHash": revision.content_hash,
            "generationAttemptId": None,
            "inputEvidenceManifestHash": root.input_evidence_manifest_hash,
            "generationPipelineManifestHash": None,
            "qualityGateManifestHash": root.quality_gate_manifest_hash,
            "qualityReportHash": root.quality_report_hash,
            "editorialPolicyHash": revision.editorial_policy_hash,
            "verificationManifestHash": revision.input_manifest_hash,
            "exclusionManifestHash": "a" * 64,
            "claimManifestHash": revision.claim_manifest_hash,
            "revalidationGeneration": 0,
        }
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, bool] | tuple[str, str]] = []
        outcome_lock = threading.Lock()

        def create() -> None:
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                row, created = services.create_publication_intent(
                    str(article.id),
                    data,
                    user=self.fixture.user,
                    audit_context=context,
                )
                outcome: tuple[str, bool] | tuple[str, str] = (
                    str(row.id),
                    created,
                )
            except Exception as exc:  # captured so both thread outcomes are asserted
                outcome = (
                    type(exc).__name__,
                    f"{exc}; cause={exc.__cause__!r}",
                )
            finally:
                connections.close_all()
            with outcome_lock:
                outcomes.append(outcome)

        with (
            patch.object(services, "_require_publication_targets_exist"),
            patch.object(services, "_lock_article_external_write_fence"),
            patch.object(services, "_lock_target_intent_fences"),
            patch.object(services, "_current_revision", return_value=(article, revision)),
            patch.object(services, "_revision_publication_material", return_value=material),
            patch.object(services, "_create_preview_render"),
            patch.object(services, "_record_publishing_audit"),
            patch.object(services, "require_audit_replay"),
        ):
            threads = [threading.Thread(target=create) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(outcomes), 2)
        intent_ids = {row[0] for row in outcomes}
        self.assertEqual(len(intent_ids), 1, outcomes)
        self.assertEqual(sorted(row[1] for row in outcomes), [False, True])
        self.assertEqual(
            PublicationIntent.objects.filter(
                article_id=article.id,
                request_key=request_key,
            ).count(),
            1,
        )

    def test_same_dispatch_request_converges_for_two_sqlite_callers(self):
        intent = self.fixture.intent
        target = self.fixture.target
        publication = Publication.objects.create(
            article_id=intent.article_id,
            target=target,
            origin_target_snapshot_id=self.fixture.snapshot.id,
            remote_lookup_key=f"dispatch-concurrency-{uuid.uuid4()}",
        )
        reason = "Concurrent publication dispatch request."
        request_key = "dispatch-concurrent-same-request"
        data = {
            "revisionNo": intent.revision_no,
            "publicationIntentId": str(intent.id),
            "targetIds": [str(target.id)],
            "expectedTargetSnapshots": intent.target_snapshot_refs,
            "publishAt": None,
            "requestKey": request_key,
            "reason": reason,
        }
        context = SimpleNamespace(
            actor_type="admin",
            actor_id=self.fixture.user.id,
            event_key=None,
            request_key=request_key,
            reason_code=reason,
            correlation_id=uuid.uuid4(),
        )
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, bool] | tuple[str, str]] = []
        outcome_lock = threading.Lock()

        def dispatch() -> None:
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                result, created = services.dispatch_publication(
                    str(intent.article_id),
                    data,
                    audit_context=context,
                )
                outcome: tuple[str, bool] | tuple[str, str] = (
                    str(result.dispatch.id),
                    created,
                )
            except Exception as exc:  # captured so both thread outcomes are asserted
                outcome = (
                    type(exc).__name__,
                    f"{exc}; cause={exc.__cause__!r}",
                )
            finally:
                connections.close_all()
            with outcome_lock:
                outcomes.append(outcome)

        with (
            patch.object(services, "_require_publication_targets_exist"),
            patch.object(services, "_lock_article_external_write_fence"),
            patch.object(services, "_lock_target_intent_fences"),
            patch.object(
                services,
                "_command_map",
                return_value={str(target.id): intent.target_commands[0]},
            ),
            patch.object(
                services,
                "_require_intent_revision_publishable",
                return_value=intent,
            ),
            patch.object(
                services,
                "_require_current_dispatch_approval_locked",
                return_value=self.fixture.root,
            ),
            patch.object(
                services,
                "_latest_approval_locked",
                return_value=self.fixture.root,
            ),
            patch.object(services, "_publication_for", return_value=publication),
            patch.object(services, "_mark_origin_run_publishing_locked"),
            patch.object(services, "_queue_attempt_on_commit"),
            patch.object(services, "_record_publishing_audit"),
            patch.object(services, "require_audit_replay"),
        ):
            threads = [threading.Thread(target=dispatch) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(outcomes), 2)
        self.assertFalse(
            any(row[0] in {"Conflict", "IntegrityError", "OperationalError"} for row in outcomes),
            outcomes,
        )
        dispatch_ids = {row[0] for row in outcomes}
        self.assertEqual(len(dispatch_ids), 1, outcomes)
        self.assertEqual(sorted(row[1] for row in outcomes), [False, True])
