from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone

from adapters.extractors.base import (
    ExtractorError,
    GenericExtractionOutput,
    canonical_bytes,
    sha256_bytes,
    sniff_mime,
)
from adapters.extractors.html import HtmlExtractor
from adapters.extractors.hwpx import HwpxExtractor
from adapters.extractors.legacy_hwp import LegacyHwpConverter
from adapters.extractors.native_pdf import NativePdfExtractor
from adapters.extractors.paddleocr import PaddleOCRExtractor
from adapters.extractors.spreadsheet import SpreadsheetExtractor
from adapters.extractors.structured import StructuredDataExtractor
from adapters.storage import ObjectInfo, S3ObjectStorage
from adapters.storage.s3 import content_addressed_key
from apps.collection.models import (
    CollectionRun,
    RecoveryState,
    RunState,
    RunStep,
)
from apps.collection.services import (
    begin_step_observation,
    project_run_terminal_observation,
    project_step_terminal_observation,
)
from wisdome_writer.infrastructure.http_safety import (
    HttpSafetyError,
    OutboundResponseTooLarge,
    UnsafeOutboundUrl,
    redact_url,
    safe_get,
)
from wisdome_writer.infrastructure.outbox import PermanentEventError, enqueue_event

from .models import (
    DocumentExtraction,
    DocumentInputKind,
    EvidenceAsset,
    EvidenceDerivationType,
    EvidenceKind,
    ExtractionEngine,
    ExtractionProfileSnapshot,
    ExtractionRun,
    ExtractionState,
    GenericExtractionAttempt,
    GenericValidationMode,
    LocatorType,
    ProfileApprovalState,
    ReviewState,
    RightsStatus,
)
from .services import (
    aggregate_document_extraction,
    calculate_publishable,
    calculate_review_subject_hash,
    canonical_hash,
    evidence_content_hash,
    extraction_fingerprint,
    generic_extraction_fingerprint,
    group_routes,
    normalize_low_confidence_reasons,
    profile_material,
    route_pdf_pages,
)

MAX_ATTACHMENT_BYTES = 200 * 1024 * 1024


def _begin_domain_step_observation(
    step: RunStep,
    run: CollectionRun,
    *,
    started_at,
) -> None:
    begin_step_observation(
        step,
        run,
        started_at=started_at,
    )
    step.retry_count = max(step.attempt_no - 1, 0)


def _project_domain_step_terminal(
    step: RunStep,
    run: CollectionRun,
    *,
    finished_at,
    final_state: str,
    affected_count: int = 0,
    error_code: str | None = None,
    recovery_state: str = RecoveryState.NOT_REQUIRED,
) -> None:
    project_step_terminal_observation(
        step,
        run,
        finished_at=finished_at,
        final_state=final_state,
        affected_count=affected_count,
        error_code=error_code,
        recovery_state=recovery_state,
    )
    step.retry_count = max(step.attempt_no - 1, 0)


def _document_input_fingerprint(
    *,
    run_source_item_id: Any,
    input_kind: str,
    input_checksum: str,
) -> str:
    return canonical_hash(
        {
            "schema": "document-input-v1",
            "run_source_item_id": str(run_source_item_id),
            "input_kind": input_kind,
            "input_checksum": input_checksum,
        }
    )


def _raw_input_fingerprint(
    *,
    run_source_item_id: Any,
    source_item_id: Any,
    input_kind: str,
    input_hash: str,
) -> str:
    return canonical_hash(
        {
            "schema": "raw-input-v1",
            "run_source_item_id": str(run_source_item_id),
            "source_item_id": str(source_item_id),
            "input_kind": input_kind,
            "input_hash": input_hash,
        }
    )


def _get_or_create_document_extraction(
    *,
    run_source_item,
    source_item,
    input_asset: EvidenceAsset,
    input_object_key: str,
    input_object_version: str,
    input_kind: str,
    input_mime_type: str,
    input_checksum: str,
    input_page_count: int,
    expected_page_indices: list[int],
    input_frame_count: int | None = None,
) -> DocumentExtraction:
    fingerprint = _document_input_fingerprint(
        run_source_item_id=run_source_item.id,
        input_kind=input_kind,
        input_checksum=input_checksum,
    )
    document, created = DocumentExtraction.objects.get_or_create(
        input_fingerprint=fingerprint,
        defaults={
            "run_source_item": run_source_item,
            "source_item": source_item,
            "input_asset": input_asset,
            "input_object_key": input_object_key,
            "input_object_version": input_object_version,
            "input_kind": input_kind,
            "input_mime_type": input_mime_type,
            "input_frame_count": input_frame_count,
            "input_checksum": input_checksum,
            "input_page_count": input_page_count,
            "expected_page_indices": expected_page_indices,
        },
    )
    if not created and (
        document.run_source_item_id != run_source_item.id
        or document.source_item_id != source_item.id
        or document.input_kind != input_kind
        or document.input_checksum != input_checksum
    ):
        raise PermanentEventError("document_input_identity_conflict")
    return document


def _has_complete_legacy_hwp_pdf(evidence: EvidenceAsset | None) -> bool:
    if evidence is None:
        return False
    checksum = evidence.checksum
    return (
        bool(evidence.object_key)
        and isinstance(checksum, str)
        and len(checksum) == 64
        and all(character in "0123456789abcdef" for character in checksum)
    )


def _fail_incomplete_legacy_hwp_locked(
    attempt: GenericExtractionAttempt,
    *,
    error_code: str,
) -> GenericExtractionAttempt:
    attempt.state = ExtractionState.FAILED
    attempt.error_code = error_code
    attempt.error_detail_redacted = (
        "legacy HWP converted PDF material is incomplete"
    )
    attempt.finished_at = timezone.now()
    attempt.save(
        update_fields=(
            "state",
            "error_code",
            "error_detail_redacted",
            "finished_at",
            "updated_at",
        )
    )
    _enqueue_finalize(
        str(attempt.run_source_item.run_id),
        f"generic-terminal:{attempt.id}",
    )
    return attempt


def _converge_legacy_hwp_document_locked(
    attempt: GenericExtractionAttempt,
) -> GenericExtractionAttempt:
    if (
        attempt.engine != ExtractionEngine.LEGACY_HWP
        or attempt.state
        not in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE)
    ):
        return attempt

    evidence = attempt.evidence_asset
    if not _has_complete_legacy_hwp_pdf(evidence):
        recoverable = list(
            attempt.derived_evidence_assets.filter(
                origin_run_source_item_id=attempt.run_source_item_id,
                source_item_id=attempt.source_item_id,
                object_key__isnull=False,
                checksum__isnull=False,
            )
            .exclude(object_key="")
            .exclude(checksum="")
            .order_by("created_at", "id")[:2]
        )
        if len(recoverable) == 1 and _has_complete_legacy_hwp_pdf(
            recoverable[0]
        ):
            evidence = recoverable[0]
            attempt.evidence_asset = evidence
            attempt.save(update_fields=("evidence_asset", "updated_at"))
        else:
            error_code = (
                "legacy_hwp_evidence_ambiguous"
                if len(recoverable) > 1
                else "legacy_hwp_conversion_material_missing"
            )
            return _fail_incomplete_legacy_hwp_locked(
                attempt,
                error_code=error_code,
            )

    document = _get_or_create_document_extraction(
        run_source_item=attempt.run_source_item,
        source_item=attempt.source_item,
        input_asset=evidence,
        input_object_key=evidence.object_key,
        input_object_version=evidence.object_version or evidence.checksum,
        input_kind=DocumentInputKind.PDF,
        input_mime_type="application/pdf",
        input_checksum=evidence.checksum,
        input_page_count=1,
        expected_page_indices=[0],
    )
    _enqueue_document_extraction(document)
    return attempt


def _ensure_legacy_hwp_document(
    attempt_id: Any,
) -> GenericExtractionAttempt:
    with transaction.atomic():
        attempt = (
            GenericExtractionAttempt.objects.select_for_update()
            .select_related(
                "run_source_item",
                "source_item",
                "evidence_asset",
            )
            .get(pk=attempt_id)
        )
        return _converge_legacy_hwp_document_locked(attempt)


def _enqueue_document_extraction(document: DocumentExtraction) -> None:
    enqueue_event(
        event_type="evidence.document_route_requested",
        aggregate_type="document_extraction",
        aggregate_id=document.id,
        job_id=document.run_source_item.run_id,
        dedupe_key=f"evidence.document_route_requested:{document.id}",
        payload={
            "run_id": str(document.run_source_item.run_id),
            "run_source_item_id": str(document.run_source_item_id),
            "source_item_id": str(document.source_item_id),
            "input_asset_id": (
                str(document.input_asset_id) if document.input_asset_id else None
            ),
            "input_kind": str(document.input_kind),
            "document_extraction_id": str(document.id),
            "input_checksum": document.input_checksum,
        },
    )


