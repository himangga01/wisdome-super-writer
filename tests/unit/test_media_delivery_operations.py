import uuid
from unittest import TestCase
from unittest.mock import patch

from django.db.models import PROTECT
from django.test import TestCase as DjangoTestCase
from django.utils import timezone

from apps.publishing import models as publishing_models
from apps.publishing import services
from apps.audit.services import AuditContext
from wisdome_writer.infrastructure.models import OutboxConsumerReceipt
from wisdome_writer.infrastructure.event_routes import payload_schema_for, route_for
from wisdome_writer.infrastructure.outbox import (
    ForbiddenEventPayload,
    _validate_event_payload,
)


REMOTE_ID = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))
DELIVERY_ID = str(uuid.UUID("22222222-2222-4222-8222-222222222222"))
ATTEMPT_ID = str(uuid.UUID("33333333-3333-4333-8333-333333333333"))
INTENT_ID = str(uuid.UUID("44444444-4444-4444-8444-444444444444"))
SNAPSHOT_ID = str(uuid.UUID("55555555-5555-4555-8555-555555555555"))
SHA = "a" * 64


class MediaDeliveryEventContractTests(TestCase):
    def test_v2_media_and_delivery_payloads_are_exact_and_bounded(self):
        cases = {
            "media.upload_requested": {
                "remote_media_id": REMOTE_ID,
                "publication_attempt_id": ATTEMPT_ID,
                "publication_intent_id": INTENT_ID,
                "operation_generation": 1,
                "target_snapshot_id": SNAPSHOT_ID,
                "target_config_hash": SHA,
            },
            "media.reconcile_requested": {
                "remote_media_id": REMOTE_ID,
                "publication_attempt_id": ATTEMPT_ID,
                "publication_intent_id": INTENT_ID,
                "operation_generation": 1,
            },
            "delivery.prepare_requested": {
                "public_delivery_asset_id": DELIVERY_ID,
                "publication_attempt_id": ATTEMPT_ID,
                "publication_intent_id": INTENT_ID,
                "operation_generation": 1,
            },
            "delivery.reconcile_requested": {
                "public_delivery_asset_id": DELIVERY_ID,
                "publication_attempt_id": ATTEMPT_ID,
                "publication_intent_id": INTENT_ID,
                "operation_generation": 1,
            },
            "media.delete_requested": {
                "remote_media_id": REMOTE_ID,
                "operation_generation": 1,
            },
            "delivery.delete_requested": {
                "public_delivery_asset_id": DELIVERY_ID,
                "operation_generation": 1,
            },
        }

        for event_type, payload in cases.items():
            with self.subTest(event_type=event_type):
                route = route_for(event_type, 2)
                schema = payload_schema_for(event_type, 2)
                self.assertIsNotNone(route)
                self.assertIsNotNone(schema)
                self.assertEqual(schema.required, frozenset(payload))
                self.assertEqual(route.argument_keys, tuple(payload))
                self.assertEqual(route.terminal_argument_keys, tuple(payload))
                _validate_event_payload(event_type, 2, payload)
                for invalid in (
                    {key: value for key, value in payload.items() if key != next(iter(payload))},
                    {**payload, "lease_token": str(uuid.uuid4())},
                    {**payload, "operation_generation": 0},
                    {**payload, "operation_generation": 6},
                ):
                    with self.assertRaises(ForbiddenEventPayload):
                        _validate_event_payload(event_type, 2, invalid)


class MediaDeliveryOperationModelContractTests(TestCase):
    def test_operation_ledger_binds_exact_mapping_event_and_generation(self):
        operation = publishing_models.MediaDeliveryOperation
        field_names = {field.name for field in operation._meta.get_fields()}
        self.assertLessEqual(
            {
                "mapping_kind",
                "remote_media",
                "public_delivery_asset",
                "publication_attempt",
                "publication_intent",
                "action",
                "generation",
                "source_event",
                "state",
                "result_hash",
            },
            field_names,
        )
        for field_name in (
            "remote_media",
            "public_delivery_asset",
            "publication_attempt",
            "publication_intent",
            "source_event",
        ):
            self.assertIs(
                operation._meta.get_field(field_name).remote_field.on_delete,
                PROTECT,
            )
        constraint_names = {
            constraint.name for constraint in operation._meta.constraints
        }
        self.assertIn("ck_media_operation_mapping_xor", constraint_names)
        self.assertIn("uq_remote_media_operation_generation", constraint_names)
        self.assertIn("uq_delivery_asset_operation_generation", constraint_names)


