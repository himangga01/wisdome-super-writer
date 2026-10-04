from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone

from adapters.extractors.base import (
    ExtractorError,
    GenericExtractionOutput,
    canonical_bytes,
    sha256_bytes,
    sha256_file,
    sniff_mime,
)
from adapters.extractors.html import HtmlExtractor
from adapters.extractors.hwpx import HwpxExtractor
from adapters.extractors.legacy_hwp import LegacyHwpConverter
from adapters.extractors.media import inspect_static_image
from adapters.extractors.native_pdf import NativePdfExtractor
from adapters.extractors.paddleocr import PaddleOCRExtractor
from adapters.extractors.spreadsheet import SpreadsheetExtractor
from adapters.extractors.structured import StructuredDataExtractor
from adapters.sources import (
    source_attachment_content_types,
)
from adapters.sources.errors import SourceAccessError
from adapters.sources.http import download_source_attachment
from adapters.storage import ObjectInfo, S3ObjectStorage
from adapters.storage.s3 import content_addressed_key
from apps.collection.models import (
    CollectionRun,
    RecoveryState,
    RunState,
    RunStep,
    SourceDiscoveryKind,
)
from apps.collection.services import (
    begin_step_observation,
    project_run_terminal_observation,
    project_step_terminal_observation,
    schedule_queue_one_release,
)
from wisdome_writer.infrastructure.http_safety import redact_url
from wisdome_writer.infrastructure.outbox import (
    CURRENT_EVENT_CONSUMER_LEASE_GENERATION,
    CURRENT_EVENT_CONSUMER_LEASE_TOKEN,
    CURRENT_EVENT_CONSUMER_NAME,
    CURRENT_EVENT_ID,
    PermanentEventError,
    enqueue_event,
)

from .models import (
    DocumentExtraction,
    DocumentInputKind,
    EvidenceAsset,
    EvidenceDerivationType,
    EvidenceKind,
    ExtractionEngine,
    ExtractionObjectWriteReservation,
    ExtractionObjectWriteState,
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
    EvidenceConflict,
    EvidenceExtractionStopped,
    aggregate_document_extraction,
    begin_document_extraction,
    begin_evidence_fanout,
    begin_extraction_run,
    begin_generic_extraction,
    bind_extraction_object_write,
    calculate_publishable,
    calculate_review_subject_hash,
    canonical_hash,
    document_evidence_manifest_entry,
    document_extraction_completion_fence,
    evidence_content_hash,
    evidence_fanout_completion_fence,
    extraction_fingerprint,
    extraction_run_completion_fence,
    generic_evidence_manifest_hash,
    generic_extraction_completion_fence,
    generic_extraction_fingerprint,
    group_routes,
    mark_extraction_object_uploaded,
    normalize_low_confidence_reasons,
    orphan_unbound_extraction_object_writes,
    queue_document_extraction_retry,
    queue_generic_extraction_retry,
    reserve_extraction_object_write,
    route_pdf_pages,
    validate_generic_evidence_set,
)

_GENERIC_BINARY_MIME_TYPES = frozenset(
    {"application/octet-stream", "binary/octet-stream"}
)
_ATTACHMENT_MIME_ALIASES = {
    "application/vnd.hancom.hwpx": "application/hwp+zip",
    "application/x-hwp": "application/haansofthwp",
    "application/vnd.hancom.hwp": "application/haansofthwp",
}
EXTRACTABLE_SOURCE_DISCOVERY_KINDS = (
    SourceDiscoveryKind.NEW_VERSION,
    SourceDiscoveryKind.CORRECTED,
    SourceDiscoveryKind.RESTORED,
)

MAX_EXTERNAL_DOCUMENT_BYTES = 12 * 1024 * 1024
MAX_DERIVED_DOCUMENT_BYTES = 64 * 1024 * 1024

GENERIC_ENGINE_FACTORIES = {
    ExtractionEngine.HTML: HtmlExtractor,
    ExtractionEngine.STRUCTURED: StructuredDataExtractor,
    ExtractionEngine.SPREADSHEET: SpreadsheetExtractor,
    ExtractionEngine.HWPX: HwpxExtractor,
    ExtractionEngine.LEGACY_HWP: LegacyHwpConverter,
}


def _read_frozen_object(
    *,
    storage: S3ObjectStorage,
    key: str,
    version_id: str,
    expected_size: int | None,
    expected_checksum: str,
    hard_max_bytes: int,
) -> bytes:
    if not version_id:
        raise ExtractorError("input_version_missing", "A frozen input object version is required")
    if (
        not expected_checksum
        or len(expected_checksum) != 64
        or any(character not in "0123456789abcdef" for character in expected_checksum)
    ):
        raise ExtractorError("generic_input_missing", "Input checksum provenance is incomplete")
    if expected_size is not None and (type(expected_size) is not int or expected_size < 1):
        raise ExtractorError("generic_input_missing", "Input byte-size provenance is invalid")
    max_bytes = hard_max_bytes if expected_size is None else min(expected_size, hard_max_bytes)
    try:
        data = storage.get_bounded_bytes(
            key=key,
            version_id=version_id,
            max_bytes=max_bytes,
        )
    except (OSError, ValueError) as exc:
        raise ExtractorError(
            "input_identity_mismatch",
            "Frozen object storage input differs from bounded provenance",
        ) from exc
    if (
        (expected_size is not None and len(data) != expected_size)
        or hashlib.sha256(data).hexdigest() != expected_checksum
    ):
        raise ExtractorError(
            "input_checksum_mismatch",
            "Downloaded input differs from frozen provenance",
        )
    return data


def _legacy_hwp_protocol_generation(database_lease_generation: int) -> int:
    """The UDS v1 protocol generation is compatibility material, not a DB lease."""
    if not isinstance(database_lease_generation, int) or database_lease_generation < 1:
        raise ValueError("database lease generation must be positive")
    return 1


def _current_extraction_delivery(aggregate_id: Any) -> dict[str, Any]:
    """Return the monotonic routed receipt lease; direct leaf calls are forbidden."""
    event_id = CURRENT_EVENT_ID.get()
    lease_generation = CURRENT_EVENT_CONSUMER_LEASE_GENERATION.get()
    lease_owner = CURRENT_EVENT_CONSUMER_NAME.get()
    lease_token = CURRENT_EVENT_CONSUMER_LEASE_TOKEN.get()
    if (
        event_id
        and isinstance(lease_generation, int)
        and lease_generation > 0
        and lease_owner
        and lease_token
    ):
        return {
            "source_event_id": event_id,
            "delivery_count": lease_generation,
            "lease_generation": lease_generation,
            "lease_owner": lease_owner,
            "lease_token": lease_token,
        }
    raise PermanentEventError(
        "routed_extraction_context_required",
        "Extraction leaf tasks require an owned outbox consumer lease",
    )


def _is_required_legacy_hwp_attachment(attachment: Mapping[str, Any]) -> bool:
    """Identify only legacy .hwp inputs; other optional attachments keep partial semantics."""
    candidates = (
        attachment.get("filename"),
        attachment.get("url"),
        attachment.get("title"),
    )
    return any(
        Path(urlparse(str(value)).path).suffix.lower() == ".hwp"
        for value in candidates
        if value
    )


def _legacy_hwp_failure_marker(failures: list[Mapping[str, Any]]) -> dict[str, Any]:
    required = [failure for failure in failures if failure.get("requiredLegacyHwp") is True]
    if not required:
        return {}
    codes = sorted({str(failure.get("code", "legacy_hwp_failed"))[:100] for failure in required})
    return {
        "legacyHwpRequiredFailures": len(required),
        "legacyHwpRequiredFailureCodes": codes,
    }


def _quarantine_legacy_hwp_raw_input(evidence: EvidenceAsset | None) -> None:
    if evidence is None or evidence.derivation_type != EvidenceDerivationType.RAW:
        return
    evidence.review_state = ReviewState.MANUAL_REQUIRED
    evidence.manual_review_required = True
    evidence.publishable = False
    evidence.save(
        update_fields=(
            "review_state",
            "manual_review_required",
            "publishable",
            "updated_at",
        )
    )


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
    page_identity_authoritative: bool = False,
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
    if not created:
        _validate_reused_document_identity(
            document,
            run_source_item_id=run_source_item.id,
            source_item_id=source_item.id,
            input_asset_id=input_asset.id,
            input_object_key=input_object_key,
            input_object_version=input_object_version,
            input_kind=input_kind,
            input_mime_type=input_mime_type,
            input_frame_count=input_frame_count,
            input_checksum=input_checksum,
            input_page_count=input_page_count,
            expected_page_indices=expected_page_indices,
            page_identity_authoritative=page_identity_authoritative,
        )
    return document


def _validate_reused_document_identity(
    document: Any,
    *,
    page_identity_authoritative: bool | None = None,
    **expected: Any,
) -> None:
    """Fail closed when a fingerprint collision points at different persisted material."""
    immutable_fields = (
        "run_source_item_id",
        "source_item_id",
        "input_asset_id",
        "input_object_key",
        "input_object_version",
        "input_kind",
        "input_mime_type",
        "input_checksum",
    )
    if page_identity_authoritative is None:
        page_identity_authoritative = (
            expected["input_page_count"] != 1
            or expected["expected_page_indices"] != [0]
        )
    if page_identity_authoritative:
        immutable_fields += (
            "input_frame_count",
            "input_page_count",
            "expected_page_indices",
        )
    for field in immutable_fields:
        value = expected[field]
        if getattr(document, field) != value:
            raise PermanentEventError("document_input_identity_conflict")


_LEGACY_HWP_POLICY = {
    "network_allowed": False,
    "read_only_rootfs": True,
    "read_only_input": True,
    "private_tmpfs": True,
    "non_root": True,
    "resource_limits_enforced": True,
}
_LEGACY_HWP_REPORT_FIELDS = frozenset(
    {
        "schema_version", "protocol_version", "attempt_id", "generation", "nonce",
        "input_checksum_sha256", "input_byte_size", "output_pdf_checksum_sha256",
        "output_pdf_byte_size", "page_count", "pdf_mime", "qpdf_validated",
        "converter_manifest_hash", "sandbox_policy", "warnings", "font_substitutions",
        "fallback_used", "partial_text_used", "stdout_evidence_used",
    }
)


def _sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) != {"0"}
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_uuid(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _verified_legacy_hwp_record_material(
    record: Mapping[str, Any],
    pdf_bytes: bytes | None = None,
) -> int:
    """Bind the exact supervisor report, locator, and optionally the upload bytes."""
    locator = record.get("locator")
    structured = record.get("structured_data")
    if not isinstance(locator, Mapping) or not isinstance(structured, Mapping):
        raise ExtractorError("legacy_hwp_report_invalid", "Converted HWP provenance is missing")
    report = structured.get("conversion_report")
    if not isinstance(report, Mapping):
        raise ExtractorError("legacy_hwp_report_invalid", "Exact conversion report is missing")
    try:
        report_bytes = canonical_bytes(dict(report))
    except (TypeError, ValueError) as exc:
        raise ExtractorError(
            "legacy_hwp_report_invalid", "Conversion report is not canonical"
        ) from exc

    page_count = report.get("page_count")
    input_size = report.get("input_byte_size")
    output_size = report.get("output_pdf_byte_size")
    generation = report.get("generation")
    bindings = (
        (locator.get("attempt_id"), report.get("attempt_id")),
        (locator.get("generation"), generation),
        (locator.get("nonce"), report.get("nonce")),
        (locator.get("input_checksum"), report.get("input_checksum_sha256")),
        (locator.get("input_byte_size"), input_size),
        (locator.get("output_pdf_checksum"), report.get("output_pdf_checksum_sha256")),
        (locator.get("output_pdf_byte_size"), output_size),
        (locator.get("converter_manifest_hash"), report.get("converter_manifest_hash")),
        (locator.get("sandbox_report_hash"), hashlib.sha256(report_bytes).hexdigest()),
        (locator.get("page_count"), page_count),
        (structured.get("converted_page_count"), page_count),
    )
    if (
        set(report) != _LEGACY_HWP_REPORT_FIELDS
        or any(left != right for left, right in bindings)
        or report.get("schema_version") != "v1"
        or report.get("protocol_version") != "wisdome-hwp-uds-v1"
        or not _canonical_uuid(report.get("attempt_id"))
        or type(generation) is not int
        or generation != 1
        or not _sha256_hex(report.get("nonce"))
        or type(input_size) is not int
        or input_size < 1
        or type(output_size) is not int
        or output_size < 1
        or type(page_count) is not int
        or not 1 <= page_count <= 10000
        or not _sha256_hex(report.get("input_checksum_sha256"))
        or not _sha256_hex(report.get("output_pdf_checksum_sha256"))
        or not _sha256_hex(report.get("converter_manifest_hash"))
        or report.get("pdf_mime") != "application/pdf"
        or report.get("qpdf_validated") is not True
        or report.get("sandbox_policy") != _LEGACY_HWP_POLICY
        or report.get("warnings") != []
        or report.get("font_substitutions") != []
        or report.get("fallback_used") is not False
        or report.get("partial_text_used") is not False
        or report.get("stdout_evidence_used") is not False
        or structured.get("sandbox_policy") != _LEGACY_HWP_POLICY
        or structured.get("follow_up_engine_allowlist")
        != ["native_pdf", "paddleocr_ppstructurev3"]
        or structured.get("qpdf_validated") is not True
        or structured.get("warnings") != []
        or structured.get("font_substitutions") != []
        or structured.get("fallback_used") is not False
    ):
        raise ExtractorError("legacy_hwp_report_invalid", "Converted HWP material is not bound")
    if pdf_bytes is not None and (
        len(pdf_bytes) != output_size
        or hashlib.sha256(pdf_bytes).hexdigest() != report.get("output_pdf_checksum_sha256")
    ):
        raise ExtractorError("legacy_hwp_output_invalid", "PDF changed after sandbox verification")
    return page_count


def _has_complete_legacy_hwp_pdf(
    evidence: EvidenceAsset | None,
    *,
    expected_attempt: GenericExtractionAttempt,
) -> bool:
    if evidence is None:
        return False
    checksum = evidence.checksum
    material_complete = (
        bool(evidence.object_key)
        and bool(evidence.object_version)
        and isinstance(checksum, str)
        and len(checksum) == 64
        and all(character in "0123456789abcdef" for character in checksum)
    )
    if not material_complete:
        return False
    try:
        page_count = _verified_legacy_hwp_page_count(evidence.structured_data)
        record = evidence.structured_data["records"][0]
        locator = record["locator"]
        report = record["structured_data"]["conversion_report"]
        input_asset = expected_attempt.input_asset
        profile = expected_attempt.extraction_profile_snapshot
        if (
            evidence.generic_extraction_attempt_id != expected_attempt.id
            or evidence.origin_run_source_item_id
            != expected_attempt.run_source_item_id
            or evidence.source_item_id != expected_attempt.source_item_id
            or evidence.parent_asset_id != expected_attempt.input_asset_id
            or evidence.mime_type != "application/pdf"
            or evidence.locator_type != LocatorType.HWP_CONVERSION
            or evidence.derivation_type != EvidenceDerivationType.OTHER
            or locator.get("attempt_id") != str(expected_attempt.id)
            or report.get("attempt_id") != str(expected_attempt.id)
            or locator.get("input_checksum") != input_asset.checksum
            or report.get("input_checksum_sha256") != input_asset.checksum
            or locator.get("input_byte_size") != input_asset.byte_size
            or report.get("input_byte_size") != input_asset.byte_size
            or locator.get("converter_manifest_hash")
            != profile.config.get("converter_manifest_hash")
            or report.get("converter_manifest_hash")
            != profile.config.get("converter_manifest_hash")
            or locator.get("output_pdf_checksum") != evidence.checksum
            or locator.get("output_pdf_byte_size") != evidence.byte_size
            or locator.get("page_count") != page_count
        ):
            return False
    except (ExtractorError, KeyError, TypeError):
        return False
    return True


def _verified_legacy_hwp_page_count(structured_data: Any) -> int:
    """Return only the page count carried by a fully validated T015 record."""
    if not isinstance(structured_data, Mapping):
        raise ExtractorError("legacy_hwp_report_invalid", "Converted HWP provenance is missing")
    records = structured_data.get("records")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], Mapping):
        raise ExtractorError("legacy_hwp_report_invalid", "Converted HWP record is ambiguous")
    return _verified_legacy_hwp_record_material(records[0])