def _enqueue_generic_extraction(attempt: GenericExtractionAttempt) -> None:
    enqueue_event(
        event_type="evidence.other_extract_requested",
        aggregate_type="generic_extraction_attempt",
        aggregate_id=attempt.id,
        job_id=attempt.run_source_item.run_id,
        dedupe_key=f"evidence.other_extract_requested:{attempt.id}",
        payload={
            "run_id": str(attempt.run_source_item.run_id),
            "run_source_item_id": str(attempt.run_source_item_id),
            "source_item_id": str(attempt.source_item_id),
            "input_asset_id": (
                str(attempt.input_asset_id) if attempt.input_asset_id else None
            ),
            "generic_extraction_attempt_id": str(attempt.id),
            "profile_snapshot_id": str(attempt.extraction_profile_snapshot_id),
            "profile_material_hash": attempt.profile_material_hash,
            "engine": attempt.engine,
            "extractor_version": attempt.extractor_version,
            "config_hash": attempt.config_hash,
            "validation_mode": attempt.validation_mode,
            "calibration_profile_key": attempt.calibration_profile_key,
            "calibration_profile_version": attempt.calibration_profile_version,
            "calibration_profile_hash": attempt.calibration_profile_hash,
            "fingerprint_schema_version": attempt.fingerprint_schema_version,
            "extraction_fingerprint": attempt.extraction_fingerprint,
        },
    )


def _enqueue_finalize(run_id: str, cause: str) -> None:
    enqueue_event(
        event_type="evidence.finalize_requested",
        aggregate_type="collection_run",
        aggregate_id=run_id,
        job_id=run_id,
        dedupe_key=f"evidence.finalize_requested:{run_id}:{cause}",
        payload={"run_id": str(run_id)},
    )


def _storage() -> S3ObjectStorage:
    return S3ObjectStorage()


def _rights(run_source_item) -> dict[str, Any]:
    config = run_source_item.source_snapshot.config or {}
    status = config.get("rightsStatus", RightsStatus.UNKNOWN)
    if status not in RightsStatus.values:
        status = RightsStatus.UNKNOWN
    source = run_source_item.source_snapshot.source
    return {
        "rights_status": status,
        "rights_basis_url": config.get("rightsBasisUrl") or source.base_url,
        "attribution_text": config.get("attributionText") or f"출처: {source.owner_name}",
    }


def _profile(engine: str, *, preferred_key: str | None = None) -> ExtractionProfileSnapshot:
    queryset = ExtractionProfileSnapshot.objects.filter(
        engine=engine, approval_state=ProfileApprovalState.APPROVED
    )
    if preferred_key:
        preferred = queryset.filter(profile_key=preferred_key).order_by("-approved_at").first()
        if preferred:
            _verify_profile(preferred)
            return preferred
    profile = queryset.order_by("-approved_at", "-created_at").first()
    if profile is None:
        raise ExtractorError("profile_not_approved", f"No approved extraction profile for {engine}")
    _verify_profile(profile)
    return profile


def _verify_profile(profile: ExtractionProfileSnapshot) -> None:
    if canonical_hash(profile.config) != profile.config_hash:
        raise ExtractorError("config_mismatch", "Extraction profile config hash does not match")
    value = {
        "extractor_version": profile.extractor_version,
        "package_version": profile.package_version,
        "runtime_version": profile.runtime_version,
        "pipeline_name": profile.pipeline_name,
        "implementation_manifest_hash": profile.implementation_manifest_hash,
        "config_hash": profile.config_hash,
        "validation_mode": profile.validation_mode,
        "calibration_profile_key": profile.calibration_profile_key,
        "calibration_profile_version": profile.calibration_profile_version,
        "calibration_profile_hash": profile.calibration_profile_hash,
        "model_manifest_hash": profile.model_manifest_hash,
    }
    if canonical_hash(value) != profile.profile_material_hash:
        raise ExtractorError("profile_material_mismatch", "Extraction profile material hash does not match")
    if profile.model_manifest is not None and canonical_hash(profile.model_manifest) != profile.model_manifest_hash:
        raise ExtractorError("model_manifest_mismatch", "Model manifest hash does not match")


def _create_raw_evidence(run_source_item) -> EvidenceAsset:
    item = run_source_item.source_item
    content_hash = evidence_content_hash(text=item.body_text, structured_data=item.metadata, checksum=None)
    fingerprint = _raw_input_fingerprint(
        run_source_item_id=run_source_item.id,
        source_item_id=item.id,
        input_kind="source_record",
        input_hash=content_hash,
    )
    rights = _rights(run_source_item)
    with transaction.atomic():
        evidence, created = EvidenceAsset.objects.get_or_create(
            raw_input_fingerprint=fingerprint,
            defaults={
                "source_item": item,
                "origin_run_source_item": run_source_item,
                "derivation_type": EvidenceDerivationType.RAW,
                "kind": EvidenceKind.TEXT,
                "locator_type": LocatorType.STRUCTURED_PATH,
                "locator": {
                    "locator_type": "structured_path",
                    "path_type": "record_key",
                    "path": "body_text",
                },
                "extracted_text": item.body_text,
                "structured_data": item.metadata,
                "extraction_method": "source_record",
                "extractor_version": "v1",
                "evidence_content_hash": content_hash,
                "review_subject_hash": "0" * 64,
                "review_state": ReviewState.PASSED,
                **rights,
            },
        )
        if not created:
            if (
                evidence.source_item_id != item.id
                or evidence.origin_run_source_item_id != run_source_item.id
                or evidence.derivation_type != EvidenceDerivationType.RAW
                or evidence.kind != EvidenceKind.TEXT
                or evidence.evidence_content_hash != content_hash
            ):
                raise PermanentEventError("raw_input_identity_conflict")
            return evidence
        evidence.review_subject_hash = calculate_review_subject_hash(evidence)
        evidence.publishable = calculate_publishable(evidence)
        evidence.full_clean()
        evidence.save(
            update_fields=(
                "review_subject_hash",
                "publishable",
                "updated_at",
            )
        )
        return evidence


def _download_attachment(run_source_item, attachment: Mapping[str, Any]) -> tuple[bytes, str, str]:
    url = str(attachment.get("url", ""))
    parsed = urlparse(url)
    source = run_source_item.source_snapshot.source
    allowed_hosts = {urlparse(source.base_url).hostname}
    allowed_hosts.update(run_source_item.source_snapshot.config.get("allowedAttachmentHosts", []))
    try:
        response = safe_get(
            url,
            max_bytes=MAX_ATTACHMENT_BYTES,
            timeout=httpx.Timeout(60, connect=10),
            allowed_hosts=allowed_hosts,
            headers={"User-Agent": "WisdomeSuperWriter/0.1 (+admin-managed research bot)"},
            max_elapsed_seconds=60.0,
        )
        response.raise_for_status()
    except OutboundResponseTooLarge:
        raise ExtractorError(
            "attachment_limit_exceeded",
            "Attachment exceeds the configured byte limit",
        ) from None
    except UnsafeOutboundUrl:
        raise ExtractorError(
            "attachment_url_not_allowed",
            "Attachment URL is outside the approved public source hosts",
        ) from None
    except HttpSafetyError as exc:
        raise ExtractorError(
            "attachment_download_failed",
            str(exc),
        ) from None
    mime_type = (
        response.headers.get("content-type", "application/octet-stream")
        .split(";", 1)[0]
        .strip()
    )
    filename = Path(parsed.path).name or "attachment.bin"
    return response.content, mime_type, filename


def _persist_attachment(run_source_item, attachment: Mapping[str, Any], data: bytes, mime_type: str, filename: str):
    checksum = hashlib.sha256(data).hexdigest()
    key = content_addressed_key(namespace="evidence/raw", checksum_sha256=checksum, filename=filename)
    info = _storage().put_bytes(
        key=key,
        data=data,
        content_type=mime_type,
        checksum_sha256=checksum,
        metadata={
            "source_item_id": str(run_source_item.source_item_id),
            "run_source_item_id": str(run_source_item.id),
        },
    )
    rights = _rights(run_source_item)
    content_hash = evidence_content_hash(text=None, structured_data={"title": attachment.get("title")}, checksum=checksum)
    fingerprint = _raw_input_fingerprint(
        run_source_item_id=run_source_item.id,
        source_item_id=run_source_item.source_item_id,
        input_kind="attachment",
        input_hash=checksum,
    )
    with transaction.atomic():
        evidence, created = EvidenceAsset.objects.get_or_create(
            raw_input_fingerprint=fingerprint,
            defaults={
                "source_item": run_source_item.source_item,
                "origin_run_source_item": run_source_item,
                "derivation_type": EvidenceDerivationType.RAW,
                "kind": EvidenceKind.ATTACHMENT,
                "locator_type": LocatorType.STRUCTURED_PATH,
                "locator": {
                    "locator_type": "structured_path",
                    "path_type": "record_key",
                    "path": "attachments",
                },
                "object_key": info.key,
                "object_version": (
                    info.version_id or info.etag or checksum
                ),
                "mime_type": info.content_type,
                "byte_size": info.size,
                "checksum": checksum,
                "structured_data": {
                    "title": attachment.get("title"),
                    "source_url": redact_url(
                        str(attachment.get("url", ""))
                    ),
                },
                "evidence_content_hash": content_hash,
                "review_subject_hash": "0" * 64,
                "review_state": ReviewState.PASSED,
                **rights,
            },
        )
        if not created:
            if (
                evidence.source_item_id
                != run_source_item.source_item_id
                or evidence.origin_run_source_item_id
                != run_source_item.id
                or evidence.derivation_type != EvidenceDerivationType.RAW
                or evidence.kind != EvidenceKind.ATTACHMENT
                or evidence.checksum != checksum
                or not evidence.object_key
                or not evidence.object_key.startswith(
                    f"evidence/raw/{checksum[:2]}/{checksum}/"
                )
                or not evidence.object_version
                or evidence.byte_size is None
                or evidence.byte_size != len(data)
                or not evidence.mime_type
                or info.checksum_sha256 != checksum
            ):
                raise PermanentEventError("raw_input_identity_conflict")
            canonical_info = ObjectInfo(
                key=evidence.object_key,
                version_id=evidence.object_version,
                checksum_sha256=checksum,
                size=evidence.byte_size,
                content_type=evidence.mime_type,
                etag=None,
            )
            return evidence, canonical_info
        evidence.review_subject_hash = calculate_review_subject_hash(evidence)
        evidence.publishable = calculate_publishable(evidence)
        evidence.full_clean()
        evidence.save(
            update_fields=(
                "review_subject_hash",
                "publishable",
                "updated_at",
            )
        )
        return evidence, info


