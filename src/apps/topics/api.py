from __future__ import annotations

import uuid
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.audit.services import AuditContext
from wisdome_writer.api.openapi import openapi_operation, openapi_operations

from .models import (
    SourceDefinition,
    SourceRegistryDecision,
    SourceRegistrySnapshot,
    TopicPolicy,
)
from .services import (
    create_registry_draft,
    create_source_definition,
    decide_source_registry,
    request_source_check,
    update_registry_membership,
    update_source_definition,
)


def _source_status(source: SourceDefinition) -> str:
    if source.latest_draft_snapshot_id is not None:
        return "draft"
    if source.latest_approved_snapshot_version is not None:
        return "approved"
    return "retired"


def _source_payload(source: SourceDefinition) -> dict[str, Any]:
    return {
        "topic": source.topic_code,
        "name": source.display_name,
        "publisher": source.publisher,
        "authorityTier": source.authority_tier,
        "independenceGroupId": source.independence_group,
        "ownerName": source.owner_name,
        "editorialControlName": source.editorial_control_name,
        "baseUrl": source.base_url,
        "accessMethod": source.access_method,
        "adapterKey": source.adapter_key,
        "externalConfig": source.external_config,
        "secretRef": source.secret_ref,
        "allowedMimeTypes": source.allowed_mime_types,
        "defaultRightsStatus": source.default_rights_status,
        "termsUrl": source.terms_url,
        "robotsUrl": source.robots_url,
        "licenseUrl": source.license_url,
        "pollIntervalSeconds": source.poll_interval_seconds,
        "rateLimitPolicy": source.rate_limit_policy,
        "enabled": source.enabled,
        "id": str(source.id),
        "latestApprovedSnapshotVersion": source.latest_approved_snapshot_version,
        "latestDraftSnapshotId": (
            str(source.latest_draft_snapshot_id)
            if source.latest_draft_snapshot_id
            else None
        ),
        "latestDraftSnapshotVersion": source.latest_draft_snapshot_version,
        "latestDraftConfigHash": source.latest_draft_config_hash,
        "status": _source_status(source),
        "lastHealth": source.last_health,
    }


def _registry_queryset():
    return SourceRegistrySnapshot.objects.select_related(
        "base_approved_registry",
        "latest_decision",
    ).prefetch_related("memberships__source_snapshot")


def _registry_payload(registry: SourceRegistrySnapshot) -> dict[str, Any]:
    return {
        "id": str(registry.id),
        "topic": registry.topic_code,
        "version": registry.version,
        "rowVersion": registry.row_version,
        "status": registry.state,
        "baseApprovedRegistryId": (
            str(registry.base_approved_registry_id)
            if registry.base_approved_registry_id
            else None
        ),
        "baseApprovedVersion": registry.base_approved_version,
        "baseApprovedManifestHash": registry.base_approved_manifest_hash,
        "manifestHash": registry.manifest_hash,
        "latestDecisionId": (
            str(registry.latest_decision_id)
            if registry.latest_decision_id
            else None
        ),
        "memberships": [
            {
                "sourceId": str(membership.source_definition_id),
                "sourceDefinitionSnapshotId": str(
                    membership.source_snapshot_id
                ),
                "sourceDefinitionSnapshotVersion": (
                    membership.source_snapshot.version
                ),
                "sourceDefinitionSnapshotStatus": (
                    membership.source_snapshot.state
                ),
                "sourceDefinitionConfigHash": (
                    membership.source_snapshot.config_hash
                ),
                "enabled": membership.enabled,
                "displayOrder": membership.display_order,
            }
            for membership in registry.memberships.all()
        ],
        "createdAt": registry.created_at.isoformat(),
    }


def _decision_payload(
    decision: SourceRegistryDecision,
) -> dict[str, Any]:
    return {
        "id": str(decision.id),
        "registryId": str(decision.registry_id),
        "version": decision.version,
        "decision": decision.decision,
        "expectedRowVersion": decision.expected_row_version,
        "expectedManifestHash": decision.expected_manifest_hash,
        "expectedCurrentHeadRegistryId": (
            str(decision.expected_current_head_registry_id)
            if decision.expected_current_head_registry_id
            else None
        ),
        "expectedCurrentHeadVersion": (
            decision.expected_current_head_version
        ),
        "expectedCurrentHeadManifestHash": (
            decision.expected_current_head_manifest_hash
        ),
        "supersedesDecisionId": (
            str(decision.supersedes_decision_id)
            if decision.supersedes_decision_id
            else None
        ),
        "requestKey": decision.request_key,
        "decisionHash": decision.decision_hash,
        "decidedBy": str(decision.decided_by_id),
        "decidedAt": decision.decided_at.isoformat(),
        "reason": decision.reason,
    }


def _admin_audit_context(
    request,
    *,
    reason: str,
    request_key: str,
) -> AuditContext:
    return AuditContext.for_admin(
        request=request,
        reason_code=reason,
        request_key=request_key,
    )