def _fail_incomplete_legacy_hwp_locked(
    attempt: GenericExtractionAttempt,
    *,
    error_code: str,
) -> GenericExtractionAttempt:
    now = timezone.now()
    run = CollectionRun.objects.get(pk=attempt.run_source_item.run_id)
    step = RunStep.objects.get(
        run=run,
        name="extract",
        attempt_no=1,
    )
    step.state = "failed"
    step.error_code = error_code[:120]
    step.error_detail_redacted = "legacy HWP converted PDF material is incomplete"
    step.finished_at = now
    step.recovery_state = RecoveryState.MANUAL_REQUIRED
    step.lease_owner = ""
    step.lease_token = None
    step.save(
        update_fields=(
            "state",
            "error_code",
            "error_detail_redacted",
            "finished_at",
            "recovery_state",
            "lease_owner",
            "lease_token",
        )
    )
    run.state = RunState.FAILED
    run.error_summary = {"stage": "extract", "code": error_code[:120]}
    run.completed_at = now
    run.recovery_state = RecoveryState.MANUAL_REQUIRED
    run.next_recovery_at = None
    run.save(
        update_fields=(
            "state",
            "error_summary",
            "completed_at",
            "recovery_state",
            "next_recovery_at",
        )
    )
    schedule_queue_one_release(run)
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
    if not _has_complete_legacy_hwp_pdf(
        evidence,
        expected_attempt=attempt,
    ):
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
            recoverable[0],
            expected_attempt=attempt,
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

    page_count = _verified_legacy_hwp_page_count(evidence.structured_data)
    document = _get_or_create_document_extraction(
        run_source_item=attempt.run_source_item,
        source_item=attempt.source_item,
        input_asset=evidence,
        input_object_key=evidence.object_key,
        input_object_version=evidence.object_version,
        input_kind=DocumentInputKind.PDF,
        input_mime_type="application/pdf",
        input_checksum=evidence.checksum,
        input_page_count=page_count,
        expected_page_indices=list(range(page_count)),
        page_identity_authoritative=True,
    )
    _enqueue_document_extraction(document)
    return attempt


def _ensure_legacy_hwp_document(
    attempt_id: Any,
) -> GenericExtractionAttempt:
    run_id = GenericExtractionAttempt.objects.values_list(
        "run_source_item__run_id", flat=True
    ).get(pk=attempt_id)
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        RunStep.objects.select_for_update().get_or_create(
            run=run, name="extract", attempt_no=1
        )
        attempt = (
            GenericExtractionAttempt.objects.select_for_update()
            .get(pk=attempt_id)
        )
        if run.state != RunState.EXTRACTING or run.stop_requested_at is not None:
            return attempt
        list(
            EvidenceAsset.objects.select_for_update()
            .filter(
                Q(pk=attempt.evidence_asset_id)
                | Q(generic_extraction_attempt_id=attempt.id)
                | Q(producing_generic_attempt_id=attempt.id)
            )
            .order_by("id")
        )
        list(
            DocumentExtraction.objects.select_for_update()
            .filter(
                Q(input_asset_id=attempt.evidence_asset_id)
                | Q(run_source_item_id=attempt.run_source_item_id)
            )
            .order_by("id")
        )
        attempt = GenericExtractionAttempt.objects.select_related(
            "run_source_item",
            "source_item",
            "input_asset",
            "evidence_asset",
            "extraction_profile_snapshot",
        ).get(pk=attempt_id)
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


_RIGHTS_RESTRICTIVENESS = {
    RightsStatus.PROHIBITED: 0,
    RightsStatus.UNKNOWN: 1,
    RightsStatus.INTERNAL_ONLY: 2,
    RightsStatus.ATTRIBUTION_REQUIRED: 3,
    RightsStatus.ALLOWED: 4,
}


def _rights(
    run_source_item,
    *,
    scope: str = "record",
    attachment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    frozen = run_source_item.source_snapshot.frozen_config
    if not isinstance(frozen, dict):
        raise PermanentEventError(
            "source_rights_policy_invalid",
            "Frozen source material is not an object.",
        )
    policy = frozen.get("rightsPolicy")
    if (
        not isinstance(policy, dict)
        or policy.get("schemaVersion") != "source-rights-policy-v1"
        or frozen.get("rightsPolicyHash") != canonical_hash(policy)
    ):
        raise PermanentEventError(
            "source_rights_policy_missing",
            "Frozen source rights policy is missing.",
        )
    decision = policy.get(scope)
    if not isinstance(decision, dict):
        raise PermanentEventError(
            "source_rights_scope_missing",
            "Frozen source rights scope is missing.",
        )
    status = decision.get("status")
    if status not in RightsStatus.values:
        raise PermanentEventError(
            "source_rights_status_invalid",
            "Frozen source rights status is invalid.",
        )
    if attachment is not None:
        attachment_status = (
            attachment.get("rights_status")
            or attachment.get("rightsStatus")
        )
        if (
            attachment_status in RightsStatus.values
            and _RIGHTS_RESTRICTIVENESS[attachment_status]
            < _RIGHTS_RESTRICTIVENESS[status]
        ):
            status = attachment_status
    basis_url = decision.get("basisUrl")
    attribution = decision.get("attributionText")
    policy_publishable = bool(decision.get("publishable", False))
    if status in {
        RightsStatus.ALLOWED,
        RightsStatus.ATTRIBUTION_REQUIRED,
    } and not basis_url:
        raise PermanentEventError(
            "source_rights_basis_missing",
            "Publishable source rights require an explicit frozen basis URL.",
        )
    if (
        status == RightsStatus.ATTRIBUTION_REQUIRED
        and not attribution
    ):
        raise PermanentEventError(
            "source_attribution_missing",
            "Attribution-required rights need frozen attribution text.",
        )
    return {
        "rights_status": status,
        "rights_basis_url": basis_url or None,
        "attribution_text": attribution or None,
        "manual_review_required": not policy_publishable,
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
        raise ExtractorError(
            "profile_material_mismatch",
            "Extraction profile material hash does not match",
        )
    if (
        profile.model_manifest is not None
        and canonical_hash(profile.model_manifest) != profile.model_manifest_hash
    ):
        raise ExtractorError("model_manifest_mismatch", "Model manifest hash does not match")
    try:
        from .profiles import release_profile_snapshot_values

        released = release_profile_snapshot_values(profile)
    except ValidationError as exc:
        raise ExtractorError(
            "profile_release_mismatch",
            "Frozen extraction profile is not executable by the active release",
        ) from exc
    comparable_fields = (
        "profile_key",
        "profile_version",
        "engine",
        "extractor_version",
        "package_version",
        "runtime_version",
        "pipeline_name",
        "implementation_manifest_hash",
        "config",
        "config_hash",
        "validation_mode",
        "calibration_profile_key",
        "calibration_profile_version",
        "calibration_profile_hash",
        "model_manifest",
        "model_manifest_hash",
        "profile_material_hash",
    )
    if any(getattr(profile, field) != released[field] for field in comparable_fields):
        raise ExtractorError(
            "profile_release_mismatch",
            "Frozen extraction profile differs from the active release material",
        )


def _lock_expected_fanout(run_id: Any, fence: Mapping[str, Any]) -> RunStep:
    run = CollectionRun.objects.select_for_update().get(pk=run_id)
    step = RunStep.objects.select_for_update().get(
        run=run,
        name="extract",
        attempt_no=1,
    )
    if (
        run.stop_requested_at is not None
        or step.fanout_completed_at is not None
        or step.state != "running"
        or step.lease_generation != fence["expected_generation"]
        or step.lease_owner != fence["expected_lease_owner"]
        or step.lease_token != fence["expected_lease_token"]
    ):
        raise EvidenceConflict("Evidence fanout lease was lost")
    return step


def _create_raw_evidence(
    run_source_item,
    *,
    fanout_fence: Mapping[str, Any],
) -> EvidenceAsset:
    item = run_source_item.source_item
    source_record = {
        "source_record": {
            "source_item_id": str(item.id),
            "body_text": item.body_text,
            "metadata": item.metadata,
        },
    }
    content_hash = evidence_content_hash(
        text=item.body_text,
        structured_data=source_record,
        checksum=None,
    )
    fingerprint = _raw_input_fingerprint(
        run_source_item_id=run_source_item.id,
        source_item_id=item.id,
        input_kind="source_record",
        input_hash=content_hash,
    )
    rights = _rights(run_source_item, scope="record")
    with transaction.atomic():
        _lock_expected_fanout(run_source_item.run_id, fanout_fence)
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
                    "path_type": "json_pointer",
                    "path": "/source_record/body_text",
                },
                "extracted_text": item.body_text,
                "structured_data": source_record,
                "extraction_method": "source_record",
                "extractor_version": "v2",
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
    try:
        response = download_source_attachment(
            run_source_item.source_snapshot,
            url,
            operation_id=(
                f"attachment:{run_source_item.id}:"
                f"{hashlib.sha256(url.encode()).hexdigest()[:24]}"
            ),
            expected_content_types=source_attachment_content_types(
                run_source_item.source_snapshot
            ),
        )
    except SourceAccessError as exc:
        raise ExtractorError(
            exc.code,
            exc.detail,
            retryable=exc.retryable,
            retry_after_seconds=exc.retry_after_seconds,
        ) from exc
    mime_type = (
        response.headers.get("content-type", "application/octet-stream")
        .split(";", 1)[0]
        .strip()
    )
    filename = Path(parsed.path).name or "attachment.bin"
    return response.content, mime_type, filename


def _normalized_attachment_mime(value: str) -> str:
    normalized = str(value).split(";", 1)[0].strip().lower()
    return _ATTACHMENT_MIME_ALIASES.get(normalized, normalized)


def _approved_attachment_mime(
    run_source_item,
    *,
    declared_mime: str,
    sniffed_mime: str,
) -> str:
    approved = {
        _normalized_attachment_mime(value)
        for value in source_attachment_content_types(
            run_source_item.source_snapshot
        )
    }
    declared = _normalized_attachment_mime(declared_mime)
    sniffed = _normalized_attachment_mime(sniffed_mime)
    declared_is_generic = declared in _GENERIC_BINARY_MIME_TYPES
    sniffed_is_generic = sniffed in _GENERIC_BINARY_MIME_TYPES
    if (
        not declared_is_generic
        and not sniffed_is_generic
        and declared != sniffed
    ):
        raise ExtractorError(
            "attachment_mime_conflict",
            "Attachment declared and detected MIME types conflict",
        )
    effective = declared if sniffed_is_generic else sniffed
    if effective not in approved:
        raise ExtractorError(
            "attachment_mime_not_allowed",
            "Attachment MIME type is outside the frozen source contract",
        )
    if not declared_is_generic and declared not in approved:
        raise ExtractorError(
            "attachment_declared_mime_not_allowed",
            "Attachment declared MIME type is outside the frozen source contract",
        )
    return effective


def _attachment_rights_scope(mime_type: str) -> str:
    normalized = _normalized_attachment_mime(mime_type)
    if normalized.startswith(("image/", "audio/", "video/")):
        return "mediaAttachment"
    return "documentAttachment"


def _reusable_object_info(
    reservation: ExtractionObjectWriteReservation,
    *,
    checksum: str,
    byte_size: int,
    content_type: str,
) -> ObjectInfo | None:
    if reservation.state not in (
        ExtractionObjectWriteState.UPLOADED,
        ExtractionObjectWriteState.BOUND,
    ):
        return None
    if (
        reservation.checksum != checksum
        or reservation.byte_size != byte_size
        or reservation.content_type != content_type
        or not reservation.object_version
    ):
        raise EvidenceConflict("Existing object-write envelope differs from replay")
    return ObjectInfo(
        key=reservation.object_key,
        version_id=reservation.object_version,
        checksum_sha256=reservation.checksum,
        size=reservation.byte_size,
        content_type=reservation.content_type,
        etag=reservation.object_etag,
    )


def _required_object_version(info: ObjectInfo) -> str:
    if not isinstance(info.version_id, str) or not info.version_id.strip():
        raise ExtractorError(
            "object_version_missing",
            "Versioned object storage did not return an immutable object version",
        )
    return info.version_id


def _persist_attachment(
    run_source_item,
    attachment: Mapping[str, Any],
    data: bytes,
    mime_type: str,
    filename: str,
    *,
    fanout_fence: Mapping[str, Any],
):
    checksum = hashlib.sha256(data).hexdigest()
    key = content_addressed_key(
        namespace="evidence/raw",
        checksum_sha256=checksum,
        filename=filename,
    )
    with transaction.atomic():
        step = _lock_expected_fanout(run_source_item.run_id, fanout_fence)
        reservation = reserve_extraction_object_write(
            aggregate_kind="fanout",
            aggregate_id=run_source_item.run_id,
            source_event_id=step.source_event_id,
            lease_generation=step.lease_generation,
            lease_owner=step.lease_owner,
            lease_token=step.lease_token,
            purpose="raw",
            object_key=key,
        )
    info = _reusable_object_info(
        reservation,
        checksum=checksum,
        byte_size=len(data),
        content_type=mime_type,
    )
    if info is None:
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
        object_version = _required_object_version(info)
        mark_extraction_object_uploaded(
            reservation.id,
            object_version=object_version,
            object_etag=info.etag,
            checksum=info.checksum_sha256,
            byte_size=info.size,
            content_type=info.content_type,
        )
    stale_after_upload = False
    with transaction.atomic():
        try:
            _lock_expected_fanout(run_source_item.run_id, fanout_fence)
        except EvidenceConflict:
            stale_after_upload = True
    if stale_after_upload:
        orphan_unbound_extraction_object_writes(
            aggregate_kind="fanout",
            aggregate_id=run_source_item.run_id,
            lease_generation=reservation.lease_generation,
        )
        raise EvidenceConflict("Evidence fanout lease was lost after object upload")
    if (
        info.checksum_sha256 != checksum
        or info.size != len(data)
        or info.content_type != mime_type
    ):
        orphan_unbound_extraction_object_writes(
            aggregate_kind="fanout",
            aggregate_id=run_source_item.run_id,
            lease_generation=reservation.lease_generation,
        )
        raise ExtractorError(
            "raw_object_identity_mismatch",
            "Stored raw object identity differs from the verified attachment",
        )
    rights = _rights(
        run_source_item,
        scope=_attachment_rights_scope(mime_type),
        attachment=attachment,
    )
    content_hash = evidence_content_hash(
        text=None,
        structured_data={"title": attachment.get("title")},
        checksum=checksum,
    )
    fingerprint = _raw_input_fingerprint(
        run_source_item_id=run_source_item.id,
        source_item_id=run_source_item.source_item_id,
        input_kind="attachment",
        input_hash=checksum,
    )
    with transaction.atomic():
        _lock_expected_fanout(run_source_item.run_id, fanout_fence)
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
                    _required_object_version(info)
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
            bind_extraction_object_write(reservation.id)
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
        bind_extraction_object_write(reservation.id)
        return evidence, info


_MEDIA_ATTACHMENT_SUFFIXES = frozenset(
    {
        ".avif",
        ".gif",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".wav",
        ".webm",
        ".webp",
    }
)


def _metadata_only_attachment(
    run_source_item,
    attachment: Mapping[str, Any],
) -> bool:
    frozen = run_source_item.source_snapshot.frozen_config
    external = (
        frozen.get("externalConfig")
        if isinstance(frozen, dict)
        else None
    )
    if (
        not isinstance(external, dict)
        or external.get("mediaDownloadPolicy") != "metadata_only"
    ):
        return False
    declared = _normalized_attachment_mime(
        attachment.get("mime_type")
        or attachment.get("mimeType")
        or "application/octet-stream"
    )
    if declared.startswith(("image/", "audio/", "video/")):
        return True
    if declared not in _GENERIC_BINARY_MIME_TYPES:
        return False
    suffix = Path(
        urlparse(str(attachment.get("url", ""))).path
    ).suffix.lower()
    return not suffix or suffix in _MEDIA_ATTACHMENT_SUFFIXES


def _persist_metadata_only_attachment(
    run_source_item,
    attachment: Mapping[str, Any],
    *,
    fanout_fence: Mapping[str, Any],
) -> EvidenceAsset:
    declared = _normalized_attachment_mime(
        attachment.get("mime_type")
        or attachment.get("mimeType")
        or "application/octet-stream"
    )
    structured = {
        "title": attachment.get("title"),
        "source_url": redact_url(str(attachment.get("url", ""))),
        "declared_mime_type": declared,
        "metadata_only": True,
        "download_block_reason": "source_media_download_policy",
    }
    content_hash = evidence_content_hash(
        text=None,
        structured_data=structured,
        checksum=None,
    )
    fingerprint = _raw_input_fingerprint(
        run_source_item_id=run_source_item.id,
        source_item_id=run_source_item.source_item_id,
        input_kind="attachment_metadata",
        input_hash=canonical_hash(structured),
    )
    rights = _rights(
        run_source_item,
        scope="mediaAttachment",
        attachment=attachment,
    )
    with transaction.atomic():
        _lock_expected_fanout(run_source_item.run_id, fanout_fence)
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
                "mime_type": declared,
                "structured_data": structured,
                "extraction_method": "source_attachment_metadata",
                "extractor_version": "v1",
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
                or evidence.object_key is not None
                or evidence.evidence_content_hash != content_hash
            ):
                raise PermanentEventError(
                    "raw_input_identity_conflict"
                )
            return evidence
        evidence.review_subject_hash = calculate_review_subject_hash(
            evidence
        )
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


