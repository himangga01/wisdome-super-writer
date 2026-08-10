import importlib
import importlib.util
from unittest import TestCase

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase as DjangoTestCase

from apps.publishing import models as publishing_models
from tests.unit.test_publication_approval_db import (
    ApprovalGuardedTransactionTestCase,
)


class PublishedAssetSnapshotMigrationContractTests(TestCase):
    """Break caught: raw SQL can mutate or re-parent frozen asset lineage."""

    def test_0012_installs_sqlite_and_postgresql_lineage_guards(self):
        module_name = "apps.publishing.migrations.0012_published_asset_snapshots"
        self.assertIsNotNone(importlib.util.find_spec(module_name))
        migration = importlib.import_module(module_name)

        sqlite_sql = "\n".join(migration.SQLITE_GUARD_SQL)
        postgres_sql = "\n".join(migration.POSTGRES_GUARD_SQL)
        for sql in (sqlite_sql, postgres_sql):
            self.assertIn("publishing_publishedassetcohort", sql)
            self.assertIn("publishing_publishedevidencesnapshot", sql)
            self.assertIn("publishing_publishedvisualizationsnapshot", sql)
            self.assertIn("publishing_publishedvisualizationinput", sql)
            self.assertIn("append-only", sql)
            self.assertIn("cohort revision mismatch", sql)
            self.assertIn("visualization input cohort mismatch", sql)

    def test_0012_populated_reverse_is_explicitly_irreversible(self):
        module_name = "apps.publishing.migrations.0012_published_asset_snapshots"
        self.assertIsNotNone(importlib.util.find_spec(module_name))
        migration = importlib.import_module(module_name)
        self.assertTrue(callable(migration.reject_populated_reverse))


class PublishedAssetSnapshotSQLiteGuardTests(DjangoTestCase):
    @classmethod
    def setUpTestData(cls):
        from tests.unit.test_publication_approval_db import (
            PublicationApprovalSQLiteTriggerTests,
        )

        PublicationApprovalSQLiteTriggerTests.setUpTestData()
        cls.revision = PublicationApprovalSQLiteTriggerTests.fixture.revision

    def _cohort(self):
        return publishing_models.PublishedAssetCohort.objects.create(
            revision=self.revision,
            item_count=0,
            manifest=[],
            manifest_hash=(
                "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e7c2a7f"
                "15ab5a670f7a8e73"
            ),
        )

    def test_sqlite_rejects_manifest_count_mismatch(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            publishing_models.PublishedAssetCohort.objects.create(
                revision=self.revision,
                item_count=1,
                manifest=[],
                manifest_hash="a" * 64,
            )

    def test_snapshot_cohort_is_immutable_through_orm_and_raw_sql(self):
        cohort = self._cohort()
        with self.assertRaises(TypeError):
            publishing_models.PublishedAssetCohort.objects.filter(
                pk=cohort.pk
            ).update(item_count=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publishing_publishedassetcohort "
                    "SET item_count = 1 WHERE id = %s",
                    [cohort.id.hex],
                )
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM publishing_publishedassetcohort WHERE id = %s",
                    [cohort.id.hex],
                )


class PublishedAssetSnapshotMigrationExecutorTests(
    ApprovalGuardedTransactionTestCase
):
    before = [
        ("publishing", "0011_publication_attempt_fencing"),
        ("editorial", "0003_editorial_policy_runtime"),
        ("evidence", "0006_generic_evidence_manifest"),
    ]
    under_test = [("publishing", "0012_published_asset_snapshots")]

    def test_0012_has_an_actual_empty_sqlite_forward_and_reverse_path(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate(self.before)
            MigrationExecutor(connection).migrate(self.under_test)
            state = MigrationExecutor(connection).loader.project_state(
                self.under_test
            )
            cohort = state.apps.get_model("publishing", "PublishedAssetCohort")
            self.assertTrue(cohort._meta.get_field("revision").unique)

            MigrationExecutor(connection).migrate(self.before)
            state = MigrationExecutor(connection).loader.project_state(self.before)
            with self.assertRaises(LookupError):
                state.apps.get_model("publishing", "PublishedAssetCohort")
        finally:
            MigrationExecutor(connection).migrate(latest)
