import importlib
import threading
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest import TestCase as UnitTestCase

from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.db.models import PROTECT
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.publishing.models import (
    Approval,
    ArticleChannelRender,
    PublicationApprovalHead,
    PublicationIntent,
    PublicationTarget,
    PublicationTargetSnapshot,
)


class PublicationApprovalModelContractTests(UnitTestCase):
    def test_approval_model_keeps_decision_lineage_as_immutable_relations(self):
        supersedes = Approval._meta.get_field("supersedes_approval")

        self.assertIs(supersedes.remote_field.on_delete, PROTECT)
        self.assertEqual(supersedes.db_column, "supersedes_approval_id")
        self.assertFalse(Approval._meta.get_field("decision_hash").null)
        self.assertFalse(Approval._meta.get_field("decision_reason").null)
        self.assertFalse(Approval._meta.get_field("decision_actor_type").null)
        self.assertTrue(Approval._meta.get_field("decision_actor_id").null)
        self.assertTrue(Approval._meta.get_field("decision_event_key").null)
        self.assertFalse(Approval._meta.get_field("admin").null)
        self.assertFalse(Approval._meta.get_field("request_hash").null)
        self.assertFalse(PublicationApprovalHead._meta.get_field("subject_hash").null)
        self.assertIn(
            "ck_approval_decision_actor_provenance",
            {constraint.name for constraint in Approval._meta.constraints},
        )
        self.assertTrue(
            {
                "ck_approval_v3_decision_material",
                "ck_approval_mode_actor_decision",
            }
            <= {constraint.name for constraint in Approval._meta.constraints}
        )

    def test_v3_subject_binds_both_intent_and_input_evidence_hashes(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0009_approval_decision_integrity"
        )
        approval = SimpleNamespace(
            target_id=uuid.uuid4(),
            target_action="unpublish",
            action_subject={"kind": "unpublish_command"},
        )
        revision = SimpleNamespace(
            id=uuid.uuid4(),
            revision_no=1,
            content_hash="a" * 64,
            editorial_policy_hash="b" * 64,
        )
        command = {
            "targetSnapshotId": uuid.uuid4(),
            "targetConfigHash": "c" * 64,
            "targetCommandHash": "d" * 64,
        }
        intent = SimpleNamespace(
            id=uuid.uuid4(),
            intent_hash="e" * 64,
            input_evidence_manifest_hash="f" * 64,
            quality_gate_manifest_hash="1" * 64,
            quality_report_hash="2" * 64,
        )

        baseline = migration._stable_subject_hash(
            approval, intent, revision, command, None
        )
        intent.input_evidence_manifest_hash = "3" * 64

        self.assertNotEqual(
            baseline,
            migration._stable_subject_hash(
                approval, intent, revision, command, None
            ),
        )

    def test_legacy_content_render_requires_immutable_media_proof(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0009_approval_decision_integrity"
        )
        revision = SimpleNamespace(id=uuid.uuid4())
        intent = SimpleNamespace(
            id=uuid.uuid4(),
            input_evidence_manifest_hash="a" * 64,
        )
        target_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        title = "legacy title"
        body = "<p>legacy body</p>"
        source_links = ["https://example.com/source"]
        render = SimpleNamespace(
            id=uuid.uuid4(),
            publication_intent_id=intent.id,
            article_revision_id=revision.id,
            target_id=target_id,
            target_snapshot_id=snapshot_id,
            target_config_hash="b" * 64,
            render_stage="preview",
            title=title,
            body_html=body,
            source_links=source_links,
            media_manifest=[{"assetId": str(uuid.uuid4())}],
            target=SimpleNamespace(channel="wordpress"),
        )
        render.content_hash = migration._hash({"title": title, "body": body})
        render.template_hash = migration._hash(
            {
                "channel": "wordpress",
                "title": title,
                "body": body,
                "revision": str(revision.id),
            }
        )
        render.source_manifest_hash = migration._hash(
            {
                "inputEvidenceManifestHash": intent.input_evidence_manifest_hash,
                "sourceLinks": source_links,
            }
        )
        approval = SimpleNamespace(
            target_id=target_id,
            target_action="create",
            article_channel_render_id=render.id,
            target_snapshot_id=snapshot_id,
            target_config_hash=render.target_config_hash,
            render_template_hash=render.template_hash,
            source_manifest_hash=render.source_manifest_hash,
            action_subject={
                "kind": "content_preview",
                "action": "create",
                "renderId": str(render.id),
                "targetId": str(target_id),
                "targetSnapshotId": str(snapshot_id),
                "targetConfigHash": render.target_config_hash,
                "templateHash": render.template_hash,
                "sourceManifestHash": render.source_manifest_hash,
            },
        )

        class RenderManager:
            def using(self, _alias):
                return self

            def get(self, **_kwargs):
                return render

        Render = SimpleNamespace(
            objects=RenderManager(),
            DoesNotExist=type("DoesNotExist", (Exception,), {}),
        )

        with self.assertRaisesRegex(RuntimeError, "immutably proven"):
            migration._require_render_exact(
                approval,
                intent,
                revision,
                {
                    "targetSnapshotId": str(snapshot_id),
                    "targetConfigHash": render.target_config_hash,
                },
                Render,
                "default",
            )

    def test_0009_postgresql_guards_cover_insert_lineage_head_identity_and_render_freeze(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0009_approval_decision_integrity"
        )
        statements = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql):
                statements.append(sql)

        editor = SimpleNamespace(
            connection=SimpleNamespace(vendor="postgresql", cursor=Cursor),
            quote_name=lambda value: value,
        )

        migration.install_t019_guards(None, editor)

        sql = "\n".join(statements)
        self.assertIn("Approval insert lineage is invalid", sql)
        self.assertIn(
            "NEW.publication_intent_id IS DISTINCT FROM OLD.publication_intent_id",
            sql,
        )
        self.assertIn("NEW.target_id IS DISTINCT FROM OLD.target_id", sql)
        self.assertIn("NEW.id IS DISTINCT FROM OLD.id", sql)
        self.assertIn("NEW.subject_hash = approval.approval_subject_hash", sql)
        self.assertIn("ArticleChannelRender approved material is append-only", sql)
        self.assertIn("OLD.render_stage = 'final'", sql)
        self.assertIn(
            "NEW.approval_material_version <> 'approval-subject-v3'",
            sql,
        )
        self.assertIn("current_decision = 'approved'", sql)
        self.assertIn("NEW.decision = 'revoked'", sql)
        self.assertIn(
            "NEW.supersedes_approval_id IS DISTINCT FROM current_head.latest_approval_id",
            sql,
        )
        self.assertIn("AFTER INSERT ON publishing_approval", sql)
        self.assertIn(
            "IF TG_OP = 'UPDATE' THEN\n                        RETURN NEW;",
            sql,
        )

    def test_0009_reverse_rejects_a_populated_approval_table_before_ddl(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0009_approval_decision_integrity"
        )

        class Rows:
            @staticmethod
            def using(_alias):
                return Rows()

            @staticmethod
            def exists():
                return True

        ApprovalModel = SimpleNamespace(objects=Rows())
        apps_registry = SimpleNamespace(
            get_model=lambda *_args: ApprovalModel,
        )
        editor = SimpleNamespace(
            connection=SimpleNamespace(alias="default"),
        )

        with self.assertRaises(IrreversibleError):
            migration.reject_populated_reverse(apps_registry, editor)


class PublicationApprovalSQLiteTriggerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.collection.models import CollectionRun
        from apps.editorial.models import (
            ArticleRevision,
            DraftArticle,
            EditorialPolicySnapshot,
        )
        from apps.topics.models import SourceRegistrySnapshot, TopicPolicy

        sha = "a" * 64
        user = get_user_model().objects.create_user(
            email="t019-db@example.com",
            password="not-a-real-secret",
            is_staff=True,
        )
        policy = TopicPolicy.objects.create(
            code="housing_subscription",
            version=1,
            title="T019 policy",
            freshness_minutes=60,
            policy={},
            policy_hash=sha,
        )
        registry = SourceRegistrySnapshot.objects.create(
            topic_code="housing_subscription",
            version=1,
            manifest_hash=sha,
        )
        now = timezone.now()
        run = CollectionRun.objects.create(
            display_id="RUN-T019-DB",
            topic_code="housing_subscription",
            window_start=now - timedelta(hours=1),
            window_end=now,
            source_registry=registry,
            registry_manifest_hash=sha,
            topic_policy=policy,
            policy_version=1,
            policy_hash=sha,
            freshness_minutes=60,
            allowed_authority_tiers=["primary_official"],
            freshness_cutoff=now - timedelta(hours=1),
            request_fingerprint="b" * 64,
        )
        policy_snapshot = EditorialPolicySnapshot(
            topic_code="housing_subscription",
            policy_key="t019-test",
            policy_version="1",
            document={},
            release_document_hash=sha,
            config_hash=sha,
            implementation_manifest={},
            implementation_manifest_hash=sha,
            material_hash=sha,
        )
        EditorialPolicySnapshot.objects.bulk_create([policy_snapshot])
        article = DraftArticle.objects.create(
            article_identity_key="t019-db-article",
            topic_code="housing_subscription",
            article_type="housing_notice",
            source_run=run,
        )
        revision = ArticleRevision.objects.create(
            article=article,
            origin_run=run,
            revision_no=1,
            editorial_policy_snapshot=policy_snapshot,
            editorial_policy_version="1",
            editorial_policy_hash="c" * 64,
            title="승인 본문",
            summary="승인 요약",
            body_markdown="승인 본문",
            content_hash="d" * 64,
            input_manifest_hash="e" * 64,
            claim_manifest_hash="f" * 64,
            quality_manifest_hash="1" * 64,
            quality_gate_manifest_hash="2" * 64,
            quality_report_hash="3" * 64,
        )
        target = PublicationTarget.objects.create(
            channel="wordpress",
            role="primary_canonical",
            environment="test",
            display_name="T019 target",
            base_url="https://example.com/",
        )
        snapshot = PublicationTargetSnapshot.objects.create(
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
        command = {
            "targetId": str(target.id),
            "targetSnapshotId": str(snapshot.id),
            "targetConfigHash": snapshot.config_hash,
            "resolvedAction": "create",
            "targetCommandHash": "7" * 64,
        }
        intent = PublicationIntent.objects.create(
            article_id=article.id,
            article_revision=revision,
            revision_no=1,
            revision_content_hash=revision.content_hash,
            target_snapshot_refs=[
                {
                    "targetId": str(target.id),
                    "targetSnapshotId": str(snapshot.id),
                    "targetConfigHash": snapshot.config_hash,
                }
            ],
            target_commands=[command],
            target_snapshot_manifest_hash="8" * 64,
            approval_mode="manual",
            input_evidence_manifest_hash="9" * 64,
            quality_gate_manifest_hash=revision.quality_gate_manifest_hash,
            quality_report_hash=revision.quality_report_hash,
            intent_hash="0" * 64,
            request_key="intent-t019-db",
            created_by=user,
        )
        render = ArticleChannelRender.objects.create(
            publication_intent=intent,
            article_revision=revision,
            target=target,
            target_snapshot=snapshot,
            target_config_hash=snapshot.config_hash,
            channel_role="primary_canonical",
            render_stage="preview",
            title="승인 본문",
            body_html="<p>승인 본문</p>",
            canonical_link_state="not_applicable",
            template_hash="a" * 64,
            content_hash="b" * 64,
            source_manifest_hash="c" * 64,
        )
        subject_hash = "d" * 64
        root = Approval.objects.create(
            article_revision=revision,
            revision_no=1,
            publication_intent=intent,
            target=target,
            target_action="create",
            article_channel_render=render,
            action_subject={
                "kind": "content_preview",
                "action": "create",
                "renderId": str(render.id),
                "targetId": str(target.id),
                "targetSnapshotId": str(snapshot.id),
                "targetConfigHash": snapshot.config_hash,
                "templateHash": render.template_hash,
                "sourceManifestHash": render.source_manifest_hash,
            },
            target_snapshot=snapshot,
            target_config_hash=snapshot.config_hash,
            mode="manual",
            decision="approved",
            approval_subject_hash=subject_hash,
            approval_material_version="approval-subject-v3",
            decision_hash="e" * 64,
            decision_reason="승인",
            decision_actor_type="admin",
            decision_actor_id=user.id,
            head_version=1,
            request_key="approval-t019-root",
            request_hash="f" * 64,
            policy_snapshot_hash=revision.editorial_policy_hash,
            quality_report_hash=revision.quality_report_hash,
            render_template_hash=render.template_hash,
            source_manifest_hash=render.source_manifest_hash,
            admin=user,
        )
        head = PublicationApprovalHead.objects.get(
            publication_intent=intent,
            target=target,
        )
        if (
            head.latest_approval_id != root.id
            or head.version != 1
            or head.subject_hash != subject_hash
        ):
            raise AssertionError("approval insert did not create its exact head")
        cls.fixture = SimpleNamespace(
            user=user,
            revision=revision,
            target=target,
            snapshot=snapshot,
            intent=intent,
            render=render,
            root=root,
            head=head,
            subject_hash=subject_hash,
        )

    def setUp(self):
        if connection.vendor != "sqlite":
            self.skipTest("SQLite trigger behavior is tested on SQLite")

    def _next_decision(self, **overrides):
        values = {
            "article_revision": self.fixture.revision,
            "revision_no": 1,
            "publication_intent": self.fixture.intent,
            "target": self.fixture.target,
            "target_action": "create",
            "article_channel_render": self.fixture.render,
            "action_subject": self.fixture.root.action_subject,
            "target_snapshot": self.fixture.snapshot,
            "target_config_hash": self.fixture.snapshot.config_hash,
            "mode": "manual",
            "decision": "revoked",
            "approval_subject_hash": self.fixture.subject_hash,
            "approval_material_version": "approval-subject-v3",
            "decision_hash": "7" * 64,
            "decision_reason": "immutable transition",
            "decision_actor_type": "admin",
            "decision_actor_id": self.fixture.user.id,
            "decision_event_key": None,
            "head_version": 2,
            "supersedes_approval": self.fixture.root,
            "request_key": "approval-t019-next",
            "request_hash": "8" * 64,
            "policy_snapshot_hash": self.fixture.revision.editorial_policy_hash,
            "quality_report_hash": self.fixture.revision.quality_report_hash,
            "render_template_hash": self.fixture.render.template_hash,
            "source_manifest_hash": self.fixture.render.source_manifest_hash,
            "admin": self.fixture.user,
        }
        values.update(overrides)
        return Approval.objects.create(**values)

    def test_head_identity_cannot_be_reparented(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PublicationApprovalHead.objects.filter(id=self.fixture.head.id).update(
                id=uuid.uuid4(),
                version=2,
            )

    def test_stale_competing_child_cannot_branch_from_the_same_head(self):
        child = Approval.objects.create(
            article_revision=self.fixture.revision,
            revision_no=1,
            publication_intent=self.fixture.intent,
            target=self.fixture.target,
            target_action="create",
            article_channel_render=self.fixture.render,
            action_subject=self.fixture.root.action_subject,
            target_snapshot=self.fixture.snapshot,
            target_config_hash=self.fixture.snapshot.config_hash,
            mode="manual",
            decision="revoked",
            approval_subject_hash=self.fixture.subject_hash,
            approval_material_version="approval-subject-v3",
            decision_hash="1" * 64,
            decision_reason="철회",
            decision_actor_type="admin",
            decision_actor_id=self.fixture.user.id,
            head_version=2,
            supersedes_approval=self.fixture.root,
            request_key="approval-t019-child-1",
            request_hash="2" * 64,
            policy_snapshot_hash=self.fixture.revision.editorial_policy_hash,
            quality_report_hash=self.fixture.revision.quality_report_hash,
            render_template_hash=self.fixture.render.template_hash,
            source_manifest_hash=self.fixture.render.source_manifest_hash,
            admin=self.fixture.user,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Approval.objects.create(
                article_revision=self.fixture.revision,
                revision_no=1,
                publication_intent=self.fixture.intent,
                target=self.fixture.target,
                target_action="create",
                article_channel_render=self.fixture.render,
                action_subject=self.fixture.root.action_subject,
                target_snapshot=self.fixture.snapshot,
                target_config_hash=self.fixture.snapshot.config_hash,
                mode="manual",
                decision="rejected",
                approval_subject_hash=self.fixture.subject_hash,
                approval_material_version="approval-subject-v3",
                decision_hash="3" * 64,
                decision_reason="경합 거절",
                decision_actor_type="admin",
                decision_actor_id=self.fixture.user.id,
                head_version=2,
                supersedes_approval=self.fixture.root,
                request_key="approval-t019-child-2",
                request_hash="4" * 64,
                policy_snapshot_hash=self.fixture.revision.editorial_policy_hash,
                quality_report_hash=self.fixture.revision.quality_report_hash,
                render_template_hash=self.fixture.render.template_hash,
                source_manifest_hash=self.fixture.render.source_manifest_hash,
                admin=self.fixture.user,
            )

        self.fixture.head.refresh_from_db()
        self.assertEqual(self.fixture.head.latest_approval_id, child.id)
        self.assertEqual(
            Approval.objects.filter(
                publication_intent=self.fixture.intent,
                target=self.fixture.target,
            ).count(),
            2,
        )

    def test_raw_child_insert_atomically_advances_the_current_head(self):
        approval_table = connection.ops.quote_name(Approval._meta.db_table)
        child_id = uuid.uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {approval_table} (
                    id,
                    article_revision_id,
                    revision_no,
                    publication_intent_id,
                    target_id,
                    target_action,
                    article_channel_render_id,
                    action_subject,
                    target_snapshot_id,
                    target_config_hash,
                    mode,
                    decision,
                    approval_subject_hash,
                    approval_material_version,
                    decision_hash,
                    decision_reason,
                    decision_actor_type,
                    decision_actor_id,
                    decision_event_key,
                    head_version,
                    supersedes_approval_id,
                    request_key,
                    request_hash,
                    reauth_proof_id,
                    policy_snapshot_hash,
                    quality_report_hash,
                    render_template_hash,
                    source_manifest_hash,
                    admin_id,
                    decided_at
                )
                SELECT
                    %s,
                    article_revision_id,
                    revision_no,
                    publication_intent_id,
                    target_id,
                    target_action,
                    article_channel_render_id,
                    action_subject,
                    target_snapshot_id,
                    target_config_hash,
                    mode,
                    'revoked',
                    approval_subject_hash,
                    approval_material_version,
                    %s,
                    %s,
                    decision_actor_type,
                    decision_actor_id,
                    decision_event_key,
                    2,
                    id,
                    %s,
                    %s,
                    reauth_proof_id,
                    policy_snapshot_hash,
                    quality_report_hash,
                    render_template_hash,
                    source_manifest_hash,
                    admin_id,
                    decided_at
                FROM {approval_table}
                WHERE id = %s
                """,
                [
                    child_id.hex,
                    "9" * 64,
                    "raw child revoke",
                    "approval-t019-raw-child",
                    "a" * 64,
                    self.fixture.root.id.hex,
                ],
            )

        self.fixture.head.refresh_from_db()
        self.assertEqual(self.fixture.head.latest_approval_id, child_id)
        self.assertEqual(self.fixture.head.version, 2)
        self.assertEqual(self.fixture.head.subject_hash, self.fixture.subject_hash)
        self.assertEqual(
            Approval.objects.filter(
                publication_intent=self.fixture.intent,
                target=self.fixture.target,
                head_version=2,
            ).count(),
            1,
        )

    def test_approved_render_material_cannot_be_updated_with_raw_sql(self):
        table = connection.ops.quote_name(ArticleChannelRender._meta.db_table)
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {table} SET body_html = %s WHERE id = %s",
                    ["<p>변조</p>", self.fixture.render.id.hex],
                )

    def test_approved_render_material_cannot_be_updated_through_the_orm(self):
        with self.assertRaisesRegex(TypeError, "append-only"):
            ArticleChannelRender.objects.filter(id=self.fixture.render.id).update(
                body_html="<p>변조</p>"
            )


    def test_approved_render_instance_save_and_delete_are_rejected(self):
        self.fixture.render.body_html = "<p>mutated</p>"
        with self.assertRaisesRegex(TypeError, "append-only"):
            self.fixture.render.save(update_fields=("body_html",))
        with self.assertRaisesRegex(TypeError, "append-only"):
            self.fixture.render.delete()

    def test_final_render_is_append_only_even_without_an_approval_fk(self):
        final_render = ArticleChannelRender.objects.create(
            publication_intent=self.fixture.intent,
            article_revision=self.fixture.revision,
            target=self.fixture.target,
            target_snapshot=self.fixture.snapshot,
            target_config_hash=self.fixture.snapshot.config_hash,
            channel_role="primary_canonical",
            render_stage="final",
            title="final title",
            body_html="<p>final body</p>",
            canonical_link_state="not_applicable",
            template_hash="9" * 64,
            content_hash="a" * 64,
            source_manifest_hash="b" * 64,
        )

        with self.assertRaisesRegex(TypeError, "append-only"):
            ArticleChannelRender.objects.filter(id=final_render.id).update(
                body_html="<p>mutated final</p>"
            )
        final_render.body_html = "<p>instance mutated final</p>"
        with self.assertRaisesRegex(TypeError, "append-only"):
            final_render.save(update_fields=("body_html",))
        with self.assertRaisesRegex(TypeError, "append-only"):
            final_render.delete()
        table = connection.ops.quote_name(ArticleChannelRender._meta.db_table)
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {table} SET body_html = %s WHERE id = %s",
                    ["<p>raw mutated final</p>", final_render.id.hex],
                )

    def test_insert_guard_requires_v3_material_and_exact_transition(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._next_decision(
                approval_material_version="approval-subject-v2",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._next_decision(
                decision="rejected",
                decision_hash="c" * 64,
                request_key="approval-t019-invalid-transition",
                request_hash="d" * 64,
            )

    def test_insert_guard_requires_approval_mode_actor_pair(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._next_decision(
                decision_actor_type="worker",
                decision_actor_id=None,
                decision_event_key=str(uuid.uuid4()),
                decision_hash="6" * 64,
                request_key="approval-t019-invalid-actor-mode",
                request_hash="f" * 64,
            )


    def test_worker_decision_without_event_key_is_rejected_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Approval.objects.create(
                article_revision=self.fixture.revision,
                revision_no=1,
                publication_intent=self.fixture.intent,
                target=self.fixture.target,
                target_action="create",
                article_channel_render=self.fixture.render,
                action_subject=self.fixture.root.action_subject,
                target_snapshot=self.fixture.snapshot,
                target_config_hash=self.fixture.snapshot.config_hash,
                mode="manual",
                decision="revoked",
                approval_subject_hash=self.fixture.subject_hash,
                approval_material_version="approval-subject-v3",
                decision_hash="5" * 64,
                decision_reason="worker rejection",
                decision_actor_type="worker",
                decision_actor_id=None,
                decision_event_key=None,
                head_version=2,
                supersedes_approval=self.fixture.root,
                request_key="approval-t019-worker-missing-event",
                request_hash="6" * 64,
                policy_snapshot_hash=self.fixture.revision.editorial_policy_hash,
                quality_report_hash=self.fixture.revision.quality_report_hash,
                render_template_hash=self.fixture.render.template_hash,
                source_manifest_hash=self.fixture.render.source_manifest_hash,
                admin=self.fixture.user,
            )


class ApprovalGuardedTransactionTestCase(TransactionTestCase):
    reset_sequences = True

    def _fixture_teardown(self):
        editorial_migration = importlib.import_module(
            "apps.editorial.migrations.0003_editorial_policy_runtime"
        )
        audit_migration = importlib.import_module(
            "apps.audit.migrations.0002_auditevent_append_only"
        )
        publishing_0008 = importlib.import_module(
            "apps.publishing.migrations.0008_approval_head_and_target_intent_fence"
        )
        try:
            publishing_0009 = importlib.import_module(
                "apps.publishing.migrations.0009_approval_decision_integrity"
            )
        except ModuleNotFoundError:
            publishing_0009 = None
        with connection.schema_editor() as editor:
            editorial_migration.remove_editorial_policy_guards(None, editor)
            audit_migration.remove_append_only_guards(None, editor)
            if publishing_0009 is not None:
                publishing_0009.remove_t019_guards(None, editor)
            else:
                publishing_0008.remove_approval_guards(None, editor)
        try:
            super()._fixture_teardown()
        finally:
            with connection.schema_editor() as editor:
                editorial_migration.install_editorial_policy_guards(None, editor)
                audit_migration.install_append_only_guards(None, editor)
                if publishing_0009 is not None:
                    publishing_0009.install_t019_guards(None, editor)
                else:
                    publishing_0008.install_approval_guards(None, editor)


class PublicationApprovalConcurrentCASTests(
    ApprovalGuardedTransactionTestCase
):
    def setUp(self):
        PublicationApprovalSQLiteTriggerTests.setUpTestData()
        self.fixture = PublicationApprovalSQLiteTriggerTests.fixture

    def test_two_competing_decisions_have_exactly_one_database_winner(self):
        barrier = threading.Barrier(2)
        result_lock = threading.Lock()
        results = []

        def decide(index):
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                with transaction.atomic():
                    child = Approval.objects.create(
                        article_revision_id=self.fixture.revision.id,
                        revision_no=1,
                        publication_intent_id=self.fixture.intent.id,
                        target_id=self.fixture.target.id,
                        target_action="create",
                        article_channel_render_id=self.fixture.render.id,
                        action_subject=self.fixture.root.action_subject,
                        target_snapshot_id=self.fixture.snapshot.id,
                        target_config_hash=self.fixture.snapshot.config_hash,
                        mode="manual",
                        decision="revoked",
                        approval_subject_hash=self.fixture.subject_hash,
                        approval_material_version="approval-subject-v3",
                        decision_hash=("1" if index == 1 else "3") * 64,
                        decision_reason=f"경합 {index}",
                        decision_actor_type="admin",
                        decision_actor_id=self.fixture.user.id,
                        head_version=2,
                        supersedes_approval_id=self.fixture.root.id,
                        request_key=f"approval-t019-race-{index}",
                        request_hash=("2" if index == 1 else "4") * 64,
                        policy_snapshot_hash=(
                            self.fixture.revision.editorial_policy_hash
                        ),
                        quality_report_hash=(
                            self.fixture.revision.quality_report_hash
                        ),
                        render_template_hash=self.fixture.render.template_hash,
                        source_manifest_hash=(
                            self.fixture.render.source_manifest_hash
                        ),
                        admin_id=self.fixture.user.id,
                    )
                outcome = ("won", str(child.id))
            except (IntegrityError, OperationalError) as exc:
                outcome = ("lost", type(exc).__name__)
            finally:
                connections.close_all()
            with result_lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=decide, args=(index,))
            for index in (1, 2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([result[0] for result in results].count("won"), 1)
        self.assertEqual([result[0] for result in results].count("lost"), 1)
        self.assertEqual(
            Approval.objects.filter(
                publication_intent_id=self.fixture.intent.id,
                target_id=self.fixture.target.id,
            ).count(),
            2,
        )
        head = PublicationApprovalHead.objects.get(id=self.fixture.head.id)
        self.assertEqual(head.version, 2)
        self.assertNotEqual(head.latest_approval_id, self.fixture.root.id)


class PublicationApprovalMigrationExecutorTests(
    ApprovalGuardedTransactionTestCase
):

    def test_0009_has_an_actual_sqlite_forward_and_reverse_path(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        before = [
            ("publishing", "0008_approval_head_and_target_intent_fence"),
            ("editorial", "0003_editorial_policy_runtime"),
            ("audit", "0002_auditevent_append_only"),
            ("accounts", "0002_adminaccount_reauthentication_throttle"),
        ]
        under_test = [("publishing", "0009_approval_decision_integrity")]

        try:
            MigrationExecutor(connection).migrate(before)
            MigrationExecutor(connection).migrate(under_test)
            state = MigrationExecutor(connection).loader.project_state(under_test)
            approval = state.apps.get_model("publishing", "Approval")
            head = state.apps.get_model("publishing", "PublicationApprovalHead")
            self.assertFalse(approval._meta.get_field("decision_hash").null)
            self.assertIs(
                approval._meta.get_field("supersedes_approval").remote_field.on_delete,
                PROTECT,
            )
            self.assertFalse(head._meta.get_field("subject_hash").null)

            MigrationExecutor(connection).migrate(before)
            state = MigrationExecutor(connection).loader.project_state(before)
            approval = state.apps.get_model("publishing", "Approval")
            with self.assertRaises(Exception):
                approval._meta.get_field("decision_hash")
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_0009_populated_reverse_is_explicitly_irreversible_before_ddl(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        before = [
            ("publishing", "0008_approval_head_and_target_intent_fence"),
            ("editorial", "0003_editorial_policy_runtime"),
            ("audit", "0002_auditevent_append_only"),
            ("accounts", "0002_adminaccount_reauthentication_throttle"),
        ]
        under_test = [("publishing", "0009_approval_decision_integrity")]
        PublicationApprovalSQLiteTriggerTests.setUpTestData()
        approval_id = PublicationApprovalSQLiteTriggerTests.fixture.root.id

        try:
            with self.assertRaises(IrreversibleError):
                MigrationExecutor(connection).migrate(before)
            self.assertTrue(Approval.objects.filter(id=approval_id).exists())

            MigrationExecutor(connection).migrate(under_test)
            self.assertTrue(Approval.objects.filter(id=approval_id).exists())
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_0009_aborts_instead_of_admitting_a_corrupt_legacy_approval(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0009_approval_decision_integrity"
        )
        publishing_0008 = importlib.import_module(
            "apps.publishing.migrations.0008_approval_head_and_target_intent_fence"
        )
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        before = [
            ("publishing", "0008_approval_head_and_target_intent_fence"),
            ("editorial", "0003_editorial_policy_runtime"),
            ("audit", "0002_auditevent_append_only"),
            ("accounts", "0002_adminaccount_reauthentication_throttle"),
        ]
        under_test = [("publishing", "0009_approval_decision_integrity")]
        old_apps = None

        try:
            MigrationExecutor(connection).migrate(before)
            executor = MigrationExecutor(connection)
            old_apps = executor.loader.project_state(before).apps
            User = old_apps.get_model("accounts", "AdminAccount")
            TopicPolicy = old_apps.get_model("topics", "TopicPolicy")
            Registry = old_apps.get_model("topics", "SourceRegistrySnapshot")
            Run = old_apps.get_model("collection", "CollectionRun")
            Policy = old_apps.get_model("editorial", "EditorialPolicySnapshot")
            Article = old_apps.get_model("editorial", "DraftArticle")
            Revision = old_apps.get_model("editorial", "ArticleRevision")
            Target = old_apps.get_model("publishing", "PublicationTarget")
            Snapshot = old_apps.get_model("publishing", "PublicationTargetSnapshot")
            Intent = old_apps.get_model("publishing", "PublicationIntent")
            LegacyApproval = old_apps.get_model("publishing", "Approval")
            LegacyHead = old_apps.get_model(
                "publishing", "PublicationApprovalHead"
            )
            AuditEvent = old_apps.get_model("audit", "AuditEvent")

            sha = "a" * 64
            user = User.objects.create(
                email="t019-legacy@example.com",
                password="unusable",
                is_active=True,
                is_staff=True,
            )
            topic_policy = TopicPolicy.objects.create(
                code="housing_subscription",
                version=1,
                title="T019 legacy policy",
                freshness_minutes=60,
                policy={},
                policy_hash=sha,
            )
            registry = Registry.objects.create(
                topic_code="housing_subscription",
                version=1,
                manifest_hash=sha,
            )
            now = timezone.now()
            run = Run.objects.create(
                display_id="RUN-T019-LEGACY",
                topic_code="housing_subscription",
                window_start=now - timedelta(hours=1),
                window_end=now,
                source_registry=registry,
                registry_manifest_hash=sha,
                topic_policy=topic_policy,
                policy_version=1,
                policy_hash=sha,
                freshness_minutes=60,
                allowed_authority_tiers=["primary_official"],
                freshness_cutoff=now - timedelta(hours=1),
                request_fingerprint="b" * 64,
            )
            policy = Policy.objects.create(
                topic_code="housing_subscription",
                policy_key="t019-legacy",
                policy_version="1",
                document={},
                release_document_hash=sha,
                config_hash=sha,
                implementation_manifest={},
                implementation_manifest_hash=sha,
                material_hash=sha,
            )
            article = Article.objects.create(
                article_identity_key="t019-legacy-article",
                topic_code="housing_subscription",
                article_type="housing_notice",
                source_run=run,
            )
            revision = Revision.objects.create(
                article=article,
                origin_run=run,
                revision_no=1,
                editorial_policy_snapshot=policy,
                editorial_policy_version="1",
                editorial_policy_hash="c" * 64,
                title="legacy",
                summary="legacy",
                body_markdown="legacy",
                content_hash="d" * 64,
                input_manifest_hash="e" * 64,
                claim_manifest_hash="f" * 64,
                quality_manifest_hash="1" * 64,
                quality_gate_manifest_hash="2" * 64,
                quality_report_hash="3" * 64,
            )
            target = Target.objects.create(
                channel="wordpress",
                role="primary_canonical",
                environment="test",
                display_name="legacy target",
                base_url="https://legacy.example/",
            )
            snapshot = Snapshot.objects.create(
                target=target,
                version=1,
                channel="wordpress",
                role="primary_canonical",
                environment="test",
                base_url="https://legacy.example/",
                credential_ref_identity_hash="4" * 64,
                connection_state="verified",
                preflight_state="passed",
                canary_state="passed",
                pilot_state="passed",
                publisher_contract_version="publisher-v1",
                publisher_adapter_manifest_hash="5" * 64,
                config_hash="6" * 64,
            )
            command = {
                "targetId": str(target.id),
                "targetSnapshotId": str(snapshot.id),
                "targetConfigHash": snapshot.config_hash,
                "resolvedAction": "unpublish",
                "targetCommandHash": "7" * 64,
            }
            target_ref = {
                "targetId": str(target.id),
                "targetSnapshotId": str(snapshot.id),
                "targetConfigHash": snapshot.config_hash,
            }
            intent = Intent.objects.create(
                article_id=article.id,
                article_revision=revision,
                revision_no=1,
                revision_content_hash=revision.content_hash,
                target_snapshot_refs=[target_ref],
                target_commands=[command],
                target_snapshot_manifest_hash="8" * 64,
                approval_mode="manual",
                input_evidence_manifest_hash="9" * 64,
                quality_gate_manifest_hash=revision.quality_gate_manifest_hash,
                quality_report_hash=revision.quality_report_hash,
                intent_hash="0" * 64,
                request_key="intent-t019-legacy",
                created_by=user,
            )
            intent.intent_hash = migration._intent_hash(
                intent,
                revision,
                {str(target.id): target_ref},
                {str(target.id): command},
            )
            Intent.objects.filter(id=intent.id).update(
                intent_hash=intent.intent_hash
            )
            approval = LegacyApproval.objects.create(
                article_revision=revision,
                revision_no=1,
                publication_intent=intent,
                target=target,
                target_action="unpublish",
                action_subject={
                    "kind": "unpublish_command",
                    "action": "unpublish",
                    "targetId": str(target.id),
                    "targetSnapshotId": str(snapshot.id),
                    "targetConfigHash": snapshot.config_hash,
                    "remotePostId": "remote-1",
                    "observedRemoteState": "published",
                    "reason": "legacy correction",
                    "affectedTargetIds": [str(target.id)],
                    "correctionEvidenceManifestHash": "9" * 64,
                },
                target_snapshot=snapshot,
                target_config_hash=snapshot.config_hash,
                mode="manual",
                decision="approved",
                approval_subject_hash="0" * 64,
                approval_material_version="approval-subject-v1",
                head_version=1,
                request_key="approval-t019-legacy",
                request_hash="1" * 64,
                reauth_proof_id=uuid.uuid4(),
                policy_snapshot_hash=intent.quality_gate_manifest_hash,
                quality_report_hash=intent.quality_report_hash,
                source_manifest_hash="9" * 64,
                admin=user,
            )
            LegacyHead.objects.create(
                publication_intent=intent,
                target=target,
                latest_approval=approval,
                version=1,
            )

            with self.assertRaisesRegex(
                RuntimeError, "invalid legacy subject hash"
            ):
                MigrationExecutor(connection).migrate(under_test)

            with connection.schema_editor() as editor:
                publishing_0008.remove_approval_guards(None, editor)
            LegacyHead.objects.all().delete()
            LegacyApproval.objects.all().delete()

            approval = LegacyApproval.objects.create(
                article_revision=revision,
                revision_no=1,
                publication_intent=intent,
                target=target,
                target_action="unpublish",
                action_subject={
                    "kind": "unpublish_command",
                    "action": "unpublish",
                    "targetId": str(target.id),
                    "targetSnapshotId": str(snapshot.id),
                    "targetConfigHash": snapshot.config_hash,
                    "remotePostId": "remote-1",
                    "observedRemoteState": "published",
                    "reason": "legacy correction",
                    "affectedTargetIds": [str(target.id)],
                    "correctionEvidenceManifestHash": "9" * 64,
                },
                target_snapshot=snapshot,
                target_config_hash=snapshot.config_hash,
                mode="manual",
                decision="approved",
                approval_subject_hash="0" * 64,
                approval_material_version="approval-subject-v1",
                head_version=1,
                request_key="approval-t019-legacy-valid",
                request_hash="1" * 64,
                reauth_proof_id=uuid.uuid4(),
                policy_snapshot_hash=intent.quality_gate_manifest_hash,
                quality_report_hash=intent.quality_report_hash,
                source_manifest_hash="9" * 64,
                admin=user,
            )
            legacy_hash = migration._legacy_v1_hash(
                approval, intent, command
            )
            LegacyApproval.objects.filter(id=approval.id).update(
                approval_subject_hash=legacy_hash
            )
            approval.approval_subject_hash = legacy_hash
            approval_v2 = LegacyApproval.objects.create(
                article_revision=revision,
                revision_no=1,
                publication_intent=intent,
                target=target,
                target_action="unpublish",
                action_subject=approval.action_subject,
                target_snapshot=snapshot,
                target_config_hash=snapshot.config_hash,
                mode="manual",
                decision="revoked",
                approval_subject_hash="0" * 64,
                approval_material_version="approval-subject-v2",
                head_version=2,
                supersedes_approval_id=approval.id,
                request_key="approval-t019-legacy-valid-v2",
                request_hash="3" * 64,
                policy_snapshot_hash=intent.quality_gate_manifest_hash,
                quality_report_hash=intent.quality_report_hash,
                source_manifest_hash="9" * 64,
                admin=user,
            )
            legacy_v2_hash = migration._legacy_v2_hash(
                approval_v2, intent, command
            )
            LegacyApproval.objects.filter(id=approval_v2.id).update(
                approval_subject_hash=legacy_v2_hash
            )
            approval_v2.approval_subject_hash = legacy_v2_hash
            LegacyHead.objects.create(
                publication_intent=intent,
                target=target,
                latest_approval=approval_v2,
                version=2,
            )
            AuditEvent.objects.create(
                id=uuid.uuid4(),
                correlation_id=uuid.uuid4(),
                actor_type="admin",
                actor=user,
                action="publication_approval.decided",
                entity_type="publishing.approval",
                entity_id=approval.id,
                reason_code="legacy approval proof",
                metadata_schema_version="1",
                redaction_policy_version="audit-v1",
                redaction_policy_hash="2" * 64,
                metadata_redacted={
                    "approval_hash": legacy_hash,
                    "decision": approval.decision,
                    "decision_id": str(approval.id),
                    "intent_id": str(intent.id),
                    "request_hash": approval.request_hash,
                    "target_id": str(target.id),
                },
            )
            AuditEvent.objects.create(
                id=uuid.uuid4(),
                correlation_id=uuid.uuid4(),
                actor_type="admin",
                actor=user,
                action="publication_approval.decided",
                entity_type="publishing.approval",
                entity_id=approval_v2.id,
                reason_code="legacy revocation proof",
                metadata_schema_version="1",
                redaction_policy_version="audit-v1",
                redaction_policy_hash="4" * 64,
                metadata_redacted={
                    "approval_hash": legacy_v2_hash,
                    "decision": approval_v2.decision,
                    "decision_id": str(approval_v2.id),
                    "intent_id": str(intent.id),
                    "request_hash": approval_v2.request_hash,
                    "target_id": str(target.id),
                },
            )
            MigrationExecutor(connection).migrate(under_test)
            migrated_apps = MigrationExecutor(connection).loader.project_state(
                under_test
            ).apps
            MigratedApproval = migrated_apps.get_model(
                "publishing", "Approval"
            )
            MigratedHead = migrated_apps.get_model(
                "publishing", "PublicationApprovalHead"
            )
            migrated = MigratedApproval.objects.get(id=approval.id)
            migrated_v2 = MigratedApproval.objects.get(id=approval_v2.id)
            migrated_head = MigratedHead.objects.get(
                latest_approval_id=approval_v2.id
            )
            self.assertEqual(
                migrated.approval_material_version,
                "approval-subject-v3",
            )
            self.assertEqual(migrated.decision_actor_type, "admin")
            self.assertEqual(migrated.decision_reason, "legacy approval proof")
            self.assertEqual(len(migrated.decision_hash), 64)
            self.assertEqual(
                migrated_v2.approval_material_version,
                "approval-subject-v3",
            )
            self.assertEqual(
                migrated_v2.decision_reason,
                "legacy revocation proof",
            )
            self.assertEqual(
                migrated_v2.approval_subject_hash,
                migrated.approval_subject_hash,
            )
            self.assertEqual(
                migrated_head.subject_hash,
                migrated_v2.approval_subject_hash,
            )
        finally:
            MigrationExecutor(connection).migrate(latest)
