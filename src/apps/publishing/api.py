from __future__ import annotations

import json
from functools import wraps
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from adapters.storage import S3ObjectStorage
from wisdome_writer.domain.errors import Conflict, DomainError, InvalidInput
from wisdome_writer.domain.hashing import sha256_hex
from wisdome_writer.infrastructure.outbox import enqueue_event

from .models import (
    Approval,
    ArticleChannelRender,
    AutoPublishActivation,
    AutoPublishValidation,
    AutoPublishValidationDecision,
    Publication,
    PublicationAttempt,
    PublicationIntent,
    PublicationTarget,
)
from .corrections import prepare_verified_correction
from .services import (
    _enqueue_reconcile_locked,
    create_auto_publish_validation,
    create_canary_run,
    create_publication_intent,
    create_target,
    complete_blogger_oauth,
    decide_approval,
    decide_auto_publish_validation,
    disconnect_target,
    dispatch_publication,
    set_auto_publish,
    start_blogger_oauth,
    update_target,
)


def admin_api(view):
    @wraps(view)
    @csrf_protect
    def wrapped(request: HttpRequest, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_active or not request.user.is_staff:
            return _problem(403, "forbidden", "관리자 로그인이 필요합니다.")
        if not request.headers.get("X-CSRFToken"):
            return _problem(403, "csrf_required", "모든 관리자 API 요청에 CSRF header가 필요합니다.")
        try:
            return view(request, *args, **kwargs)
        except DomainError as exc:
            return _problem(exc.status, exc.code, exc.detail or exc.title)
        except ObjectDoesNotExist:
            return _problem(404, "not_found", "대상을 찾을 수 없습니다.")
        except (KeyError, TypeError, ValueError) as exc:
            return _problem(422, "invalid_input", f"필수 입력값이 올바르지 않습니다: {exc}")

    return wrapped


def _problem(status: int, code: str, detail: str) -> JsonResponse:
    return JsonResponse(
        {
            "type": f"urn:wisdome-writer:problem:{code}",
            "title": code.replace("_", " "),
            "status": status,
            "detail": detail,
        },
        status=status,
    )


def _body(request: HttpRequest) -> dict[str, Any]:
    try:
        value = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidInput("JSON 요청 본문을 읽을 수 없습니다.") from exc
    if not isinstance(value, dict):
        raise InvalidInput("JSON object 요청 본문이 필요합니다.")
    return value


def target_json(row: PublicationTarget) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "channel": row.channel,
        "channelRole": row.role,
        "environment": row.environment,
        "displayName": row.display_name,
        "baseUrl": row.base_url,
        "remoteBlogId": row.remote_blog_id,
        "canaryTargetId": str(row.canary_target_id) if row.canary_target_id else None,
        "currentSnapshotId": str(row.current_snapshot_id) if row.current_snapshot_id else None,
        "currentSnapshotVersion": row.current_snapshot_version,
        "currentConfigHash": row.current_config_hash,
        "publisherContractVersion": row.publisher_contract_version,
        "publisherAdapterManifestHash": row.publisher_adapter_manifest_hash,
        "connectionState": row.connection_state,
        "preflightState": row.preflight_state,
        "canaryState": row.canary_state,
        "pilotState": row.pilot_state,
        "canaryPolicyVersion": row.canary_policy_version,
        "capabilities": row.capabilities,
        "autoPublishEnabled": row.auto_publish_enabled,
        "autoPublishActivationId": (
            str(row.latest_auto_publish_activation_id)
            if row.latest_auto_publish_activation_id
            else None
        ),
        "autoPublishActivationVersion": row.auto_publish_activation_version,
        "lastPreflightAt": row.last_preflight_at.isoformat() if row.last_preflight_at else None,
        "lastCanaryAt": row.last_canary_at.isoformat() if row.last_canary_at else None,
        "lastPilotAt": row.last_pilot_at.isoformat() if row.last_pilot_at else None,
    }


