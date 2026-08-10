from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.publishing import automation, corrections, services
from apps.publishing.models import (
    ChannelCode,
    ChannelRole,
    PublicationAction,
    PublicationAttempt,
    TargetEnvironment,
)
from wisdome_writer.domain.errors import Conflict, InvalidInput


def _target(*, channel: str, role: str, environment: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        channel=channel,
        role=role,
        environment=environment,
    )


def _linked_attempts(*, blogger_action: str = PublicationAction.CREATE):
    intent_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    article_id = uuid.uuid4()
    wordpress_target = _target(
        channel=ChannelCode.WORDPRESS,
        role=ChannelRole.PRIMARY,
        environment=TargetEnvironment.PRODUCTION,
    )
    blogger_target = _target(
        channel=ChannelCode.BLOGGER,
        role=ChannelRole.SECONDARY,
        environment=TargetEnvironment.PRODUCTION,
    )
    wordpress_publication = SimpleNamespace(
        id=uuid.uuid4(),
        article_id=article_id,
        target=wordpress_target,
        target_id=wordpress_target.id,
        state="published",
        remote_state="published",
        remote_url="https://example.com/canonical-post/",
        canonical_ready_at=object(),
    )
    dependency = SimpleNamespace(
        id=uuid.uuid4(),
        publication=wordpress_publication,
        publication_id=wordpress_publication.id,
        publication_intent_id=intent_id,
        article_revision_id=revision_id,
        target_snapshot_id=uuid.uuid4(),
        target_config_hash="a" * 64,
        resolved_action=PublicationAction.CREATE,
        approval_subject_hash="b" * 64,
        request_fingerprint="c" * 64,
        state=PublicationAttempt.State.SUCCEEDED,
    )
    intent = SimpleNamespace(
        id=intent_id,
        target_commands=[
            {
                "targetId": str(wordpress_target.id),
                "targetSnapshotId": str(dependency.target_snapshot_id),
                "targetConfigHash": dependency.target_config_hash,
                "resolvedAction": dependency.resolved_action,
                "canonicalDependencyTargetId": None,
            },
            {
                "targetId": str(blogger_target.id),
                "targetSnapshotId": str(uuid.uuid4()),
                "targetConfigHash": "d" * 64,
                "resolvedAction": blogger_action,
                "canonicalDependencyTargetId": str(wordpress_target.id),
            },
        ],
    )
    dependency.publication_intent = intent
    blogger_publication = SimpleNamespace(
        id=uuid.uuid4(),
        article_id=article_id,
        target=blogger_target,
        target_id=blogger_target.id,
    )
    blogger = SimpleNamespace(
        id=uuid.uuid4(),
        publication=blogger_publication,
        publication_id=blogger_publication.id,
        publication_intent=intent,
        publication_intent_id=intent_id,
        article_revision_id=revision_id,
        target_snapshot_id=uuid.UUID(intent.target_commands[1]["targetSnapshotId"]),
        target_config_hash=intent.target_commands[1]["targetConfigHash"],
        resolved_action=blogger_action,
        depends_on_attempt=dependency,
        depends_on_attempt_id=dependency.id,
    )
    blogger.dependency_subject_hash = services._publication_dependency_subject_hash(
        dependent_attempt=blogger,
        dependency_attempt=dependency,
    )
    return blogger, dependency


class PublicationDependencyModelTests(SimpleTestCase):
    def test_attempt_has_frozen_explicit_dependency_fields(self):
        self.assertIn("depends_on_attempt", {row.name for row in PublicationAttempt._meta.fields})
        self.assertIn("dependency_subject_hash", {row.name for row in PublicationAttempt._meta.fields})
        self.assertIn(
            "depends_on_attempt_id",
            PublicationAttempt.objects.all().FROZEN_IDENTITY_ATTNAMES,
        )
        self.assertIn(
            "dependency_subject_hash",
            PublicationAttempt.objects.all().FROZEN_IDENTITY_ATTNAMES,
        )