@login_required
@openapi_operation("listTopics")
@require_GET
def topics(request):
    rows = TopicPolicy.objects.filter(active=True).order_by(
        "code",
        "-version",
    )
    seen: set[str] = set()
    payload: list[dict[str, Any]] = []
    for row in rows:
        if row.code in seen:
            continue
        seen.add(row.code)
        payload.append(
            {
                "code": row.code,
                "name": row.title,
                "policyVersion": row.version,
                "autoPublishEligible": bool(
                    row.policy.get("autoPublishEligible", False)
                ),
            }
        )
    return JsonResponse(
        payload,
        safe=False,
    )


@login_required
@openapi_operations(
    {
        "GET": "listSources",
        "POST": "createSource",
    }
)
@require_http_methods(["GET", "POST"])
def sources(request):
    if request.method == "POST":
        body = request.openapi_body
        source, created = create_source_definition(
            body,
            admin=request.user,
            audit_context=_admin_audit_context(
                request,
                reason="source definition create",
                request_key=body["requestKey"],
            ),
            request_hash=request.openapi_request_identity,
        )
        return JsonResponse(
            _source_payload(source),
            status=201 if created else 200,
        )

    query = request.openapi_query
    queryset = SourceDefinition.objects.all()
    if query.get("topic") is not None:
        queryset = queryset.filter(topic_code=query["topic"])
    if query.get("enabled") is not None:
        queryset = queryset.filter(enabled=query["enabled"])
    return JsonResponse(
        [_source_payload(source) for source in queryset],
        safe=False,
    )


@login_required
@openapi_operations(
    {
        "GET": "getSource",
        "PATCH": "updateSource",
    }
)
@require_http_methods(["GET", "PATCH"])
def source_detail(request, source_id):
    validated_source_id = request.openapi_path["sourceId"]
    if request.method == "PATCH":
        body = request.openapi_body
        source = update_source_definition(
            validated_source_id,
            body,
            admin=request.user,
            audit_context=_admin_audit_context(
                request,
                reason="source definition update",
                request_key=body["requestKey"],
            ),
            request_hash=request.openapi_request_identity,
        )
    else:
        source = get_object_or_404(
            SourceDefinition,
            pk=validated_source_id,
        )
    return JsonResponse(_source_payload(source))


@login_required
@openapi_operation("checkSource")
@require_POST
def source_check(request, source_id):
    request_key = str(uuid.uuid4())
    event = request_source_check(
        request.openapi_path["sourceId"],
        admin=request.user,
        audit_context=_admin_audit_context(
            request,
            reason="source access check",
            request_key=request_key,
        ),
    )
    return JsonResponse(
        {
            "jobId": str(event.job_id),
            "correlationId": str(event.correlation_id),
            "acceptedAt": event.created_at.isoformat(),
        },
        status=202,
    )


@login_required
@openapi_operations(
    {
        "GET": "listSourceRegistries",
        "POST": "createSourceRegistryDraft",
    }
)
@require_http_methods(["GET", "POST"])
def source_registries(request):
    if request.method == "POST":
        body = request.openapi_body
        registry, created = create_registry_draft(
            body,
            admin=request.user,
            audit_context=_admin_audit_context(
                request,
                reason="source registry draft create",
                request_key=body["requestKey"],
            ),
            request_hash=request.openapi_request_identity,
        )
        registry = get_object_or_404(_registry_queryset(), pk=registry.pk)
        return JsonResponse(
            _registry_payload(registry),
            status=201 if created else 200,
        )

    queryset = _registry_queryset()
    topic = request.openapi_query.get("topic")
    if topic is not None:
        queryset = queryset.filter(topic_code=topic)
    return JsonResponse(
        [_registry_payload(registry) for registry in queryset],
        safe=False,
    )


@login_required
@openapi_operation("getSourceRegistry")
@require_GET
def source_registry_detail(request, registry_id):
    registry = get_object_or_404(
        _registry_queryset(),
        pk=request.openapi_path["registryId"],
    )
    return JsonResponse(_registry_payload(registry))


@login_required
@openapi_operation("updateSourceRegistryMembership")
@require_http_methods(["PUT"])
def source_registry_membership(request, registry_id, source_id):
    body = request.openapi_body
    registry, _created = update_registry_membership(
        request.openapi_path["registryId"],
        request.openapi_path["sourceId"],
        body,
        admin=request.user,
        audit_context=_admin_audit_context(
            request,
            reason="source registry membership update",
            request_key=body["requestKey"],
        ),
        request_hash=request.openapi_request_identity,
    )
    registry = get_object_or_404(_registry_queryset(), pk=registry.pk)
    return JsonResponse(_registry_payload(registry))


@login_required
@openapi_operation("decideSourceRegistry")
@require_POST
def source_registry_decisions(request, registry_id):
    body = request.openapi_body
    decision, created = decide_source_registry(
        request.openapi_path["registryId"],
        body,
        admin=request.user,
        request=request,
        audit_context=_admin_audit_context(
            request,
            reason=body["reason"],
            request_key=body["requestKey"],
        ),
        request_hash=request.openapi_request_identity,
    )
    return JsonResponse(
        _decision_payload(decision),
        status=201 if created else 200,
    )
