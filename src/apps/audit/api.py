import json
import hashlib
from datetime import datetime
from uuid import UUID

from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest, JsonResponse
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from wisdome_writer.api.openapi import openapi_operation
from wisdome_writer.domain.errors import InvalidInput, StateConflict
from wisdome_writer.infrastructure.outbox import enqueue_event

from .cursor import cursor_key, decode_cursor, encode_cursor
from apps.accounts.services import consume_reauthentication_proof

from .models import AuditEvent, RetentionBatch, RetentionBatchItem
from .redaction import sanitize_reason, validate_stored_metadata
from .retention import (
    approve_retention_batch,
    create_retention_preview,
    resume_failed_retention_batch,
    retention_authorization_request_hash,
)
from .services import AuditContext, require_audit_replay

PAGE_SIZE = 50


def _iso(value):
    return value.isoformat() if value else None


def _serialize(event: AuditEvent) -> dict:
    try:
        metadata = validate_stored_metadata(
            action=event.action,
            metadata_schema_version=event.metadata_schema_version,
            redaction_policy_version=event.redaction_policy_version,
            redaction_policy_hash_value=event.redaction_policy_hash,
            metadata=event.metadata_redacted,
        )
        reason_code = sanitize_reason(event.reason_code)
    except ValueError:
        metadata = {"error_code": "redaction_validation_failed"}
        reason_code = None
    return {
        "id": str(event.pk),
        "occurredAt": _iso(event.occurred_at),
        "correlationId": str(event.correlation_id),
        "actorType": event.actor_type,
        "actorId": str(event.actor_id) if event.actor_id else None,
        "action": event.action,
        "entityType": event.entity_type,
        "entityId": str(event.entity_id),
        "beforeHash": event.before_hash,
        "afterHash": event.after_hash,
        "reasonCode": reason_code,
        "metadataSchemaVersion": event.metadata_schema_version,
        "redactionPolicyVersion": event.redaction_policy_version,
        "redactionPolicyHash": event.redaction_policy_hash,
        "metadataRedacted": metadata,
    }


@login_required
@openapi_operation("listAuditEvents")
def list_audit_events(request: HttpRequest) -> JsonResponse:
    query = request.openapi_query
    filters = {
        key: query[key]
        for key in (
            "actorType",
            "actorId",
            "correlationId",
            "entityType",
            "entityId",
            "action",
            "from",
            "to",
        )
        if query.get(key) not in (None, "")
    }
    queryset = AuditEvent.objects.all()
    try:
        if value := filters.get("actorType"):
            queryset = queryset.filter(actor_type=value)
        if value := filters.get("actorId"):
            queryset = queryset.filter(actor_id=UUID(str(value)))
        if value := filters.get("correlationId"):
            queryset = queryset.filter(correlation_id=UUID(str(value)))
        if value := filters.get("entityType"):
            queryset = queryset.filter(entity_type=value)
        if value := filters.get("entityId"):
            queryset = queryset.filter(entity_id=UUID(str(value)))
        if value := filters.get("action"):
            queryset = queryset.filter(action=value)
        if value := filters.get("from"):
            parsed = parse_datetime(str(value))
            if parsed is None:
                raise ValueError
            queryset = queryset.filter(occurred_at__gte=parsed)
        if value := filters.get("to"):
            parsed = parse_datetime(str(value))
            if parsed is None:
                raise ValueError
            queryset = queryset.filter(occurred_at__lt=parsed)
    except (ValueError, TypeError) as exc:
        raise InvalidInput("Audit filters contain an invalid UUID or timestamp") from exc

    cursor_value = query.get("cursor")
    watermark: tuple[datetime, UUID] | None = None
    if cursor_value is not None:
        cursor = decode_cursor(
            cursor_value,
            filters=filters,
            limit=PAGE_SIZE,
        )
        watermark = cursor_key(cursor.watermark)
        last = cursor_key(cursor.position)
        queryset = queryset.filter(
            Q(occurred_at__lt=watermark[0])
            | Q(occurred_at=watermark[0], id__lte=watermark[1])
        ).filter(Q(occurred_at__lt=last[0]) | Q(occurred_at=last[0], id__lt=last[1]))

    events = list(queryset.order_by("-occurred_at", "-id")[: PAGE_SIZE + 1])
    page = events[:PAGE_SIZE]
    if watermark is None and page:
        watermark = (page[0].occurred_at, page[0].pk)
    next_cursor = None
    if len(events) > PAGE_SIZE and page and watermark:
        tail = page[-1]
        next_cursor = encode_cursor(
            filters=filters,
            watermark=(watermark[0], str(watermark[1])),
            last=(tail.occurred_at, str(tail.pk)),
            limit=PAGE_SIZE,
        )
    return JsonResponse({"items": [_serialize(event) for event in page], "nextCursor": next_cursor})


