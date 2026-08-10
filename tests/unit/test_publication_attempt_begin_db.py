from __future__ import annotations

import uuid
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.audit.services import AuditContext
from apps.publishing import models as publishing_models
from apps.publishing import services
from apps.publishing.contracts import PublishResult
from tests.unit import test_publication_approval_db as approval_db
from wisdome_writer.infrastructure.models import (
    OutboxConsumerReceipt,
    OutboxMessage,
)


class PublicationAttemptBeginSQLiteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        approval_db.PublicationApprovalSQLiteTriggerTests.setUpTestData()
        cls.fixture = approval_db.PublicationApprovalSQLiteTriggerTests.fixture
        cls.fixture.intent.state = publishing_models.PublicationIntent.State.APPROVED
        cls.fixture.intent.save(update_fields=("state",))

    def _attempt(self):
        publication = publishing_models.Publication.objects.create(
            article_id=self.fixture.intent.article_id,
            target=self.fixture.target,
            origin_target_snapshot_id=self.fixture.snapshot.id,
            remote_lookup_key=f"t021-begin-{uuid.uuid4()}",
        )
        return publishing_models.PublicationAttempt.objects.create(
            publication=publication,
            article_revision=self.fixture.revision,
            publication_intent=self.fixture.intent,
            target_snapshot=self.fixture.snapshot,
            target_config_hash=self.fixture.snapshot.config_hash,
            resolved_action="create",
            target_command_hash="7" * 64,
            publisher_contract_version="publisher-v1",
            publisher_adapter_manifest_hash="5" * 64,
            approval=self.fixture.root,
            approval_subject_hash=self.fixture.subject_hash,
            idempotency_key=f"t021-begin-{uuid.uuid4()}",
            remote_lookup_key=publication.remote_lookup_key,
            request_fingerprint="2" * 64,
            correlation_id=uuid.uuid4(),
        )

    def test_begin_inserts_observation_before_db_managed_parent_advance(self):
        attempt = self._attempt()
        event = OutboxMessage.objects.create(
            message_key=f"publication.requested:{attempt.id}:1",
            topic="publication.requested",
            event_version=2,
            aggregate_type="publication_attempt",
            aggregate_id=attempt.id,
            payload={
                "publication_attempt_id": str(attempt.id),
                "execution_attempt_no": 1,
            },
            correlation_id=attempt.correlation_id,
            job_id=attempt.id,
            immutable_material_hash="a" * 64,
        )
        token = uuid.uuid4()
        receipt = OutboxConsumerReceipt.objects.create(
            event=event,
            consumer_name="publication-execute",
            state=OutboxConsumerReceipt.State.PROCESSING,
            attempts=1,
            claimed_at=timezone.now(),
            claimed_until=timezone.now() + timezone.timedelta(minutes=5),
            lease_token=token,
            lease_generation=1,
        )
        context = AuditContext.for_worker(
            correlation_id=attempt.correlation_id,
            event_key=str(event.id),
            consumer_name="publication-execute",
            lease_token=token,
            lease_generation=1,
            reason_code="publication execution",
        )
        command = object()

        with (
            patch.object(services, "validate_attempt_gate"),
            patch.object(services, "_final_render", return_value=None),
            patch.object(services, "_command_for_attempt", return_value=command),
            patch.object(services, "_set_article_external_write_fence_locked"),
            patch.object(services, "_record_publishing_audit"),
        ):
            observed, fence, observed_command = services._begin_attempt_locked(
                str(attempt.id),
                execution_attempt_no=1,
                audit_context=context,
            )

        observed.refresh_from_db()
        self.assertEqual(observed.state, publishing_models.PublicationAttempt.State.RUNNING)
        self.assertEqual(observed.execution_generation, 1)
        self.assertEqual(observed.active_source_event_id, event.id)
        self.assertEqual(observed.active_consumer_lease_token, token)
        self.assertEqual(
            observed.active_consumer_lease_generation,
            receipt.lease_generation,
        )
        self.assertEqual(fence.execution_generation, 1)
        self.assertIs(observed_command, command)
        self.assertEqual(
            publishing_models.PublicationExecutionObservation.objects.filter(
                publication_attempt=observed,
                execution_generation=1,
            ).count(),
            1,
        )

    def test_expired_prewrite_reclaim_clears_parent_envelope_before_stale_gate(self):
        attempt = self._attempt()
        event = OutboxMessage.objects.create(
            message_key=f"publication.requested:{attempt.id}:1",
            topic="publication.requested",
            event_version=2,
            aggregate_type="publication_attempt",
            aggregate_id=attempt.id,
            payload={
                "publication_attempt_id": str(attempt.id),
                "execution_attempt_no": 1,
            },
            correlation_id=attempt.correlation_id,
            job_id=attempt.id,
            immutable_material_hash="a" * 64,
        )
        first_token = uuid.uuid4()
        first_expiry = timezone.now() + timezone.timedelta(minutes=1)
        receipt = OutboxConsumerReceipt.objects.create(
            event=event,
            consumer_name="publication-execute",
            state=OutboxConsumerReceipt.State.PROCESSING,
            attempts=1,
            claimed_at=timezone.now(),
            claimed_until=first_expiry,
            lease_token=first_token,
            lease_generation=1,
        )
        first_context = AuditContext.for_worker(
            correlation_id=attempt.correlation_id,
            event_key=str(event.id),
            consumer_name="publication-execute",
            lease_token=first_token,
            lease_generation=1,
            reason_code="publication execution",
        )
        with (
            patch.object(services, "validate_attempt_gate"),
            patch.object(services, "_final_render", return_value=None),
            patch.object(services, "_command_for_attempt", return_value=object()),
            patch.object(services, "_set_article_external_write_fence_locked"),
            patch.object(services, "_record_publishing_audit"),
        ):
            services._begin_attempt_locked(
                str(attempt.id),
                execution_attempt_no=1,
                audit_context=first_context,
            )

        second_token = uuid.uuid4()
        receipt.lease_generation = 2
        receipt.lease_token = second_token
        receipt.claimed_until = timezone.now() + timezone.timedelta(minutes=5)
        receipt.save(
            update_fields=("lease_generation", "lease_token", "claimed_until")
        )
        second_context = AuditContext.for_worker(
            correlation_id=attempt.correlation_id,
            event_key=str(event.id),
            consumer_name="publication-execute",
            lease_token=second_token,
            lease_generation=2,
            reason_code="publication execution",
        )
        after_first_expiry = first_expiry + timezone.timedelta(seconds=1)

        with (
            patch.object(services.timezone, "now", return_value=after_first_expiry),
            patch.object(
                services,
                "validate_attempt_gate",
                side_effect=services.Conflict("stopped"),
            ),
            patch.object(services, "_record_publishing_audit"),
            patch.object(services, "_release_article_external_write_fence_locked"),
        ):
            observed, fence, command = services._begin_attempt_locked(
                str(attempt.id),
                execution_attempt_no=1,
                audit_context=second_context,
            )

        observed.refresh_from_db()
        self.assertEqual(observed.state, publishing_models.PublicationAttempt.State.STALE)
        self.assertIsNone(observed.active_source_event_id)
        self.assertIsNone(observed.active_consumer_lease_token)
        self.assertEqual(observed.active_consumer_lease_generation, 0)
        self.assertIsNone(fence)
        self.assertIsNone(command)

    def test_reconcile_claim_inserts_delivery_observation_before_parent_projection(self):
        attempt = self._attempt()
        attempt.state = publishing_models.PublicationAttempt.State.UNKNOWN_OUTCOME
        attempt.recovery_state = publishing_models.PublicationRecoveryState.RECONCILING
        attempt.save(update_fields=("state", "recovery_state"))
        attempt.publication.state = publishing_models.Publication.State.RECONCILING
        attempt.publication.remote_state = publishing_models.Publication.RemoteState.UNKNOWN
        attempt.publication.save(update_fields=("state", "remote_state", "updated_at"))
        event = OutboxMessage.objects.create(
            message_key=f"publication.reconcile_requested:{attempt.id}:1",
            topic="publication.reconcile_requested",
            event_version=2,
            aggregate_type="publication_attempt",
            aggregate_id=attempt.id,
            payload={
                "publication_attempt_id": str(attempt.id),
                "reconcile_attempt_no": 1,
            },
            correlation_id=attempt.correlation_id,
            job_id=attempt.id,
            immutable_material_hash="b" * 64,
        )
        token = uuid.uuid4()
        receipt = OutboxConsumerReceipt.objects.create(
            event=event,
            consumer_name="publication-reconcile",
            state=OutboxConsumerReceipt.State.PROCESSING,
            attempts=1,
            claimed_at=timezone.now(),
            claimed_until=timezone.now() + timezone.timedelta(minutes=5),
            lease_token=token,
            lease_generation=1,
        )
        context = AuditContext.for_worker(
            correlation_id=attempt.correlation_id,
            event_key=str(event.id),
            consumer_name="publication-reconcile",
            lease_token=token,
            lease_generation=1,
            reason_code="publication reconcile",
        )
        command = object()

        with (
            patch.object(services, "_record_publishing_audit"),
            patch.object(services, "_worker_audit_replay", return_value=None),
            patch.object(services, "_final_render", return_value=None),
            patch.object(services, "_command_for_attempt", return_value=command),
        ):
            observed, generation, observed_command = services.begin_reconcile(
                str(attempt.id),
                expected_reconcile_attempt_no=1,
                source_event_id=event.id,
                audit_context=context,
            )

        generation.refresh_from_db()
        delivery = publishing_models.PublicationReconcileDeliveryObservation.objects.get(
            reconcile_generation=generation,
            consumer_lease_generation=receipt.lease_generation,
        )
        self.assertEqual(delivery.state, delivery.State.ACTIVE)
        self.assertEqual(delivery.consumer_lease_token, token)
        self.assertEqual(generation.state, generation.State.RUNNING)
        self.assertEqual(generation.consumer_lease_token, token)
        self.assertEqual(observed.state, publishing_models.PublicationAttempt.State.RECONCILING)
        self.assertIs(observed_command, command)

        receipt.claimed_until = timezone.now() - timezone.timedelta(seconds=1)
        receipt.save(update_fields=("claimed_until",))
        with (
            patch.object(services, "_record_publishing_audit"),
            patch.object(services, "_release_article_external_write_fence_locked"),
        ):
            settled = services.persist_publish_result(
                str(attempt.id),
                PublishResult(
                    status="manual_required",
                    remote_state="unknown",
                    error_code="remote_projection_mismatch",
                ),
                expected_reconcile_generation=1,
                expected_reconcile_event_id=event.id,
                audit_context=context,
            )

        settled.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(
            settled.state,
            publishing_models.PublicationAttempt.State.MANUAL_REQUIRED,
        )
        self.assertEqual(delivery.state, delivery.State.SETTLED)