def validation_json(row: AutoPublishValidation) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "topic": row.topic_code,
        "targetSnapshotId": str(row.target_snapshot_id),
        "targetConfigHash": row.target_config_hash,
        "sourceRegistrySnapshotId": str(row.source_registry_snapshot_id),
        "registryManifestHash": row.registry_manifest_hash,
        "sourceAdapterManifestHash": row.source_adapter_manifest_hash,
        "extractionProfileManifestHash": row.extraction_profile_manifest_hash,
        "generationPipelineManifestHash": row.generation_pipeline_manifest_hash,
        "topicPolicyVersion": row.topic_policy_version,
        "editorialPolicyHash": row.editorial_policy_hash,
        "qualityGateManifestHash": row.quality_gate_manifest_hash,
        "renderContractVersion": row.render_contract_version,
        "channelContractVersion": row.channel_contract_version,
        "publisherAdapterManifestHash": row.publisher_adapter_manifest_hash,
        "testReportObjectKey": row.test_report_object_key,
        "testReportObjectVersion": row.test_report_object_version,
        "testReportHash": row.test_report_hash,
        "requestKey": row.request_key,
        "materialHash": row.material_hash,
        "status": row.status,
        "latestDecisionId": str(row.latest_decision_id) if row.latest_decision_id else None,
        "decisionVersion": row.decision_version,
        "createdAt": row.created_at.isoformat(),
    }


def activation_json(row: AutoPublishActivation) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "targetId": str(row.target_id),
        "targetSnapshotId": str(row.target_snapshot_id),
        "targetOperationalConfigHash": row.target_operational_config_hash,
        "validationRefs": row.validation_refs,
        "validationManifestHash": row.validation_manifest_hash,
        "version": row.version,
        "decision": row.decision,
        "supersedesActivationId": (
            str(row.supersedes_activation_id) if row.supersedes_activation_id else None
        ),
        "requestKey": row.request_key,
        "activationHash": row.activation_hash,
        "decidedAt": row.decided_at.isoformat(),
    }


def render_json(row: ArticleChannelRender) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "publicationIntentId": str(row.publication_intent_id),
        "targetId": str(row.target_id),
        "channelRole": row.channel_role,
        "renderStage": row.render_stage,
        "title": row.title,
        "bodyHtml": row.body_html,
        "sourceLinks": row.source_links,
        "includedClaimIds": row.included_claim_ids,
        "canonicalSourceUrl": row.canonical_source_url,
        "canonicalLinkState": row.canonical_link_state,
        "templateHash": row.template_hash,
        "contentHash": row.content_hash,
        "sourceManifestHash": row.source_manifest_hash,
        "media": row.media_manifest,
    }


def intent_json(row: PublicationIntent) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "articleId": str(row.article_id),
        "revisionNo": row.revision_no,
        "revisionContentHash": row.revision_content_hash,
        "generationAttemptId": str(row.generation_attempt_id) if row.generation_attempt_id else None,
        "inputEvidenceManifestHash": row.input_evidence_manifest_hash,
        "generationPipelineManifestHash": row.generation_pipeline_manifest_hash,
        "qualityGateManifestHash": row.quality_gate_manifest_hash,
        "qualityReportHash": row.quality_report_hash,
        "correctionCaseId": str(row.correction_case_id) if row.correction_case_id else None,
        "targetSnapshots": row.target_snapshot_refs,
        "targetCommands": row.target_commands,
        "targetSnapshotManifestHash": row.target_snapshot_manifest_hash,
        "approvalMode": row.approval_mode,
        "autoPublishValidationRefs": row.auto_publish_validation_refs,
        "autoPublishActivationRefs": row.auto_publish_activation_refs,
        "autoActivationManifestHash": row.auto_activation_manifest_hash,
        "supersedesIntentId": str(row.supersedes_intent_id) if row.supersedes_intent_id else None,
        "intentHash": row.intent_hash,
        "requestKey": row.request_key,
        "state": row.state,
        "createdAt": row.created_at.isoformat(),
        "renders": [render_json(render_row) for render_row in row.renders.all()],
        "approvals": [approval_json(approval_row) for approval_row in row.approvals.all()],
    }


def approval_json(row: Approval) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "revisionNo": row.revision_no,
        "publicationIntentId": str(row.publication_intent_id),
        "targetId": str(row.target_id),
        "approvalSubjectHash": row.approval_subject_hash,
        "supersedesApprovalId": str(row.supersedes_approval_id) if row.supersedes_approval_id else None,
        "requestKey": row.request_key,
        "reauthProofId": str(row.reauth_proof_id) if row.reauth_proof_id else None,
        "actionSubject": row.action_subject,
        "mode": row.mode,
        "decision": row.decision,
        "decidedAt": row.decided_at.isoformat(),
    }


def publication_json(row: Publication) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "articleId": str(row.article_id),
        "targetId": str(row.target_id),
        "channel": row.target.channel,
        "channelRole": row.target.role,
        "state": row.state,
        "remoteState": row.remote_state,
        "remotePostId": row.remote_post_id,
        "remoteUrl": row.remote_url,
        "canonicalSourceUrl": row.canonical_source_url,
        "scheduledFor": row.scheduled_for.isoformat() if row.scheduled_for else None,
        "canonicalReadyAt": row.canonical_ready_at.isoformat() if row.canonical_ready_at else None,
        "publishedAt": row.published_at.isoformat() if row.published_at else None,
        "publishedRevisionNo": row.published_revision_no,
        "errorCode": row.last_error_code or None,
    }


