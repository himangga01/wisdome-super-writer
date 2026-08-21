from __future__ import annotations

from types import SimpleNamespace
from unittest import mock
import uuid

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from adapters.storage.base import ObjectInfo
from adapters.storage.s3 import S3ObjectStorage
from apps.audit import retention
from apps.audit.models import AuditEvent, RetentionBatch, RetentionBatchItem
from apps.audit.services import AuditContext, record_audit_event
from apps.evidence.models import EvidenceAsset
from apps.collection.models import SourceItem
from apps.topics.models import SourceDefinition


class RetentionModelContractTests(SimpleTestCase):
    def test_batch_and_item_freeze_request_object_and_tombstone_material(self):
        batch_fields = {field.name for field in RetentionBatch._meta.fields}
        self.assertTrue(
            {
                "scope",
                "request_hash",
                "preview_reason",
                "expected_item_count",
                "expected_byte_count",
                "processed_count",
                "skipped_hold_count",
                "failed_count",
            }.issubset(batch_fields)
        )
        item_fields = {field.name for field in RetentionBatchItem._meta.fields}
        self.assertTrue(
            {
                "policy_code",
                "object_version",
                "object_checksum",
                "byte_size",
                "candidate_hash",
                "dependency_manifest",
                "lease_generation",
                "tombstone_at",
                "result_hash",
                "error_code",
            }.issubset(item_fields)
        )


class RetentionPolicyAndHashTests(SimpleTestCase):
    def test_policy_declares_every_supported_category_handler(self):
        policy = retention.load_retention_policy()
        self.assertEqual(
            set(policy["categories"]),
            {
                "raw_source",
                "raw_evidence",
                "unpublished_revision",
                "published_evidence_snapshot",
                "published_visualization_snapshot",
                "audit_event",
                "public_delivery_asset",
                "wordpress_media",
            },
        )

    def test_candidate_hash_is_order_stable_and_binds_dependencies(self):
        base = {
            "policy_code": "raw_evidence",
            "entity_type": "evidence_asset",
            "entity_id": "00000000-0000-0000-0000-000000000001",
            "object_key": "evidence/raw.bin",
            "object_version": "version-7",
            "object_checksum": "a" * 64,
            "byte_size": 10,
            "dependency_manifest": [
                {"kind": "claim", "id": "2"},
                {"kind": "hold", "id": "1"},
            ],
        }
        first = retention.retention_candidate_hash(**base)
        second = retention.retention_candidate_hash(
            **{**base, "dependency_manifest": list(reversed(base["dependency_manifest"]))}
        )
        changed = retention.retention_candidate_hash(
            **{
                **base,
                "dependency_manifest": base["dependency_manifest"]
                + [{"kind": "correction", "id": "3"}],
            }
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)


