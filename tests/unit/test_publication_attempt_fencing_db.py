import importlib
import inspect
import threading
import time
import uuid
from unittest import TestCase as UnitTestCase

from django.db import (
    IntegrityError,
    OperationalError,
    connection,
    connections,
    transaction,
)
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.models import PROTECT
from django.test import TestCase
from django.utils import timezone

from apps.publishing import models as publishing_models
from wisdome_writer.infrastructure.models import (
    OutboxConsumerReceipt,
    OutboxMessage,
)
from tests.unit.test_publication_approval_db import (
    ApprovalGuardedTransactionTestCase,
)


class PublicationAttemptFencingModelContractTests(UnitTestCase):
    def test_consumer_receipt_exposes_terminal_reservation_identity(self):
        self.assertLessEqual(
            {
                "terminal_reserved_at",
                "terminal_lease_generation",
                "terminal_lease_token",
                "terminal_lease_token_hash",
                "terminal_error_code",
            },
            {field.name for field in OutboxConsumerReceipt._meta.get_fields()},
        )

    def test_attempt_execution_and_reconcile_models_expose_frozen_fence_identity(self):
        attempt = publishing_models.PublicationAttempt
        self.assertLessEqual(
            {
                "execution_identity_version",
                "execution_generation",
                "active_source_event",
                "active_consumer_name",
                "active_consumer_lease_generation",
                "active_consumer_lease_token",
                "active_lease_token_hash",
                "active_lease_expires_at",
                "active_write_marker",
                "active_write_started_at",
                "terminal_event_key",
                "terminal_generation",
                "terminal_state",
            },
            {field.name for field in attempt._meta.get_fields()},
        )
        self.assertIs(
            attempt._meta.get_field("active_source_event").remote_field.on_delete,
            PROTECT,
        )

        execution = publishing_models.PublicationExecutionObservation
        self.assertLessEqual(
            {
                "identity_version",
                "execution_generation",
                "consumer_name",
                "consumer_lease_generation",
                "consumer_lease_token",
                "lease_token_hash",
                "source_event",
                "lease_expires_at",
                "write_marker",
                "external_write_started_at",
                "state",
                "result_identity",
                "projection_disposition",
            },
            {field.name for field in execution._meta.get_fields()},
        )

        self.assertTrue(hasattr(publishing_models, "PublicationLateExecutionResult"))
        late = publishing_models.PublicationLateExecutionResult
        self.assertIs(
            late._meta.get_field("execution_observation").remote_field.on_delete,
            PROTECT,
        )
        self.assertTrue(late._meta.get_field("execution_observation").null)
        self.assertIs(
            late._meta.get_field("reconcile_generation").remote_field.on_delete,
            PROTECT,
        )
        self.assertLessEqual(
            {
                "consumer_name",
                "consumer_lease_generation",
                "consumer_lease_token",
                "lease_token_hash",
                "source_event",
            },
            {field.name for field in late._meta.get_fields()},
        )

        reconcile = publishing_models.PublicationReconcileGeneration
        self.assertLessEqual(
            {
                "delivery_identity_version",
                "consumer_name",
                "consumer_lease_generation",
                "consumer_lease_token",
                "lease_token_hash",
                "lease_expires_at",
            },
            {field.name for field in reconcile._meta.get_fields()},
        )
        self.assertEqual(
            {value for value, _label in reconcile.State.choices},
            {"queued", "running", "completed", "delivery_failed"},
        )

        delivery = publishing_models.PublicationReconcileDeliveryObservation
        self.assertLessEqual(
            {
                "reconcile_generation",
                "consumer_name",
                "consumer_lease_generation",
                "consumer_lease_token",
                "lease_token_hash",
                "lease_expires_at",
                "state",
                "started_at",
                "finished_at",
            },
            {field.name for field in delivery._meta.get_fields()},
        )
        self.assertIs(
            late._meta.get_field(
                "reconcile_delivery_observation"
            ).remote_field.on_delete,
            PROTECT,
        )


