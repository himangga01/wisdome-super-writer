import json
from datetime import datetime
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest, JsonResponse
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from wisdome_writer.api.openapi import openapi_operation
from wisdome_writer.domain.errors import InvalidInput
from wisdome_writer.infrastructure.outbox import enqueue_event

from .cursor import cursor_key, decode_cursor, encode_cursor
from apps.accounts.services import consume_reauthentication_proof

from .models import AuditEvent, RetentionBatch
from .redaction import sanitize_reason, validate_stored_metadata
from .retention import approve_retention_batch, create_retention_preview

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
        for key in ("correlationId", "entityType", "entityId", "action", "from", "to")
        if query.get(key) not in (None, "")
    }
    queryset = AuditEvent.objects.all()
    try:
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
        "cutoffAt": batch.cutoff_at.isoformat(),
        "state": batch.state,
        "rowVersion": batch.row_version,
        "previewManifestHash": batch.preview_manifest_hash,
        "counters": batch.counters,
    }


@login_required
@require_http_methods(["GET", "POST"])
def retention_batches(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        return JsonResponse({"items": [_retention_payload(row) for row in RetentionBatch.objects.order_by("-created_at")[:50]]})
    body = json.loads(request.body or b"{}")
    batch = create_retention_preview(request_key=body["requestKey"], user=request.user)
    return JsonResponse(_retention_payload(batch), status=201)


@login_required
@require_http_methods(["POST"])
@transaction.atomic
def approve_retention(request: HttpRequest, batch_id) -> JsonResponse:
    batch = get_object_or_404(RetentionBatch, id=batch_id)
    body = json.loads(request.body or b"{}")
    consume_reauthentication_proof(
        request=request,
        proof_id=body["reauthProofId"],
        action_scope="retention_execute",
        entity_type="retention_batch",
        entity_id=batch.id,
    )
    try:
        batch = approve_retention_batch(batch.id, expected_version=body["expectedVersion"])
    except ValueError:
        transaction.set_rollback(True)
        return JsonResponse({"detail": "stale_retention_batch"}, status=409)
    return JsonResponse(_retention_payload(batch))


@login_required
@require_http_methods(["POST"])
def execute_retention(request: HttpRequest, batch_id) -> JsonResponse:
    with transaction.atomic():
        batch = get_object_or_404(RetentionBatch, id=batch_id, state=RetentionBatch.State.APPROVED)
        enqueue_event(
            event_type="retention.expire_requested",
            aggregate_type="retention_batch",
            aggregate_id=batch.id,
            job_id=batch.id,
            dedupe_key=f"retention.expire_requested:{batch.id}:{batch.row_version}",
            payload={"retention_batch_id": str(batch.id)},
        )
    return JsonResponse(_retention_payload(batch), status=202)