@contextmanager
def _leaf_object_write_fence(
    aggregate: ExtractionRun | GenericExtractionAttempt,
):
    if isinstance(aggregate, ExtractionRun):
        with extraction_run_completion_fence(
            aggregate.id,
            expected_parent_generation=aggregate.parent_lease_generation,
            expected_child_generation=aggregate.lease_generation,
            expected_lease_owner=aggregate.lease_owner,
            expected_lease_token=aggregate.lease_token,
        ) as locked:
            yield locked
    else:
        with generic_extraction_completion_fence(
            aggregate.id,
            expected_generation=aggregate.lease_generation,
            expected_lease_owner=aggregate.lease_owner,
            expected_lease_token=aggregate.lease_token,
        ) as locked:
            yield locked


def _store_result(
    namespace: str,
    aggregate: ExtractionRun | GenericExtractionAttempt,
    output: Mapping[str, Any],
):
    data = canonical_bytes(output)
    checksum = sha256_bytes(data)
    key = f"evidence/results/{namespace}/{aggregate.id}/{checksum}.json"
    with _leaf_object_write_fence(aggregate) as locked:
        if locked is None:
            raise EvidenceConflict("Extraction lease was lost before object reservation")
        reservation = reserve_extraction_object_write(
            aggregate_kind=(
                "extraction_run"
                if isinstance(locked, ExtractionRun)
                else "generic"
            ),
            aggregate_id=locked.id,
            source_event_id=locked.source_event_id,
            lease_generation=locked.lease_generation,
            lease_owner=locked.lease_owner,
            lease_token=locked.lease_token,
            purpose="reason" if namespace == "reasons" else "result",
            object_key=key,
        )
    info = _reusable_object_info(
        reservation,
        checksum=checksum,
        byte_size=len(data),
        content_type="application/json",
    )
    if info is None:
        info = _storage().put_bytes(
            key=key,
            data=data,
            content_type="application/json",
            checksum_sha256=checksum,
            metadata={"aggregate_id": str(aggregate.id)},
        )
        object_version = _required_object_version(info)
        mark_extraction_object_uploaded(
            reservation.id,
            object_version=object_version,
            object_etag=info.etag,
            checksum=info.checksum_sha256,
            byte_size=info.size,
            content_type=info.content_type,
        )
    with _leaf_object_write_fence(aggregate) as locked:
        stale_after_upload = locked is None
    if stale_after_upload:
        orphan_unbound_extraction_object_writes(
            aggregate_kind=reservation.aggregate_kind,
            aggregate_id=reservation.aggregate_id,
            lease_generation=reservation.lease_generation,
        )
        raise EvidenceConflict("Extraction lease was lost after object upload")
    return info, reservation.id


def _store_converted_pdf(
    attempt: GenericExtractionAttempt,
    *,
    converted_path: Path,
    converted_checksum: str,
    converted_size: int,
):
    converted_key = content_addressed_key(
        namespace="evidence/converted",
        checksum_sha256=converted_checksum,
        filename="converted.pdf",
    )
    with _leaf_object_write_fence(attempt) as locked:
        if locked is None:
            raise EvidenceConflict(
                "Extraction lease was lost before converted object reservation"
            )
        reservation = reserve_extraction_object_write(
            aggregate_kind="generic",
            aggregate_id=locked.id,
            source_event_id=locked.source_event_id,
            lease_generation=locked.lease_generation,
            lease_owner=locked.lease_owner,
            lease_token=locked.lease_token,
            purpose="converted",
            object_key=converted_key,
        )
    info = _reusable_object_info(
        reservation,
        checksum=converted_checksum,
        byte_size=converted_size,
        content_type="application/pdf",
    )
    if info is None:
        info = _storage().put_file(
            key=converted_key,
            path=converted_path,
            content_type="application/pdf",
            checksum_sha256=converted_checksum,
            expected_size=converted_size,
            metadata={"generic_attempt_id": str(attempt.id)},
        )
        object_version = _required_object_version(info)
        mark_extraction_object_uploaded(
            reservation.id,
            object_version=object_version,
            object_etag=info.etag,
            checksum=info.checksum_sha256,
            byte_size=info.size,
            content_type=info.content_type,
        )
    with _leaf_object_write_fence(attempt) as locked:
        stale_after_upload = locked is None
    if stale_after_upload:
        orphan_unbound_extraction_object_writes(
            aggregate_kind="generic",
            aggregate_id=attempt.id,
            lease_generation=attempt.lease_generation,
        )
        raise EvidenceConflict(
            "Extraction lease was lost after converted object upload"
        )
    if (
        info.checksum_sha256 != converted_checksum
        or info.size != converted_size
        or info.content_type != "application/pdf"
    ):
        orphan_unbound_extraction_object_writes(
            aggregate_kind="generic",
            aggregate_id=attempt.id,
            lease_generation=attempt.lease_generation,
        )
        raise ExtractorError(
            "legacy_hwp_output_invalid",
            "Stored PDF identity differs from the verified conversion",
        )
    return info, reservation.id


def _expected_document_evidence_material(
    document: DocumentExtraction,
    run: ExtractionRun,
    output,
) -> tuple[str, int]:
    entries: list[dict[str, str]] = []
    for page in output.pages:
        for block in page.blocks:
            locator = {
                "locator_type": "document_block",
                "page_index": page.page_index,
                "block_id": block.block_id,
                "block_type": block.block_type,
                "polygon": block.polygon,
                "bbox": block.bbox,
                "reading_order": block.reading_order,
            }
            entries.append(
                document_evidence_manifest_entry(
                    source_item_id=document.source_item_id,
                    origin_run_source_item_id=document.run_source_item_id,
                    parent_asset_id=document.input_asset_id,
                    document_extraction_id=document.id,
                    extraction_run_id=run.id,
                    extraction_profile_snapshot_id=(
                        run.extraction_profile_snapshot_id
                    ),
                    profile_material_hash=run.profile_material_hash,
                    extraction_method=run.engine,
                    extractor_version=run.package_version,
                    extraction_config_hash=run.config_hash,
                    result_checksum=run.result_checksum,
                    locator=locator,
                    evidence_content_hash_value=evidence_content_hash(
                        text=block.text,
                        structured_data=block.structured_data,
                        checksum=None,
                    ),
                )
            )
    entries.sort(key=lambda entry: (entry["locator_hash"], entry["evidence_content_hash"]))
    return canonical_hash(entries), len(entries)


def _derived_input_rights(run_source_item, input_asset, *, manual: bool) -> dict[str, Any]:
    if input_asset is None:
        rights = _rights(run_source_item)
    else:
        rights = {
            "rights_status": input_asset.rights_status,
            "rights_basis_url": input_asset.rights_basis_url,
            "attribution_text": input_asset.attribution_text,
            "manual_review_required": input_asset.manual_review_required,
        }
    return {
        **rights,
        "manual_review_required": manual or bool(rights.get("manual_review_required")),
    }


def _make_document_evidence(document, run, output, run_source_item) -> list[EvidenceAsset]:
    reasons = normalize_low_confidence_reasons(output.low_confidence_reasons)
    manual = run.state == ExtractionState.LOW_CONFIDENCE
    rights = _derived_input_rights(run_source_item, document.input_asset, manual=manual)
    manual = rights["manual_review_required"]
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
                extraction_config_hash=run.config_hash,
                extraction_result_checksum=run.result_checksum,
                confidence=block.confidence,
                confidence_detail=output.confidence_summary,
                low_confidence_reasons=reasons,
                evidence_content_hash=content_hash,
                review_subject_hash="0" * 64,
                review_state=ReviewState.MANUAL_REQUIRED if manual else ReviewState.PASSED,
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


def _load_or_create_document_plan(
    document_id: Any,
    *,
    routes,
    expected_generation: int,
    expected_lease_owner: str,
    expected_lease_token: uuid.UUID,
) -> dict[str, ExtractionRun]:
    selected_by_engine: dict[str, ExtractionRun] = {}
    route_groups = group_routes(routes)
    with document_extraction_completion_fence(
        document_id,
        expected_generation=expected_generation,
        expected_lease_owner=expected_lease_owner,
        expected_lease_token=expected_lease_token,
    ) as document:
        if document is None:
            return selected_by_engine
        frozen_pages = (
            document.routing_manifest.get("pages", [])
            if isinstance(document.routing_manifest, dict)
            else []
        )
        if frozen_pages:
            expected_routes = {
                route.page_index: (route.engine, route.reason) for route in routes
            }
            if len(frozen_pages) != len(expected_routes):
                raise ExtractorError(
                    "routing_manifest_changed",
                    "Frozen document routing page count changed",
                )
            for page in frozen_pages:
                page_index = page.get("page_index")
                if expected_routes.get(page_index) != (
                    page.get("engine"),
                    page.get("reason"),
                ):
                    raise ExtractorError(
                        "routing_manifest_changed",
                        "Frozen document routing differs from repeated inspection",
                    )
                child = ExtractionRun.objects.select_for_update().get(
                    pk=page.get("selected_run_id"),
                    document_extraction=document,
                )
                if (
                    str(child.extraction_profile_snapshot_id)
                    != str(page.get("profile_snapshot_id"))
                    or child.engine != page.get("engine")
                    or page_index not in child.requested_page_indices
                ):
                    raise ExtractorError(
                        "routing_manifest_invalid",
                        "Frozen extraction profile routing is incompatible",
                    )
                selected_by_engine[child.engine] = child
            if set(selected_by_engine) != set(route_groups):
                raise ExtractorError(
                    "routing_manifest_changed",
                    "Frozen document routing engine set changed",
                )
            return selected_by_engine

        for engine, pages in route_groups.items():
            preferred = None
            if engine == ExtractionEngine.PADDLEOCR:
                language = document.run_source_item.source_snapshot.config.get(
                    "ocrLanguage", "ko"
                )
                preferred = "paddle-en-v1" if language == "en" else "paddle-ko-v1"
            elif engine == ExtractionEngine.NATIVE_PDF:
                preferred = "native-pdf-v1"
            safety = (
                document.routing_manifest.get("safety")
                if isinstance(document.routing_manifest, dict)
                else None
            )
            frozen_routing = (
                safety.get("routing_profiles", {})
                if isinstance(safety, dict)
                else {}
            )
            frozen_profile = frozen_routing.get(engine)
            if (
                not isinstance(frozen_profile, dict)
                and isinstance(safety, dict)
                and safety.get("engine") == engine
            ):
                frozen_profile = safety
            if isinstance(frozen_profile, dict):
                try:
                    profile = ExtractionProfileSnapshot.objects.get(
                        pk=frozen_profile.get("profile_snapshot_id"),
                        engine=engine,
                    )
                except ExtractionProfileSnapshot.DoesNotExist as exc:
                    raise ExtractorError(
                        "safety_profile_missing",
                        "Frozen routing profile is missing",
                    ) from exc
                if (
                    profile.profile_material_hash != frozen_profile.get("profile_material_hash")
                    or profile.config_hash != frozen_profile.get("config_hash")
                ):
                    raise ExtractorError(
                        "safety_profile_mismatch",
                        "Frozen routing profile material changed",
                    )
                _verify_profile(profile)
            elif safety is not None:
                raise ExtractorError(
                    "safety_profile_missing",
                    "Frozen safety routing omitted a selected engine profile",
                )
            else:
                profile = _profile(engine, preferred_key=preferred)
            page_set_hash, fingerprint = extraction_fingerprint(document, profile, pages)
            child, _ = ExtractionRun.objects.get_or_create(
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
                    "package_version": profile.package_version
                    or profile.extractor_version,
                    "runtime_version": profile.runtime_version
                    or profile.extractor_version,
                    "pipeline_name": profile.pipeline_name,
                    "model_manifest": profile.model_manifest,
                    "model_manifest_hash": profile.model_manifest_hash,
                    "language_profile": profile.config.get("text_recognition_model"),
                    "device_type": profile.config.get("device"),
                    "source_event_id": document.source_event_id,
                    "parent_lease_generation": document.lease_generation,
                },
            )
            selected_by_engine[engine] = child
        frozen_safety = (
            {
                key: document.routing_manifest.get(key)
                for key in ("safety", "safety_material_hash")
                if key in document.routing_manifest
            }
            if isinstance(document.routing_manifest, dict)
            else {}
        )
        document.routing_manifest = {
            "schema_version": "extraction-routing-v2",
            **frozen_safety,
            "pages": [
                {
                    "page_index": route.page_index,
                    "selected_run_id": str(selected_by_engine[route.engine].id),
                    "profile_snapshot_id": str(
                        selected_by_engine[route.engine].extraction_profile_snapshot_id
                    ),
                    "engine": route.engine,
                    "reason": route.reason,
                }
                for route in routes
            ],
        }
        document.save(update_fields=("routing_manifest", "updated_at"))
        return selected_by_engine


