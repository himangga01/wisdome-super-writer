from __future__ import annotations

import os
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from django.test import SimpleTestCase
from django.utils import timezone

from apps.publishing import services, tasks
from apps.publishing.contracts import PublishResult
from apps.publishing.models import PUBLICATION_EXECUTION_IDENTITY_VERSION


class PublicationExecutionClaimDecisionTests(SimpleTestCase):
    def test_legacy_requested_binding_requires_exact_current_identity_envelope(self):
        attempt_id = uuid.uuid4()
        correlation_id = uuid.uuid4()
        event_id = uuid.uuid4()
        event = SimpleNamespace(
            id=event_id,
            event_version=1,
            topic="publication.requested",
            message_key=f"publication.requested:{attempt_id}:1",
            aggregate_type="publication_attempt",
            aggregate_id=attempt_id,
            job_id=attempt_id,
            correlation_id=correlation_id,
            payload={"publication_attempt_id": str(attempt_id)},
        )
        attempt = SimpleNamespace(
            id=attempt_id,
            attempt_no=1,
            execution_generation=1,
            execution_identity_version=PUBLICATION_EXECUTION_IDENTITY_VERSION,
            correlation_id=correlation_id,
        )
        observation = SimpleNamespace(
            source_event_id=event_id,
            identity_version=PUBLICATION_EXECUTION_IDENTITY_VERSION,
            execution_attempt_no=1,
        )

        self.assertEqual(
            services._legacy_requested_binding_action(
                attempt,
                source_event=event,
                observations=(observation,),
            ),
            "resume",
        )
        attempt.execution_identity_version = "legacy-unverifiable-v1"
        self.assertEqual(
            services._legacy_requested_binding_action(
                attempt,
                source_event=event,
                observations=(observation,),
            ),
            "quarantine",
        )

    def test_legacy_reconcile_only_binds_exact_virgin_generation_one(self):
        attempt_id = uuid.uuid4()
        correlation_id = uuid.uuid4()
        event = SimpleNamespace(
            id=uuid.uuid4(),
            event_version=1,
            topic="publication.reconcile_requested",
            message_key=f"publication.reconcile_requested:{attempt_id}:1",
            aggregate_type="publication_attempt",
            aggregate_id=attempt_id,
            job_id=attempt_id,
            correlation_id=correlation_id,
            payload={"publication_attempt_id": str(attempt_id)},
        )
        attempt = SimpleNamespace(
            id=attempt_id,
            reconcile_attempt_no=0,
            correlation_id=correlation_id,
        )

        self.assertEqual(
            services._legacy_reconcile_binding_action(
                attempt,
                source_event=event,
                generation=None,
            ),
            "bind",
        )
        event.message_key = f"legacy:{attempt_id}"
        self.assertEqual(
            services._legacy_reconcile_binding_action(
                attempt,
                source_event=event,
                generation=None,
            ),
            "quarantine",
        )
        generation = SimpleNamespace(
            source_event_id=event.id,
            generation=1,
            delivery_identity_version="legacy-unverifiable-v1",
        )
        self.assertEqual(
            services._legacy_reconcile_binding_action(
                attempt,
                source_event=event,
                generation=generation,
            ),
            "quarantine",
        )

    def test_terminal_callback_requires_exact_receipt_reservation(self):
        token = uuid.uuid4()
        context = SimpleNamespace(
            worker_lease_generation=4,
            worker_lease_token=token,
        )
        missing = SimpleNamespace(
            terminal_reserved_at=None,
            terminal_lease_generation=0,
            terminal_lease_token=None,
            terminal_lease_token_hash="",
            terminal_error_code="",
        )
        reserved = SimpleNamespace(
            terminal_reserved_at=timezone.now(),
            terminal_lease_generation=4,
            terminal_lease_token=token,
            terminal_lease_token_hash=services._lease_token_hash(token),
            terminal_error_code="delivery_exhausted",
        )

        self.assertFalse(
            services._terminal_reservation_matches(
                missing,
                audit_context=context,
                error_code="delivery_exhausted",
            )
        )
        self.assertTrue(
            services._terminal_reservation_matches(
                reserved,
                audit_context=context,
                error_code="delivery_exhausted",
            )
        )

    def test_direct_terminal_services_reject_calls_without_reservation(self):
        event = SimpleNamespace(
            id=uuid.uuid4(),
            event_version=2,
            payload={"execution_attempt_no": 1, "reconcile_attempt_no": 1},
        )
        context = SimpleNamespace(actor_type="worker", event_key=str(event.id))

        with (
            patch.object(services, "_require_worker_event", return_value=event),
            patch.object(
                services,
                "_require_terminal_reservation_locked",
                side_effect=services.Conflict("missing terminal reservation"),
            ) as require_reservation,
        ):
            with self.assertRaises(services.Conflict):
                services.finalize_publication_delivery_failure.__wrapped__(
                    str(uuid.uuid4()),
                    expected_execution_attempt_no=1,
                    error_code="delivery_exhausted",
                    audit_context=context,
                )
            with self.assertRaises(services.Conflict):
                services.finalize_reconcile_delivery_failure.__wrapped__(
                    str(uuid.uuid4()),
                    source_event_id=event.id,
                    expected_reconcile_attempt_no=1,
                    error_code="delivery_exhausted",
                    audit_context=context,
                )

        self.assertEqual(require_reservation.call_count, 2)

    def test_execution_audit_material_binds_domain_and_receipt_capability(self):
        token = uuid.uuid4()

        material = services._execution_capability_audit_metadata(
            domain_generation=3,
            consumer_lease_generation=8,
            consumer_lease_token=token,
        )

        self.assertEqual(material["execution_generation"], 3)
        self.assertEqual(material["consumer_lease_generation"], 8)
        self.assertEqual(
            material["consumer_capability_hash"],
            services._lease_token_hash(token),
        )
        self.assertNotIn("consumer_lease_token", material)

    def test_late_result_url_is_bounded_without_losing_the_factual_hash(self):
        invalid = "ftp://secret.example/" + ("x" * 3000)

        url, material_hash = services._bounded_remote_url_fact(invalid)

        self.assertIsNone(url)
        self.assertEqual(len(material_hash), 64)
        self.assertEqual(
            services._bounded_remote_url_fact("https://example.test/post"),
            ("https://example.test/post", ""),
        )

    def _running(self, *, expires_at, write_started_at=None):
        return SimpleNamespace(
            state="running",
            active_source_event_id=uuid.UUID(int=1),
            active_consumer_name="publication-execute",
            active_consumer_lease_generation=7,
            active_consumer_lease_token=uuid.UUID(int=2),
            active_lease_token_hash="a" * 64,
            active_lease_expires_at=expires_at,
            active_write_marker="c" * 64,
            active_write_started_at=write_started_at,
        )

    def test_same_active_delivery_is_duplicate_and_never_runs_a_second_command(self):
        now = timezone.now()
        action = services._publication_execution_claim_action(
            self._running(expires_at=now + timedelta(minutes=1)),
            source_event_id=uuid.UUID(int=1),
            consumer_name="publication-execute",
            consumer_lease_generation=7,
            consumer_lease_token=uuid.UUID(int=2),
            lease_token_hash="a" * 64,
            now=now,
        )

        self.assertEqual(action, "duplicate")

    def test_expired_pre_write_delivery_can_reclaim_same_business_attempt(self):
        now = timezone.now()
        action = services._publication_execution_claim_action(
            self._running(expires_at=now - timedelta(seconds=1)),
            source_event_id=uuid.UUID(int=1),
            consumer_name="publication-execute",
            consumer_lease_generation=8,
            consumer_lease_token=uuid.UUID(int=3),
            lease_token_hash="b" * 64,
            now=now,
        )

        self.assertEqual(action, "reclaim")

    def test_expired_post_write_delivery_never_reexecutes_and_requires_reconcile(self):
        now = timezone.now()
        action = services._publication_execution_claim_action(
            self._running(
                expires_at=now - timedelta(seconds=1),
                write_started_at=now - timedelta(seconds=2),
            ),
            source_event_id=uuid.UUID(int=1),
            consumer_name="publication-execute",
            consumer_lease_generation=8,
            consumer_lease_token=uuid.UUID(int=3),
            lease_token_hash="b" * 64,
            now=now,
        )

        self.assertEqual(action, "reconcile")

    def test_result_settlement_keeps_same_processing_capability_after_clock_expiry(self):
        token = uuid.uuid4()
        expired = SimpleNamespace(
            state="processing",
            lease_token=token,
            lease_generation=7,
            claimed_until=timezone.now() - timedelta(seconds=1),
        )

        self.assertTrue(
            services._receipt_capability_is_current(
                expired,
                lease_token=token,
                lease_generation=7,
            )
        )
        self.assertFalse(
            services._receipt_capability_is_current(
                expired,
                lease_token=uuid.uuid4(),
                lease_generation=8,
            )
        )

    def test_service_rejects_success_that_does_not_prove_the_action_state(self):
        create = SimpleNamespace(resolved_action="create")
        unpublish = SimpleNamespace(resolved_action="unpublish")

        with self.assertRaises(services.Conflict):
            services._validate_publish_result_projection(
                create,
                PublishResult(
                    status="succeeded",
                    remote_post_id="post-42",
                    remote_state="draft",
                ),
            )

        services._validate_publish_result_projection(
            unpublish,
            PublishResult(
                status="succeeded",
                remote_post_id="post-42",
                remote_state="draft",
            ),
        )

    def test_expired_reconcile_delivery_rebinds_the_same_domain_generation(self):
        now = timezone.now()
        event_id = uuid.uuid4()
        generation = SimpleNamespace(
            state="running",
            source_event_id=event_id,
            consumer_name="publication-reconcile",
            consumer_lease_generation=3,
            consumer_lease_token=uuid.uuid4(),
            lease_token_hash="a" * 64,
            lease_expires_at=now - timedelta(seconds=1),
        )

        action = services._publication_reconcile_claim_action(
            generation,
            source_event_id=event_id,
            consumer_name="publication-reconcile",
            consumer_lease_generation=4,
            consumer_lease_token=uuid.uuid4(),
            lease_token_hash="b" * 64,
            now=now,
        )

        self.assertEqual(action, "reclaim")

    def test_old_reconcile_receipt_cannot_project_after_same_generation_rebind(self):
        old_token = uuid.uuid4()
        current_token = uuid.uuid4()
        generation = SimpleNamespace(
            consumer_name="publication-reconcile",
            consumer_lease_generation=4,
            consumer_lease_token=current_token,
            lease_token_hash=services._lease_token_hash(current_token),
        )
        current_receipt = SimpleNamespace(
            state="processing",
            lease_generation=4,
            lease_token=current_token,
            claimed_until=timezone.now() - timedelta(seconds=1),
        )
        old_context = SimpleNamespace(
            worker_consumer_name="publication-reconcile",
            worker_lease_generation=3,
            worker_lease_token=old_token,
        )
        current_context = SimpleNamespace(
            worker_consumer_name="publication-reconcile",
            worker_lease_generation=4,
            worker_lease_token=current_token,
        )

        self.assertFalse(
            services._reconcile_result_capability_is_current(
                generation,
                current_receipt,
                audit_context=old_context,
            )
        )
        self.assertTrue(
            services._reconcile_result_capability_is_current(
                generation,
                current_receipt,
                audit_context=current_context,
            )
        )

    def test_execution_replay_identity_uses_domain_generation_not_business_attempt(self):
        source_event_id = uuid.uuid4()
        attempt = SimpleNamespace(
            attempt_no=1,
            execution_generation=2,
            terminal_event_key="publication.requested:attempt:1",
            terminal_generation=2,
        )
        observations = [
            SimpleNamespace(
                source_event_id=source_event_id,
                execution_generation=1,
            ),
            SimpleNamespace(
                source_event_id=source_event_id,
                execution_generation=2,
            ),
        ]

        observed = services._execution_generation_for_source_event(
            attempt,
            source_event_id=source_event_id,
            source_message_key="publication.requested:attempt:1",
            observations=observations,
        )

        self.assertEqual(observed, 2)

    def test_legacy_requested_event_only_binds_virgin_or_same_event_lineage(self):
        attempt_id = uuid.uuid4()
        event_id = uuid.uuid4()
        correlation_id = uuid.uuid4()
        event = SimpleNamespace(
            id=event_id,
            event_version=1,
            topic="publication.requested",
            message_key=f"publication.requested:{attempt_id}:1",
            aggregate_type="publication_attempt",
            aggregate_id=attempt_id,
            job_id=attempt_id,
            correlation_id=correlation_id,
            payload={"publication_attempt_id": str(attempt_id)},
        )
        common = {
            "id": attempt_id,
            "execution_identity_version": PUBLICATION_EXECUTION_IDENTITY_VERSION,
            "correlation_id": correlation_id,
        }
        virgin = SimpleNamespace(**common, attempt_no=1, execution_generation=0)
        retried = SimpleNamespace(**common, attempt_no=2, execution_generation=1)
        resumed = SimpleNamespace(**common, attempt_no=1, execution_generation=1)
        matching = SimpleNamespace(
            source_event_id=event_id,
            identity_version=PUBLICATION_EXECUTION_IDENTITY_VERSION,
            execution_attempt_no=1,
        )

        self.assertEqual(
            services._legacy_requested_binding_action(
                virgin,
                source_event=event,
                observations=(),
            ),
            "bind",
        )
        self.assertEqual(
            services._legacy_requested_binding_action(
                retried,
                source_event=event,
                observations=(),
            ),
            "quarantine",
        )
        self.assertEqual(
            services._legacy_requested_binding_action(
                resumed,
                source_event=event,
                observations=(matching,),
            ),
            "resume",
        )
        self.assertEqual(
            services._legacy_requested_binding_action(
                resumed,
                source_event=event,
                observations=(
                    SimpleNamespace(
                        source_event_id=uuid.uuid4(),
                        identity_version=PUBLICATION_EXECUTION_IDENTITY_VERSION,
                        execution_attempt_no=1,
                    ),
                ),
            ),
            "quarantine",
        )

    def test_unproven_legacy_request_terminalizes_only_a_virgin_queued_attempt(self):
        attempt = MagicMock()
        attempt.id = uuid.uuid4()
        attempt.attempt_no = 2
        attempt.execution_generation = 0
        attempt.state = "queued"
        attempt.publication = MagicMock()
        source_event = SimpleNamespace(
            message_key=f"publication.requested:{attempt.id}:legacy",
        )
        context = SimpleNamespace(event_key=str(uuid.uuid4()))

        with (
            patch.object(services, "_audit_state", return_value={}),
            patch.object(services, "_worker_audit_replay", return_value=None),
            patch.object(services, "_record_publishing_audit") as record,
            patch.object(
                services,
                "_release_article_external_write_fence_locked",
            ) as release,
        ):
            services._quarantine_legacy_requested_delivery_locked(
                attempt,
                source_event=source_event,
                execution_generation=0,
                has_other_lineage=False,
                audit_context=context,
            )

        self.assertEqual(attempt.state, "manual_required")
        self.assertEqual(attempt.error_code, "legacy_execution_identity_unproven")
        self.assertEqual(attempt.terminal_event_key, source_event.message_key)
        self.assertEqual(attempt.terminal_generation, 0)
        attempt.save.assert_called_once()
        self.assertEqual(attempt.publication.state, "manual_required")
        attempt.publication.save.assert_called_once()
        self.assertEqual(record.call_args.kwargs["action"], "publication_attempt.finished")
        release.assert_called_once_with(attempt)

    def test_unproven_legacy_request_never_overwrites_a_newer_active_lineage(self):
        attempt = MagicMock()
        attempt.id = uuid.uuid4()
        attempt.attempt_no = 2
        attempt.execution_generation = 3
        attempt.state = "running"
        source_event = SimpleNamespace(
            message_key=f"publication.requested:{attempt.id}:legacy",
        )

        with (
            patch.object(services, "_audit_state", return_value={}),
            patch.object(services, "_worker_audit_replay", return_value=None),
            patch.object(services, "_record_publishing_audit") as record,
            patch.object(
                services,
                "_release_article_external_write_fence_locked",
            ) as release,
        ):
            services._quarantine_legacy_requested_delivery_locked(
                attempt,
                source_event=source_event,
                execution_generation=3,
                has_other_lineage=True,
                audit_context=SimpleNamespace(event_key=str(uuid.uuid4())),
            )

        attempt.save.assert_not_called()
        release.assert_not_called()
        self.assertEqual(record.call_args.kwargs["action"], "publication_attempt.skipped")


