from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods

from apps.audit.services import AuditContext
from wisdome_writer.api.openapi import openapi_operations
from wisdome_writer.domain.errors import InvalidInput, StateConflict

from .models import KillSwitchDecision, OperationalControl, Schedule
from .services import (
    create_schedule,
    disable_schedule,
    set_kill_switch,
    update_schedule,
)


def _payload(row):
    return {
        "id": str(row.id),
        "version": row.version,
        "name": row.name,
        "topic": row.topic_code,
        "cronExpression": row.cron_expression,
        "timezone": row.timezone,
        "windowMinutes": row.window_minutes,
        "targetIds": row.target_ids,
        "approvalMode": row.approval_mode,
        "autoPublishValidationRefs": row.auto_publish_validation_refs,
        "autoPublishActivationRefs": row.auto_publish_activation_refs,
        "overlapPolicy": row.overlap_policy,
        "enabled": row.enabled,
        "nextRunAt": row.next_run_at.isoformat() if row.next_run_at else None,
        "lastDispatchedAt": (
            row.last_dispatched_at.isoformat()
            if row.last_dispatched_at
            else None
        ),
        "updatedAt": row.updated_at.isoformat(),
    }


@login_required
@openapi_operations({"GET": "listSchedules", "POST": "createSchedule"})
@require_http_methods(["GET", "POST"])
def schedules(request):
    if request.method == "GET":
        rows = Schedule.objects.order_by("name", "id")[:200]
        return JsonResponse({"items": [_payload(row) for row in rows]})
    body = request.openapi_body
    audit_context = AuditContext.for_admin(
        request=request,
        reason_code=body["reason"],
        request_key=body["requestKey"],
    )
    try:
        row, created = create_schedule(
            values={
                "name": body["name"],
                "topic_code": body["topic"],
                "cron_expression": body["cronExpression"],
                "timezone": body.get("timezone", "Asia/Seoul"),
                "window_minutes": body.get("windowMinutes", 1440),
                "target_ids": body["targetIds"],
                "approval_mode": body["approvalMode"],
                "auto_publish_validation_refs": body[
                    "autoPublishValidationRefs"
                ],
                "auto_publish_activation_refs": body[
                    "autoPublishActivationRefs"
                ],
                "overlap_policy": body["overlapPolicy"],
                "enabled": body.get("enabled", False),
            },
            user=request.user,
            audit_context=audit_context,
        )
    except ValueError as exc:
        if "request_key" in str(exc):
            raise StateConflict(str(exc)) from exc
        raise InvalidInput(str(exc)) from exc
    return JsonResponse(_payload(row), status=201 if created else 200)


@login_required
@openapi_operations(
    {
        "GET": "getSchedule",
        "PATCH": "updateSchedule",
        "DELETE": "disableSchedule",
    }
)
@require_http_methods(["GET", "PATCH", "DELETE"])
def schedule_detail(request, schedule_id):
    if request.method == "GET":
        return JsonResponse(
            _payload(get_object_or_404(Schedule, id=schedule_id))
        )
    body = request.openapi_body
    audit_context = AuditContext.for_admin(
        request=request,
        reason_code=body["reason"],
        request_key=body["requestKey"],
    )
    if request.method == "DELETE":
        try:
            row, _ = disable_schedule(
                schedule_id=schedule_id,
                expected_version=body["expectedVersion"],
                user=request.user,
                audit_context=audit_context,
            )
        except ValueError as exc:
            raise StateConflict(str(exc)) from exc
        return JsonResponse(_payload(row))
    mapping = {
        "name": "name",
        "cronExpression": "cron_expression",
        "timezone": "timezone",
        "windowMinutes": "window_minutes",
        "targetIds": "target_ids",
        "approvalMode": "approval_mode",
        "autoPublishValidationRefs": "auto_publish_validation_refs",
        "autoPublishActivationRefs": "auto_publish_activation_refs",
        "overlapPolicy": "overlap_policy",
        "enabled": "enabled",
    }
    changes = {
        field: body[key]
        for key, field in mapping.items()
        if key in body
    }
    try:
        row, _ = update_schedule(
            schedule_id=schedule_id,
            expected_version=body["expectedVersion"],
            changes=changes,
            user=request.user,
            audit_context=audit_context,
        )
    except ValueError as exc:
        if "stale" in str(exc) or "request_key" in str(exc):
            raise StateConflict(str(exc)) from exc
        raise InvalidInput(str(exc)) from exc
    return JsonResponse(_payload(row))


def _kill_switch_payload(control, decision=None):
    return {
        "decisionId": str(decision.id) if decision is not None else None,
        "enabled": bool(control.enabled) if control is not None else True,
        "version": int(control.version) if control is not None else 1,
        "reason": str(control.reason or "") if control is not None else "",
        "changedAt": (
            control.updated_at.isoformat() if control is not None else None
        ),
    }


@login_required
@openapi_operations({"GET": "getKillSwitch", "PUT": "setKillSwitch"})
@require_http_methods(["GET", "PUT"])
def kill_switch(request):
    if request.method == "PUT":
        body = request.openapi_body
        audit_context = AuditContext.for_admin(
            request=request,
            reason_code=body["reason"],
            request_key=body["requestKey"],
        )
        try:
            decision = set_kill_switch(
                enabled=body["enabled"],
                expected_version=body["expectedVersion"],
                request_key=body["requestKey"],
                reason=body["reason"],
                user=request.user,
                request=request,
                reauth_proof_id=body.get("reauthProofId"),
                audit_context=audit_context,
            )
        except ValueError as exc:
            raise StateConflict(str(exc)) from exc
        control = OperationalControl.objects.get(key="global_kill_switch")
        return JsonResponse(_kill_switch_payload(control, decision))
    control = OperationalControl.objects.filter(
        key="global_kill_switch"
    ).first()
    decision = (
        KillSwitchDecision.objects.order_by("-decided_at", "-id").first()
        if control is not None
        else None
    )
    return JsonResponse(_kill_switch_payload(control, decision))
