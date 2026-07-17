import json
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.services import consume_reauthentication_proof

from .models import OperationalControl, Schedule
from .services import calculate_next_run, set_kill_switch


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
    row = Schedule(
        name=body["name"],
        topic_code=body["topic"],
        cron_expression=body["cronExpression"],
        timezone=body.get("timezone", "Asia/Seoul"),
        window_minutes=body.get("windowMinutes", 1440),
        target_ids=body.get("targetIds", []),
        approval_mode=body.get("approvalMode", "manual"),
        auto_publish_validation_refs=body.get("autoPublishValidationRefs", []),
        auto_publish_activation_refs=body.get("autoPublishActivationRefs", []),
        overlap_policy=body.get("overlapPolicy", "skip"),
        enabled=body.get("enabled", False),
        updated_by=request.user,
    )
    row.next_run_at = calculate_next_run(row, timezone.now())
    row.save()
    return JsonResponse(_payload(row), status=201)


@login_required
@require_http_methods(["GET", "PATCH", "DELETE"])
def schedule_detail(request, schedule_id):
    row = get_object_or_404(Schedule, id=schedule_id)
    if request.method == "DELETE":
        row.enabled = False
        row.save(update_fields=["enabled"])
        return JsonResponse(_payload(row))
    if request.method == "PATCH":
        body = json.loads(request.body or b"{}")
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
        for key, field in mapping.items():
            if key in body:
                setattr(row, field, body[key])
        row.version += 1
        row.updated_by = request.user
        row.next_run_at = calculate_next_run(row)
        row.save()
    return JsonResponse(_payload(row))


@login_required
@require_http_methods(["GET", "PUT"])
@transaction.atomic
def kill_switch(request):
    control, _ = OperationalControl.objects.get_or_create(key="global_kill_switch")
    if request.method == "PUT":
        body = json.loads(request.body or b"{}")
        if body["enabled"] is False:
            consume_reauthentication_proof(
                request=request,
                proof_id=body["reauthProofId"],
                action_scope="kill_switch_disable",
                entity_type="operational_control",
                entity_id=UUID("00000000-0000-4000-8000-000000000001"),
            )
        try:
            set_kill_switch(
                enabled=body["enabled"],
                expected_version=body["expectedVersion"],
                request_key=body["requestKey"],
                reason=body["reason"],
                user=request.user,
            )
        except ValueError:
            transaction.set_rollback(True)
            return JsonResponse({"detail": "stale_control_version"}, status=409)
        control.refresh_from_db()
    return JsonResponse(
        {"enabled": control.enabled, "version": control.version, "reason": control.reason}
    )