def _store_result(namespace: str, aggregate_id: Any, output: Mapping[str, Any]):
    data = canonical_bytes(output)
    checksum = sha256_bytes(data)
    key = f"evidence/results/{namespace}/{aggregate_id}/{checksum}.json"
    return _storage().put_bytes(
        key=key,
        data=data,
        content_type="application/json",
        checksum_sha256=checksum,
        metadata={"aggregate_id": str(aggregate_id)},
    )


def _make_document_evidence(document, run, output, run_source_item) -> list[EvidenceAsset]:
    rights = _rights(run_source_item)
    reasons = normalize_low_confidence_reasons(output.low_confidence_reasons)
    manual = run.state == ExtractionState.LOW_CONFIDENCE
    created: list[EvidenceAsset] = []
    for page in output.pages:
        for block in page.blocks:
            kind = {
                "table": EvidenceKind.TABLE,
                "chart": EvidenceKind.CHART,
                "image": EvidenceKind.IMAGE,
            }.get(block.block_type, EvidenceKind.TEXT)
            locator = {
                "locator_type": "document_block",
                "page_index": page.page_index,
                "block_id": block.block_id,
                "block_type": block.block_type,
                "polygon": block.polygon,
                "bbox": block.bbox,
                "reading_order": block.reading_order,
            }
            content_hash = evidence_content_hash(
                text=block.text, structured_data=block.structured_data, checksum=None
            )
            evidence = EvidenceAsset.objects.filter(
                extraction_run=run, evidence_content_hash=content_hash, locator=locator
            ).first()
            if evidence:
                created.append(evidence)
                continue
            evidence = EvidenceAsset.objects.create(
                source_item=document.source_item,
                origin_run_source_item=run_source_item,
                derivation_type=EvidenceDerivationType.DOCUMENT,
                document_extraction=document,
                extraction_run=run,
                parent_asset=document.input_asset,
                kind=kind,
                locator_type=LocatorType.DOCUMENT_BLOCK,
                locator=locator,
                extracted_text=block.text,
                structured_data=block.structured_data,
                extraction_method=run.engine,
                extractor_version=run.package_version,
                confidence=block.confidence,
                confidence_detail=output.confidence_summary,
                low_confidence_reasons=reasons,
                evidence_content_hash=content_hash,
                review_subject_hash="0" * 64,
                review_state=ReviewState.MANUAL_REQUIRED if manual else ReviewState.PASSED,
                manual_review_required=manual,
                publishable=False,
                alt_text=(f"{document.source_item.title} {block.block_type} 영역" if kind in (
                    EvidenceKind.IMAGE, EvidenceKind.CHART
                ) else None),
                **rights,
            )
            evidence.review_subject_hash = calculate_review_subject_hash(evidence)
            evidence.full_clean()
            evidence.save(update_fields=("review_subject_hash", "updated_at"))
            created.append(evidence)
    return created


def _run_document_extraction(document_id: Any) -> DocumentExtraction:
    document = DocumentExtraction.objects.select_related(
        "run_source_item__run",
        "run_source_item__source_snapshot__source",
        "source_item",
        "input_asset__generic_extraction_attempt__input_asset",
        "input_asset__producing_generic_attempt__input_asset",
        "input_asset__parent_asset",
    ).get(pk=document_id)
    if _is_audit_only_document(document):
        return document
    if document.state in (
        ExtractionState.SUCCEEDED,
        ExtractionState.LOW_CONFIDENCE,
    ):
        return document
    data = _storage().get_bytes(key=document.input_object_key, version_id=document.input_object_version or None)
    if hashlib.sha256(data).hexdigest() != document.input_checksum:
        raise ExtractorError("input_checksum_mismatch", "Downloaded input checksum differs from provenance")
    suffix = ".pdf" if document.input_kind == DocumentInputKind.PDF else ".img"
    with tempfile.TemporaryDirectory(prefix="wisdome-evidence-") as temp_dir:
        path = Path(temp_dir) / f"input{suffix}"
        path.write_bytes(data)
        native = NativePdfExtractor()
        if document.input_kind == DocumentInputKind.PDF:
            inspection = native.inspect(path)
            if inspection.checksum != document.input_checksum:
                raise ExtractorError("input_checksum_mismatch", "Local PDF checksum differs from provenance")
            document.input_page_count = inspection.page_count
            document.expected_page_indices = list(range(inspection.page_count))
            routes = route_pdf_pages(signal.as_dict() for signal in inspection.page_signals)
        else:
            from PIL import Image
            with Image.open(path) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise ExtractorError("unsupported_multiframe_image", "Animated/multi-frame images are rejected")
            document.input_page_count = 1
            document.input_frame_count = 1
            document.expected_page_indices = [0]
            from .services import PageRoute
            routes = [PageRoute(0, ExtractionEngine.PADDLEOCR, "standalone_image")]
        input_page_count = document.input_page_count
        input_frame_count = document.input_frame_count
        expected_page_indices = document.expected_page_indices
        with transaction.atomic():
            document = (
                DocumentExtraction.objects.select_for_update()
                .select_related(
                    "run_source_item__run",
                    "run_source_item__source_snapshot__source",
                    "source_item",
                    "input_asset__generic_extraction_attempt__input_asset",
                    "input_asset__producing_generic_attempt__input_asset",
                    "input_asset__parent_asset",
                )
                .get(pk=document_id)
            )
            if document.state not in (
                ExtractionState.QUEUED,
                ExtractionState.RUNNING,
            ):
                return document
            document.input_page_count = input_page_count
            document.input_frame_count = input_frame_count
            document.expected_page_indices = expected_page_indices
            document.state = ExtractionState.RUNNING
            document.started_at = document.started_at or timezone.now()
            document.full_clean()
            document.save()

        selected_by_engine: dict[str, ExtractionRun] = {}
        for engine, pages in group_routes(routes).items():
            preferred = None
            if engine == ExtractionEngine.PADDLEOCR:
                language = document.run_source_item.source_snapshot.config.get("ocrLanguage", "ko")
                preferred = "paddle-en-v1" if language == "en" else "paddle-ko-v1"
            elif engine == ExtractionEngine.NATIVE_PDF:
                preferred = "native-pdf-v1"
            profile = _profile(engine, preferred_key=preferred)
            page_set_hash, fingerprint = extraction_fingerprint(document, profile, pages)
            run, _ = ExtractionRun.objects.get_or_create(
                document_extraction=document,
                extraction_fingerprint=fingerprint,
                defaults={
                    "extraction_profile_snapshot": profile,
                    "engine": engine,
                    "page_set_hash": page_set_hash,
                    "requested_page_indices": pages,
                    "profile_key": profile.profile_key,
                    "profile_version": profile.profile_version,
                    "config_hash": profile.config_hash,
                    "profile_material_hash": profile.profile_material_hash,
                    "package_version": profile.package_version or profile.extractor_version,
                    "runtime_version": profile.runtime_version or profile.extractor_version,
                    "pipeline_name": profile.pipeline_name,
                    "model_manifest": profile.model_manifest,
                    "model_manifest_hash": profile.model_manifest_hash,
                    "language_profile": profile.config.get("text_recognition_model"),
                    "device_type": profile.config.get("device"),
                },
            )
            if run.state in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE):
                selected_by_engine[engine] = run
                continue
            run.state = ExtractionState.RUNNING
            run.started_at = timezone.now()
            run.save(update_fields=("state", "started_at", "updated_at"))
            started = timezone.now()
            if engine == ExtractionEngine.NATIVE_PDF:
                output = NativePdfExtractor(profile.config).extract(path, pages)
            else:
                output = PaddleOCRExtractor(
                    profile.config,
                    model_manifest=profile.model_manifest or {},
                    model_manifest_hash=profile.model_manifest_hash or "",
                    config_hash=profile.config_hash,
                ).extract(path, pages, input_kind=document.input_kind)
            result = _store_result("document", run.id, output.as_dict())
            reasons = normalize_low_confidence_reasons(output.low_confidence_reasons)
            terminal_state = (
                ExtractionState.LOW_CONFIDENCE
                if reasons
                else ExtractionState.SUCCEEDED
            )
            reason_info = None
            if reasons:
                reason_info = _store_result("reasons", run.id, {"reasons": reasons})
            finished_at = timezone.now()
            duration_ms = int((finished_at - started).total_seconds() * 1000)
            with transaction.atomic():
                run = ExtractionRun.objects.select_for_update().get(pk=run.pk)
                if run.state in (
                    ExtractionState.SUCCEEDED,
                    ExtractionState.LOW_CONFIDENCE,
                ):
                    selected_by_engine[engine] = run
                    continue
                run.state = terminal_state
                run.processed_page_indices = list(output.processed_page_indices)
                run.result_object_key = result.key
                run.result_checksum = result.checksum_sha256
                run.runtime_version = output.runtime_version
                run.package_version = output.package_version
                run.pipeline_name = output.pipeline_name
                run.device_type = output.device_type
                run.low_confidence_reasons = reasons or None
                run.low_confidence_reasons_hash = (
                    canonical_hash(reasons) if reasons else None
                )
                if reason_info is not None:
                    run.low_confidence_reasons_object_key = reason_info.key
                    run.low_confidence_reasons_object_version = (
                        reason_info.version_id or reason_info.etag
                    )
                run.duration_ms = duration_ms
                run.finished_at = finished_at
                run.full_clean()
                run.save()
                _make_document_evidence(
                    document,
                    run,
                    output,
                    document.run_source_item,
                )
            selected_by_engine[engine] = run

        document.routing_manifest = {
            "schema_version": "v1",
            "pages": [
                {
                    "page_index": route.page_index,
                    "selected_run_id": str(selected_by_engine[route.engine].id),
                    "engine": route.engine,
                    "reason": route.reason,
                }
                for route in routes
            ],
        }
        document.save(update_fields=("routing_manifest", "updated_at"))
    with transaction.atomic():
        document = aggregate_document_extraction(document.id)
        if document.document_complete:
            enqueue_event(
                topic="evidence.document_ready",
                aggregate_type="DocumentExtraction",
                aggregate_id=document.id,
                message_key=f"evidence.document_ready:{document.id}:{document.selected_evidence_manifest_hash}",
                payload={
                    "run_id": str(document.run_source_item.run_id),
                    "run_source_item_id": str(document.run_source_item_id),
                    "source_item_id": str(document.source_item_id),
                    "document_extraction_id": str(document.id),
                    "input_page_count": document.input_page_count,
                    "coverage_manifest_hash": document.coverage_manifest_hash,
                    "selected_evidence_manifest_hash": document.selected_evidence_manifest_hash,
                    "document_complete": True,
                },
            )
        elif document.state in {
            ExtractionState.FAILED,
            ExtractionState.LOW_CONFIDENCE,
        }:
            _enqueue_finalize(
                str(document.run_source_item.run_id),
                f"document-terminal:{document.id}:{document.state}",
            )
    return document


