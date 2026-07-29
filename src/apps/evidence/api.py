from __future__ import annotations

import json
import re
from typing import Any, Mapping
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from adapters.storage import S3ObjectStorage
from apps.audit.services import AuditContext
from wisdome_writer.api.openapi import openapi_operation
from wisdome_writer.api.pagination import (
    decode_cursor as decode_signed_cursor,
    encode_cursor as encode_signed_cursor,
)
from wisdome_writer.domain.errors import InvalidCursor
from wisdome_writer.api.problems import problem_response

from .models import (
    DocumentExtraction,
    EvidenceAsset,
    EvidenceReviewDecision,
    ExtractionProfileDecision,
    ExtractionProfileSnapshot,
)
from .services import (
    EvidenceConflict,
    EvidenceInvariantError,
    canonical_hash,
    decide_evidence_review,
    decide_extraction_profile,
)


_SNAKE = re.compile(r"_([a-z])")
_RUN_EVIDENCE_CURSOR_RESOURCE = "evidence.run"
_RUN_EVIDENCE_CURSOR_ORDER = ("id",)
_RUN_EVIDENCE_PAGE_SIZE = 100


def _camel_key(value: str) -> str:
    return _SNAKE.sub(lambda match: match.group(1).upper(), value)


def _camelize(value: Any) -> Any:
    if isinstance(value, dict):
        return {_camel_key(str(key)): _camelize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_camelize(item) for item in value]
    return value


def _profile_payload(profile: ExtractionProfileSnapshot) -> dict[str, Any]:
    return {
        "id": str(profile.id),
        "profileKey": profile.profile_key,
        "profileVersion": profile.profile_version,
        "engine": profile.engine,
        "extractorVersion": profile.extractor_version,
        "packageVersion": profile.package_version,
        "runtimeVersion": profile.runtime_version,
        "pipelineName": profile.pipeline_name,
        "implementationManifestHash": profile.implementation_manifest_hash,
        "approvalState": profile.approval_state,
        "decisionVersion": profile.decision_version,
        "latestDecisionId": str(profile.latest_decision_id) if profile.latest_decision_id else None,
        "config": profile.config,
        "configHash": profile.config_hash,
        "validationMode": profile.validation_mode,
        "calibrationProfileKey": profile.calibration_profile_key,
        "calibrationProfileVersion": profile.calibration_profile_version,
        "calibrationManifestObjectKey": profile.calibration_manifest_object_key,
        "calibrationManifestObjectVersion": profile.calibration_manifest_object_version,
        "calibrationProfileHash": profile.calibration_profile_hash,
        "modelManifest": profile.model_manifest,
        "modelManifestHash": profile.model_manifest_hash,
        "profileMaterialHash": profile.profile_material_hash,
        "createdAt": profile.created_at.isoformat(),
    }


def _profile_decision_payload(decision: ExtractionProfileDecision) -> dict[str, Any]:
    return {
        "id": str(decision.id),
        "profileSnapshotId": str(decision.profile_snapshot_id),
        "version": decision.version,
        "decision": decision.decision,
        "expectedMaterialHash": decision.expected_material_hash,
        "supersedesDecisionId": str(decision.supersedes_decision_id) if decision.supersedes_decision_id else None,
        "requestKey": decision.request_key,
        "decisionHash": decision.decision_hash,
        "decidedBy": str(decision.decided_by_id),
        "decidedAt": decision.decided_at.isoformat(),
        "reason": decision.reason,
    }


@login_required
@openapi_operation("listExtractionProfiles")
@require_GET
def extraction_profiles(request):
    queryset = ExtractionProfileSnapshot.objects.select_related("latest_decision")
    query = request.openapi_query
    if query.get("approvalState"):
        queryset = queryset.filter(approval_state=query["approvalState"])
    if query.get("engine"):
        queryset = queryset.filter(engine=query["engine"])
    return JsonResponse([_profile_payload(profile) for profile in queryset[:500]], safe=False)


