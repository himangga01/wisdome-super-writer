from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.collection import models as collection_models
from apps.collection import services as collection_services
from apps.audit.services import AuditContext
from apps.topics.models import SourceRegistrySnapshot, TopicPolicy
from apps.topics.services import registry_manifest_hash_for_memberships
from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash
from wisdome_writer.infrastructure.models import OutboxMessage
from wisdome_writer.infrastructure.outbox import enqueue_event


def _row(**values):
    return SimpleNamespace(**values)


class RunControlModelContractTests(SimpleTestCase):
    def test_run_control_decision_is_append_only_and_request_scoped(self):
        model = getattr(collection_models, "RunControlDecision")
        field_names = {field.name for field in model._meta.fields}

        self.assertTrue(
            {
                "run",
                "action",
                "scope",
                "request_key",
                "request_hash",
                "reauth_proof_id",
                "decided_by",
                "decided_at",
            }.issubset(field_names)
        )
        self.assertTrue(
            any(
                tuple(constraint.fields) == ("run", "request_key")
                for constraint in model._meta.constraints
            )
        )


class RunTerminalProjectionTests(SimpleTestCase):
    def _calculate(self, *, stop=False, steps=(), sources=(), publications=()):
        calculate = getattr(
            collection_services,
            "calculate_run_terminal_projection",
        )
        run = _row(
            stop_requested_at=(object() if stop else None),
            state="publishing",
        )
        return calculate(
            run,
            steps=list(steps),
            sources=list(sources),
            publications=list(publications),
        )

    def test_nonterminal_child_keeps_run_open(self):
        projection = self._calculate(
            steps=[_row(name="publish", attempt_no=1, state="running", error_code=None)],
            sources=[_row(id=uuid.uuid4(), state="succeeded", error_code=None)],
            publications=[_row(id=uuid.uuid4(), state="queued", error_code=None)],
        )

        self.assertIsNone(projection)

    def test_stop_wins_after_all_children_are_terminal(self):
        projection = self._calculate(
            stop=True,
            steps=[_row(name="publish", attempt_no=1, state="stopped", error_code="stop_requested")],
            sources=[_row(id=uuid.uuid4(), state="skipped", error_code="stop_requested")],
            publications=[_row(id=uuid.uuid4(), state="stale", error_code="stop_requested")],
        )

        self.assertEqual(projection.state, "stopped")
        self.assertEqual(projection.terminal_impact["failedStepCount"], 0)

    def test_all_source_failure_remains_failed(self):
        projection = self._calculate(
            steps=[_row(name="collect", attempt_no=1, state="failed", error_code="all_sources_failed")],
            sources=[_row(id=uuid.uuid4(), state="failed", error_code="source_failed")],
            publications=[],
        )

        self.assertEqual(projection.state, "failed")
        self.assertEqual(projection.terminal_impact["failedSourceCount"], 1)
        self.assertEqual(projection.error_summary["failures"][0]["scope"], "step")

    def test_completed_alias_and_successful_children_complete(self):
        projection = self._calculate(
            steps=[_row(name="publish", attempt_no=1, state="completed", error_code=None)],
            sources=[_row(id=uuid.uuid4(), state="succeeded", error_code=None)],
            publications=[_row(id=uuid.uuid4(), state="succeeded", error_code=None)],
        )

        self.assertEqual(projection.state, "completed")
        self.assertIsNone(projection.error_summary)


class SelectiveRetryScopeTests(SimpleTestCase):
    def test_exactly_one_supported_terminal_unit_is_required(self):
        normalize = getattr(
            collection_services,
            "normalize_selective_retry_scope",
        )
        source_id = uuid.uuid4()

        self.assertEqual(
            normalize({"sourceAttemptId": str(source_id)}),
            ("sourceAttemptId", source_id),
        )
        for invalid in (
            {},
            {"sourceAttemptId": str(source_id), "targetId": str(uuid.uuid4())},
            {"unknownId": str(source_id)},
        ):
            with self.assertRaises(ValueError):
                normalize(invalid)

    def test_request_hash_binds_action_scope_reason_and_actor(self):
        request_hash = getattr(
            collection_services,
            "run_control_request_hash",
        )
        run_id = uuid.uuid4()
        actor_id = uuid.uuid4()
        target_id = uuid.uuid4()
        base = request_hash(
            run_id=run_id,
            action="retry",
            scope={"targetId": str(target_id)},
            request_key="retry-one",
            reason="operator_retry",
            actor_id=actor_id,
        )

        self.assertEqual(len(base), 64)
        self.assertNotEqual(
            base,
            request_hash(
                run_id=run_id,
                action="retry",
                scope={"targetId": str(target_id)},
                request_key="retry-one",
                reason="different_reason",
                actor_id=actor_id,
            ),
        )


class RunControlServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            email="t028-run-control@example.com",
            password="not-a-real-secret",
            is_staff=True,
        )
        registry_hash = registry_manifest_hash_for_memberships([])
        cls.registry = SourceRegistrySnapshot.objects.create(
            topic_code="housing_subscription",
            version=1,
            state=SourceRegistrySnapshot.State.APPROVED,
            manifest_hash=registry_hash,
            approved_by=cls.user,
            approved_at=timezone.now(),
        )
        policy_document = {"allowedAuthorityTiers": ["primary_official"]}
        cls.policy = TopicPolicy.objects.create(
            code="housing_subscription",
            version=1,
            title="T028 policy",
            freshness_minutes=60,
            policy=policy_document,
            policy_hash=canonical_hash(
                policy_document,
                schema_version=CANONICAL_HASH_SCHEMA_V1,
            ),
        )

    def setUp(self):
        now = timezone.now()
        self.run = collection_models.CollectionRun.objects.create(
            display_id=f"RUN-T028-{uuid.uuid4().hex[:8]}",
            topic_code="housing_subscription",
            trigger="manual",
            approval_mode="manual",
            window_start=now - timedelta(hours=1),
            window_end=now,
            source_registry=self.registry,
            registry_manifest_hash=self.registry.manifest_hash,
            topic_policy=self.policy,
            policy_version=self.policy.version,
            policy_hash=self.policy.policy_hash,
            freshness_minutes=self.policy.freshness_minutes,
            allowed_authority_tiers=["primary_official"],
            freshness_cutoff=now - timedelta(hours=1),
            requested_target_ids=[],
            request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
            state=collection_models.RunState.DRAFTING,
            requested_by=self.user,
        )

    def test_control_decision_exact_replay_precedes_mutation(self):
        record = getattr(collection_services, "record_run_control_decision")
        kwargs = {
            "run_id": self.run.id,
            "action": "stop",
            "scope": {},
            "request_key": "stop-once",
            "reason": "operator_stop",
            "user": self.user,
        }

        first, created = record(**kwargs)
        replay, replay_created = record(**kwargs)

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first.id, replay.id)
        with self.assertRaises(ValueError):
            record(**{**kwargs, "reason": "changed_reason"})

        with self.assertRaises(TypeError):
            collection_models.RunControlDecision.objects.filter(
                pk=first.id
            ).update(action="retry")
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE collection_runcontroldecision "
                    "SET action = %s WHERE id = %s",
                    ["retry", first.id.hex],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM collection_runcontroldecision WHERE id = %s",
                    [first.id.hex],
                )

    def test_stop_records_decision_and_cancels_unclaimed_run_work(self):
        request_stop = getattr(collection_services, "request_run_stop")
        event = enqueue_event(
            event_type="run.draft_requested",
            aggregate_type="collection_run",
            aggregate_id=self.run.id,
            job_id=self.run.id,
            dedupe_key=f"t028-pending:{self.run.id}",
            payload={"run_id": str(self.run.id)},
            correlation_id=self.run.correlation_id,
        )

        decision, created = request_stop(
            run_id=self.run.id,
            request_key="stop-active",
            reason="operator_stop",
            user=self.user,
        )

        self.assertTrue(created)
        self.run.refresh_from_db()
        event.refresh_from_db()
        self.assertEqual(decision.action, "stop")
        self.assertEqual(self.run.state, collection_models.RunState.STOPPED)
        self.assertIsNotNone(self.run.stop_requested_at)
        self.assertEqual(event.status, OutboxMessage.Status.DEAD_LETTER)
        self.assertEqual(event.last_error_code, "run_stop_requested")

    def test_terminal_projection_releases_queue_one_after_commit(self):
        collection_models.RunStep.objects.create(
            run=self.run,
            name="editorial",
            attempt_no=1,
            correlation_id=self.run.correlation_id,
            state="succeeded",
        )
        audit_context = AuditContext.for_system(
            correlation_id=self.run.correlation_id,
            operation_key=f"run-terminal:{self.run.id}",
            reason_code="run_terminal_projection",
        )

        with (
            patch(
                "apps.scheduling.services.release_waiting_for_topic"
            ) as release,
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = getattr(
                collection_services,
                "project_collection_run_terminal",
            )(self.run.id, audit_context=audit_context)

        result.refresh_from_db()
        self.assertEqual(result.state, collection_models.RunState.COMPLETED)
        release.assert_called_once_with(
            self.run.topic_code,
            audit_context=audit_context,
        )