@admin_api
@require_http_methods(["GET", "POST"])
def targets(request: HttpRequest) -> JsonResponse:
    if request.method == "POST":
        row = create_target(_body(request))
        return JsonResponse(target_json(row), status=201)
    return JsonResponse([target_json(row) for row in PublicationTarget.objects.all()], safe=False)


@admin_api
@require_http_methods(["GET", "PATCH"])
def target_detail(request: HttpRequest, target_id: str) -> JsonResponse:
    if request.method == "PATCH":
        row = update_target(target_id, _body(request))
    else:
        row = PublicationTarget.objects.get(id=target_id)
    return JsonResponse(target_json(row))


@admin_api
@require_http_methods(["POST"])
def target_preflight(request: HttpRequest, target_id: str) -> JsonResponse:
    with transaction.atomic():
        target = PublicationTarget.objects.select_for_update().get(id=target_id)
        event = enqueue_event(
            event_type="publication.preflight_requested",
            aggregate_type="publication_target",
            aggregate_id=target.id,
            job_id=target.id,
            dedupe_key=f"publication.preflight_requested:{target.id}:{target.current_snapshot_version}",
            payload={
                "target_id": str(target.id),
                "target_snapshot_id": str(target.current_snapshot_id),
                "target_config_hash": target.current_config_hash,
            },
        )
    return JsonResponse({"jobId": str(event.id), "state": "queued"}, status=202)


@admin_api
@require_http_methods(["POST"])
def target_canary(request: HttpRequest, target_id: str) -> JsonResponse:
    data = _body(request)
    if data.get("confirmIsolatedTestTarget") is not True:
        raise InvalidInput("격리된 test target 확인이 필요합니다.")
    request_key = data.get("requestKey") or f"canary:{sha256_hex(data)[:32]}"
    row = create_canary_run(
        target_id,
        policy_version=data["policyVersion"],
        reason=data["reason"],
        request_key=request_key,
        user=request.user,
    )
    return JsonResponse({"jobId": str(row.id), "state": row.state}, status=202)


@admin_api
@require_http_methods(["GET", "POST"])
def auto_publish_validations(request: HttpRequest, target_id: str) -> JsonResponse:
    if request.method == "POST":
        row = create_auto_publish_validation(target_id, _body(request))
        return JsonResponse(validation_json(row), status=201)
    rows = AutoPublishValidation.objects.filter(target_id=target_id)
    return JsonResponse([validation_json(row) for row in rows], safe=False)


@admin_api
@require_http_methods(["POST"])
def auto_publish_validation_decisions(
    request: HttpRequest, target_id: str, validation_id: str
) -> JsonResponse:
    row, created = decide_auto_publish_validation(
        target_id, validation_id, _body(request), request=request
    )
    return JsonResponse(
        {
            "id": str(row.id),
            "validationId": str(row.validation_id),
            "version": row.version,
            "decision": row.decision,
            "supersedesDecisionId": (
                str(row.supersedes_decision_id) if row.supersedes_decision_id else None
            ),
            "requestKey": row.request_key,
            "decisionHash": row.decision_hash,
            "decidedBy": str(row.decided_by_id),
            "decidedAt": row.decided_at.isoformat(),
            "reason": row.reason,
        },
        status=201 if created else 200,
    )


@admin_api
@require_http_methods(["GET"])
def auto_publish_validation_report(
    request: HttpRequest, target_id: str, validation_id: str
) -> JsonResponse:
    row = AutoPublishValidation.objects.get(id=validation_id, target_id=target_id)
    normalized: dict[str, Any] = {
        "subjectType": "auto_publish_validation",
        "subjectId": str(row.id),
        "subjectMaterialHash": row.material_hash,
        "reportObjectVersion": row.test_report_object_version,
        "reportHash": row.test_report_hash,
        "samples": [],
        "metrics": {},
        "thresholds": {},
        "stageResults": [],
        "overallResult": "passed" if row.status == AutoPublishValidation.State.PASSED else "failed",
        "verifiedAt": row.created_at.isoformat(),
    }
    try:
        raw = S3ObjectStorage().get_bytes(
            key=row.test_report_object_key,
            version_id=row.test_report_object_version,
        )
        if sha256_hex(raw) == row.test_report_hash:
            report = json.loads(raw.decode("utf-8"))
            for key in ("samples", "metrics", "thresholds", "stageResults", "overallResult", "verifiedAt"):
                if key in report:
                    normalized[key] = report[key]
    except Exception:
        normalized["stageResults"] = [
            {"code": "immutable_report_unavailable", "result": "failed", "detailRedacted": "object unavailable"}
        ]
        normalized["overallResult"] = "failed"
    return JsonResponse(normalized)