def _retention_payload(batch: RetentionBatch) -> dict:
    return {
        "id": str(batch.id),
        "policyVersion": batch.policy_version,
        "policyHash": batch.policy_hash,
        "scope": batch.scope,
        "cutoffAt": batch.cutoff_at.isoformat(),
        "previewReason": batch.preview_reason,
        "authorizationReason": batch.authorization_reason,
        "state": batch.state,
        "version": batch.row_version,
        "previewHash": batch.preview_manifest_hash,
        "counters": batch.counters,
        "expectedItemCount": batch.expected_item_count,
        "expectedByteCount": batch.expected_byte_count,
        "processedCount": batch.processed_count,
        "skippedHoldCount": batch.skipped_hold_count,
        "failedCount": batch.failed_count,
        "failureCursor": batch.failure_cursor,
        "errorCode": batch.error_code,
        "errorDetailRedacted": batch.error_detail_redacted,
        "remediation": batch.remediation,
        "requestedBy": str(batch.requested_by_id),
        "authorizedBy": (
            str(batch.authorized_by_id) if batch.authorized_by_id else None
        ),
        "reauthProofId": (
            str(batch.reauth_proof_id) if batch.reauth_proof_id else None
        ),
        "authorizationRequestKey": batch.authorization_request_key,
        "createdAt": batch.created_at.isoformat(),
        "authorizedAt": _iso(batch.approved_at),
        "startedAt": _iso(batch.started_at),
        "completedAt": _iso(batch.completed_at),
    }


def _retention_item_payload(item: RetentionBatchItem) -> dict:
    object_key_redacted = None
    if item.object_key:
        object_key_redacted = "sha256:" + hashlib.sha256(
            item.object_key.encode("utf-8")
        ).hexdigest()[:16]
    return {
        "id": str(item.id),
        "entityType": item.entity_type,
        "entityId": str(item.entity_id),
        "policyCode": item.policy_code,
        "objectKeyRedacted": object_key_redacted,
        "objectVersion": item.object_version or None,
        "objectChecksum": item.object_checksum or None,
        "byteSize": item.byte_size,
        "preconditionHash": item.precondition_hash,
        "dependencyManifest": item.dependency_manifest[:100],
        "leaseGeneration": item.lease_generation,
        "state": item.state,
        "reasonCode": item.reason_code or None,
        "holdReason": item.hold_reason or None,
        "resultHash": item.result_hash or None,
        "errorCode": item.error_code or None,
        "errorDetailRedacted": item.error_detail_redacted or None,
        "remediation": item.remediation or None,
        "processedAt": _iso(item.processed_at),
        "tombstoneAt": _iso(item.tombstone_at),
    }


_RETENTION_CURSOR_SALT = "wisdome.retention.items.v1"


def _retention_cursor(*, batch_id, last_id) -> str:
    return signing.dumps(
        {"batchId": str(batch_id), "lastId": str(last_id)},
        salt=_RETENTION_CURSOR_SALT,
        compress=True,
    )


def _decode_retention_cursor(value: str, *, batch_id):
    try:
        payload = signing.loads(
            value,
            salt=_RETENTION_CURSOR_SALT,
            max_age=86400,
        )
        if payload.get("batchId") != str(batch_id):
            raise ValueError
        return UUID(str(payload["lastId"]))
    except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
        raise InvalidInput("retention cursor is invalid or expired") from exc


@login_required
@openapi_operation("previewRetentionBatch")
@require_http_methods(["POST"])
def retention_previews(request: HttpRequest) -> JsonResponse:
    body = request.openapi_body
    audit_context = AuditContext.for_admin(
        request=request,
        reason_code=body["reason"],
        request_key=body["requestKey"],
    )
    cutoff = parse_datetime(body["cutoffAt"])
    if cutoff is None:
        raise InvalidInput("retention cutoffAt is invalid")
    existed = RetentionBatch.objects.filter(
        request_key=body["requestKey"]
    ).exists()
    try:
        batch = create_retention_preview(
            request_key=body["requestKey"],
            user=request.user,
            scope=body["scope"],
            cutoff_at=cutoff,
            reason=body["reason"],
            audit_context=audit_context,
        )
    except ValueError as exc:
        if "conflict" in str(exc):
            raise StateConflict(str(exc)) from exc
        raise InvalidInput(str(exc)) from exc
    return JsonResponse(_retention_payload(batch), status=200 if existed else 201)


