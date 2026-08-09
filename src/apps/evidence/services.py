from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from adapters.extractors.base import canonical_bytes
from adapters.storage import S3ObjectStorage
from apps.accounts.services import consume_reauthentication_proof
from apps.audit.models import AuditEvent
from apps.audit.services import (
    AuditContext,
    record_audit_event,
    require_audit_replay,
)
from apps.collection.models import CollectionRun, RunStep
from wisdome_writer.domain.concurrency import (
    canonical_request_hash,
    require_expected_version,
    require_idempotent_match,
)
from wisdome_writer.domain.errors import RequestKeyConflict
from wisdome_writer.infrastructure.outbox import enqueue_event

from .models import (
    DocumentExtraction,
    EvidenceAsset,
    EvidenceAuditSnapshot,
    EvidenceDerivationType,
    EvidenceKind,
    EvidenceReviewDecision,
    ExtractionEngine,
    ExtractionObjectWriteReservation,
    ExtractionObjectWriteState,
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


class EvidenceExtractionStopped(Exception):
    """Raised before external extraction work when the collection run is stopping."""


_TERMINAL_EXTRACTION_STATES = frozenset(
    {
        ExtractionState.SUCCEEDED,
        ExtractionState.LOW_CONFIDENCE,
        ExtractionState.FAILED,
    }
)


def _validated_lease_identity(
    *,
    source_event_id: uuid.UUID | str,
    delivery_count: int,
    lease_generation: int,
    lease_owner: str,
    lease_token: uuid.UUID | str,
) -> tuple[uuid.UUID, int, int, str, uuid.UUID]:
    try:
        event_id = uuid.UUID(str(source_event_id))
        token = uuid.UUID(str(lease_token))
    except (TypeError, ValueError, AttributeError) as exc:
        raise EvidenceInvariantError("Extraction lease identity must contain UUIDs") from exc
    if type(delivery_count) is not int or delivery_count < 1:
        raise EvidenceInvariantError("Extraction delivery count must be positive")
    if type(lease_generation) is not int or lease_generation != delivery_count:
        raise EvidenceInvariantError(
            "Extraction generation must equal the owned consumer lease generation"
        )
    owner = str(lease_owner).strip()
    if not owner or len(owner) > 160:
        raise EvidenceInvariantError("Extraction lease owner is invalid")
    return event_id, delivery_count, lease_generation, owner, token


def _run_id_for_generic(attempt_id: Any) -> uuid.UUID:
    return GenericExtractionAttempt.objects.values_list(
        "run_source_item__run_id", flat=True
    ).get(pk=attempt_id)


def _run_id_for_document(document_id: Any) -> uuid.UUID:
    return DocumentExtraction.objects.values_list(
        "run_source_item__run_id", flat=True
    ).get(pk=document_id)


def _claim_locked_extraction(
    aggregate: DocumentExtraction | GenericExtractionAttempt,
    *,
    source_event_id: uuid.UUID,
    delivery_count: int,
    lease_generation: int,
    lease_owner: str,
    lease_token: uuid.UUID,
):
    if aggregate.state in _TERMINAL_EXTRACTION_STATES or aggregate.terminal_event_key:
        return None
    if aggregate.source_event_id not in (None, source_event_id):
        raise EvidenceConflict("Extraction aggregate belongs to another source event")
    if delivery_count <= aggregate.delivery_count:
        return None
    aggregate_kind = (
        "document"
        if isinstance(aggregate, DocumentExtraction)
        else "generic"
    )
    ExtractionObjectWriteReservation.objects.filter(
        aggregate_kind=aggregate_kind,
        aggregate_id=aggregate.id,
        lease_generation__lt=lease_generation,
        state__in=(
            ExtractionObjectWriteState.RESERVED,
            ExtractionObjectWriteState.UPLOADED,
        ),
    ).update(
        state=ExtractionObjectWriteState.ORPHANED,
        orphaned_at=timezone.now(),
    )
    aggregate.lease_generation = lease_generation
    aggregate.source_event_id = source_event_id
    aggregate.delivery_count = delivery_count
    aggregate.lease_owner = lease_owner
    aggregate.lease_token = lease_token
    aggregate.next_retry_at = None
    aggregate.state = ExtractionState.RUNNING
    aggregate.started_at = aggregate.started_at or timezone.now()
    aggregate.error_code = None
    aggregate.error_detail_redacted = None
    aggregate.finished_at = None
    aggregate.save(
        update_fields=(
            "source_event_id",
            "delivery_count",
            "lease_generation",
            "lease_owner",
            "lease_token",
            "next_retry_at",
            "state",
            "started_at",
            "error_code",
            "error_detail_redacted",
            "finished_at",
            "updated_at",
        )
    )
    return aggregate


def begin_generic_extraction(
    attempt_id: Any,
    *,
    source_event_id: uuid.UUID | str,
    delivery_count: int,
    lease_generation: int,
    lease_owner: str,
    lease_token: uuid.UUID | str,
) -> GenericExtractionAttempt | None:
    event_id, count, generation, owner, token = _validated_lease_identity(
        source_event_id=source_event_id,
        delivery_count=delivery_count,
        lease_generation=lease_generation,
        lease_owner=lease_owner,
        lease_token=lease_token,
    )
    run_id = _run_id_for_generic(attempt_id)
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        attempt = GenericExtractionAttempt.objects.select_for_update().get(pk=attempt_id)
        if run.stop_requested_at is not None or run.state == "stopping":
            raise EvidenceExtractionStopped
        if run.state != "extracting":
            return None
        return _claim_locked_extraction(
            attempt,
            source_event_id=event_id,
            delivery_count=count,
            lease_generation=generation,
            lease_owner=owner,
            lease_token=token,
        )


def begin_document_extraction(
    document_id: Any,
    *,
    source_event_id: uuid.UUID | str,
    delivery_count: int,
    lease_generation: int,
    lease_owner: str,
    lease_token: uuid.UUID | str,
) -> DocumentExtraction | None:
    event_id, count, generation, owner, token = _validated_lease_identity(
        source_event_id=source_event_id,
        delivery_count=delivery_count,
        lease_generation=lease_generation,
        lease_owner=lease_owner,
        lease_token=lease_token,
    )
    run_id = _run_id_for_document(document_id)
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        document = DocumentExtraction.objects.select_for_update().get(pk=document_id)
        if run.stop_requested_at is not None or run.state == "stopping":
            raise EvidenceExtractionStopped
        if run.state != "extracting":
            return None
        claimed = _claim_locked_extraction(
            document,
            source_event_id=event_id,
            delivery_count=count,
            lease_generation=generation,
            lease_owner=owner,
            lease_token=token,
        )
        if claimed is None:
            return None
        children = list(
            ExtractionRun.objects.select_for_update()
            .filter(
                document_extraction=document,
                state__in=(ExtractionState.QUEUED, ExtractionState.RUNNING),
            )
            .order_by("id")
        )
        for child in children:
            child.source_event_id = event_id
            child.parent_lease_generation = generation
            child.state = ExtractionState.QUEUED
            child.lease_owner = ""
            child.lease_token = None
            child.next_retry_at = None
            child.save(
                update_fields=(
                    "source_event_id",
                    "parent_lease_generation",
                    "state",
                    "lease_owner",
                    "lease_token",
                    "next_retry_at",
                    "updated_at",
                )
            )
        return claimed


def begin_evidence_fanout(
    run_id: Any,
    *,
    source_event_id: uuid.UUID | str,
    delivery_count: int,
    lease_generation: int,
    lease_owner: str,
    lease_token: uuid.UUID | str,
) -> RunStep | None:
    event_id, count, generation, owner, token = _validated_lease_identity(
        source_event_id=source_event_id,
        delivery_count=delivery_count,
        lease_generation=lease_generation,
        lease_owner=lease_owner,
        lease_token=lease_token,
    )
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        step, _ = RunStep.objects.select_for_update().get_or_create(
            run=run,
            name="extract",
            attempt_no=1,
        )
        if run.stop_requested_at is not None or run.state == "stopping":
            raise EvidenceExtractionStopped
        if run.state != "extracting":
            return None
        if step.fanout_completed_at is not None:
            return None
        if step.source_event_id not in (None, event_id):
            raise EvidenceConflict("Evidence fanout belongs to another source event")
        if count <= step.delivery_count:
            return None
        ExtractionObjectWriteReservation.objects.filter(
            aggregate_kind="fanout",
            aggregate_id=run.id,
            lease_generation__lt=generation,
            state__in=(
                ExtractionObjectWriteState.RESERVED,
                ExtractionObjectWriteState.UPLOADED,
            ),
        ).update(
            state=ExtractionObjectWriteState.ORPHANED,
            orphaned_at=timezone.now(),
        )
        step.lease_generation = generation
        step.source_event_id = event_id
        step.delivery_count = count
        step.lease_owner = owner
        step.lease_token = token
        step.state = "running"
        step.save(
            update_fields=(
                "source_event_id",
                "delivery_count",
                "lease_generation",
                "lease_owner",
                "lease_token",
                "state",
            )
        )
        return step


@contextmanager
def evidence_fanout_completion_fence(
    run_id: Any,
    *,
    expected_generation: int,
    expected_lease_owner: str,
    expected_lease_token: uuid.UUID | str,
):
    try:
        token = uuid.UUID(str(expected_lease_token))
    except (TypeError, ValueError, AttributeError):
        token = None
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        step = RunStep.objects.select_for_update().get(
            run=run,
            name="extract",
            attempt_no=1,
        )
        if (
            run.stop_requested_at is not None
            or run.state != "extracting"
            or step.fanout_completed_at is not None
            or step.state != "running"
            or step.lease_generation != expected_generation
            or step.lease_owner != expected_lease_owner
            or step.lease_token != token
        ):
            yield None
            return
        yield step


def _lease_matches(
    aggregate: DocumentExtraction | ExtractionRun | GenericExtractionAttempt,
    *,
    expected_generation: int,
    expected_lease_owner: str,
    expected_lease_token: uuid.UUID | str,
) -> bool:
    try:
        token = uuid.UUID(str(expected_lease_token))
    except (TypeError, ValueError, AttributeError):
        return False
    return bool(
        aggregate.state == ExtractionState.RUNNING
        and aggregate.lease_generation == expected_generation
        and aggregate.lease_owner == expected_lease_owner
        and aggregate.lease_token == token
    )


def queue_generic_extraction_retry(
    attempt_id: Any,
    *,
    expected_generation: int,
    expected_lease_owner: str,
    expected_lease_token: uuid.UUID | str,
    retry_at: datetime,
    error_code: str,
    error_detail_redacted: str,
) -> bool:
    run_id = _run_id_for_generic(attempt_id)
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        attempt = GenericExtractionAttempt.objects.select_for_update().get(pk=attempt_id)
        if run.stop_requested_at is not None or run.state != "extracting" or not _lease_matches(
            attempt,
            expected_generation=expected_generation,
            expected_lease_owner=expected_lease_owner,
            expected_lease_token=expected_lease_token,
        ):
            return False
        attempt.state = ExtractionState.QUEUED
        attempt.lease_owner = ""
        attempt.lease_token = None
        attempt.next_retry_at = retry_at
        attempt.error_code = error_code[:120]
        attempt.error_detail_redacted = error_detail_redacted[:1000]
        attempt.finished_at = None
        attempt.save(
            update_fields=(
                "state",
                "lease_owner",
                "lease_token",
                "next_retry_at",
                "error_code",
                "error_detail_redacted",
                "finished_at",
                "updated_at",
            )
        )
        return True


def queue_document_extraction_retry(
    document_id: Any,
    *,
    expected_generation: int,
    expected_lease_owner: str,
    expected_lease_token: uuid.UUID | str,
    retry_at: datetime,
    error_code: str,
    error_detail_redacted: str,
) -> bool:
    run_id = _run_id_for_document(document_id)
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        document = DocumentExtraction.objects.select_for_update().get(pk=document_id)
        if run.stop_requested_at is not None or run.state != "extracting" or not _lease_matches(
            document,
            expected_generation=expected_generation,
            expected_lease_owner=expected_lease_owner,
            expected_lease_token=expected_lease_token,
        ):
            return False
        document.state = ExtractionState.QUEUED
        document.document_complete = False
        document.lease_owner = ""
        document.lease_token = None
        document.next_retry_at = retry_at
        document.error_code = error_code[:120]
        document.error_detail_redacted = error_detail_redacted[:1000]
        document.finished_at = None
        document.save(
            update_fields=(
                "state",
                "document_complete",
                "lease_owner",
                "lease_token",
                "next_retry_at",
                "error_code",
                "error_detail_redacted",
                "finished_at",
                "updated_at",
            )
        )
        child_runs = list(
            ExtractionRun.objects.select_for_update()
            .filter(document_extraction=document)
            .order_by("id")
        )
        for child in child_runs:
            if child.state in _TERMINAL_EXTRACTION_STATES:
                continue
            child.parent_lease_generation = document.lease_generation
            child.source_event_id = document.source_event_id
            child.lease_owner = ""
            child.lease_token = None
            child.next_retry_at = retry_at
            if child.state == ExtractionState.RUNNING:
                child.state = ExtractionState.QUEUED
                child.lease_generation = document.lease_generation
                child.error_code = error_code[:120]
                child.error_detail_redacted = error_detail_redacted[:1000]
                child.finished_at = None
            child.save(
                update_fields=(
                    "parent_lease_generation",
                    "source_event_id",
                    "lease_generation",
                    "lease_owner",
                    "lease_token",
                    "next_retry_at",
                    "state",
                    "error_code",
                    "error_detail_redacted",
                    "finished_at",
                    "updated_at",
                )
            )
        return True


@contextmanager
def generic_extraction_completion_fence(
    attempt_id: Any,
    *,
    expected_generation: int,
    expected_lease_owner: str,
    expected_lease_token: uuid.UUID | str,
):
    run_id = _run_id_for_generic(attempt_id)
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        attempt = GenericExtractionAttempt.objects.select_for_update().get(pk=attempt_id)
        if (
            run.stop_requested_at is not None
            or run.state != "extracting"
            or not _lease_matches(
            attempt,
            expected_generation=expected_generation,
            expected_lease_owner=expected_lease_owner,
            expected_lease_token=expected_lease_token,
            )
        ):
            yield None
            return
        yield attempt


@contextmanager
def document_extraction_completion_fence(
    document_id: Any,
    *,
    expected_generation: int,
    expected_lease_owner: str,
    expected_lease_token: uuid.UUID | str,
):
    run_id = _run_id_for_document(document_id)
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        document = DocumentExtraction.objects.select_for_update().get(pk=document_id)
        if (
            run.stop_requested_at is not None
            or run.state != "extracting"
            or not _lease_matches(
            document,
            expected_generation=expected_generation,
            expected_lease_owner=expected_lease_owner,
            expected_lease_token=expected_lease_token,
            )
        ):
            yield None
            return
        yield document


def begin_extraction_run(
    extraction_run_id: Any,
    *,
    expected_parent_generation: int,
    expected_parent_lease_owner: str,
    expected_parent_lease_token: uuid.UUID | str,
) -> ExtractionRun | None:
    run_id, document_id = ExtractionRun.objects.values_list(
        "document_extraction__run_source_item__run_id",
        "document_extraction_id",
    ).get(pk=extraction_run_id)
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        document = DocumentExtraction.objects.select_for_update().get(pk=document_id)
        child = ExtractionRun.objects.select_for_update().get(pk=extraction_run_id)
        if run.stop_requested_at is not None or run.state == "stopping":
            raise EvidenceExtractionStopped
        if run.state != "extracting":
            return None
        if not _lease_matches(
            document,
            expected_generation=expected_parent_generation,
            expected_lease_owner=expected_parent_lease_owner,
            expected_lease_token=expected_parent_lease_token,
        ):
            return None
        if child.state in {ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE}:
            return None
        ExtractionObjectWriteReservation.objects.filter(
            aggregate_kind="extraction_run",
            aggregate_id=child.id,
            lease_generation__lt=document.lease_generation,
            state__in=(
                ExtractionObjectWriteState.RESERVED,
                ExtractionObjectWriteState.UPLOADED,
            ),
        ).update(
            state=ExtractionObjectWriteState.ORPHANED,
            orphaned_at=timezone.now(),
        )
        child.source_event_id = document.source_event_id
        child.parent_lease_generation = document.lease_generation
        child.lease_generation = document.lease_generation
        child.delivery_count = document.delivery_count
        child.lease_owner = document.lease_owner
        child.lease_token = document.lease_token
        child.next_retry_at = None
        child.state = ExtractionState.RUNNING
        child.started_at = child.started_at or timezone.now()
        child.error_code = None
        child.error_detail_redacted = None
        child.finished_at = None
        child.save()
        return child


@contextmanager
def extraction_run_completion_fence(
    extraction_run_id: Any,
    *,
    expected_parent_generation: int,
    expected_child_generation: int,
    expected_lease_owner: str,
    expected_lease_token: uuid.UUID | str,
):
    run_id, document_id = ExtractionRun.objects.values_list(
        "document_extraction__run_source_item__run_id",
        "document_extraction_id",
    ).get(pk=extraction_run_id)
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run_id)
        document = DocumentExtraction.objects.select_for_update().get(pk=document_id)
        child = ExtractionRun.objects.select_for_update().get(pk=extraction_run_id)
        if (
            run.stop_requested_at is not None
            or run.state != "extracting"
            or document.state != ExtractionState.RUNNING
            or document.lease_generation != expected_parent_generation
            or child.parent_lease_generation != expected_parent_generation
            or not _lease_matches(
                child,
                expected_generation=expected_child_generation,
                expected_lease_owner=expected_lease_owner,
                expected_lease_token=expected_lease_token,
            )
        ):
            yield None
            return
        yield child


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


