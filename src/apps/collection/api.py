import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import CollectionRun, RunState
from .services import create_run
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
        "createdAt": run.created_at.isoformat(),
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
            topic_code=body["topic"], window_start=window_start, window_end=window_end, user=request.user
        )
        if created:
            enqueue_event(
                event_type="run.requested",
                aggregate_type="collection_run",
                aggregate_id=run.id,
                job_id=run.id,
                dedupe_key=f"run.requested:{run.id}",
                payload={"run_id": str(run.id)},
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
            "discoveryKind": link.discovery_kind,
        }
        for link in run.run_source_items.select_related("source_item")
    ]
    return JsonResponse(payload)


@login_required
@require_http_methods(["POST"])
def stop_run(request, run_id):
    run = get_object_or_404(CollectionRun, id=run_id)
    if run.state not in {RunState.COMPLETED, RunState.FAILED, RunState.STOPPED}:
        run.state = RunState.STOPPING
        run.stop_requested_at = timezone.now()
        run.save(update_fields=["state", "stop_requested_at"])
    return JsonResponse(_run_payload(run))


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
        )
        if created:
            enqueue_event(
                event_type="run.requested",
                aggregate_type="collection_run",
                aggregate_id=run.id,
                job_id=run.id,
                dedupe_key=f"run.requested:{run.id}",
                payload={"run_id": str(run.id)},
            )
    return JsonResponse(_run_payload(run), status=202)
