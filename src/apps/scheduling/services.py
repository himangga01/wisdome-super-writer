from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from croniter import croniter
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.accounts.services import consume_reauthentication_proof
from apps.audit.models import AuditEvent
from apps.audit.services import (
    AuditContext,
    AuditIdentityConflict,
    audit_event_id,
    record_audit_event,
    require_audit_replay,
)
from apps.collection.models import CollectionRun, RunState
from apps.collection.services import create_run
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)
from wisdome_writer.infrastructure.outbox import enqueue_event

from .controls import is_external_write_blocked
from .models import KillSwitchDecision, OperationalControl, Schedule, ScheduleDispatch


TERMINAL_RUN_STATES = {RunState.COMPLETED, RunState.STOPPED, RunState.FAILED}
_SCHEDULE_ID_NAMESPACE = uuid.UUID("af919249-fd4b-4d1d-8e7e-d6943171cfcb")


def _request_hash(material) -> str:
    return canonical_hash(
        material,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _schedule_config_hash(schedule: Schedule) -> str:
    return _request_hash(
        {
            "schema_version": "schedule-config-v1",
            "name": schedule.name,
            "topic_code": schedule.topic_code,
            "cron_expression": schedule.cron_expression,
            "timezone": schedule.timezone,
            "window_minutes": schedule.window_minutes,
            "target_ids": schedule.target_ids,
            "approval_mode": schedule.approval_mode,
            "auto_publish_validation_refs": schedule.auto_publish_validation_refs,
            "auto_publish_activation_refs": schedule.auto_publish_activation_refs,
            "overlap_policy": schedule.overlap_policy,
        }
    )


def _schedule_material(schedule: Schedule) -> dict:
    return {
        "schema_version": "schedule-audit-v1",
        "schedule_id": str(schedule.id),
        "version": schedule.version,
        "config_hash": _schedule_config_hash(schedule),
        "enabled": schedule.enabled,
        "next_run_at": (
            schedule.next_run_at.isoformat() if schedule.next_run_at else None
        ),
        "last_dispatched_at": (
            schedule.last_dispatched_at.isoformat()
            if schedule.last_dispatched_at
            else None
        ),
        "updated_by_id": (
            str(schedule.updated_by_id) if schedule.updated_by_id else None
        ),
    }


def _dispatch_material(dispatch: ScheduleDispatch) -> dict:
    return {
        "schema_version": "schedule-dispatch-audit-v1",
        "dispatch_id": str(dispatch.id),
        "schedule_id": str(dispatch.schedule_id),
        "schedule_version": dispatch.schedule_version,
        "scheduled_for": dispatch.scheduled_for.isoformat(),
        "tick_key": dispatch.tick_key,
        "state": dispatch.state,
        "reason_code": dispatch.reason_code,
        "coalesced_into_id": (
            str(dispatch.coalesced_into_id)
            if dispatch.coalesced_into_id
            else None
        ),
        "collection_run_id": (
            str(dispatch.collection_run_id)
            if dispatch.collection_run_id
            else None
        ),
    }


def _collection_run_material(run: CollectionRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "run_id": str(run.id),
        "topic_code": run.topic_code,
        "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(),
        "trigger": run.trigger,
        "state": run.state,
        "requested_target_ids": run.requested_target_ids,
        "approval_mode": run.approval_mode,
    }


def _control_material(control: OperationalControl) -> dict:
    return {
        "schema_version": "kill-switch-audit-v1",
        "key": control.key,
        "enabled": control.enabled,
        "version": control.version,
        "reason_hash": _request_hash(
            {
                "schema_version": "kill-switch-reason-v1",
                "reason": control.reason,
            }
        ),
        "updated_by_id": (
            str(control.updated_by_id) if control.updated_by_id else None
        ),
    }


def _audit_for_request(
    *,
    alias: str,
    action: str,
    entity,
    request_key: str,
) -> AuditEvent | None:
    return AuditEvent.objects.using(alias).filter(
        id=audit_event_id(
            action=action,
            entity=entity,
            identity_key=request_key,
        )
    ).first()


def _require_admin_context(
    *,
    audit_context: AuditContext,
    user,
) -> None:
    if (
        audit_context.actor_type != AuditEvent.ActorType.ADMIN
        or str(audit_context.actor_id) != str(user.pk)
    ):
        raise ValueError("audit actor does not match the schedule administrator")
    if not audit_context.request_key:
        raise ValueError("request_key is required")
    if audit_context.reason_code is None:
        raise ValueError("reason is required")


def _schedule_create_id(audit_context: AuditContext) -> uuid.UUID:
    return uuid.uuid5(
        _SCHEDULE_ID_NAMESPACE,
        _request_hash(
            {
                "schema_version": "schedule-create-identity-v1",
                "request_key": audit_context.request_key,
            }
        ),
    )


def create_schedule(
    *,
    values: dict,
    user,
    audit_context: AuditContext,
) -> tuple[Schedule, bool]:
    _require_admin_context(audit_context=audit_context, user=user)
    alias = audit_context.database_alias
    schedule_id = _schedule_create_id(audit_context)
    payload_hash = _request_hash(
        {
            "schema_version": "schedule-create-request-v1",
            "values": values,
            "request_key": audit_context.request_key,
            "reason": audit_context.reason_code,
            "actor_id": str(user.pk),
        }
    )
    with transaction.atomic(using=alias):
        (
            get_user_model()
            .objects.using(alias)
            .select_for_update()
            .get(pk=user.pk)
        )
        existing = (
            Schedule.objects.using(alias)
            .select_for_update()
            .filter(pk=schedule_id)
            .first()
        )
        if existing is not None:
            replay = _audit_for_request(
                alias=alias,
                action="schedule.created",
                entity=existing,
                request_key=audit_context.request_key,
            )
            if replay is None:
                raise ValueError(
                    "request_key was already used with a different schedule create request"
                )
            require_audit_replay(
                context=audit_context,
                action="schedule.created",
                entity=existing,
                identity_key=audit_context.request_key,
                request_hash=payload_hash,
            )
            return existing, False

        schedule = Schedule(
            id=schedule_id,
            name=values["name"],
            topic_code=values["topic_code"],
            cron_expression=values["cron_expression"],
            timezone=values.get("timezone", "Asia/Seoul"),
            window_minutes=values.get("window_minutes", 1440),
            target_ids=values.get("target_ids", []),
            approval_mode=values.get("approval_mode", "manual"),
            auto_publish_validation_refs=values.get(
                "auto_publish_validation_refs", []
            ),
            auto_publish_activation_refs=values.get(
                "auto_publish_activation_refs", []
            ),
            overlap_policy=values.get("overlap_policy", "skip"),
            enabled=values.get("enabled", False),
            updated_by=user,
        )
        schedule.next_run_at = calculate_next_run(schedule, timezone.now())
        schedule.save(using=alias)
        record_audit_event(
            context=audit_context,
            action="schedule.created",
            entity=schedule,
            identity_key=audit_context.request_key,
            material_schema_version="schedule-audit-v1",
            before_material={"state": "not_created"},
            after_material=_schedule_material(schedule),
            metadata={
                "request_hash": payload_hash,
                "result": "created",
                "schedule_id": str(schedule.id),
                "schedule_version": schedule.version,
                "enabled": schedule.enabled,
            },
        )
        return schedule, True


def update_schedule(
    *,
    schedule_id,
    changes: dict,
    user,
    audit_context: AuditContext,
) -> tuple[Schedule, bool]:
    _require_admin_context(audit_context=audit_context, user=user)
    alias = audit_context.database_alias
    payload_hash = _request_hash(
        {
            "schema_version": "schedule-update-request-v1",
            "schedule_id": str(schedule_id),
            "changes": changes,
            "request_key": audit_context.request_key,
            "reason": audit_context.reason_code,
            "actor_id": str(user.pk),
        }
    )
    with transaction.atomic(using=alias):
        schedule = (
            Schedule.objects.using(alias)
            .select_for_update()
            .get(pk=schedule_id)
        )
        replay = _audit_for_request(
            alias=alias,
            action="schedule.updated",
            entity=schedule,
            request_key=audit_context.request_key,
        )
        if replay is not None:
            require_audit_replay(
                context=audit_context,
                action="schedule.updated",
                entity=schedule,
                identity_key=audit_context.request_key,
                request_hash=payload_hash,
            )
            return schedule, False

        before_material = _schedule_material(schedule)
        for field, value in changes.items():
            setattr(schedule, field, value)
        schedule.version += 1
        schedule.updated_by = user
        schedule.next_run_at = calculate_next_run(schedule)
        schedule.save(using=alias)
        record_audit_event(
            context=audit_context,
            action="schedule.updated",
            entity=schedule,
            identity_key=audit_context.request_key,
            material_schema_version="schedule-audit-v1",
            before_material=before_material,
            after_material=_schedule_material(schedule),
            metadata={
                "request_hash": payload_hash,
                "result": "updated",
                "schedule_id": str(schedule.id),
                "schedule_version": schedule.version,
                "enabled": schedule.enabled,
            },
        )
        return schedule, True


def disable_schedule(
    *,
    schedule_id,
    user,
    audit_context: AuditContext,
) -> tuple[Schedule, bool]:
    _require_admin_context(audit_context=audit_context, user=user)
    alias = audit_context.database_alias
    payload_hash = _request_hash(
        {
            "schema_version": "schedule-disable-request-v1",
            "schedule_id": str(schedule_id),
            "request_key": audit_context.request_key,
            "reason": audit_context.reason_code,
            "actor_id": str(user.pk),
        }
    )
    with transaction.atomic(using=alias):
        schedule = (
            Schedule.objects.using(alias)
            .select_for_update()
            .get(pk=schedule_id)
        )
        replay = _audit_for_request(
            alias=alias,
            action="schedule.disabled",
            entity=schedule,
            request_key=audit_context.request_key,
        )
        if replay is not None:
            require_audit_replay(
                context=audit_context,
                action="schedule.disabled",
                entity=schedule,
                identity_key=audit_context.request_key,
                request_hash=payload_hash,
            )
            return schedule, False

        before_material = _schedule_material(schedule)
        schedule.enabled = False
        schedule.version += 1
        schedule.updated_by = user
        schedule.save(
            update_fields=("enabled", "version", "updated_by", "updated_at"),
            using=alias,
        )
        record_audit_event(
            context=audit_context,
            action="schedule.disabled",
            entity=schedule,
            identity_key=audit_context.request_key,
            material_schema_version="schedule-audit-v1",
            before_material=before_material,
            after_material=_schedule_material(schedule),
            metadata={
                "request_hash": payload_hash,
                "result": "disabled",
                "schedule_id": str(schedule.id),
                "schedule_version": schedule.version,
                "enabled": False,
            },
        )
        return schedule, True


def calculate_next_run(schedule: Schedule, after: datetime | None = None) -> datetime:
    after = after or timezone.now()
    local = after.astimezone(ZoneInfo(schedule.timezone))
    value = croniter(schedule.cron_expression, local).get_next(datetime)
    return value.astimezone(UTC)


def dispatch_schedule(
    schedule_id,
    scheduled_for: datetime | None = None,
    *,
    audit_context: AuditContext,
):
    if audit_context.actor_type != AuditEvent.ActorType.SYSTEM:
        raise ValueError("scheduled dispatch requires explicit system provenance")
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        schedule = (
            Schedule.objects.using(alias)
            .select_for_update()
            .get(id=schedule_id)
        )
        if not schedule.enabled:
            return None
        external_writes_blocked = is_external_write_blocked(
            using=alias
        )
        schedule_before_material = _schedule_material(schedule)
        scheduled_for = scheduled_for or schedule.next_run_at or timezone.now()
        window_end = scheduled_for
        window_start = window_end - timedelta(minutes=schedule.window_minutes)
        tick_key = (
            f"{schedule.id}:{schedule.version}:{scheduled_for.isoformat()}"
        )
        existing = (
            ScheduleDispatch.objects.using(alias)
            .filter(tick_key=tick_key)
            .first()
        )
        if existing is not None:
            for replay_action in (
                "schedule_dispatch.skipped",
                "schedule_dispatch.queued",
                "schedule_dispatch.coalesced",
                "schedule_dispatch.dispatched",
            ):
                expected_audit_id = audit_event_id(
                    action=replay_action,
                    entity=existing,
                    identity_key=tick_key,
                )
                if not AuditEvent.objects.using(alias).filter(
                    id=expected_audit_id
                ).exists():
                    continue
                require_audit_replay(
                    context=audit_context,
                    action=replay_action,
                    entity=existing,
                    identity_key=tick_key,
                )
                return existing
            raise AuditIdentityConflict(
                "existing schedule tick has no immutable initial audit event"
            )

        dispatch = ScheduleDispatch.objects.using(alias).create(
            schedule=schedule,
            schedule_version=schedule.version,
            scheduled_for=scheduled_for,
            tick_key=tick_key,
            state=ScheduleDispatch.State.SKIPPED,
            window_start=window_start,
            window_end=window_end,
        )
        created_run = None
        if external_writes_blocked:
            dispatch.reason_code = "global_kill_switch"
        else:
            active = (
                CollectionRun.objects.using(alias)
                .filter(topic_code=schedule.topic_code)
                .exclude(state__in=TERMINAL_RUN_STATES)
            )
            if active.exists():
                if schedule.overlap_policy == Schedule.OverlapPolicy.SKIP:
                    dispatch.reason_code = "active_run"
                else:
                    pending = (
                        schedule.dispatches.filter(
                            state=ScheduleDispatch.State.QUEUED
                        )
                        .order_by("scheduled_for")
                        .first()
                    )
                    if pending:
                        dispatch.state = ScheduleDispatch.State.COALESCED
                        dispatch.coalesced_into = pending
                        dispatch.reason_code = "coalesced"
                    else:
                        dispatch.state = ScheduleDispatch.State.QUEUED
                        dispatch.reason_code = "waiting_for_active_run"
            else:
                run, _ = create_run(
                    topic_code=schedule.topic_code,
                    window_start=window_start,
                    window_end=window_end,
                    user=schedule.updated_by,
                    trigger="schedule",
                )
                if run._state.db != alias:
                    raise ValueError(
                        "schedule run and audit database aliases differ"
                    )
                run.requested_target_ids = schedule.target_ids
                run.approval_mode = schedule.approval_mode
                run.save(
                    update_fields=["requested_target_ids", "approval_mode"],
                    using=alias,
                )
                created_run = run
                dispatch.state = ScheduleDispatch.State.DISPATCHED
                dispatch.collection_run = run
                enqueue_event(
                    event_type="run.requested",
                    aggregate_type="collection_run",
                    aggregate_id=run.id,
                    job_id=run.id,
                    dedupe_key=f"run.requested:{run.id}",
                    correlation_id=audit_context.correlation_id,
                    payload={"run_id": str(run.id)},
                )
        dispatch.save(using=alias)
        schedule.last_dispatched_at = scheduled_for
        schedule.next_run_at = calculate_next_run(schedule, scheduled_for)
        schedule.save(
            update_fields=["last_dispatched_at", "next_run_at"],
            using=alias,
        )
        action = {
            ScheduleDispatch.State.SKIPPED: "schedule_dispatch.skipped",
            ScheduleDispatch.State.QUEUED: "schedule_dispatch.queued",
            ScheduleDispatch.State.COALESCED: "schedule_dispatch.coalesced",
            ScheduleDispatch.State.DISPATCHED: "schedule_dispatch.dispatched",
        }[dispatch.state]
        record_audit_event(
            context=audit_context,
            action=action,
            entity=dispatch,
            identity_key=tick_key,
            material_schema_version="schedule-dispatch-audit-v1",
            before_material={
                "dispatch": {
                    "state": "not_dispatched",
                    "tick_key": tick_key,
                },
                "schedule": schedule_before_material,
                "collectionRun": None,
            },
            after_material={
                "dispatch": _dispatch_material(dispatch),
                "schedule": _schedule_material(schedule),
                "collectionRun": _collection_run_material(created_run),
            },
            metadata={
                "result": dispatch.state,
                "state": dispatch.state,
                "reason_code": dispatch.reason_code,
                "dispatch_id": str(dispatch.id),
                "schedule_id": str(schedule.id),
                "schedule_version": dispatch.schedule_version,
                "collection_run_id": (
                    str(dispatch.collection_run_id)
                    if dispatch.collection_run_id
                    else None
                ),
                "coalesced_into_id": (
                    str(dispatch.coalesced_into_id)
                    if dispatch.coalesced_into_id
                    else None
                ),
            },
        )
        return dispatch


def dispatch_due_schedules(
    now: datetime | None = None,
    *,
    audit_context: AuditContext,
):
    now = now or timezone.now()
    alias = audit_context.database_alias
    ids = list(
        Schedule.objects.using(alias)
        .filter(enabled=True, next_run_at__lte=now)
        .values_list("id", flat=True)
    )
    return [
        dispatch_schedule(
            schedule_id,
            audit_context=audit_context,
        )
        for schedule_id in ids
    ]


def release_queued_dispatch(
    schedule_id,
    *,
    audit_context: AuditContext,
):
    """Start the single coalesced tick after the active topic run reaches a terminal state."""
    if audit_context.actor_type != AuditEvent.ActorType.SYSTEM:
        raise ValueError(
            "queued schedule release requires explicit system provenance"
        )
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        schedule = (
            Schedule.objects.using(alias)
            .select_for_update()
            .get(id=schedule_id)
        )
        dispatch = (
            schedule.dispatches.select_for_update()
            .filter(state=ScheduleDispatch.State.QUEUED)
            .order_by("scheduled_for")
            .first()
        )
        if dispatch is None or not schedule.enabled:
            return dispatch
        external_writes_blocked = is_external_write_blocked(
            using=alias
        )
        before_material = _dispatch_material(dispatch)
        identity_key = _request_hash(
            {
                "schema_version": "schedule-release-identity-v1",
                "dispatch_id": str(dispatch.id),
                "event_key": audit_context.event_key,
                "operation_key": audit_context.operation_key,
            }
        )
        if external_writes_blocked:
            if dispatch.reason_code == "global_kill_switch":
                return dispatch
            dispatch.reason_code = "global_kill_switch"
            dispatch.save(update_fields=["reason_code"], using=alias)
            record_audit_event(
                context=audit_context,
                action="schedule_dispatch.skipped",
                entity=dispatch,
                identity_key=identity_key,
                material_schema_version="schedule-dispatch-audit-v1",
                before_material={
                    "dispatch": before_material,
                    "collectionRun": None,
                },
                after_material={
                    "dispatch": _dispatch_material(dispatch),
                    "collectionRun": None,
                },
                metadata={
                    "result": "release_blocked",
                    "state": dispatch.state,
                    "reason_code": dispatch.reason_code,
                    "dispatch_id": str(dispatch.id),
                    "schedule_id": str(schedule.id),
                    "schedule_version": dispatch.schedule_version,
                },
            )
            return dispatch
        active = (
            CollectionRun.objects.using(alias)
            .filter(topic_code=schedule.topic_code)
            .exclude(state__in=TERMINAL_RUN_STATES)
        )
        if active.exists():
            return dispatch
        run, _ = create_run(
            topic_code=schedule.topic_code,
            window_start=dispatch.window_start,
            window_end=dispatch.window_end,
            user=schedule.updated_by,
            trigger="schedule",
        )
        if run._state.db != alias:
            raise ValueError("schedule run and audit database aliases differ")
        run.requested_target_ids = schedule.target_ids
        run.approval_mode = schedule.approval_mode
        run.save(
            update_fields=["requested_target_ids", "approval_mode"],
            using=alias,
        )
        dispatch.collection_run = run
        dispatch.state = ScheduleDispatch.State.DISPATCHED
        dispatch.reason_code = "released_after_terminal_run"
        dispatch.save(
            update_fields=["collection_run", "state", "reason_code"],
            using=alias,
        )
        enqueue_event(
            event_type="run.requested",
            aggregate_type="collection_run",
            aggregate_id=run.id,
            job_id=run.id,
            dedupe_key=f"run.requested:{run.id}",
            correlation_id=audit_context.correlation_id,
            payload={"run_id": str(run.id)},
        )
        record_audit_event(
            context=audit_context,
            action="schedule_dispatch.released",
            entity=dispatch,
            identity_key=identity_key,
            material_schema_version="schedule-dispatch-audit-v1",
            before_material={
                "dispatch": before_material,
                "collectionRun": None,
            },
            after_material={
                "dispatch": _dispatch_material(dispatch),
                "collectionRun": _collection_run_material(run),
            },
            metadata={
                "result": "released",
                "state": dispatch.state,
                "reason_code": dispatch.reason_code,
                "dispatch_id": str(dispatch.id),
                "schedule_id": str(schedule.id),
                "schedule_version": dispatch.schedule_version,
                "collection_run_id": str(run.id),
            },
        )
        return dispatch


def release_waiting_for_topic(
    topic_code: str,
    *,
    audit_context: AuditContext,
) -> None:
    alias = audit_context.database_alias
    schedule_ids = list(
        Schedule.objects.using(alias).filter(
            topic_code=topic_code,
            dispatches__state=ScheduleDispatch.State.QUEUED,
        )
        .distinct()
        .values_list("id", flat=True)
    )
    for schedule_id in schedule_ids:
        release_queued_dispatch(
            schedule_id,
            audit_context=audit_context,
        )


def set_kill_switch(
    *,
    enabled: bool,
    expected_version: int,
    request_key: str,
    reason: str,
    user,
    request=None,
    reauth_proof_id=None,
    audit_context: AuditContext,
):
    _require_admin_context(audit_context=audit_context, user=user)
    if audit_context.request_key != request_key:
        raise ValueError("audit request_key does not match kill-switch request")
    if audit_context.reason_code != reason:
        raise ValueError("audit reason does not match kill-switch request")

    alias = audit_context.database_alias
    request_hash = _request_hash(
        {
            "schema_version": "kill-switch-request-v1",
            "enabled": enabled,
            "expected_version": expected_version,
            "request_key": request_key,
            "reason": reason,
            "actor_id": str(user.pk),
        }
    )
    with transaction.atomic(using=alias):
        existing = (
            KillSwitchDecision.objects.using(alias)
            .select_for_update()
            .filter(request_key=request_key)
            .first()
        )
        if existing:
            if (
                existing.enabled != enabled
                or existing.expected_version != expected_version
                or existing.reason != reason
                or existing.decided_by_id != user.pk
            ):
                raise ValueError("request_key_conflict")
            replay = _audit_for_request(
                alias=alias,
                action="kill_switch.decided",
                entity=existing,
                request_key=request_key,
            )
            if replay is None:
                raise ValueError(
                    "existing kill-switch decision has no matching audit event"
                )
            require_audit_replay(
                context=audit_context,
                action="kill_switch.decided",
                entity=existing,
                identity_key=request_key,
                request_hash=request_hash,
            )
            return existing
        control, control_created = (
            OperationalControl.objects.using(alias)
            .select_for_update()
            .get_or_create(key="global_kill_switch")
        )
        if control.version != expected_version:
            raise ValueError("stale_control_version")
        before_material = (
            {
                "schema_version": "kill-switch-audit-v1",
                "state": "not_created",
            }
            if control_created
            else _control_material(control)
        )
        if not enabled:
            if request is None or not reauth_proof_id:
                raise ValueError("reauthentication_required")
            consume_reauthentication_proof(
                request=request,
                proof_id=reauth_proof_id,
                action_scope="kill_switch_disable",
                entity_type="operational_control",
                entity_id=UUID(
                    "00000000-0000-4000-8000-000000000001"
                ),
            )
        decision = KillSwitchDecision.objects.using(alias).create(
            expected_version=expected_version,
            enabled=enabled,
            request_key=request_key,
            reason=reason,
            decided_by=user,
        )
        control.enabled = enabled
        control.version += 1
        control.reason = reason
        control.updated_by = user
        control.save(using=alias)
        record_audit_event(
            context=audit_context,
            action="kill_switch.decided",
            entity=decision,
            identity_key=request_key,
            material_schema_version="kill-switch-audit-v1",
            before_material=before_material,
            after_material=_control_material(control),
            metadata={
                "request_hash": request_hash,
                "decision": "enabled" if enabled else "disabled",
                "enabled": enabled,
                "version": control.version,
                "decision_id": str(decision.id),
                "reauth_proof_id": (
                    str(reauth_proof_id) if reauth_proof_id else None
                ),
            },
        )
        return decision