@login_required
@openapi_operation("getRetentionBatch")
@require_http_methods(["GET"])
def retention_batch_detail(request: HttpRequest, retention_batch_id) -> JsonResponse:
    batch = get_object_or_404(RetentionBatch, id=retention_batch_id)
    return JsonResponse(_retention_payload(batch))


@login_required
@openapi_operation("listRetentionBatchItems")
@require_http_methods(["GET"])
def retention_batch_items(request: HttpRequest, retention_batch_id) -> JsonResponse:
    batch = get_object_or_404(RetentionBatch, id=retention_batch_id)
    queryset = batch.items.order_by("id")
    if request.openapi_query.get("cursor"):
        last_id = _decode_retention_cursor(
            request.openapi_query["cursor"],
            batch_id=batch.id,
        )
        queryset = queryset.filter(id__gt=last_id)
    rows = list(queryset[:51])
    page = rows[:50]
    next_cursor = (
        _retention_cursor(batch_id=batch.id, last_id=page[-1].id)
        if len(rows) > 50 and page
        else None
    )
    return JsonResponse(
        {
            "batchId": str(batch.id),
            "previewHash": batch.preview_manifest_hash,
            "items": [_retention_item_payload(row) for row in page],
            "nextCursor": next_cursor,
        }
    )


@login_required
@openapi_operation("executeRetentionBatch")
@require_http_methods(["POST"])
def execute_retention(request: HttpRequest, retention_batch_id) -> JsonResponse:
    body = request.openapi_body
    audit_context = AuditContext.for_admin(
        request=request,
        reason_code=body["reason"],
        request_key=body["requestKey"],
    )
    newly_authorized = False
    with transaction.atomic():
        batch = get_object_or_404(
            RetentionBatch.objects.select_for_update(),
            id=retention_batch_id,
        )
        if batch.state in {
            RetentionBatch.State.PREVIEW,
            RetentionBatch.State.FAILED,
        }:
            consume_reauthentication_proof(
                request=request,
                proof_id=body["reauthProofId"],
                action_scope="retention_execute",
                entity_type="retention_batch",
                entity_id=batch.id,
            )
            try:
                authorize = (
                    approve_retention_batch
                    if batch.state == RetentionBatch.State.PREVIEW
                    else resume_failed_retention_batch
                )
                batch = authorize(
                    batch.id,
                    expected_version=body["expectedVersion"],
                    expected_preview_hash=body["previewHash"],
                    authorized_by=request.user,
                    authorization_request_key=body["requestKey"],
                    authorization_reason=body["reason"],
                    reauth_proof_id=body["reauthProofId"],
                    audit_context=audit_context,
                )
            except ValueError as exc:
                raise StateConflict(str(exc)) from exc
            newly_authorized = True
        else:
            replay_matches = (
                batch.preview_manifest_hash == body["previewHash"]
                and batch.authorization_request_key == body["requestKey"]
                and batch.authorization_reason == body["reason"]
                and str(batch.reauth_proof_id) == body["reauthProofId"]
                and batch.authorized_by_id == request.user.pk
                and batch.row_version == body["expectedVersion"] + 1
            )
            if not replay_matches:
                raise StateConflict("retention authorization request conflicts with current state")
            require_audit_replay(
                context=audit_context,
                action="retention_batch.authorized",
                entity=batch,
                identity_key=body["requestKey"],
                request_hash=retention_authorization_request_hash(
                    batch_id=batch.id,
                    expected_version=body["expectedVersion"],
                    expected_preview_hash=body["previewHash"],
                    authorization_request_key=body["requestKey"],
                    authorization_reason=body["reason"],
                    reauth_proof_id=body["reauthProofId"],
                    actor_id=request.user.pk,
                ),
            )
        if batch.state == RetentionBatch.State.APPROVED:
            enqueue_event(
                event_type="retention.expire_requested",
                aggregate_type="retention_batch",
                aggregate_id=batch.id,
                job_id=batch.id,
                dedupe_key=(
                    f"retention.expire_requested:{batch.id}:{batch.row_version}"
                ),
                payload={"retention_batch_id": str(batch.id)},
            )
    batch.refresh_from_db()
    return JsonResponse(
        _retention_payload(batch),
        status=202 if newly_authorized else 200,
    )