def extraction_lease_identity_hash(
    *, source_event_id: Any, lease_generation: int, lease_owner: str, lease_token: Any
) -> str:
    return canonical_hash(
        {
            "source_event_id": str(source_event_id),
            "lease_generation": lease_generation,
            "lease_owner": lease_owner,
            "lease_token": str(lease_token),
        }
    )


def reserve_extraction_object_write(
    *,
    aggregate_kind: str,
    aggregate_id: Any,
    source_event_id: Any,
    lease_generation: int,
    lease_owner: str,
    lease_token: Any,
    purpose: str,
    object_key: str,
) -> ExtractionObjectWriteReservation:
    identity_hash = extraction_lease_identity_hash(
        source_event_id=source_event_id,
        lease_generation=lease_generation,
        lease_owner=lease_owner,
        lease_token=lease_token,
    )
    with transaction.atomic():
        token = uuid.UUID(str(lease_token))
        if aggregate_kind == "fanout":
            run = CollectionRun.objects.select_for_update().get(pk=aggregate_id)
            aggregate = RunStep.objects.select_for_update().get(
                run=run, name="extract", attempt_no=1
            )
            owned = (
                run.state == "extracting"
                and run.stop_requested_at is None
                and aggregate.state == "running"
                and str(aggregate.source_event_id) == str(source_event_id)
                and aggregate.lease_generation == lease_generation
                and aggregate.lease_owner == lease_owner
                and aggregate.lease_token == token
            )
        elif aggregate_kind == "generic":
            run_id = _run_id_for_generic(aggregate_id)
            run = CollectionRun.objects.select_for_update().get(pk=run_id)
            aggregate = GenericExtractionAttempt.objects.select_for_update().get(
                pk=aggregate_id
            )
            owned = (
                run.state == "extracting"
                and run.stop_requested_at is None
                and str(aggregate.source_event_id) == str(source_event_id)
                and _lease_matches(
                    aggregate,
                    expected_generation=lease_generation,
                    expected_lease_owner=lease_owner,
                    expected_lease_token=token,
                )
            )
        elif aggregate_kind == "document":
            run_id = _run_id_for_document(aggregate_id)
            run = CollectionRun.objects.select_for_update().get(pk=run_id)
            aggregate = DocumentExtraction.objects.select_for_update().get(
                pk=aggregate_id
            )
            owned = (
                run.state == "extracting"
                and run.stop_requested_at is None
                and str(aggregate.source_event_id) == str(source_event_id)
                and _lease_matches(
                    aggregate,
                    expected_generation=lease_generation,
                    expected_lease_owner=lease_owner,
                    expected_lease_token=token,
                )
            )
        elif aggregate_kind == "extraction_run":
            run_id, document_id = ExtractionRun.objects.values_list(
                "document_extraction__run_source_item__run_id",
                "document_extraction_id",
            ).get(pk=aggregate_id)
            run = CollectionRun.objects.select_for_update().get(pk=run_id)
            DocumentExtraction.objects.select_for_update().get(pk=document_id)
            aggregate = ExtractionRun.objects.select_for_update().get(pk=aggregate_id)
            owned = (
                run.state == "extracting"
                and run.stop_requested_at is None
                and str(aggregate.source_event_id) == str(source_event_id)
                and _lease_matches(
                    aggregate,
                    expected_generation=lease_generation,
                    expected_lease_owner=lease_owner,
                    expected_lease_token=token,
                )
            )
        else:
            raise EvidenceInvariantError("Unknown extraction object aggregate kind")
        if not owned:
            raise EvidenceConflict("Object-write reservation lease is stale")
        reservation, created = ExtractionObjectWriteReservation.objects.get_or_create(
            aggregate_kind=aggregate_kind,
            aggregate_id=aggregate_id,
            lease_generation=lease_generation,
            purpose=purpose,
            object_key=object_key,
            defaults={
                "source_event_id": source_event_id,
                "lease_identity_hash": identity_hash,
            },
        )
        if not created and (
            str(reservation.source_event_id) != str(source_event_id)
            or reservation.lease_identity_hash != identity_hash
        ):
            raise EvidenceConflict("Object-write reservation belongs to another lease")
        return reservation


