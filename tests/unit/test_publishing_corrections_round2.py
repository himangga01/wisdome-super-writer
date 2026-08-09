from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from apps.publishing import corrections


def _frozen_intent(*, intent_id: str, case_id: str, supersedes_id=None):
    target_id = "00000000-0000-4000-8000-000000000101"
    snapshot_id = "00000000-0000-4000-8000-000000000201"
    return SimpleNamespace(
        id=intent_id,
        article_id="00000000-0000-4000-8000-000000000301",
        article_revision_id="00000000-0000-4000-8000-000000000401",
        correction_case_id=case_id,
        state="dispatched",
        revision_no=2,
        revision_content_hash="a" * 64,
        target_snapshot_refs=[
            {
                "targetId": target_id,
                "targetSnapshotId": snapshot_id,
                "targetConfigHash": "b" * 64,
            }
        ],
        target_commands=[
            {
                "targetId": target_id,
                "targetSnapshotId": snapshot_id,
                "targetConfigHash": "b" * 64,
                "resolvedAction": "update",
                "canonicalDependencyTargetId": None,
            }
        ],
        approval_mode="manual",
        auto_publish_validation_refs=[],
        auto_publish_activation_refs=[],
        supersedes_intent_id=supersedes_id,
        request_key="correction-request-1",
    )


class CorrectionReplayAndLineageTests(TestCase):
    def test_exact_correction_replay_precedes_live_case_and_revision_gates(self):
        case_id = "00000000-0000-4000-8000-000000000501"
        existing = _frozen_intent(intent_id="00000000-0000-4000-8000-000000000601", case_id=case_id)
        case = SimpleNamespace(
            id=uuid.UUID(case_id),
            article_id=uuid.UUID(existing.article_id),
            state="completed",
            kind="correction",
        )
        case_query = MagicMock()
        case_query.get.return_value = case
        intent_query = MagicMock()
        intent_query.filter.return_value.first.return_value = existing
        context = SimpleNamespace(reason_code="Correct a verified article.")

        with (
            patch.object(
                corrections.CorrectionCase.objects,
                "select_for_update",
                return_value=case_query,
            ),
            patch.object(
                corrections.PublicationIntent.objects,
                "select_related",
                return_value=intent_query,
            ),
            patch.object(
                corrections,
                "build_correction_plan",
                side_effect=AssertionError("live correction material must not precede replay"),
            ),
            patch.object(
                corrections,
                "create_publication_intent",
                return_value=(existing, False),
            ) as replay,
        ):
            observed = corrections._prepare_verified_correction_atomic.__wrapped__(
                case_id,
                user=SimpleNamespace(pk="admin-1"),
                request_key=existing.request_key,
                audit_context=context,
            )

        self.assertIs(observed, existing)
        replay.assert_called_once()

    def test_completion_uses_the_unique_locked_correction_intent_leaf(self):
        case_id = "00000000-0000-4000-8000-000000000501"
        intent = _frozen_intent(intent_id="00000000-0000-4000-8000-000000000601", case_id=case_id)
        case = SimpleNamespace(
            id=uuid.UUID(case_id),
            article_id=uuid.UUID(intent.article_id),
            state="applying",
            completed_at=None,
            save=MagicMock(),
        )
        case_query = MagicMock()
        case_query.get.return_value = case
        intent_query = MagicMock()
        intent_query.filter.return_value.order_by.return_value = [intent]
        publication = SimpleNamespace(
            target_id=intent.target_commands[0]["targetId"],
            state="published",
        )
        global_head = SimpleNamespace(
            id="00000000-0000-4000-8000-000000000999",
            correction_case_id=None,
        )
        publication_query = MagicMock()
        publication_query.filter.return_value = [publication]

        with (
            patch.object(
                corrections.CorrectionCase.objects,
                "select_for_update",
                return_value=case_query,
            ),
            patch.object(
                corrections.PublicationIntent.objects,
                "select_for_update",
                return_value=intent_query,
            ),
            patch.object(
                corrections,
                "resolve_current_publication_intent",
                return_value=global_head,
            ),
            patch.object(
                corrections.Publication.objects,
                "select_for_update",
                return_value=publication_query,
            ),
        ):
            observed = corrections.complete_correction_if_terminal.__wrapped__(case_id)

        self.assertTrue(observed)
        self.assertEqual(case.state, "completed")

    def test_ambiguous_correction_intent_leaves_fail_closed(self):
        case_id = "00000000-0000-4000-8000-000000000501"
        first = _frozen_intent(intent_id="00000000-0000-4000-8000-000000000601", case_id=case_id)
        second = _frozen_intent(intent_id="00000000-0000-4000-8000-000000000602", case_id=case_id)
        case = SimpleNamespace(
            id=uuid.UUID(case_id),
            article_id=uuid.UUID(first.article_id),
            state="applying",
        )
        case_query = MagicMock()
        case_query.get.return_value = case
        intent_query = MagicMock()
        intent_query.filter.return_value.order_by.return_value = [first, second]

        with (
            patch.object(
                corrections.CorrectionCase.objects,
                "select_for_update",
                return_value=case_query,
            ),
            patch.object(
                corrections.PublicationIntent.objects,
                "select_for_update",
                return_value=intent_query,
            ),
            patch.object(
                corrections,
                "resolve_current_publication_intent",
                return_value=None,
            ),
        ):
            with self.assertRaisesRegex(
                corrections.CorrectionWorkflowError,
                "ambiguous",
            ):
                corrections.complete_correction_if_terminal.__wrapped__(case_id)
