import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods

from apps.audit.services import AuditContext

from .models import OperationalControl, Schedule
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
    }


@login_required
@require_http_methods(["GET", "POST"])
def schedules(request):
    if request.method == "GET":
        return JsonResponse({"items": [_payload(row) for row in Schedule.objects.all()]})
    body = json.loads(request.body or b"{}")
    required = {"name", "topic", "cronExpression", "requestKey", "reason"}
    if missing := required - set(body):
        return JsonResponse(
            {"detail": f"Missing fields: {', '.join(sorted(missing))}"},
            status=422,
        )
    audit_context = AuditContext.for_admin(
        request=request,
        reason_code=body["reason"],
        request_key=body["requestKey"],
    )
    row, created = create_schedule(
        values={
            "name": body["name"],
            "topic_code": body["topic"],
            "cron_expression": body["cronExpression"],
            "timezone": body.get("timezone", "Asia/Seoul"),
            "window_minutes": body.get("windowMinutes", 1440),
            "target_ids": body.get("targetIds", []),
            "approval_mode": body.get("approvalMode", "manual"),
            "auto_publish_validation_refs": body.get(
                "autoPublishValidationRefs", []
            ),
            "auto_publish_activation_refs": body.get(
                "autoPublishActivationRefs", []
            ),
            "overlap_policy": body.get("overlapPolicy", "skip"),
            "enabled": body.get("enabled", False),
        },
        user=request.user,
        audit_context=audit_context,
    )
    return JsonResponse(_payload(row), status=201 if created else 200)


@login_required
@require_http_methods(["GET", "PATCH", "DELETE"])
def schedule_detail(request, schedule_id):
    row = get_object_or_404(Schedule, id=schedule_id)
    if request.method == "DELETE":
        body = json.loads(request.body or b"{}")
        required = {"requestKey", "reason"}
        if missing := required - set(body):
            return JsonResponse(
                {"detail": f"Missing fields: {', '.join(sorted(missing))}"},
                status=422,
            )
        audit_context = AuditContext.for_admin(
            request=request,
            reason_code=body["reason"],
            request_key=body["requestKey"],
        )
        row, _ = disable_schedule(
            schedule_id=row.id,
            user=request.user,
            audit_context=audit_context,
        )
        return JsonResponse(_payload(row))
    if request.method == "PATCH":
        body = json.loads(request.body or b"{}")
        required = {"requestKey", "reason"}
        if missing := required - set(body):
            return JsonResponse(
                {"detail": f"Missing fields: {', '.join(sorted(missing))}"},
                status=422,
            )
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
        audit_context = AuditContext.for_admin(
            request=request,
            reason_code=body["reason"],
            request_key=body["requestKey"],
        )
        row, _ = update_schedule(
            schedule_id=row.id,
            changes=changes,
            user=request.user,
            audit_context=audit_context,
        )
    return JsonResponse(_payload(row))


@login_required
@require_http_methods(["GET", "PUT"])
def kill_switch(request):
    if request.method == "PUT":
        body = json.loads(request.body or b"{}")
        required = {"enabled", "expectedVersion", "requestKey", "reason"}
        if missing := required - set(body):
            return JsonResponse(
                {"detail": f"Missing fields: {', '.join(sorted(missing))}"},
                status=422,
            )
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
            return JsonResponse({"detail": str(exc)}, status=409)
        return JsonResponse(
            {
                "enabled": decision.enabled,
                "version": decision.expected_version + 1,
                "reason": decision.reason,
            }
        )
    control = OperationalControl.objects.filter(
        key="global_kill_switch"
    ).first()
    if control is None:
        return JsonResponse({"enabled": True, "version": 1, "reason": ""})
    return JsonResponse(
        {"enabled": control.enabled, "version": control.version, "reason": control.reason}
    )