class PublicationAttemptFencingMigrationContractTests(UnitTestCase):
    def test_0003_postgresql_and_sqlite_terminal_reservation_guards_match(self):
        migration = importlib.import_module(
            "wisdome_writer.infrastructure.migrations.0003_outbox_terminal_reservation"
        )
        sqlite_sql = "\n".join(migration.SQLITE_GUARD_SQL)
        postgres_sql = "\n".join(migration.POSTGRES_GUARD_SQL)

        for sql in (sqlite_sql, postgres_sql):
            self.assertIn("terminal_reserved_at", sql)
            self.assertIn("terminal_lease_generation", sql)
            self.assertIn("terminal_lease_token_hash", sql)
            self.assertIn("terminal_error_code", sql)
            self.assertIn("state = 'succeeded'", sql)
            self.assertIn("state <> 'processing'", sql)

    def test_0011_postgresql_and_sqlite_guards_cover_execution_and_reconcile_fences(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0011_publication_attempt_fencing"
        )
        sqlite_sql = "\n".join(migration.SQLITE_GUARD_SQL)
        postgres_sql = "\n".join(migration.POSTGRES_GUARD_SQL)

        for sql in (sqlite_sql, postgres_sql):
            self.assertIn("publishing_publicationattempt", sql)
            self.assertIn("publishing_publicationexecutionobservation", sql)
            self.assertIn("publishing_publicationlateexecutionresult", sql)
            self.assertIn("publishing_publicationreconcilegeneration", sql)
            self.assertIn("infrastructure_outboxmessage", sql)
            self.assertIn("infrastructure_outboxconsumerreceipt", sql)
            self.assertIn("publication.requested", sql)
            self.assertIn("publication.reconcile_requested", sql)
            self.assertIn("lease_generation", sql)
            self.assertIn("terminal projection is immutable", sql)
            self.assertIn("late execution result is append-only", sql)

        self.assertIn("FOR UPDATE", postgres_sql)
        self.assertIn("IS DISTINCT FROM", postgres_sql)
        self.assertIn("OLD.state = 'queued'", postgres_sql)
        self.assertIn("IF NEW.state = 'running'", postgres_sql)
        self.assertIn("OLD.state = 'running'", postgres_sql)

        self.assertIn("julianday('now')", sqlite_sql)
        self.assertNotIn("CURRENT_TIMESTAMP", sqlite_sql)
        self.assertIn("clock_timestamp()", postgres_sql)
        self.assertNotIn("CURRENT_TIMESTAMP", postgres_sql)
        self.assertIn("publishing_publicationreconciledeliveryobservation", sqlite_sql)
        self.assertIn("publishing_publicationreconciledeliveryobservation", postgres_sql)
        self.assertNotIn(
            "now",
            inspect.signature(migration._active_receipt).parameters,
        )

    def test_0011_legacy_terminal_identity_is_explicitly_non_replayable(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0011_publication_attempt_fencing"
        )
        self.assertEqual(
            migration.LEGACY_UNVERIFIABLE_IDENTITY_VERSION,
            "legacy-unverifiable-v1",
        )


class PublicationAttemptFencingSQLiteGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from tests.unit.test_publication_approval_db import (
            PublicationApprovalSQLiteTriggerTests,
        )

        PublicationApprovalSQLiteTriggerTests.setUpTestData()
        cls.fixture = PublicationApprovalSQLiteTriggerTests.fixture
        cls.fixture.intent.state = publishing_models.PublicationIntent.State.APPROVED
        cls.fixture.intent.save(update_fields=("state",))

    def _publication(self):
        return publishing_models.Publication.objects.create(
            article_id=self.fixture.intent.article_id,
            target=self.fixture.target,
            origin_target_snapshot_id=self.fixture.snapshot.id,
            remote_lookup_key=f"t021-{uuid.uuid4()}",
        )

    def _attempt(self):
        publication = self._publication()
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
            idempotency_key=f"t021-attempt-{uuid.uuid4()}",
            remote_lookup_key=publication.remote_lookup_key,
            request_fingerprint="2" * 64,
            correlation_id=uuid.uuid4(),
        )

    def _event(self, attempt, *, topic, counter_name, counter):
        event = OutboxMessage.objects.create(
            message_key=f"{topic}:{attempt.id}:{counter}:{uuid.uuid4()}",
            topic=topic,
            event_version=2,
            aggregate_type="publication_attempt",
            aggregate_id=attempt.id,
            payload={
                "publication_attempt_id": str(attempt.id),
                counter_name: counter,
            },
            correlation_id=attempt.correlation_id,
            job_id=attempt.id,
            immutable_material_hash="a" * 64,
        )
        token = uuid.uuid4()
        claimed_until = timezone.now() + timezone.timedelta(minutes=5)
        receipt = OutboxConsumerReceipt.objects.create(
            event=event,
            consumer_name=(
                "publication-execute"
                if topic == "publication.requested"
                else "publication-reconcile"
            ),
            state=OutboxConsumerReceipt.State.PROCESSING,
            attempts=1,
            claimed_at=timezone.now(),
            claimed_until=claimed_until,
            lease_token=token,
            lease_generation=counter,
        )
        token_hash = importlib.import_module(
            "apps.publishing.migrations.0011_publication_attempt_fencing"
        )._lease_token_hash(token)
        return event, receipt, token_hash

    def _observation(self, attempt, event, receipt, token_hash, *, generation):
        return publishing_models.PublicationExecutionObservation.objects.create(
            publication_attempt=attempt,
            execution_attempt_no=attempt.attempt_no,
            execution_generation=generation,
            correlation_id=event.correlation_id,
            source_event=event,
            consumer_name="publication-execute",
            consumer_lease_generation=receipt.lease_generation,
            consumer_lease_token=receipt.lease_token,
            lease_token_hash=token_hash,
            lease_expires_at=receipt.claimed_until,
            write_marker=str(generation) * 64,
            started_at=timezone.now(),
        )

    def test_execution_generation_reclaim_keeps_business_attempt_number(self):
        attempt = self._attempt()
        event, receipt, token_hash = self._event(
            attempt,
            topic="publication.requested",
            counter_name="execution_attempt_no",
            counter=1,
        )
        first = self._observation(attempt, event, receipt, token_hash, generation=1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, "running")
        self.assertEqual(attempt.execution_generation, 1)
        self.assertEqual(attempt.active_consumer_lease_token, receipt.lease_token)
        publishing_models.PublicationExecutionObservation.objects.filter(
            id=first.id
        ).update(
            state="delivery_unknown",
            finished_at=timezone.now(),
            projection_disposition="no_result",
        )
        publishing_models.PublicationAttempt.objects.filter(id=attempt.id).update(
            state="retryable_failed",
            active_source_event=None,
            active_consumer_name="",
            active_consumer_lease_generation=0,
            active_consumer_lease_token=None,
            active_lease_token_hash="",
            active_lease_expires_at=None,
            active_write_marker="",
            active_write_started_at=None,
        )

        second_event, second_receipt, second_hash = self._event(
            attempt,
            topic="publication.requested",
            counter_name="execution_attempt_no",
            counter=1,
        )
        second = self._observation(
            attempt,
            second_event,
            second_receipt,
            second_hash,
            generation=2,
        )

        self.assertEqual(second.execution_attempt_no, 1)
        self.assertEqual(second.execution_generation, 2)
        attempt.refresh_from_db()
        self.assertEqual(attempt.execution_generation, 2)
        self.assertEqual(attempt.active_consumer_lease_token, second_receipt.lease_token)

    def test_execution_observation_rejects_a_token_different_from_the_receipt(self):
        attempt = self._attempt()
        event, receipt, token_hash = self._event(
            attempt,
            topic="publication.requested",
            counter_name="execution_attempt_no",
            counter=1,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            publishing_models.PublicationExecutionObservation.objects.create(
                publication_attempt=attempt,
                execution_attempt_no=1,
                execution_generation=1,
                correlation_id=event.correlation_id,
                source_event=event,
                consumer_name="publication-execute",
                consumer_lease_generation=receipt.lease_generation,
                consumer_lease_token=uuid.uuid4(),
                lease_token_hash=token_hash,
                lease_expires_at=receipt.claimed_until,
                write_marker="f" * 64,
                started_at=timezone.now(),
            )

    def test_migration_active_receipt_rechecks_expiry_inside_the_transaction(self):
        attempt = self._attempt()
        event, receipt, _ = self._event(
            attempt,
            topic="publication.requested",
            counter_name="execution_attempt_no",
            counter=1,
        )
        receipt.claimed_until = timezone.now() + timezone.timedelta(
            milliseconds=100
        )
        receipt.save(update_fields=("claimed_until",))
        migration = importlib.import_module(
            "apps.publishing.migrations.0011_publication_attempt_fencing"
        )
        with transaction.atomic():
            self.assertIsNotNone(
                migration._active_receipt(
                    OutboxConsumerReceipt,
                    event=event,
                    consumer_name="publication-execute",
                    alias="default",
                )
            )
            time.sleep(0.15)
            self.assertIsNone(
                migration._active_receipt(
                    OutboxConsumerReceipt,
                    event=event,
                    consumer_name="publication-execute",
                    alias="default",
                )
            )

    def test_reconcile_insert_auto_advances_parent_and_running_lease_can_be_reclaimed(self):
        attempt = self._attempt()
        event, receipt, token_hash = self._event(
            attempt,
            topic="publication.reconcile_requested",
            counter_name="reconcile_attempt_no",
            counter=1,
        )
        generation = publishing_models.PublicationReconcileGeneration.objects.create(
            publication_attempt=attempt,
            generation=1,
            source_event=event,
            not_before=event.not_before,
            started_at=event.occurred_at,
            correlation_id=event.correlation_id,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.reconcile_attempt_no, 1)
        receipt.claimed_until = timezone.now() + timezone.timedelta(
            milliseconds=50
        )
        receipt.save(update_fields=("claimed_until",))
        publishing_models.PublicationReconcileDeliveryObservation.objects.create(
            reconcile_generation=generation,
            consumer_name="publication-reconcile",
            consumer_lease_generation=receipt.lease_generation,
            consumer_lease_token=receipt.lease_token,
            lease_token_hash=token_hash,
            lease_expires_at=receipt.claimed_until,
            started_at=timezone.now(),
        )
        time.sleep(0.1)
        next_token = uuid.uuid4()
        next_expiry = timezone.now() + timezone.timedelta(minutes=5)
        OutboxConsumerReceipt.objects.filter(id=receipt.id).update(
            lease_generation=receipt.lease_generation + 1,
            lease_token=next_token,
            claimed_until=next_expiry,
        )
        publishing_models.PublicationReconcileDeliveryObservation.objects.create(
            reconcile_generation=generation,
            consumer_name="publication-reconcile",
            consumer_lease_generation=receipt.lease_generation + 1,
            consumer_lease_token=next_token,
            lease_token_hash=importlib.import_module(
                "apps.publishing.migrations.0011_publication_attempt_fencing"
            )._lease_token_hash(next_token),
            lease_expires_at=next_expiry,
            started_at=timezone.now(),
        )
        generation.refresh_from_db()
        self.assertEqual(generation.consumer_lease_generation, 2)
        self.assertEqual(generation.consumer_lease_token, next_token)

    def test_terminal_reservation_is_complete_set_once_and_can_prove_delivery_failure(self):
        attempt = self._attempt()
        event, receipt, token_hash = self._event(
            attempt,
            topic="publication.reconcile_requested",
            counter_name="reconcile_attempt_no",
            counter=1,
        )
        generation = publishing_models.PublicationReconcileGeneration.objects.create(
            publication_attempt=attempt,
            generation=1,
            source_event=event,
            not_before=event.not_before,
            started_at=event.occurred_at,
            correlation_id=event.correlation_id,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            OutboxConsumerReceipt.objects.filter(id=receipt.id).update(
                terminal_reserved_at=timezone.now(),
            )
        reserved_at = timezone.now()
        OutboxConsumerReceipt.objects.filter(id=receipt.id).update(
            terminal_reserved_at=reserved_at,
            terminal_lease_generation=receipt.lease_generation,
            terminal_lease_token=receipt.lease_token,
            terminal_lease_token_hash=token_hash,
            terminal_error_code="consumer_attempts_exhausted",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            OutboxConsumerReceipt.objects.filter(id=receipt.id).update(
                terminal_error_code="different",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            OutboxConsumerReceipt.objects.filter(id=receipt.id).update(
                state="retry",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            OutboxConsumerReceipt.objects.filter(id=receipt.id).update(
                lease_generation=receipt.lease_generation + 1,
                lease_token=uuid.uuid4(),
            )
        publishing_models.PublicationReconcileGeneration.objects.filter(
            id=generation.id
        ).update(
            state="delivery_failed",
            completed_at=timezone.now(),
            error_code="consumer_attempts_exhausted",
            recovery_state="manual_required",
        )
        generation.refresh_from_db()
        self.assertEqual(generation.state, "delivery_failed")

        with self.assertRaises((IntegrityError, TypeError)), transaction.atomic():
            OutboxConsumerReceipt.objects.filter(id=receipt.id).delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM infrastructure_outboxconsumerreceipt WHERE id = %s",
                    [receipt.id.hex],
                )

    def test_running_attempt_requires_exact_active_observation(self):
        attempt = self._attempt()
        with self.assertRaises(IntegrityError), transaction.atomic():
            publishing_models.PublicationAttempt.objects.filter(id=attempt.id).update(
                state="running",
            )

    def test_reconcile_delivery_child_projects_parent_and_fences_late_result(self):
        attempt = self._attempt()
        event, receipt, token_hash = self._event(
            attempt,
            topic="publication.reconcile_requested",
            counter_name="reconcile_attempt_no",
            counter=1,
        )
        receipt.claimed_until = timezone.now() + timezone.timedelta(
            milliseconds=500
        )
        receipt.save(update_fields=("claimed_until",))
        generation = publishing_models.PublicationReconcileGeneration.objects.create(
            publication_attempt=attempt,
            generation=1,
            source_event=event,
            not_before=event.not_before,
            started_at=event.occurred_at,
            correlation_id=event.correlation_id,
        )
        delivery = (
            publishing_models.PublicationReconcileDeliveryObservation.objects.create(
                reconcile_generation=generation,
                consumer_name="publication-reconcile",
                consumer_lease_generation=receipt.lease_generation,
                consumer_lease_token=receipt.lease_token,
                lease_token_hash=token_hash,
                lease_expires_at=receipt.claimed_until,
                state="active",
                started_at=timezone.now(),
            )
        )
        generation.refresh_from_db()
        self.assertEqual(generation.state, "running")
        self.assertEqual(
            generation.consumer_lease_token,
            delivery.consumer_lease_token,
        )

        time.sleep(0.6)
        next_token = uuid.uuid4()
        next_hash = importlib.import_module(
            "apps.publishing.migrations.0011_publication_attempt_fencing"
        )._lease_token_hash(next_token)
        next_expiry = timezone.now() + timezone.timedelta(minutes=5)
        OutboxConsumerReceipt.objects.filter(id=receipt.id).update(
            lease_generation=receipt.lease_generation + 1,
            lease_token=next_token,
            claimed_until=next_expiry,
        )
        next_delivery = (
            publishing_models.PublicationReconcileDeliveryObservation.objects.create(
                reconcile_generation=generation,
                consumer_name="publication-reconcile",
                consumer_lease_generation=receipt.lease_generation + 1,
                consumer_lease_token=next_token,
                lease_token_hash=next_hash,
                lease_expires_at=next_expiry,
                state="active",
                started_at=timezone.now(),
            )
        )
        delivery.refresh_from_db()
        generation.refresh_from_db()
        self.assertEqual(delivery.state, "superseded")
        self.assertEqual(generation.consumer_lease_token, next_token)

        with self.assertRaises(IntegrityError), transaction.atomic():
            publishing_models.PublicationLateExecutionResult.objects.create(
                reconcile_generation=generation,
                reconcile_delivery_observation=next_delivery,
                source_event=event,
                consumer_name="publication-reconcile",
                consumer_lease_generation=receipt.lease_generation,
                consumer_lease_token=uuid.uuid4(),
                lease_token_hash="f" * 64,
                result_identity="e" * 64,
                result_state="succeeded",
            )

        late = publishing_models.PublicationLateExecutionResult.objects.create(
            reconcile_generation=generation,
            reconcile_delivery_observation=delivery,
            source_event=event,
            consumer_name=delivery.consumer_name,
            consumer_lease_generation=delivery.consumer_lease_generation,
            consumer_lease_token=delivery.consumer_lease_token,
            lease_token_hash=delivery.lease_token_hash,
            result_identity="d" * 64,
            result_state="succeeded",
        )
        self.assertEqual(late.reconcile_delivery_observation_id, delivery.id)

    def test_reconcile_result_settlement_ignores_expiry_for_exact_capability(self):
        attempt = self._attempt()
        event, receipt, token_hash = self._event(
            attempt,
            topic="publication.reconcile_requested",
            counter_name="reconcile_attempt_no",
            counter=1,
        )
        receipt.claimed_until = timezone.now() + timezone.timedelta(
            milliseconds=100
        )
        receipt.save(update_fields=("claimed_until",))
        generation = publishing_models.PublicationReconcileGeneration.objects.create(
            publication_attempt=attempt,
            generation=1,
            source_event=event,
            not_before=event.not_before,
            started_at=event.occurred_at,
            correlation_id=event.correlation_id,
        )
        delivery = (
            publishing_models.PublicationReconcileDeliveryObservation.objects.create(
                reconcile_generation=generation,
                consumer_name="publication-reconcile",
                consumer_lease_generation=receipt.lease_generation,
                consumer_lease_token=receipt.lease_token,
                lease_token_hash=token_hash,
                lease_expires_at=receipt.claimed_until,
                started_at=timezone.now(),
            )
        )
        time.sleep(0.15)
        publishing_models.PublicationReconcileGeneration.objects.filter(
            id=generation.id
        ).update(
            state="completed",
            completed_at=timezone.now(),
            result_identity="c" * 64,
            result_state="succeeded",
        )
        generation.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(generation.state, "completed")
        self.assertEqual(delivery.state, "settled")

    def test_late_reconcile_result_requires_exactly_one_parent(self):
        attempt = self._attempt()
        event, receipt, token_hash = self._event(
            attempt,
            topic="publication.reconcile_requested",
            counter_name="reconcile_attempt_no",
            counter=1,
        )
        generation = publishing_models.PublicationReconcileGeneration.objects.create(
            publication_attempt=attempt,
            generation=1,
            source_event=event,
            not_before=event.not_before,
            started_at=event.occurred_at,
            correlation_id=event.correlation_id,
        )
        receipt.claimed_until = timezone.now() + timezone.timedelta(
            milliseconds=50
        )
        receipt.save(update_fields=("claimed_until",))
        old_delivery = publishing_models.PublicationReconcileDeliveryObservation.objects.create(
            reconcile_generation=generation,
            consumer_name="publication-reconcile",
            consumer_lease_generation=receipt.lease_generation,
            consumer_lease_token=receipt.lease_token,
            lease_token_hash=token_hash,
            lease_expires_at=receipt.claimed_until,
            started_at=timezone.now(),
        )
        old_token = receipt.lease_token
        time.sleep(0.1)
        next_token = uuid.uuid4()
        next_token_hash = importlib.import_module(
            "apps.publishing.migrations.0011_publication_attempt_fencing"
        )._lease_token_hash(next_token)
        next_expiry = timezone.now() + timezone.timedelta(minutes=5)
        OutboxConsumerReceipt.objects.filter(id=receipt.id).update(
            lease_generation=receipt.lease_generation + 1,
            lease_token=next_token,
            claimed_until=next_expiry,
        )
        next_delivery = publishing_models.PublicationReconcileDeliveryObservation.objects.create(
            reconcile_generation=generation,
            consumer_name="publication-reconcile",
            consumer_lease_generation=receipt.lease_generation + 1,
            consumer_lease_token=next_token,
            lease_token_hash=next_token_hash,
            lease_expires_at=next_expiry,
            started_at=timezone.now(),
        )
        OutboxConsumerReceipt.objects.filter(id=receipt.id).update(
            terminal_reserved_at=timezone.now(),
            terminal_lease_generation=receipt.lease_generation + 1,
            terminal_lease_token=next_token,
            terminal_lease_token_hash=next_token_hash,
            terminal_error_code="lost",
        )
        publishing_models.PublicationReconcileGeneration.objects.filter(
            id=generation.id
        ).update(
            state="delivery_failed",
            completed_at=timezone.now(),
            error_code="lost",
            recovery_state="manual_required",
        )
        late = publishing_models.PublicationLateExecutionResult.objects.create(
            reconcile_generation=generation,
            reconcile_delivery_observation=old_delivery,
            result_identity="d" * 64,
            result_state="succeeded",
            source_event=event,
            consumer_name="publication-reconcile",
            consumer_lease_generation=receipt.lease_generation,
            consumer_lease_token=old_token,
            lease_token_hash=token_hash,
        )
        self.assertIsNone(late.execution_observation_id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            publishing_models.PublicationLateExecutionResult.objects.create(
                result_identity="e" * 64,
                result_state="succeeded",
            )

    def test_execution_and_reconcile_require_exact_processing_receipts(self):
        attempt = self._attempt()
        execution_event, receipt, token_hash = self._event(
            attempt,
            topic="publication.requested",
            counter_name="execution_attempt_no",
            counter=1,
        )
        receipt.state = OutboxConsumerReceipt.State.RETRY
        receipt.save(update_fields=("state",))
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._observation(
                attempt, execution_event, receipt, token_hash, generation=1
            )

        reconcile_event, reconcile_receipt, reconcile_hash = self._event(
            attempt,
            topic="publication.reconcile_requested",
            counter_name="reconcile_attempt_no",
            counter=1,
        )
        generation = publishing_models.PublicationReconcileGeneration.objects.create(
            publication_attempt=attempt,
            generation=1,
            source_event=reconcile_event,
            not_before=reconcile_event.not_before,
            started_at=reconcile_event.occurred_at,
            correlation_id=reconcile_event.correlation_id,
        )
        reconcile_receipt.state = OutboxConsumerReceipt.State.RETRY
        reconcile_receipt.save(update_fields=("state",))
        with self.assertRaises(IntegrityError), transaction.atomic():
            publishing_models.PublicationReconcileDeliveryObservation.objects.create(
                reconcile_generation=generation,
                consumer_name="publication-reconcile",
                consumer_lease_generation=reconcile_receipt.lease_generation,
                consumer_lease_token=reconcile_receipt.lease_token,
                lease_token_hash=reconcile_hash,
                lease_expires_at=reconcile_receipt.claimed_until,
                started_at=timezone.now(),
            )

    def test_late_result_is_fenced_and_append_only(self):
        attempt = self._attempt()
        event, receipt, token_hash = self._event(
            attempt,
            topic="publication.requested",
            counter_name="execution_attempt_no",
            counter=1,
        )
        observation = self._observation(
            attempt, event, receipt, token_hash, generation=1
        )
        publishing_models.PublicationExecutionObservation.objects.filter(
            id=observation.id
        ).update(
            state="delivery_unknown",
            finished_at=timezone.now(),
            projection_disposition="no_result",
        )
        late = publishing_models.PublicationLateExecutionResult.objects.create(
            execution_observation=observation,
            result_identity="b" * 64,
            result_state="succeeded",
        )
        with self.assertRaises(TypeError):
            late.delete()
        with self.assertRaises(TypeError):
            publishing_models.PublicationLateExecutionResult.objects.filter(
                id=late.id
            ).update(error_code="changed")

    def test_terminal_projection_requires_exact_message_key_and_is_immutable(self):
        attempt = self._attempt()
        event, receipt, token_hash = self._event(
            attempt,
            topic="publication.requested",
            counter_name="execution_attempt_no",
            counter=1,
        )
        observation = self._observation(
            attempt, event, receipt, token_hash, generation=1
        )
        publishing_models.PublicationAttempt.objects.filter(id=attempt.id).update(
            state="running",
            execution_generation=1,
            active_source_event=event,
            active_consumer_name="publication-execute",
            active_consumer_lease_generation=receipt.lease_generation,
            active_lease_token_hash=token_hash,
            active_lease_expires_at=receipt.claimed_until,
            active_write_marker="1" * 64,
        )
        publishing_models.PublicationExecutionObservation.objects.filter(
            id=observation.id
        ).update(
            state="completed",
            finished_at=timezone.now(),
            result_state="succeeded",
            result_identity="c" * 64,
            projection_disposition="applied",
        )
        publishing_models.PublicationAttempt.objects.filter(id=attempt.id).update(
            state="succeeded",
            active_source_event=None,
            active_consumer_name="",
            active_consumer_lease_generation=0,
            active_consumer_lease_token=None,
            active_lease_token_hash="",
            active_lease_expires_at=None,
            active_write_marker="",
            active_write_started_at=None,
            terminal_event_key=event.message_key,
            terminal_generation=1,
            terminal_state="succeeded",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            publishing_models.PublicationAttempt.objects.filter(
                id=attempt.id
            ).update(error_code="changed-after-terminal")


class PublicationAttemptFencingGuardedTransactionTestCase(
    ApprovalGuardedTransactionTestCase
):
    def _fixture_teardown(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0011_publication_attempt_fencing"
        )
        infrastructure_migration = importlib.import_module(
            "wisdome_writer.infrastructure.migrations.0003_outbox_terminal_reservation"
        )
        with connection.schema_editor() as editor:
            migration.remove_t021_guards(None, editor)
            infrastructure_migration.drop_terminal_reservation_guards(editor)
        try:
            super()._fixture_teardown()
        finally:
            with connection.schema_editor() as editor:
                infrastructure_migration.install_terminal_reservation_guards(
                    None, editor
                )
                migration.install_t021_guards(None, editor)


class PublicationAttemptFencingMigrationExecutorTests(
    PublicationAttemptFencingGuardedTransactionTestCase
):
    before = [("publishing", "0010_intent_dispatch_identity")]
    under_test = [("publishing", "0011_publication_attempt_fencing")]

    def test_0011_has_an_actual_empty_sqlite_forward_and_reverse_path(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate(self.before)
            MigrationExecutor(connection).migrate(self.under_test)
            state = MigrationExecutor(connection).loader.project_state(
                self.under_test
            )
            attempt = state.apps.get_model("publishing", "PublicationAttempt")
            observation = state.apps.get_model(
                "publishing", "PublicationExecutionObservation"
            )
            self.assertIsNotNone(attempt._meta.get_field("execution_generation"))
            self.assertIsNotNone(observation._meta.get_field("result_identity"))

            MigrationExecutor(connection).migrate(self.before)
            state = MigrationExecutor(connection).loader.project_state(self.before)
            legacy_attempt = state.apps.get_model(
                "publishing", "PublicationAttempt"
            )
            with self.assertRaises(Exception):
                legacy_attempt._meta.get_field("execution_generation")
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_0011_populated_reverse_is_explicitly_irreversible(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        PublicationAttemptFencingSQLiteGuardTests.setUpTestData()
        helper = PublicationAttemptFencingSQLiteGuardTests(methodName="runTest")
        helper.fixture = PublicationAttemptFencingSQLiteGuardTests.fixture
        attempt = helper._attempt()
        try:
            with self.assertRaises(IrreversibleError):
                MigrationExecutor(connection).migrate(self.before)
            self.assertTrue(
                publishing_models.PublicationAttempt.objects.filter(
                    id=attempt.id
                ).exists()
            )
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_infrastructure_0003_has_actual_empty_forward_and_reverse_path(self):
        infra_before = [("infrastructure", "0002_outboxconsumerreceipt_and_more")]
        infra_under_test = [
            ("infrastructure", "0003_outbox_terminal_reservation")
        ]
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate(self.before + infra_before)
            MigrationExecutor(connection).migrate(self.before + infra_under_test)
            state = MigrationExecutor(connection).loader.project_state(
                self.before + infra_under_test
            )
            receipt = state.apps.get_model(
                "infrastructure", "OutboxConsumerReceipt"
            )
            self.assertIsNotNone(receipt._meta.get_field("terminal_reserved_at"))

            MigrationExecutor(connection).migrate(self.before + infra_before)
            state = MigrationExecutor(connection).loader.project_state(
                self.before + infra_before
            )
            legacy_receipt = state.apps.get_model(
                "infrastructure", "OutboxConsumerReceipt"
            )
            with self.assertRaises(Exception):
                legacy_receipt._meta.get_field("terminal_reserved_at")
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_infrastructure_0003_populated_reverse_is_irreversible(self):
        infra_before = [("infrastructure", "0002_outboxconsumerreceipt_and_more")]
        infra_under_test = [
            ("infrastructure", "0003_outbox_terminal_reservation")
        ]
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        event_id = uuid.uuid4()
        receipt_id = uuid.uuid4()
        try:
            MigrationExecutor(connection).migrate(self.before + infra_under_test)
            state = MigrationExecutor(connection).loader.project_state(
                self.before + infra_under_test
            )
            event_model = state.apps.get_model("infrastructure", "OutboxMessage")
            receipt_model = state.apps.get_model(
                "infrastructure", "OutboxConsumerReceipt"
            )
            now = timezone.now()
            token = uuid.uuid4()
            event_model.objects.create(
                id=event_id,
                message_key=f"infra-0003-{event_id}",
                topic="publication.requested",
                event_version=2,
                aggregate_type="publication_attempt",
                aggregate_id=uuid.uuid4(),
                payload={},
                correlation_id=uuid.uuid4(),
                job_id=uuid.uuid4(),
                immutable_material_hash="a" * 64,
            )
            receipt_model.objects.create(
                id=receipt_id,
                event_id=event_id,
                consumer_name="publication-execute",
                state="processing",
                lease_generation=1,
                lease_token=token,
                claimed_until=now + timezone.timedelta(minutes=5),
                terminal_reserved_at=now,
                terminal_lease_generation=1,
                terminal_lease_token=token,
                terminal_lease_token_hash="b" * 64,
                terminal_error_code="consumer_attempts_exhausted",
            )

            with self.assertRaises(IrreversibleError):
                MigrationExecutor(connection).migrate(self.before + infra_before)
        finally:
            infrastructure_migration = importlib.import_module(
                "wisdome_writer.infrastructure.migrations.0003_outbox_terminal_reservation"
            )
            with connection.schema_editor() as editor:
                infrastructure_migration.drop_terminal_reservation_guards(editor)
            state = MigrationExecutor(connection).loader.project_state(
                self.before + infra_under_test
            )
            receipt_model = state.apps.get_model(
                "infrastructure", "OutboxConsumerReceipt"
            )
            event_model = state.apps.get_model("infrastructure", "OutboxMessage")
            receipt_model.objects.filter(id=receipt_id).delete()
            event_model.objects.filter(id=event_id).delete()
            with connection.schema_editor() as editor:
                infrastructure_migration.install_terminal_reservation_guards(
                    None, editor
                )
            MigrationExecutor(connection).migrate(latest)


class PublicationAttemptFencingConcurrencyTests(
    PublicationAttemptFencingGuardedTransactionTestCase
):
    def setUp(self):
        PublicationAttemptFencingSQLiteGuardTests.setUpTestData()
        self.fixture = PublicationAttemptFencingSQLiteGuardTests.fixture
        helper = PublicationAttemptFencingSQLiteGuardTests(methodName="runTest")
        helper.fixture = self.fixture
        self.attempt = helper._attempt()
        self.event, self.receipt, self.token_hash = helper._event(
            self.attempt,
            topic="publication.requested",
            counter_name="execution_attempt_no",
            counter=1,
        )

    def test_two_execution_claims_have_exactly_one_generation_winner(self):
        barrier = threading.Barrier(2)
        result_lock = threading.Lock()
        results = []

        def claim(index):
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                publishing_models.PublicationExecutionObservation.objects.create(
                    publication_attempt_id=self.attempt.id,
                    execution_attempt_no=1,
                    execution_generation=1,
                    correlation_id=self.event.correlation_id,
                    source_event_id=self.event.id,
                consumer_name="publication-execute",
                consumer_lease_generation=self.receipt.lease_generation,
                consumer_lease_token=self.receipt.lease_token,
                lease_token_hash=self.token_hash,
                    lease_expires_at=self.receipt.claimed_until,
                    write_marker=str(index) * 64,
                    started_at=timezone.now(),
                )
                outcome = "created"
            except (IntegrityError, OperationalError):
                outcome = "lost"
            finally:
                connections.close_all()
            with result_lock:
                results.append(outcome)

        threads = [threading.Thread(target=claim, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(sorted(results), ["created", "lost"])
        self.assertEqual(
            publishing_models.PublicationExecutionObservation.objects.filter(
                publication_attempt=self.attempt,
                state="started",
            ).count(),
            1,
        )