def _frozen_document_safety_profile(
    document_id: Any,
    *,
    engine: str,
    expected_generation: int,
    expected_lease_owner: str,
    expected_lease_token: uuid.UUID,
    preferred_key: str | None = None,
    additional_profiles: tuple[tuple[str, str | None], ...] = (),
) -> ExtractionProfileSnapshot:
    with document_extraction_completion_fence(
        document_id,
        expected_generation=expected_generation,
        expected_lease_owner=expected_lease_owner,
        expected_lease_token=expected_lease_token,
    ) as document:
        if document is None:
            raise EvidenceConflict("Document extraction lease was lost before safety routing")
        manifest = dict(document.routing_manifest or {})
        safety = manifest.get("safety")
        if safety is not None:
            if (
                not isinstance(safety, dict)
                or manifest.get("safety_material_hash") != canonical_hash(safety)
                or safety.get("engine") != engine
            ):
                raise ExtractorError(
                    "safety_profile_mismatch",
                    "Frozen document inspection safety material is invalid",
                )
            try:
                profile = ExtractionProfileSnapshot.objects.get(
                    pk=safety.get("profile_snapshot_id"),
                    engine=engine,
                )
            except ExtractionProfileSnapshot.DoesNotExist as exc:
                raise ExtractorError(
                    "safety_profile_missing",
                    "Frozen document inspection profile is missing",
                ) from exc
            if (
                profile.profile_material_hash != safety.get("profile_material_hash")
                or profile.config_hash != safety.get("config_hash")
            ):
                raise ExtractorError(
                    "safety_profile_mismatch",
                    "Frozen document inspection profile differs from its routing material",
                )
            _verify_profile(profile)
            return profile
        if manifest.get("pages"):
            raise ExtractorError(
                "safety_profile_missing",
                "Legacy document routing has no frozen inspection safety material",
            )
        profile = _profile(engine, preferred_key=preferred_key)
        routing_profiles = {}
        for routing_engine, routing_key in ((engine, preferred_key), *additional_profiles):
            routed = profile if routing_engine == engine else _profile(
                routing_engine, preferred_key=routing_key
            )
            routing_profiles[routing_engine] = {
                "engine": routed.engine,
                "profile_snapshot_id": str(routed.id),
                "profile_material_hash": routed.profile_material_hash,
                "config_hash": routed.config_hash,
            }
        safety = {
            "engine": profile.engine,
            "profile_snapshot_id": str(profile.id),
            "profile_material_hash": profile.profile_material_hash,
            "config_hash": profile.config_hash,
            "routing_profiles": routing_profiles,
        }
        manifest.update({
            "schema_version": "extraction-routing-v2",
            "safety": safety,
            "safety_material_hash": canonical_hash(safety),
            "pages": [],
        })
        document.routing_manifest = manifest
        document.save(update_fields=("routing_manifest", "updated_at"))
        return profile


def _run_document_extraction(
    document_id: Any,
    *,
    expected_generation: int,
    expected_lease_owner: str,
    expected_lease_token: uuid.UUID,
) -> DocumentExtraction:
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
    with document_extraction_completion_fence(
        document_id,
        expected_generation=expected_generation,
        expected_lease_owner=expected_lease_owner,
        expected_lease_token=expected_lease_token,
    ) as current_document:
        if current_document is None:
            return DocumentExtraction.objects.get(pk=document_id)
    input_size = document.input_asset.byte_size if document.input_asset_id else None
    raw_external = bool(
        document.input_asset_id
        and document.input_asset.derivation_type == EvidenceDerivationType.RAW
    )
    data = _read_frozen_object(
        storage=_storage(),
        key=document.input_object_key,
        version_id=document.input_object_version,
        expected_size=input_size,
        expected_checksum=document.input_checksum,
        hard_max_bytes=(
            MAX_EXTERNAL_DOCUMENT_BYTES if raw_external else MAX_DERIVED_DOCUMENT_BYTES
        ),
    )
    suffix = ".pdf" if document.input_kind == DocumentInputKind.PDF else ".img"
    with tempfile.TemporaryDirectory(prefix="wisdome-evidence-") as temp_dir:
        path = Path(temp_dir) / f"input{suffix}"
        path.write_bytes(data)
        if document.input_kind == DocumentInputKind.PDF:
            language = document.run_source_item.source_snapshot.config.get(
                "ocrLanguage", "ko"
            )
            native_profile = _frozen_document_safety_profile(
                document.id,
                engine=ExtractionEngine.NATIVE_PDF,
                preferred_key="native-pdf-v1",
                expected_generation=expected_generation,
                expected_lease_owner=expected_lease_owner,
                expected_lease_token=expected_lease_token,
                additional_profiles=((
                    ExtractionEngine.PADDLEOCR,
                    "paddle-en-v1" if language == "en" else "paddle-ko-v1",
                ),),
            )
            inspection = NativePdfExtractor(native_profile.config).inspect(path)
            if inspection.checksum != document.input_checksum:
                raise ExtractorError(
                    "input_checksum_mismatch",
                    "Local PDF checksum differs from provenance",
                )
            document.input_page_count = inspection.page_count
            document.expected_page_indices = list(range(inspection.page_count))
            routes = route_pdf_pages(signal.as_dict() for signal in inspection.page_signals)
        else:
            language = document.run_source_item.source_snapshot.config.get(
                "ocrLanguage", "ko"
            )
            paddle_profile = _frozen_document_safety_profile(
                document.id,
                engine=ExtractionEngine.PADDLEOCR,
                preferred_key=("paddle-en-v1" if language == "en" else "paddle-ko-v1"),
                expected_generation=expected_generation,
                expected_lease_owner=expected_lease_owner,
                expected_lease_token=expected_lease_token,
            )
            inspect_static_image(path, paddle_profile.config)
            document.input_page_count = 1
            document.input_frame_count = 1
            document.expected_page_indices = [0]
            from .services import PageRoute
            routes = [PageRoute(0, ExtractionEngine.PADDLEOCR, "standalone_image")]
        input_page_count = document.input_page_count
        input_frame_count = document.input_frame_count
        expected_page_indices = document.expected_page_indices
        with document_extraction_completion_fence(
            document_id,
            expected_generation=expected_generation,
            expected_lease_owner=expected_lease_owner,
            expected_lease_token=expected_lease_token,
        ) as document:
            if document is None:
                return DocumentExtraction.objects.get(pk=document_id)
            document.input_page_count = input_page_count
            document.input_frame_count = input_frame_count
            document.expected_page_indices = expected_page_indices
            document.full_clean()
            document.save(
                update_fields=(
                    "input_page_count",
                    "input_frame_count",
                    "expected_page_indices",
                    "updated_at",
                )
            )

        route_groups = group_routes(routes)
        selected_by_engine = _load_or_create_document_plan(
            document_id,
            routes=routes,
            expected_generation=expected_generation,
            expected_lease_owner=expected_lease_owner,
            expected_lease_token=expected_lease_token,
        )
        if set(selected_by_engine) != set(route_groups):
            return DocumentExtraction.objects.get(pk=document_id)

        for engine, pages in route_groups.items():
            run = selected_by_engine[engine]
            if run.state in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE):
                continue
            run = begin_extraction_run(
                run.id,
                expected_parent_generation=expected_generation,
                expected_parent_lease_owner=expected_lease_owner,
                expected_parent_lease_token=expected_lease_token,
            )
            if run is None:
                return DocumentExtraction.objects.get(pk=document_id)
            profile = run.extraction_profile_snapshot
            started = timezone.now()
            with extraction_run_completion_fence(
                run.id,
                expected_parent_generation=expected_generation,
                expected_child_generation=run.lease_generation,
                expected_lease_owner=run.lease_owner,
                expected_lease_token=run.lease_token,
            ) as current_child:
                if current_child is None:
                    return DocumentExtraction.objects.get(pk=document_id)
            if not CollectionRun.objects.filter(
                pk=document.run_source_item.run_id,
                state=RunState.EXTRACTING,
                stop_requested_at__isnull=True,
            ).exists():
                return DocumentExtraction.objects.get(pk=document_id)
            if engine == ExtractionEngine.NATIVE_PDF:
                output = NativePdfExtractor(profile.config).extract(path, pages)
            else:
                output = PaddleOCRExtractor(
                    profile.config,
                    model_manifest=profile.model_manifest or {},
                    model_manifest_hash=profile.model_manifest_hash or "",
                    config_hash=profile.config_hash,
                ).extract(path, pages, input_kind=document.input_kind)
            result, result_reservation_id = _store_result(
                "document", run, output.as_dict()
            )
            reasons = normalize_low_confidence_reasons(output.low_confidence_reasons)
            terminal_state = (
                ExtractionState.LOW_CONFIDENCE
                if reasons
                else ExtractionState.SUCCEEDED
            )
            reason_info = None
            if reasons:
                reason_info, reason_reservation_id = _store_result(
                    "reasons", run, {"reasons": reasons}
                )
            else:
                reason_reservation_id = None
            finished_at = timezone.now()
            duration_ms = int((finished_at - started).total_seconds() * 1000)
            with extraction_run_completion_fence(
                run.id,
                expected_parent_generation=expected_generation,
                expected_child_generation=run.lease_generation,
                expected_lease_owner=run.lease_owner,
                expected_lease_token=run.lease_token,
            ) as run:
                if run is None:
                    return DocumentExtraction.objects.get(pk=document_id)
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
                        _required_object_version(reason_info)
                    )
                run.duration_ms = duration_ms
                run.finished_at = finished_at
                (
                    run.expected_evidence_manifest_hash,
                    run.expected_evidence_count,
                ) = _expected_document_evidence_material(document, run, output)
                run.next_retry_at = None
                run.lease_owner = ""
                run.lease_token = None
                run.terminal_state = "ready"
                run.terminal_event_key = (
                    f"extraction.run.ready:{run.id}:generation:"
                    f"{run.lease_generation}:{run.result_checksum}"
                )
                run.full_clean()
                run.save()
                _make_document_evidence(
                    document,
                    run,
                    output,
                    document.run_source_item,
                )
                bind_extraction_object_write(result_reservation_id)
                if reason_reservation_id is not None:
                    bind_extraction_object_write(reason_reservation_id)
    with document_extraction_completion_fence(
        document_id,
        expected_generation=expected_generation,
        expected_lease_owner=expected_lease_owner,
        expected_lease_token=expected_lease_token,
    ) as current_document:
        if current_document is None:
            return DocumentExtraction.objects.get(pk=document_id)
        document = aggregate_document_extraction(current_document.id)
        if document.document_complete:
            ready_event_key = (
                f"evidence.document_ready:{document.id}:"
                f"{document.selected_evidence_manifest_hash}"
            )
            document.terminal_event_key = ready_event_key
            document.terminal_state = "ready"
            document.lease_owner = ""
            document.lease_token = None
            document.next_retry_at = None
            document.save(
                update_fields=(
                    "terminal_event_key",
                    "terminal_state",
                    "lease_owner",
                    "lease_token",
                    "next_retry_at",
                    "updated_at",
                )
            )
            enqueue_event(
                topic="evidence.document_ready",
                aggregate_type="DocumentExtraction",
                aggregate_id=document.id,
                message_key=ready_event_key,
                payload={
                    "run_id": str(document.run_source_item.run_id),
                    "run_source_item_id": str(document.run_source_item_id),
                    "source_item_id": str(document.source_item_id),
                    "document_extraction_id": str(document.id),
                    "input_page_count": document.input_page_count,
                    "coverage_manifest_hash": document.coverage_manifest_hash,
                    "selected_evidence_manifest_hash": document.selected_evidence_manifest_hash,
                    "document_complete": True,
                    "input_asset_id": (
                        str(document.input_asset_id)
                        if document.input_asset_id else None
                    ),
                    "input_checksum": document.input_checksum,
                    "input_fingerprint": document.input_fingerprint,
                    "routing_manifest_hash": canonical_hash(
                        document.routing_manifest
                    ),
                },
            )
        elif document.state in {
            ExtractionState.FAILED,
            ExtractionState.LOW_CONFIDENCE,
        }:
            cause = (
                f"document-terminal:{document.id}:generation:"
                f"{expected_generation}:{document.state}"
            )
            document.terminal_event_key = (
                f"evidence.finalize_requested:{document.run_source_item.run_id}:{cause}"
            )
            document.terminal_state = (
                "ready"
                if document.state == ExtractionState.LOW_CONFIDENCE
                else "failed"
            )
            document.lease_owner = ""
            document.lease_token = None
            document.save(
                update_fields=(
                    "terminal_event_key",
                    "terminal_state",
                    "lease_owner",
                    "lease_token",
                    "updated_at",
                )
            )
            _enqueue_finalize(str(document.run_source_item.run_id), cause)
    return document