@login_required
@openapi_operation("getExtractionProfile")
@require_GET
def extraction_profile_detail(request, profile_id):
    profile = get_object_or_404(ExtractionProfileSnapshot, pk=profile_id)
    return JsonResponse(_profile_payload(profile))


@login_required
@openapi_operation("decideExtractionProfile")
@require_POST
def extraction_profile_decisions(request, profile_id):
    try:
        body = request.openapi_body
        audit_context = AuditContext.for_admin(
            request=request,
            reason_code=body["reason"],
            request_key=body["requestKey"],
        )
        decision, created = decide_extraction_profile(
            profile_id=profile_id,
            decision=body["decision"],
            expected_material_hash=body["expectedMaterialHash"],
            expected_latest_decision_id=body["expectedLatestDecisionId"],
            request_key=body["requestKey"],
            reason=body["reason"],
            admin=request.user,
            request=request,
            reauth_proof_id=body["reauthProofId"],
            audit_context=audit_context,
        )
        return JsonResponse(_profile_decision_payload(decision), status=201 if created else 200)
    except (EvidenceConflict, EvidenceInvariantError):
        return problem_response(status=409, code="profile_decision_conflict")
    except ValidationError:
        return problem_response(status=422, code="profile_decision_invalid")


@login_required
@openapi_operation("getExtractionProfileReport")
@require_GET
def extraction_profile_report(request, profile_id):
    profile = get_object_or_404(ExtractionProfileSnapshot, pk=profile_id)
    if not profile.verification_report_object_key or not profile.verification_report_hash:
        return problem_response(status=404, code="verification_report_not_found")
    try:
        raw = S3ObjectStorage().get_bytes(key=profile.verification_report_object_key)
        report = json.loads(raw)
    except Exception:
        return problem_response(status=409, code="verification_report_unavailable")
    core = {
        key: value for key, value in report.items()
        if key not in {"reportObjectKey", "reportObjectVersion", "reportHash"}
    }
    if (
        canonical_hash(core) != profile.verification_report_hash
        or report.get("reportHash") != profile.verification_report_hash
        or report.get("subjectId") != str(profile.id)
        or report.get("subjectMaterialHash") != profile.profile_material_hash
        or report.get("reportObjectKey") != profile.verification_report_object_key
        or report.get("reportObjectVersion") != profile.verification_report_object_version
    ):
        return problem_response(status=409, code="verification_report_hash_mismatch")
    return JsonResponse(report)


def _document_provenance(evidence: EvidenceAsset) -> dict[str, Any] | None:
    run = evidence.extraction_run
    document = evidence.document_extraction
    origin = evidence.origin_run_source_item
    if not run or not document or not origin:
        return None
    if (
        document.run_source_item_id != origin.id
        or document.source_item_id != evidence.source_item_id
        or run.document_extraction_id != document.id
    ):
        raise EvidenceInvariantError("Document evidence lineage is inconsistent")
    return {
        "documentExtractionId": str(document.id),
        "collectionRunId": str(origin.run_id),
        "runSourceItemId": str(origin.id),
        "sourceSnapshotId": str(origin.source_snapshot_id),
        "extractionRunId": str(run.id),
        "retryOfRunId": str(run.retry_of_run_id) if run.retry_of_run_id else None,
        "inputKind": document.input_kind,
        "inputMimeType": document.input_mime_type,
        "inputChecksum": document.input_checksum,
        "inputFrameCount": document.input_frame_count,
        "profileSnapshotId": str(run.extraction_profile_snapshot_id),
        "profileMaterialHash": run.profile_material_hash,
        "engine": run.engine,
        "fingerprintSchemaVersion": run.fingerprint_schema_version,
        "extractionFingerprint": run.extraction_fingerprint,
        "profileKey": run.profile_key,
        "profileVersion": run.profile_version,
        "packageVersion": run.package_version,
        "runtimeVersion": run.runtime_version,
        "pipelineName": run.pipeline_name,
        "modelManifestHash": run.model_manifest_hash,
        "configHash": run.config_hash,
        "resultChecksum": run.result_checksum,
        "lowConfidenceReasonsHash": run.low_confidence_reasons_hash,
        "languageProfile": run.language_profile,
        "inputPageCount": document.input_page_count,
        "pageSetHash": run.page_set_hash,
        "requestedPageIndices": run.requested_page_indices,
        "processedPageIndices": run.processed_page_indices,
        "runComplete": run.requested_page_indices == run.processed_page_indices,
        "runState": run.state,
    }


