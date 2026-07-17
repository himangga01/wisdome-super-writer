import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from .models import SourceDefinition, SourceRegistrySnapshot, TopicPolicy
from .services import approve_registry


@login_required
def topics(request):
    items = [
        {
            "code": row.code,
            "version": row.version,
            "title": row.title,
            "freshnessMinutes": row.freshness_minutes,
            "active": row.active,
        }
        for row in TopicPolicy.objects.filter(active=True).order_by("code", "-version")
    ]
    return JsonResponse({"items": items})


@login_required
def sources(request):
    topic = request.GET.get("topic")
    queryset = SourceDefinition.objects.all()
    if topic:
        queryset = queryset.filter(topic_code=topic)
    return JsonResponse(
        {
            "items": [
                {
                    "id": str(row.id),
                    "topic": row.topic_code,
                    "key": row.key,
                    "displayName": row.display_name,
                    "baseUrl": row.base_url,
                    "authorityTier": row.authority_tier,
                    "enabled": row.enabled,
                }
                for row in queryset
            ]
        }
    )


@login_required
@require_http_methods(["GET", "POST"])
def registry_detail(request, registry_id):
    registry = SourceRegistrySnapshot.objects.prefetch_related(
        "memberships__source_snapshot__source"
    ).get(id=registry_id)
    if request.method == "POST":
        body = json.loads(request.body or b"{}")
        if body.get("decision") != "approved":
            return JsonResponse({"detail": "Only approved decision is supported."}, status=422)
        approve_registry(registry, request.user)
    return JsonResponse(
        {
            "id": str(registry.id),
            "topic": registry.topic_code,
            "version": registry.version,
            "state": registry.state,
            "manifestHash": registry.manifest_hash,
            "sources": [
                {
                    "snapshotId": str(member.source_snapshot_id),
                    "key": member.source_snapshot.source.key,
                    "enabled": member.enabled,
                }
                for member in registry.memberships.all()
            ],
        }
    )