class PublicationExecutionWorkerContractTests(SimpleTestCase):
    def test_initial_attempt_delivery_is_emitted_as_requested_v2(self):
        attempt = SimpleNamespace(
            id=uuid.UUID(int=12),
            attempt_no=1,
            correlation_id=uuid.UUID(int=13),
            publication=SimpleNamespace(),
        )

        with patch.object(services, "_enqueue_event") as enqueue:
            services._queue_attempt_on_commit(attempt)

        enqueue.assert_called_once_with(
            "publication.requested",
            {
                "publication_attempt_id": str(attempt.id),
                "execution_attempt_no": 1,
            },
            event_version=2,
            dedupe_key=f"publication.requested:{attempt.id}:1",
            aggregate_type="publication_attempt",
            aggregate_id=attempt.id,
            job_id=attempt.id,
            available_at=None,
            correlation_id=attempt.correlation_id,
        )

    def test_reconcile_delivery_terminalizes_attempt_once_with_final_impact(self):
        attempt = MagicMock()
        attempt.state = "reconciling"
        generation = MagicMock()
        generation.state = "running"
        generation.generation = 4
        generation.source_event.message_key = "publication.reconcile:attempt:4"

        with patch.object(
            services,
            "_manualize_reconcile_attempt_locked",
        ) as manualize:
            services._terminalize_reconcile_generation_locked(
                attempt,
                generation,
                error_code="delivery_exhausted",
            )

        manualize.assert_called_once()
        self.assertIn("terminal_impact", manualize.call_args.kwargs)
        attempt.save.assert_not_called()

    def test_reconcile_delivery_failed_terminal_callback_is_an_exact_noop(self):
        attempt = MagicMock()
        generation = MagicMock()
        generation.state = "delivery_failed"

        with patch.object(
            services,
            "_manualize_reconcile_attempt_locked",
        ) as manualize:
            services._terminalize_reconcile_generation_locked(
                attempt,
                generation,
                error_code="delivery_exhausted",
            )

        manualize.assert_not_called()
        generation.save.assert_not_called()
        attempt.save.assert_not_called()

    def test_exact_dependent_terminal_delivery_records_one_idempotent_skip(self):
        attempt_id = uuid.uuid4()
        message_key = f"publication.requested:{attempt_id}:1"
        attempt = SimpleNamespace(
            id=attempt_id,
            pk=attempt_id,
            _meta=SimpleNamespace(label_lower="publishing.publicationattempt"),
            state="manual_required",
            terminal_state="manual_required",
            terminal_event_key=message_key,
            terminal_generation=0,
            execution_generation=0,
            attempt_no=1,
            error_code="canonical_dependency_failed",
        )
        source_event = SimpleNamespace(
            id=uuid.uuid4(),
            message_key=message_key,
        )
        audit_context = SimpleNamespace(event_key=str(source_event.id))

        with (
            patch.object(services, "_worker_audit_replay", return_value=None),
            patch.object(services, "_record_publishing_audit") as record,
            patch.object(
                services,
                "_release_article_external_write_fence_locked",
            ) as release,
        ):
            services._converge_terminal_attempt_redelivery_locked(
                attempt,
                source_event=source_event,
                execution_generation=0,
                audit_context=audit_context,
            )

        record.assert_called_once()
        self.assertEqual(
            record.call_args.kwargs["action"],
            "publication_attempt.skipped",
        )
        release.assert_called_once_with(attempt)

    def test_run_finalizer_uses_the_immutable_dispatch_cohort_after_intent_state_changes(self):
        intent_id = uuid.UUID(int=4)
        correlation_id = uuid.UUID(int=5)
        dispatch = SimpleNamespace(
            publication_intent_id=intent_id,
            attempt_count=1,
            attempt_manifest_hash=(
                "7b3e374055554f7d04213536eb73b4971829e0f18bb2e0bd65041e8f46847b02"
            ),
            correlation_id=correlation_id,
        )
        attempt = SimpleNamespace(
            id=uuid.UUID(int=1),
            publication_id=uuid.UUID(int=2),
            publication_intent_id=intent_id,
            publication_intent=SimpleNamespace(
                state="stale",
                target_commands=[],
            ),
            publication=SimpleNamespace(target_id=uuid.UUID(int=3)),
            correlation_id=correlation_id,
            resolved_action="create",
        )

        observed = services._validated_dispatch_attempt_cohort(
            [dispatch],
            [attempt],
        )

        self.assertEqual(observed, (attempt,))

    def test_requested_v2_binds_execution_attempt_and_pre_io_guard_to_persist(self):
        attempt_id = str(uuid.UUID(int=10))
        fence = SimpleNamespace(execution_generation=11)
        command = object()
        target = SimpleNamespace(channel="blogger")
        attempt = SimpleNamespace(
            id=uuid.UUID(attempt_id),
            state="running",
            error_code="",
            resolved_action="create",
            publication=SimpleNamespace(target=target, remote_post_id=None),
        )
        persisted = SimpleNamespace(
            state="succeeded",
            publication=SimpleNamespace(remote_post_id="remote-42"),
        )
        adapter = MagicMock()
        audit_context = object()
        adapter.execute.return_value = PublishResult(
            status="succeeded",
            remote_post_id="remote-42",
            remote_state="published",
        )
        captured_guard = None

        def publisher_factory(_target, *, write_guard):
            nonlocal captured_guard
            captured_guard = write_guard
            adapter.execute.side_effect = lambda _command: (
                write_guard(),
                PublishResult(
                    status="succeeded",
                    remote_post_id="remote-42",
                    remote_state="published",
                ),
            )[1]
            return adapter

        with (
            patch.object(tasks, "_worker_audit_context", return_value=audit_context),
            patch.object(
                tasks,
                "begin_attempt",
                return_value=(attempt, fence, command),
            ) as begin,
            patch.object(tasks, "publisher_for_target", side_effect=publisher_factory),
            patch.object(
                tasks,
                "authorize_publication_external_write",
            ) as authorize,
            patch.object(
                tasks,
                "persist_publish_result",
                return_value=persisted,
            ) as persist,
        ):
            observed = tasks.execute_publication_attempt.run(attempt_id, 3)

        self.assertIsNotNone(captured_guard)
        begin.assert_called_once_with(
            attempt_id,
            execution_attempt_no=3,
            audit_context=audit_context,
        )
        authorize.assert_called_once()
        self.assertIs(authorize.call_args.args[0], fence)
        self.assertIs(persist.call_args.kwargs["execution_fence"], fence)
        self.assertIs(persist.call_args.kwargs["external_write_authorized"], True)
        self.assertEqual(observed["state"], "succeeded")

    def test_preexisting_create_result_is_a_current_no_write_settlement(self):
        attempt_id = str(uuid.UUID(int=20))
        fence = SimpleNamespace(execution_generation=4)
        attempt = SimpleNamespace(
            id=uuid.UUID(attempt_id),
            state="running",
            error_code="",
            resolved_action="create",
            publication=SimpleNamespace(
                target=SimpleNamespace(channel="blogger"),
                remote_post_id=None,
            ),
        )
        adapter = MagicMock()
        adapter.execute.return_value = PublishResult(
            status="succeeded",
            remote_post_id="preexisting-42",
            remote_state="published",
        )
        persisted = SimpleNamespace(
            state="succeeded",
            publication=SimpleNamespace(remote_post_id="preexisting-42"),
        )

        with (
            patch.object(tasks, "_worker_audit_context", return_value=object()),
            patch.object(
                tasks,
                "begin_attempt",
                return_value=(attempt, fence, object()),
            ),
            patch.object(tasks, "publisher_for_target", return_value=adapter),
            patch.object(
                tasks,
                "authorize_publication_external_write",
            ) as authorize,
            patch.object(
                tasks,
                "persist_publish_result",
                return_value=persisted,
            ) as persist,
        ):
            observed = tasks.execute_publication_attempt.run(attempt_id, 1)

        authorize.assert_not_called()
        self.assertIs(
            persist.call_args.kwargs["external_write_authorized"],
            False,
        )
        self.assertEqual(observed["state"], "succeeded")

    def test_direct_prewrite_retry_after_is_forwarded_to_domain_retry(self):
        attempt_id = str(uuid.UUID(int=21))
        attempt = SimpleNamespace(
            id=uuid.UUID(attempt_id),
            state="running",
            error_code="",
            resolved_action="create",
            publication=SimpleNamespace(
                target=SimpleNamespace(channel="wordpress"),
                remote_post_id=None,
            ),
        )
        adapter = MagicMock()
        adapter.execute.side_effect = services.PublisherError(
            "wordpress_unavailable",
            category="retryable",
            retry_after_seconds=137,
        )
        persisted = SimpleNamespace(
            state="retryable_failed",
            publication=SimpleNamespace(remote_post_id=None),
        )

        with (
            patch.object(tasks, "_worker_audit_context", return_value=object()),
            patch.object(
                tasks,
                "begin_attempt",
                return_value=(attempt, SimpleNamespace(), object()),
            ),
            patch.object(tasks, "publisher_for_target", return_value=adapter),
            patch.object(
                tasks,
                "persist_publish_result",
                return_value=persisted,
            ) as persist,
        ):
            tasks.execute_publication_attempt.run(attempt_id, 1)

        self.assertEqual(persist.call_args.kwargs["retry_after_seconds"], 137)
        self.assertIs(
            persist.call_args.kwargs["external_write_authorized"],
            False,
        )
