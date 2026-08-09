import importlib
import threading
import uuid
from types import SimpleNamespace
from unittest import TestCase as UnitTestCase
from unittest.mock import patch

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

from apps.publishing import models as publishing_models
from tests.unit.test_publication_approval_db import (
    ApprovalGuardedTransactionTestCase,
)


class PublicationIntentIdentityModelContractTests(UnitTestCase):
    def test_intent_dispatch_and_attempt_have_durable_single_writer_identity(self):
        intent = publishing_models.PublicationIntent
        intent_fields = {field.name for field in intent._meta.get_fields()}

        self.assertIn("request_hash", intent_fields)
        self.assertIn("request_hash_version", intent_fields)
        self.assertIn("supersedes_intent", intent_fields)
        self.assertFalse(intent._meta.get_field("request_hash").null)
        self.assertFalse(intent._meta.get_field("request_hash_version").null)
        self.assertFalse(intent._meta.get_field("intent_hash").unique)
        self.assertIs(
            intent._meta.get_field("supersedes_intent").remote_field.on_delete,
            PROTECT,
        )
        self.assertEqual(
            intent._meta.get_field("supersedes_intent").db_column,
            "supersedes_intent_id",
        )
        self.assertIn(
            "uq_intent_article_request",
            {constraint.name for constraint in intent._meta.constraints},
        )

        self.assertTrue(hasattr(publishing_models, "PublicationIntentHead"))
        head = publishing_models.PublicationIntentHead
        self.assertFalse(head._meta.get_field("article_id").null)
        self.assertTrue(head._meta.get_field("article_id").unique)
        self.assertIs(
            head._meta.get_field("latest_intent").remote_field.on_delete,
            PROTECT,
        )

        self.assertTrue(hasattr(publishing_models, "PublicationDispatch"))
        dispatch = publishing_models.PublicationDispatch
        self.assertTrue(dispatch._meta.get_field("publication_intent").unique)
        self.assertFalse(dispatch._meta.get_field("request_hash").null)
        self.assertFalse(dispatch._meta.get_field("attempt_manifest_hash").null)

        self.assertIn(
            "uq_attempt_intent_publication",
            {
                constraint.name
                for constraint in publishing_models.PublicationAttempt._meta.constraints
            },
        )


class PublicationIdentityConstantTests(UnitTestCase):
    def test_legacy_request_identity_is_explicitly_non_replayable(self):
        self.assertEqual(
            publishing_models.LEGACY_UNVERIFIABLE_INTENT_REQUEST_VERSION,
            "legacy-unverifiable-v1",
        )
        self.assertEqual(
            publishing_models.PUBLICATION_INTENT_REQUEST_VERSION,
            "publication-intent-request-v1",
        )
        self.assertEqual(
            publishing_models.PUBLICATION_DISPATCH_REQUEST_VERSION,
            "publication-dispatch-request-v1",
        )


