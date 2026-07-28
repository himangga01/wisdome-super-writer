from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from croniter import croniter
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.services import consume_reauthentication_proof
from apps.collection.models import CollectionRun, RunState
from apps.collection.services import create_run
from wisdome_writer.infrastructure.outbox import enqueue_event

from .models import KillSwitchDecision, OperationalControl, Schedule, ScheduleDispatch


TERMINAL_RUN_STATES = {RunState.COMPLETED, RunState.STOPPED, RunState.FAILED}


def calculate_next_run(schedule: Schedule, after: datetime | None = None) -> datetime:
    after = after or timezone.now()
    local = after.astimezone(ZoneInfo(schedule.timezone))
    value = croniter(schedule.cron_expression, local).get_next(datetime)
    return value.astimezone(UTC)


@transaction.atomic
def dispatch_schedule(schedule_id, scheduled_for: datetime | None = None):
    schedule = Schedule.objects.select_for_update().get(id=schedule_id)
    if not schedule.enabled:
        return None
    control, _ = OperationalControl.objects.get_or_create(key="global_kill_switch")
    scheduled_for = scheduled_for or schedule.next_run_at or timezone.now()
    window_end = scheduled_for
    window_start = window_end - timedelta(minutes=schedule.window_minutes)
    tick_key = f"{schedule.id}:{schedule.version}:{scheduled_for.isoformat()}"
    try:
        dispatch = ScheduleDispatch.objects.create(
            schedule=schedule,
            schedule_version=schedule.version,
            scheduled_for=scheduled_for,
            tick_key=tick_key,
            state=ScheduleDispatch.State.SKIPPED,
            window_start=window_start,
            window_end=window_end,
        )
    except IntegrityError:
        return ScheduleDispatch.objects.get(tick_key=tick_key)
    if control.enabled:
        dispatch.reason_code = "global_kill_switch"
    else:
        active = CollectionRun.objects.filter(topic_code=schedule.topic_code).exclude(state__in=TERMINAL_RUN_STATES)
        if active.exists():
            if schedule.overlap_policy == Schedule.OverlapPolicy.SKIP:
                dispatch.reason_code = "active_run"
            else:
                pending = schedule.dispatches.filter(state=ScheduleDispatch.State.QUEUED).first()
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
            run.requested_target_ids = schedule.target_ids
            run.approval_mode = schedule.approval_mode
            run.save(update_fields=["requested_target_ids", "approval_mode"])
            dispatch.state = ScheduleDispatch.State.DISPATCHED
            dispatch.collection_run = run
            enqueue_event(
                event_type="run.requested",
                aggregate_type="collection_run",
                aggregate_id=run.id,
                job_id=run.id,
                dedupe_key=f"run.requested:{run.id}",
                payload={"run_id": str(run.id), "topic_code": run.topic_code},
            )
    dispatch.save()
    schedule.last_dispatched_at = scheduled_for
    schedule.next_run_at = calculate_next_run(schedule, scheduled_for)
    schedule.save(update_fields=["last_dispatched_at", "next_run_at"])
    return dispatch


def dispatch_due_schedules(now: datetime | None = None):
    now = now or timezone.now()
    ids = list(Schedule.objects.filter(enabled=True, next_run_at__lte=now).values_list("id", flat=True))
    return [dispatch_schedule(schedule_id) for schedule_id in ids]


@transaction.atomic
def release_queued_dispatch(schedule_id):
    """Start the single coalesced tick after the active topic run reaches a terminal state."""
    schedule = Schedule.objects.select_for_update().get(id=schedule_id)
    dispatch = (
        schedule.dispatches.select_for_update()
        .filter(state=ScheduleDispatch.State.QUEUED)
        .order_by("scheduled_for")
        .first()
    )
    if dispatch is None or not schedule.enabled:
        return dispatch
    control, _ = OperationalControl.objects.get_or_create(key="global_kill_switch")
    if control.enabled:
        dispatch.reason_code = "global_kill_switch"
        dispatch.save(update_fields=["reason_code"])
        return dispatch
    active = CollectionRun.objects.filter(topic_code=schedule.topic_code).exclude(
        state__in=TERMINAL_RUN_STATES
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
    run.requested_target_ids = schedule.target_ids
    run.approval_mode = schedule.approval_mode
    run.save(update_fields=["requested_target_ids", "approval_mode"])
    dispatch.collection_run = run
    dispatch.state = ScheduleDispatch.State.DISPATCHED
    dispatch.reason_code = "released_after_terminal_run"
    dispatch.save(update_fields=["collection_run", "state", "reason_code"])
    enqueue_event(
        event_type="run.requested",
        aggregate_type="collection_run",
        aggregate_id=run.id,
        job_id=run.id,
        dedupe_key=f"run.requested:{run.id}",
        payload={"run_id": str(run.id), "topic_code": run.topic_code},
    )
    return dispatch


def release_waiting_for_topic(topic_code: str) -> None:
    schedule_ids = list(
        Schedule.objects.filter(
            topic_code=topic_code,
            dispatches__state=ScheduleDispatch.State.QUEUED,
        )
        .distinct()
        .values_list("id", flat=True)
    )
    for schedule_id in schedule_ids:
        release_queued_dispatch(schedule_id)


@transaction.atomic
def set_kill_switch(
    *,
    enabled: bool,
    expected_version: int,
    request_key: str,
    reason: str,
    user,
    request=None,
    reauth_proof_id=None,
):
    control, _ = OperationalControl.objects.select_for_update().get_or_create(
        key="global_kill_switch"
    )
    existing = KillSwitchDecision.objects.filter(request_key=request_key).first()
    if existing:
        if (
            existing.enabled != enabled
            or existing.expected_version != expected_version
            or existing.reason != reason
            or existing.decided_by_id != user.pk
        ):
            raise ValueError("request_key_conflict")
        return existing
    if control.version != expected_version:
        raise ValueError("stale_control_version")
    if not enabled:
        if request is None or not reauth_proof_id:
            raise ValueError("reauthentication_required")
        consume_reauthentication_proof(
            request=request,
            proof_id=reauth_proof_id,
            action_scope="kill_switch_disable",
            entity_type="operational_control",
            entity_id=UUID("00000000-0000-4000-8000-000000000001"),
        )
    decision = KillSwitchDecision.objects.create(
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
    control.save()
    return decision