def _generic_provenance(evidence: EvidenceAsset) -> dict[str, Any] | None:
    attempt = evidence.generic_extraction_attempt
    origin = evidence.origin_run_source_item
    if not attempt or not origin:
        return None
    if attempt.run_source_item_id != origin.id or attempt.source_item_id != evidence.source_item_id:
        raise EvidenceInvariantError("Generic evidence lineage is inconsistent")
    return {
        "collectionRunId": str(origin.run_id),
        "runSourceItemId": str(origin.id),
        "sourceSnapshotId": str(origin.source_snapshot_id),
        "attemptId": str(attempt.id),
        "inputChecksum": attempt.input_asset.checksum if attempt.input_asset else evidence.source_item.content_hash,
        "profileSnapshotId": str(attempt.extraction_profile_snapshot_id),
        "profileMaterialHash": attempt.profile_material_hash,
        "fingerprintSchemaVersion": attempt.fingerprint_schema_version,
        "extractionFingerprint": attempt.extraction_fingerprint,
        "attemptState": attempt.state,
        "engine": attempt.engine,
        "extractorVersion": attempt.extractor_version,
        "configHash": attempt.config_hash,
        "validationMode": attempt.validation_mode,
        "resultChecksum": attempt.result_checksum,
        "lowConfidenceReasonsHash": attempt.low_confidence_reasons_hash,
        "calibrationProfileKey": attempt.calibration_profile_key,
        "calibrationProfileVersion": attempt.calibration_profile_version,
        "calibrationProfileHash": attempt.calibration_profile_hash,
    }


def _decision_summary(decision: EvidenceReviewDecision | None) -> dict[str, Any] | None:
    if not decision:
        return None
    return {
        "id": str(decision.id),
        "decision": decision.decision,
        "reason": decision.reason,
        "reviewerAdminId": str(decision.reviewer_admin_id),
        "decidedAt": decision.decided_at.isoformat(),
    }


def _evidence_payload(evidence: EvidenceAsset) -> dict[str, Any]:
    source = evidence.source_item
    origin = evidence.origin_run_source_item
    extraction = None
    if evidence.derivation_type == "document_derived":
        extraction = _document_provenance(evidence)
    elif evidence.derivation_type == "other_derived":
        extraction = _generic_provenance(evidence)
    excerpt = evidence.extracted_text[:2000] if evidence.extracted_text else None
    return {
        "id": str(evidence.id),
        "sourceItemId": str(evidence.source_item_id) if evidence.source_item_id else None,
        "originCollectionRunId": str(origin.run_id) if origin else None,
        "originRunSourceItemId": str(origin.id) if origin else None,
        "originSourceSnapshotId": str(origin.source_snapshot_id) if origin else None,
        "sourceChecksum": source.content_hash if source else None,
        "derivationType": evidence.derivation_type,
        "kind": evidence.kind,
        "contentHash": evidence.evidence_content_hash,
        "sourceTitle": source.title if source else None,
        "sourceUrl": source.canonical_url if source else None,
        "publisher": source.publisher if source else "Wisdome Super Writer",
        "publishedAt": source.published_at.isoformat() if source and source.published_at else None,
        "locator": _camelize(evidence.locator) if evidence.locator else None,
        "excerpt": excerpt,
        "extraction": extraction,
        "rightsStatus": evidence.rights_status,
        "rightsBasisUrl": evidence.rights_basis_url,
        "attribution": evidence.attribution_text,
        "altText": evidence.alt_text,
        "confidence": float(evidence.confidence) if evidence.confidence is not None else None,
        "confidenceDetail": evidence.confidence_detail,
        "lowConfidenceReasons": evidence.low_confidence_reasons,
        "reviewSubjectSchemaVersion": evidence.review_subject_schema_version,
        "reviewSubjectHash": evidence.review_subject_hash,
        "latestManualReviewDecision": _decision_summary(evidence.latest_review_decision),
        "reviewState": evidence.review_state,
        "manualReviewRequired": evidence.manual_review_required,
        "publishable": evidence.publishable,
        "exclusionReason": None if evidence.publishable else _exclusion_reason(evidence),
    }


