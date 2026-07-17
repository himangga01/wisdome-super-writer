from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx
from celery import shared_task
from django.db import transaction
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
from adapters.storage import S3ObjectStorage
from adapters.storage.s3 import content_addressed_key
from apps.collection.models import CollectionRun, RunState, RunStep
from wisdome_writer.infrastructure.outbox import enqueue_event

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
    existing = EvidenceAsset.objects.filter(
        source_item=item,
        origin_run_source_item=run_source_item,
        derivation_type=EvidenceDerivationType.RAW,
        kind=EvidenceKind.TEXT,
        evidence_content_hash=content_hash,
    ).first()
    if existing:
        return existing
    rights = _rights(run_source_item)
    evidence = EvidenceAsset.objects.create(
        source_item=item,
        origin_run_source_item=run_source_item,
        derivation_type=EvidenceDerivationType.RAW,
        kind=EvidenceKind.TEXT,
        locator_type=LocatorType.STRUCTURED_PATH,
        locator={"locator_type": "structured_path", "path_type": "record_key", "path": "body_text"},
        extracted_text=item.body_text,
        structured_data=item.metadata,
        extraction_method="source_record",
        extractor_version="v1",
        evidence_content_hash=content_hash,
        review_subject_hash="0" * 64,
        review_state=ReviewState.PASSED,
        **rights,
    )
    evidence.review_subject_hash = calculate_review_subject_hash(evidence)
    evidence.publishable = calculate_publishable(evidence)
    evidence.full_clean()
    evidence.save(update_fields=("review_subject_hash", "publishable", "updated_at"))
    return evidence