def _generic_extractor(profile: ExtractionProfileSnapshot):
    mapping = {
        ExtractionEngine.HTML: HtmlExtractor,
        ExtractionEngine.STRUCTURED: StructuredDataExtractor,
        ExtractionEngine.SPREADSHEET: SpreadsheetExtractor,
        ExtractionEngine.HWPX: HwpxExtractor,
        ExtractionEngine.LEGACY_HWP: LegacyHwpConverter,
    }
    try:
        return mapping[profile.engine](profile.config)
    except KeyError as exc:
        raise ExtractorError("generic_engine_unsupported", f"No local adapter for {profile.engine}") from exc


def _is_quarantined_generic_attempt(
    attempt: GenericExtractionAttempt,
) -> bool:
    input_asset = attempt.input_asset
    return bool(
        input_asset is not None
        and (
            (
                input_asset.derivation_type == EvidenceDerivationType.RAW
                and input_asset.raw_input_fingerprint is None
            )
            or (
                input_asset.parent_asset_id is not None
                and input_asset.parent_asset.derivation_type
                == EvidenceDerivationType.RAW
                and input_asset.parent_asset.raw_input_fingerprint is None
            )
        )
    )


def _is_audit_only_document(
    document: DocumentExtraction,
) -> bool:
    if document.input_fingerprint is None:
        return True
    input_asset = document.input_asset
    if (
        input_asset is not None
        and input_asset.derivation_type == EvidenceDerivationType.RAW
        and input_asset.raw_input_fingerprint is None
    ):
        return True
    if (
        input_asset is not None
        and input_asset.parent_asset_id is not None
        and input_asset.parent_asset.derivation_type
        == EvidenceDerivationType.RAW
        and input_asset.parent_asset.raw_input_fingerprint is None
    ):
        return True
    if input_asset is None:
        return False
    attempts = []
    if input_asset.generic_extraction_attempt_id is not None:
        attempts.append(input_asset.generic_extraction_attempt)
    producing_attempt = getattr(
        input_asset,
        "producing_generic_attempt",
        None,
    )
    if producing_attempt is not None:
        attempts.append(producing_attempt)
    return any(
        _is_quarantined_generic_attempt(attempt)
        for attempt in attempts
    )