def mark_extraction_object_uploaded(
    reservation_id: Any,
    *,
    object_version: str,
    object_etag: str | None = None,
    checksum: str,
    byte_size: int,
    content_type: str = "application/octet-stream",
) -> ExtractionObjectWriteReservation:
    if not isinstance(object_version, str) or not object_version.strip():
        raise EvidenceInvariantError("Uploaded extraction objects require an immutable version")
    if (
        not isinstance(checksum, str)
        or len(checksum) != 64
        or any(character not in "0123456789abcdef" for character in checksum)
        or type(byte_size) is not int
        or byte_size < 0
        or not isinstance(content_type, str)
        or not content_type.strip()
    ):
        raise EvidenceInvariantError("Uploaded extraction object provenance is incomplete")
    with transaction.atomic():
        reservation = ExtractionObjectWriteReservation.objects.select_for_update().get(
            pk=reservation_id
        )
        uploaded_envelope = (
            reservation.object_version == object_version
            and reservation.object_etag == object_etag
            and reservation.checksum == checksum
            and reservation.byte_size == byte_size
            and reservation.content_type == content_type
        )
        if reservation.state in (
            ExtractionObjectWriteState.UPLOADED,
            ExtractionObjectWriteState.BOUND,
        ):
            if not uploaded_envelope:
                raise EvidenceConflict("Uploaded object envelope changed")
            return reservation
        if reservation.state == ExtractionObjectWriteState.ORPHANED:
            if reservation.object_version is not None:
                if not uploaded_envelope:
                    raise EvidenceConflict("Orphaned object envelope changed")
                return reservation
        elif reservation.state != ExtractionObjectWriteState.RESERVED:
            raise EvidenceConflict("Object-write reservation is no longer uploadable")
        if reservation.state == ExtractionObjectWriteState.RESERVED:
            reservation.state = ExtractionObjectWriteState.UPLOADED
        reservation.object_version = object_version
        reservation.object_etag = object_etag
        reservation.checksum = checksum
        reservation.byte_size = byte_size
        reservation.content_type = content_type
        reservation.uploaded_at = timezone.now()
        reservation.save()
        return reservation