class PublicationDependencyMaterialTests(SimpleTestCase):
    def test_dispatch_binds_blogger_to_the_preceding_exact_wordpress_attempt(self):
        blogger, dependency = _linked_attempts()
        observed = services._dispatch_attempt_dependency(
            target=blogger.publication.target,
            command=blogger.publication_intent.target_commands[1],
            attempts_by_target={
                str(dependency.publication.target_id): dependency,
            },
        )
        self.assertIs(observed, dependency)

        with self.assertRaisesRegex(Conflict, "exact dispatch cohort"):
            services._dispatch_attempt_dependency(
                target=blogger.publication.target,
                command=blogger.publication_intent.target_commands[1],
                attempts_by_target={},
            )

    def test_ready_dependency_is_exact_linked_primary_wordpress_attempt(self):
        blogger, dependency = _linked_attempts()

        self.assertIs(
            services._require_wordpress_dependency(blogger, require_ready=True),
            dependency,
        )
        self.assertTrue(services._wordpress_dependency_ready(blogger))
        self.assertEqual(
            services._canonical_wordpress_url(blogger),
            dependency.publication.remote_url,
        )

    def test_dependency_rejects_cross_environment_and_changed_subject(self):
        blogger, dependency = _linked_attempts()
        dependency.publication.target.environment = TargetEnvironment.TEST

        with self.assertRaisesRegex(Conflict, "environment"):
            services._require_wordpress_dependency(blogger, require_ready=True)

        dependency.publication.target.environment = TargetEnvironment.PRODUCTION
        blogger.dependency_subject_hash = "f" * 64
        with self.assertRaisesRegex(Conflict, "subject"):
            services._require_wordpress_dependency(blogger, require_ready=True)

    def test_unpublish_waits_for_the_exact_wordpress_terminal_attempt(self):
        blogger, dependency = _linked_attempts(
            blogger_action=PublicationAction.UNPUBLISH,
        )
        dependency.resolved_action = PublicationAction.UNPUBLISH
        blogger.publication_intent.target_commands[0]["resolvedAction"] = (
            PublicationAction.UNPUBLISH
        )
        dependency.publication.state = "withdrawn"
        dependency.publication.remote_state = "withdrawn"
        blogger.dependency_subject_hash = services._publication_dependency_subject_hash(
            dependent_attempt=blogger,
            dependency_attempt=dependency,
        )

        self.assertTrue(services._wordpress_dependency_ready(blogger))

        dependency.state = PublicationAttempt.State.RUNNING
        self.assertFalse(services._wordpress_dependency_ready(blogger))

    def test_release_and_terminalization_select_only_explicit_children(self):
        blogger, dependency = _linked_attempts()
        query = MagicMock()
        query.values_list.return_value = []
        with patch.object(
            services.PublicationAttempt.objects,
            "filter",
            return_value=query,
        ) as attempt_filter:
            services._release_dependents_on_commit(dependency)

        attempt_filter.assert_called_once_with(
            depends_on_attempt=dependency,
            state=PublicationAttempt.State.QUEUED,
        )

    def test_dispatch_manifest_and_final_render_freeze_dependency_and_url(self):
        blogger, dependency = _linked_attempts()
        manifest = services._publication_attempt_manifest([blogger, dependency])
        blogger_row = next(
            row for row in manifest if row["attemptId"] == str(blogger.id)
        )
        self.assertEqual(blogger_row["dependsOnAttemptId"], str(dependency.id))
        self.assertEqual(
            blogger_row["dependencySubjectHash"],
            blogger.dependency_subject_hash,
        )

        approval_render = SimpleNamespace(
            article_revision_id=blogger.article_revision_id,
            target_snapshot_id=blogger.target_snapshot_id,
            target_snapshot=SimpleNamespace(id=blogger.target_snapshot_id),
            target_config_hash=blogger.target_config_hash,
            channel_role=ChannelRole.SECONDARY,
            title="보조 배포 글",
            body_html='<p><a href="{{CANONICAL_WORDPRESS_URL}}">원문</a></p>',
            labels=[],
            source_links=[],
            included_claim_ids=[],
            template_hash="1" * 64,
            source_manifest_hash="2" * 64,
            media_manifest=[],
            correction_history=[],
        )
        blogger.approval = SimpleNamespace(article_channel_render=approval_render)
        render_query = MagicMock()
        render_query.first.return_value = None
        created_render = object()
        with (
            patch.object(
                services.ArticleChannelRender.objects,
                "filter",
                return_value=render_query,
            ),
            patch.object(
                services.ArticleChannelRender.objects,
                "create",
                return_value=created_render,
            ) as create_render,
        ):
            self.assertIs(services._final_render(blogger), created_render)

        create_kwargs = create_render.call_args.kwargs
        self.assertEqual(
            create_kwargs["canonical_source_url"],
            dependency.publication.remote_url,
        )
        self.assertIn(
            dependency.publication.remote_url,
            create_kwargs["body_html"],
        )


class CanonicalDependencySelectionTests(SimpleTestCase):
    def test_automation_selects_the_single_primary_wordpress_in_same_environment(self):
        primary_prod = _target(
            channel=ChannelCode.WORDPRESS,
            role=ChannelRole.PRIMARY,
            environment=TargetEnvironment.PRODUCTION,
        )
        primary_test = _target(
            channel=ChannelCode.WORDPRESS,
            role=ChannelRole.PRIMARY,
            environment=TargetEnvironment.TEST,
        )
        blogger = _target(
            channel=ChannelCode.BLOGGER,
            role=ChannelRole.SECONDARY,
            environment=TargetEnvironment.PRODUCTION,
        )

        self.assertEqual(
            automation._canonical_wordpress_target_id(
                blogger,
                {
                    str(primary_test.id): primary_test,
                    str(primary_prod.id): primary_prod,
                    str(blogger.id): blogger,
                },
            ),
            str(primary_prod.id),
        )

    def test_correction_rejects_blogger_without_same_environment_primary(self):
        wordpress = SimpleNamespace(
            target=_target(
                channel=ChannelCode.WORDPRESS,
                role=ChannelRole.PRIMARY,
                environment=TargetEnvironment.TEST,
            )
        )
        blogger = SimpleNamespace(
            target=_target(
                channel=ChannelCode.BLOGGER,
                role=ChannelRole.SECONDARY,
                environment=TargetEnvironment.PRODUCTION,
            )
        )

        with self.assertRaisesRegex(
            corrections.CorrectionWorkflowError,
            "same-environment primary WordPress",
        ):
            corrections._canonical_wordpress_target_id(blogger, [wordpress, blogger])

    def test_intent_rejects_blogger_dependency_outside_exact_target_set(self):
        blogger = _target(
            channel=ChannelCode.BLOGGER,
            role=ChannelRole.SECONDARY,
            environment=TargetEnvironment.PRODUCTION,
        )
        command = {
            "targetId": str(blogger.id),
            "resolvedAction": PublicationAction.CREATE,
            "canonicalDependencyTargetId": str(uuid.uuid4()),
        }

        with self.assertRaises(InvalidInput):
            services._validate_canonical_dependency_commands(
                target_rows={str(blogger.id): blogger},
                commands={str(blogger.id): command},
            )