class RetentionObjectDeletionTests(SimpleTestCase):
    def test_s3_delete_version_requires_and_forwards_exact_version(self):
        client = mock.Mock()
        storage = S3ObjectStorage(bucket="bucket", client=client)

        storage.delete_version(key="evidence/raw.bin", version_id="version-7")

        client.delete_object.assert_called_once_with(
            Bucket="bucket",
            Key="evidence/raw.bin",
            VersionId="version-7",
        )
        with self.assertRaises(ValueError):
            storage.delete_version(key="evidence/raw.bin", version_id="")

    def test_object_precondition_rejects_version_or_checksum_drift(self):
        item = SimpleNamespace(
            object_key="evidence/raw.bin",
            object_version="version-7",
            object_checksum="a" * 64,
            byte_size=10,
        )
        storage = mock.Mock()
        storage.head.return_value = ObjectInfo(
            key=item.object_key,
            version_id="version-8",
            checksum_sha256="b" * 64,
            size=10,
            content_type="application/octet-stream",
        )

        with self.assertRaisesRegex(ValueError, "stale_retention_candidate"):
            retention.verify_retention_object_precondition(item=item, storage=storage)

    def test_delete_response_loss_converges_when_exact_version_is_absent(self):
        item = SimpleNamespace(
            object_key="evidence/raw.bin",
            object_version="version-7",
            object_checksum="a" * 64,
            byte_size=10,
        )
        storage = mock.Mock()
        storage.head.return_value = ObjectInfo(
            key=item.object_key,
            version_id=item.object_version,
            checksum_sha256=item.object_checksum,
            size=item.byte_size,
            content_type="application/octet-stream",
        )
        storage.delete_version.side_effect = TimeoutError("response lost")
        storage.version_exists.side_effect = (True, False)

        result_hash = retention.delete_exact_retention_object(
            item=item,
            storage=storage,
        )

        self.assertEqual(len(result_hash), 64)
        storage.delete_version.assert_called_once_with(
            key=item.object_key,
            version_id=item.object_version,
        )

    def test_delete_does_not_claim_success_while_exact_version_remains(self):
        item = SimpleNamespace(
            object_key="evidence/raw.bin",
            object_version="version-7",
            object_checksum="a" * 64,
            byte_size=10,
        )
        storage = mock.Mock()
        storage.head.return_value = ObjectInfo(
            key=item.object_key,
            version_id=item.object_version,
            checksum_sha256=item.object_checksum,
            size=item.byte_size,
            content_type="application/octet-stream",
        )
        storage.version_exists.return_value = True

        with self.assertRaisesRegex(ValueError, "retention_object_delete_unconfirmed"):
            retention.delete_exact_retention_object(item=item, storage=storage)


class RetentionDependencyTests(SimpleTestCase):
    def test_blocking_dependencies_fail_closed(self):
        self.assertEqual(
            retention.blocking_dependency_codes(
                [
                    {"kind": "published_reference", "id": "2", "blocking": True},
                    {"kind": "informational", "id": "1", "blocking": False},
                    {"kind": "legal_hold", "id": "3", "blocking": True},
                ]
            ),
            ("legal_hold", "published_reference"),
        )


class RetentionCategoryHandlerTests(SimpleTestCase):
    def test_every_policy_category_has_one_explicit_handler(self):
        self.assertEqual(
            set(retention.RETENTION_CATEGORY_HANDLERS),
            set(retention.SUPPORTED_RETENTION_CATEGORIES),
        )
        self.assertEqual(
            retention.RETENTION_CATEGORY_HANDLERS["unpublished_revision"],
            "manual_archive_required",
        )
        self.assertEqual(
            retention.RETENTION_CATEGORY_HANDLERS["audit_event"],
            "manual_archive_required",
        )

    def test_published_snapshot_uses_exact_object_delete_without_mutating_snapshot(self):
        item = SimpleNamespace(
            policy_code="published_evidence_snapshot",
            entity_type="published_evidence_snapshot",
            entity_id="00000000-0000-0000-0000-000000000001",
            object_key="published/evidence.bin",
            object_version="version-1",
            object_checksum="a" * 64,
            byte_size=10,
            candidate_hash="b" * 64,
        )
        storage = mock.Mock()
        storage.head.return_value = ObjectInfo(
            key=item.object_key,
            version_id=item.object_version,
            checksum_sha256=item.object_checksum,
            size=item.byte_size,
            content_type="application/octet-stream",
        )
        storage.version_exists.side_effect = (True, False)

        outcome = retention.execute_retention_candidate(item=item, storage=storage)

        self.assertEqual(outcome.state, "purged")
        self.assertEqual(len(outcome.result_hash), 64)
        storage.delete_version.assert_called_once()

    def test_public_delivery_uses_separate_cleanup_lease(self):
        item = SimpleNamespace(
            policy_code="public_delivery_asset",
            entity_type="public_delivery_asset",
            entity_id="00000000-0000-0000-0000-000000000001",
        )
        operation = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000010",
            state="queued",
        )
        with mock.patch(
            "apps.publishing.services.schedule_orphan_media_cleanup_locked",
            return_value=operation,
        ) as schedule:
            outcome = retention.execute_retention_candidate(
                item=item,
                storage=mock.Mock(),
            )

        self.assertEqual(outcome.state, "deletion_pending")
        self.assertEqual(outcome.cleanup_operation_id, str(operation.id))
        schedule.assert_called_once_with(public_delivery_asset_id=item.entity_id)

    def test_wordpress_media_uses_separate_cleanup_lease(self):
        item = SimpleNamespace(
            policy_code="wordpress_media",
            entity_type="remote_media",
            entity_id="00000000-0000-0000-0000-000000000002",
        )
        operation = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000011",
            state="queued",
        )
        with mock.patch(
            "apps.publishing.services.schedule_orphan_media_cleanup_locked",
            return_value=operation,
        ) as schedule:
            outcome = retention.execute_retention_candidate(
                item=item,
                storage=mock.Mock(),
            )

        self.assertEqual(outcome.state, "deletion_pending")
        schedule.assert_called_once_with(remote_media_id=item.entity_id)

    def test_manual_archive_categories_never_claim_physical_deletion(self):
        for policy_code in ("unpublished_revision", "audit_event"):
            with self.subTest(policy_code=policy_code):
                outcome = retention.execute_retention_candidate(
                    item=SimpleNamespace(policy_code=policy_code),
                    storage=mock.Mock(),
                )
                self.assertEqual(outcome.state, "held")
                self.assertEqual(outcome.reason_code, "manual_archive_required")


class RetentionServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="retention@example.com",
            password="test-password",
            is_staff=True,
        )

    def _asset(self, *, suffix: str = "1") -> EvidenceAsset:
        row = EvidenceAsset.objects.create(
            derivation_type="visualization_derived",
            visualization_render_id="00000000-0000-0000-0000-000000000001",
            kind="image",
            locator={},
            object_key=f"evidence/raw-{suffix}.bin",
            object_version=f"version-{suffix}",
            mime_type="application/octet-stream",
            byte_size=10,
            checksum=suffix * 64,
            extracted_text="raw evidence text",
            structured_data={"raw": True},
            evidence_content_hash="e" * 64,
            review_subject_hash="f" * 64,
        )
        EvidenceAsset.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=91),
        )
        row.refresh_from_db()
        return row

    def _source_item(self) -> SourceItem:
        source = SourceDefinition.objects.create(
            topic_code="housing_subscription",
            key="retention-source",
            display_name="Retention source",
            publisher="Publisher",
            owner_name="Owner",
            editorial_control_name="Editor",
            base_url="https://example.com/",
            authority_tier="primary_official",
            access_method="public_file",
            independence_group="official",
            adapter_key="test",
        )
        return SourceItem.objects.create(
            source=source,
            external_id="retention-1",
            canonical_url="https://example.com/source/1",
            title="Source title",
            publisher="Publisher",
            first_collected_at=timezone.now() - timezone.timedelta(days=91),
            content_hash="c" * 64,
            source_version_hash="d" * 64,
            source_version_schema="source-item-version-v1",
            body_text="sensitive raw source body",
            metadata={"raw": "metadata"},
            attachments=[{"url": "https://example.com/raw.pdf"}],
        )

    @staticmethod
    def _storage_for(asset: EvidenceAsset):
        storage = mock.Mock()
        storage.head.return_value = ObjectInfo(
            key=asset.object_key,
            version_id=asset.object_version,
            checksum_sha256=asset.checksum,
            size=asset.byte_size,
            content_type=asset.mime_type,
        )
        storage.version_exists.side_effect = (True, False)
        return storage

    def test_preview_freezes_exact_candidate_and_replay_hash(self):
        asset = self._asset()
        cutoff = timezone.now() - timezone.timedelta(days=90)

        batch = retention.create_retention_preview(
            scope="raw_evidence",
            cutoff_at=cutoff,
            request_key="retention-preview-1",
            reason="오래된 원문 증거 정리",
            user=self.user,
        )
        replay = retention.create_retention_preview(
            scope="raw_evidence",
            cutoff_at=cutoff,
            request_key="retention-preview-1",
            reason="오래된 원문 증거 정리",
            user=self.user,
        )

        self.assertEqual(replay.id, batch.id)
        item = batch.items.get(entity_id=asset.id)
        self.assertEqual(item.policy_code, "raw_evidence")
        self.assertEqual(item.object_version, asset.object_version)
        self.assertEqual(item.object_checksum, asset.checksum)
        self.assertEqual(item.precondition_hash, item.candidate_hash)
        self.assertEqual(batch.expected_item_count, 1)
        self.assertEqual(batch.expected_byte_count, asset.byte_size)
        with self.assertRaisesRegex(ValueError, "retention_request_key_conflict"):
            retention.create_retention_preview(
                scope="raw_evidence",
                cutoff_at=cutoff,
                request_key="retention-preview-1",
                reason="다른 사유",
                user=self.user,
            )

    def test_preview_records_exact_admin_audit_when_context_is_supplied(self):
        self._asset(suffix="audit")
        request = RequestFactory().post("/api/v1/retention/previews")
        request.user = self.user
        request.correlation_id = uuid.uuid4()
        audit_context = AuditContext.for_admin(
            request=request,
            reason_code="보존 삭제 후보 검토",
            request_key="retention-preview-audit",
        )

        batch = retention.create_retention_preview(
            scope="raw_evidence",
            cutoff_at=timezone.now() - timezone.timedelta(days=90),
            request_key="retention-preview-audit",
            reason="보존 삭제 후보 검토",
            user=self.user,
            audit_context=audit_context,
        )
        replay = retention.create_retention_preview(
            scope="raw_evidence",
            cutoff_at=batch.cutoff_at,
            request_key="retention-preview-audit",
            reason="보존 삭제 후보 검토",
            user=self.user,
            audit_context=audit_context,
        )

        self.assertEqual(replay.id, batch.id)
        event = AuditEvent.objects.get(
            action="retention_batch.previewed",
            entity_id=batch.id,
        )
        self.assertEqual(event.metadata_redacted["request_hash"], batch.request_hash)

    def test_approval_revalidates_and_execution_tombstones_exact_object(self):
        asset = self._asset()
        batch = retention.create_retention_preview(
            scope="raw_evidence",
            cutoff_at=timezone.now() - timezone.timedelta(days=90),
            request_key="retention-preview-2",
            reason="오래된 원문 증거 정리",
            user=self.user,
        )
        storage = self._storage_for(asset)

        retention.approve_retention_batch(
            batch.id,
            expected_version=batch.row_version,
            expected_preview_hash=batch.preview_manifest_hash,
            authorized_by=self.user,
            authorization_request_key="retention-authorize-2",
            authorization_reason="검증 후 삭제 승인",
            storage=storage,
        )
        result = retention.execute_retention_batch(batch.id, storage=storage)

        item = result.items.get(entity_id=asset.id)
        asset.refresh_from_db()
        self.assertEqual(item.state, RetentionBatchItem.State.PURGED)
        self.assertIsNotNone(item.tombstone_at)
        self.assertEqual(len(item.result_hash), 64)
        self.assertIsNone(asset.object_key)
        self.assertIsNone(asset.object_version)
        self.assertIsNone(asset.extracted_text)
        self.assertIsNone(asset.structured_data)

    def test_failed_batch_resume_revalidates_only_failed_items(self):
        asset = self._asset(suffix="resume")
        batch = retention.create_retention_preview(
            scope="raw_evidence",
            cutoff_at=timezone.now() - timezone.timedelta(days=90),
            request_key="retention-preview-resume",
            reason="실패 재개 대상 미리보기",
            user=self.user,
        )
        item = batch.items.get(entity_id=asset.id)
        item.state = RetentionBatchItem.State.FAILED
        item.error_code = "TimeoutError"
        item.error_detail_redacted = "Retention candidate could not be purged."
        item.processed_at = timezone.now()
        item.save(
            update_fields=(
                "state",
                "error_code",
                "error_detail_redacted",
                "processed_at",
            )
        )
        RetentionBatch.objects.filter(pk=batch.id).update(
            state=RetentionBatch.State.FAILED,
            row_version=2,
            failed_count=1,
        )
        batch.refresh_from_db()
        proof_id = uuid.uuid4()
        request = RequestFactory().post("/api/v1/retention/resume")
        request.user = self.user
        request.correlation_id = uuid.uuid4()
        audit_context = AuditContext.for_admin(
            request=request,
            reason_code="실패 원인 해소 후 재개",
            request_key="retention-resume-1",
        )

        resumed = retention.resume_failed_retention_batch(
            batch.id,
            expected_version=2,
            expected_preview_hash=batch.preview_manifest_hash,
            authorized_by=self.user,
            authorization_request_key="retention-resume-1",
            authorization_reason="실패 원인 해소 후 재개",
            reauth_proof_id=proof_id,
            storage=self._storage_for(asset),
            audit_context=audit_context,
        )

        item.refresh_from_db()
        self.assertEqual(resumed.state, RetentionBatch.State.APPROVED)
        self.assertEqual(resumed.row_version, 3)
        self.assertEqual(item.state, RetentionBatchItem.State.CANDIDATE)
        self.assertEqual(item.lease_generation, 2)
        self.assertEqual(item.error_code, "")
        self.assertIsNone(item.processed_at)
        event = AuditEvent.objects.get(
            action="retention_batch.authorized",
            entity_id=batch.id,
        )
        self.assertEqual(event.metadata_redacted["result"], "resumed")

    def test_new_hold_after_preview_blocks_external_delete(self):
        asset = self._asset(suffix="2")
        batch = retention.create_retention_preview(
            scope="raw_evidence",
            cutoff_at=timezone.now() - timezone.timedelta(days=90),
            request_key="retention-preview-3",
            reason="오래된 원문 증거 정리",
            user=self.user,
        )
        storage = self._storage_for(asset)
        retention.approve_retention_batch(
            batch.id,
            expected_version=batch.row_version,
            expected_preview_hash=batch.preview_manifest_hash,
            authorized_by=self.user,
            authorization_request_key="retention-authorize-3",
            authorization_reason="검증 후 삭제 승인",
            storage=storage,
        )
        from apps.audit.models import RetentionHold

        RetentionHold.objects.create(
            scope_type="evidence_asset",
            scope_id=asset.id,
            reason="분쟁 보존",
            created_by=self.user,
        )

        result = retention.execute_retention_batch(batch.id, storage=storage)

        item = result.items.get(entity_id=asset.id)
        self.assertEqual(item.state, RetentionBatchItem.State.HELD)
        self.assertEqual(item.reason_code, "dependency_changed_after_preview")
        storage.delete_version.assert_not_called()

    def test_candidate_identity_is_immutable_in_orm_and_database(self):
        asset = self._asset(suffix="3")
        batch = retention.create_retention_preview(
            scope="raw_evidence",
            cutoff_at=timezone.now() - timezone.timedelta(days=90),
            request_key="retention-preview-4",
            reason="오래된 원문 증거 정리",
            user=self.user,
        )
        item = batch.items.get(entity_id=asset.id)
        with self.assertRaises(TypeError):
            RetentionBatchItem.objects.filter(pk=item.pk).update(
                candidate_hash="0" * 64
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE audit_retentionbatchitem SET candidate_hash = %s WHERE id = %s",
                    ["0" * 64, item.id.hex],
                )

    def test_raw_source_tombstone_is_one_way_and_preserves_identity(self):
        source_item = self._source_item()
        batch = retention.create_retention_preview(
            scope="raw_evidence",
            cutoff_at=timezone.now() - timezone.timedelta(days=90),
            request_key="retention-preview-source",
            reason="오래된 원문 본문 정리",
            user=self.user,
        )
        item = batch.items.get(entity_id=source_item.id)
        self.assertEqual(item.policy_code, "raw_source")
        storage = mock.Mock()
        retention.approve_retention_batch(
            batch.id,
            expected_version=batch.row_version,
            expected_preview_hash=batch.preview_manifest_hash,
            authorized_by=self.user,
            authorization_request_key="retention-authorize-source",
            authorization_reason="원문 tombstone 승인",
            storage=storage,
        )

        retention.execute_retention_batch(batch.id, storage=storage)

        item.refresh_from_db()
        source_item.refresh_from_db()
        self.assertEqual(
            item.state,
            RetentionBatchItem.State.PURGED,
            f"reason={item.reason_code}; error={item.error_code}",
        )
        self.assertEqual(source_item.body_text, "")
        self.assertEqual(source_item.metadata, {})
        self.assertEqual(source_item.attachments, [])
        self.assertIsNotNone(source_item.retention_tombstoned_at)
        self.assertEqual(source_item.content_hash, "c" * 64)
        with self.assertRaises(TypeError):
            SourceItem.objects.filter(pk=source_item.pk).update(title="changed")

    def test_media_cleanup_terminal_result_projects_item_and_batch(self):
        batch = RetentionBatch.objects.create(
            policy_version=1,
            policy_hash="a" * 64,
            scope="public_delivery_assets",
            request_hash="b" * 64,
            preview_reason="public delivery cleanup",
            cutoff_at=timezone.now(),
            state=RetentionBatch.State.RUNNING,
            request_key="retention-media-finalizer",
            preview_manifest_hash="c" * 64,
            counters={"candidate": 1},
            expected_item_count=1,
            requested_by=self.user,
        )
        operation_id = "00000000-0000-0000-0000-000000000099"
        item = RetentionBatchItem.objects.create(
            batch=batch,
            entity_type="public_delivery_asset",
            entity_id="00000000-0000-0000-0000-000000000001",
            policy_code="public_delivery_asset",
            candidate_hash="d" * 64,
            precondition_hash="d" * 64,
            cleanup_operation_id=operation_id,
            state=RetentionBatchItem.State.DELETION_PENDING,
        )

        retention.finalize_retention_media_cleanup(
            operation=SimpleNamespace(
                id=operation_id,
                state="succeeded",
                result_hash="e" * 64,
                error_code="",
            )
        )

        item.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(item.state, RetentionBatchItem.State.PURGED)
        self.assertIsNotNone(item.tombstone_at)
        self.assertEqual(batch.state, RetentionBatch.State.COMPLETED)

    def test_audit_expiry_preview_is_explicitly_manual_and_fail_closed(self):
        with transaction.atomic():
            event, _created = record_audit_event(
                context=AuditContext.for_system(
                    correlation_id=uuid.uuid4(),
                    operation_key="retention-audit-fixture",
                ),
                action="publication_intent.created",
                entity=self.user,
                identity_key="retention-audit-fixture",
                material_schema_version="retention-test-v1",
                after_material={"state": "created"},
                metadata={},
            )

        batch = retention.create_retention_preview(
            scope="audit_expiry",
            cutoff_at=timezone.now() + timezone.timedelta(minutes=1),
            request_key="retention-audit-preview",
            reason="감사 보존 기간 검토",
            user=self.user,
        )

        item = batch.items.get(entity_id=event.id)
        self.assertEqual(item.policy_code, "audit_event")
        self.assertEqual(item.state, RetentionBatchItem.State.HELD)
        self.assertEqual(item.reason_code, "blocking_dependency")