def bind_extraction_object_write(reservation_id: Any) -> bool:
    reservation = ExtractionObjectWriteReservation.objects.select_for_update().get(
        pk=reservation_id
    )
    if reservation.state == ExtractionObjectWriteState.BOUND:
        return True
    if reservation.state != ExtractionObjectWriteState.UPLOADED:
        return False
    reservation.state = ExtractionObjectWriteState.BOUND
    reservation.bound_at = timezone.now()
    reservation.save(update_fields=("state", "bound_at", "updated_at"))
    return True


def orphan_unbound_extraction_object_writes(
    *, aggregate_kind: str, aggregate_id: Any, lease_generation: int
) -> int:
    now = timezone.now()
    with transaction.atomic():
        reservations = list(
            ExtractionObjectWriteReservation.objects.select_for_update()
            .filter(
                aggregate_kind=aggregate_kind,
                aggregate_id=aggregate_id,
                lease_generation=lease_generation,
                state__in=(
                    ExtractionObjectWriteState.RESERVED,
                    ExtractionObjectWriteState.UPLOADED,
                ),
            )
            .order_by("id")
        )
        for reservation in reservations:
            reservation.state = ExtractionObjectWriteState.ORPHANED
            reservation.orphaned_at = now
            reservation.save(update_fields=("state", "orphaned_at", "updated_at"))
        return len(reservations)


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
        "safety_material_hash": (
            document.routing_manifest.get("safety_material_hash")
            if isinstance(document.routing_manifest, dict)
            else None
        ),
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


def validate_selected_page_coverage(
    *,
    routing_manifest: Mapping[str, Any],
    expected_page_indices: Sequence[int],
    runs_by_id: Mapping[str, ExtractionRun],
) -> dict[int, ExtractionRun]:
    pages = routing_manifest.get("pages", [])
    if not isinstance(pages, list):
        raise EvidenceInvariantError("Routing manifest pages must be a list")
    selected: dict[int, ExtractionRun] = {}
    for page in pages:
        if not isinstance(page, Mapping) or type(page.get("page_index")) is not int:
            raise EvidenceInvariantError("Routing manifest page identity is invalid")
        index = page["page_index"]
        run = runs_by_id.get(str(page.get("selected_run_id")))
        if (
            run is None
            or index in selected
            or index not in run.requested_page_indices
            or index not in run.processed_page_indices
        ):
            raise EvidenceInvariantError(
                "Routing manifest references a missing, duplicate or incompatible run"
            )
        selected[index] = run
    if sorted(selected) != list(expected_page_indices):
        raise EvidenceInvariantError("Routing manifest does not exactly cover the document")
    return selected


def document_evidence_manifest_entry(
    *,
    source_item_id: Any,
    origin_run_source_item_id: Any,
    parent_asset_id: Any,
    document_extraction_id: Any,
    extraction_run_id: Any,
    extraction_profile_snapshot_id: Any,
    profile_material_hash: str,
    extraction_method: str,
    extractor_version: str,
    extraction_config_hash: str,
    result_checksum: str,
    locator: Mapping[str, Any],
    evidence_content_hash_value: str,
) -> dict[str, Any]:
    return {
        "source_item_id": str(source_item_id),
        "origin_run_source_item_id": str(origin_run_source_item_id),
        "parent_asset_id": str(parent_asset_id) if parent_asset_id else None,
        "document_extraction_id": str(document_extraction_id),
        "extraction_run_id": str(extraction_run_id),
        "extraction_profile_snapshot_id": str(extraction_profile_snapshot_id),
        "profile_material_hash": profile_material_hash,
        "extraction_method": extraction_method,
        "extractor_version": extractor_version,
        "extraction_config_hash": extraction_config_hash,
        "result_checksum": result_checksum,
        "page_index": locator.get("page_index"),
        "locator_hash": canonical_hash(locator),
        "evidence_content_hash": evidence_content_hash_value,
    }


def generic_evidence_manifest_entry(evidence: EvidenceAsset) -> dict[str, Any]:
    attempt = evidence.generic_extraction_attempt
    return {
        "evidence_asset_id": str(evidence.id),
        "source_item_id": str(evidence.source_item_id),
        "origin_run_source_item_id": str(evidence.origin_run_source_item_id),
        "parent_asset_id": str(evidence.parent_asset_id) if evidence.parent_asset_id else None,
        "generic_extraction_attempt_id": str(evidence.generic_extraction_attempt_id),
        "extraction_profile_snapshot_id": str(attempt.extraction_profile_snapshot_id),
        "profile_material_hash": attempt.profile_material_hash,
        "kind": evidence.kind,
        "locator_type": evidence.locator_type,
        "locator_hash": canonical_hash(evidence.locator),
        "extraction_method": evidence.extraction_method,
        "extractor_version": evidence.extractor_version,
        "extraction_config_hash": evidence.extraction_config_hash,
        "validation_mode": evidence.validation_mode,
        "result_checksum": evidence.extraction_result_checksum,
        "evidence_content_hash": evidence.evidence_content_hash,
        "review_subject_hash": evidence.review_subject_hash,
        "calibration_profile_key": evidence.calibration_profile_key,
        "calibration_profile_version": evidence.calibration_profile_version,
        "calibration_profile_hash": evidence.calibration_profile_hash,
        "confidence": str(evidence.confidence) if evidence.confidence is not None else None,
        "object_key": evidence.object_key,
        "object_version": evidence.object_version,
        "checksum": evidence.checksum,
    }


def generic_evidence_manifest(evidence_assets: Sequence[EvidenceAsset]) -> list[dict[str, Any]]:
    return [
        generic_evidence_manifest_entry(evidence)
        for evidence in sorted(evidence_assets, key=lambda item: str(item.id))
    ]


def generic_evidence_manifest_hash(evidence_assets: Sequence[EvidenceAsset]) -> str:
    return canonical_hash(generic_evidence_manifest(evidence_assets))


def validate_generic_evidence_set(
    attempt: GenericExtractionAttempt,
    evidence_assets: Sequence[EvidenceAsset],
    *,
    allow_legacy_missing_manifest: bool = False,
) -> tuple[list[str], str]:
    """Recompute the authoritative generic evidence set from locked database rows."""
    attempt.full_clean()
    if (
        attempt.state not in {
            ExtractionState.SUCCEEDED,
            ExtractionState.LOW_CONFIDENCE,
        }
        or attempt.terminal_state != "ready"
        or not attempt.terminal_event_key
        or not attempt.result_checksum
    ):
        raise ValidationError("Generic attempt terminal envelope is incomplete")
    assets = sorted(evidence_assets, key=lambda item: str(item.id))
    if not assets:
        raise ValidationError("Generic evidence set is empty")
    manifest_missing = (
        attempt.expected_evidence_count is None
        or not attempt.expected_evidence_manifest_hash
    )
    if manifest_missing and not (
        allow_legacy_missing_manifest and len(assets) == 1
    ):
        raise ValidationError("Generic evidence manifest envelope is missing")
    if not manifest_missing and attempt.expected_evidence_count != len(assets):
        raise ValidationError("Generic evidence count differs from the frozen manifest")
    if attempt.evidence_asset_id is None or sum(
        item.id == attempt.evidence_asset_id for item in assets
    ) != 1:
        raise ValidationError("Generic evidence primary anchor is missing or ambiguous")
    if attempt.engine == ExtractionEngine.LEGACY_HWP and len(assets) != 1:
        raise ValidationError("Legacy HWP requires exactly one converted PDF evidence")
    profile = attempt.extraction_profile_snapshot
    if (
        attempt.profile_material_hash != profile.profile_material_hash
        or attempt.engine != profile.engine
        or attempt.extractor_version != profile.extractor_version
        or attempt.config_hash != profile.config_hash
        or attempt.validation_mode != profile.validation_mode
        or attempt.calibration_profile_key != profile.calibration_profile_key
        or attempt.calibration_profile_version != profile.calibration_profile_version
        or attempt.calibration_profile_hash != profile.calibration_profile_hash
    ):
        raise ValidationError("Generic attempt differs from its frozen profile")
    for evidence in assets:
        if (
            evidence.generic_extraction_attempt_id != attempt.id
            or evidence.source_item_id != attempt.source_item_id
            or evidence.origin_run_source_item_id != attempt.run_source_item_id
            or evidence.parent_asset_id != attempt.input_asset_id
            or evidence.derivation_type != EvidenceDerivationType.OTHER
            or evidence.extraction_method != attempt.engine
            or evidence.extractor_version != attempt.extractor_version
            or evidence.extraction_config_hash != attempt.config_hash
            or evidence.validation_mode != attempt.validation_mode
            or evidence.extraction_result_checksum != attempt.result_checksum
            or evidence.calibration_profile_key != attempt.calibration_profile_key
            or evidence.calibration_profile_version != attempt.calibration_profile_version
            or evidence.calibration_profile_hash != attempt.calibration_profile_hash
        ):
            raise ValidationError("Generic evidence lineage differs from its attempt")
        observed_content_hash = evidence_content_hash(
            text=evidence.extracted_text,
            structured_data=evidence.structured_data,
            checksum=evidence.checksum,
        )
        if observed_content_hash != evidence.evidence_content_hash:
            raise ValidationError("Generic evidence content hash is stale")
        observed_review_hash = calculate_review_subject_hash(evidence)
        if observed_review_hash != evidence.review_subject_hash:
            raise ValidationError("Generic evidence review hash is stale")
        if calculate_publishable(evidence) != evidence.publishable:
            raise ValidationError("Generic evidence publishability projection is stale")
        evidence.full_clean()
    observed_hash = generic_evidence_manifest_hash(assets)
    if not manifest_missing and observed_hash != attempt.expected_evidence_manifest_hash:
        raise ValidationError("Generic evidence manifest differs from the frozen attempt")
    return [str(item.id) for item in assets], observed_hash


