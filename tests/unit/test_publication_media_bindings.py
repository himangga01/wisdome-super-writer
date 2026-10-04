import uuid
from types import SimpleNamespace
from unittest import TestCase

from django.db import IntegrityError, connection, transaction
from django.db.models import PROTECT
from django.test import TestCase as DjangoTestCase
from django.utils import timezone

from apps.publishing import models as publishing_models
from apps.publishing import services
from wisdome_writer.domain.errors import Conflict


class PublicationMediaBindingModelContractTests(TestCase):
    """Publication media must point at the immutable T022 asset lineage."""

    def test_binding_uses_exact_asset_cohort_and_snapshot_foreign_keys(self):
        binding = publishing_models.PublicationMedia
        field_names = {field.name for field in binding._meta.get_fields()}

        self.assertLessEqual(
            {
                "asset_cohort",
                "published_evidence_snapshot",
                "published_visualization_snapshot",
            },
            field_names,
        )
        self.assertNotIn("evidence_asset_id", field_names)

        for field_name in (
            "asset_cohort",
            "published_evidence_snapshot",
            "published_visualization_snapshot",
        ):
            field = binding._meta.get_field(field_name)
            self.assertIs(field.remote_field.on_delete, PROTECT)

        constraints = {constraint.name: constraint for constraint in binding._meta.constraints}
        self.assertEqual(
            tuple(constraints["uq_publication_evidence_snapshot"].fields),
            ("publication", "published_evidence_snapshot"),
        )
        self.assertEqual(
            tuple(constraints["uq_publication_visual_snapshot"].fields),
            ("publication", "published_visualization_snapshot"),
        )

    def test_content_addressed_delivery_rows_do_not_duplicate_source_identity(self):
        remote_fields = {
            field.name for field in publishing_models.RemoteMedia._meta.get_fields()
        }
        delivery_fields = {
            field.name for field in publishing_models.PublicDeliveryAsset._meta.get_fields()
        }

        self.assertNotIn("evidence_asset_id", remote_fields)
        self.assertNotIn("source_evidence_asset_id", delivery_fields)

    def test_mapping_and_binding_identity_reject_orm_mutation_and_delete(self):
        with self.assertRaises(TypeError):
            publishing_models.RemoteMedia.objects.none().update(
                asset_checksum="a" * 64
            )
        with self.assertRaises(TypeError):
            publishing_models.PublicDeliveryAsset.objects.none().update(
                presentation_hash="b" * 64
            )
        with self.assertRaises(TypeError):
            publishing_models.PublicationMedia.objects.none().update(
                block_id="changed"
            )
        with self.assertRaises(TypeError):
            publishing_models.PublicationMedia.objects.none().delete()


