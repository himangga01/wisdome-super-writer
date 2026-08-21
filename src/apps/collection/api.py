from datetime import timedelta

from django.apps import apps
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import CollectionRun, RecoveryState, RunState
from .services import (
    create_run,
    request_run_stop,
    request_selective_retry,
)
from apps.audit.services import AuditContext
from wisdome_writer.api.openapi import openapi_operation, openapi_operations
from wisdome_writer.domain.errors import InvalidInput, StateConflict
from wisdome_writer.infrastructure.outbox import enqueue_event


def _run_payload(run):
    return {
        "id": str(run.id),
        "displayId": run.display_id,
        "topic": run.topic_code,
        "trigger": run.trigger,
        "approvalMode": run.approval_mode,
        "state": run.state,
        "windowStart": run.window_start.isoformat(),
        "windowEnd": run.window_end.isoformat(),
        "sourceRegistrySnapshotId": str(run.source_registry_id),
        "registryVersion": run.source_registry.version,
        "registryManifestHash": run.registry_manifest_hash,
        "topicPolicyId": str(run.topic_policy_id),
        "policyVersion": run.policy_version,
        "policyHash": run.policy_hash,
        "freshnessMinutes": run.freshness_minutes,
        "allowedAuthorityTiers": run.allowed_authority_tiers,
        "freshnessCutoff": run.freshness_cutoff.isoformat(),
        "requestedTargetIds": run.requested_target_ids,
        "requestFingerprint": run.request_fingerprint,
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
        "stopRequestedAt": _timestamp(run.stop_requested_at),
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


def _timestamp(value):
    return value.isoformat() if value else None


def _observation_payload(observation):
    return {
        "id": str(observation.id),
        "deliveryAttemptNo": observation.delivery_attempt_no,
        "outcome": observation.outcome,
        "failureCategory": observation.failure_category or None,
        "errorCode": observation.error_code,
        # This field is redacted before it is persisted; never expose a raw
        # exception message or request material from this read API.
        "errorDetail": observation.error_detail_redacted,
        "httpStatus": observation.http_status,
        "retryCount": observation.retry_count,
        "retryAt": _timestamp(observation.retry_at),
        "retryAfterSeconds": observation.retry_after_seconds,
        "requestCount": observation.request_count,
        "freshnessExcludedCount": (
            observation.freshness_excluded_count
        ),
        "durationMs": observation.duration_ms,
        "accessPolicyHash": observation.access_policy_hash or None,
        "authorityTier": observation.authority_tier or None,
        "freshnessCutoff": _timestamp(observation.freshness_cutoff),
        "recordedAt": observation.recorded_at.isoformat(),
    }


def _attempt_payload(attempt):
    return {
        "id": str(attempt.id),
        "sourceSnapshotId": str(attempt.source_snapshot_id),
        "adapterName": attempt.adapter_name,
        "adapterVersion": attempt.adapter_version,
        "adapterImplementationManifestHash": (
            attempt.adapter_implementation_manifest_hash
        ),
        "adapterConfigHash": attempt.adapter_config_hash,
        "requestFingerprint": attempt.request_fingerprint,
        "requestWindowStart": _timestamp(attempt.request_window_start),
        "requestWindowEnd": _timestamp(attempt.request_window_end),
        "state": attempt.state,
        "failureCategory": attempt.failure_category or None,
        "errorCode": attempt.error_code,
        "errorDetail": attempt.error_detail_redacted,
        "httpStatus": attempt.http_status,
        "retryCount": attempt.retry_count,
        "retryAt": _timestamp(attempt.retry_at),
        "retryAfterSeconds": attempt.retry_after_seconds,
        "requestCount": attempt.request_count,
        "responseCount": attempt.response_count,
        "responseChecksum": attempt.response_checksum,
        "durationMs": attempt.duration_ms,
        "accessPolicyHash": attempt.access_policy_hash or None,
        "rightsPolicyHash": attempt.rights_policy_hash or None,
        "authorityTier": attempt.authority_tier or None,
        "freshnessCutoff": _timestamp(attempt.freshness_cutoff),
        "freshnessExcludedCount": attempt.freshness_excluded_count,
        "startedAt": _timestamp(attempt.started_at),
        "finishedAt": _timestamp(attempt.finished_at),
        "observations": [
            _observation_payload(observation)
            for observation in attempt.observations.all()
        ],
    }


def _step_payload(step):
    return {
        "name": step.name,
        "attemptNo": step.attempt_no,
        "state": step.state,
        "inputCount": step.input_count,
        "outputCount": step.output_count,
        "errorCode": step.error_code,
        "errorDetail": step.error_detail_redacted,
        "startedAt": _timestamp(step.started_at),
        "fanoutCompletedAt": _timestamp(step.fanout_completed_at),
        "finishedAt": _timestamp(step.finished_at),
        "durationMs": step.duration_ms,
        "retryCount": step.retry_count,
        "retryAt": _timestamp(step.retry_at),
        "terminalImpact": step.terminal_impact,
        "recoveryState": step.recovery_state,
    }


def _run_control_payload(decision):
    return {
        "id": str(decision.id),
        "runId": str(decision.run_id),
        "action": decision.action,
        "scope": decision.scope,
        "requestKey": decision.request_key,
        "reauthProofId": (
            str(decision.reauth_proof_id)
            if decision.reauth_proof_id
            else None
        ),
        "decidedBy": str(decision.decided_by_id),
        "decidedAt": decision.decided_at.isoformat(),
    }


def _retry_target_payload(target):
    if target is None:
        return None
    model_name = type(target).__name__
    kind = {
        "SourceCollectionAttempt": "source_attempt",
        "DocumentExtraction": "document_extraction",
        "PublicationAttempt": "publication_attempt",
    }.get(model_name)
    if kind is None:
        kind = "publication_attempt" if hasattr(target, "publication") else "source_attempt"
    return {
        "kind": kind,
        "id": str(target.id),
        "state": str(target.state),
    }


def _run_recovery_payload(run, *, document_rows, publication_rows):
    scopes = []
    if run.stop_requested_at is None:
        scopes.extend(
            {"sourceAttemptId": str(row.id)}
            for row in run.collection_attempts.all()
            if row.state == "failed"
        )
        scopes.extend(
            {"documentExtractionId": str(row.id)}
            for row in document_rows
            if row.state == "failed"
        )
        retryable_publication_states = {
            "retryable_failed",
            "unknown_outcome",
            "reconciling",
            "permanent_failed",
            "manual_required",
            "stale",
        }
        for row in publication_rows:
            if row.state not in retryable_publication_states:
                continue
            scopes.append({"publicationAttemptId": str(row.id)})
            scopes.append({"targetId": str(row.publication.target_id)})
    return {
        "state": getattr(run, "recovery_state", RecoveryState.IN_PROGRESS),
        "terminalImpact": getattr(run, "terminal_impact", {}),
        "nextRecoveryAt": _timestamp(getattr(run, "next_recovery_at", None)),
        "stopRequestedAt": _timestamp(run.stop_requested_at),
        "allowedRetryScopes": scopes,
        "canStop": (
            run.stop_requested_at is None
            and getattr(run, "state", None)
            not in {RunState.COMPLETED, RunState.FAILED, RunState.STOPPED}
        ),
    }


def _locked_run_for_api(run_id):
    return get_object_or_404(
        CollectionRun.objects.select_related(
            "source_registry",
            "topic_policy",
        ),
        id=run_id,
    )


@login_required
@openapi_operations({"GET": "listRuns", "POST": "createRun"})
@require_http_methods(["GET", "POST"])
def runs(request):
    if request.method == "GET":
        query = request.openapi_query
        queryset = CollectionRun.objects.select_related(
            "source_registry",
            "topic_policy",
        )
        if query.get("topic"):
            queryset = queryset.filter(topic_code=query["topic"])
        if query.get("state"):
            queryset = queryset.filter(state=query["state"])
        limit = int(query.get("limit") or 50)
        rows = list(queryset[:limit])
        return JsonResponse(
            {"items": [_run_payload(run) for run in rows], "nextCursor": None}
        )
    body = request.openapi_body
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
            requested_target_ids=body["targetIds"],
            approval_mode=body["approvalMode"],
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
@openapi_operation("getRun")
@require_http_methods(["GET"])
def run_detail(request, run_id):
    run = get_object_or_404(
        CollectionRun.objects.select_related(
            "source_registry",
            "topic_policy",
        ).prefetch_related(
            "steps",
            "run_source_items__source_item",
            "collection_attempts__observations",
        ),
        id=run_id,
    )
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
    payload["steps"] = [
        _step_payload(step)
        for step in run.steps.all().order_by("name", "attempt_no")
    ]
    payload["sourceAttempts"] = [
        _attempt_payload(attempt)
        for attempt in run.collection_attempts.all().order_by(
            "source_snapshot_id"
        )
    ]
    DocumentExtraction = apps.get_model("evidence", "DocumentExtraction")
    document_rows = list(
        DocumentExtraction.objects.filter(
            run_source_item__run_id=run.id
        ).order_by("id")[:200]
    )
    PublicationAttempt = apps.get_model("publishing", "PublicationAttempt")
    publication_rows = list(
        PublicationAttempt.objects.filter(
            publication_intent__origin_collection_run_id=run.id
        )
        .select_related("publication")
        .order_by("id")[:200]
    )
    payload["recovery"] = _run_recovery_payload(
        run,
        document_rows=document_rows,
        publication_rows=publication_rows,
    )
    return JsonResponse(payload)


@login_required
@openapi_operation("stopRun")
@require_http_methods(["POST"])
def stop_run(request, run_id):
    body = request.openapi_body
    audit_context = AuditContext.for_admin(
        request=request,
        reason_code=body["reason"],
        request_key=body["requestKey"],
    )
    try:
        decision, created = request_run_stop(
            run_id=run_id,
            expected_state=body["expectedState"],
            request_key=body["requestKey"],
            reason=body["reason"],
            user=request.user,
            request=request,
            reauth_proof_id=body["reauthProofId"],
            audit_context=audit_context,
        )
    except ValueError as exc:
        raise StateConflict(str(exc)) from exc
    run = _locked_run_for_api(run_id)
    return JsonResponse(
        {
            "decision": _run_control_payload(decision),
            "created": created,
            "run": _run_payload(run),
            "retryTarget": None,
        },
        status=202 if created else 200,
    )


@login_required
@openapi_operation("retryRun")
@require_http_methods(["POST"])
def retry_run(request, run_id):
    body = request.openapi_body
    audit_context = AuditContext.for_admin(
        request=request,
        reason_code=body["reason"],
        request_key=body["requestKey"],
    )
    try:
        decision, target, created = request_selective_retry(
            run_id=run_id,
            scope=body["scope"],
            request_key=body["requestKey"],
            reason=body["reason"],
            user=request.user,
            reauth_proof_id=body["reauthProofId"],
            request=request,
            audit_context=audit_context,
        )
    except ValueError as exc:
        message = str(exc)
        if "conflict" in message or "cannot" in message or "requires" in message:
            raise StateConflict(message) from exc
        raise InvalidInput(message) from exc
    run = _locked_run_for_api(run_id)
    return JsonResponse(
        {
            "decision": _run_control_payload(decision),
            "created": created,
            "run": _run_payload(run),
            "retryTarget": _retry_target_payload(target),
        },
        status=202 if created else 200,
    )