def _selected_runs(
    document: DocumentExtraction,
    *,
    locked_runs: Sequence[ExtractionRun] | None = None,
) -> dict[int, ExtractionRun]:
    pages = document.routing_manifest.get("pages", []) if isinstance(document.routing_manifest, dict) else []
    run_ids = {str(page.get("selected_run_id")) for page in pages if page.get("selected_run_id")}
    candidates = (
        locked_runs
        if locked_runs is not None
        else document.extraction_runs.filter(id__in=run_ids).select_related(
            "extraction_profile_snapshot"
        )
    )
    runs = {str(run.id): run for run in candidates if str(run.id) in run_ids}
    return validate_selected_page_coverage(
        routing_manifest=document.routing_manifest,
        expected_page_indices=list(range(document.input_page_count)),
        runs_by_id=runs,
    )


def aggregate_document_extraction(
    document_id: Any,
    *,
    using: str = "default",
) -> DocumentExtraction:
    run_id = (
        DocumentExtraction.objects.using(using)
        .values_list("run_source_item__run_id", flat=True)
        .get(pk=document_id)
    )
    with transaction.atomic(using=using):
        CollectionRun.objects.using(using).select_for_update().get(pk=run_id)
        RunStep.objects.using(using).select_for_update().filter(
            run_id=run_id, name="extract", attempt_no=1
        ).first()
        document = (
            DocumentExtraction.objects.using(using)
            .select_for_update()
            .select_related(
                "input_asset__generic_extraction_attempt__input_asset",
                "input_asset__producing_generic_attempt__input_asset",
                "input_asset__parent_asset",
            )
            .get(pk=document_id)
        )
        locked_runs = list(
            ExtractionRun.objects.using(using)
            .select_for_update()
            .filter(document_extraction=document)
            .select_related("extraction_profile_snapshot")
            .order_by("id")
        )
        input_asset = document.input_asset
        duplicate_input = _uses_audit_only_raw_input(input_asset)
        if document.input_fingerprint is None or duplicate_input:
            return document
        try:
            selected = _selected_runs(document, locked_runs=locked_runs)
        except EvidenceInvariantError as exc:
            document.state = ExtractionState.FAILED
            document.error_code = "routing_manifest_invalid"
            document.error_detail_redacted = str(exc)
            document.document_complete = False
            document.coverage_manifest_hash = None
            document.selected_evidence_manifest_hash = None
            document.finished_at = timezone.now()
            document.next_retry_at = None
            document.lease_owner = ""
            document.lease_token = None
            document.terminal_event_key = (
                f"evidence.document.failed:{document.id}:generation:"
                f"{document.lease_generation}:routing_manifest_invalid"
            )
            document.terminal_state = "failed"
            document.save(
                update_fields=(
                    "state",
                    "error_code",
                    "error_detail_redacted",
                    "document_complete",
                    "coverage_manifest_hash",
                    "selected_evidence_manifest_hash",
                    "finished_at",
                    "next_retry_at",
                    "lease_owner",
                    "lease_token",
                    "terminal_event_key",
                    "terminal_state",
                ),
                using=using,
            )
            return document

        expected = list(range(document.input_page_count))
        covered = sorted(selected)
        complete = covered == expected
        selected_run_ids = {run.id for run in selected.values()}
        evidence = list(
            EvidenceAsset.objects.using(using)
            .select_for_update()
            .filter(
                document_extraction=document,
                extraction_run_id__in=selected_run_ids,
            )
            .select_related("latest_review_decision", "extraction_run")
            .order_by("id")
        )
        missing_successful_evidence = False
        if complete:
            selected_pages_by_run: dict[Any, set[int]] = {}
            for page_index, run in selected.items():
                selected_pages_by_run.setdefault(run.id, set()).add(page_index)
            for run in {item.id: item for item in selected.values()}.values():
                if run.state not in (
                    ExtractionState.SUCCEEDED,
                    ExtractionState.LOW_CONFIDENCE,
                ):
                    complete = False
                    break
                run_evidence = [
                    item for item in evidence if item.extraction_run_id == run.id
                ]
                if not run_evidence:
                    missing_successful_evidence = True
                    complete = False
                    break
                evidence_manifest = sorted(
                    (
                        document_evidence_manifest_entry(
                            source_item_id=item.source_item_id,
                            origin_run_source_item_id=item.origin_run_source_item_id,
                            parent_asset_id=item.parent_asset_id,
                            document_extraction_id=item.document_extraction_id,
                            extraction_run_id=item.extraction_run_id,
                            extraction_profile_snapshot_id=(
                                run.extraction_profile_snapshot_id
                            ),
                            profile_material_hash=run.profile_material_hash,
                            extraction_method=item.extraction_method,
                            extractor_version=item.extractor_version,
                            extraction_config_hash=item.extraction_config_hash,
                            result_checksum=item.extraction_result_checksum,
                            locator=item.locator,
                            evidence_content_hash_value=item.evidence_content_hash,
                        )
                        for item in run_evidence
                    ),
                    key=lambda entry: (
                        entry["locator_hash"],
                        entry["evidence_content_hash"],
                    ),
                )
                if (
                    run.expected_evidence_count != len(run_evidence)
                    or run.expected_evidence_manifest_hash
                    != canonical_hash(evidence_manifest)
                    or run.profile_key
                    != run.extraction_profile_snapshot.profile_key
                    or run.profile_version
                    != run.extraction_profile_snapshot.profile_version
                    or run.config_hash != run.extraction_profile_snapshot.config_hash
                    or run.profile_material_hash
                    != run.extraction_profile_snapshot.profile_material_hash
                    or any(
                        not isinstance(item.locator, dict)
                        or item.locator.get("page_index")
                        not in selected_pages_by_run[run.id]
                        or item.extraction_result_checksum != run.result_checksum
                        for item in run_evidence
                    )
                ):
                    complete = False
                    document.error_code = "selected_evidence_manifest_mismatch"
                    document.error_detail_redacted = (
                        "Selected evidence does not match its immutable child manifest"
                    )
                    break
                if (
                    run.state == ExtractionState.LOW_CONFIDENCE
                    and not all(
                        _approved_current_decision(item)
                        for item in run_evidence
                    )
                ):
                    complete = False
                    break

        document.covered_page_indices = covered
        if complete:
            coverage = [
                {
                    "page_index": index,
                    "selected_run_id": str(run.id),
                    "engine": run.engine,
                    "result_checksum": run.result_checksum,
                }
                for index, run in sorted(selected.items())
            ]
            selected_assets = [
                {
                    "evidence_asset_id": str(item.id),
                    "evidence_content_hash": item.evidence_content_hash,
                    "result_checksum": item.extraction_result_checksum,
                    "locator_hash": canonical_hash(item.locator),
                }
                for item in evidence
            ]
            document.coverage_manifest_hash = canonical_hash(coverage)
            document.selected_evidence_manifest_hash = canonical_hash(
                selected_assets
            )
            document.document_complete = True
            document.state = ExtractionState.SUCCEEDED
            document.finished_at = timezone.now()
            document.error_code = None
            document.error_detail_redacted = None
        else:
            document.coverage_manifest_hash = None
            document.selected_evidence_manifest_hash = None
            document.document_complete = False
            if document.error_code == "selected_evidence_manifest_mismatch":
                document.state = ExtractionState.FAILED
                document.finished_at = timezone.now()
            elif missing_successful_evidence:
                document.state = ExtractionState.FAILED
                document.finished_at = timezone.now()
                document.error_code = "selected_run_evidence_missing"
                document.error_detail_redacted = (
                    "Selected successful extraction run has no evidence assets"
                )
            elif any(
                run.state == ExtractionState.FAILED
                for run in selected.values()
            ):
                document.state = ExtractionState.FAILED
            elif selected:
                document.state = ExtractionState.LOW_CONFIDENCE
        if document.state == ExtractionState.SUCCEEDED:
            document.terminal_event_key = (
                f"evidence.document_ready:{document.id}:"
                f"{document.selected_evidence_manifest_hash}"
            )
            document.terminal_state = "ready"
            document.next_retry_at = None
            document.lease_owner = ""
            document.lease_token = None
        elif document.state == ExtractionState.FAILED:
            document.terminal_event_key = (
                f"evidence.document.failed:{document.id}:generation:"
                f"{document.lease_generation}:{document.error_code or 'failed'}"
            )
            document.terminal_state = "failed"
            document.next_retry_at = None
            document.lease_owner = ""
            document.lease_token = None
        elif document.state == ExtractionState.LOW_CONFIDENCE:
            cause = (
                f"document-terminal:{document.id}:generation:"
                f"{document.lease_generation}:{document.state}"
            )
            document.terminal_event_key = (
                f"evidence.finalize_requested:{run_id}:{cause}"
            )
            document.terminal_state = "ready"
            document.next_retry_at = None
            document.lease_owner = ""
            document.lease_token = None
        document.full_clean()
        document.save(using=using)

        for item in evidence:
            publishable = calculate_publishable(
                item,
                document_complete=document.document_complete,
            )
            if item.publishable != publishable:
                item.publishable = publishable
                item.save(update_fields=("publishable",), using=using)
        return document