def _run_generic_extraction(attempt_id: Any) -> GenericExtractionAttempt:
    attempt = GenericExtractionAttempt.objects.select_related(
        "run_source_item__run", "run_source_item__source_snapshot__source", "source_item",
        "input_asset__parent_asset", "evidence_asset", "extraction_profile_snapshot",
    ).get(pk=attempt_id)
    if _is_quarantined_generic_attempt(attempt):
        return attempt
    if attempt.state in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE):
        return _ensure_legacy_hwp_document(attempt.id)
    profile = attempt.extraction_profile_snapshot
    _verify_profile(profile)
    if attempt.input_asset is None or not attempt.input_asset.object_key:
        raise ExtractorError("generic_input_missing", "Generic extraction input object is missing")
    data = _storage().get_bytes(
        key=attempt.input_asset.object_key,
        version_id=attempt.input_asset.object_version or None,
    )
    if attempt.input_asset.checksum and hashlib.sha256(data).hexdigest() != attempt.input_asset.checksum:
        raise ExtractorError("input_checksum_mismatch", "Downloaded input checksum differs from provenance")
    suffix = Path(str((attempt.input_asset.structured_data or {}).get("source_url", "input.bin"))).suffix
    with tempfile.TemporaryDirectory(prefix="wisdome-generic-") as temp_dir:
        path = Path(temp_dir) / f"input{suffix or '.bin'}"
        path.write_bytes(data)
        with transaction.atomic():
            GenericExtractionAttempt.objects.select_for_update().filter(pk=attempt.id).update(
                state=ExtractionState.RUNNING,
                started_at=timezone.now(),
            )
        output: GenericExtractionOutput = _generic_extractor(profile).extract(path)
        result = _store_result("generic", attempt.id, output.as_dict())
        reasons = normalize_low_confidence_reasons(output.low_confidence_reasons)
        reason_info = None
        if reasons:
            reason_info = _store_result("reasons", attempt.id, {"reasons": reasons})
        records = [record.as_dict() for record in output.records]
        first = output.records[0] if output.records else None
        if first is None:
            raise ExtractorError("generic_result_empty", "Generic extractor returned no evidence records")
        text = "\n\n".join(record.text for record in output.records if record.text)
        structured = {"records": records, "metadata": dict(output.metadata)}
        content_hash = evidence_content_hash(text=text or None, structured_data=structured, checksum=None)
        kind = first.kind if first.kind in EvidenceKind.values else EvidenceKind.TEXT
        legacy_info = None
        if profile.engine == ExtractionEngine.LEGACY_HWP:
            converted_path = Path(first.object_path or "")
            if not converted_path.is_file():
                raise ExtractorError("legacy_hwp_output_invalid", "Converted PDF disappeared before storage")
            converted_data = converted_path.read_bytes()
            converted_checksum = hashlib.sha256(converted_data).hexdigest()
            info = _storage().put_bytes(
                key=content_addressed_key(
                    namespace="evidence/converted", checksum_sha256=converted_checksum, filename="converted.pdf"
                ),
                data=converted_data,
                content_type="application/pdf",
                checksum_sha256=converted_checksum,
                metadata={"generic_attempt_id": str(attempt.id)},
            )
            legacy_info = (info, converted_checksum, len(converted_data))

        with transaction.atomic():
            attempt = GenericExtractionAttempt.objects.select_for_update().select_related(
                "run_source_item__run",
                "run_source_item__source_snapshot__source",
                "source_item",
                "input_asset__parent_asset",
                "extraction_profile_snapshot",
            ).get(pk=attempt_id)
            if _is_quarantined_generic_attempt(attempt):
                return attempt
            if attempt.state in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE):
                return _converge_legacy_hwp_document_locked(attempt)
            attempt.state = (
                ExtractionState.LOW_CONFIDENCE if reasons else ExtractionState.SUCCEEDED
            )
            attempt.result_checksum = result.checksum_sha256
            attempt.low_confidence_reasons_hash = canonical_hash(reasons) if reasons else None
            if reason_info:
                attempt.low_confidence_reasons_object_key = reason_info.key
                attempt.low_confidence_reasons_object_version = (
                    reason_info.version_id or reason_info.etag
                )
            attempt.finished_at = timezone.now()
            attempt.full_clean()
            attempt.save()

            manual = attempt.state == ExtractionState.LOW_CONFIDENCE
            rights = _rights(attempt.run_source_item)
            storage_fields = {}
            if legacy_info:
                info, converted_checksum, converted_size = legacy_info
                storage_fields = {
                    "object_key": info.key,
                    "object_version": info.version_id or info.etag or converted_checksum,
                    "mime_type": "application/pdf",
                    "byte_size": converted_size,
                    "checksum": converted_checksum,
                }
            evidence = EvidenceAsset.objects.create(
                source_item=attempt.source_item,
                origin_run_source_item=attempt.run_source_item,
                derivation_type=EvidenceDerivationType.OTHER,
                generic_extraction_attempt=attempt,
                parent_asset=attempt.input_asset,
                kind=kind,
                locator_type=first.locator_type,
                locator=dict(first.locator),
                extracted_text=text or None,
                structured_data=structured,
                extraction_method=profile.engine,
                extractor_version=profile.extractor_version,
                extraction_config_hash=profile.config_hash,
                validation_mode=profile.validation_mode,
                extraction_result_checksum=attempt.result_checksum,
                calibration_profile_key=profile.calibration_profile_key,
                calibration_profile_version=profile.calibration_profile_version,
                calibration_profile_hash=profile.calibration_profile_hash,
                confidence=(
                    first.confidence
                    if profile.validation_mode == GenericValidationMode.CALIBRATED
                    else None
                ),
                low_confidence_reasons=reasons,
                evidence_content_hash=content_hash,
                review_subject_hash="0" * 64,
                review_state=ReviewState.MANUAL_REQUIRED if manual else ReviewState.PASSED,
                manual_review_required=manual,
                alt_text=first.alt_text,
                **storage_fields,
                **rights,
            )
            evidence.review_subject_hash = calculate_review_subject_hash(evidence)
            evidence.publishable = calculate_publishable(evidence)
            evidence.full_clean()
            evidence.save(update_fields=("review_subject_hash", "publishable", "updated_at"))
            attempt.evidence_asset = evidence
            attempt.save(update_fields=("evidence_asset", "updated_at"))

            enqueue_event(
                topic="evidence.other_ready",
                aggregate_type="GenericExtractionAttempt",
                aggregate_id=attempt.id,
                message_key=f"evidence.other_ready:{attempt.id}:{attempt.result_checksum}",
                payload={
                    "run_id": str(attempt.run_source_item.run_id),
                    "run_source_item_id": str(attempt.run_source_item_id),
                    "source_item_id": str(attempt.source_item_id),
                    "generic_extraction_attempt_id": str(attempt.id),
                    "evidence_asset_id": str(evidence.id),
                    "engine": attempt.engine,
                    "locator_type": evidence.locator_type,
                    "validation_mode": attempt.validation_mode,
                    "result_checksum": attempt.result_checksum,
                    "low_confidence_reasons_hash": attempt.low_confidence_reasons_hash,
                    "calibration_profile_key": attempt.calibration_profile_key,
                    "calibration_profile_version": attempt.calibration_profile_version,
                    "calibration_profile_hash": attempt.calibration_profile_hash,
                    "extraction_fingerprint": attempt.extraction_fingerprint,
                },
            )

            if legacy_info:
                info, converted_checksum, _ = legacy_info
                document = _get_or_create_document_extraction(
                    run_source_item=attempt.run_source_item,
                    source_item=attempt.source_item,
                    input_asset=evidence,
                    input_object_key=info.key,
                    input_object_version=info.version_id or info.etag or converted_checksum,
                    input_kind=DocumentInputKind.PDF,
                    input_mime_type="application/pdf",
                    input_checksum=converted_checksum,
                    input_page_count=1,
                    expected_page_indices=[0],
                )
                _enqueue_document_extraction(document)
    return attempt


def _process_attachment(run_source_item, attachment: Mapping[str, Any]) -> None:
    data, declared_mime, filename = _download_attachment(run_source_item, attachment)
    with tempfile.TemporaryDirectory(prefix="wisdome-sniff-") as temp_dir:
        temp_path = Path(temp_dir) / filename
        temp_path.write_bytes(data)
        sniffed = sniff_mime(temp_path)
    mime_type = sniffed if sniffed != "application/octet-stream" else declared_mime
    raw_asset, info = _persist_attachment(run_source_item, attachment, data, mime_type, filename)
    suffix = Path(filename).suffix.lower()
    if mime_type == "application/pdf":
        with transaction.atomic():
            document = _get_or_create_document_extraction(
                run_source_item=run_source_item,
                source_item=run_source_item.source_item,
                input_asset=raw_asset,
                input_object_key=info.key,
                input_object_version=info.version_id or info.etag or raw_asset.checksum,
                input_kind=DocumentInputKind.PDF,
                input_mime_type=mime_type,
                input_checksum=raw_asset.checksum,
                input_page_count=1,
                expected_page_indices=[0],
            )
            _enqueue_document_extraction(document)
        return
    if mime_type in {"image/png", "image/jpeg", "image/tiff"}:
        with transaction.atomic():
            document = _get_or_create_document_extraction(
                run_source_item=run_source_item,
                source_item=run_source_item.source_item,
                input_asset=raw_asset,
                input_object_key=info.key,
                input_object_version=info.version_id or info.etag or raw_asset.checksum,
                input_kind=DocumentInputKind.STANDALONE_IMAGE,
                input_mime_type=mime_type,
                input_frame_count=1,
                input_checksum=raw_asset.checksum,
                input_page_count=1,
                expected_page_indices=[0],
            )
            _enqueue_document_extraction(document)
        return
    engine = None
    preferred = None
    if suffix == ".hwpx":
        engine, preferred = ExtractionEngine.HWPX, "hwpx-deterministic-v1"
    elif suffix == ".hwp":
        engine, preferred = ExtractionEngine.LEGACY_HWP, "legacy-hwp-v1"
    elif suffix in {".xlsx", ".xlsm", ".csv", ".tsv"}:
        engine, preferred = ExtractionEngine.SPREADSHEET, "spreadsheet-deterministic-v1"
    elif suffix in {".html", ".htm"}:
        engine, preferred = ExtractionEngine.HTML, "html-deterministic-v1"
    elif suffix in {".json", ".jsonld", ".xml", ".rss", ".atom"}:
        engine, preferred = ExtractionEngine.STRUCTURED, "structured-deterministic-v1"
    if not engine:
        return
    profile = _profile(engine, preferred_key=preferred)
    fingerprint = generic_extraction_fingerprint(
        run_source_item_id=run_source_item.id,
        source_item_id=run_source_item.source_item_id,
        input_asset_id=raw_asset.id,
        input_checksum=raw_asset.checksum,
        profile=profile,
    )
    with transaction.atomic():
        attempt, _ = GenericExtractionAttempt.objects.get_or_create(
            run_source_item=run_source_item,
            extraction_fingerprint=fingerprint,
            defaults={
                "source_item": run_source_item.source_item,
                "input_asset": raw_asset,
                "extraction_profile_snapshot": profile,
                "profile_material_hash": profile.profile_material_hash,
                "engine": profile.engine,
                "extractor_version": profile.extractor_version,
                "config_hash": profile.config_hash,
                "validation_mode": (
                    profile.validation_mode or GenericValidationMode.DETERMINISTIC
                ),
                "calibration_profile_key": profile.calibration_profile_key,
                "calibration_profile_version": profile.calibration_profile_version,
                "calibration_profile_hash": profile.calibration_profile_hash,
            },
        )
        _enqueue_generic_extraction(attempt)


def _queue_document_retry(document_id: str, exc: Exception) -> None:
    DocumentExtraction.objects.filter(
        pk=document_id,
        input_fingerprint__isnull=False,
        state__in=(
            ExtractionState.QUEUED,
            ExtractionState.RUNNING,
        ),
    ).exclude(
        input_asset__derivation_type=EvidenceDerivationType.RAW,
        input_asset__raw_input_fingerprint__isnull=True,
    ).exclude(
        input_asset__parent_asset__derivation_type=EvidenceDerivationType.RAW,
        input_asset__parent_asset__raw_input_fingerprint__isnull=True,
    ).exclude(
        input_asset__generic_extraction_attempt__input_asset__derivation_type=(
            EvidenceDerivationType.RAW
        ),
        input_asset__generic_extraction_attempt__input_asset__raw_input_fingerprint__isnull=(
            True
        ),
    ).exclude(
        input_asset__producing_generic_attempt__input_asset__derivation_type=(
            EvidenceDerivationType.RAW
        ),
        input_asset__producing_generic_attempt__input_asset__raw_input_fingerprint__isnull=(
            True
        ),
    ).update(
        state=ExtractionState.QUEUED,
        document_complete=False,
        error_code=str(getattr(exc, "code", exc.__class__.__name__))[:120],
        error_detail_redacted=str(
            getattr(exc, "detail_redacted", str(exc))
        )[:1000],
        finished_at=None,
    )


