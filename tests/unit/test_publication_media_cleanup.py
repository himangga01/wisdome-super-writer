from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from apps.audit.models import RetentionHold
from apps.editorial.models import CorrectionCase
from apps.publishing import models as publishing_models
from apps.publishing import services


class PublicationMediaCleanupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from tests.unit.test_publication_media_bindings import (
            PublicationMediaBindingServiceTests,
        )

        PublicationMediaBindingServiceTests.setUpTestData()
        cls.fixture = PublicationMediaBindingServiceTests.fixture
        cls.revision = PublicationMediaBindingServiceTests.revision

    from tests.unit.test_publication_media_bindings import (
        PublicationMediaBindingServiceTests as _BindingFixture,
    )

    _evidence_revision = _BindingFixture._evidence_revision
    _intent_for_revision = _BindingFixture._intent_for_revision
    _attempt = _BindingFixture._attempt

    def _available_remote_binding(self):
        attempt = self._attempt()
        binding = services.prepare_publication_media_bindings_locked(
            attempt=attempt
        )[0]
        remote = binding.remote_media
        remote.state = publishing_models.RemoteMedia.State.AVAILABLE
        remote.remote_media_id = "wp-media-cleanup-1"
        remote.remote_source_url = "https://example.com/media/cleanup-1.png"
        remote.save(
            update_fields=(
                "state",
                "remote_media_id",
                "remote_source_url",
            )
        )
        return binding, remote

    def test_prepared_binding_blocks_orphan_grace(self):
        _binding, remote = self._available_remote_binding()

        operation = services.schedule_orphan_media_cleanup_locked(
            remote_media_id=remote.id,
        )

        remote.refresh_from_db()
        self.assertIsNone(operation)
        self.assertEqual(remote.state, publishing_models.RemoteMedia.State.AVAILABLE)
        self.assertIsNone(remote.orphaned_at)

    def test_zero_references_start_grace_then_create_exact_delete_generation(self):
        binding, remote = self._available_remote_binding()
        binding.binding_state = publishing_models.PublicationMedia.BindingState.REMOVED
        binding.removed_at = timezone.now()
        binding.save(update_fields=("binding_state", "removed_at"))

        first = services.schedule_orphan_media_cleanup_locked(
            remote_media_id=remote.id,
        )
        remote.refresh_from_db()
        self.assertIsNone(first)
        self.assertEqual(remote.state, publishing_models.RemoteMedia.State.ORPHANED)
        self.assertIsNotNone(remote.orphaned_at)

        publishing_models.RemoteMedia.objects.filter(pk=remote.id).update(
            orphaned_at=timezone.now() - timezone.timedelta(days=31),
        )
        operation = services.schedule_orphan_media_cleanup_locked(
            remote_media_id=remote.id,
        )

        self.assertIsNotNone(operation)
        self.assertEqual(operation.action, publishing_models.MediaDeliveryOperation.Action.DELETE)
        self.assertEqual(operation.generation, 1)
        self.assertEqual(operation.remote_media_id, remote.id)

    def test_rereference_supersedes_queued_delete_before_remote_io(self):
        binding, remote = self._available_remote_binding()
        binding.binding_state = publishing_models.PublicationMedia.BindingState.REMOVED
        binding.removed_at = timezone.now()
        binding.save(update_fields=("binding_state", "removed_at"))
        services.schedule_orphan_media_cleanup_locked(remote_media_id=remote.id)
        publishing_models.RemoteMedia.objects.filter(pk=remote.id).update(
            orphaned_at=timezone.now() - timezone.timedelta(days=31),
        )
        operation = services.schedule_orphan_media_cleanup_locked(
            remote_media_id=remote.id,
        )

        binding.binding_state = publishing_models.PublicationMedia.BindingState.ACTIVE
        binding.removed_at = None
        binding.save(update_fields=("binding_state", "removed_at"))
        replay = services.schedule_orphan_media_cleanup_locked(
            remote_media_id=remote.id,
        )

        self.assertIsNone(replay)
        operation.refresh_from_db()
        remote.refresh_from_db()
        self.assertEqual(
            operation.state,
            publishing_models.MediaDeliveryOperation.State.SUPERSEDED,
        )
        self.assertEqual(remote.state, publishing_models.RemoteMedia.State.AVAILABLE)

    def test_remote_body_correction_and_retention_hold_each_block_grace(self):
        binding, remote = self._available_remote_binding()
        binding.binding_state = publishing_models.PublicationMedia.BindingState.REMOVED
        binding.removed_at = timezone.now()
        binding.remote_body_hash = "a" * 64
        binding.save(
            update_fields=("binding_state", "removed_at", "remote_body_hash")
        )
        self.assertIsNone(
            services.schedule_orphan_media_cleanup_locked(
                remote_media_id=remote.id
            )
        )
        remote.refresh_from_db()
        self.assertIsNone(remote.orphaned_at)

        binding.remote_body_hash = ""
        binding.save(update_fields=("remote_body_hash",))
        hold = RetentionHold.objects.create(
            scope_type=remote._meta.label_lower,
            scope_id=remote.id,
            reason="legal hold",
            created_by=self.fixture.user,
        )
        self.assertIsNone(
            services.schedule_orphan_media_cleanup_locked(
                remote_media_id=remote.id
            )
        )
        remote.refresh_from_db()
        self.assertIsNone(remote.orphaned_at)

        hold.active = False
        hold.released_at = timezone.now()
        hold.save(update_fields=("active", "released_at"))
        snapshot = binding.published_evidence_snapshot
        CorrectionCase.objects.create(
            article=binding.article_revision.article,
            source_item=snapshot.evidence.source_item,
            subject_hash="b" * 64,
            state=CorrectionCase.State.APPLYING,
        )
        self.assertIsNone(
            services.schedule_orphan_media_cleanup_locked(
                remote_media_id=remote.id
            )
        )
        remote.refresh_from_db()
        self.assertIsNone(remote.orphaned_at)