def _generic_extractor(profile: ExtractionProfileSnapshot):
    try:
        return GENERIC_ENGINE_FACTORIES[profile.engine](profile.config)
    except KeyError as exc:
        raise ExtractorError(
            "generic_engine_unsupported",
            f"No local adapter for {profile.engine}",
        ) from exc


def _validate_generic_output(
    profile: ExtractionProfileSnapshot,
    output: GenericExtractionOutput,
) -> None:
    if (
        output.engine != profile.engine
        or output.extractor_version != profile.extractor_version
        or output.validation_mode != profile.validation_mode
    ):
        raise ExtractorError(
            "generic_output_identity_mismatch",
            "Generic extractor output differs from the frozen profile identity",
        )
    if not output.records:
        raise ExtractorError(
            "generic_result_empty",
            "Generic extractor returned no evidence records",
        )
    configured_limit = int(getattr(profile, "config", {}).get("max_records", 50_000))
    if len(output.records) > min(max(configured_limit, 1), 50_000):
        raise ExtractorError(
            "generic_record_limit_exceeded",
            "Generic extractor returned more records than the frozen safety limit",
        )
    if profile.engine == ExtractionEngine.LEGACY_HWP and len(output.records) != 1:
        raise ExtractorError(
            "legacy_hwp_output_invalid",
            "Legacy HWP conversion must return exactly one verified PDF record",
        )
    calibrated = profile.validation_mode == GenericValidationMode.CALIBRATED
    if calibrated and any(record.confidence is None for record in output.records):
        raise ExtractorError(
            "generic_confidence_missing",
            "Calibrated generic output omitted numeric confidence",
        )
    if not calibrated and any(record.confidence is not None for record in output.records):
        raise ExtractorError(
            "generic_confidence_unapproved",
            "Non-calibrated generic output included numeric confidence",
        )


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


def _run_generic_extraction(
    attempt_id: Any,
    *,
    expected_generation: int,
    expected_lease_owner: str,
    expected_lease_token: uuid.UUID,
) -> GenericExtractionAttempt:
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
    suffix = Path(
        str((attempt.input_asset.structured_data or {}).get("source_url", "input.bin"))
    ).suffix
    with tempfile.TemporaryDirectory(prefix="wisdome-generic-") as temp_dir:
        path = Path(temp_dir) / f"input{suffix or '.bin'}"
        if profile.engine == ExtractionEngine.LEGACY_HWP:
            if (
                not attempt.input_asset.checksum
                or not isinstance(attempt.input_asset.byte_size, int)
                or attempt.input_asset.byte_size < 1
                or not attempt.input_asset.object_version
            ):
                raise ExtractorError(
                    "generic_input_missing",
                    "Legacy HWP input provenance is incomplete",
                )
            try:
                _storage().get_file(
                    key=attempt.input_asset.object_key,
                    version_id=attempt.input_asset.object_version,
                    destination=path,
                    expected_checksum_sha256=attempt.input_asset.checksum,
                    expected_size=attempt.input_asset.byte_size,
                )
            except (OSError, ValueError) as exc:
                raise ExtractorError(
                    "input_checksum_mismatch",
                    "Downloaded legacy HWP input differs from provenance",
                ) from exc
        else:
            data = _read_frozen_object(
                storage=_storage(),
                key=attempt.input_asset.object_key,
                version_id=attempt.input_asset.object_version or "",
                expected_size=attempt.input_asset.byte_size,
                expected_checksum=attempt.input_asset.checksum or "",
                hard_max_bytes=MAX_DERIVED_DOCUMENT_BYTES,
            )
            path.write_bytes(data)
        with generic_extraction_completion_fence(
            attempt.id,
            expected_generation=expected_generation,
            expected_lease_owner=expected_lease_owner,
            expected_lease_token=expected_lease_token,
        ) as current_attempt:
            if current_attempt is None:
                return GenericExtractionAttempt.objects.get(pk=attempt.id)
        if not CollectionRun.objects.filter(
            pk=attempt.run_source_item.run_id,
            state=RunState.EXTRACTING,
            stop_requested_at__isnull=True,
        ).exists():
            return GenericExtractionAttempt.objects.get(pk=attempt.id)
        extractor = _generic_extractor(profile)
        if profile.engine == ExtractionEngine.LEGACY_HWP:
            output: GenericExtractionOutput = extractor.extract(
                path,
                attempt_id=str(attempt.id),
                generation=_legacy_hwp_protocol_generation(expected_generation),
            )
        else:
            output = extractor.extract(path)
        _validate_generic_output(profile, output)
        result, result_reservation_id = _store_result(
            "generic", attempt, output.as_dict()
        )
        reasons = normalize_low_confidence_reasons(output.low_confidence_reasons)
        reason_info = None
        if reasons:
            reason_info, reason_reservation_id = _store_result(
                "reasons", attempt, {"reasons": reasons}
            )
        else:
            reason_reservation_id = None
        first = output.records[0] if output.records else None
        if first is None:
            raise ExtractorError(
                "generic_result_empty",
                "Generic extractor returned no evidence records",
            )
        legacy_info = None
        if profile.engine == ExtractionEngine.LEGACY_HWP:
            converted_path = Path(first.object_path or "")
            if not converted_path.is_file():
                raise ExtractorError(
                    "legacy_hwp_output_invalid",
                    "Converted PDF disappeared before storage",
                )
            converted_size = converted_path.stat().st_size
            converted_checksum = sha256_file(converted_path)
            first_record = first.as_dict()
            converted_page_count = _verified_legacy_hwp_record_material(first_record)
            locator = first_record["locator"]
            report = first_record["structured_data"]["conversion_report"]
            if (
                locator.get("output_pdf_checksum") != converted_checksum
                or locator.get("output_pdf_byte_size") != converted_size
                or report.get("output_pdf_checksum_sha256") != converted_checksum
                or report.get("output_pdf_byte_size") != converted_size
            ):
                raise ExtractorError(
                    "legacy_hwp_output_invalid",
                    "Converted PDF changed before storage",
                )
            info, converted_reservation_id = _store_converted_pdf(
                attempt,
                converted_path=converted_path,
                converted_checksum=converted_checksum,
                converted_size=converted_size,
            )
            legacy_info = (
                info,
                converted_checksum,
                converted_size,
                converted_page_count,
                converted_reservation_id,
            )

        with generic_extraction_completion_fence(
            attempt_id,
            expected_generation=expected_generation,
            expected_lease_owner=expected_lease_owner,
            expected_lease_token=expected_lease_token,
        ) as attempt:
            if attempt is None:
                return GenericExtractionAttempt.objects.get(pk=attempt_id)
            if _is_quarantined_generic_attempt(attempt):
                return attempt
            if attempt.state in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE):
                return _converge_legacy_hwp_document_locked(attempt)
            attempt.result_checksum = result.checksum_sha256
            attempt.low_confidence_reasons_hash = canonical_hash(reasons) if reasons else None
            if reason_info:
                attempt.low_confidence_reasons_object_key = reason_info.key
                attempt.low_confidence_reasons_object_version = (
                    _required_object_version(reason_info)
                )
            ready_event_key = (
                f"evidence.other_ready:{attempt.id}:{attempt.result_checksum}"
            )
            manual = bool(reasons)
            rights = _derived_input_rights(
                attempt.run_source_item, attempt.input_asset, manual=manual,
            )
            manual = rights["manual_review_required"]
            evidence_assets: list[EvidenceAsset] = []
            for record_index, record in enumerate(output.records):
                record_material = record.as_dict()
                record_material["object_path"] = None
                structured = {
                    "records": [record_material],
                    "metadata": dict(output.metadata),
                    "record_index": record_index,
                }
                storage_fields = {}
                if legacy_info and record_index == 0:
                    info, converted_checksum, converted_size, _, _ = legacy_info
                    storage_fields = {
                        "object_key": info.key,
                        "object_version": _required_object_version(info),
                        "mime_type": "application/pdf",
                        "byte_size": converted_size,
                        "checksum": converted_checksum,
                    }
                evidence = EvidenceAsset(
                    source_item=attempt.source_item,
                    origin_run_source_item=attempt.run_source_item,
                    derivation_type=EvidenceDerivationType.OTHER,
                    generic_extraction_attempt=attempt,
                    parent_asset=attempt.input_asset,
                    kind=record.kind,
                    locator_type=record.locator_type,
                    locator=dict(record.locator),
                    extracted_text=record.text or None,
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
                        record.confidence
                        if profile.validation_mode == GenericValidationMode.CALIBRATED
                        else None
                    ),
                    low_confidence_reasons=reasons,
                    evidence_content_hash=evidence_content_hash(
                        text=record.text or None,
                        structured_data=structured,
                        checksum=storage_fields.get("checksum"),
                    ),
                    review_subject_hash="0" * 64,
                    review_state=ReviewState.MANUAL_REQUIRED if manual else ReviewState.PASSED,
                    alt_text=record.alt_text,
                    **storage_fields,
                    **rights,
                )
                evidence.save()
                evidence.review_subject_hash = calculate_review_subject_hash(evidence)
                evidence.publishable = calculate_publishable(evidence)
                evidence.full_clean()
                evidence.save(update_fields=("review_subject_hash", "publishable", "updated_at"))
                evidence_assets.append(evidence)

            evidence = evidence_assets[0]
            attempt.evidence_asset = evidence
            attempt.expected_evidence_count = len(evidence_assets)
            attempt.expected_evidence_manifest_hash = generic_evidence_manifest_hash(
                evidence_assets
            )
            attempt.state = (
                ExtractionState.LOW_CONFIDENCE if reasons else ExtractionState.SUCCEEDED
            )
            attempt.finished_at = timezone.now()
            attempt.next_retry_at = None
            attempt.lease_owner = ""
            attempt.lease_token = None
            attempt.terminal_event_key = ready_event_key
            attempt.terminal_state = "ready"
            attempt.full_clean()
            attempt.save()
            bind_extraction_object_write(result_reservation_id)
            if reason_reservation_id is not None:
                bind_extraction_object_write(reason_reservation_id)
            if legacy_info:
                bind_extraction_object_write(legacy_info[4])

            enqueue_event(
                topic="evidence.other_ready",
                aggregate_type="GenericExtractionAttempt",
                aggregate_id=attempt.id,
                message_key=ready_event_key,
                payload={
                    "run_id": str(attempt.run_source_item.run_id),
                    "run_source_item_id": str(attempt.run_source_item_id),
                    "source_item_id": str(attempt.source_item_id),
                    "generic_extraction_attempt_id": str(attempt.id),
                    "evidence_asset_id": str(evidence.id),
                    "evidence_count": attempt.expected_evidence_count,
                    "evidence_manifest_hash": attempt.expected_evidence_manifest_hash,
                    "engine": attempt.engine,
                    "locator_type": evidence.locator_type,
                    "validation_mode": attempt.validation_mode,
                    "result_checksum": attempt.result_checksum,
                    "low_confidence_reasons_hash": attempt.low_confidence_reasons_hash,
                    "calibration_profile_key": attempt.calibration_profile_key,
                    "calibration_profile_version": attempt.calibration_profile_version,
                    "calibration_profile_hash": attempt.calibration_profile_hash,
                    "extraction_fingerprint": attempt.extraction_fingerprint,
                    "input_asset_id": (
                        str(attempt.input_asset_id)
                        if attempt.input_asset_id else None
                    ),
                    "parent_asset_id": (
                        str(evidence.parent_asset_id)
                        if evidence.parent_asset_id else None
                    ),
                    "profile_snapshot_id": str(
                        attempt.extraction_profile_snapshot_id
                    ),
                    "profile_material_hash": attempt.profile_material_hash,
                    "extractor_version": attempt.extractor_version,
                    "config_hash": attempt.config_hash,
                    "evidence_content_hash": evidence.evidence_content_hash,
                    "review_subject_hash": evidence.review_subject_hash,
                    "locator_hash": canonical_hash(evidence.locator),
                },
            )

            if legacy_info:
                info, converted_checksum, _, converted_page_count, _ = legacy_info
                document = _get_or_create_document_extraction(
                    run_source_item=attempt.run_source_item,
                    source_item=attempt.source_item,
                    input_asset=evidence,
                    input_object_key=info.key,
                    input_object_version=_required_object_version(info),
                    input_kind=DocumentInputKind.PDF,
                    input_mime_type="application/pdf",
                    input_checksum=converted_checksum,
                    input_page_count=converted_page_count,
                    expected_page_indices=list(range(converted_page_count)),
                    page_identity_authoritative=True,
                )
                _enqueue_document_extraction(document)
    return attempt