def _queue_generic_retry(attempt_id: str, exc: Exception) -> None:
    GenericExtractionAttempt.objects.filter(pk=attempt_id).exclude(
        input_asset__derivation_type=EvidenceDerivationType.RAW,
        input_asset__raw_input_fingerprint__isnull=True,
    ).exclude(
        input_asset__parent_asset__derivation_type=EvidenceDerivationType.RAW,
        input_asset__parent_asset__raw_input_fingerprint__isnull=True,
    ).update(
        state=ExtractionState.QUEUED,
        error_code=str(getattr(exc, "code", exc.__class__.__name__))[:120],
        error_detail_redacted=str(
            getattr(exc, "detail_redacted", str(exc))
        )[:1000],
        finished_at=None,
    )


@shared_task
def finalize_document_extraction_failure(document_id: str, error_code: str):
    with transaction.atomic():
        document = (
            DocumentExtraction.objects.select_for_update()
            .select_related(
                "run_source_item",
                "input_asset__generic_extraction_attempt__input_asset",
                "input_asset__producing_generic_attempt__input_asset",
                "input_asset__parent_asset",
            )
            .get(pk=document_id)
        )
        if _is_audit_only_document(document):
            return {
                "documentExtractionId": str(document.id),
                "state": "legacy_duplicate_audit_only",
            }
        if document.state in {
            ExtractionState.SUCCEEDED,
            ExtractionState.LOW_CONFIDENCE,
        }:
            return {"documentExtractionId": str(document.id), "state": document.state}
        document.state = ExtractionState.FAILED
        document.document_complete = False
        document.error_code = error_code[:120]
        document.finished_at = timezone.now()
        document.save(
            update_fields=(
                "state",
                "document_complete",
                "error_code",
                "finished_at",
                "updated_at",
            )
        )
        _enqueue_finalize(
            str(document.run_source_item.run_id),
            f"document-terminal:{document.id}",
        )
        return {"documentExtractionId": str(document.id), "state": document.state}


@shared_task
def finalize_generic_extraction_failure(attempt_id: str, error_code: str):
    with transaction.atomic():
        attempt = (
            GenericExtractionAttempt.objects.select_for_update()
            .select_related("run_source_item", "input_asset__parent_asset")
            .get(pk=attempt_id)
        )
        if _is_quarantined_generic_attempt(attempt):
            return {
                "genericExtractionAttemptId": str(attempt.id),
                "state": "legacy_duplicate_audit_only",
            }
        if attempt.state in {
            ExtractionState.SUCCEEDED,
            ExtractionState.LOW_CONFIDENCE,
        }:
            return {"genericExtractionAttemptId": str(attempt.id), "state": attempt.state}
        attempt.state = ExtractionState.FAILED
        attempt.error_code = error_code[:120]
        attempt.finished_at = timezone.now()
        attempt.save(
            update_fields=("state", "error_code", "finished_at", "updated_at")
        )
        _enqueue_finalize(
            str(attempt.run_source_item.run_id),
            f"generic-terminal:{attempt.id}",
        )
        return {"genericExtractionAttemptId": str(attempt.id), "state": attempt.state}


@shared_task
def process_document_extraction(document_id: str):
    try:
        document = _run_document_extraction(document_id)
        return {"documentExtractionId": str(document.id), "state": document.state}
    except ExtractorError as exc:
        with transaction.atomic():
            _queue_document_retry(document_id, exc)
        if exc.retryable:
            raise
        raise PermanentEventError(exc.code, exc.detail_redacted) from exc
    except Exception as exc:
        with transaction.atomic():
            _queue_document_retry(document_id, exc)
        raise


@shared_task
def process_paddleocr_document(document_id: str):
    """Dedicated queue entry point; page routing still comes from the parent aggregate."""
    return process_document_extraction.run(document_id)


@shared_task
def process_generic_extraction(attempt_id: str):
    try:
        attempt = _run_generic_extraction(attempt_id)
        return {"genericExtractionAttemptId": str(attempt.id), "state": attempt.state}
    except ExtractorError as exc:
        with transaction.atomic():
            _queue_generic_retry(attempt_id, exc)
        if exc.retryable:
            raise
        raise PermanentEventError(exc.code, exc.detail_redacted) from exc
    except Exception as exc:
        with transaction.atomic():
            _queue_generic_retry(attempt_id, exc)
        raise


