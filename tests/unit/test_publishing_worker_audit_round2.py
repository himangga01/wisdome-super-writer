from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from apps.publishing import tasks


class PublishingWorkerAuditReasonTests(TestCase):
    def test_unrelated_worker_replay_preserves_legacy_reasonless_identity(self):
        context_value = SimpleNamespace(get=lambda: "00000000-0000-4000-8000-000000000001")
        generation_value = SimpleNamespace(get=lambda: 1)
        with (
            patch.object(tasks, "CURRENT_EVENT_ID", context_value),
            patch.object(tasks, "CURRENT_EVENT_CORRELATION_ID", context_value),
            patch.object(tasks, "CURRENT_EVENT_CONSUMER_NAME", SimpleNamespace(get=lambda: "consumer")),
            patch.object(tasks, "CURRENT_EVENT_CONSUMER_LEASE_TOKEN", context_value),
            patch.object(tasks, "CURRENT_EVENT_CONSUMER_LEASE_GENERATION", generation_value),
            patch.object(tasks.AuditContext, "for_worker", return_value=object()) as factory,
        ):
            tasks._worker_audit_context()

        self.assertIsNone(factory.call_args.kwargs["reason_code"])

    def test_schedule_worker_can_bind_the_new_reasoned_request_version(self):
        context_value = SimpleNamespace(get=lambda: "00000000-0000-4000-8000-000000000001")
        generation_value = SimpleNamespace(get=lambda: 1)
        with (
            patch.object(tasks, "CURRENT_EVENT_ID", context_value),
            patch.object(tasks, "CURRENT_EVENT_CORRELATION_ID", context_value),
            patch.object(tasks, "CURRENT_EVENT_CONSUMER_NAME", SimpleNamespace(get=lambda: "consumer")),
            patch.object(tasks, "CURRENT_EVENT_CONSUMER_LEASE_TOKEN", context_value),
            patch.object(tasks, "CURRENT_EVENT_CONSUMER_LEASE_GENERATION", generation_value),
            patch.object(tasks.AuditContext, "for_worker", return_value=object()) as factory,
        ):
            tasks._worker_audit_context(reason_code=tasks.SCHEDULE_PUBLICATION_AUDIT_REASON)

        self.assertEqual(
            factory.call_args.kwargs["reason_code"],
            tasks.SCHEDULE_PUBLICATION_AUDIT_REASON,
        )
