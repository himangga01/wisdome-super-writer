from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from apps.publishing import automation
from wisdome_writer.domain.errors import Conflict


class FrozenScheduleDispatchTests(TestCase):
    def test_live_schedule_version_drift_is_rejected_before_material_use(self):
        dispatch = SimpleNamespace(
            schedule_version=4,
            schedule=SimpleNamespace(version=5),
        )

        with self.assertRaisesRegex(Conflict, "schedule version"):
            automation._require_frozen_schedule(dispatch)

    def test_disabled_schedule_is_rejected_even_when_version_matches(self):
        dispatch = SimpleNamespace(
            schedule_version=4,
            schedule=SimpleNamespace(version=4, enabled=False),
        )

        with self.assertRaisesRegex(Conflict, "disabled"):
            automation._require_frozen_schedule(dispatch)

    def test_completed_dispatch_replays_before_live_schedule_validation(self):
        attempts = [SimpleNamespace(id="attempt-1")]
        intent = SimpleNamespace(
            id="intent-1",
            state="dispatched",
            article_id="article-1",
            article_revision_id="revision-1",
            origin_collection_run_id="run-1",
            approval_mode="validated_auto",
            target_commands=[{"targetId": "target-1"}],
            attempts=SimpleNamespace(all=lambda: attempts),
        )
        revision = SimpleNamespace(id="revision-1", quality_state="passed")
        article = SimpleNamespace(
            id="article-1",
            current_revision=revision,
            current_revision_id=revision.id,
            state="publishing",
        )
        article_query = MagicMock()
        article_query.select_related.return_value.get.return_value = article
        run = SimpleNamespace(
            id="run-1",
            approval_mode="validated_auto",
            requested_target_ids=["target-1"],
            articles=SimpleNamespace(select_for_update=lambda: article_query),
            state="publishing",
            topic_code="housing_subscription",
        )
        run_query = MagicMock()
        run_query.get.return_value = run
        schedule_dispatch = SimpleNamespace(
            id="dispatch-1",
            schedule_version=4,
            schedule=SimpleNamespace(version=5, enabled=True),
        )
        dispatch_query = MagicMock()
        dispatch_query.select_related.return_value.get.return_value = schedule_dispatch
        intent_query = MagicMock()
        intent_query.filter.return_value.first.return_value = intent
        audit_context = SimpleNamespace(actor_type="worker", event_key="event-1")

        with (
            patch.object(automation, "_require_worker_event"),
            patch.object(
                automation.CollectionRun.objects,
                "select_for_update",
                return_value=run_query,
            ),
            patch.object(
                automation.ScheduleDispatch.objects,
                "select_for_update",
                return_value=dispatch_query,
            ),
            patch.object(
                automation.PublicationIntent.objects,
                "select_related",
                return_value=intent_query,
            ),
            patch.object(
                automation,
                "_require_frozen_schedule",
                side_effect=AssertionError(
                    "live schedule must not be read before exact replay"
                ),
            ),
            patch.object(automation, "require_audit_replay") as replay,
        ):
            observed = automation._dispatch_validated_schedule_run_atomic.__wrapped__(
                "run-1",
                audit_context=audit_context,
            )

        self.assertEqual(observed, attempts)
        replay.assert_called_once()

    def test_automation_locks_target_intent_fences_before_target_rows(self):
        order: list[str] = []
        target_query = MagicMock()
        target_query.filter.return_value.order_by.return_value = []

        with (
            patch.object(
                automation,
                "_lock_target_intent_fences",
                side_effect=lambda _ids: order.append("fence"),
            ),
            patch.object(
                automation.PublicationTarget.objects,
                "select_for_update",
                side_effect=lambda: (order.append("target") or target_query),
            ),
        ):
            observed = automation._lock_automation_targets(
                ["00000000-0000-0000-0000-000000000103"]
            )

        self.assertEqual(order, ["fence", "target"])
        self.assertEqual(observed, {})

    def test_schedule_delivery_exhaustion_has_a_terminal_run_callback(self):
        from wisdome_writer.infrastructure.event_routes import EVENT_ROUTES

        route = EVENT_ROUTES[("publication.scheduled_run_requested", 1)]
        self.assertEqual(
            route.terminal_task_name,
            "apps.publishing.tasks.finalize_scheduled_publication_delivery_failure",
        )
        self.assertEqual(route.terminal_argument_keys, ("run_id",))
        self.assertTrue(
            callable(
                getattr(
                    automation,
                    "finalize_scheduled_publication_delivery_failure",
                    None,
                )
            )
        )