def _exclusion_reason(evidence: EvidenceAsset) -> str:
    if evidence.manual_review_required:
        return "manual_review_required"
    if evidence.rights_status not in {"allowed", "attribution_required"}:
        return "rights_not_publishable"
    if evidence.document_extraction_id and not evidence.document_extraction.document_complete:
        return "document_incomplete"
    return "quality_gate_not_passed"


@login_required
@openapi_operation("listRunEvidence")
def run_evidence(request, run_id):
    query = request.openapi_query
    publishable_filter = query.get("publishable")
    kind_filter = query.get("kind")
    cursor_filters = {
        "run_id": request.openapi_path["runId"],
        "publishable": publishable_filter,
        "kind": kind_filter,
    }
    queryset = EvidenceAsset.objects.filter(origin_run_source_item__run_id=run_id).select_related(
        "source_item", "origin_run_source_item", "latest_review_decision",
        "document_extraction", "extraction_run", "generic_extraction_attempt__input_asset",
    ).order_by("id")
    if publishable_filter is not None:
        queryset = queryset.filter(publishable=publishable_filter)
    if kind_filter:
        queryset = queryset.filter(kind=kind_filter)
    cursor_value = query.get("cursor")
    if cursor_value is not None:
        state = decode_signed_cursor(
            cursor_value,
            resource=_RUN_EVIDENCE_CURSOR_RESOURCE,
            filters=cursor_filters,
            order=_RUN_EVIDENCE_CURSOR_ORDER,
            limit=_RUN_EVIDENCE_PAGE_SIZE,
        )
        if set(state.position) != {"id"}:
            raise InvalidCursor("The evidence cursor position is invalid")
        identifier_value = state.position["id"]
        if not isinstance(identifier_value, str):
            raise InvalidCursor("The evidence cursor position is invalid")
        try:
            identifier = UUID(identifier_value)
        except ValueError as exc:
            raise InvalidCursor("The evidence cursor position is invalid") from exc
        if str(identifier) != identifier_value:
            raise InvalidCursor("The evidence cursor position is invalid")
        queryset = queryset.filter(id__gt=identifier)
    items = list(queryset[: _RUN_EVIDENCE_PAGE_SIZE + 1])
    next_cursor = None
    if len(items) > _RUN_EVIDENCE_PAGE_SIZE:
        next_cursor = encode_signed_cursor(
            resource=_RUN_EVIDENCE_CURSOR_RESOURCE,
            filters=cursor_filters,
            order=_RUN_EVIDENCE_CURSOR_ORDER,
            limit=_RUN_EVIDENCE_PAGE_SIZE,
            position={"id": str(items[_RUN_EVIDENCE_PAGE_SIZE - 1].id)},
        )
    try:
        return JsonResponse({
            "items": [
                _evidence_payload(item)
                for item in items[:_RUN_EVIDENCE_PAGE_SIZE]
            ],
            "nextCursor": next_cursor,
        })
    except EvidenceInvariantError:
        return problem_response(status=409, code="evidence_lineage_invalid")


