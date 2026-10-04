from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone as django_timezone

from apps.collection import services as collection_services
from apps.collection.models import CollectionRun, RunState
from apps.publishing import automation
from apps.scheduling import services as scheduling_services
from apps.scheduling.models import Schedule, ScheduleDispatch
from apps.topics.models import SourceRegistrySnapshot, TopicPolicy
from apps.topics.services import registry_manifest_hash_for_memberships
from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash


SHA_A = "a" * 64
SHA_B = "b" * 64
TICK = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)


class ScheduleDispatchMaterialContractTests(SimpleTestCase):
    def test_model_exposes_frozen_tick_execution_and_queue_union_material(self):
        fields = {field.name for field in ScheduleDispatch._meta.get_fields()}
        self.assertTrue(
            {
                "material_version",
                "schedule_material",
                "schedule_material_hash",
                "source_registry",
                "registry_manifest_hash",
                "topic_policy",
                "topic_policy_version",
                "topic_policy_hash",
                "target_snapshot_refs",
                "approval_mode_snapshot",
                "validation_refs",
                "activation_refs",
                "requested_by",
                "coalesced_tick_refs",
                "coalesced_tick_manifest_hash",
                "tick_set_version",
                "execution_material",
                "execution_material_hash",
                "run_request_fingerprint",
                "dispatched_at",
            }.issubset(fields)
        )

    def test_queue_one_union_is_sorted_bounded_and_covers_every_tick_window(self):
        root = {
            "dispatchId": "00000000-0000-0000-0000-000000000002",
            "tickKey": "tick-2",
            "scheduledFor": (TICK + timedelta(hours=1)).isoformat(),
            "windowStart": TICK.isoformat(),
            "windowEnd": (TICK + timedelta(hours=1)).isoformat(),
        }
        earlier = {
            "dispatchId": "00000000-0000-0000-0000-000000000001",
            "tickKey": "tick-1",
            "scheduledFor": TICK.isoformat(),
            "windowStart": (TICK - timedelta(hours=1)).isoformat(),
            "windowEnd": TICK.isoformat(),
        }

        refs, window_start, window_end, manifest_hash = (
            scheduling_services._coalesced_tick_union([root], earlier)
        )

        self.assertEqual([row["tickKey"] for row in refs], ["tick-1", "tick-2"])
        self.assertEqual(window_start, TICK - timedelta(hours=1))
        self.assertEqual(window_end, TICK + timedelta(hours=1))
        self.assertEqual(
            manifest_hash,
            canonical_hash(refs, schema_version=CANONICAL_HASH_SCHEMA_V1),
        )

    def test_due_scan_passes_the_captured_tick_instead_of_rereading_next_run(self):
        queryset = MagicMock()
        queryset.filter.return_value.values_list.return_value = [
            ("00000000-0000-0000-0000-000000000001", TICK)
        ]
        queryset.filter.return_value.order_by.return_value = queryset.filter.return_value
        audit_context = SimpleNamespace(database_alias="default", actor_type="system")

        with (
            patch.object(
                scheduling_services.Schedule.objects,
                "using",
                return_value=queryset,
            ),
            patch.object(
                scheduling_services,
                "dispatch_schedule",
                return_value=SimpleNamespace(id="dispatch-1"),
            ) as dispatch,
        ):
            scheduling_services.dispatch_due_schedules(
                now=TICK + timedelta(minutes=1),
                audit_context=audit_context,
            )

        dispatch.assert_called_once_with(
            "00000000-0000-0000-0000-000000000001",
            scheduled_for=TICK,
            audit_context=audit_context,
        )

    def test_sqlite_busy_tick_retries_then_converges_to_one_replay(self):
        expected = SimpleNamespace(id="dispatch-1")
        audit_context = SimpleNamespace(actor_type="system")
        with (
            patch.object(
                scheduling_services,
                "_dispatch_schedule_atomic",
                side_effect=[OperationalError("database is locked"), expected],
            ) as atomic_dispatch,
            patch.object(scheduling_services.time, "sleep"),
        ):
            observed = scheduling_services.dispatch_schedule(
                "00000000-0000-0000-0000-000000000001",
                scheduled_for=TICK,
                audit_context=audit_context,
            )

        self.assertIs(observed, expected)
        self.assertEqual(atomic_dispatch.call_count, 2)

    def test_collection_run_fingerprint_binds_frozen_schedule_execution_hash(self):
        base = {
            "topic_code": "housing_subscription",
            "window_start": TICK - timedelta(hours=1),
            "window_end": TICK,
            "trigger": "schedule",
            "registry_id": "00000000-0000-0000-0000-000000000010",
            "registry_hash": SHA_A,
            "policy_id": "00000000-0000-0000-0000-000000000020",
            "policy_version": 3,
            "policy_hash": SHA_B,
            "freshness_minutes": 60,
            "allowed_authority_tiers": ["primary_official"],
            "requested_target_ids": [
                "00000000-0000-0000-0000-000000000030"
            ],
            "approval_mode": "validated_auto",
        }

        first = collection_services._collection_run_request_fingerprint(
            **base,
            execution_material_hash=SHA_A,
        )
        second = collection_services._collection_run_request_fingerprint(
            **base,
            execution_material_hash=SHA_B,
        )

        self.assertNotEqual(first, second)

    def test_automation_uses_only_frozen_dispatch_material(self):
        execution = {
            "schemaVersion": "schedule-execution-material-v1",
            "scheduleDispatchId": "00000000-0000-0000-0000-000000000001",
            "topic": "housing_subscription",
            "approvalMode": "validated_auto",
            "requestedById": "00000000-0000-0000-0000-000000000002",
            "registry": {
                "snapshotId": "00000000-0000-0000-0000-000000000003",
                "manifestHash": SHA_A,
            },
            "topicPolicy": {
                "id": "00000000-0000-0000-0000-000000000004",
                "version": 2,
                "policyHash": SHA_B,
            },
            "targetSnapshots": [],
            "validationRefs": [],
            "activationRefs": [],
            "tickRefs": [],
            "windowStart": (TICK - timedelta(hours=1)).isoformat(),
            "windowEnd": TICK.isoformat(),
        }
        dispatch = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000001",
            material_version="schedule-dispatch-material-v1",
            execution_material=execution,
            execution_material_hash=canonical_hash(
                execution,
                schema_version=CANONICAL_HASH_SCHEMA_V1,
            ),
            run_request_fingerprint=SHA_A,
        )
        run = SimpleNamespace(
            request_fingerprint=SHA_A,
            topic_code="housing_subscription",
            approval_mode="validated_auto",
            requested_target_ids=[],
            requested_by_id="00000000-0000-0000-0000-000000000002",
            source_registry_id="00000000-0000-0000-0000-000000000003",
            registry_manifest_hash=SHA_A,
            topic_policy_id="00000000-0000-0000-0000-000000000004",
            policy_version=2,
            policy_hash=SHA_B,
            window_start=TICK - timedelta(hours=1),
            window_end=TICK,
        )

        observed = automation._require_frozen_schedule(dispatch, run=run)

        self.assertEqual(observed, execution)

    def test_tick_material_is_derived_from_locked_server_snapshots(self):
        target_id = "00000000-0000-0000-0000-000000000030"
        snapshot_id = "00000000-0000-0000-0000-000000000040"
        validation_id = "00000000-0000-0000-0000-000000000050"
        activation_id = "00000000-0000-0000-0000-000000000060"
        validation_refs = [
            {
                "targetId": target_id,
                "targetSnapshotId": snapshot_id,
                "validationId": validation_id,
                "materialHash": SHA_A,
            }
        ]
        activation_refs = [
            {
                "targetId": target_id,
                "targetSnapshotId": snapshot_id,
                "activationId": activation_id,
                "version": 4,
                "activationHash": SHA_B,
            }
        ]
        schedule = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000010",
            version=3,
            name="server-owned schedule",
            topic_code="housing_subscription",
            cron_expression="0 * * * *",
            timezone="Asia/Seoul",
            window_minutes=60,
            target_ids=[target_id],
            approval_mode="validated_auto",
            auto_publish_validation_refs=validation_refs,
            auto_publish_activation_refs=activation_refs,
            overlap_policy="queue_one",
            updated_by_id="00000000-0000-0000-0000-000000000020",
        )
        target = SimpleNamespace(
            id=target_id,
            current_snapshot_id=snapshot_id,
            current_config_hash=SHA_A,
            channel="wordpress",
            role="primary_canonical",
            environment="test",
            credential_version="secret-v7",
        )
        target_query = MagicMock()
        target_query.select_for_update.return_value = target_query
        target_query.select_related.return_value = target_query
        target_query.filter.return_value = target_query
        target_query.order_by.return_value = [target]
        registry = {
            "snapshotId": "00000000-0000-0000-0000-000000000070",
            "manifestHash": SHA_A,
        }
        policy = {
            "id": "00000000-0000-0000-0000-000000000080",
            "version": 2,
            "policyHash": SHA_B,
        }

        with (
            patch.object(
                scheduling_services.transaction,
                "get_connection",
                return_value=SimpleNamespace(in_atomic_block=True),
            ),
            patch(
                "apps.publishing.models.PublicationTarget.objects.using",
                return_value=target_query,
            ),
            patch("apps.publishing.services._lock_target_intent_fences"),
            patch(
                "apps.publishing.services._normalized_validation_refs",
                return_value=validation_refs,
            ),
            patch(
                "apps.publishing.services._validated_auto_target_material_eligible",
                return_value=True,
            ) as eligible,
            patch(
                "apps.topics.services.approved_registry_material",
                return_value=registry,
            ),
            patch(
                "apps.topics.services.approved_topic_policy_material",
                return_value=policy,
            ),
        ):
            material = scheduling_services.build_schedule_dispatch_material(
                schedule,
                dispatch_id="00000000-0000-0000-0000-000000000090",
                scheduled_for=TICK,
            )

        self.assertEqual(material["registry"], registry)
        self.assertEqual(material["topicPolicy"], policy)
        self.assertEqual(
            material["targetSnapshots"][0]["credentialVersion"],
            "secret-v7",
        )
        self.assertEqual(material["validationRefs"], validation_refs)
        self.assertEqual(material["activationRefs"], activation_refs)
        eligible.assert_called_once()


class ScheduleDispatchMaterialDatabaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            email="t027-schedule@example.com",
            password="not-a-real-secret",
            is_staff=True,
        )
        cls.registry_hash = registry_manifest_hash_for_memberships([])
        cls.registry = SourceRegistrySnapshot.objects.create(
            topic_code="housing_subscription",
            version=1,
            state=SourceRegistrySnapshot.State.APPROVED,
            manifest_hash=cls.registry_hash,
            approved_by=cls.user,
            approved_at=django_timezone.now(),
        )
        policy_document = {"allowedAuthorityTiers": ["primary_official"]}
        cls.policy = TopicPolicy.objects.create(
            code="housing_subscription",
            version=1,
            title="T027 policy",
            freshness_minutes=60,
            policy=policy_document,
            policy_hash=canonical_hash(
                policy_document,
                schema_version=CANONICAL_HASH_SCHEMA_V1,
            ),
        )

    def setUp(self):
        self.schedule = Schedule.objects.create(
            name="T027 schedule",
            topic_code="housing_subscription",
            cron_expression="0 * * * *",
            window_minutes=60,
            target_ids=["00000000-0000-0000-0000-000000000030"],
            approval_mode="validated_auto",
            auto_publish_validation_refs=[],
            auto_publish_activation_refs=[],
            overlap_policy=Schedule.OverlapPolicy.QUEUE_ONE,
            enabled=True,
            next_run_at=django_timezone.now() - timedelta(minutes=1),
            updated_by=self.user,
        )
        self.audit_context = SimpleNamespace(
            actor_type="system",
            database_alias="default",
            correlation_id="00000000-0000-0000-0000-000000000099",
            event_key="schedule-terminal:event-1",
            operation_key="schedule-queued-release",
        )

    def _material(self, schedule, *, dispatch_id, scheduled_for, using="default"):
        window_start = scheduled_for - timedelta(minutes=schedule.window_minutes)
        return {
            "schemaVersion": "schedule-dispatch-material-v1",
            "scheduleDispatchId": str(dispatch_id),
            "scheduleId": str(schedule.id),
            "scheduleVersion": schedule.version,
            "scheduleConfigHash": SHA_A,
            "scheduledFor": scheduled_for.isoformat(),
            "topic": schedule.topic_code,
            "approvalMode": schedule.approval_mode,
            "overlapPolicy": schedule.overlap_policy,
            "requestedById": str(self.user.id),
            "registry": {
                "snapshotId": str(self.registry.id),
                "manifestHash": self.registry_hash,
            },
            "topicPolicy": {
                "id": str(self.policy.id),
                "version": self.policy.version,
                "policyHash": self.policy.policy_hash,
            },
            "targetSnapshots": [
                {
                    "targetId": "00000000-0000-0000-0000-000000000030",
                    "targetSnapshotId": "00000000-0000-0000-0000-000000000040",
                    "targetConfigHash": SHA_B,
                    "channel": "wordpress",
                    "role": "primary_canonical",
                    "environment": "test",
                    "credentialVersion": "v1",
                }
            ],
            "validationRefs": [],
            "activationRefs": [],
            "windowStart": window_start.isoformat(),
            "windowEnd": scheduled_for.isoformat(),
        }

    def _service_patches(self):
        return (
            patch.object(
                scheduling_services,
                "build_schedule_dispatch_material",
                side_effect=self._material,
            ),
            patch.object(
                scheduling_services,
                "is_external_write_blocked",
                return_value=False,
            ),
            patch.object(scheduling_services, "record_audit_event"),
            patch.object(scheduling_services, "enqueue_event"),
            patch.object(
                scheduling_services,
                "calculate_next_run",
                side_effect=lambda _schedule, after=None: (
                    (after or django_timezone.now()) + timedelta(hours=1)
                ),
            ),
        )

    def test_immediate_tick_freezes_one_execution_and_run_fingerprint(self):
        tick = django_timezone.now() - timedelta(minutes=1)
        patches = self._service_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            dispatch = scheduling_services.dispatch_schedule(
                self.schedule.id,
                scheduled_for=tick,
                audit_context=self.audit_context,
            )

        dispatch.refresh_from_db()
        run = dispatch.collection_run
        self.assertEqual(dispatch.state, ScheduleDispatch.State.DISPATCHED)
        self.assertEqual(dispatch.execution_material_hash, canonical_hash(
            dispatch.execution_material,
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        ))
        self.assertEqual(dispatch.run_request_fingerprint, run.request_fingerprint)
        self.assertEqual(run.requested_target_ids, self.schedule.target_ids)
        self.assertEqual(run.approval_mode, "validated_auto")
        dispatch.schedule_material = {
            **dispatch.schedule_material,
            "topic": "semiconductor_news",
        }
        with self.assertRaises(ValidationError):
            dispatch.save()
        with self.assertRaises(TypeError):
            ScheduleDispatch.objects.filter(pk=dispatch.id).update(
                schedule_material_hash=SHA_B
            )

    def test_queue_one_coalesces_windows_without_creating_a_second_run(self):
        now = django_timezone.now() - timedelta(hours=2)
        active_run = CollectionRun.objects.create(
            display_id="RUN-T027-ACTIVE",
            topic_code="housing_subscription",
            trigger="manual",
            approval_mode="manual",
            window_start=now - timedelta(hours=1),
            window_end=now,
            source_registry=self.registry,
            registry_manifest_hash=self.registry_hash,
            topic_policy=self.policy,
            policy_version=self.policy.version,
            policy_hash=self.policy.policy_hash,
            freshness_minutes=60,
            allowed_authority_tiers=["primary_official"],
            freshness_cutoff=now - timedelta(hours=1),
            requested_target_ids=[],
            request_fingerprint="c" * 64,
            state=RunState.COLLECTING,
            requested_by=self.user,
        )
        patches = self._service_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            first = scheduling_services.dispatch_schedule(
                self.schedule.id,
                scheduled_for=now,
                audit_context=self.audit_context,
            )
            second = scheduling_services.dispatch_schedule(
                self.schedule.id,
                scheduled_for=now + timedelta(hours=1),
                audit_context=self.audit_context,
            )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, ScheduleDispatch.State.QUEUED)
        self.assertEqual(second.state, ScheduleDispatch.State.COALESCED)
        self.assertEqual(second.coalesced_into_id, first.id)
        self.assertEqual(first.tick_set_version, 2)
        self.assertEqual(len(first.coalesced_tick_refs), 2)
        self.assertEqual(
            ScheduleDispatch.objects.filter(state=ScheduleDispatch.State.QUEUED).count(),
            1,
        )
        self.assertEqual(
            ScheduleDispatch.objects.exclude(collection_run=None).count(),
            0,
        )

        replacement_owner = get_user_model().objects.create_user(
            email="t027-updated-owner@example.com",
            password="not-a-real-secret",
            is_staff=True,
        )
        self.schedule.target_ids = [
            "00000000-0000-0000-0000-000000000031"
        ]
        self.schedule.approval_mode = "manual"
        self.schedule.auto_publish_validation_refs = []
        self.schedule.auto_publish_activation_refs = []
        self.schedule.updated_by = replacement_owner
        self.schedule.version += 1
        self.schedule.save()
        active_run.state = RunState.COMPLETED
        active_run.save(update_fields=["state"])
        with (
            patch.object(
                scheduling_services,
                "is_external_write_blocked",
                return_value=False,
            ),
            patch.object(scheduling_services, "record_audit_event"),
            patch.object(scheduling_services, "enqueue_event"),
        ):
            released = scheduling_services.release_queued_dispatch(
                self.schedule.id,
                audit_context=self.audit_context,
            )

        released.refresh_from_db()
        self.assertEqual(released.state, ScheduleDispatch.State.DISPATCHED)
        self.assertEqual(
            released.collection_run.requested_target_ids,
            ["00000000-0000-0000-0000-000000000030"],
        )
        self.assertEqual(released.collection_run.approval_mode, "validated_auto")
        self.assertEqual(released.collection_run.requested_by_id, self.user.id)
        self.assertEqual(released.execution_material["tickSetVersion"], 2)

    def test_legacy_queued_dispatch_is_quarantined_without_creating_a_run(self):
        tick = django_timezone.now() - timedelta(hours=1)
        legacy = ScheduleDispatch.objects.create(
            schedule=self.schedule,
            schedule_version=self.schedule.version,
            scheduled_for=tick,
            tick_key=f"legacy:{self.schedule.id}:{tick.isoformat()}",
            state=ScheduleDispatch.State.QUEUED,
            window_start=tick - timedelta(hours=1),
            window_end=tick,
        )

        with patch.object(
            scheduling_services,
            "is_external_write_blocked",
            side_effect=AssertionError("legacy material must fail before release"),
        ):
            observed = scheduling_services.release_queued_dispatch(
                self.schedule.id,
                audit_context=self.audit_context,
            )

        observed.refresh_from_db()
        self.assertEqual(observed.id, legacy.id)
        self.assertEqual(observed.state, ScheduleDispatch.State.QUEUED)
        self.assertEqual(observed.reason_code, "legacy_unverifiable_material")
        self.assertIsNone(observed.collection_run_id)
