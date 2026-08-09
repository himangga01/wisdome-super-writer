from __future__ import annotations

import uuid
import os
import importlib
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from celery.exceptions import SoftTimeLimitExceeded
from django.test import TransactionTestCase
from django.utils import timezone

from adapters.extractors.base import GenericEvidenceRecord, GenericExtractionOutput

from apps.collection.models import (
    CollectionRun,
    RunStep,
    RunSourceItem,
    RunState,
    SourceCollectionAttempt,
    SourceItem,
)
from apps.evidence import services, tasks
from apps.evidence.models import (
    DocumentExtraction,
    EvidenceAsset,
    EvidenceDerivationType,
    EvidenceKind,
    ExtractionEngine,
    ExtractionObjectWritePurpose,
    ExtractionObjectWriteReservation,
    ExtractionObjectWriteState,
    ExtractionProfileSnapshot,
    ExtractionRun,
    ExtractionState,
    GenericExtractionAttempt,
    GenericValidationMode,
    LocatorType,
)
from apps.topics.models import (
    SourceDefinition,
    SourceDefinitionSnapshot,
    SourceRegistryMembership,
    SourceRegistrySnapshot,
    TopicPolicy,
)
from wisdome_writer.infrastructure.models import OutboxConsumerReceipt, OutboxMessage
from wisdome_writer.infrastructure.event_routes import route_for
from wisdome_writer.infrastructure.outbox import PermanentEventError
from wisdome_writer.infrastructure.outbox import event_context
from wisdome_writer.infrastructure import outbox


SHA = "1" * 64


@contextmanager
def _routed_context(
    *, lease_generation: int = 1, attempt: int = 1, event_id: uuid.UUID | None = None
):
    event_id = event_id or uuid.uuid4()
    token = uuid.uuid4()
    with event_context(
        {
            "event_id": str(event_id),
            "correlation_id": str(uuid.uuid4()),
        },
        consumer_name="test-consumer",
        lease_token=token,
        lease_generation=lease_generation,
        consumer_attempt=attempt,
    ):
        yield event_id, token


def _lineage(*, stopped: bool = False):
    now = timezone.now()
    policy, _ = TopicPolicy.objects.get_or_create(
        code="housing_subscription",
        version=1,
        defaults={
            "title": "test",
            "freshness_minutes": 60,
            "policy": {},
            "policy_hash": SHA,
        },
    )
    source = SourceDefinition.objects.create(
        topic_code="housing_subscription",
        key=f"source-{uuid.uuid4().hex[:8]}",
        display_name="source",
        publisher="publisher",
        owner_name="owner",
        editorial_control_name="editor",
        base_url="https://example.com/",
        authority_tier="primary_official",
        access_method="public_file",
        independence_group="official",
        adapter_key="test",
    )
    snapshot = SourceDefinitionSnapshot.objects.create(
        source=source,
        topic_code="housing_subscription",
        version=1,
        config={},
        config_hash=SHA,
        frozen_config={},
        frozen_config_hash=SHA,
        independence_group="official",
        owner_name="owner",
        editorial_control_name="editor",
    )
    registry, _ = SourceRegistrySnapshot.objects.get_or_create(
        topic_code="housing_subscription",
        version=1,
        defaults={"manifest_hash": SHA},
    )
    SourceRegistryMembership.objects.create(
        registry=registry,
        source_definition=source,
        source_snapshot=snapshot,
    )
    run = CollectionRun.objects.create(
        display_id=f"RUN-{uuid.uuid4().hex[:8]}",
        topic_code="housing_subscription",
        window_start=now,
        window_end=now,
        source_registry=registry,
        registry_manifest_hash=SHA,
        topic_policy=policy,
        policy_version=1,
        policy_hash=SHA,
        freshness_minutes=60,
        allowed_authority_tiers=["primary_official"],
        freshness_cutoff=now,
        request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        state=RunState.EXTRACTING,
        stop_requested_at=now if stopped else None,
    )
    collection_attempt = SourceCollectionAttempt.objects.create(
        run=run,
        source_snapshot=snapshot,
        adapter_name="test",
    )
    source_item = SourceItem.objects.create(
        source=source,
        external_id=uuid.uuid4().hex,
        canonical_url="https://example.com/item",
        title="item",
        publisher="publisher",
        first_collected_at=now,
        content_hash=SHA,
        source_version_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        metadata={"test": True},
        attachments=[{"test": True}],
    )
    run_source_item = RunSourceItem.objects.create(
        run=run,
        collection_attempt=collection_attempt,
        source_item=source_item,
        source_snapshot=snapshot,
    )
    return run, run_source_item, source_item


def _profile() -> ExtractionProfileSnapshot:
    return ExtractionProfileSnapshot.objects.create(
        profile_key=f"test-{uuid.uuid4().hex[:8]}",
        profile_version="1.0.0",
        engine=ExtractionEngine.HTML,
        extractor_version="1.0.0",
        implementation_manifest_hash=SHA,
        config={},
        config_hash=SHA,
        validation_mode=GenericValidationMode.DETERMINISTIC,
        profile_material_hash=uuid.uuid4().hex + uuid.uuid4().hex,
    )


def _generic_attempt(*, stopped: bool = False) -> GenericExtractionAttempt:
    _, run_source_item, source_item = _lineage(stopped=stopped)
    return _generic_for_lineage(run_source_item, source_item)


def _generic_for_lineage(run_source_item, source_item) -> GenericExtractionAttempt:
    profile = _profile()
    return GenericExtractionAttempt.objects.create(
        run_source_item=run_source_item,
        source_item=source_item,
        extraction_profile_snapshot=profile,
        profile_material_hash=profile.profile_material_hash,
        engine=profile.engine,
        extractor_version=profile.extractor_version,
        config_hash=profile.config_hash,
        validation_mode=GenericValidationMode.DETERMINISTIC,
        extraction_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
    )


def _document_extraction(*, stopped: bool = False) -> DocumentExtraction:
    _, run_source_item, source_item = _lineage(stopped=stopped)
    return DocumentExtraction.objects.create(
        run_source_item=run_source_item,
        source_item=source_item,
        input_object_key="evidence/raw/test.pdf",
        input_object_version="v1",
        input_kind="pdf",
        input_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        input_mime_type="application/pdf",
        input_checksum=SHA,
        input_page_count=1,
        expected_page_indices=[0],
    )


def _document_child(document: DocumentExtraction) -> ExtractionRun:
    profile = ExtractionProfileSnapshot.objects.create(
        profile_key=f"native-{uuid.uuid4().hex[:8]}",
        profile_version="1.0.0",
        engine=ExtractionEngine.NATIVE_PDF,
        extractor_version="1.0.0",
        implementation_manifest_hash=SHA,
        config={},
        config_hash=SHA,
        validation_mode=GenericValidationMode.DETERMINISTIC,
        profile_material_hash=uuid.uuid4().hex + uuid.uuid4().hex,
    )
    return ExtractionRun.objects.create(
        document_extraction=document,
        extraction_profile_snapshot=profile,
        engine=profile.engine,
        page_set_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        extraction_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        requested_page_indices=[0],
        profile_key=profile.profile_key,
        profile_version=profile.profile_version,
        config_hash=profile.config_hash,
        profile_material_hash=profile.profile_material_hash,
        package_version="1.0.0",
        runtime_version="1.0.0",
    )