def _decision_request_hash(payload: Mapping[str, Any]) -> str:
    return canonical_hash(dict(payload))


def _review_decision_request_hash(payload: Mapping[str, Any]) -> str:
    return canonical_request_hash(
        operation_id="createEvidenceReviewDecision",
        path="/evidence/{evidenceId}/review-decisions",
        payload=payload,
    )


def _profile_audit_material(
    profile: ExtractionProfileSnapshot,
) -> dict[str, Any]:
    return {
        "schema_version": "extraction-profile-audit-v1",
        "profile_id": str(profile.id),
        "profile_material_hash": profile.profile_material_hash,
        "approval_state": profile.approval_state,
        "decision_version": profile.decision_version,
        "latest_decision_id": (
            str(profile.latest_decision_id)
            if profile.latest_decision_id
            else None
        ),
        "verification_report_hash": profile.verification_report_hash,
    }


def _evidence_audit_material(evidence: EvidenceAsset) -> dict[str, Any]:
    return {
        "schema_version": "evidence-review-audit-v1",
        "evidence_id": str(evidence.id),
        "review_subject_schema_version": (
            evidence.review_subject_schema_version
        ),
        "review_subject_hash": evidence.review_subject_hash,
        "latest_review_decision_id": (
            str(evidence.latest_review_decision_id)
            if evidence.latest_review_decision_id
            else None
        ),
        "review_state": evidence.review_state,
        "manual_review_required": evidence.manual_review_required,
        "publishable": evidence.publishable,
    }


def _document_review_projection_material(
    evidence: EvidenceAsset,
    *,
    using: str,
) -> dict[str, Any] | None:
    if not evidence.document_extraction_id:
        return None
    document = (
        DocumentExtraction.objects.using(using)
        .get(pk=evidence.document_extraction_id)
    )
    evidence_rows = list(
        EvidenceAsset.objects.using(using)
        .filter(document_extraction_id=document.id)
        .order_by("id")
        .values(
            "id",
            "review_state",
            "manual_review_required",
            "publishable",
        )
    )
    return {
        "document_id": str(document.id),
        "state": document.state,
        "document_complete": document.document_complete,
        "coverage_manifest_hash": document.coverage_manifest_hash,
        "selected_evidence_manifest_hash": (
            document.selected_evidence_manifest_hash
        ),
        "evidence": [
            {
                "id": str(row["id"]),
                "review_state": row["review_state"],
                "manual_review_required": row[
                    "manual_review_required"
                ],
                "publishable": row["publishable"],
            }
            for row in evidence_rows
        ],
    }


def _validate_profile_report(
    profile: ExtractionProfileSnapshot,
    report: Any,
) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValidationError("The profile verification report is invalid")
    valid = (
        not any(
            key in report
            for key in {"reportObjectKey", "reportObjectVersion", "reportHash"}
        )
        and canonical_hash(report) == profile.verification_report_hash
        and report.get("subjectId") == str(profile.id)
        and report.get("subjectMaterialHash")
        == profile.profile_material_hash
    )
    if not valid:
        raise ValidationError(
            "The profile verification report does not match the approved material"
        )
    if report.get("overallResult") != "passed":
        raise ValidationError(
            "A failed extraction profile report cannot be approved"
        )
    if any(
        item.get("result") != "passed"
        for item in report.get("stageResults", [])
    ):
        raise ValidationError(
            "Every extraction profile verification stage must pass"
        )
    return report


def _validate_profile_report_bytes(
    profile: ExtractionProfileSnapshot,
    raw: bytes,
) -> dict[str, Any]:
    try:
        report = json.loads(raw)
        if canonical_bytes(report) != raw:
            raise ValueError("report bytes are not the canonical core object")
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "The profile verification report bytes are not canonical"
        ) from exc
    return _validate_profile_report(profile, report)


def _profile_report_envelope(
    profile: ExtractionProfileSnapshot,
) -> tuple[str, str, str]:
    projection = (
        profile.verification_report_object_key,
        profile.verification_report_object_version,
        profile.verification_report_hash,
    )
    if not all(projection):
        raise ValidationError("The profile report envelope is incomplete")
    if profile.approval_state in {
        ProfileApprovalState.APPROVED,
        ProfileApprovalState.RETIRED,
    }:
        decision = profile.latest_decision
        expected_decision = (
            ExtractionProfileDecision.Decision.APPROVED
            if profile.approval_state == ProfileApprovalState.APPROVED
            else ExtractionProfileDecision.Decision.RETIRED
        )
        frozen = (
            decision.verification_report_object_key if decision else None,
            decision.verification_report_object_version if decision else None,
            decision.verification_report_hash if decision else None,
        )
        if decision is None or decision.decision != expected_decision or frozen != projection:
            raise ValidationError(
                "The profile projection differs from its frozen decision report envelope"
            )
    return projection  # type: ignore[return-value]