@login_required
@openapi_operation("getEvidence")
@require_GET
def evidence_detail(request, evidence_id):
    evidence = get_object_or_404(
        EvidenceAsset.objects.select_related(
            "source_item", "origin_run_source_item", "latest_review_decision",
            "document_extraction", "extraction_run", "generic_extraction_attempt__input_asset",
        ),
        pk=evidence_id,
    )
    try:
        return JsonResponse(_evidence_payload(evidence))
    except EvidenceInvariantError:
        return problem_response(status=409, code="evidence_lineage_invalid")


@login_required
@openapi_operation("getDocumentExtraction")
@require_GET
def document_extraction_detail(request, document_extraction_id):
    document = get_object_or_404(
        DocumentExtraction.objects.select_related("run_source_item"), pk=document_extraction_id
    )
    origin = document.run_source_item
    return JsonResponse({
        "documentExtractionId": str(document.id),
        "collectionRunId": str(origin.run_id),
        "runSourceItemId": str(origin.id),
        "sourceItemId": str(document.source_item_id),
        "sourceSnapshotId": str(origin.source_snapshot_id),
        "inputKind": document.input_kind,
        "inputMimeType": document.input_mime_type,
        "inputChecksum": document.input_checksum,
        "inputFrameCount": document.input_frame_count,
        "inputPageCount": document.input_page_count,
        "expectedPageIndices": document.expected_page_indices,
        "coveredPageIndices": document.covered_page_indices,
        "state": document.state,
        "coverageManifestHash": document.coverage_manifest_hash,
        "selectedEvidenceManifestHash": document.selected_evidence_manifest_hash,
        "documentComplete": document.document_complete,
    })


def _review_decision_payload(decision: EvidenceReviewDecision) -> dict[str, Any]:
    return {
        "id": str(decision.id),
        "evidenceAuditSnapshotId": str(decision.evidence_audit_snapshot_id),
        "evidenceId": str(decision.evidence_asset_id) if decision.evidence_asset_id else None,
        "decisionProvenanceType": decision.decision_provenance_type,
        "subjectSchemaVersion": decision.review_subject_schema_version,
        "subjectHash": decision.review_subject_hash,
        "extractionRunId": str(decision.extraction_run_id) if decision.extraction_run_id else None,
        "genericExtractionAttemptId": (
            str(decision.generic_extraction_attempt_id) if decision.generic_extraction_attempt_id else None
        ),
        "supersedesDecisionId": str(decision.supersedes_decision_id) if decision.supersedes_decision_id else None,
        "requestKey": decision.request_key,
        "decision": decision.decision,
        "reason": decision.reason,
        "reviewerAdminId": str(decision.reviewer_admin_id),
        "decidedAt": decision.decided_at.isoformat(),
    }


@login_required
@openapi_operation("createEvidenceReviewDecision")
@require_POST
def evidence_review_decisions(request, evidence_id):
    try:
        body = request.openapi_body
        audit_context = AuditContext.for_admin(
            request=request,
            reason_code=body["reason"],
            request_key=body["requestKey"],
        )
        decision, created = decide_evidence_review(
            evidence_id=evidence_id,
            decision=body["decision"],
            expected_subject_version=body["subjectSchemaVersion"],
            expected_subject_hash=body["subjectHash"],
            expected_latest_decision_id=body["expectedLatestDecisionId"],
            request_key=body["requestKey"],
            reason=body["reason"],
            admin=request.user,
            audit_context=audit_context,
        )
        return JsonResponse(_review_decision_payload(decision), status=201 if created else 200)
    except (EvidenceConflict, EvidenceInvariantError):
        return problem_response(status=409, code="evidence_review_conflict")
    except ValidationError:
        return problem_response(status=422, code="evidence_review_invalid")