class ExtractionGenerationPersistenceTests(TransactionTestCase):
    def test_extraction_aggregates_persist_generation_and_terminal_identity_fields(self):
        required = {
            "source_event_id",
            "lease_generation",
            "lease_owner",
            "lease_token",
            "delivery_count",
            "next_retry_at",
            "terminal_event_key",
            "terminal_state",
        }
        for model in (DocumentExtraction, ExtractionRun, GenericExtractionAttempt):
            self.assertLessEqual(required, {field.name for field in model._meta.fields})
        self.assertIn(
            "parent_lease_generation",
            {field.name for field in ExtractionRun._meta.fields},
        )
        self.assertTrue(
            {"expected_evidence_manifest_hash", "expected_evidence_count"}
            <= {field.name for field in ExtractionRun._meta.fields}
        )
        constraint_names = {
            constraint.name
            for model in (DocumentExtraction, ExtractionRun, GenericExtractionAttempt)
            for constraint in model._meta.constraints
        }
        self.assertTrue({
            "ck_document_extraction_lease_complete",
            "ck_extraction_run_lease_complete",
            "ck_generic_extraction_lease_complete",
            "ck_document_extraction_terminal_complete",
            "ck_extraction_run_terminal_complete",
            "ck_generic_extraction_terminal_complete",
        } <= constraint_names)

    def test_parent_fanout_step_persists_delivery_generation_identity(self):
        required = {
            "source_event_id",
            "lease_generation",
            "lease_owner",
            "lease_token",
            "delivery_count",
        }
        self.assertLessEqual(required, {field.name for field in RunStep._meta.fields})

    def test_begin_replay_and_retry_advance_generation_once_per_delivery(self):
        attempt = _generic_attempt()
        event_id = uuid.uuid4()
        first_token = uuid.uuid4()

        first = services.begin_generic_extraction(
            attempt.id,
            source_event_id=event_id,
            delivery_count=1,
            lease_generation=1,
            lease_owner="worker-1",
            lease_token=first_token,
        )
        self.assertIsNotNone(first)
        self.assertEqual(first.lease_generation, 1)

        self.assertIsNone(services.begin_generic_extraction(
            attempt.id,
            source_event_id=event_id,
            delivery_count=1,
            lease_generation=1,
            lease_owner="worker-1",
            lease_token=first_token,
        ))

        second_token = uuid.uuid4()
        second = services.begin_generic_extraction(
            attempt.id,
            source_event_id=event_id,
            delivery_count=2,
            lease_generation=2,
            lease_owner="worker-2",
            lease_token=second_token,
        )
        self.assertIsNotNone(second)
        self.assertEqual(second.lease_generation, 2)

        retry_at = timezone.now()
        self.assertTrue(services.queue_generic_extraction_retry(
            attempt.id,
            expected_generation=2,
            expected_lease_owner="worker-2",
            expected_lease_token=second_token,
            retry_at=retry_at,
            error_code="temporary",
            error_detail_redacted="safe",
        ))
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, ExtractionState.QUEUED)
        self.assertEqual(attempt.lease_generation, 2)
        self.assertEqual(attempt.next_retry_at, retry_at)

        third_token = uuid.uuid4()
        third = services.begin_generic_extraction(
            attempt.id,
            source_event_id=event_id,
            delivery_count=3,
            lease_generation=3,
            lease_owner="worker-3",
            lease_token=third_token,
        )
        self.assertIsNotNone(third)
        self.assertEqual(third.lease_generation, 3)

    def test_stale_completion_fence_does_not_return_a_writable_attempt(self):
        attempt = _generic_attempt()
        event_id = uuid.uuid4()
        first_token = uuid.uuid4()
        services.begin_generic_extraction(
            attempt.id,
            source_event_id=event_id,
            delivery_count=1,
            lease_generation=1,
            lease_owner="worker-1",
            lease_token=first_token,
        )
        second_token = uuid.uuid4()
        services.begin_generic_extraction(
            attempt.id,
            source_event_id=event_id,
            delivery_count=2,
            lease_generation=2,
            lease_owner="worker-2",
            lease_token=second_token,
        )

        with services.generic_extraction_completion_fence(
            attempt.id,
            expected_generation=1,
            expected_lease_owner="worker-1",
            expected_lease_token=first_token,
        ) as locked:
            self.assertIsNone(locked)
        with services.generic_extraction_completion_fence(
            attempt.id,
            expected_generation=2,
            expected_lease_owner="worker-2",
            expected_lease_token=second_token,
        ) as locked:
            self.assertIsNotNone(locked)

    def test_wrong_lease_cannot_queue_or_complete_current_generation(self):
        attempt = _generic_attempt()
        token = uuid.uuid4()
        services.begin_generic_extraction(
            attempt.id,
            source_event_id=uuid.uuid4(),
            delivery_count=1,
            lease_generation=1,
            lease_owner="worker-1",
            lease_token=token,
        )

        self.assertFalse(services.queue_generic_extraction_retry(
            attempt.id,
            expected_generation=1,
            expected_lease_owner="worker-2",
            expected_lease_token=uuid.uuid4(),
            retry_at=timezone.now(),
            error_code="temporary",
            error_detail_redacted="safe",
        ))
        with services.generic_extraction_completion_fence(
            attempt.id,
            expected_generation=1,
            expected_lease_owner="worker-2",
            expected_lease_token=uuid.uuid4(),
        ) as locked:
            self.assertIsNone(locked)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, ExtractionState.RUNNING)

    def test_unexpected_exception_waits_for_atomic_terminal_callback(self):
        attempt = _generic_attempt()
        with patch.object(
            tasks,
            "_run_generic_extraction",
            side_effect=RuntimeError("password=do-not-store"),
        ):
            with _routed_context(lease_generation=7, attempt=99):
                with self.assertRaises(PermanentEventError) as raised:
                    tasks.process_generic_extraction.run(str(attempt.id))

        attempt.refresh_from_db()
        self.assertEqual(raised.exception.code, "unexpected_extraction_failure")
        self.assertEqual(attempt.state, ExtractionState.RUNNING)
        self.assertIsNone(attempt.terminal_event_key)
        self.assertFalse(OutboxMessage.objects.filter(
            topic="evidence.finalize_requested",
            aggregate_id=attempt.run_source_item.run_id,
        ).exists())

    def test_stopped_run_is_terminalized_before_generic_storage_io(self):
        attempt = _generic_attempt(stopped=True)
        with patch.object(tasks, "_storage") as storage:
            with _routed_context():
                result = tasks.process_generic_extraction.run(str(attempt.id))

        storage.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(result["state"], ExtractionState.FAILED)
        self.assertEqual(attempt.error_code, "extraction_stopped")
        self.assertTrue(attempt.terminal_event_key)
        self.assertEqual(OutboxMessage.objects.filter(
            topic="evidence.finalize_requested",
            message_key=attempt.terminal_event_key,
        ).count(), 1)

    def test_document_retry_closes_running_child_and_fences_stale_completion(self):
        document = _document_extraction()
        child = _document_child(document)
        event_id = uuid.uuid4()
        token = uuid.uuid4()
        claimed = services.begin_document_extraction(
            document.id,
            source_event_id=event_id,
            delivery_count=1,
            lease_generation=1,
            lease_owner="document-worker",
            lease_token=token,
        )
        running_child = services.begin_extraction_run(
            child.id,
            expected_parent_generation=claimed.lease_generation,
            expected_parent_lease_owner=claimed.lease_owner,
            expected_parent_lease_token=claimed.lease_token,
        )

        self.assertTrue(services.queue_document_extraction_retry(
            document.id,
            expected_generation=claimed.lease_generation,
            expected_lease_owner=claimed.lease_owner,
            expected_lease_token=claimed.lease_token,
            retry_at=timezone.now(),
            error_code="temporary",
            error_detail_redacted="safe",
        ))
        child.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(document.lease_generation, 1)
        self.assertEqual(child.state, ExtractionState.QUEUED)
        self.assertEqual(child.parent_lease_generation, 1)
        with services.extraction_run_completion_fence(
            child.id,
            expected_parent_generation=1,
            expected_child_generation=running_child.lease_generation,
            expected_lease_owner=running_child.lease_owner,
            expected_lease_token=running_child.lease_token,
        ) as locked:
            self.assertIsNone(locked)

    def test_parent_failure_fences_running_child_completion(self):
        document = _document_extraction()
        child = _document_child(document)
        token = uuid.uuid4()
        claimed = services.begin_document_extraction(
            document.id,
            source_event_id=uuid.uuid4(),
            delivery_count=1,
            lease_generation=1,
            lease_owner="document-worker",
            lease_token=token,
        )
        running_child = services.begin_extraction_run(
            child.id,
            expected_parent_generation=1,
            expected_parent_lease_owner="document-worker",
            expected_parent_lease_token=token,
        )
        tasks._terminalize_document_extraction(
            document.id,
            error_code="permanent",
            error_detail_redacted="safe",
            expected_generation=claimed.lease_generation,
            expected_source_event_id=claimed.source_event_id,
            expected_lease_owner=claimed.lease_owner,
            expected_lease_token=claimed.lease_token,
        )

        with services.extraction_run_completion_fence(
            child.id,
            expected_parent_generation=1,
            expected_child_generation=running_child.lease_generation,
            expected_lease_owner=running_child.lease_owner,
            expected_lease_token=running_child.lease_token,
        ) as locked:
            self.assertIsNone(locked)
        child.refresh_from_db()
        self.assertEqual(child.state, ExtractionState.FAILED)

    def test_stop_after_external_result_prevents_generic_commit(self):
        attempt = _generic_attempt()
        token = uuid.uuid4()
        claimed = services.begin_generic_extraction(
            attempt.id,
            source_event_id=uuid.uuid4(),
            delivery_count=1,
            lease_generation=1,
            lease_owner="generic-worker",
            lease_token=token,
        )
        CollectionRun.objects.filter(pk=attempt.run_source_item.run_id).update(
            stop_requested_at=timezone.now()
        )

        with services.generic_extraction_completion_fence(
            attempt.id,
            expected_generation=claimed.lease_generation,
            expected_lease_owner=claimed.lease_owner,
            expected_lease_token=claimed.lease_token,
        ) as locked:
            self.assertIsNone(locked)
        self.assertFalse(EvidenceAsset.objects.filter(
            generic_extraction_attempt=attempt
        ).exists())
        self.assertFalse(OutboxMessage.objects.filter(
            topic="evidence.other_ready",
            aggregate_id=attempt.id,
        ).exists())

    def test_exact_coverage_rejects_evidence_locator_on_another_page(self):
        document = _document_extraction()
        child = _document_child(document)
        result_checksum = "2" * 64
        locator = {
            "page_index": 1,
            "block_id": "b1",
            "block_type": "text",
            "bbox": [0, 0, 1, 1],
            "polygon": None,
            "reading_order": 0,
        }
        evidence_hash = "3" * 64
        child.state = ExtractionState.SUCCEEDED
        child.processed_page_indices = [0]
        child.result_checksum = result_checksum
        child.expected_evidence_count = 1
        child.expected_evidence_manifest_hash = services.canonical_hash([{
            "evidence_content_hash": evidence_hash,
            "locator_hash": services.canonical_hash(locator),
        }])
        child.terminal_state = "ready"
        child.terminal_event_key = f"extraction.run.ready:{child.id}:generation:0"
        child.save()
        EvidenceAsset.objects.create(
            source_item=document.source_item,
            origin_run_source_item=document.run_source_item,
            derivation_type=EvidenceDerivationType.DOCUMENT,
            document_extraction=document,
            extraction_run=child,
            kind=EvidenceKind.TEXT,
            locator_type=LocatorType.DOCUMENT_BLOCK,
            locator=locator,
            extraction_result_checksum=result_checksum,
            evidence_content_hash=evidence_hash,
            review_subject_hash="4" * 64,
        )
        document.routing_manifest = {
            "schema_version": "extraction-routing-v2",
            "pages": [{
                "page_index": 0,
                "selected_run_id": str(child.id),
                "engine": child.engine,
                "reason": "native_text",
            }],
        }
        document.save(update_fields=("routing_manifest", "updated_at"))

        result = services.aggregate_document_extraction(document.id)
        self.assertEqual(result.state, ExtractionState.FAILED)
        self.assertEqual(result.error_code, "selected_evidence_manifest_mismatch")

    def test_ready_payload_tamper_is_rejected_before_finalizer(self):
        attempt = _generic_attempt()
        checksum = "5" * 64
        evidence = EvidenceAsset.objects.create(
            source_item=attempt.source_item,
            origin_run_source_item=attempt.run_source_item,
            derivation_type=EvidenceDerivationType.OTHER,
            generic_extraction_attempt=attempt,
            kind=EvidenceKind.TEXT,
            locator_type=LocatorType.HTML_DOM,
            locator={
                "locator_type": LocatorType.HTML_DOM,
                "css_selector": "main",
                "xpath": None,
            },
            extraction_result_checksum=checksum,
            evidence_content_hash="6" * 64,
            review_subject_hash="7" * 64,
        )
        attempt.state = ExtractionState.SUCCEEDED
        attempt.result_checksum = checksum
        attempt.evidence_asset = evidence
        attempt.expected_evidence_count = 1
        attempt.expected_evidence_manifest_hash = services.generic_evidence_manifest_hash([evidence])
        attempt.terminal_state = "ready"
        attempt.terminal_event_key = f"evidence.other_ready:{attempt.id}:{checksum}"
        attempt.save()

        with self.assertRaises(PermanentEventError):
            tasks.consume_other_ready.run(
                str(attempt.run_source_item.run_id),
                str(attempt.run_source_item_id),
                str(attempt.source_item_id),
                str(evidence.id),
                str(attempt.id),
                attempt.engine,
                evidence.locator_type,
                attempt.validation_mode,
                "8" * 64,
                None,
                None,
                None,
                None,
                attempt.extraction_fingerprint,
            )

    def test_other_ready_rejects_evidence_lineage_tamper(self):
        attempt = _generic_attempt()
        checksum = "c" * 64
        evidence = EvidenceAsset.objects.create(
            source_item=attempt.source_item,
            origin_run_source_item=attempt.run_source_item,
            derivation_type=EvidenceDerivationType.OTHER,
            generic_extraction_attempt=attempt,
            kind=EvidenceKind.TEXT,
            locator_type=LocatorType.HTML_DOM,
            locator={"selector": "main"},
            extraction_method=attempt.engine,
            extractor_version=attempt.extractor_version,
            extraction_config_hash=attempt.config_hash,
            validation_mode=attempt.validation_mode,
            extraction_result_checksum=checksum,
            evidence_content_hash="d" * 64,
            review_subject_hash="e" * 64,
        )
        attempt.state = ExtractionState.SUCCEEDED
        attempt.result_checksum = checksum
        attempt.evidence_asset = evidence
        attempt.expected_evidence_count = 1
        attempt.expected_evidence_manifest_hash = services.generic_evidence_manifest_hash([evidence])
        attempt.terminal_state = "ready"
        attempt.terminal_event_key = f"evidence.other_ready:{attempt.id}:{checksum}"
        attempt.save()
        EvidenceAsset.objects.filter(pk=evidence.id).update(origin_run_source_item=None)

        with self.assertRaises(PermanentEventError), _routed_context():
            tasks.consume_other_ready.run(
                str(attempt.run_source_item.run_id),
                str(attempt.run_source_item_id),
                str(attempt.source_item_id),
                str(evidence.id),
                str(attempt.id),
                attempt.engine,
                evidence.locator_type,
                attempt.validation_mode,
                checksum,
                None,
                None,
                None,
                None,
                attempt.extraction_fingerprint,
            )

    def test_frozen_document_plan_replay_does_not_reselect_latest_profile(self):
        document = _document_extraction()
        child = _document_child(document)
        token = uuid.uuid4()
        claimed = services.begin_document_extraction(
            document.id,
            source_event_id=uuid.uuid4(),
            delivery_count=1,
            lease_generation=1,
            lease_owner="document-worker",
            lease_token=token,
        )
        document.routing_manifest = {
            "schema_version": "extraction-routing-v2",
            "pages": [{
                "page_index": 0,
                "selected_run_id": str(child.id),
                "profile_snapshot_id": str(child.extraction_profile_snapshot_id),
                "engine": child.engine,
                "reason": "native_text",
            }],
        }
        document.save(update_fields=("routing_manifest", "updated_at"))
        routes = [services.PageRoute(0, child.engine, "native_text")]

        with patch.object(tasks, "_profile", side_effect=AssertionError("profile drift")):
            selected = tasks._load_or_create_document_plan(
                document.id,
                routes=routes,
                expected_generation=claimed.lease_generation,
                expected_lease_owner=claimed.lease_owner,
                expected_lease_token=claimed.lease_token,
            )
        self.assertEqual(selected[child.engine].id, child.id)

    def test_stale_fanout_delivery_cannot_write_completion_marker(self):
        run, _, _ = _lineage()
        first_token = uuid.uuid4()
        first = services.begin_evidence_fanout(
            run.id,
            source_event_id=uuid.uuid4(),
            delivery_count=1,
            lease_generation=1,
            lease_owner="fanout-1",
            lease_token=first_token,
        )
        second_token = uuid.uuid4()
        second = services.begin_evidence_fanout(
            run.id,
            source_event_id=first.source_event_id,
            delivery_count=2,
            lease_generation=2,
            lease_owner="fanout-2",
            lease_token=second_token,
        )

        with services.evidence_fanout_completion_fence(
            run.id,
            expected_generation=first.lease_generation,
            expected_lease_owner="fanout-1",
            expected_lease_token=first_token,
        ) as step:
            self.assertIsNone(step)
        with services.evidence_fanout_completion_fence(
            run.id,
            expected_generation=second.lease_generation,
            expected_lease_owner="fanout-2",
            expected_lease_token=second_token,
        ) as step:
            self.assertIsNotNone(step)
            step.fanout_completed_at = timezone.now()
            step.save(update_fields=("fanout_completed_at",))
        first.refresh_from_db()
        self.assertIsNotNone(first.fanout_completed_at)

    def test_routed_identity_uses_consumer_lease_generation_not_attempt_count(self):
        aggregate_id = uuid.uuid4()
        with _routed_context(lease_generation=11, attempt=3) as (event_id, token):
            delivery = tasks._current_extraction_delivery(aggregate_id)
        self.assertEqual(delivery["source_event_id"], str(event_id))
        self.assertEqual(delivery["delivery_count"], 11)
        self.assertEqual(delivery["lease_generation"], 11)
        self.assertEqual(delivery["lease_token"], token)

    def test_consume_event_routes_persisted_topic_and_version_to_handler(self):
        run, _, _ = _lineage()
        event = tasks.enqueue_event(
            topic="evidence.finalize_requested",
            aggregate_type="collection_run",
            aggregate_id=run.id,
            message_key=f"test:consume:{run.id}",
            job_id=run.id,
            payload={"run_id": str(run.id)},
        )
        OutboxMessage.objects.filter(pk=event.id).update(attempts=1)
        event.refresh_from_db()
        outbox.validate_persisted_event(event)
        handled = []
        result = outbox.consume_event(
            envelope=outbox.event_envelope(event),
            consumer_name="evidence-finalize",
            handler=lambda run_id: handled.append(run_id),
            argument_keys=("run_id",),
        )
        self.assertEqual(
            result["state"],
            "succeeded",
            OutboxConsumerReceipt.objects.get(
                event=event, consumer_name="evidence-finalize"
            ).last_error_code,
        )
        self.assertEqual(handled, [str(run.id)])

    def test_same_attempt_redelivery_reclaims_with_new_receipt_lease_and_fences_old_token(self):
        attempt = _generic_attempt()
        source_event_id = uuid.uuid4()
        with _routed_context(lease_generation=4, attempt=1, event_id=source_event_id):
            first_delivery = tasks._current_extraction_delivery(attempt.id)
        first = services.begin_generic_extraction(attempt.id, **first_delivery)
        with _routed_context(lease_generation=5, attempt=1, event_id=source_event_id):
            second_delivery = tasks._current_extraction_delivery(attempt.id)
        second = services.begin_generic_extraction(attempt.id, **second_delivery)
        self.assertEqual(second.lease_generation, 5)
        self.assertNotEqual(first.lease_token, second.lease_token)
        with services.generic_extraction_completion_fence(
            attempt.id,
            expected_generation=first.lease_generation,
            expected_lease_owner=first.lease_owner,
            expected_lease_token=first.lease_token,
        ) as locked:
            self.assertIsNone(locked)

    def test_database_and_soft_timeout_errors_never_create_domain_terminal(self):
        for exception in (DatabaseError("db unavailable"), SoftTimeLimitExceeded()):
            attempt = _generic_attempt()
            with patch.object(tasks, "_run_generic_extraction", side_effect=exception):
                with _routed_context(), self.assertRaises(type(exception)):
                    tasks.process_generic_extraction.run(str(attempt.id))
            attempt.refresh_from_db()
            self.assertEqual(attempt.state, ExtractionState.RUNNING)
            self.assertIsNone(attempt.terminal_event_key)
            self.assertFalse(OutboxMessage.objects.filter(
                topic="evidence.finalize_requested",
                aggregate_id=attempt.run_source_item.run_id,
            ).exists())

    def test_terminal_callback_projects_one_failure_and_one_finalizer(self):
        attempt = _generic_attempt()
        with patch.object(
            tasks,
            "_run_generic_extraction",
            side_effect=RuntimeError("programming defect"),
        ), _routed_context(lease_generation=6) as (_, token):
            with self.assertRaises(PermanentEventError):
                tasks.process_generic_extraction.run(str(attempt.id))
            first = tasks.finalize_generic_extraction_failure.run(
                str(attempt.id), "unexpected_extraction_failure"
            )
            second = tasks.finalize_generic_extraction_failure.run(
                str(attempt.id), "unexpected_extraction_failure"
            )
        attempt.refresh_from_db()
        self.assertEqual(first, second)
        self.assertEqual(attempt.state, ExtractionState.FAILED)
        self.assertEqual(attempt.lease_token, None)
        self.assertEqual(OutboxMessage.objects.filter(
            topic="evidence.finalize_requested",
            message_key=attempt.terminal_event_key,
        ).count(), 1)

    def test_terminal_callback_closes_same_generation_released_retry(self):
        generic = _generic_attempt()
        document = _document_extraction()
        child = _document_child(document)
        source_event_id = uuid.uuid4()
        with _routed_context(
            lease_generation=4, event_id=source_event_id
        ) as (_, token):
            claimed_generic = services.begin_generic_extraction(
                generic.id,
                source_event_id=source_event_id,
                delivery_count=4,
                lease_generation=4,
                lease_owner="test-consumer",
                lease_token=token,
            )
            services.queue_generic_extraction_retry(
                generic.id,
                expected_generation=4,
                expected_lease_owner="test-consumer",
                expected_lease_token=token,
                retry_at=timezone.now(),
                error_code="temporary",
                error_detail_redacted="safe",
            )
            generic_result = tasks.finalize_generic_extraction_failure.run(
                str(generic.id), "retry_exhausted"
            )

            claimed_document = services.begin_document_extraction(
                document.id,
                source_event_id=source_event_id,
                delivery_count=4,
                lease_generation=4,
                lease_owner="test-consumer",
                lease_token=token,
            )
            services.begin_extraction_run(
                child.id,
                expected_parent_generation=4,
                expected_parent_lease_owner="test-consumer",
                expected_parent_lease_token=token,
            )
            services.queue_document_extraction_retry(
                document.id,
                expected_generation=4,
                expected_lease_owner="test-consumer",
                expected_lease_token=token,
                retry_at=timezone.now(),
                error_code="temporary",
                error_detail_redacted="safe",
            )
            document_result = tasks.finalize_document_extraction_failure.run(
                str(document.id), "retry_exhausted"
            )
        generic.refresh_from_db()
        document.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(claimed_generic.lease_generation, 4)
        self.assertEqual(claimed_document.lease_generation, 4)
        self.assertEqual(generic_result["state"], ExtractionState.FAILED)
        self.assertEqual(document_result["state"], ExtractionState.FAILED)
        self.assertEqual(generic.state, ExtractionState.FAILED)
        self.assertEqual(document.state, ExtractionState.FAILED)
        self.assertEqual(child.state, ExtractionState.FAILED)

    def test_terminal_callback_binds_virgin_and_newer_callback_to_persisted_delivery(self):
        virgin = _generic_attempt()
        source_event_id = uuid.uuid4()
        with _routed_context(lease_generation=3, event_id=source_event_id):
            result = tasks.finalize_generic_extraction_failure.run(
                str(virgin.id), "dispatch_exhausted"
            )
        virgin.refresh_from_db()
        self.assertEqual(result["state"], ExtractionState.FAILED)
        self.assertEqual(virgin.source_event_id, source_event_id)
        self.assertEqual(virgin.lease_generation, 3)

        released = _document_extraction()
        first_token = uuid.uuid4()
        services.begin_document_extraction(
            released.id,
            source_event_id=source_event_id,
            delivery_count=1,
            lease_generation=1,
            lease_owner="worker-1",
            lease_token=first_token,
        )
        services.queue_document_extraction_retry(
            released.id,
            expected_generation=1,
            expected_lease_owner="worker-1",
            expected_lease_token=first_token,
            retry_at=timezone.now(),
            error_code="temporary",
            error_detail_redacted="safe",
        )
        with _routed_context(lease_generation=2, event_id=source_event_id):
            result = tasks.finalize_document_extraction_failure.run(
                str(released.id), "consumer_exhausted"
            )
        released.refresh_from_db()
        self.assertEqual(result["state"], ExtractionState.FAILED)
        self.assertEqual(released.lease_generation, 1)

    def test_dispatch_and_preclaim_exhaustion_terminalize_virgin_leaf_deliveries(self):
        cases = (
            (
                _generic_attempt,
                tasks._enqueue_generic_extraction,
                "generic-extraction",
                "generic_extraction_attempt_id",
                tasks.finalize_generic_extraction_failure.run,
            ),
            (
                _document_extraction,
                tasks._enqueue_document_extraction,
                "document-extraction",
                "document_extraction_id",
                tasks.finalize_document_extraction_failure.run,
            ),
        )
        for factory, enqueue, consumer, payload_key, terminal in cases:
            with self.subTest(consumer=consumer, path="dispatch"):
                aggregate = factory()
                aggregate.refresh_from_db()
                enqueue(aggregate)
                event = OutboxMessage.objects.get(
                    aggregate_id=aggregate.id,
                    topic__endswith="requested",
                )
                result = outbox.dead_letter_consumer_event(
                    event.id,
                    consumer_name=consumer,
                    error_code="dispatch_exhausted",
                    terminal_handler=terminal,
                    terminal_argument_keys=(payload_key,),
                )
                aggregate.refresh_from_db()
                self.assertEqual(result["state"], "dead_letter")
                self.assertEqual(aggregate.state, ExtractionState.FAILED)

            with self.subTest(consumer=consumer, path="preclaim"):
                aggregate = factory()
                aggregate.refresh_from_db()
                enqueue(aggregate)
                event = OutboxMessage.objects.get(
                    aggregate_id=aggregate.id,
                    topic__endswith="requested",
                )
                OutboxMessage.objects.filter(pk=event.id).update(attempts=1)
                event.refresh_from_db()
                OutboxConsumerReceipt.objects.create(
                    event=event,
                    consumer_name=consumer,
                    state="retry",
                    attempts=5,
                )
                result = outbox.consume_event(
                    envelope=outbox.event_envelope(event),
                    consumer_name=consumer,
                    handler=lambda *_: self.fail("exhausted handler must not run"),
                    argument_keys=(payload_key,),
                    terminal_handler=terminal,
                    terminal_argument_keys=(payload_key,),
                    max_attempts=5,
                )
                aggregate.refresh_from_db()
                self.assertEqual(result["state"], "dead_letter")
                self.assertEqual(aggregate.state, ExtractionState.FAILED)

    def test_current_handler_max_attempt_terminalizes_just_released_leaf(self):
        for aggregate, enqueue, consumer, payload_key, terminal, begin, queue in (
            (
                _generic_attempt(),
                tasks._enqueue_generic_extraction,
                "generic-extraction",
                "generic_extraction_attempt_id",
                tasks.finalize_generic_extraction_failure.run,
                services.begin_generic_extraction,
                services.queue_generic_extraction_retry,
            ),
            (
                _document_extraction(),
                tasks._enqueue_document_extraction,
                "document-extraction",
                "document_extraction_id",
                tasks.finalize_document_extraction_failure.run,
                services.begin_document_extraction,
                services.queue_document_extraction_retry,
            ),
        ):
            with self.subTest(consumer=consumer):
                aggregate.refresh_from_db()
                enqueue(aggregate)
                event = OutboxMessage.objects.get(
                    aggregate_id=aggregate.id,
                    topic__endswith="requested",
                )
                OutboxMessage.objects.filter(pk=event.id).update(attempts=1)
                event.refresh_from_db()

                def release_then_fail(aggregate_id):
                    delivery = tasks._current_extraction_delivery(aggregate_id)
                    claimed = begin(aggregate_id, **delivery)
                    queue(
                        aggregate_id,
                        expected_generation=claimed.lease_generation,
                        expected_lease_owner=claimed.lease_owner,
                        expected_lease_token=claimed.lease_token,
                        retry_at=timezone.now(),
                        error_code="temporary",
                        error_detail_redacted="safe",
                    )
                    raise RuntimeError("last handler attempt")

                result = outbox.consume_event(
                    envelope=outbox.event_envelope(event),
                    consumer_name=consumer,
                    handler=release_then_fail,
                    argument_keys=(payload_key,),
                    terminal_handler=terminal,
                    terminal_argument_keys=(payload_key,),
                    max_attempts=1,
                )
                aggregate.refresh_from_db()
                self.assertEqual(result["state"], "dead_letter")
                self.assertEqual(aggregate.state, ExtractionState.FAILED)

    def test_stop_closes_fanout_document_generic_and_running_child(self):
        run, run_source_item, source_item = _lineage()
        document = DocumentExtraction.objects.create(
            run_source_item=run_source_item,
            source_item=source_item,
            input_object_key="raw/test.pdf",
            input_object_version="v1",
            input_kind="pdf",
            input_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
            input_mime_type="application/pdf",
            input_checksum=SHA,
            input_page_count=1,
            expected_page_indices=[0],
        )
        child = _document_child(document)
        attempt = _generic_for_lineage(run_source_item, source_item)
        event_id = uuid.uuid4()
        token = uuid.uuid4()
        services.begin_evidence_fanout(
            run.id,
            source_event_id=event_id,
            delivery_count=3,
            lease_generation=3,
            lease_owner="worker",
            lease_token=token,
        )
        claimed_document = services.begin_document_extraction(
            document.id,
            source_event_id=event_id,
            delivery_count=3,
            lease_generation=3,
            lease_owner="worker",
            lease_token=token,
        )
        services.begin_extraction_run(
            child.id,
            expected_parent_generation=3,
            expected_parent_lease_owner="worker",
            expected_parent_lease_token=token,
        )
        services.begin_generic_extraction(
            attempt.id,
            source_event_id=event_id,
            delivery_count=3,
            lease_generation=3,
            lease_owner="worker",
            lease_token=token,
        )
        CollectionRun.objects.filter(pk=run.id).update(
            stop_requested_at=timezone.now(), state=RunState.STOPPING
        )

        tasks._terminalize_stopped_run_evidence(
            run.id,
            source_event_id=event_id,
            expected_generation=claimed_document.lease_generation,
        )
        step = RunStep.objects.get(run=run, name="extract", attempt_no=1)
        for aggregate in (
            DocumentExtraction.objects.get(pk=document.id),
            GenericExtractionAttempt.objects.get(pk=attempt.id),
            ExtractionRun.objects.get(pk=child.id),
        ):
            self.assertEqual(aggregate.state, ExtractionState.FAILED)
            self.assertEqual(aggregate.lease_owner, "")
            self.assertIsNone(aggregate.lease_token)
            self.assertTrue(aggregate.terminal_event_key)
        self.assertNotEqual(step.state, "running")
        self.assertEqual(step.lease_owner, "")
        self.assertIsNone(step.lease_token)

    def test_leaf_task_without_routed_context_fails_closed(self):
        attempt = _generic_attempt()
        with self.assertRaises(PermanentEventError):
            tasks.process_generic_extraction.run(str(attempt.id))
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, ExtractionState.QUEUED)
        self.assertEqual(attempt.lease_generation, 0)

    def test_legacy_hwp_protocol_generation_is_not_database_lease_generation(self):
        self.assertEqual(tasks._legacy_hwp_protocol_generation(27), 1)

    def test_begin_requires_extracting_collection_run(self):
        attempt = _generic_attempt()
        CollectionRun.objects.filter(pk=attempt.run_source_item.run_id).update(
            state=RunState.FAILED
        )
        claimed = services.begin_generic_extraction(
            attempt.id,
            source_event_id=uuid.uuid4(),
            delivery_count=1,
            lease_generation=1,
            lease_owner="worker",
            lease_token=uuid.uuid4(),
        )
        self.assertIsNone(claimed)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, ExtractionState.QUEUED)
        self.assertEqual(attempt.lease_owner, "")
        self.assertIsNone(attempt.lease_token)

    def test_delayed_leaf_on_failed_run_never_rewrites_run_to_stopped(self):
        attempt = _generic_attempt()
        run_id = attempt.run_source_item.run_id
        CollectionRun.objects.filter(pk=run_id).update(state=RunState.FAILED)
        with _routed_context():
            result = tasks.process_generic_extraction.run(str(attempt.id))
        self.assertEqual(result["state"], ExtractionState.QUEUED)
        self.assertEqual(CollectionRun.objects.get(pk=run_id).state, RunState.FAILED)

    def test_running_and_terminal_database_envelopes_are_enforced(self):
        attempt = _generic_attempt()
        with self.assertRaises(IntegrityError), transaction.atomic():
            GenericExtractionAttempt.objects.filter(pk=attempt.id).update(
                state=ExtractionState.RUNNING,
                lease_owner="",
                lease_token=None,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            GenericExtractionAttempt.objects.filter(pk=attempt.id).update(
                state=ExtractionState.SUCCEEDED,
                result_checksum="9" * 64,
                terminal_event_key=None,
                terminal_state=None,
            )

    def test_generic_terminal_hwp_replay_runs_recovery_without_conversion(self):
        attempt = _generic_attempt()
        attempt.engine = ExtractionEngine.LEGACY_HWP
        attempt.state = ExtractionState.SUCCEEDED
        attempt.result_checksum = "a" * 64
        attempt.expected_evidence_count = 0
        attempt.expected_evidence_manifest_hash = services.canonical_hash([])
        attempt.terminal_event_key = f"evidence.other_ready:{attempt.id}:{attempt.result_checksum}"
        attempt.terminal_state = "ready"
        attempt.save()
        with patch.object(
            tasks,
            "_ensure_legacy_hwp_document",
            return_value=attempt,
        ) as recovery, _routed_context():
            result = tasks.process_generic_extraction.run(str(attempt.id))
        recovery.assert_called_once_with(attempt.id)
        self.assertEqual(result["state"], ExtractionState.SUCCEEDED)

    def test_generic_attempt_replay_keeps_frozen_profile(self):
        attempt = _generic_attempt()
        raw = EvidenceAsset.objects.create(
            source_item=attempt.source_item,
            origin_run_source_item=attempt.run_source_item,
            derivation_type=EvidenceDerivationType.RAW,
            raw_input_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
            kind=EvidenceKind.ATTACHMENT,
            locator_type=LocatorType.STRUCTURED_PATH,
            locator={"path": "attachments"},
            checksum="f" * 64,
            evidence_content_hash="1" * 64,
            review_subject_hash="2" * 64,
        )
        GenericExtractionAttempt.objects.filter(pk=attempt.id).update(input_asset=raw)
        attempt.refresh_from_db()

        with patch.object(tasks, "_profile", side_effect=AssertionError("profile drift")):
            replay = tasks._load_or_create_generic_attempt(
                run_source_item=attempt.run_source_item,
                source_item=attempt.source_item,
                input_asset=raw,
                engine=attempt.engine,
                preferred_key="new-profile-must-not-win",
                fanout_fence=None,
            )
        self.assertEqual(replay.id, attempt.id)
        self.assertEqual(
            replay.extraction_profile_snapshot_id,
            attempt.extraction_profile_snapshot_id,
        )

    def test_document_retry_preserves_succeeded_child_generation_history(self):
        document = _document_extraction()
        child = _document_child(document)
        event_id = uuid.uuid4()
        token = uuid.uuid4()
        claimed = services.begin_document_extraction(
            document.id,
            source_event_id=event_id,
            delivery_count=1,
            lease_generation=1,
            lease_owner="document-worker",
            lease_token=token,
        )
        child.source_event_id = event_id
        child.parent_lease_generation = 1
        child.lease_generation = 1
        child.state = ExtractionState.SUCCEEDED
        child.processed_page_indices = [0]
        child.result_checksum = "b" * 64
        child.expected_evidence_count = 0
        child.expected_evidence_manifest_hash = services.canonical_hash([])
        child.terminal_state = "ready"
        child.terminal_event_key = f"extraction.run.ready:{child.id}:generation:1"
        child.save()

        services.queue_document_extraction_retry(
            document.id,
            expected_generation=claimed.lease_generation,
            expected_lease_owner=claimed.lease_owner,
            expected_lease_token=claimed.lease_token,
            retry_at=timezone.now(),
            error_code="temporary",
            error_detail_redacted="safe",
        )
        child.refresh_from_db()
        self.assertEqual(child.parent_lease_generation, 1)
        self.assertEqual(child.source_event_id, event_id)

    def test_new_parent_generation_rebinds_queued_child_before_permanent_terminal(self):
        document = _document_extraction()
        child = _document_child(document)
        event_id = uuid.uuid4()
        token1 = uuid.uuid4()
        services.begin_document_extraction(
            document.id,
            source_event_id=event_id,
            delivery_count=1,
            lease_generation=1,
            lease_owner="worker-1",
            lease_token=token1,
        )
        ExtractionRun.objects.filter(pk=child.id).update(
            source_event_id=event_id, parent_lease_generation=1
        )
        services.queue_document_extraction_retry(
            document.id,
            expected_generation=1,
            expected_lease_owner="worker-1",
            expected_lease_token=token1,
            retry_at=timezone.now(),
            error_code="temporary",
            error_detail_redacted="safe",
        )
        token2 = uuid.uuid4()
        claimed = services.begin_document_extraction(
            document.id,
            source_event_id=event_id,
            delivery_count=2,
            lease_generation=2,
            lease_owner="worker-2",
            lease_token=token2,
        )
        tasks._terminalize_document_extraction(
            document.id,
            error_code="permanent",
            error_detail_redacted="safe",
            expected_generation=2,
            expected_source_event_id=event_id,
            expected_lease_owner="worker-2",
            expected_lease_token=token2,
        )
        child.refresh_from_db()
        self.assertEqual(claimed.lease_generation, 2)
        self.assertEqual(child.state, ExtractionState.FAILED)
        self.assertEqual(child.parent_lease_generation, 2)

    def test_extraction_object_write_reservation_is_generation_scoped(self):
        model = __import__(
            "apps.evidence.models", fromlist=["ExtractionObjectWriteReservation"]
        ).ExtractionObjectWriteReservation
        required = {
            "aggregate_kind",
            "aggregate_id",
            "source_event_id",
            "lease_generation",
            "lease_identity_hash",
            "purpose",
            "object_key",
            "state",
            "object_version",
            "checksum",
            "byte_size",
        }
        self.assertLessEqual(required, {field.name for field in model._meta.fields})
        self.assertTrue(any(
            constraint.name == "uq_extraction_object_write_generation"
            for constraint in model._meta.constraints
        ))

    def test_terminal_identity_is_database_immutable(self):
        attempt = _generic_attempt()
        attempt.state = ExtractionState.FAILED
        attempt.terminal_event_key = f"terminal:{attempt.id}"
        attempt.terminal_state = "failed"
        attempt.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            GenericExtractionAttempt.objects.filter(pk=attempt.id).update(
                terminal_event_key=f"terminal:rewritten:{attempt.id}"
            )

    def test_terminal_hwp_replay_on_failed_run_is_strict_noop(self):
        attempt = _generic_attempt()
        attempt.engine = ExtractionEngine.LEGACY_HWP
        attempt.state = ExtractionState.SUCCEEDED
        attempt.result_checksum = "a" * 64
        attempt.expected_evidence_count = 0
        attempt.expected_evidence_manifest_hash = services.canonical_hash([])
        attempt.terminal_event_key = f"evidence.other_ready:{attempt.id}:{attempt.result_checksum}"
        attempt.terminal_state = "ready"
        attempt.save()
        CollectionRun.objects.filter(pk=attempt.run_source_item.run_id).update(
            state=RunState.FAILED
        )
        with patch.object(tasks, "_ensure_legacy_hwp_document") as recovery:
            with _routed_context():
                result = tasks.process_generic_extraction.run(str(attempt.id))
        recovery.assert_not_called()
        self.assertEqual(result["state"], ExtractionState.SUCCEEDED)

    def test_new_lease_orphans_crash_reservations_and_rejects_old_writer(self):
        model = __import__(
            "apps.evidence.models", fromlist=["ExtractionObjectWriteReservation"]
        ).ExtractionObjectWriteReservation
        attempt = _generic_attempt()
        source_event_id = uuid.uuid4()
        first_token = uuid.uuid4()
        first = services.begin_generic_extraction(
            attempt.id,
            source_event_id=source_event_id,
            delivery_count=1,
            lease_generation=1,
            lease_owner="worker-1",
            lease_token=first_token,
        )
        reserved = services.reserve_extraction_object_write(
            aggregate_kind="generic",
            aggregate_id=attempt.id,
            source_event_id=source_event_id,
            lease_generation=1,
            lease_owner="worker-1",
            lease_token=first_token,
            purpose="result",
            object_key="evidence/results/crash.json",
        )
        services.mark_extraction_object_uploaded(
            reserved.id,
            object_version="v1",
            checksum=SHA,
            byte_size=10,
        )
        services.begin_generic_extraction(
            attempt.id,
            source_event_id=source_event_id,
            delivery_count=2,
            lease_generation=2,
            lease_owner="worker-2",
            lease_token=uuid.uuid4(),
        )
        reserved.refresh_from_db()
        self.assertEqual(reserved.state, "orphaned")
        with self.assertRaises(services.EvidenceConflict):
            services.reserve_extraction_object_write(
                aggregate_kind="generic",
                aggregate_id=attempt.id,
                source_event_id=source_event_id,
                lease_generation=first.lease_generation,
                lease_owner=first.lease_owner,
                lease_token=first.lease_token,
                purpose="result",
                object_key="evidence/results/stale.json",
            )
        self.assertFalse(hasattr(model.objects, "delete_shared_key_immediately"))

    def test_result_reason_and_converted_puts_use_reservation_lifecycle(self):
        model = __import__(
            "apps.evidence.models", fromlist=["ExtractionObjectWriteReservation"]
        ).ExtractionObjectWriteReservation
        attempt = _generic_attempt()
        token = uuid.uuid4()
        attempt = services.begin_generic_extraction(
            attempt.id,
            source_event_id=uuid.uuid4(),
            delivery_count=1,
            lease_generation=1,
            lease_owner="worker",
            lease_token=token,
        )
        storage_patcher = patch.object(tasks, "_storage")
        storage = storage_patcher.start()
        self.addCleanup(storage_patcher.stop)
        storage.return_value.put_bytes.side_effect = lambda **kwargs: tasks.ObjectInfo(
            key=kwargs["key"],
            version_id="v1",
            checksum_sha256=kwargs["checksum_sha256"],
            size=len(kwargs["data"]),
            content_type=kwargs["content_type"],
            etag=None,
        )
        for namespace, purpose in (("generic", "result"), ("reasons", "reason")):
            _, reservation_id = tasks._store_result(
                namespace, attempt, {"value": namespace}
            )
            with transaction.atomic():
                self.assertTrue(services.bind_extraction_object_write(reservation_id))
            self.assertEqual(model.objects.get(pk=reservation_id).state, "bound")
            self.assertEqual(model.objects.get(pk=reservation_id).purpose, purpose)

        with tempfile.TemporaryDirectory() as temp_dir:
            converted = Path(temp_dir) / "converted.pdf"
            converted.write_bytes(b"%PDF-test")
            checksum = __import__("hashlib").sha256(converted.read_bytes()).hexdigest()
            storage.return_value.put_file.return_value = tasks.ObjectInfo(
                key="evidence/converted/test.pdf",
                version_id="v2",
                checksum_sha256=checksum,
                size=converted.stat().st_size,
                content_type="application/pdf",
                etag=None,
            )
            _, reservation_id = tasks._store_converted_pdf(
                attempt,
                converted_path=converted,
                converted_checksum=checksum,
                converted_size=converted.stat().st_size,
            )
            with transaction.atomic():
                services.bind_extraction_object_write(reservation_id)
            self.assertEqual(model.objects.get(pk=reservation_id).state, "bound")
            self.assertEqual(model.objects.get(pk=reservation_id).purpose, "converted")
        storage.return_value.delete.assert_not_called()

    def test_raw_put_binds_or_orphans_after_stop_without_deleting_shared_key(self):
        model = __import__(
            "apps.evidence.models", fromlist=["ExtractionObjectWriteReservation"]
        ).ExtractionObjectWriteReservation
        run, run_source_item, _ = _lineage()
        event_id = uuid.uuid4()
        token = uuid.uuid4()
        claimed = services.begin_evidence_fanout(
            run.id,
            source_event_id=event_id,
            delivery_count=1,
            lease_generation=1,
            lease_owner="fanout-worker",
            lease_token=token,
        )
        fence = {
            "expected_generation": claimed.lease_generation,
            "expected_lease_owner": claimed.lease_owner,
            "expected_lease_token": claimed.lease_token,
        }
        def put_bytes(**kwargs):
            return tasks.ObjectInfo(
                key=kwargs["key"],
                version_id="v1",
                checksum_sha256=kwargs["checksum_sha256"],
                size=len(kwargs["data"]),
                content_type=kwargs["content_type"],
                etag=None,
            )

        rights = {
            "rights_status": "allowed",
            "rights_basis_url": "https://example.com/rights",
            "attribution_text": None,
            "manual_review_required": False,
        }
        with patch.object(tasks, "_rights", return_value=rights), patch.object(
            tasks, "_storage"
        ) as storage_factory:
            storage_factory.return_value.put_bytes.side_effect = put_bytes
            first_evidence, first_info = tasks._persist_attachment(
                run_source_item,
                {"title": "raw", "url": "https://example.com/raw.pdf"},
                b"raw-one",
                "application/pdf",
                "raw.pdf",
                fanout_fence=fence,
            )
            self.assertEqual(
                model.objects.get(aggregate_kind="fanout").state, "bound"
            )
            replay_evidence, replay_info = tasks._persist_attachment(
                run_source_item,
                {"title": "raw", "url": "https://example.com/raw.pdf"},
                b"raw-one",
                "application/pdf",
                "raw.pdf",
                fanout_fence=fence,
            )
            self.assertEqual(replay_evidence.id, first_evidence.id)
            self.assertEqual(replay_info.version_id, first_info.version_id)
            self.assertEqual(storage_factory.return_value.put_bytes.call_count, 1)

            def stop_after_put(**kwargs):
                info = put_bytes(**kwargs)
                CollectionRun.objects.filter(pk=run.id).update(
                    stop_requested_at=timezone.now(), state=RunState.STOPPING
                )
                return info

            storage_factory.return_value.put_bytes.side_effect = stop_after_put
            with self.assertRaises(services.EvidenceConflict):
                tasks._persist_attachment(
                    run_source_item,
                    {"title": "raw2", "url": "https://example.com/raw2.pdf"},
                    b"raw-two",
                    "application/pdf",
                    "raw2.pdf",
                    fanout_fence=fence,
                )
            self.assertEqual(
                model.objects.filter(state="orphaned").count(), 1
            )
            orphan = model.objects.get(state="orphaned")
            self.assertEqual(orphan.object_version, "v1")
            self.assertEqual(
                orphan.checksum,
                __import__("hashlib").sha256(b"raw-two").hexdigest(),
            )
            self.assertEqual(orphan.byte_size, len(b"raw-two"))
            storage_factory.return_value.delete.assert_not_called()

    def test_fanout_terminal_cleanup_orphans_current_generation_residual_write(self):
        model = __import__(
            "apps.evidence.models", fromlist=["ExtractionObjectWriteReservation"]
        ).ExtractionObjectWriteReservation
        run, _, _ = _lineage()
        event_id = uuid.uuid4()
        token = uuid.uuid4()
        step = services.begin_evidence_fanout(
            run.id,
            source_event_id=event_id,
            delivery_count=1,
            lease_generation=1,
            lease_owner="fanout-worker",
            lease_token=token,
        )
        reservation = services.reserve_extraction_object_write(
            aggregate_kind="fanout",
            aggregate_id=run.id,
            source_event_id=event_id,
            lease_generation=step.lease_generation,
            lease_owner=step.lease_owner,
            lease_token=step.lease_token,
            purpose="raw",
            object_key="evidence/raw/crash.pdf",
        )
        services.mark_extraction_object_uploaded(
            reservation.id,
            object_version="v1",
            checksum=SHA,
            byte_size=10,
        )
        CollectionRun.objects.filter(pk=run.id).update(
            stop_requested_at=timezone.now(), state=RunState.STOPPING
        )
        tasks._terminalize_stopped_run_evidence(
            run.id,
            source_event_id=event_id,
            expected_generation=1,
        )
        reservation.refresh_from_db()
        self.assertEqual(reservation.state, "orphaned")
        self.assertEqual(model.objects.filter(state="orphaned").count(), 1)

    def test_old_unfenced_queue_mutation_helpers_are_removed(self):
        self.assertFalse(hasattr(tasks, "_queue_document_retry"))
        self.assertFalse(hasattr(tasks, "_queue_generic_retry"))

    def test_generation_trigger_forward_and_backward_are_operational(self):
        migration = importlib.import_module(
            "apps.evidence.migrations.0005_extraction_generation_fencing"
        )
        document = _document_extraction()
        child = _document_child(document)
        event_id = uuid.uuid4()
        DocumentExtraction.objects.filter(pk=document.id).update(
            source_event_id=event_id,
            lease_generation=1,
            delivery_count=1,
        )
        ExtractionRun.objects.filter(pk=child.id).update(
            source_event_id=event_id,
            parent_lease_generation=1,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExtractionRun.objects.filter(pk=child.id).update(
                parent_lease_generation=2
            )
        with connection.schema_editor() as editor:
            migration.drop_parent_generation_triggers(None, editor)
        ExtractionRun.objects.filter(pk=child.id).update(parent_lease_generation=2)
        ExtractionRun.objects.filter(pk=child.id).update(parent_lease_generation=1)
        with connection.schema_editor() as editor:
            migration.create_parent_generation_triggers(None, editor)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExtractionRun.objects.filter(pk=child.id).update(
                parent_lease_generation=2
            )

    def test_parent_generation_trigger_rebinds_active_child(self):
        document = _document_extraction()
        child = _document_child(document)
        event_id = uuid.uuid4()
        document_token = uuid.uuid4()
        child_token = uuid.uuid4()
        DocumentExtraction.objects.filter(pk=document.id).update(
            state=ExtractionState.RUNNING,
            source_event_id=event_id,
            lease_generation=1,
            delivery_count=1,
            lease_owner="document-worker",
            lease_token=document_token,
        )
        ExtractionRun.objects.filter(pk=child.id).update(
            state=ExtractionState.RUNNING,
            source_event_id=event_id,
            parent_lease_generation=1,
            lease_generation=1,
            delivery_count=1,
            lease_owner="child-worker",
            lease_token=child_token,
        )

        DocumentExtraction.objects.filter(pk=document.id).update(
            lease_generation=2,
            delivery_count=2,
        )

        child.refresh_from_db()
        self.assertEqual(child.parent_lease_generation, 2)
        self.assertEqual(child.source_event_id, event_id)
        self.assertEqual(child.state, ExtractionState.QUEUED)
        self.assertEqual(child.lease_owner, "")
        self.assertIsNone(child.lease_token)

    def test_ready_dlq_locks_run_before_aggregate_after_identity_lookup(self):
        document = _document_extraction()
        run_id = document.run_source_item.run_id
        CollectionRun.objects.filter(pk=run_id).update(state=RunState.FAILED)
        sql = []

        def capture(execute, statement, params, many, context):
            sql.append(statement.lower())
            return execute(statement, params, many, context)

        with connection.execute_wrapper(capture):
            tasks.finalize_document_ready_wake_failure.run(
                str(document.id), "delivery_exhausted"
            )
        document_selects = [
            index
            for index, statement in enumerate(sql)
            if "evidence_documentextraction" in statement
        ]
        run_selects = [
            index
            for index, statement in enumerate(sql)
            if "collection_collectionrun" in statement
        ]
        self.assertGreaterEqual(len(document_selects), 2)
        self.assertTrue(run_selects)
        self.assertLess(run_selects[0], document_selects[1])

    def test_migration_rearms_historical_running_and_preserves_exact_pending_ready(self):
        running = _generic_attempt()
        running.refresh_from_db()
        tasks._enqueue_generic_extraction(running)
        requested = OutboxMessage.objects.get(
            topic="evidence.other_extract_requested", aggregate_id=running.id
        )
        receipt = OutboxConsumerReceipt.objects.create(
            event=requested,
            consumer_name="generic-extraction",
            state="retry",
            attempts=2,
        )

        completed_delivery = _generic_attempt()
        completed_delivery.refresh_from_db()
        tasks._enqueue_generic_extraction(completed_delivery)
        completed_event = OutboxMessage.objects.get(
            topic="evidence.other_extract_requested",
            aggregate_id=completed_delivery.id,
        )
        completed_event.status = "published"
        completed_event.published_at = timezone.now()
        completed_event.save(update_fields=("status", "published_at"))
        completed_receipt = OutboxConsumerReceipt.objects.create(
            event=completed_event,
            consumer_name="generic-extraction",
            state="succeeded",
            attempts=1,
            completed_at=timezone.now(),
        )

        invalid_delivery = _generic_attempt()
        invalid_delivery.refresh_from_db()
        tasks._enqueue_generic_extraction(invalid_delivery)
        invalid_event = OutboxMessage.objects.get(
            topic="evidence.other_extract_requested",
            aggregate_id=invalid_delivery.id,
        )
        OutboxConsumerReceipt.objects.create(
            event=invalid_event,
            consumer_name="generic-extraction",
            state="retry",
            attempts=1,
        )
        OutboxMessage.objects.filter(pk=invalid_event.id).update(
            immutable_material_hash="0" * 64
        )

        ready = _generic_attempt()
        raw = EvidenceAsset.objects.create(
            source_item=ready.source_item,
            origin_run_source_item=ready.run_source_item,
            derivation_type=EvidenceDerivationType.RAW,
            raw_input_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
            kind=EvidenceKind.ATTACHMENT,
            locator_type=LocatorType.STRUCTURED_PATH,
            locator={"path": "attachments"},
            checksum="a" * 64,
            evidence_content_hash=services.evidence_content_hash(
                text=None, structured_data=None, checksum=None
            ),
            review_subject_hash="c" * 64,
        )
        GenericExtractionAttempt.objects.filter(pk=ready.id).update(input_asset=raw)
        ready.refresh_from_db()
        checksum = "d" * 64
        evidence = EvidenceAsset.objects.create(
            source_item=ready.source_item,
            origin_run_source_item=ready.run_source_item,
            derivation_type=EvidenceDerivationType.OTHER,
            generic_extraction_attempt=ready,
            parent_asset=raw,
            kind=EvidenceKind.TEXT,
            locator_type=LocatorType.HTML_DOM,
            locator={
                "locator_type": LocatorType.HTML_DOM,
                "css_selector": "main",
                "xpath": None,
            },
            extraction_method=ready.engine,
            extractor_version=ready.extractor_version,
            extraction_config_hash=ready.config_hash,
            validation_mode=ready.validation_mode,
            extraction_result_checksum=checksum,
            evidence_content_hash=services.evidence_content_hash(
                text=None, structured_data=None, checksum=None
            ),
            review_subject_hash="f" * 64,
        )
        evidence.refresh_from_db()
        ready.state = ExtractionState.SUCCEEDED
        ready.result_checksum = checksum
        ready.evidence_asset = evidence
        ready.expected_evidence_count = 1
        ready.terminal_event_key = f"evidence.other_ready:{ready.id}:{checksum}"
        ready.terminal_state = "ready"
        evidence.generic_extraction_attempt = ready
        evidence.review_subject_hash = services.calculate_review_subject_hash(evidence)
        evidence.publishable = services.calculate_publishable(evidence)
        evidence.save(update_fields=("review_subject_hash", "publishable", "updated_at"))
        ready.expected_evidence_manifest_hash = services.generic_evidence_manifest_hash([evidence])
        ready.save()
        tasks.enqueue_event(
            topic="evidence.other_ready",
            aggregate_type="GenericExtractionAttempt",
            aggregate_id=ready.id,
            message_key=ready.terminal_event_key,
            job_id=ready.run_source_item.run_id,
            payload={
                "run_id": str(ready.run_source_item.run_id),
                "run_source_item_id": str(ready.run_source_item_id),
                "source_item_id": str(ready.source_item_id),
                "generic_extraction_attempt_id": str(ready.id),
                "evidence_asset_id": str(evidence.id),
                "engine": ready.engine,
                "locator_type": evidence.locator_type,
                "validation_mode": ready.validation_mode,
                "result_checksum": checksum,
                "extraction_fingerprint": ready.extraction_fingerprint,
            },
        )
        ready_document = _document_extraction()
        ready_child = _document_child(ready_document)
        ready_child.refresh_from_db()
        child_checksum = "2" * 64
        locator = {
            "page_index": 0,
            "block_id": "legacy-block",
            "block_type": "text",
            "bbox": [0, 0, 1, 1],
            "polygon": None,
            "reading_order": 0,
        }
        content_hash = "3" * 64
        document_evidence = EvidenceAsset.objects.create(
            source_item=ready_document.source_item,
            origin_run_source_item=ready_document.run_source_item,
            derivation_type=EvidenceDerivationType.DOCUMENT,
            document_extraction=ready_document,
            extraction_run=ready_child,
            kind=EvidenceKind.TEXT,
            locator_type=LocatorType.DOCUMENT_BLOCK,
            locator=locator,
            extraction_method=ready_child.engine,
            extractor_version=ready_child.package_version,
            extraction_config_hash=ready_child.config_hash,
            extraction_result_checksum=child_checksum,
            evidence_content_hash=content_hash,
            review_subject_hash="4" * 64,
        )
        manifest_entry = services.document_evidence_manifest_entry(
            source_item_id=ready_document.source_item_id,
            origin_run_source_item_id=ready_document.run_source_item_id,
            parent_asset_id=ready_document.input_asset_id,
            document_extraction_id=ready_document.id,
            extraction_run_id=ready_child.id,
            extraction_profile_snapshot_id=ready_child.extraction_profile_snapshot_id,
            profile_material_hash=ready_child.profile_material_hash,
            extraction_method=ready_child.engine,
            extractor_version=ready_child.package_version,
            extraction_config_hash=ready_child.config_hash,
            result_checksum=child_checksum,
            locator=locator,
            evidence_content_hash_value=content_hash,
        )
        ready_child.state = ExtractionState.SUCCEEDED
        ready_child.processed_page_indices = [0]
        ready_child.result_checksum = child_checksum
        ready_child.expected_evidence_count = 1
        ready_child.expected_evidence_manifest_hash = services.canonical_hash(
            [manifest_entry]
        )
        ready_child.terminal_event_key = (
            f"extraction.run.ready:{ready_child.id}:generation:0"
        )
        ready_child.terminal_state = "ready"
        ready_child.save()
        ready_document.routing_manifest = {
            "schema_version": "extraction-routing-v2",
            "pages": [{
                "page_index": 0,
                "selected_run_id": str(ready_child.id),
                "profile_snapshot_id": str(
                    ready_child.extraction_profile_snapshot_id
                ),
                "engine": ready_child.engine,
                "reason": "native_text",
            }],
        }
        ready_document.save(update_fields=("routing_manifest", "updated_at"))
        ready_document = services.aggregate_document_extraction(ready_document.id)
        tasks.enqueue_event(
            topic="evidence.document_ready",
            aggregate_type="DocumentExtraction",
            aggregate_id=ready_document.id,
            message_key=ready_document.terminal_event_key,
            job_id=ready_document.run_source_item.run_id,
            payload={
                "run_id": str(ready_document.run_source_item.run_id),
                "run_source_item_id": str(ready_document.run_source_item_id),
                "source_item_id": str(ready_document.source_item_id),
                "document_extraction_id": str(ready_document.id),
                "input_page_count": ready_document.input_page_count,
                "coverage_manifest_hash": ready_document.coverage_manifest_hash,
                "selected_evidence_manifest_hash": (
                    ready_document.selected_evidence_manifest_hash
                ),
                "document_complete": True,
            },
        )
        unproven = _generic_attempt()

        under_test = [("evidence", "0005_extraction_generation_fencing")]
        latest = [("evidence", "0006_generic_evidence_manifest")]
        executor = MigrationExecutor(connection)
        try:
            executor.migrate([("evidence", "0004_extractionprofiledecision_report_envelope")])
            old_apps = executor.loader.project_state(
                [("evidence", "0004_extractionprofiledecision_report_envelope")]
            ).apps
            OldAttempt = old_apps.get_model("evidence", "GenericExtractionAttempt")
            OldAttempt.objects.filter(pk=running.id).update(state="running")
            OldAttempt.objects.filter(pk=completed_delivery.id).update(state="running")
            OldAttempt.objects.filter(pk=invalid_delivery.id).update(state="running")
            OldAttempt.objects.filter(pk=unproven.id).update(state="succeeded")
            executor = MigrationExecutor(connection)
            executor.migrate(under_test)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(latest)

        running.refresh_from_db()
        ready.refresh_from_db()
        ready_document.refresh_from_db()
        unproven.refresh_from_db()
        requested.refresh_from_db()
        receipt.refresh_from_db()
        completed_delivery.refresh_from_db()
        completed_event.refresh_from_db()
        completed_receipt.refresh_from_db()
        invalid_delivery.refresh_from_db()
        invalid_event.refresh_from_db()
        self.assertEqual(running.state, ExtractionState.QUEUED)
        self.assertEqual(running.source_event_id, requested.id)
        self.assertEqual(requested.status, "pending")
        self.assertEqual(receipt.state, "retry")
        self.assertIsNone(receipt.lease_token)
        self.assertEqual(completed_receipt.state, "succeeded")
        self.assertEqual(completed_event.status, "published")
        self.assertEqual(completed_delivery.state, ExtractionState.FAILED)
        self.assertEqual(
            CollectionRun.objects.get(pk=completed_delivery.run_source_item.run_id).state,
            RunState.FAILED,
        )
        self.assertEqual(invalid_delivery.state, ExtractionState.FAILED)
        self.assertEqual(
            CollectionRun.objects.get(pk=invalid_delivery.run_source_item.run_id).state,
            RunState.FAILED,
        )
        self.assertEqual(invalid_event.immutable_material_hash, "0" * 64)
        self.assertEqual(ready.state, ExtractionState.SUCCEEDED)
        self.assertEqual(ready.terminal_event_key, f"evidence.other_ready:{ready.id}:{checksum}")
        self.assertEqual(ready.expected_evidence_count, 1, ready.error_detail_redacted)
        self.assertEqual(unproven.state, ExtractionState.FAILED)
        self.assertEqual(unproven.error_code, "legacy_ready_identity_unproven")
        services.validate_generic_evidence_set(
            ready,
            list(ready.derived_evidence_assets.order_by("id")),
        )
        with _routed_context():
            result = tasks.consume_other_ready.run(
                str(ready.run_source_item.run_id),
                str(ready.run_source_item_id),
                str(ready.source_item_id),
                str(evidence.id),
                str(ready.id),
                ready.engine,
                evidence.locator_type,
                ready.validation_mode,
                checksum,
                None,
                None,
                None,
                None,
                ready.extraction_fingerprint,
            )
        self.assertEqual(result["runId"], str(ready.run_source_item.run_id))
        document_result = tasks.consume_document_ready.run(
            str(ready_document.run_source_item.run_id),
            str(ready_document.run_source_item_id),
            str(ready_document.source_item_id),
            str(ready_document.id),
            ready_document.input_page_count,
            ready_document.coverage_manifest_hash,
            ready_document.selected_evidence_manifest_hash,
            True,
        )
        self.assertEqual(
            document_result["runId"], str(ready_document.run_source_item.run_id)
        )

    def test_migration_refuses_active_processing_delivery_until_drained(self):
        attempt = _generic_attempt()
        attempt.refresh_from_db()
        tasks._enqueue_generic_extraction(attempt)
        event = OutboxMessage.objects.get(
            topic="evidence.other_extract_requested", aggregate_id=attempt.id
        )
        receipt = OutboxConsumerReceipt.objects.create(
            event=event,
            consumer_name="generic-extraction",
            state="processing",
            attempts=1,
            claimed_at=timezone.now(),
            claimed_until=timezone.now() + __import__("datetime").timedelta(minutes=5),
            lease_token=uuid.uuid4(),
            lease_generation=1,
        )
        old_target = [("evidence", "0004_extractionprofiledecision_report_envelope")]
        under_test = [("evidence", "0005_extraction_generation_fencing")]
        latest = [("evidence", "0006_generic_evidence_manifest")]
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(old_target)
            old_apps = executor.loader.project_state(old_target).apps
            old_apps.get_model("evidence", "GenericExtractionAttempt").objects.filter(
                pk=attempt.id
            ).update(state="running")
            executor = MigrationExecutor(connection)
            with self.assertRaisesRegex(RuntimeError, "drain"):
                executor.migrate(under_test)
            OutboxConsumerReceipt.objects.filter(pk=receipt.id).update(
                state="retry",
                claimed_at=None,
                claimed_until=None,
                lease_token=None,
            )
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(latest)

    def test_collection_migration_never_rearms_terminal_or_ambiguous_fanout(self):
        terminal_run, _, _ = _lineage()
        event = tasks.enqueue_event(
            topic="run.evidence_requested",
            aggregate_type="collection_run",
            aggregate_id=terminal_run.id,
            message_key=f"run.evidence_requested:{terminal_run.id}",
            job_id=terminal_run.id,
            payload={"run_id": str(terminal_run.id)},
        )
        event.status = "published"
        event.published_at = timezone.now()
        event.save(update_fields=("status", "published_at"))
        receipt = OutboxConsumerReceipt.objects.create(
            event=event,
            consumer_name="run-evidence",
            state="succeeded",
            attempts=1,
            completed_at=timezone.now(),
        )
        RunStep.objects.create(
            run=terminal_run,
            name="extract",
            attempt_no=1,
            state="running",
            source_event_id=event.id,
            delivery_count=1,
            lease_generation=1,
            lease_owner="legacy-worker",
            lease_token=uuid.uuid4(),
        )

        ambiguous_run, _, _ = _lineage()
        RunStep.objects.create(
            run=ambiguous_run,
            name="extract",
            attempt_no=1,
            state="running",
            source_event_id=uuid.uuid4(),
            delivery_count=1,
            lease_generation=1,
            lease_owner="legacy-worker",
            lease_token=uuid.uuid4(),
        )

        old_targets = [
            ("evidence", "0004_extractionprofiledecision_report_envelope"),
            ("collection", "0008_source_access_policy_runtime"),
        ]
        collection_latest = [
            ("evidence", "0004_extractionprofiledecision_report_envelope"),
            ("collection", "0009_run_step_generation_fencing"),
        ]
        latest = [("evidence", "0006_generic_evidence_manifest")]
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(old_targets)
            old_apps = executor.loader.project_state(old_targets).apps
            OldStep = old_apps.get_model("collection", "RunStep")
            OldStep.objects.filter(
                run_id__in=(terminal_run.id, ambiguous_run.id), name="extract"
            ).update(state="running")
            executor = MigrationExecutor(connection)
            executor.migrate(collection_latest)
            terminal_run.refresh_from_db()
            ambiguous_run.refresh_from_db()
            terminal_step = RunStep.objects.get(run=terminal_run, name="extract")
            ambiguous_step = RunStep.objects.get(run=ambiguous_run, name="extract")
            receipt.refresh_from_db()
            event.refresh_from_db()
            self.assertEqual(receipt.state, "succeeded")
            self.assertEqual(event.status, "published")
            self.assertEqual(terminal_run.state, RunState.FAILED)
            self.assertEqual(terminal_step.recovery_state, "manual_required")
            self.assertEqual(ambiguous_run.state, RunState.FAILED)
            self.assertEqual(ambiguous_step.recovery_state, "manual_required")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(latest)

class SelectedPageCoverageTests(TestCase):
    def test_ready_routes_use_identity_validating_handlers_with_full_payload(self):
        document = route_for("evidence.document_ready", 1)
        other = route_for("evidence.other_ready", 1)
        self.assertEqual(
            document.task_name,
            "apps.evidence.tasks.consume_document_ready",
        )
        self.assertEqual(
            other.task_name,
            "apps.evidence.tasks.consume_other_ready",
        )
        self.assertIn("coverage_manifest_hash", document.argument_keys)
        self.assertIn("selected_evidence_manifest_hash", document.argument_keys)
        self.assertIn("result_checksum", other.argument_keys)
        self.assertIn("evidence_asset_id", other.argument_keys)

    def test_optional_ready_fields_may_be_omitted_by_legacy_event(self):
        route = route_for("evidence.other_ready", 1)
        payload = {
            "run_id": str(uuid.uuid4()),
            "run_source_item_id": str(uuid.uuid4()),
            "source_item_id": str(uuid.uuid4()),
            "evidence_asset_id": str(uuid.uuid4()),
            "generic_extraction_attempt_id": str(uuid.uuid4()),
            "engine": ExtractionEngine.HTML,
            "locator_type": LocatorType.HTML_DOM,
            "validation_mode": GenericValidationMode.DETERMINISTIC,
            "result_checksum": SHA,
            "extraction_fingerprint": SHA,
        }
        args = outbox.routed_event_arguments(
            event_type="evidence.other_ready",
            event_version=1,
            argument_keys=route.argument_keys,
            payload=payload,
        )
        values = dict(zip(route.argument_keys, args, strict=True))
        self.assertIsNone(values["low_confidence_reasons_hash"])
        self.assertIsNone(values["calibration_profile_hash"])

    def test_selected_page_coverage_rejects_pages_outside_the_child_request(self):
        run = SimpleNamespace(
            id=uuid.uuid4(),
            requested_page_indices=[0],
            processed_page_indices=[0, 1],
        )
        manifest = {
            "pages": [
                {"page_index": 0, "selected_run_id": str(run.id)},
                {"page_index": 1, "selected_run_id": str(run.id)},
            ]
        }

        with self.assertRaises(services.EvidenceInvariantError):
            services.validate_selected_page_coverage(
                routing_manifest=manifest,
                expected_page_indices=[0, 1],
                runs_by_id={str(run.id): run},
            )

    def test_reused_document_identity_ignores_inspection_derived_page_fields(self):
        document = SimpleNamespace(
            run_source_item_id=uuid.uuid4(),
            source_item_id=uuid.uuid4(),
            input_asset_id=uuid.uuid4(),
            input_object_key="raw/key",
            input_object_version="v1",
            input_kind="pdf",
            input_mime_type="application/pdf",
            input_checksum=SHA,
            input_page_count=8,
            expected_page_indices=list(range(8)),
            input_frame_count=None,
        )

        tasks._validate_reused_document_identity(
            document,
            run_source_item_id=document.run_source_item_id,
            source_item_id=document.source_item_id,
            input_asset_id=document.input_asset_id,
            input_object_key=document.input_object_key,
            input_object_version=document.input_object_version,
            input_kind=document.input_kind,
            input_mime_type=document.input_mime_type,
            input_checksum=document.input_checksum,
            input_page_count=1,
            expected_page_indices=[0],
            input_frame_count=None,
        )

    def test_migration_dependencies_include_fanout_and_receipt_schema(self):
        fanout_migration = importlib.import_module(
            "apps.collection.migrations.0009_run_step_generation_fencing"
        ).Migration
        migration = importlib.import_module(
            "apps.evidence.migrations.0005_extraction_generation_fencing"
        ).Migration
        self.assertIn(
            ("evidence", "0004_extractionprofiledecision_report_envelope"),
            fanout_migration.dependencies,
        )
        self.assertIn(("collection", "0009_run_step_generation_fencing"), migration.dependencies)
        self.assertIn(
            ("infrastructure", "0002_outboxconsumerreceipt_and_more"),
            migration.dependencies,
        )


class GenericMultiRecordEvidenceTests(TransactionTestCase):
    def _terminal_generic_evidence(
        self,
        attempt,
        *,
        checksum="a" * 64,
        state=ExtractionState.SUCCEEDED,
    ):
        # Keep the in-memory relation used by review hashing aligned with the
        # terminal envelope that is persisted below.
        attempt.state = state
        attempt.result_checksum = checksum
        evidence = EvidenceAsset.objects.create(
            source_item=attempt.source_item,
            origin_run_source_item=attempt.run_source_item,
            derivation_type=EvidenceDerivationType.OTHER,
            generic_extraction_attempt=attempt,
            kind=EvidenceKind.TEXT,
            locator_type=LocatorType.HTML_DOM,
            locator={
                "locator_type": LocatorType.HTML_DOM,
                "css_selector": "main > p",
                "xpath": None,
            },
            extracted_text="original",
            structured_data={"record_index": 0},
            extraction_method=attempt.engine,
            extractor_version=attempt.extractor_version,
            extraction_config_hash=attempt.config_hash,
            validation_mode=attempt.validation_mode,
            extraction_result_checksum=checksum,
            rights_status="allowed",
            rights_basis_url="https://example.com/rights",
            review_state="passed",
            evidence_content_hash=services.evidence_content_hash(
                text="original", structured_data={"record_index": 0}, checksum=None
            ),
            review_subject_hash=SHA,
        )
        evidence.review_subject_hash = services.calculate_review_subject_hash(evidence)
        evidence.publishable = services.calculate_publishable(evidence)
        evidence.save(update_fields=("review_subject_hash", "publishable", "updated_at"))
        manifest_hash = services.generic_evidence_manifest_hash([evidence])
        GenericExtractionAttempt.objects.filter(pk=attempt.id).update(
            state=state,
            result_checksum=checksum,
            evidence_asset=evidence,
            expected_evidence_count=1,
            expected_evidence_manifest_hash=manifest_hash,
            terminal_event_key=f"evidence.other_ready:{attempt.id}:{checksum}",
            terminal_state="ready",
        )
        attempt.refresh_from_db()
        return evidence

    def test_document_replay_uses_frozen_inspection_profile_without_latest_selection(self):
        document = _document_extraction()
        token = uuid.uuid4()
        claimed = services.begin_document_extraction(
            document.id,
            source_event_id=uuid.uuid4(),
            delivery_count=1,
            lease_generation=1,
            lease_owner="worker",
            lease_token=token,
        )
        frozen = ExtractionProfileSnapshot.objects.create(
            profile_key="native-pdf-v1",
            profile_version="1.1.0",
            engine=ExtractionEngine.NATIVE_PDF,
            extractor_version="1.0.0",
            package_version="1.28.0",
            runtime_version="Python-3.12.10",
            implementation_manifest_hash=SHA,
            config={"python_runtime_version": "3.12.10"},
            config_hash=SHA,
            profile_material_hash="2" * 64,
        )
        safety = {
            "engine": frozen.engine,
            "profile_snapshot_id": str(frozen.id),
            "profile_material_hash": frozen.profile_material_hash,
            "config_hash": frozen.config_hash,
        }
        DocumentExtraction.objects.filter(pk=document.id).update(
            routing_manifest={
                "schema_version": "extraction-routing-v2",
                "safety": safety,
                "safety_material_hash": services.canonical_hash(safety),
                "pages": [],
            }
        )
        with patch.object(tasks, "_profile", side_effect=AssertionError("latest profile selected")), patch.object(
            tasks, "_verify_profile"
        ):
            observed = tasks._frozen_document_safety_profile(
                document.id,
                engine=ExtractionEngine.NATIVE_PDF,
                expected_generation=claimed.lease_generation,
                expected_lease_owner=claimed.lease_owner,
                expected_lease_token=claimed.lease_token,
            )
        self.assertEqual(observed.id, frozen.id)

    def test_terminal_generic_attempt_requires_persisted_manifest_envelope(self):
        attempt = _generic_attempt()
        with self.assertRaises(IntegrityError), transaction.atomic():
            GenericExtractionAttempt.objects.filter(pk=attempt.id).update(
                state=ExtractionState.SUCCEEDED,
                result_checksum="a" * 64,
                terminal_event_key=f"evidence.other_ready:{attempt.id}:{'a' * 64}",
                terminal_state="ready",
                expected_evidence_count=None,
                expected_evidence_manifest_hash=None,
            )

    def test_generic_records_create_exact_authoritative_evidence_manifest(self):
        attempt = _generic_attempt()
        raw = b"<html><main><p>A</p><p>B</p></main></html>"
        raw_checksum = __import__("hashlib").sha256(raw).hexdigest()
        input_asset = EvidenceAsset.objects.create(
            source_item=attempt.source_item,
            origin_run_source_item=attempt.run_source_item,
            derivation_type=EvidenceDerivationType.RAW,
            raw_input_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
            kind=EvidenceKind.ATTACHMENT,
            locator={},
            object_key="evidence/raw/input.html",
            object_version="frozen-v1",
            mime_type="text/html",
            byte_size=len(raw),
            checksum=raw_checksum,
            structured_data={"source_url": "https://example.com/input.html"},
            evidence_content_hash=raw_checksum,
            review_subject_hash=SHA,
        )
        attempt.input_asset = input_asset
        attempt.save(update_fields=("input_asset", "updated_at"))
        token = uuid.uuid4()
        claimed = services.begin_generic_extraction(
            attempt.id,
            source_event_id=uuid.uuid4(),
            delivery_count=1,
            lease_generation=1,
            lease_owner="worker",
            lease_token=token,
        )
        output = GenericExtractionOutput(
            engine=attempt.engine,
            extractor_version=attempt.extractor_version,
            validation_mode=attempt.validation_mode,
            records=[
                GenericEvidenceRecord(
                    kind="text",
                    locator_type="html_dom",
                    locator={
                        "locator_type": "html_dom",
                        "css_selector": "main > p:nth-of-type(1)",
                        "xpath": None,
                    },
                    text="A",
                ),
                GenericEvidenceRecord(
                    kind="text",
                    locator_type="html_dom",
                    locator={
                        "locator_type": "html_dom",
                        "css_selector": "main > p:nth-of-type(2)",
                        "xpath": None,
                    },
                    text="B",
                ),
            ],
            metadata={"record_count": 2},
        )

        def put_bytes(**kwargs):
            return tasks.ObjectInfo(
                key=kwargs["key"],
                version_id="result-v1",
                checksum_sha256=kwargs["checksum_sha256"],
                size=len(kwargs["data"]),
                content_type=kwargs["content_type"],
                etag="result-etag",
            )

        rights = {
            "rights_status": "allowed",
            "rights_basis_url": "https://example.com/rights",
            "attribution_text": None,
        }
        with patch.object(tasks, "_verify_profile"), patch.object(
            tasks, "_generic_extractor"
        ) as extractor_factory, patch.object(tasks, "_storage") as storage_factory, patch.object(
            tasks, "_rights", return_value=rights
        ):
            extractor_factory.return_value.extract.return_value = output
            storage_factory.return_value.get_bounded_bytes.return_value = raw
            storage_factory.return_value.put_bytes.side_effect = put_bytes
            tasks._run_generic_extraction(
                attempt.id,
                expected_generation=claimed.lease_generation,
                expected_lease_owner=claimed.lease_owner,
                expected_lease_token=claimed.lease_token,
            )

        attempt.refresh_from_db()
        evidence = list(attempt.derived_evidence_assets.order_by("id"))
        self.assertEqual(len(evidence), 2)
        self.assertNotEqual(evidence[0].locator, evidence[1].locator)
        self.assertEqual(attempt.expected_evidence_count, 2)
        self.assertEqual(
            attempt.expected_evidence_manifest_hash,
            services.generic_evidence_manifest_hash(evidence),
        )
        ready = OutboxMessage.objects.get(
            topic="evidence.other_ready", aggregate_id=attempt.id
        )
        self.assertNotIn("evidence_asset_ids", ready.payload)
        self.assertEqual(ready.payload["evidence_count"], 2)
        self.assertEqual(
            ready.payload["evidence_manifest_hash"],
            attempt.expected_evidence_manifest_hash,
        )

        evidence[1].structured_data = {"records": [{"text": "tampered"}]}
        evidence[1].save(update_fields=("structured_data", "updated_at"))
        tampered = dict(ready.payload)
        route = route_for("evidence.other_ready", 1)
        args = outbox.routed_event_arguments(
            event_type="evidence.other_ready",
            event_version=1,
            argument_keys=route.argument_keys,
            payload=tampered,
        )
        with self.assertRaises(PermanentEventError):
            tasks.consume_other_ready.run(*args)

    def test_finalizer_fail_closes_terminal_attempt_when_evidence_material_changed(self):
        attempt = _generic_attempt()
        run = attempt.run_source_item.run
        step = RunStep.objects.create(
            run=run,
            name="extract",
            attempt_no=1,
            state="queued",
            input_count=1,
            fanout_completed_at=timezone.now(),
        )
        checksum = "a" * 64
        evidence = EvidenceAsset.objects.create(
            source_item=attempt.source_item,
            origin_run_source_item=attempt.run_source_item,
            derivation_type=EvidenceDerivationType.OTHER,
            generic_extraction_attempt=attempt,
            kind=EvidenceKind.TEXT,
            locator_type=LocatorType.HTML_DOM,
            locator={
                "locator_type": LocatorType.HTML_DOM,
                "css_selector": "main > p",
                "xpath": None,
            },
            extracted_text="original",
            structured_data={"record_index": 0},
            extraction_method=attempt.engine,
            extractor_version=attempt.extractor_version,
            extraction_config_hash=attempt.config_hash,
            validation_mode=attempt.validation_mode,
            extraction_result_checksum=checksum,
            rights_status="allowed",
            rights_basis_url="https://example.com/rights",
            review_state="passed",
            evidence_content_hash=services.evidence_content_hash(
                text="original", structured_data={"record_index": 0}, checksum=None
            ),
            review_subject_hash=SHA,
        )
        evidence.review_subject_hash = services.calculate_review_subject_hash(evidence)
        evidence.publishable = services.calculate_publishable(evidence)
        evidence.save(update_fields=("review_subject_hash", "publishable", "updated_at"))
        attempt.state = ExtractionState.SUCCEEDED
        attempt.result_checksum = checksum
        attempt.evidence_asset = evidence
        attempt.expected_evidence_count = 1
        attempt.expected_evidence_manifest_hash = services.generic_evidence_manifest_hash([evidence])
        attempt.terminal_event_key = f"evidence.other_ready:{attempt.id}:{checksum}"
        attempt.terminal_state = "ready"
        attempt.save()

        evidence.extracted_text = "changed without refreshing hashes"
        evidence.save(update_fields=("extracted_text", "updated_at"))
        result = tasks.finalize_run_evidence.run(str(run.id))

        run.refresh_from_db()
        step.refresh_from_db()
        self.assertEqual(result["state"], RunState.FAILED)
        self.assertEqual(run.state, RunState.FAILED)
        self.assertEqual(run.recovery_state, "manual_required")
        self.assertEqual(step.error_code, "generic_evidence_manifest_invalid")

    def test_finalizer_manifest_failure_closes_active_siblings_children_and_ledgers(self):
        attempt = _generic_attempt()
        run = attempt.run_source_item.run
        step = RunStep.objects.create(
            run=run,
            name="extract",
            attempt_no=1,
            state="queued",
            input_count=3,
            fanout_completed_at=timezone.now(),
        )
        evidence = self._terminal_generic_evidence(attempt)
        evidence.extracted_text = "tampered"
        evidence.save(update_fields=("extracted_text", "updated_at"))

        sibling = _generic_for_lineage(attempt.run_source_item, attempt.source_item)
        sibling_event = uuid.uuid4()
        sibling_token = uuid.uuid4()
        GenericExtractionAttempt.objects.filter(pk=sibling.id).update(
            state=ExtractionState.RUNNING,
            source_event_id=sibling_event,
            lease_generation=3,
            delivery_count=3,
            lease_owner="worker-b",
            lease_token=sibling_token,
            next_retry_at=timezone.now(),
        )
        document = DocumentExtraction.objects.create(
            run_source_item=attempt.run_source_item,
            source_item=attempt.source_item,
            input_object_key="evidence/raw/sibling.pdf",
            input_object_version="v1",
            input_kind="pdf",
            input_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
            input_mime_type="application/pdf",
            input_checksum=SHA,
            input_page_count=1,
            expected_page_indices=[0],
            state=ExtractionState.QUEUED,
        )
        child = _document_child(document)
        child_event = uuid.uuid4()
        ExtractionRun.objects.filter(pk=child.id).update(
            state=ExtractionState.RUNNING,
            source_event_id=child_event,
            lease_generation=2,
            delivery_count=2,
            lease_owner="child-worker",
            lease_token=uuid.uuid4(),
            next_retry_at=timezone.now(),
        )
        reservation = ExtractionObjectWriteReservation.objects.create(
            aggregate_kind="generic",
            aggregate_id=sibling.id,
            source_event_id=sibling_event,
            lease_generation=3,
            lease_identity_hash="b" * 64,
            purpose=ExtractionObjectWritePurpose.RESULT,
            object_key="evidence/results/sibling.json",
            state=ExtractionObjectWriteState.UPLOADED,
            object_version="version-1",
            checksum="c" * 64,
            byte_size=7,
            content_type="application/json",
            uploaded_at=timezone.now(),
        )

        result = tasks.finalize_run_evidence.run(str(run.id))

        sibling.refresh_from_db()
        document.refresh_from_db()
        child.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(result["state"], RunState.FAILED)
        for aggregate in (sibling, document, child):
            self.assertEqual(aggregate.state, ExtractionState.FAILED)
            self.assertIsNone(aggregate.next_retry_at)
            self.assertEqual(aggregate.lease_owner, "")
            self.assertIsNone(aggregate.lease_token)
            self.assertEqual(aggregate.terminal_state, "failed")
        self.assertEqual(reservation.state, ExtractionObjectWriteState.ORPHANED)

    def test_generic_validator_rejects_deterministic_low_confidence_attempt(self):
        attempt = _generic_attempt()
        # The direct update deliberately bypasses model clean to reproduce a
        # historical invalid row while respecting terminal identity immutability.
        evidence = self._terminal_generic_evidence(
            attempt,
            state=ExtractionState.LOW_CONFIDENCE,
        )
        EvidenceAsset.objects.filter(pk=evidence.id).update(publishable=False)
        evidence = EvidenceAsset.objects.select_related(
            "generic_extraction_attempt"
        ).get(pk=evidence.id)

        with self.assertRaisesRegex(
            ValidationError,
            "Only calibrated generic extraction can be low-confidence",
        ):
            services.validate_generic_evidence_set(attempt, [evidence])

    def test_terminal_generic_manifest_fields_are_database_immutable(self):
        attempt = _generic_attempt()
        self._terminal_generic_evidence(attempt)
        with self.assertRaises(DatabaseError), transaction.atomic():
            GenericExtractionAttempt.objects.filter(pk=attempt.id).update(
                expected_evidence_count=2,
                expected_evidence_manifest_hash="d" * 64,
            )

    def test_0006_postgresql_trigger_uses_generic_specific_function(self):
        migration = importlib.import_module(
            "apps.evidence.migrations.0006_generic_evidence_manifest"
        )
        statements = []
        editor = SimpleNamespace(
            connection=SimpleNamespace(vendor="postgresql"),
            execute=statements.append,
        )
        migration.create_generic_manifest_terminal_trigger(None, editor)
        sql = "\n".join(statements)
        self.assertIn("evidence_reject_generic_terminal_change", sql)
        self.assertIn("expected_evidence_count", sql)
        self.assertIn("expected_evidence_manifest_hash", sql)
        self.assertNotIn(
            "CREATE OR REPLACE FUNCTION evidence_reject_terminal_identity_change",
            sql,
        )

    def test_0006_reverse_restores_shared_terminal_identity_trigger(self):
        attempt = _generic_attempt()
        self._terminal_generic_evidence(attempt)
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([("evidence", "0005_extraction_generation_fencing")])
            with self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE evidence_genericextractionattempt SET state = %s WHERE id = %s",
                        ["failed", attempt.id.hex],
                    )
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(latest)

    def test_0006_backfills_exact_single_and_fail_closes_unproven_run(self):
        valid = _generic_attempt()
        checksum = "a" * 64
        evidence = EvidenceAsset.objects.create(
            source_item=valid.source_item,
            origin_run_source_item=valid.run_source_item,
            derivation_type=EvidenceDerivationType.OTHER,
            generic_extraction_attempt=valid,
            kind=EvidenceKind.TEXT,
            locator_type=LocatorType.HTML_DOM,
            locator={
                "locator_type": LocatorType.HTML_DOM,
                "css_selector": "main > p",
                "xpath": None,
            },
            extraction_method=valid.engine,
            extractor_version=valid.extractor_version,
            extraction_config_hash=valid.config_hash,
            validation_mode=valid.validation_mode,
            extraction_result_checksum=checksum,
            evidence_content_hash=services.evidence_content_hash(
                text=None, structured_data=None, checksum=None
            ),
            review_subject_hash="c" * 64,
        )
        valid.state = ExtractionState.SUCCEEDED
        valid.result_checksum = checksum
        valid.evidence_asset = evidence
        valid.terminal_event_key = f"evidence.other_ready:{valid.id}:{checksum}"
        valid.terminal_state = "ready"
        valid.expected_evidence_count = 1
        valid.expected_evidence_manifest_hash = SHA
        valid.save()
        evidence.review_subject_hash = services.calculate_review_subject_hash(evidence)
        evidence.save(update_fields=("review_subject_hash", "updated_at"))

        invalid_locator = _generic_for_lineage(valid.run_source_item, valid.source_item)
        invalid_checksum = "e" * 64
        invalid_evidence = EvidenceAsset.objects.create(
            source_item=invalid_locator.source_item,
            origin_run_source_item=invalid_locator.run_source_item,
            derivation_type=EvidenceDerivationType.OTHER,
            generic_extraction_attempt=invalid_locator,
            kind=EvidenceKind.TEXT,
            locator_type=LocatorType.HTML_DOM,
            locator={
                "locator_type": LocatorType.HTML_DOM,
                "css_selector": "main",
                "xpath": "/html/body/main",
            },
            extraction_method=invalid_locator.engine,
            extractor_version=invalid_locator.extractor_version,
            extraction_config_hash=invalid_locator.config_hash,
            validation_mode=invalid_locator.validation_mode,
            extraction_result_checksum=invalid_checksum,
            evidence_content_hash=services.evidence_content_hash(
                text=None, structured_data=None, checksum=None
            ),
            review_subject_hash=SHA,
        )
        invalid_locator.result_checksum = invalid_checksum
        invalid_evidence.review_subject_hash = services.calculate_review_subject_hash(
            invalid_evidence
        )
        invalid_evidence.save(update_fields=("review_subject_hash", "updated_at"))
        GenericExtractionAttempt.objects.filter(pk=invalid_locator.id).update(
            state=ExtractionState.SUCCEEDED,
            result_checksum=invalid_checksum,
            evidence_asset=invalid_evidence,
            expected_evidence_count=1,
            expected_evidence_manifest_hash=SHA,
            terminal_event_key=(
                f"evidence.other_ready:{invalid_locator.id}:{invalid_checksum}"
            ),
            terminal_state="ready",
        )

        unproven = _generic_attempt()
        unproven.state = ExtractionState.SUCCEEDED
        unproven.result_checksum = "d" * 64
        unproven.terminal_event_key = (
            f"evidence.other_ready:{unproven.id}:{unproven.result_checksum}"
        )
        unproven.terminal_state = "ready"
        unproven.expected_evidence_count = 1
        unproven.expected_evidence_manifest_hash = SHA
        unproven.save()
        step = RunStep.objects.create(
            run=unproven.run_source_item.run,
            name="extract",
            attempt_no=1,
            state="queued",
        )
        active = _generic_for_lineage(
            unproven.run_source_item,
            unproven.source_item,
        )
        active_event = uuid.uuid4()
        GenericExtractionAttempt.objects.filter(pk=active.id).update(
            state=ExtractionState.RUNNING,
            source_event_id=active_event,
            lease_generation=4,
            delivery_count=4,
            lease_owner="migration-worker",
            lease_token=uuid.uuid4(),
            next_retry_at=timezone.now(),
        )
        uploaded = ExtractionObjectWriteReservation.objects.create(
            aggregate_kind="generic",
            aggregate_id=active.id,
            source_event_id=active_event,
            lease_generation=4,
            lease_identity_hash="a" * 64,
            purpose=ExtractionObjectWritePurpose.RESULT,
            object_key="evidence/results/migration-uploaded.json",
            state=ExtractionObjectWriteState.UPLOADED,
            object_version="version-1",
            checksum="b" * 64,
            byte_size=3,
            content_type="application/json",
            uploaded_at=timezone.now(),
        )
        bound = ExtractionObjectWriteReservation.objects.create(
            aggregate_kind="generic",
            aggregate_id=active.id,
            source_event_id=active_event,
            lease_generation=4,
            lease_identity_hash="a" * 64,
            purpose=ExtractionObjectWritePurpose.REASON,
            object_key="evidence/results/migration-bound.json",
            state=ExtractionObjectWriteState.BOUND,
            object_version="version-2",
            checksum="c" * 64,
            byte_size=4,
            content_type="application/json",
            uploaded_at=timezone.now(),
            bound_at=timezone.now(),
        )

        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([("evidence", "0005_extraction_generation_fencing")])
            executor = MigrationExecutor(connection)
            executor.migrate([("evidence", "0006_generic_evidence_manifest")])
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(latest)

        valid.refresh_from_db()
        invalid_locator.refresh_from_db()
        invalid_evidence.refresh_from_db()
        unproven.refresh_from_db()
        active.refresh_from_db()
        uploaded.refresh_from_db()
        bound.refresh_from_db()
        step.refresh_from_db()
        self.assertEqual(
            valid.expected_evidence_count,
            1,
            valid.error_detail_redacted,
        )
        self.assertEqual(
            valid.expected_evidence_manifest_hash,
            services.generic_evidence_manifest_hash([evidence]),
        )
        self.assertEqual(unproven.expected_evidence_count, 0)
        self.assertEqual(
            unproven.expected_evidence_manifest_hash,
            services.canonical_hash([]),
        )
        self.assertEqual(unproven.state, ExtractionState.SUCCEEDED)
        self.assertEqual(invalid_locator.expected_evidence_count, 0)
        self.assertTrue(invalid_evidence.manual_review_required)
        self.assertFalse(invalid_evidence.publishable)
        self.assertEqual(active.state, ExtractionState.FAILED)
        self.assertIsNone(active.next_retry_at)
        self.assertEqual(uploaded.state, ExtractionObjectWriteState.ORPHANED)
        self.assertEqual(uploaded.object_version, "version-1")
        self.assertEqual(bound.state, ExtractionObjectWriteState.BOUND)
        self.assertEqual(
            CollectionRun.objects.get(pk=unproven.run_source_item.run_id).state,
            RunState.FAILED,
        )
        self.assertEqual(step.state, "failed")
        self.assertEqual(step.recovery_state, "manual_required")