def _verified_profile_report(
    profile: ExtractionProfileSnapshot,
) -> dict[str, Any]:
    if not (
        profile.verification_report_object_key
        and profile.verification_report_object_version
        and profile.verification_report_hash
    ):
        raise ValidationError("A hash-verified profile report is required before approval")
    try:
        stored_version = profile.verification_report_object_version
        raw = S3ObjectStorage().get_bytes(
            key=profile.verification_report_object_key,
            version_id=(
                None
                if stored_version.startswith("etag:")
                else stored_version
            ),
        )
        report = _validate_profile_report_bytes(profile, raw)
    except Exception as exc:
        raise ValidationError("The profile verification report is unavailable") from exc
    return report


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
    audit_context: AuditContext,
) -> tuple[ExtractionProfileDecision, bool]:
    if (
        audit_context.actor_type != AuditEvent.ActorType.ADMIN
        or str(audit_context.actor_id) != str(admin.pk)
    ):
        raise ValueError(
            "audit actor does not match the profile decision administrator"
        )
    if audit_context.request_key != request_key:
        raise ValueError("audit request key does not match the profile decision")
    if audit_context.reason_code != reason:
        raise ValueError("audit reason does not match the profile decision")

    alias = audit_context.database_alias
    request_payload = {
        "decision": decision,
        "expected_material_hash": expected_material_hash,
        "expected_latest_decision_id": (
            str(expected_latest_decision_id) if expected_latest_decision_id else None
        ),
        "request_key": request_key,
        "reason": reason,
        "admin_id": str(admin.pk),
    }
    request_hash = _decision_request_hash(request_payload)
    existing = ExtractionProfileDecision.objects.using(alias).filter(
        profile_snapshot_id=profile_id,
        request_key=request_key,
    ).first()
    if existing:
        if existing.request_hash != request_hash or existing.decided_by_id != admin.pk:
            raise EvidenceConflict("The request key was already used with a different payload")
        require_audit_replay(
            context=audit_context,
            action="evidence.profile_decided",
            entity=existing.profile_snapshot,
            identity_key=request_key,
            request_hash=request_hash,
        )
        return existing, False

    report: dict[str, Any] | None = None
    report_fence: tuple[Any, ...] | None = None
    if decision == ExtractionProfileDecision.Decision.APPROVED:
        profile_for_report = ExtractionProfileSnapshot.objects.using(alias).get(
            pk=profile_id
        )
        report_fence = (
            profile_for_report.profile_material_hash,
            profile_for_report.verification_report_object_key,
            profile_for_report.verification_report_object_version,
            profile_for_report.verification_report_hash,
        )
        # Object storage access and report parsing intentionally happen before
        # the database transaction and its row locks.
        report = _verified_profile_report(profile_for_report)
    elif decision == ExtractionProfileDecision.Decision.RETIRED:
        pass
    else:
        raise ValidationError({"decision": "Unsupported extraction profile decision"})

    with transaction.atomic(using=alias):
        profile = (
            ExtractionProfileSnapshot.objects.using(alias)
            .select_for_update()
            .get(pk=profile_id)
        )
        existing = ExtractionProfileDecision.objects.using(alias).filter(
            profile_snapshot=profile,
            request_key=request_key,
        ).first()
        if existing:
            if (
                existing.request_hash != request_hash
                or existing.decided_by_id != admin.pk
            ):
                raise EvidenceConflict(
                    "The request key was already used with a different payload"
                )
            require_audit_replay(
                context=audit_context,
                action="evidence.profile_decided",
                entity=profile,
                identity_key=request_key,
                request_hash=request_hash,
            )
            return existing, False

        if profile.profile_material_hash != expected_material_hash:
            raise EvidenceConflict("The extraction profile material changed")
        current = (
            str(profile.latest_decision_id)
            if profile.latest_decision_id
            else None
        )
        expected = (
            str(expected_latest_decision_id)
            if expected_latest_decision_id
            else None
        )
        if current != expected:
            raise EvidenceConflict(
                "The extraction profile decision projection is stale"
            )

        if decision == ExtractionProfileDecision.Decision.APPROVED:
            locked_report_fence = (
                profile.profile_material_hash,
                profile.verification_report_object_key,
                profile.verification_report_object_version,
                profile.verification_report_hash,
            )
            if report_fence != locked_report_fence:
                raise EvidenceConflict(
                    "The extraction profile verification report reference changed"
                )
            _validate_profile_report(profile, report)
            if profile.approval_state != ProfileApprovalState.DRAFT:
                raise EvidenceConflict(
                    "Only a draft extraction profile can be approved"
                )
            next_state = ProfileApprovalState.APPROVED
            report_envelope = locked_report_fence[1:]
        else:
            if profile.approval_state != ProfileApprovalState.APPROVED:
                raise EvidenceConflict(
                    "Only an approved extraction profile can be retired"
                )
            report_envelope = _profile_report_envelope(profile)
            next_state = ProfileApprovalState.RETIRED

        before_material = _profile_audit_material(profile)
        consume_reauthentication_proof(
            request=request,
            proof_id=reauth_proof_id,
            action_scope="profile_decision",
            entity_type="extraction_profile_snapshot",
            entity_id=profile.id,
        )
        now = timezone.now()
        version = profile.decision_version + 1
        decision_hash = canonical_hash(
            {
                **request_payload,
                "profile_snapshot_id": str(profile.id),
                "version": version,
                "decided_by": str(admin.pk),
                "decided_at": now,
                "verification_report": {
                    "object_key": report_envelope[0],
                    "object_version": report_envelope[1],
                    "sha256": report_envelope[2],
                },
            }
        )
        record = ExtractionProfileDecision(
            profile_snapshot=profile,
            version=version,
            decision=decision,
            expected_material_hash=expected_material_hash,
            supersedes_decision=profile.latest_decision,
            request_key=request_key,
            request_hash=request_hash,
            decision_hash=decision_hash,
            verification_report_object_key=report_envelope[0],
            verification_report_object_version=report_envelope[1],
            verification_report_hash=report_envelope[2],
            decided_by=admin,
            decided_at=now,
            reason=reason,
        )
        record.full_clean()
        record.save(using=alias)
        profile.latest_decision = record
        profile.decision_version = version
        profile.approval_state = next_state
        if next_state == ProfileApprovalState.APPROVED:
            profile.approved_at = now
        else:
            profile.retired_at = now
        profile.save(using=alias)
        record_audit_event(
            context=audit_context,
            action="evidence.profile_decided",
            entity=profile,
            identity_key=request_key,
            material_schema_version="extraction-profile-audit-v1",
            before_material=before_material,
            after_material=_profile_audit_material(profile),
            metadata={
                "request_hash": request_hash,
                "decision": decision,
                "decision_hash": decision_hash,
                "decision_id": str(record.id),
                "profile_id": str(profile.id),
                "profile_material_hash": profile.profile_material_hash,
                "verification_report_object_key": report_envelope[0],
                "verification_report_object_version": report_envelope[1],
                "verification_report_hash": report_envelope[2],
                "result": "decided",
                "version": version,
                "reauth_proof_id": str(reauth_proof_id),
            },
        )
        enqueue_event(
            topic="evidence.profile_decided",
            aggregate_type="ExtractionProfileSnapshot",
            aggregate_id=profile.id,
            message_key=f"evidence.profile_decided:{profile.id}:{record.id}",
            correlation_id=audit_context.correlation_id,
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


def _ensure_review_snapshot(
    evidence: EvidenceAsset,
    *,
    using: str,
) -> EvidenceAuditSnapshot:
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
    snapshot, _ = EvidenceAuditSnapshot.objects.using(using).get_or_create(
        snapshot_hash=snapshot_hash,
        defaults={**payload, "original_evidence_asset_id": evidence.id},
    )
    return snapshot


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
    audit_context: AuditContext,
) -> tuple[EvidenceReviewDecision, bool]:
    if (
        audit_context.actor_type != AuditEvent.ActorType.ADMIN
        or str(audit_context.actor_id) != str(admin.pk)
    ):
        raise ValueError(
            "audit actor does not match the evidence review administrator"
        )
    if audit_context.request_key != request_key:
        raise ValueError("audit request key does not match the evidence review")
    if audit_context.reason_code != reason:
        raise ValueError("audit reason does not match the evidence review")

    alias = audit_context.database_alias
    expected = (
        str(expected_latest_decision_id)
        if expected_latest_decision_id
        else None
    )
    request_payload = {
        "path": {"evidenceId": str(evidence_id)},
        "query": {},
        "body": {
            "decision": decision,
            "subjectSchemaVersion": expected_subject_version,
            "subjectHash": expected_subject_hash,
            "expectedLatestDecisionId": expected,
            "requestKey": request_key,
            "reason": reason,
        },
    }
    request_hash = _review_decision_request_hash(request_payload)
    legacy_request_hash = _decision_request_hash({
        "evidence_id": str(evidence_id),
        "decision": decision,
        "expected_subject_version": expected_subject_version,
        "expected_subject_hash": expected_subject_hash,
        "expected_latest_decision_id": expected,
        "request_key": request_key,
        "reason": reason,
        "admin_id": str(admin.pk),
    })

    def replay(
        existing: EvidenceReviewDecision,
    ) -> tuple[EvidenceReviewDecision, bool]:
        if existing.reviewer_admin_id != admin.pk:
            raise RequestKeyConflict(
                "The request key is already bound to another administrator"
            )
        if existing.request_hash == request_hash:
            matched_request_hash = request_hash
        elif existing.request_hash == legacy_request_hash:
            matched_request_hash = legacy_request_hash
        else:
            require_idempotent_match(
                stored_hash=existing.request_hash,
                expected_hash=request_hash,
            )
            matched_request_hash = request_hash
        require_idempotent_match(
            stored_hash=existing.request_hash,
            expected_hash=matched_request_hash,
        )
        replay_evidence = existing.evidence_asset
        if replay_evidence is None:
            original_evidence_id = (
                existing.evidence_audit_snapshot.original_evidence_asset_id
            )
            if (
                original_evidence_id is None
                or str(original_evidence_id) != str(evidence_id)
            ):
                raise EvidenceInvariantError(
                    "The durable evidence review identity is unavailable"
                )
            replay_evidence = EvidenceAsset(pk=original_evidence_id)
            replay_evidence._state.adding = False
            replay_evidence._state.db = alias
        require_audit_replay(
            context=audit_context,
            action="evidence.review_decided",
            entity=replay_evidence,
            identity_key=canonical_hash(
                {
                    "schema_version": "evidence-review-identity-v1",
                    "snapshot_id": str(existing.evidence_audit_snapshot_id),
                    "request_key": request_key,
                }
            ),
            request_hash=matched_request_hash,
        )
        return existing, False

    existing = (
        EvidenceReviewDecision.objects.using(alias)
        .select_related("evidence_asset", "evidence_audit_snapshot")
        .filter(
            request_key=request_key,
        )
        .filter(
            Q(evidence_asset_id=evidence_id)
            | Q(
                evidence_audit_snapshot__original_evidence_asset_id=evidence_id
            )
        )
        .order_by("decided_at", "id")
        .first()
    )
    if existing:
        return replay(existing)

    with transaction.atomic(using=alias):
        document_id, generic_attempt_id, origin_run_id = (
            EvidenceAsset.objects.using(alias)
            .filter(pk=evidence_id)
            .values_list(
                "document_extraction_id",
                "generic_extraction_attempt_id",
                "origin_run_source_item__run_id",
            )
            .get()
        )
        run_id = origin_run_id
        if document_id is not None:
            run_id = (
                DocumentExtraction.objects.using(alias)
                .values_list("run_source_item__run_id", flat=True)
                .get(pk=document_id)
            )
        elif generic_attempt_id is not None:
            run_id = (
                GenericExtractionAttempt.objects.using(alias)
                .values_list("run_source_item__run_id", flat=True)
                .get(pk=generic_attempt_id)
            )
        if run_id is not None:
            CollectionRun.objects.using(alias).select_for_update().get(pk=run_id)
            RunStep.objects.using(alias).select_for_update().filter(
                run_id=run_id, name="extract", attempt_no=1
            ).first()
        if document_id is not None:
            DocumentExtraction.objects.using(alias).select_for_update().get(
                pk=document_id
            )
            locked_evidence_ids = list(
                EvidenceAsset.objects.using(alias)
                .select_for_update()
                .filter(document_extraction_id=document_id)
                .order_by("id")
                .values_list("id", flat=True)
            )
            if not any(
                str(locked_id) == str(evidence_id)
                for locked_id in locked_evidence_ids
            ):
                raise EvidenceConflict(
                    "The evidence document membership changed; retry the review"
                )
        elif generic_attempt_id is not None:
            GenericExtractionAttempt.objects.using(alias).select_for_update().get(
                pk=generic_attempt_id
            )
            EvidenceAsset.objects.using(alias).select_for_update().get(
                pk=evidence_id
            )
        else:
            EvidenceAsset.objects.using(alias).select_for_update().get(
                pk=evidence_id
            )
        evidence = (
            EvidenceAsset.objects.using(alias)
            .select_related(
                "latest_review_decision",
                "extraction_run",
                "generic_extraction_attempt",
                "document_extraction",
                "origin_run_source_item",
            )
            .get(pk=evidence_id)
        )
        if evidence.document_extraction_id != document_id:
            raise EvidenceConflict(
                "The evidence document membership changed; retry the review"
            )
        existing = (
            EvidenceReviewDecision.objects.using(alias)
            .select_related("evidence_asset", "evidence_audit_snapshot")
            .filter(
                evidence_asset=evidence,
                request_key=request_key,
            )
            .order_by("decided_at", "id")
            .first()
        )
        if existing:
            return replay(existing)

        calculated = calculate_review_subject_hash(evidence)
        if calculated != evidence.review_subject_hash:
            raise EvidenceInvariantError(
                "Stored evidence review subject hash is invalid"
            )
        if (
            evidence.review_subject_schema_version
            != expected_subject_version
            or evidence.review_subject_hash != expected_subject_hash
        ):
            raise EvidenceConflict(
                "The evidence subject changed and must be reviewed again"
            )
        reasons = normalize_low_confidence_reasons(
            evidence.low_confidence_reasons or []
        )
        expected_reason_hash = _reason_hash_for_evidence(evidence)
        if (
            expected_reason_hash
            and canonical_hash(reasons) != expected_reason_hash
        ):
            raise EvidenceInvariantError(
                "The complete low-confidence reason manifest is unavailable or invalid"
            )

        snapshot = _ensure_review_snapshot(evidence, using=alias)
        current = (
            str(evidence.latest_review_decision_id)
            if evidence.latest_review_decision_id
            else None
        )
        require_expected_version(
            actual=current,
            expected=expected,
            subject="Evidence review projection",
        )
        if decision not in EvidenceReviewDecision.Decision.values:
            raise ValidationError(
                {"decision": "Unsupported evidence review decision"}
            )

        before_material = _evidence_audit_material(evidence)
        document_before_material = _document_review_projection_material(
            evidence,
            using=alias,
        )
        provenance_type, extraction_run, generic_attempt = (
            _provenance_for_review(evidence)
        )
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
        record.save(using=alias)
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
        evidence.save(
            update_fields=(
                "latest_review_decision",
                "manual_reviewed_at",
                "manual_reviewed_by",
                "manual_review_required",
                "review_state",
                "publishable",
            ),
            using=alias,
        )
        evidence.publishable = calculate_publishable(
            evidence,
            document_complete=(
                evidence.document_extraction.document_complete
                if evidence.document_extraction_id
                else None
            ),
        )
        evidence.save(update_fields=("publishable",), using=alias)
        document_after_material = _document_review_projection_material(
            evidence,
            using=alias,
        )

        record_audit_event(
            context=audit_context,
            action="evidence.review_decided",
            entity=evidence,
            identity_key=canonical_hash(
                {
                    "schema_version": "evidence-review-identity-v1",
                    "snapshot_id": str(snapshot.id),
                    "request_key": request_key,
                }
            ),
            material_schema_version="evidence-review-audit-v1",
            before_material={
                "evidence": before_material,
                "document_projection": document_before_material,
            },
            after_material={
                "evidence": _evidence_audit_material(evidence),
                "document_projection": document_after_material,
            },
            metadata={
                "request_hash": request_hash,
                "decision": decision,
                "decision_id": str(record.id),
                "evidence_id": str(evidence.id),
                "result": "decided",
                "subject_hash": evidence.review_subject_hash,
            },
        )
        enqueue_event(
            topic="evidence.review_decided",
            aggregate_type="EvidenceAsset",
            aggregate_id=evidence.id,
            message_key=f"evidence.review_decided:{snapshot.id}:{request_key}",
            correlation_id=audit_context.correlation_id,
            payload={
                "evidence_audit_snapshot_id": str(snapshot.id),
                "evidence_asset_id": str(evidence.id),
                "review_decision_id": str(record.id),
                "review_subject_schema_version": (
                    record.review_subject_schema_version
                ),
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