class MediaDeliveryOperationServiceTests(DjangoTestCase):
    @classmethod
    def setUpTestData(cls):
        from tests.unit.test_publication_approval_service import (
            ApprovalDecisionDatabaseTests,
        )

        ApprovalDecisionDatabaseTests.setUpTestData()
        cls.fixture = ApprovalDecisionDatabaseTests.fixture
        cls.revision = cls.fixture.intent.article_revision

    from tests.unit.test_published_asset_snapshots import (
        PublishedAssetCohortServiceTests as _AssetFixture,
    )
    from tests.unit.test_publication_media_bindings import (
        PublicationMediaBindingServiceTests as _BindingFixture,
    )

    _evidence_revision = _AssetFixture._evidence_revision
    _intent_for_revision = _AssetFixture._intent_for_revision
    _attempt = _BindingFixture._attempt

    def test_delete_operation_enqueues_exact_v2_event_and_replays(self):
        attempt = self._attempt()
        binding = services.prepare_publication_media_bindings_locked(
            attempt=attempt
        )[0]

        first, created = services.enqueue_media_delivery_operation_locked(
            remote_media=binding.remote_media,
            action=publishing_models.MediaDeliveryOperation.Action.DELETE,
        )
        replay, replay_created = services.enqueue_media_delivery_operation_locked(
            remote_media=binding.remote_media,
            action=publishing_models.MediaDeliveryOperation.Action.DELETE,
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first.id, replay.id)
        self.assertEqual(first.generation, 1)
        self.assertEqual(first.source_event.topic, "media.delete_requested")
        self.assertEqual(first.source_event.event_version, 2)
        self.assertEqual(
            first.source_event.payload,
            {
                "remote_media_id": str(binding.remote_media_id),
                "operation_generation": 1,
            },
        )

    def test_only_exact_receipt_capability_claims_an_operation_once(self):
        attempt = self._attempt()
        binding = services.prepare_publication_media_bindings_locked(
            attempt=attempt
        )[0]
        operation, _created = services.enqueue_media_delivery_operation_locked(
            remote_media=binding.remote_media,
            action=publishing_models.MediaDeliveryOperation.Action.DELETE,
        )
        token = uuid.uuid4()
        receipt = OutboxConsumerReceipt.objects.create(
            event=operation.source_event,
            consumer_name="media-delete-v2",
            state=OutboxConsumerReceipt.State.PROCESSING,
            attempts=1,
            claimed_at=timezone.now(),
            claimed_until=timezone.now() + timezone.timedelta(minutes=1),
            lease_token=token,
            lease_generation=1,
        )
        context = AuditContext.for_worker(
            correlation_id=operation.source_event.correlation_id,
            event_key=str(operation.source_event_id),
            consumer_name=receipt.consumer_name,
            lease_token=token,
            lease_generation=1,
        )

        claimed, fence = services.begin_media_delivery_operation(
            operation.id,
            expected_generation=1,
            audit_context=context,
        )
        replay, replay_fence = services.begin_media_delivery_operation(
            operation.id,
            expected_generation=1,
            audit_context=context,
        )

        self.assertEqual(claimed.state, publishing_models.MediaDeliveryOperation.State.RUNNING)
        self.assertEqual(fence.operation_id, operation.id)
        self.assertEqual(fence.generation, 1)
        self.assertEqual(replay.id, operation.id)
        self.assertIsNone(replay_fence)

        finished = services.persist_media_delivery_operation_result(
            fence,
            result={
                "status": "manual_required",
                "error_code": "remote_match_ambiguous",
            },
            audit_context=context,
        )
        self.assertEqual(
            finished.state,
            publishing_models.MediaDeliveryOperation.State.MANUAL_REQUIRED,
        )
        self.assertEqual(finished.error_code, "remote_match_ambiguous")

    def test_reclaimed_receipt_keeps_late_result_as_audit_only(self):
        attempt = self._attempt()
        binding = services.prepare_publication_media_bindings_locked(
            attempt=attempt
        )[0]
        operation, _created = services.enqueue_media_delivery_operation_locked(
            remote_media=binding.remote_media,
            action=publishing_models.MediaDeliveryOperation.Action.DELETE,
        )
        first_token = uuid.uuid4()
        receipt = OutboxConsumerReceipt.objects.create(
            event=operation.source_event,
            consumer_name="media-delete-v2",
            state=OutboxConsumerReceipt.State.PROCESSING,
            attempts=1,
            claimed_at=timezone.now(),
            claimed_until=timezone.now() + timezone.timedelta(minutes=1),
            lease_token=first_token,
            lease_generation=1,
        )
        first_context = AuditContext.for_worker(
            correlation_id=operation.source_event.correlation_id,
            event_key=str(operation.source_event_id),
            consumer_name=receipt.consumer_name,
            lease_token=first_token,
            lease_generation=1,
        )
        _claimed, fence = services.begin_media_delivery_operation(
            operation.id,
            expected_generation=1,
            audit_context=first_context,
        )

        receipt.lease_generation = 2
        receipt.lease_token = uuid.uuid4()
        receipt.claimed_at = timezone.now()
        receipt.claimed_until = timezone.now() + timezone.timedelta(minutes=1)
        receipt.save(
            update_fields=(
                "lease_generation",
                "lease_token",
                "claimed_at",
                "claimed_until",
            )
        )

        unchanged = services.persist_media_delivery_operation_result(
            fence,
            result={"status": "succeeded"},
            audit_context=first_context,
        )
        unchanged.refresh_from_db()
        binding.remote_media.refresh_from_db()
        self.assertEqual(
            unchanged.state,
            publishing_models.MediaDeliveryOperation.State.RUNNING,
        )
        self.assertNotEqual(
            binding.remote_media.state,
            publishing_models.RemoteMedia.State.DELETED,
        )

    def test_post_write_reclaim_fences_old_generation_and_queues_recovery(self):
        attempt = self._attempt()
        binding = services.prepare_publication_media_bindings_locked(
            attempt=attempt
        )[0]
        binding.binding_state = (
            publishing_models.PublicationMedia.BindingState.REMOVED
        )
        binding.removed_at = timezone.now()
        binding.save(update_fields=("binding_state", "removed_at"))
        operation, _created = services.enqueue_media_delivery_operation_locked(
            remote_media=binding.remote_media,
            action=publishing_models.MediaDeliveryOperation.Action.DELETE,
        )
        first_token = uuid.uuid4()
        receipt = OutboxConsumerReceipt.objects.create(
            event=operation.source_event,
            consumer_name="media-delete-v2",
            state=OutboxConsumerReceipt.State.PROCESSING,
            attempts=1,
            claimed_at=timezone.now(),
            claimed_until=timezone.now() + timezone.timedelta(minutes=1),
            lease_token=first_token,
            lease_generation=1,
        )
        first_context = AuditContext.for_worker(
            correlation_id=operation.source_event.correlation_id,
            event_key=str(operation.source_event_id),
            consumer_name=receipt.consumer_name,
            lease_token=first_token,
            lease_generation=1,
        )
        _claimed, fence = services.begin_media_delivery_operation(
            operation.id,
            expected_generation=1,
            audit_context=first_context,
        )
        with patch.object(services, "_kill_switch_enabled", return_value=False):
            services.authorize_media_delivery_external_write(
                fence,
                audit_context=first_context,
            )

        second_token = uuid.uuid4()
        receipt.lease_generation = 2
        receipt.lease_token = second_token
        receipt.claimed_at = timezone.now()
        receipt.claimed_until = timezone.now() + timezone.timedelta(minutes=1)
        receipt.save(
            update_fields=(
                "lease_generation",
                "lease_token",
                "claimed_at",
                "claimed_until",
            )
        )
        second_context = AuditContext.for_worker(
            correlation_id=operation.source_event.correlation_id,
            event_key=str(operation.source_event_id),
            consumer_name=receipt.consumer_name,
            lease_token=second_token,
            lease_generation=2,
        )

        reclaimed, reclaimed_fence = services.begin_media_delivery_operation(
            operation.id,
            expected_generation=1,
            audit_context=second_context,
        )

        self.assertIsNone(reclaimed_fence)
        self.assertEqual(
            reclaimed.state,
            publishing_models.MediaDeliveryOperation.State.UNKNOWN_OUTCOME,
        )
        recovery = publishing_models.MediaDeliveryOperation.objects.get(
            remote_media=binding.remote_media,
            generation=2,
        )
        self.assertEqual(
            recovery.action,
            publishing_models.MediaDeliveryOperation.Action.DELETE,
        )
        self.assertEqual(
            recovery.state,
            publishing_models.MediaDeliveryOperation.State.QUEUED,
        )

    def test_terminal_reservation_projects_delivery_failure_once(self):
        attempt = self._attempt()
        binding = services.prepare_publication_media_bindings_locked(
            attempt=attempt
        )[0]
        operation, _created = services.enqueue_media_delivery_operation_locked(
            remote_media=binding.remote_media,
            action=publishing_models.MediaDeliveryOperation.Action.DELETE,
        )
        token = uuid.uuid4()
        now = timezone.now()
        receipt = OutboxConsumerReceipt.objects.create(
            event=operation.source_event,
            consumer_name="media-delete-v2",
            state=OutboxConsumerReceipt.State.PROCESSING,
            attempts=5,
            claimed_at=now,
            claimed_until=now + timezone.timedelta(minutes=1),
            lease_token=token,
            lease_generation=1,
            terminal_reserved_at=now,
            terminal_lease_generation=1,
            terminal_lease_token=token,
            terminal_lease_token_hash=services._lease_token_hash(token),
            terminal_error_code="media_delivery_exhausted",
        )
        context = AuditContext.for_worker(
            correlation_id=operation.source_event.correlation_id,
            event_key=str(operation.source_event_id),
            consumer_name=receipt.consumer_name,
            lease_token=token,
            lease_generation=1,
        )

        first = services.finalize_media_delivery_operation_failure(
            operation.id,
            expected_generation=1,
            error_code="media_delivery_exhausted",
            audit_context=context,
        )
        replay = services.finalize_media_delivery_operation_failure(
            operation.id,
            expected_generation=1,
            error_code="media_delivery_exhausted",
            audit_context=context,
        )

        self.assertEqual(first.id, replay.id)
        self.assertEqual(
            replay.state,
            publishing_models.MediaDeliveryOperation.State.DELIVERY_FAILED,
        )
        self.assertEqual(replay.error_code, "media_delivery_exhausted")

    def test_v2_routes_resolve_worker_and_terminal_entrypoints(self):
        from apps.publishing import tasks

        self.assertTrue(callable(tasks.execute_media_delivery_operation))
        self.assertTrue(callable(tasks.finalize_media_delivery_operation_failure))