def _process_attachment(
    run_source_item,
    attachment: Mapping[str, Any],
    *,
    fanout_fence: Mapping[str, Any],
) -> None:
    if _metadata_only_attachment(run_source_item, attachment):
        _persist_metadata_only_attachment(
            run_source_item,
            attachment,
            fanout_fence=fanout_fence,
        )
        return
    data, declared_mime, filename = _download_attachment(run_source_item, attachment)
    with tempfile.TemporaryDirectory(prefix="wisdome-sniff-") as temp_dir:
        temp_path = Path(temp_dir) / filename
        temp_path.write_bytes(data)
        sniffed = sniff_mime(temp_path)
    mime_type = _approved_attachment_mime(
        run_source_item,
        declared_mime=declared_mime,
        sniffed_mime=sniffed,
    )
    raw_asset, info = _persist_attachment(
        run_source_item,
        attachment,
        data,
        mime_type,
        filename,
        fanout_fence=fanout_fence,
    )
    suffix = Path(filename).suffix.lower()
    if mime_type == "application/pdf":
        with transaction.atomic():
            _lock_expected_fanout(run_source_item.run_id, fanout_fence)
            document = _get_or_create_document_extraction(
                run_source_item=run_source_item,
                source_item=run_source_item.source_item,
                input_asset=raw_asset,
                input_object_key=info.key,
                input_object_version=_required_object_version(info),
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
            _lock_expected_fanout(run_source_item.run_id, fanout_fence)
            document = _get_or_create_document_extraction(
                run_source_item=run_source_item,
                source_item=run_source_item.source_item,
                input_asset=raw_asset,
                input_object_key=info.key,
                input_object_version=_required_object_version(info),
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
    if suffix == ".hwpx" and mime_type == "application/hwp+zip":
        engine, preferred = ExtractionEngine.HWPX, "hwpx-deterministic-v1"
    elif (
        suffix == ".hwp"
        and mime_type == "application/haansofthwp"
    ):
        engine, preferred = ExtractionEngine.LEGACY_HWP, "legacy-hwp-v1"
    elif (
        suffix in {".xlsx", ".xlsm"}
        and mime_type
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ) or (
        suffix == ".xls"
        and mime_type == "application/vnd.ms-excel"
    ) or (
        suffix in {".csv", ".tsv"}
        and mime_type in {"text/csv", "text/tab-separated-values"}
    ):
        engine, preferred = ExtractionEngine.SPREADSHEET, "spreadsheet-deterministic-v1"
    elif (
        suffix in {".html", ".htm"}
        and mime_type in {"text/html", "application/xhtml+xml"}
    ):
        engine, preferred = ExtractionEngine.HTML, "html-deterministic-v1"
    elif (
        suffix in {".json", ".jsonld"}
        and mime_type in {"application/json", "application/ld+json"}
    ) or (
        suffix in {".xml", ".rss", ".atom"}
        and mime_type
        in {
            "application/xml",
            "text/xml",
            "application/rss+xml",
            "application/atom+xml",
        }
    ):
        engine, preferred = ExtractionEngine.STRUCTURED, "structured-deterministic-v1"
    if not engine:
        return
    with transaction.atomic():
        _lock_expected_fanout(run_source_item.run_id, fanout_fence)
        attempt = _load_or_create_generic_attempt(
            run_source_item=run_source_item,
            source_item=run_source_item.source_item,
            input_asset=raw_asset,
            engine=engine,
            preferred_key=preferred,
            fanout_fence=fanout_fence,
        )
        _enqueue_generic_extraction(attempt)


def _load_or_create_generic_attempt(
    *,
    run_source_item,
    source_item,
    input_asset: EvidenceAsset,
    engine: str,
    preferred_key: str | None,
    fanout_fence: Mapping[str, Any] | None,
) -> GenericExtractionAttempt:
    existing = list(
        GenericExtractionAttempt.objects.select_for_update()
        .filter(
            run_source_item=run_source_item,
            input_asset=input_asset,
            engine=engine,
        )
        .order_by("created_at", "id")[:2]
    )
    if len(existing) > 1:
        raise EvidenceConflict("Generic extraction lineage has multiple frozen attempts")
    if existing:
        attempt = existing[0]
        if attempt.source_item_id != source_item.id:
            raise EvidenceConflict("Generic extraction source lineage changed")
        return attempt
    if fanout_fence is None:
        raise EvidenceConflict("New generic attempts require an owned fanout lease")
    _lock_expected_fanout(run_source_item.run_id, fanout_fence)
    try:
        profile = _profile(engine, preferred_key=preferred_key)
    except ExtractorError:
        if engine == ExtractionEngine.LEGACY_HWP:
            _quarantine_legacy_hwp_raw_input(input_asset)
        raise
    fingerprint = generic_extraction_fingerprint(
        run_source_item_id=run_source_item.id,
        source_item_id=source_item.id,
        input_asset_id=input_asset.id,
        input_checksum=input_asset.checksum,
        profile=profile,
    )
    attempt, _ = GenericExtractionAttempt.objects.get_or_create(
        run_source_item=run_source_item,
        extraction_fingerprint=fingerprint,
        defaults={
            "source_item": source_item,
            "input_asset": input_asset,
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
    return attempt


def _terminalize_generic_extraction(
    attempt_id: Any,
    *,
    error_code: str,
    error_detail_redacted: str,
    expected_generation: int | None = None,
    expected_source_event_id: uuid.UUID | str | None = None,
    expected_lease_owner: str | None = None,
    expected_lease_token: uuid.UUID | None = None,
) -> dict[str, str]:
    run_id = GenericExtractionAttempt.objects.values_list(
        "run_source_item__run_id", flat=True
    ).get(pk=attempt_id)
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        RunStep.objects.select_for_update().filter(
            run_id=run_id, name="extract", attempt_no=1
        ).first()
        attempt = (
            GenericExtractionAttempt.objects.select_for_update()
            .select_related("run_source_item", "input_asset__parent_asset")
            .get(pk=attempt_id)
        )
        if attempt.state in {
            ExtractionState.SUCCEEDED,
            ExtractionState.LOW_CONFIDENCE,
        } or attempt.terminal_state == "ready":
            return {
                "genericExtractionAttemptId": str(attempt.id),
                "state": attempt.state,
            }
        if attempt.terminal_event_key:
            return {
                "genericExtractionAttemptId": str(attempt.id),
                "state": attempt.state,
            }
        if expected_generation is not None:
            run_accepts_terminal = (
                run.state == RunState.EXTRACTING
                and run.stop_requested_at is None
            )
            same_source = (
                attempt.source_event_id is not None
                and str(attempt.source_event_id) == str(expected_source_event_id)
            )
            owned_running = (
                run_accepts_terminal
                and attempt.state == ExtractionState.RUNNING
                and same_source
                and attempt.lease_generation == expected_generation
                and attempt.lease_owner == expected_lease_owner
                and attempt.lease_token == expected_lease_token
            )
            released_retry = (
                run_accepts_terminal
                and attempt.state == ExtractionState.QUEUED
                and same_source
                and attempt.lease_owner == ""
                and attempt.lease_token is None
            )
            virgin_delivery = (
                run_accepts_terminal
                and attempt.state == ExtractionState.QUEUED
                and attempt.source_event_id is None
                and attempt.lease_generation == 0
                and attempt.lease_owner == ""
                and attempt.lease_token is None
            )
            if virgin_delivery:
                attempt.source_event_id = expected_source_event_id
                attempt.lease_generation = expected_generation
                attempt.delivery_count = expected_generation
            if not (owned_running or released_retry or virgin_delivery):
                return {
                    "genericExtractionAttemptId": str(attempt.id),
                    "state": attempt.state,
                }
        attempt.state = ExtractionState.FAILED
        attempt.error_code = error_code[:120]
        attempt.error_detail_redacted = error_detail_redacted[:1000]
        attempt.finished_at = timezone.now()
        attempt.next_retry_at = None
        attempt.lease_owner = ""
        attempt.lease_token = None
        cause = f"generic-terminal:{attempt.id}:generation:{attempt.lease_generation}"
        attempt.terminal_event_key = (
            f"evidence.finalize_requested:{run_id}:{cause}"
        )
        attempt.terminal_state = "failed"
        if attempt.engine == ExtractionEngine.LEGACY_HWP:
            _quarantine_legacy_hwp_raw_input(attempt.input_asset)
        attempt.save(
            update_fields=(
                "state",
                "source_event_id",
                "lease_generation",
                "delivery_count",
                "error_code",
                "error_detail_redacted",
                "finished_at",
                "next_retry_at",
                "lease_owner",
                "lease_token",
                "terminal_event_key",
                "terminal_state",
                "updated_at",
            )
        )
        orphan_unbound_extraction_object_writes(
            aggregate_kind="generic",
            aggregate_id=attempt.id,
            lease_generation=attempt.lease_generation,
        )
        _enqueue_finalize(str(run_id), cause)
        return {
            "genericExtractionAttemptId": str(attempt.id),
            "state": attempt.state,
        }


def _terminalize_document_extraction(
    document_id: Any,
    *,
    error_code: str,
    error_detail_redacted: str,
    expected_generation: int | None = None,
    expected_source_event_id: uuid.UUID | str | None = None,
    expected_lease_owner: str | None = None,
    expected_lease_token: uuid.UUID | None = None,
) -> dict[str, str]:
    run_id = DocumentExtraction.objects.values_list(
        "run_source_item__run_id", flat=True
    ).get(pk=document_id)
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        RunStep.objects.select_for_update().filter(
            run_id=run_id, name="extract", attempt_no=1
        ).first()
        document = (
            DocumentExtraction.objects.select_for_update()
            .select_related("run_source_item")
            .get(pk=document_id)
        )
        child_runs = list(
            ExtractionRun.objects.select_for_update()
            .filter(document_extraction=document)
            .order_by("id")
        )
        if document.state == ExtractionState.SUCCEEDED or document.terminal_state == "ready":
            return {
                "documentExtractionId": str(document.id),
                "state": document.state,
            }
        if document.terminal_event_key:
            return {
                "documentExtractionId": str(document.id),
                "state": document.state,
            }
        if expected_generation is not None:
            run_accepts_terminal = (
                run.state == RunState.EXTRACTING
                and run.stop_requested_at is None
            )
            same_source = (
                document.source_event_id is not None
                and str(document.source_event_id) == str(expected_source_event_id)
            )
            owned_running = (
                run_accepts_terminal
                and document.state == ExtractionState.RUNNING
                and same_source
                and document.lease_generation == expected_generation
                and document.lease_owner == expected_lease_owner
                and document.lease_token == expected_lease_token
            )
            released_retry = (
                run_accepts_terminal
                and document.state == ExtractionState.QUEUED
                and same_source
                and document.lease_owner == ""
                and document.lease_token is None
            )
            virgin_delivery = (
                run_accepts_terminal
                and document.state == ExtractionState.QUEUED
                and document.source_event_id is None
                and document.lease_generation == 0
                and document.lease_owner == ""
                and document.lease_token is None
            )
            if virgin_delivery:
                document.source_event_id = expected_source_event_id
                document.lease_generation = expected_generation
                document.delivery_count = expected_generation
                document.save(
                    update_fields=(
                        "source_event_id",
                        "lease_generation",
                        "delivery_count",
                        "updated_at",
                    )
                )
            if not (owned_running or released_retry or virgin_delivery):
                return {
                    "documentExtractionId": str(document.id),
                    "state": document.state,
                }
        finished_at = timezone.now()
        for child in child_runs:
            if child.state in (ExtractionState.QUEUED, ExtractionState.RUNNING):
                child.parent_lease_generation = document.lease_generation
                child.source_event_id = document.source_event_id
                child.state = ExtractionState.FAILED
                child.error_code = error_code[:120]
                child.error_detail_redacted = error_detail_redacted[:1000]
                child.finished_at = finished_at
                child.lease_owner = ""
                child.lease_token = None
                child.terminal_state = "failed"
                child.terminal_event_key = (
                    f"extraction.run.failed:{child.id}:generation:"
                    f"{child.lease_generation}"
                )
                child.save()
                orphan_unbound_extraction_object_writes(
                    aggregate_kind="extraction_run",
                    aggregate_id=child.id,
                    lease_generation=child.lease_generation,
                )
        document.state = ExtractionState.FAILED
        document.document_complete = False
        document.error_code = error_code[:120]
        document.error_detail_redacted = error_detail_redacted[:1000]
        document.finished_at = finished_at
        document.next_retry_at = None
        document.lease_owner = ""
        document.lease_token = None
        cause = f"document-terminal:{document.id}:generation:{document.lease_generation}"
        document.terminal_event_key = (
            f"evidence.finalize_requested:{run_id}:{cause}"
        )
        document.terminal_state = "failed"
        document.save()
        orphan_unbound_extraction_object_writes(
            aggregate_kind="document",
            aggregate_id=document.id,
            lease_generation=document.lease_generation,
        )
        _enqueue_finalize(str(run_id), cause)
        return {
            "documentExtractionId": str(document.id),
            "state": document.state,
        }


@shared_task
def finalize_document_extraction_failure(document_id: str, error_code: str):
    delivery = _current_extraction_delivery(document_id)
    return _terminalize_document_extraction(
        document_id,
        error_code=error_code,
        error_detail_redacted="Document extraction delivery exhausted",
        expected_generation=delivery["lease_generation"],
        expected_source_event_id=delivery["source_event_id"],
        expected_lease_owner=delivery["lease_owner"],
        expected_lease_token=delivery["lease_token"],
    )


@shared_task
def finalize_generic_extraction_failure(attempt_id: str, error_code: str):
    delivery = _current_extraction_delivery(attempt_id)
    return _terminalize_generic_extraction(
        attempt_id,
        error_code=error_code,
        error_detail_redacted="Generic extraction delivery exhausted",
        expected_generation=delivery["lease_generation"],
        expected_source_event_id=delivery["source_event_id"],
        expected_lease_owner=delivery["lease_owner"],
        expected_lease_token=delivery["lease_token"],
    )


@shared_task
def process_document_extraction(document_id: str):
    delivery = _current_extraction_delivery(document_id)
    try:
        initial = DocumentExtraction.objects.select_related(
            "input_asset__generic_extraction_attempt__input_asset",
            "input_asset__producing_generic_attempt__input_asset",
            "input_asset__parent_asset",
        ).get(pk=document_id)
        if _is_audit_only_document(initial):
            return {
                "documentExtractionId": str(initial.id),
                "state": "legacy_duplicate_audit_only",
            }
        claimed = begin_document_extraction(document_id, **delivery)
        if claimed is None:
            document = DocumentExtraction.objects.get(pk=document_id)
            return {"documentExtractionId": str(document.id), "state": document.state}
        document = _run_document_extraction(
            document_id,
            expected_generation=claimed.lease_generation,
            expected_lease_owner=claimed.lease_owner,
            expected_lease_token=claimed.lease_token,
        )
        return {"documentExtractionId": str(document.id), "state": document.state}
    except EvidenceExtractionStopped:
        run_id = DocumentExtraction.objects.values_list(
            "run_source_item__run_id", flat=True
        ).get(pk=document_id)
        _terminalize_stopped_run_evidence(run_id)
        document = DocumentExtraction.objects.get(pk=document_id)
        return {"documentExtractionId": str(document.id), "state": document.state}
    except EvidenceConflict as exc:
        raise PermanentEventError(
            "extraction_source_event_conflict",
            "Extraction aggregate source event did not match",
        ) from exc
    except (DatabaseError, SoftTimeLimitExceeded, OSError, httpx.HTTPError):
        raise
    except ExtractorError as exc:
        if exc.retryable:
            queue_document_extraction_retry(
                document_id,
                expected_generation=claimed.lease_generation,
                expected_lease_owner=claimed.lease_owner,
                expected_lease_token=claimed.lease_token,
                retry_at=timezone.now(),
                error_code=exc.code,
                error_detail_redacted=exc.detail_redacted,
            )
            raise
        raise PermanentEventError(exc.code, exc.detail_redacted) from exc
    except Exception as exc:
        raise PermanentEventError(
            "unexpected_extraction_failure",
            "Unexpected extraction failure",
        ) from exc


@shared_task
def process_paddleocr_document(document_id: str):
    """Dedicated queue entry point; page routing still comes from the parent aggregate."""
    return process_document_extraction.run(document_id)


@shared_task
def process_generic_extraction(attempt_id: str):
    delivery = _current_extraction_delivery(attempt_id)
    try:
        initial = GenericExtractionAttempt.objects.select_related(
            "input_asset__parent_asset"
        ).get(pk=attempt_id)
        if _is_quarantined_generic_attempt(initial):
            return {
                "genericExtractionAttemptId": str(initial.id),
                "state": "legacy_duplicate_audit_only",
            }
        claimed = begin_generic_extraction(attempt_id, **delivery)
        if claimed is None:
            attempt = GenericExtractionAttempt.objects.select_related(
                "run_source_item__run"
            ).get(pk=attempt_id)
            if (
                attempt.engine == ExtractionEngine.LEGACY_HWP
                and attempt.state
                in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE)
                and attempt.run_source_item.run.state == RunState.EXTRACTING
                and attempt.run_source_item.run.stop_requested_at is None
            ):
                attempt = _ensure_legacy_hwp_document(attempt.id)
            return {
                "genericExtractionAttemptId": str(attempt.id),
                "state": attempt.state,
            }
        attempt = _run_generic_extraction(
            attempt_id,
            expected_generation=claimed.lease_generation,
            expected_lease_owner=claimed.lease_owner,
            expected_lease_token=claimed.lease_token,
        )
        return {"genericExtractionAttemptId": str(attempt.id), "state": attempt.state}
    except EvidenceExtractionStopped:
        run_id = GenericExtractionAttempt.objects.values_list(
            "run_source_item__run_id", flat=True
        ).get(pk=attempt_id)
        _terminalize_stopped_run_evidence(run_id)
        attempt = GenericExtractionAttempt.objects.get(pk=attempt_id)
        return {"genericExtractionAttemptId": str(attempt.id), "state": attempt.state}
    except EvidenceConflict as exc:
        raise PermanentEventError(
            "extraction_source_event_conflict",
            "Extraction aggregate source event did not match",
        ) from exc
    except (DatabaseError, SoftTimeLimitExceeded, OSError, httpx.HTTPError):
        raise
    except ExtractorError as exc:
        if exc.retryable:
            retry_at = timezone.now()
            queue_generic_extraction_retry(
                attempt_id,
                expected_generation=claimed.lease_generation,
                expected_lease_owner=claimed.lease_owner,
                expected_lease_token=claimed.lease_token,
                retry_at=retry_at,
                error_code=exc.code,
                error_detail_redacted=exc.detail_redacted,
            )
            raise
        raise PermanentEventError(exc.code, exc.detail_redacted) from exc
    except Exception as exc:
        raise PermanentEventError(
            "unexpected_extraction_failure",
            "Unexpected extraction failure",
        ) from exc


def _ready_identity_mismatch(kind: str) -> PermanentEventError:
    return PermanentEventError(
        f"{kind}_ready_identity_mismatch",
        "Ready event identity no longer matches the locked extraction aggregate",
    )


@shared_task(name="apps.evidence.tasks.consume_document_ready")
def consume_document_ready(
    run_id: str,
    run_source_item_id: str,
    source_item_id: str,
    document_extraction_id: str,
    input_page_count: int,
    coverage_manifest_hash: str,
    selected_evidence_manifest_hash: str,
    document_complete: bool,
    input_asset_id: str | None = None,
    input_checksum: str | None = None,
    input_fingerprint: str | None = None,
    routing_manifest_hash: str | None = None,
):
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        document = (
            DocumentExtraction.objects.select_for_update()
            .select_related("run_source_item")
            .get(pk=document_extraction_id)
        )
        document = aggregate_document_extraction(document.id)
        expected_ready_key = (
            f"evidence.document_ready:{document.id}:"
            f"{document.selected_evidence_manifest_hash}"
        )
        if (
            str(document.run_source_item.run_id) != str(run.id)
            or str(document.run_source_item_id) != run_source_item_id
            or str(document.source_item_id) != source_item_id
            or document.state != ExtractionState.SUCCEEDED
            or document.input_page_count != input_page_count
            or document.coverage_manifest_hash != coverage_manifest_hash
            or document.selected_evidence_manifest_hash
            != selected_evidence_manifest_hash
            or document.document_complete is not document_complete
            or document_complete is not True
            or document.terminal_state != "ready"
            or document.terminal_event_key != expected_ready_key
            or (
                input_asset_id is not None
                and str(document.input_asset_id) != input_asset_id
            )
            or (
                input_checksum is not None
                and document.input_checksum != input_checksum
            )
            or (
                input_fingerprint is not None
                and document.input_fingerprint != input_fingerprint
            )
            or (
                routing_manifest_hash is not None
                and canonical_hash(document.routing_manifest)
                != routing_manifest_hash
            )
        ):
            raise _ready_identity_mismatch("document")
        return finalize_run_evidence.run(str(run.id))


@shared_task(name="apps.evidence.tasks.consume_other_ready")
def consume_other_ready(
    run_id: str,
    run_source_item_id: str,
    source_item_id: str,
    evidence_asset_id: str,
    generic_extraction_attempt_id: str,
    engine: str,
    locator_type: str,
    validation_mode: str,
    result_checksum: str,
    low_confidence_reasons_hash: str | None,
    calibration_profile_key: str | None,
    calibration_profile_version: str | None,
    calibration_profile_hash: str | None,
    extraction_fingerprint: str,
    input_asset_id: str | None = None,
    parent_asset_id: str | None = None,
    profile_snapshot_id: str | None = None,
    profile_material_hash: str | None = None,
    extractor_version: str | None = None,
    config_hash: str | None = None,
    evidence_content_hash: str | None = None,
    review_subject_hash: str | None = None,
    locator_hash: str | None = None,
    evidence_asset_ids: list[str] | None = None,
    evidence_count: int | None = None,
    evidence_manifest_hash: str | None = None,
):
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        attempt = (
            GenericExtractionAttempt.objects.select_for_update(of=("self",))
            .select_related(
                "run_source_item",
                "evidence_asset",
                "input_asset",
                "extraction_profile_snapshot",
            )
            .get(pk=generic_extraction_attempt_id)
        )
        evidence_assets = list(
            EvidenceAsset.objects.select_for_update(of=("self",))
            .filter(generic_extraction_attempt=attempt)
            .select_related(
                "origin_run_source_item__source_snapshot",
                "generic_extraction_attempt__extraction_profile_snapshot",
                "generic_extraction_attempt__input_asset__parent_asset",
                "parent_asset",
                "latest_review_decision",
            )
            .order_by("id")
        )
        evidence_by_id = {str(item.id): item for item in evidence_assets}
        evidence = evidence_by_id.get(str(evidence_asset_id))
        manifest_extension_supplied = (
            evidence_count is not None or evidence_manifest_hash is not None
        )
        manifest_extension_incomplete = (
            (evidence_count is None) != (evidence_manifest_hash is None)
        )
        legacy_missing_manifest = (
            not manifest_extension_supplied
            and evidence_asset_ids is None
            and attempt.expected_evidence_count is None
            and attempt.expected_evidence_manifest_hash is None
        )
        try:
            observed_ids, observed_manifest_hash = validate_generic_evidence_set(
                attempt,
                evidence_assets,
                allow_legacy_missing_manifest=legacy_missing_manifest,
            )
            evidence_set_invalid = False
        except ValidationError:
            observed_ids, observed_manifest_hash = [], None
            evidence_set_invalid = True
        expected_ready_key = f"evidence.other_ready:{attempt.id}:{attempt.result_checksum}"
        if (
            str(attempt.run_source_item.run_id) != str(run.id)
            or str(attempt.run_source_item_id) != run_source_item_id
            or str(attempt.source_item_id) != source_item_id
            or attempt.state
            not in {ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE}
            or evidence is None
            or attempt.evidence_asset_id != evidence.id
            or evidence.generic_extraction_attempt_id != attempt.id
            or evidence.source_item_id != attempt.source_item_id
            or evidence.origin_run_source_item_id != attempt.run_source_item_id
            or evidence.parent_asset_id != attempt.input_asset_id
            or attempt.engine != engine
            or evidence.locator_type != locator_type
            or evidence.extraction_method != attempt.engine
            or evidence.extractor_version != attempt.extractor_version
            or evidence.extraction_config_hash != attempt.config_hash
            or evidence.validation_mode != attempt.validation_mode
            or attempt.validation_mode != validation_mode
            or attempt.result_checksum != result_checksum
            or evidence.extraction_result_checksum != result_checksum
            or attempt.low_confidence_reasons_hash != low_confidence_reasons_hash
            or attempt.calibration_profile_key != calibration_profile_key
            or attempt.calibration_profile_version != calibration_profile_version
            or attempt.calibration_profile_hash != calibration_profile_hash
            or attempt.extraction_fingerprint != extraction_fingerprint
            or attempt.terminal_state != "ready"
            or attempt.terminal_event_key != expected_ready_key
            or evidence_set_invalid
            or manifest_extension_incomplete
            or (
                manifest_extension_supplied
                and (
                    evidence_count is None
                    or evidence_manifest_hash is None
                    or evidence_count != len(evidence_assets)
                    or evidence_count != attempt.expected_evidence_count
                    or evidence_manifest_hash != observed_manifest_hash
                    or evidence_manifest_hash
                    != attempt.expected_evidence_manifest_hash
                )
            )
            or (
                not manifest_extension_supplied
                and (
                    len(evidence_assets) != 1
                    or attempt.evidence_asset_id != evidence.id
                )
            )
            or (
                evidence_asset_ids is not None
                and (
                    not all(isinstance(item, str) for item in evidence_asset_ids)
                    or evidence_asset_ids != sorted(set(evidence_asset_ids))
                    or evidence_asset_ids != observed_ids
                )
            )
            or (
                attempt.engine == ExtractionEngine.LEGACY_HWP
                and not _has_complete_legacy_hwp_pdf(
                    evidence,
                    expected_attempt=attempt,
                )
            )
            or (
                input_asset_id is not None
                and str(attempt.input_asset_id) != input_asset_id
            )
            or (
                parent_asset_id is not None
                and str(evidence.parent_asset_id) != parent_asset_id
            )
            or (
                profile_snapshot_id is not None
                and str(attempt.extraction_profile_snapshot_id)
                != profile_snapshot_id
            )
            or (
                profile_material_hash is not None
                and attempt.profile_material_hash != profile_material_hash
            )
            or (
                extractor_version is not None
                and attempt.extractor_version != extractor_version
            )
            or (config_hash is not None and attempt.config_hash != config_hash)
            or (
                evidence_content_hash is not None
                and evidence.evidence_content_hash != evidence_content_hash
            )
            or (
                review_subject_hash is not None
                and evidence.review_subject_hash != review_subject_hash
            )
            or (
                locator_hash is not None
                and canonical_hash(evidence.locator) != locator_hash
            )
        ):
            raise _ready_identity_mismatch("other")
        return finalize_run_evidence.run(str(run.id))


def _fail_generic_evidence_manifest_locked(
    run: CollectionRun,
    step: RunStep,
    *,
    documents: Sequence[DocumentExtraction] | None = None,
    attempts: Sequence[GenericExtractionAttempt] | None = None,
    children: Sequence[ExtractionRun] | None = None,
) -> dict[str, Any]:
    now = timezone.now()
    error_code = "generic_evidence_manifest_invalid"
    _close_active_extraction_aggregates_locked(
        run,
        error_code=error_code,
        error_detail_redacted=(
            "Generic evidence material differs from its frozen manifest"
        ),
        finished_at=now,
        documents=documents,
        attempts=attempts,
        children=children,
        enqueue_finalizer=False,
    )
    orphan_unbound_extraction_object_writes(
        aggregate_kind="fanout",
        aggregate_id=run.id,
        lease_generation=step.lease_generation,
    )
    step.state = "failed"
    step.error_code = error_code
    step.error_detail_redacted = "Generic evidence material differs from its frozen manifest"
    step.finished_at = now
    step.lease_owner = ""
    step.lease_token = None
    _project_domain_step_terminal(
        step,
        run,
        finished_at=now,
        final_state=step.state,
        affected_count=1,
        error_code=error_code,
        recovery_state=RecoveryState.MANUAL_REQUIRED,
    )
    step.save()
    run.state = RunState.FAILED
    run.completed_at = now
    run.error_summary = {"stage": "extract", "code": error_code}
    project_run_terminal_observation(
        run,
        finished_at=now,
        stage="extract",
        final_state=run.state,
        affected_count=1,
        error_code=error_code,
        recovery_state=RecoveryState.MANUAL_REQUIRED,
    )
    run.save()
    schedule_queue_one_release(run)
    return {
        "runId": str(run.id),
        "state": run.state,
        "code": error_code,
        "failures": 1,
    }


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
        documents = list(
            DocumentExtraction.objects.select_for_update(of=("self",))
            .filter(run_source_item__run=run)
            .select_related(
                "input_asset__generic_extraction_attempt__input_asset__parent_asset",
                "input_asset__producing_generic_attempt__input_asset__parent_asset",
                "input_asset__parent_asset",
            )
            .order_by("id")
        )
        attempts = list(
            GenericExtractionAttempt.objects.select_for_update(of=("self",))
            .filter(run_source_item__run=run)
            .select_related(
                "input_asset__parent_asset",
                "extraction_profile_snapshot",
                "run_source_item__source_snapshot",
            )
            .order_by("id")
        )
        children = list(
            ExtractionRun.objects.select_for_update()
            .filter(document_extraction__run_source_item__run=run)
            .order_by("document_extraction_id", "id")
        )
        locked_evidence = list(
            EvidenceAsset.objects.select_for_update(of=("self",))
            .filter(origin_run_source_item__run=run)
            .select_related(
                "origin_run_source_item__source_snapshot",
                "generic_extraction_attempt__extraction_profile_snapshot",
                "generic_extraction_attempt__input_asset__parent_asset",
                "parent_asset",
                "latest_review_decision",
            )
            .order_by("id")
        )
        orphan_unbound_extraction_object_writes(
            aggregate_kind="fanout",
            aggregate_id=run.id,
            lease_generation=step.lease_generation,
        )
        generic_evidence_by_attempt: dict[Any, list[EvidenceAsset]] = {}
        for evidence in locked_evidence:
            if evidence.generic_extraction_attempt_id is not None:
                generic_evidence_by_attempt.setdefault(
                    evidence.generic_extraction_attempt_id, []
                ).append(evidence)
        try:
            for attempt in attempts:
                if attempt.state not in {
                    ExtractionState.SUCCEEDED,
                    ExtractionState.LOW_CONFIDENCE,
                } or _is_quarantined_generic_attempt(attempt):
                    continue
                attempt_evidence = generic_evidence_by_attempt.get(attempt.id, [])
                validate_generic_evidence_set(attempt, attempt_evidence)
                if attempt.engine == ExtractionEngine.LEGACY_HWP and not (
                    len(attempt_evidence) == 1
                    and _has_complete_legacy_hwp_pdf(
                        attempt_evidence[0],
                        expected_attempt=attempt,
                    )
                ):
                    raise ValidationError("Legacy HWP converted PDF material is incomplete")
        except (ValidationError, ExtractorError):
            return _fail_generic_evidence_manifest_locked(
                run,
                step,
                documents=documents,
                attempts=attempts,
                children=children,
            )
        pending_documents = any(
            document.input_fingerprint is not None
            and document.state in (ExtractionState.QUEUED, ExtractionState.RUNNING)
            and not _is_audit_only_document(document)
            for document in documents
        )
        pending_generic = any(
            attempt.state in (ExtractionState.QUEUED, ExtractionState.RUNNING)
            and not _is_quarantined_generic_attempt(attempt)
            for attempt in attempts
        )
        pending_children = any(
            child.state in (ExtractionState.QUEUED, ExtractionState.RUNNING)
            for child in children
        )
        if pending_documents or pending_generic or pending_children:
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
        generic_failure_attempts = GenericExtractionAttempt.objects.filter(
            run_source_item__run=run, state=ExtractionState.FAILED
        ).exclude(
            input_asset__derivation_type=EvidenceDerivationType.RAW,
            input_asset__raw_input_fingerprint__isnull=True,
        ).exclude(
            input_asset__parent_asset__derivation_type=(
                EvidenceDerivationType.RAW
            ),
            input_asset__parent_asset__raw_input_fingerprint__isnull=True,
        )
        pre_attempt_legacy_hwp_failures = int(
            (run.counters or {}).get("legacyHwpRequiredFailures", 0)
        )
        required_legacy_hwp_failure = (
            pre_attempt_legacy_hwp_failures > 0
            or generic_failure_attempts.filter(engine=ExtractionEngine.LEGACY_HWP).exists()
        )
        generic_failures = generic_failure_attempts.count()
        failure_count = (
            document_failures
            + generic_failures
            + pre_attempt_legacy_hwp_failures
        )
        if step.input_count == 0:
            finished_at = timezone.now()
            step.output_count = 0
            step.error_code = None
            step.error_detail_redacted = None
            step.state = "succeeded"
            _project_domain_step_terminal(
                step,
                run,
                finished_at=finished_at,
                final_state=step.state,
                affected_count=0,
                error_code=None,
                recovery_state=RecoveryState.NOT_REQUIRED,
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
            run.state = RunState.COMPLETED
            run.completed_at = finished_at
            run.error_summary = None
            run.counters = {
                **run.counters,
                "evidence": 0,
                "extractionFailures": 0,
            }
            project_run_terminal_observation(
                run,
                finished_at=finished_at,
                stage="extract",
                final_state=run.state,
                affected_count=0,
                error_code=None,
                recovery_state=RecoveryState.NOT_REQUIRED,
            )
            run.save(
                update_fields=(
                    "state",
                    "completed_at",
                    "error_summary",
                    "counters",
                    "duration_ms",
                    "terminal_impact",
                    "recovery_state",
                    "next_recovery_at",
                )
            )
            schedule_queue_one_release(run)
            return {
                "runId": str(run.id),
                "state": run.state,
                "evidence": 0,
                "failures": 0,
                "noSourceChanges": True,
            }
        if missing_selected_evidence or required_legacy_hwp_failure:
            now = timezone.now()
            error_code = (
                "legacy_hwp_required_failure"
                if required_legacy_hwp_failure
                else "selected_run_evidence_missing"
            )
            step.output_count = evidence_count
            step.error_code = error_code
            step.error_detail_redacted = (
                "Required legacy HWP did not produce a verified PDF"
                if required_legacy_hwp_failure
                else "Selected successful extraction run has no evidence assets"
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
            schedule_queue_one_release(run)
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
            event_type="run.evidence_ready",
            aggregate_type="collection_run",
            aggregate_id=run.id,
            job_id=run.id,
            dedupe_key=f"run.evidence_ready:{run.id}",
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
        orphan_unbound_extraction_object_writes(
            aggregate_kind="fanout",
            aggregate_id=run.id,
            lease_generation=step.lease_generation,
        )
        step.state = "stopped" if stopped else "failed"
        step.lease_owner = ""
        step.lease_token = None
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
                "lease_owner",
                "lease_token",
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
            _close_active_extraction_aggregates_locked(
                run,
                error_code=redacted_code,
                error_detail_redacted="Evidence fanout delivery exhausted",
                finished_at=now,
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
        schedule_queue_one_release(run)
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
    _close_active_extraction_aggregates_locked(
        run,
        error_code="extraction_stopped",
        error_detail_redacted="Extraction stopped by collection request",
        finished_at=now,
    )
    orphan_unbound_extraction_object_writes(
        aggregate_kind="fanout",
        aggregate_id=run.id,
        lease_generation=step.lease_generation,
    )
    step.state = "stopped"
    step.lease_owner = ""
    step.lease_token = None
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
            "lease_owner",
            "lease_token",
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
    schedule_queue_one_release(run)
    return {"runId": str(run.id), "state": run.state}


def _close_active_extraction_aggregates_locked(
    run: CollectionRun,
    *,
    error_code: str,
    error_detail_redacted: str,
    finished_at,
    documents: Sequence[DocumentExtraction] | None = None,
    attempts: Sequence[GenericExtractionAttempt] | None = None,
    children: Sequence[ExtractionRun] | None = None,
    enqueue_finalizer: bool = True,
) -> None:
    documents = list(documents) if documents is not None else list(
        DocumentExtraction.objects.select_for_update()
        .filter(run_source_item__run=run)
        .order_by("id")
    )
    attempts = list(attempts) if attempts is not None else list(
        GenericExtractionAttempt.objects.select_for_update()
        .filter(run_source_item__run=run)
        .order_by("id")
    )
    children = list(children) if children is not None else list(
        ExtractionRun.objects.select_for_update()
        .filter(document_extraction__run_source_item__run=run)
        .order_by("document_extraction_id", "id")
    )
    for child in children:
        if child.state not in (ExtractionState.QUEUED, ExtractionState.RUNNING):
            continue
        child.state = ExtractionState.FAILED
        child.error_code = error_code[:120]
        child.error_detail_redacted = error_detail_redacted[:1000]
        child.finished_at = finished_at
        child.next_retry_at = None
        child.lease_owner = ""
        child.lease_token = None
        child.terminal_event_key = (
            f"extraction.run.failed:{child.id}:generation:{child.lease_generation}"
        )
        child.terminal_state = "failed"
        child.save()
        orphan_unbound_extraction_object_writes(
            aggregate_kind="extraction_run",
            aggregate_id=child.id,
            lease_generation=child.lease_generation,
        )
    for aggregate, aggregate_kind in (
        *((document, "document") for document in documents),
        *((attempt, "generic") for attempt in attempts),
    ):
        if aggregate.state not in (ExtractionState.QUEUED, ExtractionState.RUNNING):
            continue
        aggregate.state = ExtractionState.FAILED
        aggregate.error_code = error_code[:120]
        aggregate.error_detail_redacted = error_detail_redacted[:1000]
        aggregate.finished_at = finished_at
        aggregate.next_retry_at = None
        aggregate.lease_owner = ""
        aggregate.lease_token = None
        if isinstance(aggregate, DocumentExtraction):
            aggregate.document_complete = False
        terminal_cause = (
            f"stop:{aggregate_kind}:{aggregate.id}:generation:"
            f"{aggregate.lease_generation}"
        )
        aggregate.terminal_event_key = (
            f"evidence.finalize_requested:{run.id}:{terminal_cause}"
        )
        aggregate.terminal_state = "failed"
        aggregate.save()
        if enqueue_finalizer:
            _enqueue_finalize(str(run.id), terminal_cause)
        orphan_unbound_extraction_object_writes(
            aggregate_kind=aggregate_kind,
            aggregate_id=aggregate.id,
            lease_generation=aggregate.lease_generation,
        )


def _terminalize_stopped_run_evidence(
    run_id: Any,
    *,
    source_event_id: Any | None = None,
    expected_generation: int | None = None,
) -> dict[str, str]:
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        step, _ = RunStep.objects.select_for_update().get_or_create(
            run=run, name="extract", attempt_no=1
        )
        if source_event_id is not None and step.source_event_id not in (
            None,
            uuid.UUID(str(source_event_id)),
        ):
            return {"runId": str(run.id), "state": run.state, "stale": True}
        if expected_generation is not None and step.lease_generation not in (
            0,
            expected_generation,
        ):
            return {"runId": str(run.id), "state": run.state, "stale": True}
        return _stop_run_evidence_locked(run, step)


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
    step.lease_owner = ""
    step.lease_token = None
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
            "lease_owner",
            "lease_token",
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
    _close_active_extraction_aggregates_locked(
        run,
        error_code=redacted_code,
        error_detail_redacted=(
            "Evidence finalizer delivery exhausted before completion"
        ),
        finished_at=now,
    )
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
    schedule_queue_one_release(run)
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
    run_id = (
        DocumentExtraction.objects.filter(pk=document_id)
        .values_list("run_source_item__run_id", flat=True)
        .first()
    )
    if run_id is None:
        return {"documentExtractionId": document_id, "state": "missing"}
    with transaction.atomic():
        CollectionRun.objects.select_for_update().get(pk=run_id)
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
    run_id = (
        GenericExtractionAttempt.objects.filter(pk=attempt_id)
        .values_list("run_source_item__run_id", flat=True)
        .first()
    )
    if run_id is None:
        return {"genericExtractionAttemptId": attempt_id, "state": "missing"}
    with transaction.atomic():
        CollectionRun.objects.select_for_update().get(pk=run_id)
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
    delivery = _current_extraction_delivery(run_id)
    try:
        claimed = begin_evidence_fanout(run_id, **delivery)
    except EvidenceExtractionStopped:
        with transaction.atomic():
            run = CollectionRun.objects.select_for_update().get(pk=run_id)
            step, _ = RunStep.objects.select_for_update().get_or_create(
                run=run,
                name="extract",
                attempt_no=1,
            )
            return _stop_run_evidence_locked(run, step, output_count=0)
    if claimed is None:
        run = CollectionRun.objects.get(pk=run_id)
        step = RunStep.objects.filter(run=run, name="extract", attempt_no=1).first()
        return {
            "runId": str(run.id),
            "state": run.state,
            "fanoutComplete": bool(step and step.fanout_completed_at),
            "stale": bool(step and step.fanout_completed_at is None),
        }
    fanout_fence = {
        "expected_generation": claimed.lease_generation,
        "expected_lease_owner": claimed.lease_owner,
        "expected_lease_token": claimed.lease_token,
    }
    with evidence_fanout_completion_fence(run_id, **fanout_fence) as step:
        if step is None:
            run = CollectionRun.objects.get(pk=run_id)
            return {"runId": str(run.id), "state": run.state, "stale": True}
        run = step.run
        if run.state != RunState.EXTRACTING:
            return {"runId": str(run.id), "state": run.state}
        started_at = timezone.now()
        _begin_domain_step_observation(
            step,
            run,
            started_at=started_at,
        )
        step.input_count = run.run_source_items.filter(
            discovery_kind__in=EXTRACTABLE_SOURCE_DISCOVERY_KINDS,
            collection_attempt__state="succeeded",
        ).count()
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
    failures: list[dict[str, Any]] = []
    items = run.run_source_items.select_related(
        "source_item", "source_snapshot__source"
    ).filter(
        discovery_kind__in=EXTRACTABLE_SOURCE_DISCOVERY_KINDS,
        collection_attempt__state="succeeded",
    ).order_by("id")
    lost_fence = False
    for run_source_item in items:
        if CollectionRun.objects.filter(
            pk=run_id,
            stop_requested_at__isnull=False,
        ).exists():
            break
        try:
            with evidence_fanout_completion_fence(
                run_id, **fanout_fence
            ) as current_step:
                if current_step is None:
                    lost_fence = True
                    break
            _create_raw_evidence(
                run_source_item,
                fanout_fence=fanout_fence,
            )
            output_count += 1
            for attachment in run_source_item.source_item.attachments or []:
                try:
                    with evidence_fanout_completion_fence(
                        run_id, **fanout_fence
                    ) as current_step:
                        if current_step is None:
                            lost_fence = True
                            break
                    _process_attachment(
                        run_source_item,
                        attachment,
                        fanout_fence=fanout_fence,
                    )
                    output_count += 1
                except (ExtractorError, httpx.HTTPError, OSError) as exc:
                    if isinstance(exc, ExtractorError) and exc.retryable:
                        raise
                    failures.append({
                        "runSourceItemId": str(run_source_item.id),
                        "attachment": str(attachment.get("title", "attachment"))[:120],
                        "code": getattr(exc, "code", exc.__class__.__name__),
                        "requiredLegacyHwp": _is_required_legacy_hwp_attachment(attachment),
                    })
            if lost_fence:
                break
        except EvidenceConflict:
            lost_fence = True
            break
        except (DatabaseError, SoftTimeLimitExceeded):
            raise
        except (ExtractorError, httpx.HTTPError, OSError) as exc:
            if isinstance(exc, ExtractorError) and exc.retryable:
                raise
            failures.append({
                "runSourceItemId": str(run_source_item.id),
                "attachment": "source-record",
                "code": getattr(exc, "code", exc.__class__.__name__),
            })
    if lost_fence:
        run = CollectionRun.objects.get(pk=run_id)
        return {"runId": str(run.id), "state": run.state, "stale": True}
    if CollectionRun.objects.filter(
        pk=run_id, stop_requested_at__isnull=False
    ).exists():
        with transaction.atomic():
            run = CollectionRun.objects.select_for_update().get(pk=run_id)
            step = RunStep.objects.select_for_update().get(
                run=run,
                name="extract",
                attempt_no=1,
            )
            return _stop_run_evidence_locked(
                run,
                step,
                output_count=output_count,
            )
    with evidence_fanout_completion_fence(run_id, **fanout_fence) as step:
        if step is None:
            run = CollectionRun.objects.get(pk=run_id)
            return {"runId": str(run.id), "state": run.state, "stale": True}
        run = step.run
        if run.state != RunState.EXTRACTING:
            return {"runId": str(run.id), "state": run.state}
        step.output_count = output_count
        step.error_code = "partial_extraction_failure" if failures else None
        step.error_detail_redacted = (
            json.dumps(failures[:20], ensure_ascii=False)[:500]
            if failures
            else None
        )
        step.fanout_completed_at = timezone.now()
        step.state = "succeeded"
        step.lease_owner = ""
        step.lease_token = None
        step.save(
            update_fields=(
                "output_count",
                "error_code",
                "error_detail_redacted",
                "fanout_completed_at",
                "state",
                "lease_owner",
                "lease_token",
            )
        )
        run.counters = {
            **run.counters,
            "evidence": output_count,
            "extractionFailures": len(failures),
            **_legacy_hwp_failure_marker(failures),
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
