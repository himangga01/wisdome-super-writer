from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from adapters.storage import S3ObjectStorage
from apps.accounts.services import consume_reauthentication_proof
from apps.audit.models import AuditEvent
from wisdome_writer.infrastructure.outbox import enqueue_event

from .models import (
    DocumentExtraction,
    EvidenceAsset,
    EvidenceAuditSnapshot,
    EvidenceDerivationType,
    EvidenceKind,
    EvidenceReviewDecision,
    ExtractionEngine,
    ExtractionProfileDecision,
    ExtractionProfileSnapshot,
    ExtractionRun,
    ExtractionState,
    GenericExtractionAttempt,
    GenericValidationMode,
    ProfileApprovalState,
    ReviewState,
    RightsStatus,
)


class EvidenceConflict(Exception):
    """Raised when a compare-and-swap or idempotency condition is stale."""


class EvidenceInvariantError(Exception):
    """Raised when stored provenance cannot satisfy the extraction contract."""


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {unicodedata.normalize("NFC", str(k)): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "hex") and value.__class__.__name__ == "UUID":
        return str(value)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return NFC canonical JSON, preferring the RFC 8785 implementation when installed."""
    normalized = _normalize(value)
    try:
        import rfc8785  # type: ignore

        return rfc8785.dumps(normalized)
    except ImportError:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def normalize_page_indices(indices: Iterable[int], *, page_count: int | None = None) -> list[int]:
    normalized = sorted(set(int(index) for index in indices))
    if not normalized:
        raise EvidenceInvariantError("A child extraction page set cannot be empty")
    if normalized[0] < 0:
        raise EvidenceInvariantError("Page indices must be non-negative")
    if page_count is not None and normalized[-1] >= page_count:
        raise EvidenceInvariantError("Page index is outside the input document")
    return normalized


def profile_material(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "extractor_version": profile.get("extractor_version"),
        "package_version": profile.get("package_version"),
        "runtime_version": profile.get("runtime_version"),
        "pipeline_name": profile.get("pipeline_name"),
        "implementation_manifest_hash": profile.get("implementation_manifest_hash"),
        "config_hash": profile.get("config_hash"),
        "validation_mode": profile.get("validation_mode"),
        "calibration_profile_key": profile.get("calibration_profile_key"),
        "calibration_profile_version": profile.get("calibration_profile_version"),
        "calibration_profile_hash": profile.get("calibration_profile_hash"),
        "model_manifest_hash": profile.get("model_manifest_hash"),
    }


def extraction_fingerprint(
    document: DocumentExtraction,
    profile: ExtractionProfileSnapshot,
    pages: Sequence[int],
) -> tuple[str, str]:
    pages = normalize_page_indices(pages, page_count=document.input_page_count)
    page_set_hash = canonical_hash(pages)
    material = {
        "fingerprint_schema_version": "v1",
        "document_extraction_id": str(document.id),
        "input_kind": document.input_kind,
        "mime_type": document.input_mime_type,
        "input_frame_count": document.input_frame_count,
        "input_checksum": document.input_checksum,
        "page_set_hash": page_set_hash,
        "engine": profile.engine,
        "profile_key": profile.profile_key,
        "profile_snapshot_id": str(profile.id),
        "profile_material_hash": profile.profile_material_hash,
        "profile_version": profile.profile_version,
        "package_version": profile.package_version,
        "runtime_version": profile.runtime_version,
        "model_manifest_hash": profile.model_manifest_hash,
        "config_hash": profile.config_hash,
    }
    return page_set_hash, canonical_hash(material)


def generic_extraction_fingerprint(
    *,
    run_source_item_id: Any,
    source_item_id: Any,
    input_asset_id: Any,
    input_checksum: str,
    profile: ExtractionProfileSnapshot,
) -> str:
    return canonical_hash({
        "fingerprint_schema_version": "v1",
        "run_source_item_id": str(run_source_item_id),
        "source_item_id": str(source_item_id),
        "input_asset_id": str(input_asset_id) if input_asset_id else None,
        "input_checksum": input_checksum,
        "profile_snapshot_id": str(profile.id),
        "profile_material_hash": profile.profile_material_hash,
        "engine": profile.engine,
        "extractor_version": profile.extractor_version,
        "config_hash": profile.config_hash,
        "validation_mode": profile.validation_mode,
        "calibration_profile_hash": profile.calibration_profile_hash,
    })


_IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}


def normalize_low_confidence_reasons(reasons: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = [_normalize(dict(reason)) for reason in reasons]
    normalized.sort(key=lambda reason: (
        _IMPACT_ORDER.get(str(reason.get("impact", "low")), 99),
        str(reason.get("scope") or ""),
        (reason.get("fieldOrBlockRef", reason.get("ref"))) is not None,
        str(reason.get("fieldOrBlockRef", reason.get("ref")) or ""),
        str(reason.get("code") or ""),
        str(reason.get("observedConfidence", reason.get("observed")) or ""),
        str(reason.get("threshold") or ""),
        str(reason.get("message") or ""),
    ))
    return normalized


def evidence_content_hash(*, text: str | None, structured_data: Any, checksum: str | None) -> str:
    return canonical_hash({"text": text, "structured_data": structured_data, "object_checksum": checksum})


def review_subject_payload(evidence: EvidenceAsset) -> dict[str, Any]:
    source_snapshot_id = None
    origin = evidence.origin_run_source_item
    if origin is not None:
        source_snapshot_id = getattr(origin, "source_snapshot_id", None)
    input_checksum = evidence.checksum
    document = evidence.extraction_run
    generic = evidence.generic_extraction_attempt
    if evidence.document_extraction_id:
        input_checksum = evidence.document_extraction.input_checksum

    payload: dict[str, Any] = {
        "review_subject_schema_version": "v1",
        "evidence_asset_id": str(evidence.id),
        "source_item_id": str(evidence.source_item_id) if evidence.source_item_id else None,
        "origin_run_source_item_id": (
            str(evidence.origin_run_source_item_id) if evidence.origin_run_source_item_id else None
        ),
        "source_snapshot_id": str(source_snapshot_id) if source_snapshot_id else None,
        "input_checksum": input_checksum,
        "derivation_type": evidence.derivation_type,
        "kind": evidence.kind,
        "locator_type": evidence.locator_type,
        "locator": evidence.locator,
        "evidence_content_hash": evidence.evidence_content_hash,
        "confidence": str(evidence.confidence) if evidence.confidence is not None else None,
        "confidence_detail_hash": canonical_hash(evidence.confidence_detail),
        "document": None,
        "generic": None,
    }
    if evidence.derivation_type == EvidenceDerivationType.DOCUMENT and document:
        payload["document"] = {
            "extraction_run_id": str(document.id),
            "extraction_fingerprint": document.extraction_fingerprint,
            "result_checksum": document.result_checksum,
            "low_confidence_reasons_hash": document.low_confidence_reasons_hash,
        }
    elif evidence.derivation_type == EvidenceDerivationType.OTHER and generic:
        payload["generic"] = {
            "generic_extraction_attempt_id": str(generic.id),
            "extraction_fingerprint": generic.extraction_fingerprint,
            "engine": generic.engine,
            "extractor_version": generic.extractor_version,
            "config_hash": generic.config_hash,
            "validation_mode": generic.validation_mode,
            "result_checksum": generic.result_checksum,
            "low_confidence_reasons_hash": generic.low_confidence_reasons_hash,
            "calibration_profile_key": generic.calibration_profile_key,
            "calibration_profile_version": generic.calibration_profile_version,
            "calibration_profile_hash": generic.calibration_profile_hash,
        }
    return payload


def calculate_review_subject_hash(evidence: EvidenceAsset) -> str:
    return canonical_hash(review_subject_payload(evidence))


def _approved_current_decision(evidence: EvidenceAsset) -> bool:
    decision = evidence.latest_review_decision
    return bool(
        decision
        and decision.decision == EvidenceReviewDecision.Decision.APPROVED
        and decision.review_subject_schema_version == evidence.review_subject_schema_version
        and decision.review_subject_hash == evidence.review_subject_hash
    )


def _uses_audit_only_raw_input(evidence: EvidenceAsset | None) -> bool:
    if evidence is None:
        return False
    if (
        evidence.derivation_type == EvidenceDerivationType.RAW
        and evidence.raw_input_fingerprint is None
    ):
        return True
    if (
        evidence.parent_asset_id is not None
        and evidence.parent_asset.derivation_type
        == EvidenceDerivationType.RAW
        and evidence.parent_asset.raw_input_fingerprint is None
    ):
        return True
    attempts = []
    if evidence.generic_extraction_attempt_id is not None:
        attempts.append(evidence.generic_extraction_attempt)
    producing_attempt = getattr(
        evidence,
        "producing_generic_attempt",
        None,
    )
    if producing_attempt is not None:
        attempts.append(producing_attempt)
    return any(
        attempt.input_asset is not None
        and (
            (
                attempt.input_asset.derivation_type
                == EvidenceDerivationType.RAW
                and attempt.input_asset.raw_input_fingerprint is None
            )
            or (
                attempt.input_asset.parent_asset_id is not None
                and attempt.input_asset.parent_asset.derivation_type
                == EvidenceDerivationType.RAW
                and attempt.input_asset.parent_asset.raw_input_fingerprint
                is None
            )
        )
        for attempt in attempts
    )


def calculate_publishable(evidence: EvidenceAsset, *, document_complete: bool | None = None) -> bool:
    if (
        evidence.derivation_type == EvidenceDerivationType.RAW
        and evidence.raw_input_fingerprint is None
    ):
        return False
    if (
        evidence.derivation_type == EvidenceDerivationType.DOCUMENT
        and evidence.document_extraction is not None
    ):
        document = evidence.document_extraction
        if document.input_fingerprint is None:
            return False
        input_asset = document.input_asset
        if _uses_audit_only_raw_input(input_asset):
            return False
    if (
        evidence.derivation_type == EvidenceDerivationType.OTHER
    ):
        if _uses_audit_only_raw_input(evidence):
            return False
    if evidence.rights_status not in (RightsStatus.ALLOWED, RightsStatus.ATTRIBUTION_REQUIRED):
        return False
    if not evidence.rights_basis_url:
        return False
    if evidence.rights_status == RightsStatus.ATTRIBUTION_REQUIRED and not evidence.attribution_text:
        return False
    if evidence.kind in (EvidenceKind.IMAGE, EvidenceKind.CHART, EvidenceKind.SCREENSHOT) and not evidence.alt_text:
        return False
    if evidence.manual_review_required or evidence.review_state in (ReviewState.REJECTED, ReviewState.MANUAL_REQUIRED):
        return False
    if evidence.derivation_type == EvidenceDerivationType.DOCUMENT:
        complete = document_complete
        if complete is None:
            complete = bool(evidence.document_extraction and evidence.document_extraction.document_complete)
        if not complete:
            return False
        if evidence.extraction_run and evidence.extraction_run.state == ExtractionState.LOW_CONFIDENCE:
            return _approved_current_decision(evidence)
    if evidence.derivation_type == EvidenceDerivationType.OTHER:
        attempt = evidence.generic_extraction_attempt
        if attempt and attempt.state == ExtractionState.LOW_CONFIDENCE:
            return _approved_current_decision(evidence)
    return evidence.review_state in (ReviewState.PASSED, ReviewState.PENDING)


def _selected_runs(document: DocumentExtraction) -> dict[int, ExtractionRun]:
    pages = document.routing_manifest.get("pages", []) if isinstance(document.routing_manifest, dict) else []
    run_ids = {str(page.get("selected_run_id")) for page in pages if page.get("selected_run_id")}
    runs = {
        str(run.id): run
        for run in document.extraction_runs.filter(id__in=run_ids).select_related("extraction_profile_snapshot")
    }
    selected: dict[int, ExtractionRun] = {}
    for page in pages:
        index = int(page["page_index"])
        run = runs.get(str(page.get("selected_run_id")))
        if run is None or index in selected or index not in run.processed_page_indices:
            raise EvidenceInvariantError("Routing manifest references a missing, duplicate or incompatible run")
        selected[index] = run
    return selected


@transaction.atomic
def aggregate_document_extraction(document_id: Any) -> DocumentExtraction:
    document = (
        DocumentExtraction.objects.select_for_update()
        .select_related(
            "input_asset__generic_extraction_attempt__input_asset",
            "input_asset__producing_generic_attempt__input_asset",
            "input_asset__parent_asset",
        )
        .get(pk=document_id)
    )
    input_asset = document.input_asset
    duplicate_input = _uses_audit_only_raw_input(input_asset)
    if (
        document.input_fingerprint is None
        or duplicate_input
    ):
        return document
    try:
        selected = _selected_runs(document)
    except EvidenceInvariantError as exc:
        document.state = ExtractionState.FAILED
        document.error_code = "routing_manifest_invalid"
        document.error_detail_redacted = str(exc)
        document.document_complete = False
        document.coverage_manifest_hash = None
        document.selected_evidence_manifest_hash = None
        document.save(update_fields=(
            "state", "error_code", "error_detail_redacted", "document_complete",
            "coverage_manifest_hash", "selected_evidence_manifest_hash",
        ))
        return document

    expected = list(range(document.input_page_count))
    covered = sorted(selected)
    complete = covered == expected
    selected_run_ids = {run.id for run in selected.values()}
    evidence = list(
        EvidenceAsset.objects.select_for_update()
        .filter(document_extraction=document, extraction_run_id__in=selected_run_ids)
        .select_related("latest_review_decision", "extraction_run")
        .order_by("id")
    )
    if complete:
        for run in selected.values():
            if run.state not in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE):
                complete = False
                break
            if run.state == ExtractionState.LOW_CONFIDENCE:
                run_evidence = [item for item in evidence if item.extraction_run_id == run.id]
                if not run_evidence or not all(_approved_current_decision(item) for item in run_evidence):
                    complete = False
                    break

    document.covered_page_indices = covered
    if complete:
        coverage = [{
            "page_index": index,
            "selected_run_id": str(run.id),
            "engine": run.engine,
            "result_checksum": run.result_checksum,
        } for index, run in sorted(selected.items())]
        selected_assets = [{
            "evidence_asset_id": str(item.id),
            "evidence_content_hash": item.evidence_content_hash,
            "result_checksum": item.extraction_run.result_checksum if item.extraction_run else None,
            "locator_hash": canonical_hash(item.locator),
        } for item in evidence]
        document.coverage_manifest_hash = canonical_hash(coverage)
        document.selected_evidence_manifest_hash = canonical_hash(selected_assets)
        document.document_complete = True
        document.state = ExtractionState.SUCCEEDED
        document.finished_at = timezone.now()
        document.error_code = None
        document.error_detail_redacted = None
    else:
        document.coverage_manifest_hash = None
        document.selected_evidence_manifest_hash = None
        document.document_complete = False
        if any(run.state == ExtractionState.FAILED for run in selected.values()):
            document.state = ExtractionState.FAILED
        elif selected:
            document.state = ExtractionState.LOW_CONFIDENCE
    document.full_clean()
    document.save()

    for item in evidence:
        publishable = calculate_publishable(item, document_complete=document.document_complete)
        if item.publishable != publishable:
            item.publishable = publishable
            item.save(update_fields=("publishable",))
    return document


def _decision_request_hash(payload: Mapping[str, Any]) -> str:
    return canonical_hash(dict(payload))


@transaction.atomic
def _verified_profile_report(profile: ExtractionProfileSnapshot) -> dict[str, Any]:
    if not (
        profile.verification_report_object_key
        and profile.verification_report_object_version
        and profile.verification_report_hash
    ):
        raise ValidationError("A hash-verified profile report is required before approval")
    try:
        raw = S3ObjectStorage().get_bytes(key=profile.verification_report_object_key)
        report = json.loads(raw)
    except Exception as exc:
        raise ValidationError("The profile verification report is unavailable") from exc
    if not isinstance(report, dict):
        raise ValidationError("The profile verification report is invalid")
    core = {
        key: value
        for key, value in report.items()
        if key not in {"reportObjectKey", "reportObjectVersion", "reportHash"}
    }
    valid = (
        canonical_hash(core) == profile.verification_report_hash
        and report.get("reportHash") == profile.verification_report_hash
        and report.get("reportObjectKey") == profile.verification_report_object_key
        and report.get("reportObjectVersion") == profile.verification_report_object_version
        and report.get("subjectId") == str(profile.id)
        and report.get("subjectMaterialHash") == profile.profile_material_hash
    )
    if not valid:
        raise ValidationError("The profile verification report does not match the approved material")
    if report.get("overallResult") != "passed":
        raise ValidationError("A failed extraction profile report cannot be approved")
    if any(item.get("result") != "passed" for item in report.get("stageResults", [])):
        raise ValidationError("Every extraction profile verification stage must pass")
    return report


@transaction.atomic
def decide_extraction_profile(
    *,
    profile_id: Any,
    decision: str,
    expected_material_hash: str,
    expected_latest_decision_id: Any,
    request_key: str,
    reason: str,
    admin: Any,
    request: Any,
    reauth_proof_id: Any,
) -> tuple[ExtractionProfileDecision, bool]:
    profile = ExtractionProfileSnapshot.objects.select_for_update().get(pk=profile_id)
    request_payload = {
        "decision": decision,
        "expected_material_hash": expected_material_hash,
        "expected_latest_decision_id": (
            str(expected_latest_decision_id) if expected_latest_decision_id else None
        ),
        "request_key": request_key,
        "reason": reason,
    }
    request_hash = _decision_request_hash(request_payload)
    existing = ExtractionProfileDecision.objects.filter(
        profile_snapshot=profile, request_key=request_key
    ).first()
    if existing:
        if existing.request_hash != request_hash or existing.decided_by_id != admin.pk:
            raise EvidenceConflict("The request key was already used with a different payload")
        return existing, False
    consume_reauthentication_proof(
        request=request,
        proof_id=reauth_proof_id,
        action_scope="profile_decision",
        entity_type="extraction_profile_snapshot",
        entity_id=profile.id,
    )
    if profile.profile_material_hash != expected_material_hash:
        raise EvidenceConflict("The extraction profile material changed")
    current = str(profile.latest_decision_id) if profile.latest_decision_id else None
    expected = str(expected_latest_decision_id) if expected_latest_decision_id else None
    if current != expected:
        raise EvidenceConflict("The extraction profile decision projection is stale")
    if decision == ExtractionProfileDecision.Decision.APPROVED:
        if profile.approval_state != ProfileApprovalState.DRAFT:
            raise EvidenceConflict("Only a draft extraction profile can be approved")
        _verified_profile_report(profile)
        next_state = ProfileApprovalState.APPROVED
    elif decision == ExtractionProfileDecision.Decision.RETIRED:
        if profile.approval_state != ProfileApprovalState.APPROVED:
            raise EvidenceConflict("Only an approved extraction profile can be retired")
        next_state = ProfileApprovalState.RETIRED
    else:
        raise ValidationError({"decision": "Unsupported extraction profile decision"})

    now = timezone.now()
    version = profile.decision_version + 1
    decision_hash = canonical_hash({
        **request_payload,
        "profile_snapshot_id": str(profile.id),
        "version": version,
        "decided_by": str(admin.pk),
        "decided_at": now,
    })
    record = ExtractionProfileDecision(
        profile_snapshot=profile,
        version=version,
        decision=decision,
        expected_material_hash=expected_material_hash,
        supersedes_decision=profile.latest_decision,
        request_key=request_key,
        request_hash=request_hash,
        decision_hash=decision_hash,
        decided_by=admin,
        decided_at=now,
        reason=reason,
    )
    record.full_clean()
    record.save()
    profile.latest_decision = record
    profile.decision_version = version
    profile.approval_state = next_state
    if next_state == ProfileApprovalState.APPROVED:
        profile.approved_at = now
    else:
        profile.retired_at = now
    profile.save()
    AuditEvent.objects.record(
        correlation_id=profile.id,
        actor_type=AuditEvent.ActorType.ADMIN,
        actor_id=admin.pk,
        action="evidence.profile_decided",
        entity_type="ExtractionProfileSnapshot",
        entity_id=profile.id,
        before_hash=expected_material_hash,
        after_hash=profile.profile_material_hash,
        reason_code=decision,
    )
    enqueue_event(
        topic="evidence.profile_decided",
        aggregate_type="ExtractionProfileSnapshot",
        aggregate_id=profile.id,
        message_key=f"evidence.profile_decided:{profile.id}:{record.id}",
        correlation_id=profile.id,
        payload={
            "profile_snapshot_id": str(profile.id),
            "decision_id": str(record.id),
            "decision": decision,
            "profile_material_hash": profile.profile_material_hash,
        },
    )
    return record, True


def _provenance_for_review(evidence: EvidenceAsset) -> tuple[str, ExtractionRun | None, GenericExtractionAttempt | None]:
    if evidence.derivation_type == EvidenceDerivationType.DOCUMENT:
        return "document", evidence.extraction_run, None
    if evidence.derivation_type == EvidenceDerivationType.OTHER:
        return "generic", None, evidence.generic_extraction_attempt
    return "raw", None, None


def _reason_hash_for_evidence(evidence: EvidenceAsset) -> str | None:
    if evidence.extraction_run:
        return evidence.extraction_run.low_confidence_reasons_hash
    if evidence.generic_extraction_attempt:
        return evidence.generic_extraction_attempt.low_confidence_reasons_hash
    return None


def _ensure_review_snapshot(evidence: EvidenceAsset) -> EvidenceAuditSnapshot:
    provenance_type, extraction_run, generic_attempt = _provenance_for_review(evidence)
    source_identity_hash = canonical_hash({
        "source_item_id": str(evidence.source_item_id) if evidence.source_item_id else None,
        "origin_run_source_item_id": (
            str(evidence.origin_run_source_item_id) if evidence.origin_run_source_item_id else None
        ),
    })
    provenance_hash = canonical_hash({
        "type": provenance_type,
        "extraction_run_id": str(extraction_run.id) if extraction_run else None,
        "extraction_fingerprint": extraction_run.extraction_fingerprint if extraction_run else None,
        "generic_attempt_id": str(generic_attempt.id) if generic_attempt else None,
        "generic_fingerprint": generic_attempt.extraction_fingerprint if generic_attempt else None,
    })
    payload = {
        "original_evidence_asset_id": str(evidence.id),
        "source_identity_hash": source_identity_hash,
        "evidence_content_hash": evidence.evidence_content_hash,
        "locator_hash": canonical_hash(evidence.locator),
        "provenance_type": provenance_type,
        "provenance_manifest_hash": provenance_hash,
        "low_confidence_reasons_hash": _reason_hash_for_evidence(evidence),
        "review_subject_schema_version": evidence.review_subject_schema_version,
        "review_subject_hash": evidence.review_subject_hash,
    }
    snapshot_hash = canonical_hash(payload)
    snapshot, _ = EvidenceAuditSnapshot.objects.get_or_create(
        snapshot_hash=snapshot_hash,
        defaults={**payload, "original_evidence_asset_id": evidence.id},
    )
    return snapshot


@transaction.atomic
@transaction.atomic
def decide_evidence_review(
    *,
    evidence_id: Any,
    decision: str,
    expected_subject_version: str,
    expected_subject_hash: str,
    expected_latest_decision_id: Any,
    request_key: str,
    reason: str,
    admin: Any,
) -> tuple[EvidenceReviewDecision, bool]:
    evidence = (
        EvidenceAsset.objects.select_for_update()
        .select_related(
            "latest_review_decision", "extraction_run", "generic_extraction_attempt",
            "document_extraction", "origin_run_source_item",
        )
        .get(pk=evidence_id)
    )
    calculated = calculate_review_subject_hash(evidence)
    if calculated != evidence.review_subject_hash:
        raise EvidenceInvariantError("Stored evidence review subject hash is invalid")
    if (
        evidence.review_subject_schema_version != expected_subject_version
        or evidence.review_subject_hash != expected_subject_hash
    ):
        raise EvidenceConflict("The evidence subject changed and must be reviewed again")
    current = str(evidence.latest_review_decision_id) if evidence.latest_review_decision_id else None
    expected = str(expected_latest_decision_id) if expected_latest_decision_id else None
    if current != expected:
        raise EvidenceConflict("The evidence review projection is stale")
    reasons = normalize_low_confidence_reasons(evidence.low_confidence_reasons or [])
    expected_reason_hash = _reason_hash_for_evidence(evidence)
    if expected_reason_hash and canonical_hash(reasons) != expected_reason_hash:
        raise EvidenceInvariantError("The complete low-confidence reason manifest is unavailable or invalid")

    snapshot = _ensure_review_snapshot(evidence)
    request_payload = {
        "evidence_id": str(evidence.id),
        "decision": decision,
        "expected_subject_version": expected_subject_version,
        "expected_subject_hash": expected_subject_hash,
        "expected_latest_decision_id": expected,
        "request_key": request_key,
        "reason": reason,
    }
    request_hash = _decision_request_hash(request_payload)
    existing = EvidenceReviewDecision.objects.filter(
        evidence_audit_snapshot=snapshot, request_key=request_key
    ).first()
    if existing:
        if existing.request_hash != request_hash:
            raise EvidenceConflict("The request key was already used with a different payload")
        return existing, False
    if decision not in EvidenceReviewDecision.Decision.values:
        raise ValidationError({"decision": "Unsupported evidence review decision"})

    provenance_type, extraction_run, generic_attempt = _provenance_for_review(evidence)
    now = timezone.now()
    record = EvidenceReviewDecision(
        evidence_asset=evidence,
        evidence_audit_snapshot=snapshot,
        decision_provenance_type=provenance_type,
        review_subject_schema_version=expected_subject_version,
        review_subject_hash=expected_subject_hash,
        extraction_run=extraction_run,
        generic_extraction_attempt=generic_attempt,
        supersedes_decision=evidence.latest_review_decision,
        request_key=request_key,
        request_hash=request_hash,
        decision=decision,
        reason=reason,
        reviewer_admin=admin,
        decided_at=now,
    )
    record.full_clean()
    record.save()
    evidence.latest_review_decision = record
    evidence.manual_reviewed_at = now
    evidence.manual_reviewed_by = admin
    if decision == EvidenceReviewDecision.Decision.APPROVED:
        evidence.manual_review_required = False
        evidence.review_state = ReviewState.PASSED
    else:
        evidence.manual_review_required = True
        evidence.review_state = ReviewState.REJECTED
    evidence.publishable = False
    evidence.save(update_fields=(
        "latest_review_decision", "manual_reviewed_at", "manual_reviewed_by",
        "manual_review_required", "review_state", "publishable",
    ))
    if evidence.document_extraction_id:
        aggregate_document_extraction(evidence.document_extraction_id)
    else:
        evidence.publishable = calculate_publishable(evidence)
        evidence.save(update_fields=("publishable",))
    correlation_id = (
        evidence.origin_run_source_item.run_id
        if evidence.origin_run_source_item_id
        else evidence.id
    )
    AuditEvent.objects.record(
        correlation_id=correlation_id,
        actor_type=AuditEvent.ActorType.ADMIN,
        actor_id=admin.pk,
        action="evidence.review_decided",
        entity_type="EvidenceAsset",
        entity_id=evidence.id,
        before_hash=expected_subject_hash,
        after_hash=evidence.review_subject_hash,
        reason_code=decision,
    )
    enqueue_event(
        topic="evidence.review_decided",
        aggregate_type="EvidenceAsset",
        aggregate_id=evidence.id,
        message_key=f"evidence.review_decided:{snapshot.id}:{request_key}",
        correlation_id=correlation_id,
        payload={
            "evidence_audit_snapshot_id": str(snapshot.id),
            "evidence_asset_id": str(evidence.id),
            "review_decision_id": str(record.id),
            "review_subject_schema_version": record.review_subject_schema_version,
            "review_subject_hash": record.review_subject_hash,
            "request_key": request_key,
            "decision": decision,
        },
    )
    return record, True


@dataclass(frozen=True)
class PageRoute:
    page_index: int
    engine: str
    reason: str


def route_pdf_pages(page_signals: Iterable[Mapping[str, Any]]) -> list[PageRoute]:
    routes: list[PageRoute] = []
    for signal in page_signals:
        page_index = int(signal["page_index"])
        text_chars = int(signal.get("text_chars", 0))
        replacement_ratio = float(signal.get("replacement_ratio", 0.0))
        complex_layout = bool(signal.get("complex_layout", False))
        if text_chars < int(signal.get("minimum_text_chars", 40)):
            routes.append(PageRoute(page_index, ExtractionEngine.PADDLEOCR, "native_text_missing"))
        elif replacement_ratio > float(signal.get("maximum_replacement_ratio", 0.02)):
            routes.append(PageRoute(page_index, ExtractionEngine.PADDLEOCR, "native_text_quality_low"))
        elif complex_layout:
            routes.append(PageRoute(page_index, ExtractionEngine.PADDLEOCR, "structure_recognition_required"))
        else:
            routes.append(PageRoute(page_index, ExtractionEngine.NATIVE_PDF, "native_text_accepted"))
    return sorted(routes, key=lambda route: route.page_index)


def group_routes(routes: Sequence[PageRoute]) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for route in routes:
        grouped.setdefault(route.engine, []).append(route.page_index)
    return {engine: sorted(pages) for engine, pages in grouped.items()}
