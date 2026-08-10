import importlib
from unittest import TestCase

from django.db import IntegrityError, connection, transaction
from django.test import TestCase as DjangoTestCase

from apps.publishing import models as publishing_models
from apps.publishing import services


class MediaDeliveryOperationMigrationContractTests(TestCase):
    def test_0012_guards_operation_lineage_with_sqlite_postgresql_parity(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0012_published_asset_snapshots"
        )
        for sql in (
            "\n".join(migration.SQLITE_GUARD_SQL),
            "\n".join(migration.POSTGRES_GUARD_SQL),
        ):
            self.assertIn("publishing_mediadeliveryoperation", sql)
            self.assertIn("infrastructure_outboxmessage", sql)
            self.assertIn("infrastructure_outboxconsumerreceipt", sql)
            self.assertIn("media delivery operation identity is immutable", sql)
            self.assertIn("media delivery operation is append-only", sql)


class MediaDeliveryOperationSQLiteGuardTests(DjangoTestCase):
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

    def _operation(self):
        attempt = self._attempt()
        binding = services.prepare_publication_media_bindings_locked(
            attempt=attempt
        )[0]
        operation, _created = services.enqueue_media_delivery_operation_locked(
            remote_media=binding.remote_media,
            action=publishing_models.MediaDeliveryOperation.Action.DELETE,
        )
        return operation

    def test_raw_identity_update_and_delete_are_rejected(self):
        operation = self._operation()
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publishing_mediadeliveryoperation "
                    "SET generation = 2 WHERE id = %s",
                    [operation.id.hex],
                )
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM publishing_mediadeliveryoperation WHERE id = %s",
                    [operation.id.hex],
                )