@shared_task
def finalize_run_evidence(run_id: str):
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        if run.state in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.STOPPED,
        }:
            return {"runId": str(run.id), "state": run.state}
        if run.stop_requested_at:
            step, _ = RunStep.objects.select_for_update().get_or_create(
                run=run,
                name="extract",
                attempt_no=1,
            )
            return _stop_run_evidence_locked(run, step)
        if run.state != RunState.EXTRACTING:
            return {"runId": str(run.id), "state": run.state}
        step = (
            RunStep.objects.select_for_update()
            .filter(run=run, name="extract", attempt_no=1)
            .first()
        )
        if step is None or step.fanout_completed_at is None:
            return {
                "runId": str(run.id),
                "state": run.state,
                "pending": True,
                "fanoutComplete": False,
            }
        pending_documents = DocumentExtraction.objects.filter(
            run_source_item__run=run,
            input_fingerprint__isnull=False,
            state__in=(ExtractionState.QUEUED, ExtractionState.RUNNING),
        ).exclude(
            input_asset__derivation_type=EvidenceDerivationType.RAW,
            input_asset__raw_input_fingerprint__isnull=True,
        ).exclude(
            input_asset__parent_asset__derivation_type=(
                EvidenceDerivationType.RAW
            ),
            input_asset__parent_asset__raw_input_fingerprint__isnull=True,
        ).exclude(
            input_asset__generic_extraction_attempt__input_asset__derivation_type=(
                EvidenceDerivationType.RAW
            ),
            input_asset__generic_extraction_attempt__input_asset__raw_input_fingerprint__isnull=(
                True
            ),
        ).exclude(
            input_asset__producing_generic_attempt__input_asset__derivation_type=(
                EvidenceDerivationType.RAW
            ),
            input_asset__producing_generic_attempt__input_asset__raw_input_fingerprint__isnull=(
                True
            ),
        ).exists()
        pending_generic = GenericExtractionAttempt.objects.filter(
            run_source_item__run=run,
            state__in=(ExtractionState.QUEUED, ExtractionState.RUNNING),
        ).exclude(
            input_asset__derivation_type=EvidenceDerivationType.RAW,
            input_asset__raw_input_fingerprint__isnull=True,
        ).exclude(
            input_asset__parent_asset__derivation_type=(
                EvidenceDerivationType.RAW
            ),
            input_asset__parent_asset__raw_input_fingerprint__isnull=True,
        ).exists()
        if pending_documents or pending_generic:
            return {"runId": str(run.id), "state": run.state, "pending": True}
        evidence_count = (
            EvidenceAsset.objects.filter(origin_run_source_item__run=run)
            .exclude(
                derivation_type=EvidenceDerivationType.RAW,
                raw_input_fingerprint__isnull=True,
            )
            .exclude(
                generic_extraction_attempt__input_asset__derivation_type=(
                    EvidenceDerivationType.RAW
                ),
                generic_extraction_attempt__input_asset__raw_input_fingerprint__isnull=(
                    True
                ),
            )
            .exclude(
                parent_asset__derivation_type=EvidenceDerivationType.RAW,
                parent_asset__raw_input_fingerprint__isnull=True,
            )
            .exclude(
                producing_generic_attempt__input_asset__derivation_type=(
                    EvidenceDerivationType.RAW
                ),
                producing_generic_attempt__input_asset__raw_input_fingerprint__isnull=(
                    True
                ),
            )
            .exclude(
                document_extraction__input_asset__derivation_type=(
                    EvidenceDerivationType.RAW
                ),
                document_extraction__input_asset__raw_input_fingerprint__isnull=(
                    True
                ),
            )
            .exclude(
                document_extraction__input_asset__generic_extraction_attempt__input_asset__derivation_type=(
                    EvidenceDerivationType.RAW
                ),
                document_extraction__input_asset__generic_extraction_attempt__input_asset__raw_input_fingerprint__isnull=(
                    True
                ),
            )
            .exclude(
                document_extraction__input_asset__parent_asset__derivation_type=(
                    EvidenceDerivationType.RAW
                ),
                document_extraction__input_asset__parent_asset__raw_input_fingerprint__isnull=(
                    True
                ),
            )
            .exclude(
                document_extraction__input_asset__producing_generic_attempt__input_asset__derivation_type=(
                    EvidenceDerivationType.RAW
                ),
                document_extraction__input_asset__producing_generic_attempt__input_asset__raw_input_fingerprint__isnull=(
                    True
                ),
            )
            .filter(
                Q(document_extraction__isnull=True)
                | Q(
                    document_extraction__input_fingerprint__isnull=False
                )
            )
            .count()
        )
        failed_documents = DocumentExtraction.objects.filter(
            run_source_item__run=run,
            input_fingerprint__isnull=False,
            state=ExtractionState.FAILED,
        ).exclude(
            input_asset__derivation_type=EvidenceDerivationType.RAW,
            input_asset__raw_input_fingerprint__isnull=True,
        ).exclude(
            input_asset__parent_asset__derivation_type=(
                EvidenceDerivationType.RAW
            ),
            input_asset__parent_asset__raw_input_fingerprint__isnull=True,
        ).exclude(
            input_asset__generic_extraction_attempt__input_asset__derivation_type=(
                EvidenceDerivationType.RAW
            ),
            input_asset__generic_extraction_attempt__input_asset__raw_input_fingerprint__isnull=(
                True
            ),
        ).exclude(
            input_asset__producing_generic_attempt__input_asset__derivation_type=(
                EvidenceDerivationType.RAW
            ),
            input_asset__producing_generic_attempt__input_asset__raw_input_fingerprint__isnull=(
                True
            ),
        )
        missing_selected_evidence = failed_documents.filter(
            error_code="selected_run_evidence_missing",
        ).exists()
        document_failures = failed_documents.count()
        generic_failures = GenericExtractionAttempt.objects.filter(
            run_source_item__run=run, state=ExtractionState.FAILED
        ).exclude(
            input_asset__derivation_type=EvidenceDerivationType.RAW,
            input_asset__raw_input_fingerprint__isnull=True,
        ).exclude(
            input_asset__parent_asset__derivation_type=(
                EvidenceDerivationType.RAW
            ),
            input_asset__parent_asset__raw_input_fingerprint__isnull=True,
        ).count()
        failure_count = document_failures + generic_failures
        if missing_selected_evidence:
            now = timezone.now()
            error_code = "selected_run_evidence_missing"
            step.output_count = evidence_count
            step.error_code = error_code
            step.error_detail_redacted = (
                "Selected successful extraction run has no evidence assets"
            )
            step.state = "failed"
            _project_domain_step_terminal(
                step,
                run,
                finished_at=now,
                final_state=step.state,
                affected_count=max(failure_count, 1),
                error_code=error_code,
                recovery_state=RecoveryState.MANUAL_REQUIRED,
            )
            step.save(
                update_fields=(
                    "correlation_id",
                    "worker_task_id",
                    "output_count",
                    "error_code",
                    "error_detail_redacted",
                    "state",
                    "finished_at",
                    "duration_ms",
                    "retry_count",
                    "retry_at",
                    "terminal_impact",
                    "recovery_state",
                )
            )
            run.state = RunState.FAILED
            run.error_summary = {
                "stage": "extract",
                "code": error_code,
            }
            run.completed_at = now
            run.counters = {
                **run.counters,
                "evidence": evidence_count,
                "extractionFailures": failure_count,
            }
            project_run_terminal_observation(
                run,
                finished_at=now,
                stage="extract",
                final_state=run.state,
                affected_count=max(failure_count, 1),
                error_code=error_code,
                recovery_state=RecoveryState.MANUAL_REQUIRED,
            )
            run.save(
                update_fields=(
                    "state",
                    "error_summary",
                    "completed_at",
                    "counters",
                    "duration_ms",
                    "terminal_impact",
                    "recovery_state",
                    "next_recovery_at",
                )
            )
            return {
                "runId": str(run.id),
                "state": run.state,
                "evidence": evidence_count,
                "failures": failure_count,
                "code": error_code,
            }
        step.output_count = evidence_count
        step.error_code = "partial_extraction_failure" if failure_count else None
        step.error_detail_redacted = (
            f"failed document/generic attempts: {failure_count}" if failure_count else None
        )
        step.state = "failed" if evidence_count == 0 else "succeeded"
        finished_at = timezone.now()
        _project_domain_step_terminal(
            step,
            run,
            finished_at=finished_at,
            final_state=step.state,
            affected_count=(
                max(failure_count, step.input_count)
                if step.state == "failed"
                else failure_count
            ),
            error_code=step.error_code,
            recovery_state=(
                RecoveryState.MANUAL_REQUIRED
                if step.state == "failed"
                else RecoveryState.NOT_REQUIRED
            ),
        )
        step.save(
            update_fields=(
                "correlation_id",
                "worker_task_id",
                "output_count",
                "error_code",
                "error_detail_redacted",
                "state",
                "finished_at",
                "duration_ms",
                "retry_count",
                "retry_at",
                "terminal_impact",
                "recovery_state",
            )
        )
        run.state = RunState.VALIDATING
        run.recovery_state = RecoveryState.IN_PROGRESS
        run.next_recovery_at = None
        run.counters = {
            **run.counters,
            "evidence": evidence_count,
            "extractionFailures": failure_count,
        }
        run.save(
            update_fields=(
                "state",
                "counters",
                "recovery_state",
                "next_recovery_at",
            )
        )
        enqueue_event(
            event_type="run.draft_requested",
            aggregate_type="collection_run",
            aggregate_id=run.id,
            job_id=run.id,
            dedupe_key=f"run.draft_requested:{run.id}",
            payload={"run_id": str(run.id)},
        )
        return {
            "runId": str(run.id), "state": run.state,
            "evidence": evidence_count, "failures": failure_count,
        }


@shared_task(name="apps.evidence.tasks.finalize_run_evidence_fanout_failure")
def finalize_run_evidence_fanout_failure(run_id: str, error_code: str):
    now = timezone.now()
    redacted_code = str(error_code)[:100]
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().filter(pk=run_id).first()
        if run is None:
            return {"runId": run_id, "state": "missing"}
        step, _ = RunStep.objects.select_for_update().get_or_create(
            run=run,
            name="extract",
            attempt_no=1,
        )
        stopped = run.stop_requested_at is not None
        if stopped:
            return _stop_run_evidence_locked(run, step)
        if run.state != RunState.EXTRACTING:
            return {"runId": str(run.id), "state": run.state}
        step.state = "stopped" if stopped else "failed"
        step.error_code = redacted_code
        step.error_detail_redacted = "evidence fan-out delivery exhausted"
        _project_domain_step_terminal(
            step,
            run,
            finished_at=now,
            final_state=step.state,
            affected_count=step.input_count,
            error_code=step.error_code,
            recovery_state=(
                RecoveryState.STOPPED
                if stopped
                else RecoveryState.MANUAL_REQUIRED
            ),
        )
        step.save(
            update_fields=(
                "correlation_id",
                "worker_task_id",
                "state",
                "error_code",
                "error_detail_redacted",
                "finished_at",
                "duration_ms",
                "retry_count",
                "retry_at",
                "terminal_impact",
                "recovery_state",
            )
        )
        if stopped:
            run.state = RunState.STOPPED
            run.error_summary = None
        else:
            run.state = RunState.FAILED
            run.error_summary = {
                "stage": "extract",
                "code": redacted_code,
            }
        project_run_terminal_observation(
            run,
            finished_at=now,
            stage="extract",
            final_state=run.state,
            affected_count=step.input_count,
            error_code=redacted_code,
            recovery_state=(
                RecoveryState.STOPPED
                if stopped
                else RecoveryState.MANUAL_REQUIRED
            ),
        )
        run.save(
            update_fields=(
                "state",
                "error_summary",
                "completed_at",
                "duration_ms",
                "terminal_impact",
                "recovery_state",
                "next_recovery_at",
            )
        )
        return {
            "runId": str(run.id),
            "state": run.state,
            "code": redacted_code,
        }


def _stop_run_evidence_locked(
    run: CollectionRun,
    step: RunStep,
    *,
    output_count: int | None = None,
) -> dict[str, str]:
    now = timezone.now()
    step.state = "stopped"
    if output_count is not None:
        step.output_count = output_count
    step.error_code = "stop_requested"
    step.error_detail_redacted = "evidence fan-out stopped by request"
    _project_domain_step_terminal(
        step,
        run,
        finished_at=now,
        final_state=step.state,
        affected_count=max(
            step.input_count - step.output_count,
            0,
        ),
        error_code=step.error_code,
        recovery_state=RecoveryState.STOPPED,
    )
    step.save(
        update_fields=(
            "correlation_id",
            "worker_task_id",
            "state",
            "output_count",
            "error_code",
            "error_detail_redacted",
            "finished_at",
            "duration_ms",
            "retry_count",
            "retry_at",
            "terminal_impact",
            "recovery_state",
        )
    )
    run.state = RunState.STOPPED
    run.error_summary = None
    project_run_terminal_observation(
        run,
        finished_at=now,
        stage="extract",
        final_state=run.state,
        affected_count=max(
            step.input_count - step.output_count,
            0,
        ),
        error_code="stop_requested",
        recovery_state=RecoveryState.STOPPED,
    )
    run.save(
        update_fields=(
            "state",
            "error_summary",
            "completed_at",
            "duration_ms",
            "terminal_impact",
            "recovery_state",
            "next_recovery_at",
        )
    )
    return {"runId": str(run.id), "state": run.state}