@admin_api
@require_http_methods(["PUT"])
def target_auto_publish(request: HttpRequest, target_id: str) -> JsonResponse:
    activation, created = set_auto_publish(target_id, _body(request), request=request)
    return JsonResponse(
        {
            "activation": activation_json(activation),
            "target": target_json(activation.target),
        },
        status=201 if created else 200,
    )


@admin_api
@require_http_methods(["DELETE"])
def target_connection(request: HttpRequest, target_id: str) -> JsonResponse:
    decision = disconnect_target(target_id, _body(request), request=request)
    return JsonResponse({"jobId": str(decision.id), "state": decision.state}, status=202)


@admin_api
@require_http_methods(["POST"])
def target_oauth_start(request: HttpRequest, target_id: str) -> JsonResponse:
    redirect_uri = request.build_absolute_uri("/api/v1/publishing/oauth/google/callback")
    return JsonResponse(
        start_blogger_oauth(target_id, user=request.user, redirect_uri=redirect_uri)
    )


@require_http_methods(["GET"])
def blogger_oauth_callback(request: HttpRequest) -> JsonResponse:
    if not request.user.is_authenticated or not request.user.is_active or not request.user.is_staff:
        return _problem(403, "forbidden", "OAuth를 시작한 관리자 로그인이 필요합니다.")
    if request.GET.get("error"):
        return _problem(400, "blogger_oauth_rejected", "Google OAuth 승인이 거절되었습니다.")
    code = request.GET.get("code")
    state = request.GET.get("state")
    if not code or not state:
        return _problem(400, "blogger_oauth_invalid_callback", "OAuth code와 state가 필요합니다.")
    try:
        target = complete_blogger_oauth(
            code=code,
            state=state,
            user=request.user,
            redirect_uri=request.build_absolute_uri("/api/v1/publishing/oauth/google/callback"),
        )
    except DomainError as exc:
        return _problem(exc.status, exc.code, exc.detail or exc.title)
    return JsonResponse(
        {
            "targetId": str(target.id),
            "connectionState": target.connection_state,
            "next": f"/console/publishing/targets/{target.id}/",
        }
    )


@admin_api
@require_http_methods(["GET", "POST"])
def publication_intents(request: HttpRequest, article_id: str) -> JsonResponse:
    if request.method == "GET":
        row = PublicationIntent.objects.filter(article_id=article_id).order_by("-created_at").first()
        return JsonResponse({"item": intent_json(row) if row else None})
    row = create_publication_intent(article_id, _body(request), user=request.user)
    return JsonResponse(intent_json(row), status=201)


@admin_api
@require_http_methods(["GET"])
def article_preview(request: HttpRequest, article_id: str) -> JsonResponse:
    target_id = request.GET.get("targetId")
    if not target_id:
        raise InvalidInput("targetId query가 필요합니다.")
    intent = PublicationIntent.objects.filter(article_id=article_id).order_by("-created_at").first()
    if not intent:
        raise InvalidInput("먼저 publication intent를 만드세요.")
    row = intent.renders.get(target_id=target_id, render_stage=ArticleChannelRender.Stage.PREVIEW)
    return JsonResponse(render_json(row))


@admin_api
@require_http_methods(["POST"])
def approvals(request: HttpRequest, article_id: str) -> JsonResponse:
    data = _body(request)
    target_id = data.get("actionSubject", {}).get("targetId")
    if not target_id:
        raise InvalidInput("actionSubject.targetId가 필요합니다.")
    row, created = decide_approval(
        article_id, target_id, data, user=request.user, request=request
    )
    return JsonResponse(approval_json(row), status=201 if created else 200)


@admin_api
@require_http_methods(["POST"])
def publish(request: HttpRequest, article_id: str) -> JsonResponse:
    rows = dispatch_publication(article_id, _body(request))
    return JsonResponse(
        {
            "jobId": str(rows[0].id) if rows else None,
            "state": "queued",
            "attemptIds": [str(row.id) for row in rows],
        },
        status=202,
    )