class PublicationMediaBindingServiceTests(DjangoTestCase):
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

    _evidence_revision = _AssetFixture._evidence_revision
    _intent_for_revision = _AssetFixture._intent_for_revision

    def _attempt(self):
        revision, _placement, _evidence, _source_item = self._evidence_revision()
        intent = self._intent_for_revision(revision)
        render = services._create_preview_render(
            intent,
            revision,
            self.fixture.target,
        )
        publication = publishing_models.Publication.objects.create(
            article_id=revision.article_id,
            target=self.fixture.target,
            origin_target_snapshot_id=self.fixture.target.current_snapshot_id,
            remote_lookup_key=f"t022-publication-{uuid.uuid4().hex}",
        )
        return SimpleNamespace(
            id=uuid.uuid4(),
            publication=publication,
            publication_id=publication.id,
            publication_intent=intent,
            publication_intent_id=intent.id,
            article_revision=revision,
            article_revision_id=revision.id,
            approval=SimpleNamespace(article_channel_render=render),
            resolved_action=publishing_models.PublicationAction.CREATE,
        )

    def _blogger_attempt(self):
        revision, _placement, _evidence, _source_item = self._evidence_revision()
        intent = self._intent_for_revision(revision)
        target = publishing_models.PublicationTarget.objects.create(
            channel="blogger",
            role="secondary_distribution",
            environment="test",
            display_name=f"T022 Blogger {uuid.uuid4().hex}",
            remote_blog_id="blog-22",
            base_url="https://blog.example.com/",
            connection_state="verified",
            current_config_hash="6" * 64,
            publisher_adapter_manifest_hash=services.ADAPTER_MANIFESTS["blogger"],
        )
        snapshot = publishing_models.PublicationTargetSnapshot.objects.create(
            target=target,
            version=1,
            channel=target.channel,
            role=target.role,
            environment=target.environment,
            base_url=target.base_url,
            credential_ref_identity_hash="4" * 64,
            connection_state="verified",
            preflight_state="passed",
            canary_state="passed",
            pilot_state="passed",
            publisher_contract_version="publisher-v1",
            publisher_adapter_manifest_hash=target.publisher_adapter_manifest_hash,
            config_hash=target.current_config_hash,
        )
        target.current_snapshot_id = snapshot.id
        target.save(update_fields=("current_snapshot_id", "current_config_hash"))
        render = services._create_preview_render(intent, revision, target)
        publication = publishing_models.Publication.objects.create(
            article_id=revision.article_id,
            target=target,
            origin_target_snapshot_id=snapshot.id,
            remote_lookup_key=f"t022-blogger-{uuid.uuid4().hex}",
        )
        return SimpleNamespace(
            id=uuid.uuid4(),
            publication=publication,
            publication_id=publication.id,
            publication_intent=intent,
            publication_intent_id=intent.id,
            article_revision=revision,
            article_revision_id=revision.id,
            approval=SimpleNamespace(article_channel_render=render),
            resolved_action=publishing_models.PublicationAction.CREATE,
        )

    def test_available_verified_wordpress_mapping_activates_a_new_binding(self):
        self._assert_available_verified_mapping("wordpress")

    def test_available_verified_blogger_mapping_activates_a_new_binding(self):
        self._assert_available_verified_mapping("blogger")

    def _assert_available_verified_mapping(self, channel):
        attempt = self._attempt() if channel == "wordpress" else self._blogger_attempt()
        binding = services.prepare_publication_media_bindings_locked(attempt=attempt)[0]
        mapping = binding.remote_media if channel == "wordpress" else binding.public_delivery_asset
        verified_at = timezone.now()
        mapping.state = "available"
        mapping.last_reconciled_at = verified_at
        if channel == "wordpress":
            mapping.remote_media_id = "verified-media"
            mapping.remote_source_url = "https://example.com/verified.png"
            mapping.save(
                update_fields=(
                    "state", "last_reconciled_at",
                    "remote_media_id", "remote_source_url",
                )
            )
        else:
            mapping.delivery_object_version = "verified-version"
            mapping.public_url = "https://example.com/verified.png"
            mapping.save(
                update_fields=(
                    "state", "last_reconciled_at",
                    "delivery_object_version", "public_url",
                )
            )
        self.assertEqual(
            services.ensure_publication_media_delivery_operations_locked(attempt=attempt), ()
        )
        binding.refresh_from_db()
        self.assertEqual(binding.binding_state, "active")
        self.assertEqual(binding.remote_verified_at, verified_at)
        services.require_publication_media_ready_locked(attempt=attempt)

    def test_uncertain_delete_records_its_outcome_without_invalid_publication_reconcile(self):
        from unittest.mock import patch

        from apps.audit.models import AuditEvent
        from apps.audit.services import AuditContext
        from wisdome_writer.infrastructure.models import OutboxConsumerReceipt

        attempt = self._attempt()
        binding = services.prepare_publication_media_bindings_locked(attempt=attempt)[0]
        binding.binding_state = "removed"
        binding.removed_at = timezone.now()
        binding.save(update_fields=("binding_state", "removed_at"))
        operation, _ = services.enqueue_media_delivery_operation_locked(
            remote_media=binding.remote_media,
            action=publishing_models.MediaDeliveryOperation.Action.DELETE,
        )
        token = uuid.uuid4()
        receipt = OutboxConsumerReceipt.objects.create(
            event=operation.source_event, consumer_name="media-delete-v2", state="processing",
            attempts=1, claimed_at=timezone.now(),
            claimed_until=timezone.now() + timezone.timedelta(minutes=1),
            lease_token=token, lease_generation=1,
        )
        context = AuditContext.for_worker(
            correlation_id=operation.source_event.correlation_id,
            event_key=str(operation.source_event_id),
            consumer_name=receipt.consumer_name, lease_token=token, lease_generation=1,
        )
        _, fence = services.begin_media_delivery_operation(
            operation.id, expected_generation=1, audit_context=context
        )
        with patch.object(services, "_kill_switch_enabled", return_value=False):
            services.authorize_media_delivery_external_write(fence, audit_context=context)
        services.persist_media_delivery_operation_result(
            fence,
            result={"status": "unknown_outcome", "error_code": "remote_media_delete_unproven"},
            audit_context=context,
        )
        operation.refresh_from_db()
        binding.remote_media.refresh_from_db()
        self.assertEqual(operation.state, "unknown_outcome")
        self.assertNotEqual(binding.remote_media.state, "deleted")
        self.assertEqual(
            publishing_models.MediaDeliveryOperation.objects.filter(
                remote_media=binding.remote_media
            ).count(), 1
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                action="media_delivery_operation.finished", entity_id=operation.id
            ).exists()
        )

    def test_prepare_replays_exact_wordpress_mapping_and_binding(self):
        attempt = self._attempt()

        first = services.prepare_publication_media_bindings_locked(attempt=attempt)
        second = services.prepare_publication_media_bindings_locked(attempt=attempt)

        self.assertEqual([row.id for row in first], [row.id for row in second])
        self.assertEqual(len(first), 1)
        binding = first[0]
        snapshot = attempt.article_revision.published_asset_cohort.evidence_snapshots.get()
        self.assertEqual(binding.asset_cohort_id, snapshot.cohort_id)
        self.assertEqual(binding.published_evidence_snapshot_id, snapshot.id)
        self.assertIsNone(binding.published_visualization_snapshot_id)
        self.assertEqual(binding.remote_media.target_id, self.fixture.target.id)
        self.assertEqual(binding.remote_media.asset_checksum, snapshot.asset_checksum)
        self.assertEqual(
            binding.remote_media.presentation_hash,
            snapshot.presentation_hash,
        )

    def test_external_write_gate_requires_exact_available_mapping(self):
        attempt = self._attempt()
        binding = services.prepare_publication_media_bindings_locked(
            attempt=attempt
        )[0]

        with self.assertRaises(Conflict):
            services.require_publication_media_ready_locked(attempt=attempt)

        binding.remote_media.state = publishing_models.RemoteMedia.State.AVAILABLE
        binding.remote_media.remote_media_id = "wp-media-101"
        binding.remote_media.remote_source_url = "https://example.com/media/101.png"
        binding.remote_media.last_reconciled_at = timezone.now()
        binding.remote_media.save(
            update_fields=(
                "state",
                "remote_media_id",
                "remote_source_url",
                "last_reconciled_at",
            )
        )
        binding.binding_state = publishing_models.PublicationMedia.BindingState.ACTIVE
        binding.remote_verified_at = timezone.now()
        binding.save(update_fields=("binding_state", "remote_verified_at"))

        services.require_publication_media_ready_locked(attempt=attempt)
        resolved = services._ready_publication_media_manifest(attempt=attempt)
        self.assertEqual(
            resolved[0]["assetId"],
            str(binding.published_evidence_snapshot_id),
        )
        self.assertEqual(resolved[0]["deliveryKind"], "wordpress_media")
        self.assertEqual(resolved[0]["deliveryId"], "wp-media-101")
        self.assertEqual(
            resolved[0]["deliveryUrl"],
            "https://example.com/media/101.png",
        )

    def test_blogger_uses_content_addressed_public_delivery_asset(self):
        attempt = self._blogger_attempt()
        binding = services.prepare_publication_media_bindings_locked(
            attempt=attempt
        )[0]

        self.assertIsNone(binding.remote_media_id)
        self.assertIsNotNone(binding.public_delivery_asset_id)
        delivery = binding.public_delivery_asset
        self.assertEqual(delivery.state, publishing_models.PublicDeliveryAsset.State.PENDING)
        self.assertIsNone(delivery.public_url)
        with self.assertRaises(Conflict):
            services.require_publication_media_ready_locked(attempt=attempt)

        delivery.state = publishing_models.PublicDeliveryAsset.State.AVAILABLE
        delivery.delivery_object_version = "delivery-version-1"
        delivery.public_url = "https://assets.example.com/t022/image.png"
        delivery.save(
            update_fields=("state", "delivery_object_version", "public_url")
        )
        binding.binding_state = publishing_models.PublicationMedia.BindingState.ACTIVE
        binding.remote_verified_at = timezone.now()
        binding.save(update_fields=("binding_state", "remote_verified_at"))

        resolved = services._ready_publication_media_manifest(attempt=attempt)
        self.assertEqual(resolved[0]["deliveryKind"], "public_delivery_asset")
        self.assertEqual(resolved[0]["deliveryId"], str(delivery.id))
        self.assertEqual(resolved[0]["deliveryUrl"], delivery.public_url)

    def test_sqlite_rejects_raw_mapping_and_binding_identity_updates(self):
        attempt = self._attempt()
        binding = services.prepare_publication_media_bindings_locked(
            attempt=attempt
        )[0]

        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publishing_publicationmedia "
                    "SET block_id = %s WHERE id = %s",
                    ["tampered", binding.id.hex],
                )
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publishing_remotemedia "
                    "SET asset_checksum = %s WHERE id = %s",
                    ["b" * 64, binding.remote_media_id.hex],
                )