def _finalize_run_evidence_wake_failure_locked(
    run_id: str,
    error_code: str,
) -> dict[str, str]:
    run = (
        CollectionRun.objects.select_for_update()
        .filter(pk=run_id)
        .first()
    )
    if run is None:
        return {"runId": run_id, "state": "missing"}
    if run.state in {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.STOPPED,
    }:
        return {"runId": str(run.id), "state": run.state}

    extracting = run.state == RunState.EXTRACTING
    step = None
    if run.state == RunState.STOPPING:
        steps = list(
            RunStep.objects.select_for_update()
            .filter(run=run)
            .order_by("attempt_no", "id")
        )
        if any(
            candidate.name not in {"collect", "extract"}
            for candidate in steps
        ):
            return {"runId": str(run.id), "state": run.state}
        if any(
            candidate.name == "collect"
            and candidate.state == "running"
            for candidate in steps
        ):
            return {"runId": str(run.id), "state": run.state}
        extract_step = next(
            (
                candidate
                for candidate in steps
                if candidate.name == "extract"
                and candidate.attempt_no == 1
            ),
            None,
        )
        if extract_step is not None and extract_step.state not in {
            "queued",
            "running",
        }:
            return {"runId": str(run.id), "state": run.state}
        step = extract_step
        collect_succeeded = any(
            candidate.name == "collect"
            and candidate.attempt_no == 1
            and candidate.state == "succeeded"
            for candidate in steps
        )
        if step is None and not collect_succeeded:
            return {"runId": str(run.id), "state": run.state}
        if step is None:
            step = RunStep.objects.create(
                run=run,
                name="extract",
                attempt_no=1,
            )
        stopping = True
    elif extracting:
        step, _ = RunStep.objects.select_for_update().get_or_create(
            run=run,
            name="extract",
            attempt_no=1,
        )
        stopping = run.stop_requested_at is not None
    else:
        return {"runId": str(run.id), "state": run.state}

    if stopping:
        return _stop_run_evidence_locked(run, step)

    now = timezone.now()
    redacted_code = str(error_code)[:100]
    step.state = "failed"
    step.error_code = redacted_code
    step.error_detail_redacted = (
        "evidence finalizer delivery exhausted before completion"
    )
    _project_domain_step_terminal(
        step,
        run,
        finished_at=now,
        final_state=step.state,
        affected_count=step.input_count,
        error_code=step.error_code,
        recovery_state=RecoveryState.MANUAL_REQUIRED,
    )
    step.save(
        update_fields=(
            "correlation_id",
            "worker_task_id",
            "state",
            "error_code",
            "error_detail_redacted",
            "finished_at",
            "duration_ms",
            "retry_count",
            "retry_at",
            "terminal_impact",
            "recovery_state",
        )
    )
    run.state = RunState.FAILED
    run.error_summary = {
        "stage": "extract",
        "code": redacted_code,
    }
    project_run_terminal_observation(
        run,
        finished_at=now,
        stage="extract",
        final_state=run.state,
        affected_count=step.input_count,
        error_code=redacted_code,
        recovery_state=RecoveryState.MANUAL_REQUIRED,
    )
    run.save(
        update_fields=(
            "state",
            "error_summary",
            "completed_at",
            "duration_ms",
            "terminal_impact",
            "recovery_state",
            "next_recovery_at",
        )
    )
    return {
        "runId": str(run.id),
        "state": run.state,
        "code": redacted_code,
    }


@shared_task(
    name="apps.evidence.tasks.finalize_run_evidence_wake_failure"
)
def finalize_run_evidence_wake_failure(
    run_id: str,
    error_code: str,
):
    with transaction.atomic():
        return _finalize_run_evidence_wake_failure_locked(
            run_id,
            error_code,
        )


@shared_task(
    name="apps.evidence.tasks.finalize_document_ready_wake_failure"
)
def finalize_document_ready_wake_failure(
    document_id: str,
    error_code: str,
):
    with transaction.atomic():
        document = (
            DocumentExtraction.objects.select_for_update()
            .select_related(
                "run_source_item",
                "input_asset__generic_extraction_attempt__input_asset__parent_asset",
                "input_asset__producing_generic_attempt__input_asset__parent_asset",
                "input_asset__parent_asset",
            )
            .filter(pk=document_id)
            .first()
        )
        if document is None:
            return {
                "documentExtractionId": document_id,
                "state": "missing",
            }
        if _is_audit_only_document(document):
            return {
                "documentExtractionId": str(document.id),
                "state": "legacy_duplicate_audit_only",
            }
        return _finalize_run_evidence_wake_failure_locked(
            str(document.run_source_item.run_id),
            error_code,
        )


@shared_task(
    name="apps.evidence.tasks.finalize_other_ready_wake_failure"
)
def finalize_other_ready_wake_failure(
    attempt_id: str,
    error_code: str,
):
    with transaction.atomic():
        attempt = (
            GenericExtractionAttempt.objects.select_for_update()
            .select_related(
                "run_source_item",
                "input_asset__parent_asset",
            )
            .filter(pk=attempt_id)
            .first()
        )
        if attempt is None:
            return {
                "genericExtractionAttemptId": attempt_id,
                "state": "missing",
            }
        if _is_quarantined_generic_attempt(attempt):
            return {
                "genericExtractionAttemptId": str(attempt.id),
                "state": "legacy_duplicate_audit_only",
            }
        return _finalize_run_evidence_wake_failure_locked(
            str(attempt.run_source_item.run_id),
            error_code,
        )


@shared_task(name="apps.evidence.tasks.process_run_evidence")
def process_run_evidence(run_id: str):
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        step, _ = RunStep.objects.select_for_update().get_or_create(
            run=run,
            name="extract",
            attempt_no=1,
        )
        if run.stop_requested_at:
            return _stop_run_evidence_locked(run, step, output_count=0)
        if run.state != RunState.EXTRACTING:
            return {"runId": str(run.id), "state": run.state}
        if step.fanout_completed_at is not None:
            return {
                "runId": str(run.id),
                "state": run.state,
                "fanoutComplete": True,
            }
        step.state = "running"
        started_at = timezone.now()
        _begin_domain_step_observation(
            step,
            run,
            started_at=started_at,
        )
        step.input_count = run.run_source_items.count()
        step.save(
            update_fields=(
                "correlation_id",
                "worker_task_id",
                "state",
                "started_at",
                "finished_at",
                "input_count",
                "duration_ms",
                "retry_count",
                "retry_at",
                "terminal_impact",
                "recovery_state",
            )
        )

    output_count = 0
    failures: list[dict[str, str]] = []
    items = run.run_source_items.select_related(
        "source_item", "source_snapshot__source"
    ).order_by("id")
    for run_source_item in items:
        if CollectionRun.objects.filter(
            pk=run_id,
            stop_requested_at__isnull=False,
        ).exists():
            break
        try:
            _create_raw_evidence(run_source_item)
            output_count += 1
            for attachment in run_source_item.source_item.attachments or []:
                try:
                    _process_attachment(run_source_item, attachment)
                    output_count += 1
                except (ExtractorError, httpx.HTTPError, OSError) as exc:
                    failures.append({
                        "runSourceItemId": str(run_source_item.id),
                        "attachment": str(attachment.get("title", "attachment"))[:120],
                        "code": getattr(exc, "code", exc.__class__.__name__),
                    })
        except (DatabaseError, SoftTimeLimitExceeded):
            raise
        except (ExtractorError, httpx.HTTPError, OSError) as exc:
            failures.append({
                "runSourceItemId": str(run_source_item.id),
                "attachment": "source-record",
                "code": getattr(exc, "code", exc.__class__.__name__),
            })
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        step = RunStep.objects.select_for_update().get(
            run=run,
            name="extract",
            attempt_no=1,
        )
        if run.stop_requested_at:
            return _stop_run_evidence_locked(
                run,
                step,
                output_count=output_count,
            )
        if run.state != RunState.EXTRACTING:
            return {"runId": str(run.id), "state": run.state}
        if step.fanout_completed_at is not None:
            return {
                "runId": str(run.id),
                "state": run.state,
                "fanoutComplete": True,
            }
        step.output_count = output_count
        step.error_code = "partial_extraction_failure" if failures else None
        step.error_detail_redacted = (
            json.dumps(failures[:20], ensure_ascii=False)[:500]
            if failures
            else None
        )
        step.fanout_completed_at = timezone.now()
        step.save(
            update_fields=(
                "output_count",
                "error_code",
                "error_detail_redacted",
                "fanout_completed_at",
            )
        )
        run.counters = {
            **run.counters,
            "evidence": output_count,
            "extractionFailures": len(failures),
        }
        run.save(update_fields=("counters",))
        _enqueue_finalize(str(run.id), "fanout-complete")
        return {
            "runId": str(run.id),
            "state": run.state,
            "fanoutComplete": True,
            "outputCount": output_count,
            "failureCount": len(failures),
        }
