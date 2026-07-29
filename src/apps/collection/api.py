import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import CollectionRun, RecoveryState, RunState
from .services import create_run, project_run_terminal_observation
from wisdome_writer.infrastructure.outbox import enqueue_event


def _run_payload(run):
    return {
        "id": str(run.id),
        "displayId": run.display_id,
        "topic": run.topic_code,
        "trigger": run.trigger,
        "state": run.state,
        "windowStart": run.window_start.isoformat(),
        "windowEnd": run.window_end.isoformat(),
        "counters": run.counters,
        "errorSummary": run.error_summary,
        "correlationId": str(run.correlation_id),
        "durationMs": run.duration_ms,
        "retryCount": run.retry_count,
        "terminalImpact": run.terminal_impact,
        "recoveryState": run.recovery_state,
        "nextRecoveryAt": (
            run.next_recovery_at.isoformat()
            if run.next_recovery_at
            else None
        ),
        "createdAt": run.created_at.isoformat(),
        "startedAt": (
            run.started_at.isoformat()
            if run.started_at
            else None
        ),
        "completedAt": (
            run.completed_at.isoformat()
            if run.completed_at
            else None
        ),
    }


@login_required
@require_http_methods(["GET", "POST"])
def runs(request):
    if request.method == "GET":
        queryset = CollectionRun.objects.all()[:100]
        return JsonResponse({"items": [_run_payload(run) for run in queryset]})
    body = json.loads(request.body or b"{}")
    now = timezone.now()
    window_end = timezone.datetime.fromisoformat(body["windowEnd"]) if body.get("windowEnd") else now
    if timezone.is_naive(window_end):
        window_end = timezone.make_aware(window_end)
    window_start = (
        timezone.datetime.fromisoformat(body["windowStart"])
        if body.get("windowStart")
        else window_end - timedelta(hours=24)
    )
    if timezone.is_naive(window_start):
        window_start = timezone.make_aware(window_start)
    with transaction.atomic():
        run, created = create_run(
            topic_code=body["topic"],
            window_start=window_start,
            window_end=window_end,
            user=request.user,
            correlation_id=getattr(request, "correlation_id", None),
        )
        if created:
            enqueue_event(
                event_type="run.requested",
                aggregate_type="collection_run",
                aggregate_id=run.id,
                job_id=run.id,
                dedupe_key=f"run.requested:{run.id}",
                payload={"run_id": str(run.id)},
                correlation_id=run.correlation_id,
            )
    return JsonResponse(_run_payload(run), status=201 if created else 200)


@login_required
def run_detail(request, run_id):
    run = get_object_or_404(CollectionRun, id=run_id)
    payload = _run_payload(run)
    payload["sources"] = [
        {
            "id": str(link.source_item_id),
            "title": link.source_item.title,
            "url": link.source_item.canonical_url,
            "status": link.source_item.status,
            "sourceVersionSchema": (
                link.source_item.source_version_schema
            ),
            "discoveryKind": link.discovery_kind,
            "previousRunSourceItemId": (
                str(link.previous_run_source_item_id)
                if link.previous_run_source_item_id
                else None
            ),
        }
        for link in run.run_source_items.select_related("source_item")
    ]
    return JsonResponse(payload)


@login_required
@require_http_methods(["POST"])
def stop_run(request, run_id):
    response_status = 200
    with transaction.atomic():
        run = get_object_or_404(
            CollectionRun.objects.select_for_update(),
            id=run_id,
        )
        if run.state not in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.STOPPED,
        }:
            previous_state = run.state
            if previous_state in {
                RunState.QUEUED,
                RunState.AWAITING_APPROVAL,
            }:
                now = timezone.now()
                run.stop_requested_at = (
                    run.stop_requested_at or now
                )
                run.state = RunState.STOPPED
                project_run_terminal_observation(
                    run,
                    finished_at=now,
                    stage=previous_state,
                    final_state=run.state,
                    error_code="stop_requested",
                    recovery_state=RecoveryState.STOPPED,
                )
                run.save(
                    update_fields=[
                        "state",
                        "stop_requested_at",
                        "completed_at",
                        "duration_ms",
                        "terminal_impact",
                        "recovery_state",
                        "next_recovery_at",
                    ]
                )
            elif previous_state == RunState.COLLECTING:
                run.stop_requested_at = (
                    run.stop_requested_at or timezone.now()
                )
                run.state = RunState.STOPPING
                run.save(update_fields=["state", "stop_requested_at"])
            elif previous_state == RunState.EXTRACTING:
                run.stop_requested_at = (
                    run.stop_requested_at or timezone.now()
                )
                run.state = RunState.STOPPING
                run.save(update_fields=["state", "stop_requested_at"])
                enqueue_event(
                    event_type="evidence.finalize_requested",
                    aggregate_type="collection_run",
                    aggregate_id=run.id,
                    job_id=run.id,
                    dedupe_key=(
                        f"evidence.finalize_requested:stop:{run.id}"
                    ),
                    payload={"run_id": str(run.id)},
                    correlation_id=run.correlation_id,
                )
            elif previous_state == RunState.STOPPING:
                pass
            else:
                response_status = 409
    payload = _run_payload(run)
    if response_status == 409:
        payload["error"] = "stop_not_supported_for_active_phase"
    return JsonResponse(payload, status=response_status)


@login_required
@require_http_methods(["POST"])
def retry_run(request, run_id):
    old = get_object_or_404(CollectionRun, id=run_id)
    with transaction.atomic():
        run, created = create_run(
            topic_code=old.topic_code,
            window_start=old.window_start,
            window_end=old.window_end,
            user=request.user,
            trigger="retry",
            correlation_id=old.correlation_id,
            retry_count=old.retry_count + 1,
        )
        if created:
            enqueue_event(
                event_type="run.requested",
                aggregate_type="collection_run",
                aggregate_id=run.id,
                job_id=run.id,
                dedupe_key=f"run.requested:{run.id}",
                payload={"run_id": str(run.id)},
                correlation_id=run.correlation_id,
            )
    return JsonResponse(_run_payload(run), status=202)
