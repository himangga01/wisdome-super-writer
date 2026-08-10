from __future__ import annotations

import importlib
import uuid
from types import SimpleNamespace
from unittest import TestCase as UnitTestCase

from django.db import DatabaseError, connection, transaction
from django.db.models import PROTECT
from django.test import TestCase

from apps.publishing.models import Publication, PublicationAttempt
from tests.unit import test_publication_approval_db as approval_db


class PublicationDependencyMigrationContractTests(UnitTestCase):
    def test_dependency_fk_is_protected_and_postgres_guard_is_exact(self):
        field = PublicationAttempt._meta.get_field("depends_on_attempt")
        self.assertIs(field.remote_field.on_delete, PROTECT)

        migration = importlib.import_module(
            "apps.publishing.migrations.0014_publication_dependency"
        )
        sql = "\n".join(migration.POSTGRES_STATEMENTS)
        self.assertIn("dependency_target.role = 'primary_canonical'", sql)
        self.assertIn(
            "dependency_target.environment = child_target.environment",
            sql,
        )
        self.assertIn(
            "dependency.publication_intent_id = NEW.publication_intent_id",
            sql,
        )
        self.assertIn("canonicalDependencyTargetId", sql)
        self.assertIn("publication attempt dependency is immutable", sql)

    def test_reverse_rejects_frozen_dependencies(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0014_publication_dependency"
        )

        class Rows:
            @staticmethod
            def using(_alias):
                return Rows()

            @staticmethod
            def filter(**_kwargs):
                return Rows()

            @staticmethod
            def exists():
                return True

        apps = SimpleNamespace(
            get_model=lambda *_args: SimpleNamespace(objects=Rows()),
        )
        editor = SimpleNamespace(connection=SimpleNamespace(alias="default"))
        with self.assertRaisesRegex(Exception, "T024 dependency manifests"):
            migration.reject_populated_reverse(apps, editor)


class PublicationDependencySQLiteGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        approval_db.PublicationApprovalSQLiteTriggerTests.setUpTestData()
        cls.fixture = approval_db.PublicationApprovalSQLiteTriggerTests.fixture
        cls.fixture.intent.state = "approved"
        cls.fixture.intent.save(update_fields=("state",))
        cls.publication = Publication.objects.create(
            article_id=cls.fixture.intent.article_id,
            target=cls.fixture.target,
            origin_target_snapshot_id=cls.fixture.snapshot.id,
            remote_lookup_key=f"t024-{uuid.uuid4()}",
        )
        cls.attempt = PublicationAttempt.objects.create(
            publication=cls.publication,
            article_revision=cls.fixture.revision,
            publication_intent=cls.fixture.intent,
            target_snapshot=cls.fixture.snapshot,
            target_config_hash=cls.fixture.snapshot.config_hash,
            resolved_action="create",
            target_command_hash="7" * 64,
            publisher_contract_version="publisher-v1",
            publisher_adapter_manifest_hash="5" * 64,
            approval=cls.fixture.root,
            approval_subject_hash=cls.fixture.subject_hash,
            idempotency_key=f"t024-{uuid.uuid4()}",
            remote_lookup_key=cls.publication.remote_lookup_key,
            request_fingerprint="2" * 64,
            correlation_id=uuid.uuid4(),
        )

    def test_raw_dependency_identity_update_is_rejected(self):
        table = connection.ops.quote_name(PublicationAttempt._meta.db_table)
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {table} SET dependency_subject_hash = %s WHERE id = %s",
                    ["a" * 64, self.attempt.id.hex],
                )