class PublicationIdentityMigrationContractTests(UnitTestCase):
    def test_legacy_request_hash_is_a_deterministic_non_replayable_sentinel(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0010_intent_dispatch_identity"
        )
        sentinel = getattr(
            migration,
            "legacy_unverifiable_request_hash",
            lambda **_kwargs: "",
        )(
            publication_intent_id="11111111-1111-1111-1111-111111111111",
            intent_hash="a" * 64,
        )

        self.assertEqual(
            sentinel,
            "23243a811be399a13c03e51b259f8583405d436611876bd15dc2a42fbf5e8fd8",
        )
        self.assertNotEqual(sentinel, "a" * 64)

    def test_postgresql_guard_contract_covers_all_t020_single_writer_tables(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0010_intent_dispatch_identity"
        )
        statements = getattr(migration, "POSTGRES_GUARD_SQL", ())
        sql = "\n".join(statements)

        self.assertIn("publishing_publicationintent", sql)
        self.assertIn("publishing_publicationintenthead", sql)
        self.assertIn("publishing_publicationdispatch", sql)
        self.assertIn("publishing_publicationattempt", sql)
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("IS DISTINCT FROM", sql)
        self.assertIn("publisher_contract_version = NEW.publisher_contract_version", sql)
        self.assertIn(
            "publisher_adapter_manifest_hash = NEW.publisher_adapter_manifest_hash",
            sql,
        )
        self.assertIn("publication dispatch attempt cohort is frozen", sql)
        self.assertIn("jsonb_array_length(target_commands)", sql)
        self.assertIn(
            "publication.remote_lookup_key = NEW.remote_lookup_key",
            sql,
        )
        self.assertIn("publishing_target_snapshot_guard_t020_fn", sql)
        self.assertIn("publishing_publication_insert_guard_t020_fn", sql)
        self.assertIn("publishing_publication_update_guard_t020_fn", sql)
        self.assertIn("btrim(COALESCE(NEW.remote_lookup_key, ''))", sql)
        self.assertIn("snapshot.id = NEW.origin_target_snapshot_id", sql)
        self.assertIn("snapshot.target_id = NEW.target_id", sql)
        self.assertIn(
            "head.subject_hash = attempt.approval_subject_hash",
            sql,
        )
        self.assertIn("intent.state = 'approved'", sql)
        self.assertIn(
            "NEW.resolved_action IN ('unpublish', 'mark_withdrawn')",
            sql,
        )
        self.assertIn("intent.state IN ('approved', 'stale')", sql)
        self.assertIn("FOR UPDATE OF intent_head", sql)
        self.assertIn("FOR UPDATE OF head", sql)
        self.assertNotIn("ON CONFLICT", sql)

    def test_postgresql_content_writers_lock_intent_head_before_intent(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0010_intent_dispatch_identity"
        )
        statements = getattr(migration, "POSTGRES_GUARD_SQL", ())
        for function_name in (
            "publishing_dispatch_guard_t020_fn",
            "publishing_attempt_guard_t020_fn",
        ):
            function_sql = next(
                statement
                for statement in statements
                if f"FUNCTION {function_name}" in statement
            )
            self.assertLess(
                function_sql.index("PERFORM intent_head.id"),
                function_sql.index("PERFORM intent.id"),
                function_name,
            )


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class PublicationIntentSQLiteGuardTests(TestCase):
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
            remote_lookup_key=f"t020-{_uuid()}",
        )

    def _attempt(self, *, publication=None, **overrides):
        publication = publication or self._publication()
        values = {
            "publication": publication,
            "article_revision": self.fixture.revision,
            "publication_intent": self.fixture.intent,
            "target_snapshot": self.fixture.snapshot,
            "target_config_hash": self.fixture.snapshot.config_hash,
            "resolved_action": "create",
            "target_command_hash": "7" * 64,
            "publisher_contract_version": "publisher-v1",
            "publisher_adapter_manifest_hash": "5" * 64,
            "approval": self.fixture.root,
            "approval_subject_hash": self.fixture.subject_hash,
            "idempotency_key": f"t020-attempt-{_uuid()}",
            "remote_lookup_key": publication.remote_lookup_key,
            "request_fingerprint": "2" * 64,
            "correlation_id": _uuid(),
        }
        values.update(overrides)
        return publishing_models.PublicationAttempt.objects.create(**values)

    def _raw_publication_insert(self, *, origin_snapshot_id, remote_lookup_key):
        publication_id = _uuid()
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO publishing_publication "
                "(id, article_id, target_id, origin_target_snapshot_id, "
                "remote_lookup_key, state, remote_state, last_error_code, "
                "created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, 'pending', 'unknown', '', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                [
                    publication_id.hex,
                    self.fixture.intent.article_id.hex,
                    self.fixture.target.id.hex,
                    origin_snapshot_id.hex,
                    remote_lookup_key,
                ],
            )
        return publication_id

    def test_intent_insert_atomically_advances_one_head_and_rejects_a_branch(self):
        root = self.fixture.intent
        child = publishing_models.PublicationIntent.objects.create(
            article_id=root.article_id,
            article_revision=root.article_revision,
            revision_no=root.revision_no,
            revision_content_hash=root.revision_content_hash,
            target_snapshot_refs=root.target_snapshot_refs,
            target_commands=root.target_commands,
            target_snapshot_manifest_hash=root.target_snapshot_manifest_hash,
            approval_mode=root.approval_mode,
            input_evidence_manifest_hash=root.input_evidence_manifest_hash,
            quality_gate_manifest_hash=root.quality_gate_manifest_hash,
            quality_report_hash=root.quality_report_hash,
            supersedes_intent=root,
            intent_hash=root.intent_hash,
            request_key="intent-t020-child",
            request_hash="3" * 64,
            request_hash_version="publication-intent-request-v1",
            created_by=self.fixture.user,
        )

        head = publishing_models.PublicationIntentHead.objects.get(
            article_id=root.article_id
        )
        self.assertEqual(head.latest_intent_id, child.id)
        self.assertEqual(head.version, 2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            publishing_models.PublicationIntent.objects.create(
                article_id=root.article_id,
                article_revision=root.article_revision,
                revision_no=root.revision_no,
                revision_content_hash=root.revision_content_hash,
                target_snapshot_refs=root.target_snapshot_refs,
                target_commands=root.target_commands,
                target_snapshot_manifest_hash=root.target_snapshot_manifest_hash,
                approval_mode=root.approval_mode,
                input_evidence_manifest_hash=root.input_evidence_manifest_hash,
                quality_gate_manifest_hash=root.quality_gate_manifest_hash,
                quality_report_hash=root.quality_report_hash,
                supersedes_intent=root,
                intent_hash="4" * 64,
                request_key="intent-t020-branch",
                request_hash="5" * 64,
                request_hash_version="publication-intent-request-v1",
                created_by=self.fixture.user,
            )

    def test_attempt_insert_requires_exact_lineage_and_one_row_per_target(self):
        publication = self._publication()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._attempt(
                publication=publication,
                target_config_hash="9" * 64,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._attempt(
                publication=publication,
                publisher_contract_version="publisher-v2",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._attempt(
                publication=publication,
                publisher_adapter_manifest_hash="9" * 64,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._attempt(
                publication=publication,
                auto_publish_activation_id=_uuid(),
                auto_publish_activation_hash="8" * 64,
            )
        attempt = self._attempt(publication=publication)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._attempt(publication=publication)
        self.assertEqual(
            publishing_models.PublicationAttempt.objects.filter(
                publication_intent=self.fixture.intent,
                publication=publication,
            ).count(),
            1,
        )
        self.assertEqual(attempt.attempt_no, 1)

    def test_attempt_remote_lookup_must_match_its_publication(self):
        publication = self._publication()

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._attempt(
                publication=publication,
                remote_lookup_key="different-remote-object",
            )

    def test_content_attempt_requires_current_approved_intent_head(self):
        root = self.fixture.intent
        publishing_models.PublicationIntent.objects.create(
            article_id=root.article_id,
            article_revision=root.article_revision,
            revision_no=root.revision_no,
            revision_content_hash=root.revision_content_hash,
            target_snapshot_refs=root.target_snapshot_refs,
            target_commands=root.target_commands,
            target_snapshot_manifest_hash=root.target_snapshot_manifest_hash,
            approval_mode=root.approval_mode,
            input_evidence_manifest_hash=root.input_evidence_manifest_hash,
            quality_gate_manifest_hash=root.quality_gate_manifest_hash,
            quality_report_hash=root.quality_report_hash,
            supersedes_intent=root,
            intent_hash=root.intent_hash,
            request_key="intent-t020-new-head",
            request_hash="3" * 64,
            request_hash_version="publication-intent-request-v1",
            created_by=self.fixture.user,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._attempt()

    def test_content_attempt_requires_approved_intent_state(self):
        self.fixture.intent.state = (
            publishing_models.PublicationIntent.State.AWAITING_APPROVAL
        )
        self.fixture.intent.save(update_fields=("state",))

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._attempt()

    def test_publication_and_snapshot_frozen_identity_reject_orm_and_raw_updates(self):
        publication = self._publication()

        with self.assertRaises(TypeError):
            publishing_models.Publication.objects.filter(pk=publication.pk).update(
                remote_lookup_key="changed-publication-lookup"
            )
        original_lookup_key = publication.remote_lookup_key
        publication.remote_lookup_key = "changed-publication-instance-lookup"
        with self.assertRaises(TypeError):
            publication.save()
        publication.remote_lookup_key = original_lookup_key
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publishing_publication SET article_id = %s WHERE id = %s",
                    [_uuid().hex, publication.id.hex],
                )
        with self.assertRaises(TypeError):
            publishing_models.PublicationTargetSnapshot.objects.filter(
                pk=self.fixture.snapshot.pk
            ).update(config_hash="9" * 64)
        self.fixture.snapshot.config_hash = "9" * 64
        with self.assertRaises(TypeError):
            self.fixture.snapshot.save(update_fields=("config_hash",))
        with self.assertRaises(TypeError):
            self.fixture.snapshot.delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publishing_publicationtargetsnapshot "
                    "SET publisher_adapter_manifest_hash = %s WHERE id = %s",
                    ["8" * 64, self.fixture.snapshot.id.hex],
                )

        publication.state = publishing_models.Publication.State.IN_PROGRESS
        publication.remote_state = publishing_models.Publication.RemoteState.DRAFT
        publication.save(update_fields=("state", "remote_state", "updated_at"))
        publication.refresh_from_db()
        self.assertEqual(
            publication.state,
            publishing_models.Publication.State.IN_PROGRESS,
        )

    def test_publication_orm_insert_rejects_empty_remote_lookup_identity(self):
        with self.assertRaises(TypeError):
            publishing_models.Publication.objects.create(
                article_id=self.fixture.intent.article_id,
                target=self.fixture.target,
                origin_target_snapshot_id=self.fixture.snapshot.id,
                remote_lookup_key="",
            )

    def test_publication_orm_insert_rejects_cross_target_origin_snapshot(self):
        other_target = publishing_models.PublicationTarget.objects.create(
            channel="blogger",
            role="secondary_distribution",
            environment="test",
            display_name="T020 other target",
            base_url="https://other.example.com/",
        )
        other_snapshot = publishing_models.PublicationTargetSnapshot.objects.create(
            target=other_target,
            version=1,
            channel="blogger",
            role="secondary_distribution",
            environment="test",
            base_url="https://other.example.com/",
            credential_ref_identity_hash="4" * 64,
            connection_state="verified",
            preflight_state="passed",
            canary_state="passed",
            pilot_state="passed",
            publisher_contract_version="publisher-v1",
            publisher_adapter_manifest_hash="5" * 64,
            config_hash="6" * 64,
        )

        with self.assertRaises(TypeError):
            publishing_models.Publication.objects.create(
                article_id=self.fixture.intent.article_id,
                target=self.fixture.target,
                origin_target_snapshot_id=other_snapshot.id,
                remote_lookup_key=f"cross-target-{_uuid()}",
            )

    def test_publication_raw_insert_rejects_empty_remote_lookup_identity(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._raw_publication_insert(
                origin_snapshot_id=self.fixture.snapshot.id,
                remote_lookup_key="",
            )

    def test_publication_raw_insert_rejects_unknown_origin_snapshot(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._raw_publication_insert(
                origin_snapshot_id=_uuid(),
                remote_lookup_key=f"unknown-origin-{_uuid()}",
            )

    def test_frozen_intent_attempt_and_dispatch_material_reject_raw_mutation(self):
        attempt = self._attempt()
        with self.assertRaises(IntegrityError), transaction.atomic():
            publishing_models.PublicationDispatch.objects.create(
                publication_intent=self.fixture.intent,
                request_key="dispatch-before-state",
                request_hash="6" * 64,
                request_hash_version="publication-dispatch-request-v1",
                correlation_id=attempt.correlation_id,
                attempt_count=1,
                attempt_manifest_hash="7" * 64,
            )
        self.fixture.intent.state = publishing_models.PublicationIntent.State.DISPATCHED
        self.fixture.intent.save(update_fields=("state",))
        dispatch = publishing_models.PublicationDispatch.objects.create(
            publication_intent=self.fixture.intent,
            request_key="dispatch-t020",
            request_hash="6" * 64,
            request_hash_version="publication-dispatch-request-v1",
            correlation_id=attempt.correlation_id,
            attempt_count=1,
            attempt_manifest_hash="7" * 64,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publishing_publicationintent SET intent_hash = %s WHERE id = %s",
                    ["8" * 64, self.fixture.intent.id.hex],
                )
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publishing_publicationattempt SET request_fingerprint = %s WHERE id = %s",
                    ["9" * 64, attempt.id.hex],
                )
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publishing_publicationdispatch SET request_key = %s WHERE id = %s",
                    ["changed-dispatch", dispatch.id.hex],
                )

        attempt.state = publishing_models.PublicationAttempt.State.RUNNING
        attempt.attempt_no = 2
        attempt.save(update_fields=("state", "attempt_no"))
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, publishing_models.PublicationAttempt.State.RUNNING)
        self.assertEqual(attempt.attempt_no, 2)

    def test_dispatch_revalidates_every_attempt_current_approval_head(self):
        attempt = self._attempt()
        revoked = publishing_models.Approval.objects.create(
            article_revision=self.fixture.revision,
            revision_no=self.fixture.root.revision_no,
            publication_intent=self.fixture.intent,
            target=self.fixture.target,
            target_action=self.fixture.root.target_action,
            article_channel_render=self.fixture.render,
            action_subject=self.fixture.root.action_subject,
            target_snapshot=self.fixture.snapshot,
            target_config_hash=self.fixture.snapshot.config_hash,
            mode="manual",
            decision="revoked",
            approval_subject_hash=self.fixture.subject_hash,
            approval_material_version="approval-subject-v3",
            decision_hash="4" * 64,
            decision_reason="dispatch race revocation",
            decision_actor_type="admin",
            decision_actor_id=self.fixture.user.id,
            head_version=2,
            supersedes_approval=self.fixture.root,
            request_key="approval-t020-dispatch-race",
            request_hash="5" * 64,
            policy_snapshot_hash=self.fixture.revision.editorial_policy_hash,
            quality_report_hash=self.fixture.revision.quality_report_hash,
            render_template_hash=self.fixture.render.template_hash,
            source_manifest_hash=self.fixture.render.source_manifest_hash,
            admin=self.fixture.user,
        )
        self.assertEqual(revoked.decision, "revoked")
        self.fixture.intent.state = publishing_models.PublicationIntent.State.DISPATCHED
        self.fixture.intent.save(update_fields=("state",))

        with self.assertRaises(IntegrityError), transaction.atomic():
            publishing_models.PublicationDispatch.objects.create(
                publication_intent=self.fixture.intent,
                request_key="dispatch-after-revoke",
                request_hash="6" * 64,
                request_hash_version="publication-dispatch-request-v1",
                correlation_id=attempt.correlation_id,
                attempt_count=1,
                attempt_manifest_hash="7" * 64,
            )


class _Rows(list):
    def using(self, alias):
        return self

    def order_by(self, *fields):
        return self

    def select_related(self, *fields):
        return self


class _FakeModel:
    def __init__(self, rows):
        self.objects = _Rows(rows)


class _FakeApps:
    def __init__(self, *, intents, attempts, publications=(), snapshots=()):
        self.models = {
            ("publishing", "PublicationIntent"): _FakeModel(intents),
            ("publishing", "PublicationAttempt"): _FakeModel(attempts),
            ("publishing", "Publication"): _FakeModel(publications),
            ("publishing", "PublicationTargetSnapshot"): _FakeModel(snapshots),
        }

    def get_model(self, app_label, model_name):
        return self.models[(app_label, model_name)]


class PublicationIdentityLegacyValidationTests(UnitTestCase):
    def setUp(self):
        self.migration = importlib.import_module(
            "apps.publishing.migrations.0010_intent_dispatch_identity"
        )
        self.schema_editor = SimpleNamespace(
            connection=SimpleNamespace(alias="default")
        )

    def test_ambiguous_legacy_intent_chain_is_rejected(self):
        article_id = _uuid()
        root_id = _uuid()
        intents = [
            SimpleNamespace(
                id=root_id,
                article_id=article_id,
                request_key="root",
                supersedes_intent_id=None,
            ),
            SimpleNamespace(
                id=_uuid(),
                article_id=article_id,
                request_key="branch-a",
                supersedes_intent_id=root_id,
            ),
            SimpleNamespace(
                id=_uuid(),
                article_id=article_id,
                request_key="branch-b",
                supersedes_intent_id=root_id,
            ),
        ]

        with self.assertRaisesRegex(RuntimeError, "ambiguous publication intent chain"):
            self.migration.validate_legacy_t020_rows(
                _FakeApps(intents=intents, attempts=[]),
                self.schema_editor,
            )

    def test_duplicate_legacy_target_attempts_are_rejected(self):
        article_id = _uuid()
        intent_id = _uuid()
        publication_id = _uuid()
        intent = SimpleNamespace(
            id=intent_id,
            article_id=article_id,
            request_key="root",
            supersedes_intent_id=None,
        )
        attempts = [
            SimpleNamespace(
                id=_uuid(),
                publication_intent_id=intent_id,
                publication_id=publication_id,
                publication=SimpleNamespace(target_id=_uuid()),
            ),
            SimpleNamespace(
                id=_uuid(),
                publication_intent_id=intent_id,
                publication_id=publication_id,
                publication=SimpleNamespace(target_id=_uuid()),
            ),
        ]

        with patch.object(self.migration, "_validate_attempt", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "duplicate target attempts"):
                self.migration.validate_legacy_t020_rows(
                    _FakeApps(intents=[intent], attempts=attempts),
                    self.schema_editor,
                )

    def test_incomplete_legacy_dispatch_target_cohort_is_rejected(self):
        article_id = _uuid()
        intent_id = _uuid()
        target_a = _uuid()
        target_b = _uuid()
        intent = SimpleNamespace(
            id=intent_id,
            article_id=article_id,
            request_key="root",
            supersedes_intent_id=None,
            target_commands=[
                {"targetId": str(target_a)},
                {"targetId": str(target_b)},
            ],
            target_snapshot_refs=[
                {"targetId": str(target_a)},
                {"targetId": str(target_b)},
            ],
        )
        attempts = [
            SimpleNamespace(
                id=_uuid(),
                publication_intent_id=intent_id,
                publication_id=_uuid(),
                publication=SimpleNamespace(target_id=target_a),
            )
        ]

        with patch.object(self.migration, "_validate_attempt", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "incomplete target cohort"):
                self.migration.validate_legacy_t020_rows(
                    _FakeApps(intents=[intent], attempts=attempts),
                    self.schema_editor,
                )

    def test_legacy_publication_with_empty_remote_lookup_is_rejected(self):
        target_id = _uuid()
        snapshot_id = _uuid()
        publication = SimpleNamespace(
            id=_uuid(),
            target_id=target_id,
            origin_target_snapshot_id=snapshot_id,
            remote_lookup_key="",
        )
        snapshot = SimpleNamespace(id=snapshot_id, target_id=target_id)

        with self.assertRaisesRegex(RuntimeError, "Publication frozen identity"):
            self.migration.validate_legacy_t020_rows(
                _FakeApps(
                    intents=[],
                    attempts=[],
                    publications=[publication],
                    snapshots=[snapshot],
                ),
                self.schema_editor,
            )

    def test_legacy_publication_origin_snapshot_must_belong_to_its_target(self):
        publication_target_id = _uuid()
        snapshot = SimpleNamespace(id=_uuid(), target_id=_uuid())
        publication = SimpleNamespace(
            id=_uuid(),
            target_id=publication_target_id,
            origin_target_snapshot_id=snapshot.id,
            remote_lookup_key="legacy-publication-key",
        )

        with self.assertRaisesRegex(RuntimeError, "Publication frozen identity"):
            self.migration.validate_legacy_t020_rows(
                _FakeApps(
                    intents=[],
                    attempts=[],
                    publications=[publication],
                    snapshots=[snapshot],
                ),
                self.schema_editor,
            )


class PublicationIdentityGuardedTransactionTestCase(
    ApprovalGuardedTransactionTestCase
):
    pass


class PublicationIdentityMigrationExecutorTests(
    PublicationIdentityGuardedTransactionTestCase
):
    before = [
        ("publishing", "0009_approval_decision_integrity"),
        ("editorial", "0003_editorial_policy_runtime"),
        ("audit", "0002_auditevent_append_only"),
        ("accounts", "0002_adminaccount_reauthentication_throttle"),
    ]
    under_test = [("publishing", "0010_intent_dispatch_identity")]

    def _target(self, *, display_name):
        return publishing_models.PublicationTarget.objects.create(
            channel="wordpress",
            role="primary_canonical",
            environment="test",
            display_name=display_name,
            base_url="https://example.com/",
        )

    def _snapshot(self, target):
        return publishing_models.PublicationTargetSnapshot.objects.create(
            target=target,
            version=1,
            channel="wordpress",
            role="primary_canonical",
            environment="test",
            base_url="https://example.com/",
            credential_ref_identity_hash="4" * 64,
            connection_state="verified",
            preflight_state="passed",
            canary_state="passed",
            pilot_state="passed",
            publisher_contract_version="publisher-v1",
            publisher_adapter_manifest_hash="5" * 64,
            config_hash="6" * 64,
        )

    def test_0010_has_an_actual_empty_sqlite_forward_and_reverse_path(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate(self.before)
            MigrationExecutor(connection).migrate(self.under_test)
            state = MigrationExecutor(connection).loader.project_state(
                self.under_test
            )
            intent = state.apps.get_model("publishing", "PublicationIntent")
            dispatch = state.apps.get_model("publishing", "PublicationDispatch")
            self.assertFalse(intent._meta.get_field("request_hash").null)
            self.assertTrue(
                dispatch._meta.get_field("publication_intent").unique
            )

            MigrationExecutor(connection).migrate(self.before)
            state = MigrationExecutor(connection).loader.project_state(self.before)
            legacy_intent = state.apps.get_model(
                "publishing", "PublicationIntent"
            )
            with self.assertRaises(Exception):
                legacy_intent._meta.get_field("request_hash")
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_0010_populated_reverse_is_explicitly_irreversible(self):
        from tests.unit.test_publication_approval_db import (
            PublicationApprovalSQLiteTriggerTests,
        )

        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        PublicationApprovalSQLiteTriggerTests.setUpTestData()
        intent_id = PublicationApprovalSQLiteTriggerTests.fixture.intent.id
        try:
            with self.assertRaises(IrreversibleError):
                MigrationExecutor(connection).migrate(self.before)
            self.assertTrue(
                publishing_models.PublicationIntent.objects.filter(
                    id=intent_id
                ).exists()
            )
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_0010_snapshot_only_database_is_explicitly_irreversible(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        self._snapshot(self._target(display_name="T020 reverse snapshot"))
        try:
            with self.assertRaises(IrreversibleError):
                MigrationExecutor(connection).migrate(self.before)
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_0010_publication_only_database_is_explicitly_irreversible(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0010_intent_dispatch_identity"
        )
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        target = self._target(display_name="T020 reverse publication")
        publication_id = _uuid()
        with connection.schema_editor() as editor:
            migration.remove_t020_guards(None, editor)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO publishing_publication "
                    "(id, article_id, target_id, origin_target_snapshot_id, "
                    "remote_lookup_key, state, remote_state, last_error_code, "
                    "created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, 'pending', 'unknown', '', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    [
                        publication_id.hex,
                        _uuid().hex,
                        target.id.hex,
                        _uuid().hex,
                        "reverse-publication-only",
                    ],
                )
        finally:
            with connection.schema_editor() as editor:
                migration.install_t020_guards(None, editor)
        try:
            with self.assertRaises(IrreversibleError):
                MigrationExecutor(connection).migrate(self.before)
        finally:
            publishing_models.Publication.objects.filter(
                id=publication_id
            ).delete()
            MigrationExecutor(connection).migrate(latest)


class PublicationIdentityConcurrencyTests(
    PublicationIdentityGuardedTransactionTestCase
):
    def setUp(self):
        from tests.unit.test_publication_approval_db import (
            PublicationApprovalSQLiteTriggerTests,
        )

        PublicationApprovalSQLiteTriggerTests.setUpTestData()
        self.fixture = PublicationApprovalSQLiteTriggerTests.fixture
        self.fixture.intent.state = publishing_models.PublicationIntent.State.APPROVED
        self.fixture.intent.save(update_fields=("state",))

    def _intent_values(self, *, index):
        root = self.fixture.intent
        return {
            "article_id": root.article_id,
            "article_revision_id": root.article_revision_id,
            "revision_no": root.revision_no,
            "revision_content_hash": root.revision_content_hash,
            "target_snapshot_refs": root.target_snapshot_refs,
            "target_commands": root.target_commands,
            "target_snapshot_manifest_hash": root.target_snapshot_manifest_hash,
            "approval_mode": root.approval_mode,
            "input_evidence_manifest_hash": root.input_evidence_manifest_hash,
            "quality_gate_manifest_hash": root.quality_gate_manifest_hash,
            "quality_report_hash": root.quality_report_hash,
            "supersedes_intent_id": root.id,
            "intent_hash": root.intent_hash,
            "request_key": f"intent-t020-race-{index}",
            "request_hash": str(index) * 64,
            "request_hash_version": "publication-intent-request-v1",
            "created_by_id": self.fixture.user.id,
        }

    def test_two_competing_intent_creates_have_exactly_one_head_winner(self):
        barrier = threading.Barrier(2)
        result_lock = threading.Lock()
        results = []

        def create(index):
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                with transaction.atomic():
                    child = publishing_models.PublicationIntent.objects.create(
                        **self._intent_values(index=index)
                    )
                outcome = ("won", str(child.id))
            except (IntegrityError, OperationalError) as exc:
                outcome = ("lost", type(exc).__name__)
            finally:
                connections.close_all()
            with result_lock:
                results.append(outcome)

        threads = [threading.Thread(target=create, args=(index,)) for index in (3, 4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([row[0] for row in results].count("won"), 1)
        self.assertEqual([row[0] for row in results].count("lost"), 1)
        head = publishing_models.PublicationIntentHead.objects.get(
            article_id=self.fixture.intent.article_id
        )
        self.assertEqual(head.version, 2)
        self.assertEqual(
            publishing_models.PublicationIntent.objects.filter(
                article_id=self.fixture.intent.article_id
            ).count(),
            2,
        )

    def test_two_competing_dispatch_cohorts_have_exactly_one_winner(self):
        publication = publishing_models.Publication.objects.create(
            article_id=self.fixture.intent.article_id,
            target=self.fixture.target,
            origin_target_snapshot_id=self.fixture.snapshot.id,
            remote_lookup_key=f"t020-race-{_uuid()}",
        )
        barrier = threading.Barrier(2)
        result_lock = threading.Lock()
        results = []

        def dispatch(index):
            connections.close_all()
            correlation_id = _uuid()
            try:
                barrier.wait(timeout=5)
                with transaction.atomic():
                    attempt = publishing_models.PublicationAttempt.objects.create(
                        publication_id=publication.id,
                        article_revision_id=self.fixture.revision.id,
                        publication_intent_id=self.fixture.intent.id,
                        target_snapshot_id=self.fixture.snapshot.id,
                        target_config_hash=self.fixture.snapshot.config_hash,
                        resolved_action="create",
                        target_command_hash="7" * 64,
                        publisher_contract_version="publisher-v1",
                        publisher_adapter_manifest_hash="5" * 64,
                        approval_id=self.fixture.root.id,
                        approval_subject_hash=self.fixture.subject_hash,
                        idempotency_key=f"t020-dispatch-race-{index}",
                        remote_lookup_key=publication.remote_lookup_key,
                        request_fingerprint=str(index) * 64,
                        correlation_id=correlation_id,
                    )
                    publishing_models.PublicationIntent.objects.filter(
                        pk=self.fixture.intent.id
                    ).update(state=publishing_models.PublicationIntent.State.DISPATCHED)
                    publishing_models.PublicationDispatch.objects.create(
                        publication_intent_id=self.fixture.intent.id,
                        request_key=f"dispatch-race-{index}",
                        request_hash=str(index + 2) * 64,
                        request_hash_version="publication-dispatch-request-v1",
                        correlation_id=correlation_id,
                        attempt_count=1,
                        attempt_manifest_hash=str(index + 4) * 64,
                    )
                outcome = ("won", str(attempt.id))
            except (IntegrityError, OperationalError) as exc:
                outcome = ("lost", type(exc).__name__)
            finally:
                connections.close_all()
            with result_lock:
                results.append(outcome)

        threads = [threading.Thread(target=dispatch, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([row[0] for row in results].count("won"), 1)
        self.assertEqual([row[0] for row in results].count("lost"), 1)
        self.assertEqual(
            publishing_models.PublicationAttempt.objects.filter(
                publication_intent_id=self.fixture.intent.id
            ).count(),
            1,
        )
        self.assertEqual(
            publishing_models.PublicationDispatch.objects.filter(
                publication_intent_id=self.fixture.intent.id
            ).count(),
            1,
        )