@admin_api
@require_http_methods(["GET"])
def article_publications(request: HttpRequest, article_id: str) -> JsonResponse:
    rows = Publication.objects.select_related("target").filter(article_id=article_id)
    return JsonResponse([publication_json(row) for row in rows], safe=False)


@admin_api
@require_http_methods(["GET"])
def publication_attempts(request: HttpRequest, publication_id: str) -> JsonResponse:
    rows = PublicationAttempt.objects.filter(publication_id=publication_id)
    return JsonResponse(
        [
            {
                "id": str(row.id),
                "state": row.state,
                "action": row.resolved_action,
                "attemptNo": row.attempt_no,
                "reconcileAttemptNo": row.reconcile_attempt_no,
                "errorCode": row.error_code or None,
                "httpStatus": row.http_status,
                "startedAt": row.started_at.isoformat() if row.started_at else None,
                "finishedAt": row.finished_at.isoformat() if row.finished_at else None,
            }
            for row in rows
        ],
        safe=False,
    )


@admin_api
@require_http_methods(["POST"])
def retry_publication_attempt(request: HttpRequest, attempt_id: str) -> JsonResponse:
    with transaction.atomic():
        row = (
            PublicationAttempt.objects.select_for_update()
            .select_related("publication__target")
            .get(id=attempt_id)
        )
        if row.state in {
            PublicationAttempt.State.UNKNOWN_OUTCOME,
            PublicationAttempt.State.RECONCILING,
        }:
            _enqueue_reconcile_locked(row)
            action = "reconcile"
        elif row.state == PublicationAttempt.State.RETRYABLE_FAILED:
            row.state = PublicationAttempt.State.QUEUED
            row.attempt_no += 1
            row.started_at = None
            row.finished_at = None
            row.next_retry_at = None
            row.error_detail_redacted = ""
            row.save(
                update_fields=[
                    "state",
                    "attempt_no",
                    "started_at",
                    "finished_at",
                    "next_retry_at",
                    "error_detail_redacted",
                ]
            )
            enqueue_event(
                event_type="publication.requested",
                aggregate_type="publication_attempt",
                aggregate_id=row.id,
                job_id=row.id,
                dedupe_key=f"publication.requested:{row.id}:{row.attempt_no}",
                payload={
                    "publication_attempt_id": str(row.id),
                },
            )
            action = "retry"
        else:
            raise Conflict("재시도 가능 실패 또는 결과 불명 attempt만 안전하게 재개할 수 있습니다.")
    return JsonResponse({"attemptId": str(row.id), "state": row.state, "action": action}, status=202)


@admin_api
@require_http_methods(["POST"])
def prepare_correction(request: HttpRequest, correction_id: str) -> JsonResponse:
    body = _body(request)
    intent = prepare_verified_correction(
        correction_id,
        user=request.user,
        request_key=body["requestKey"],
    )
    return JsonResponse(intent_json(intent), status=201)


def _console_guard(request: HttpRequest) -> HttpResponse | None:
    if not request.user.is_authenticated or not request.user.is_staff:
        return HttpResponse("관리자 로그인이 필요합니다.", status=403)
    return None


@require_http_methods(["GET"])
def console_index(request: HttpRequest) -> HttpResponse:
    denied = _console_guard(request)
    if denied:
        return denied
    targets_rows = PublicationTarget.objects.all()
    publications_rows = Publication.objects.select_related("target").order_by("-updated_at")[:30]
    return render(
        request,
        "admin_console/publishing/index.html",
        {"targets": targets_rows, "publications": publications_rows},
    )


@require_http_methods(["GET"])
def console_target(request: HttpRequest, target_id: str) -> HttpResponse:
    denied = _console_guard(request)
    if denied:
        return denied
    target = PublicationTarget.objects.get(id=target_id)
    return render(
        request,
        "admin_console/publishing/target_detail.html",
        {
            "target": target,
            "validations": target.validations.all()[:20],
            "canary_runs": target.canary_runs.all()[:20],
        },
    )


@require_http_methods(["GET"])
def console_article(request: HttpRequest, article_id: str) -> HttpResponse:
    denied = _console_guard(request)
    if denied:
        return denied
    intent = PublicationIntent.objects.filter(article_id=article_id).order_by("-created_at").first()
    publications_rows = Publication.objects.select_related("target").filter(article_id=article_id)
    return render(
        request,
        "admin_console/publishing/article_publish.html",
        {
            "article_id": article_id,
            "intent": intent,
            "renders": intent.renders.all() if intent else [],
            "approvals": intent.approvals.all() if intent else [],
            "publications": publications_rows,
        },
    )