def _download_attachment(run_source_item, attachment: Mapping[str, Any]) -> tuple[bytes, str, str]:
    url = str(attachment.get("url", ""))
    parsed = urlparse(url)
    source = run_source_item.source_snapshot.source
    allowed_hosts = {urlparse(source.base_url).hostname}
    allowed_hosts.update(run_source_item.source_snapshot.config.get("allowedAttachmentHosts", []))
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ExtractorError("attachment_url_not_allowed", "Attachment URL is outside the approved source hosts")
    with httpx.Client(
        timeout=httpx.Timeout(60, connect=10),
        follow_redirects=False,
        headers={"User-Agent": "WisdomeSuperWriter/0.1 (+admin-managed research bot)"},
    ) as client:
        response = client.get(url)
        response.raise_for_status()
    if len(response.content) > MAX_ATTACHMENT_BYTES:
        raise ExtractorError("attachment_limit_exceeded", "Attachment exceeds the configured byte limit")
    mime_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip()
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
    evidence = EvidenceAsset.objects.filter(
        source_item=run_source_item.source_item,
        origin_run_source_item=run_source_item,
        derivation_type=EvidenceDerivationType.RAW,
        checksum=checksum,
    ).first()
    if evidence:
        return evidence, info
    evidence = EvidenceAsset.objects.create(
        source_item=run_source_item.source_item,
        origin_run_source_item=run_source_item,
        derivation_type=EvidenceDerivationType.RAW,
        kind=EvidenceKind.ATTACHMENT,
        locator_type=LocatorType.STRUCTURED_PATH,
        locator={"locator_type": "structured_path", "path_type": "record_key", "path": "attachments"},
        object_key=info.key,
        object_version=info.version_id or info.etag or checksum,
        mime_type=info.content_type,
        byte_size=info.size,
        checksum=checksum,
        structured_data={"title": attachment.get("title"), "source_url": attachment.get("url")},
        evidence_content_hash=content_hash,
        review_subject_hash="0" * 64,
        review_state=ReviewState.PASSED,
        **rights,
    )
    evidence.review_subject_hash = calculate_review_subject_hash(evidence)
    evidence.publishable = calculate_publishable(evidence)
    evidence.full_clean()
    evidence.save(update_fields=("review_subject_hash", "publishable", "updated_at"))
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
        "run_source_item__run", "run_source_item__source_snapshot__source", "source_item", "input_asset"
    ).get(pk=document_id)
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
            run.state = ExtractionState.LOW_CONFIDENCE if reasons else ExtractionState.SUCCEEDED
            run.processed_page_indices = list(output.processed_page_indices)
            run.result_object_key = result.key
            run.result_checksum = result.checksum_sha256
            run.runtime_version = output.runtime_version
            run.package_version = output.package_version
            run.pipeline_name = output.pipeline_name
            run.device_type = output.device_type
            run.low_confidence_reasons = reasons or None
            run.low_confidence_reasons_hash = canonical_hash(reasons) if reasons else None
            if reasons:
                reason_info = _store_result("reasons", run.id, {"reasons": reasons})
                run.low_confidence_reasons_object_key = reason_info.key
                run.low_confidence_reasons_object_version = reason_info.version_id or reason_info.etag
            run.duration_ms = int((timezone.now() - started).total_seconds() * 1000)
            run.finished_at = timezone.now()
            run.full_clean()
            run.save()
            _make_document_evidence(document, run, output, document.run_source_item)
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
    document = aggregate_document_extraction(document.id)
    if document.document_complete:
        enqueue_event(
            topic="evidence.document_ready",
            aggregate_type="DocumentExtraction",
            aggregate_id=document.id,
            message_key=f"evidence.document_ready:{document.id}:{document.selected_evidence_manifest_hash}",
            correlation_id=document.run_source_item.run_id,
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


def _run_generic_extraction(attempt_id: Any) -> GenericExtractionAttempt:
    attempt = GenericExtractionAttempt.objects.select_related(
        "run_source_item__run", "run_source_item__source_snapshot__source", "source_item",
        "input_asset", "extraction_profile_snapshot",
    ).get(pk=attempt_id)
    if attempt.state in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE):
        return attempt
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
        attempt.state = ExtractionState.RUNNING
        attempt.started_at = timezone.now()
        attempt.save(update_fields=("state", "started_at", "updated_at"))
        output: GenericExtractionOutput = _generic_extractor(profile).extract(path)
        result = _store_result("generic", attempt.id, output.as_dict())
        reasons = normalize_low_confidence_reasons(output.low_confidence_reasons)
        attempt.state = ExtractionState.LOW_CONFIDENCE if reasons else ExtractionState.SUCCEEDED
        attempt.result_checksum = result.checksum_sha256
        attempt.low_confidence_reasons_hash = canonical_hash(reasons) if reasons else None
        if reasons:
            reason_info = _store_result("reasons", attempt.id, {"reasons": reasons})
            attempt.low_confidence_reasons_object_key = reason_info.key
            attempt.low_confidence_reasons_object_version = reason_info.version_id or reason_info.etag
        attempt.finished_at = timezone.now()
        attempt.full_clean()
        attempt.save()

        records = [record.as_dict() for record in output.records]
        first = output.records[0] if output.records else None
        if first is None:
            raise ExtractorError("generic_result_empty", "Generic extractor returned no evidence records")
        text = "\n\n".join(record.text for record in output.records if record.text)
        structured = {"records": records, "metadata": dict(output.metadata)}
        content_hash = evidence_content_hash(text=text or None, structured_data=structured, checksum=None)
        kind = first.kind if first.kind in EvidenceKind.values else EvidenceKind.TEXT
        manual = attempt.state == ExtractionState.LOW_CONFIDENCE
        rights = _rights(attempt.run_source_item)
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
            confidence=first.confidence if profile.validation_mode == GenericValidationMode.CALIBRATED else None,
            low_confidence_reasons=reasons,
            evidence_content_hash=content_hash,
            review_subject_hash="0" * 64,
            review_state=ReviewState.MANUAL_REQUIRED if manual else ReviewState.PASSED,
            manual_review_required=manual,
            alt_text=first.alt_text,
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
            correlation_id=attempt.run_source_item.run_id,
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
            evidence.object_key = info.key
            evidence.object_version = info.version_id or info.etag or converted_checksum
            evidence.mime_type = "application/pdf"
            evidence.byte_size = len(converted_data)
            evidence.checksum = converted_checksum
            evidence.save(update_fields=(
                "object_key", "object_version", "mime_type", "byte_size", "checksum", "updated_at"
            ))
            document = DocumentExtraction.objects.create(
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
            transaction.on_commit(lambda: process_paddleocr_document.delay(str(document.id)))
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
        document = DocumentExtraction.objects.create(
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
        transaction.on_commit(lambda: process_paddleocr_document.delay(str(document.id)))
        return
    if mime_type in {"image/png", "image/jpeg", "image/tiff"}:
        document = DocumentExtraction.objects.create(
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
        transaction.on_commit(lambda: process_paddleocr_document.delay(str(document.id)))
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
            "validation_mode": profile.validation_mode or GenericValidationMode.DETERMINISTIC,
            "calibration_profile_key": profile.calibration_profile_key,
            "calibration_profile_version": profile.calibration_profile_version,
            "calibration_profile_hash": profile.calibration_profile_hash,
        },
    )
    _run_generic_extraction(attempt.id)


@shared_task(bind=True, autoretry_for=(TimeoutError,), retry_backoff=True, max_retries=2)
def process_document_extraction(self, document_id: str):
    run_id = None
    try:
        run_id = str(
            DocumentExtraction.objects.values_list("run_source_item__run_id", flat=True).get(pk=document_id)
        )
        document = _run_document_extraction(document_id)
        return {"documentExtractionId": str(document.id), "state": document.state}
    except ExtractorError as exc:
        DocumentExtraction.objects.filter(pk=document_id).update(
            state=ExtractionState.FAILED,
            document_complete=False,
            error_code=exc.code,
            error_detail_redacted=exc.detail_redacted,
            finished_at=timezone.now(),
        )
        if exc.retryable:
            raise TimeoutError(exc.detail_redacted) from exc
        return {"documentExtractionId": str(document_id), "state": "failed", "errorCode": exc.code}
    finally:
        if run_id:
            finalize_run_evidence.delay(run_id)


@shared_task(bind=True, autoretry_for=(TimeoutError,), retry_backoff=True, max_retries=2)
def process_paddleocr_document(self, document_id: str):
    """Dedicated queue entry point; page routing still comes from the parent aggregate."""
    return process_document_extraction.run(document_id)


@shared_task(bind=True, autoretry_for=(TimeoutError,), retry_backoff=True, max_retries=2)
def process_generic_extraction(self, attempt_id: str):
    run_id = None
    try:
        run_id = str(
            GenericExtractionAttempt.objects.values_list("run_source_item__run_id", flat=True).get(pk=attempt_id)
        )
        attempt = _run_generic_extraction(attempt_id)
        return {"genericExtractionAttemptId": str(attempt.id), "state": attempt.state}
    except ExtractorError as exc:
        GenericExtractionAttempt.objects.filter(pk=attempt_id).update(
            state=ExtractionState.FAILED,
            error_code=exc.code,
            error_detail_redacted=exc.detail_redacted,
            finished_at=timezone.now(),
        )
        if exc.retryable:
            raise TimeoutError(exc.detail_redacted) from exc
        return {"genericExtractionAttemptId": str(attempt_id), "state": "failed", "errorCode": exc.code}
    finally:
        if run_id:
            finalize_run_evidence.delay(run_id)


@shared_task
def finalize_run_evidence(run_id: str):
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        if run.state != RunState.EXTRACTING:
            return {"runId": str(run.id), "state": run.state}
        pending_documents = DocumentExtraction.objects.filter(
            run_source_item__run=run,
            state__in=(ExtractionState.QUEUED, ExtractionState.RUNNING),
        ).exists()
        pending_generic = GenericExtractionAttempt.objects.filter(
            run_source_item__run=run,
            state__in=(ExtractionState.QUEUED, ExtractionState.RUNNING),
        ).exists()
        if pending_documents or pending_generic:
            return {"runId": str(run.id), "state": run.state, "pending": True}
        evidence_count = EvidenceAsset.objects.filter(origin_run_source_item__run=run).count()
        document_failures = DocumentExtraction.objects.filter(
            run_source_item__run=run, state=ExtractionState.FAILED
        ).count()
        generic_failures = GenericExtractionAttempt.objects.filter(
            run_source_item__run=run, state=ExtractionState.FAILED
        ).count()
        failure_count = document_failures + generic_failures
        step, _ = RunStep.objects.select_for_update().get_or_create(run=run, name="extract", attempt_no=1)
        step.output_count = evidence_count
        step.error_code = "partial_extraction_failure" if failure_count else None
        step.error_detail_redacted = (
            f"failed document/generic attempts: {failure_count}" if failure_count else None
        )
        step.state = "failed" if evidence_count == 0 else "succeeded"
        step.finished_at = timezone.now()
        step.save()
        run.state = RunState.VALIDATING
        run.counters = {
            **run.counters,
            "evidence": evidence_count,
            "extractionFailures": failure_count,
        }
        run.save(update_fields=("state", "counters"))
        transaction.on_commit(
            lambda: __import__("apps.editorial.tasks", fromlist=["generate_run_draft"])
            .generate_run_draft.delay(str(run.id))
        )
        return {
            "runId": str(run.id), "state": run.state,
            "evidence": evidence_count, "failures": failure_count,
        }


@shared_task(bind=True, autoretry_for=(TimeoutError,), retry_backoff=True, max_retries=2)
def process_run_evidence(self, run_id: str):
    run = CollectionRun.objects.get(pk=run_id)
    if run.state not in {RunState.EXTRACTING, RunState.VALIDATING}:
        return {"runId": str(run.id), "state": run.state}
    step, _ = RunStep.objects.get_or_create(run=run, name="extract", attempt_no=1)
    step.state = "running"
    step.started_at = step.started_at or timezone.now()
    step.input_count = run.run_source_items.count()
    step.save(update_fields=("state", "started_at", "input_count"))
    output_count = 0
    failures: list[dict[str, str]] = []
    items = run.run_source_items.select_related(
        "source_item", "source_snapshot__source"
    ).order_by("id")
    for run_source_item in items:
        if run.stop_requested_at:
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
        except Exception as exc:
            failures.append({
                "runSourceItemId": str(run_source_item.id),
                "attachment": "source-record",
                "code": getattr(exc, "code", exc.__class__.__name__),
            })
    step.output_count = output_count
    step.error_code = "partial_extraction_failure" if failures else None
    step.error_detail_redacted = json.dumps(failures[:20], ensure_ascii=False)[:500] if failures else None
    step.save(update_fields=("output_count", "error_code", "error_detail_redacted"))
    if run.stop_requested_at:
        run.state = RunState.STOPPED
        run.completed_at = timezone.now()
        run.save(update_fields=("state", "completed_at"))
        return {"runId": str(run.id), "state": run.state}
    run.counters = {
        **run.counters,
        "evidence": output_count,
        "extractionFailures": len(failures),
    }
    run.save(update_fields=("counters",))
    return finalize_run_evidence.run(str(run.id))
