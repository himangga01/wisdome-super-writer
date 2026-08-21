from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

from .models import AuditEvent, RetentionBatch, RetentionBatchItem, RetentionHold


SUPPORTED_RETENTION_CATEGORIES = frozenset(
    {
        "raw_source",
        "raw_evidence",
        "unpublished_revision",
        "published_evidence_snapshot",
        "published_visualization_snapshot",
        "audit_event",
        "public_delivery_asset",
        "wordpress_media",
    }
)
SUPPORTED_RETENTION_SCOPES = frozenset(
    {
        "raw_evidence",
        "unpublished_drafts",
        "wordpress_media",
        "public_delivery_assets",
        "audit_expiry",
    }
)
RETENTION_CATEGORY_HANDLERS = {
    "raw_source": "source_text_tombstone",
    "raw_evidence": "exact_object_delete",
    "unpublished_revision": "manual_archive_required",
    "published_evidence_snapshot": "exact_object_delete",
    "published_visualization_snapshot": "exact_object_delete",
    "audit_event": "manual_archive_required",
    "public_delivery_asset": "public_delivery_delete_lease",
    "wordpress_media": "wordpress_media_delete_lease",
}


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    policy_code: str
    entity_type: str
    entity_id: object
    object_key: str | None
    object_version: str
    object_checksum: str
    byte_size: int
    dependency_manifest: tuple[dict, ...]
    candidate_hash: str


@dataclass(frozen=True, slots=True)
class RetentionExecutionOutcome:
    state: str
    reason_code: str = ""
    result_hash: str = ""
    cleanup_operation_id: str | None = None


def _candidate(
    *,
    policy_code: str,
    entity_type: str,
    entity_id,
    object_key: str | None = None,
    object_version: str | None = None,
    object_checksum: str | None = None,
    byte_size: int | None = None,
    dependency_manifest: list[dict] | tuple[dict, ...] = (),
) -> RetentionCandidate:
    dependencies = tuple(_sorted_dependency_manifest(dependency_manifest))
    material_hash = retention_candidate_hash(
        policy_code=policy_code,
        entity_type=entity_type,
        entity_id=entity_id,
        object_key=object_key,
        object_version=object_version,
        object_checksum=object_checksum,
        byte_size=int(byte_size or 0),
        dependency_manifest=dependencies,
    )
    return RetentionCandidate(
        policy_code=policy_code,
        entity_type=entity_type,
        entity_id=entity_id,
        object_key=object_key,
        object_version=object_version or "",
        object_checksum=object_checksum or "",
        byte_size=int(byte_size or 0),
        dependency_manifest=dependencies,
        candidate_hash=material_hash,
    )


def _sorted_dependency_manifest(rows: list[dict] | tuple[dict, ...]) -> list[dict]:
    normalized = [dict(row) for row in rows]
    return sorted(
        normalized,
        key=lambda row: json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def retention_candidate_hash(
    *,
    policy_code: str,
    entity_type: str,
    entity_id,
    object_key: str | None,
    object_version: str | None,
    object_checksum: str | None,
    byte_size: int,
    dependency_manifest: list[dict] | tuple[dict, ...],
) -> str:
    return canonical_hash(
        {
            "schemaVersion": "retention-candidate-v1",
            "policyCode": str(policy_code),
            "entityType": str(entity_type),
            "entityId": str(entity_id).lower(),
            "objectKey": object_key or None,
            "objectVersion": object_version or None,
            "objectChecksum": object_checksum or None,
            "byteSize": int(byte_size),
            "dependencyManifest": _sorted_dependency_manifest(dependency_manifest),
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def blocking_dependency_codes(dependency_manifest: list[dict] | tuple[dict, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(row.get("kind") or "unknown_dependency")
                for row in dependency_manifest
                if row.get("blocking") is True
            }
        )
    )


def _count_dependency(*, kind: str, queryset) -> dict | None:
    ids = [str(value) for value in queryset.order_by("id").values_list("id", flat=True)]
    if not ids:
        return None
    return {
        "kind": kind,
        "blocking": True,
        "count": len(ids),
        "manifestHash": canonical_hash(
            ids,
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        ),
    }


def _hold_dependencies(*, entity_type: str, entity_id) -> list[dict]:
    now = timezone.now()
    rows = (
        RetentionHold.objects.filter(active=True)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .filter(
            Q(scope_type=entity_type, scope_id=entity_id)
            | Q(scope_type=entity_type, scope_id__isnull=True)
            | Q(scope_type=f"evidence.{entity_type}", scope_id=entity_id)
        )
        .order_by("id")
    )
    return [
        {
            "kind": "legal_hold",
            "blocking": True,
            "id": str(row.id),
        }
        for row in rows
    ]


def _evidence_candidate(row) -> RetentionCandidate:
    dependencies = _hold_dependencies(
        entity_type="evidence_asset",
        entity_id=row.id,
    )
    claim_dependency = _count_dependency(
        kind="claim_reference",
        queryset=row.claim_links.all(),
    )
    if claim_dependency:
        dependencies.append(claim_dependency)
    published_dependency = _count_dependency(
        kind="published_snapshot",
        queryset=row.published_asset_snapshots.all(),
    )
    if published_dependency:
        dependencies.append(published_dependency)
    if row.source_item_id:
        from apps.editorial.models import CorrectionCase

        correction_dependency = _count_dependency(
            kind="open_correction",
            queryset=CorrectionCase.objects.filter(
                source_item_id=row.source_item_id,
                state__in=("detected", "verifying", "verified", "applying"),
            ),
        )
        if correction_dependency:
            dependencies.append(correction_dependency)
    if not (row.object_key and row.object_version and row.checksum and row.byte_size):
        dependencies.append(
            {
                "kind": "incomplete_object_identity",
                "blocking": True,
            }
        )
    return _candidate(
        policy_code="raw_evidence",
        entity_type="evidence_asset",
        entity_id=row.id,
        object_key=row.object_key,
        object_version=row.object_version,
        object_checksum=row.checksum,
        byte_size=row.byte_size,
        dependency_manifest=dependencies,
    )


def _source_candidate(row) -> RetentionCandidate:
    dependencies = _hold_dependencies(
        entity_type="source_item",
        entity_id=row.id,
    )
    from apps.editorial.models import CorrectionCase
    from apps.evidence.models import EvidenceAsset, EvidenceAuditSnapshot

    correction_dependency = _count_dependency(
        kind="open_correction",
        queryset=CorrectionCase.objects.filter(
            source_item_id=row.id,
            state__in=("detected", "verifying", "verified", "applying"),
        ),
    )
    if correction_dependency:
        dependencies.append(correction_dependency)
    evidence_ids = list(
        EvidenceAsset.objects.filter(source_item_id=row.id)
        .order_by("id")
        .values_list("id", flat=True)
    )
    if evidence_ids:
        snapshot_ids = set(
            EvidenceAuditSnapshot.objects.filter(
                original_evidence_asset_id__in=evidence_ids
            ).values_list("original_evidence_asset_id", flat=True)
        )
        missing = [str(value) for value in evidence_ids if value not in snapshot_ids]
        if missing:
            dependencies.append(
                {
                    "kind": "missing_evidence_audit_snapshot",
                    "blocking": True,
                    "count": len(missing),
                    "manifestHash": canonical_hash(
                        missing,
                        schema_version=CANONICAL_HASH_SCHEMA_V1,
                    ),
                }
            )
    dependencies.append(
        {
            "kind": "source_identity",
            "blocking": False,
            "contentHash": row.content_hash,
            "sourceVersionHash": row.source_version_hash,
        }
    )
    return _candidate(
        policy_code="raw_source",
        entity_type="source_item",
        entity_id=row.id,
        dependency_manifest=dependencies,
    )


def _manual_archive_candidate(*, policy_code: str, entity_type: str, entity_id) -> RetentionCandidate:
    return _candidate(
        policy_code=policy_code,
        entity_type=entity_type,
        entity_id=entity_id,
        dependency_manifest=(
            {
                "kind": "manual_archive_required",
                "blocking": True,
            },
        ),
    )


def _published_snapshot_dependencies(row) -> list[dict]:
    dependencies = _hold_dependencies(
        entity_type=row._meta.model_name,
        entity_id=row.id,
    )
    binding_dependency = _count_dependency(
        kind="publication_media_binding",
        queryset=row.publication_media_bindings.exclude(binding_state="removed"),
    )
    if binding_dependency:
        dependencies.append(binding_dependency)
    return dependencies


def _published_evidence_candidate(row) -> RetentionCandidate:
    return _candidate(
        policy_code="published_evidence_snapshot",
        entity_type="published_evidence_snapshot",
        entity_id=row.id,
        object_key=row.object_key,
        object_version=row.object_version,
        object_checksum=row.asset_checksum,
        byte_size=row.byte_size,
        dependency_manifest=_published_snapshot_dependencies(row),
    )


def _published_visualization_candidate(row) -> RetentionCandidate:
    return _candidate(
        policy_code="published_visualization_snapshot",
        entity_type="published_visualization_snapshot",
        entity_id=row.id,
        object_key=row.object_key,
        object_version=row.object_version,
        object_checksum=row.output_checksum,
        byte_size=row.byte_size,
        dependency_manifest=_published_snapshot_dependencies(row),
    )


def _public_delivery_candidate(row) -> RetentionCandidate:
    dependencies = _hold_dependencies(
        entity_type="public_delivery_asset",
        entity_id=row.id,
    )
    active = row.publicationmedia_set.exclude(binding_state="removed")
    binding_dependency = _count_dependency(
        kind="publication_media_binding",
        queryset=active,
    )
    if binding_dependency:
        dependencies.append(binding_dependency)
    if row.active_reference_count:
        dependencies.append(
            {
                "kind": "cached_active_reference",
                "blocking": True,
                "count": row.active_reference_count,
            }
        )
    dependencies.append(
        {
            "kind": "delivery_identity",
            "blocking": False,
            "presentationHash": row.presentation_hash,
            "leaseGeneration": row.lease_generation,
        }
    )
    return _candidate(
        policy_code="public_delivery_asset",
        entity_type="public_delivery_asset",
        entity_id=row.id,
        object_key=row.delivery_object_key,
        object_version=row.delivery_object_version,
        object_checksum=row.asset_checksum,
        byte_size=row.byte_size,
        dependency_manifest=dependencies,
    )


def _wordpress_media_candidate(row) -> RetentionCandidate:
    dependencies = _hold_dependencies(
        entity_type="remote_media",
        entity_id=row.id,
    )
    binding_dependency = _count_dependency(
        kind="publication_media_binding",
        queryset=row.publicationmedia_set.exclude(binding_state="removed"),
    )
    if binding_dependency:
        dependencies.append(binding_dependency)
    dependencies.append(
        {
            "kind": "remote_media_identity",
            "blocking": False,
            "remoteMediaId": row.remote_media_id,
            "requestFingerprint": row.request_fingerprint,
            "leaseGeneration": row.lease_generation,
        }
    )
    return _candidate(
        policy_code="wordpress_media",
        entity_type="remote_media",
        entity_id=row.id,
        dependency_manifest=dependencies,
    )


def _collect_retention_candidates(*, scope: str, cutoff_at) -> list[RetentionCandidate]:
    candidates: list[RetentionCandidate] = []
    if scope == "raw_evidence":
        from apps.collection.models import SourceItem
        from apps.evidence.models import EvidenceAsset

        candidates.extend(
            _source_candidate(row)
            for row in SourceItem.objects.filter(
                first_collected_at__lt=cutoff_at,
                retention_tombstoned_at__isnull=True,
            )
            .exclude(body_text="")
            .order_by("id")
        )
        candidates.extend(
            _evidence_candidate(row)
            for row in EvidenceAsset.objects.filter(created_at__lt=cutoff_at)
            .exclude(extracted_text__isnull=True, object_key__isnull=True)
            .order_by("id")
        )
    elif scope == "unpublished_drafts":
        from apps.editorial.models import ArticleRevision

        candidates.extend(
            _manual_archive_candidate(
                policy_code="unpublished_revision",
                entity_type="article_revision",
                entity_id=row.id,
            )
            for row in ArticleRevision.objects.filter(
                created_at__lt=cutoff_at,
                publication_intents__isnull=True,
            )
            .order_by("id")
            .distinct()
        )
    elif scope == "wordpress_media":
        from apps.publishing.models import RemoteMedia

        candidates.extend(
            _wordpress_media_candidate(row)
            for row in RemoteMedia.objects.filter(
                orphaned_at__lt=cutoff_at,
            )
            .exclude(state="deleted")
            .order_by("id")
        )
    elif scope == "public_delivery_assets":
        from apps.publishing.models import PublicDeliveryAsset

        candidates.extend(
            _public_delivery_candidate(row)
            for row in PublicDeliveryAsset.objects.filter(
                zero_reference_at__lt=cutoff_at,
            )
            .exclude(state="deleted")
            .order_by("id")
        )
    elif scope == "audit_expiry":
        from apps.publishing.models import (
            PublishedEvidenceSnapshot,
            PublishedVisualizationSnapshot,
        )

        candidates.extend(
            _published_evidence_candidate(row)
            for row in PublishedEvidenceSnapshot.objects.filter(
                created_at__lt=cutoff_at,
            ).order_by("id")
        )
        candidates.extend(
            _published_visualization_candidate(row)
            for row in PublishedVisualizationSnapshot.objects.filter(
                created_at__lt=cutoff_at,
            ).order_by("id")
        )
        candidates.extend(
            _manual_archive_candidate(
                policy_code="audit_event",
                entity_type="audit_event",
                entity_id=row.id,
            )
            for row in AuditEvent.objects.filter(occurred_at__lt=cutoff_at).order_by("id")
        )
    else:
        raise ValueError("unsupported_retention_scope")
    return sorted(
        candidates,
        key=lambda row: (row.policy_code, row.entity_type, str(row.entity_id)),
    )


def _candidate_for_item(item: RetentionBatchItem) -> RetentionCandidate:
    if item.entity_type == "source_item" and item.policy_code == "raw_source":
        from apps.collection.models import SourceItem

        return _source_candidate(SourceItem.objects.get(id=item.entity_id))
    if item.entity_type == "evidence_asset" and item.policy_code == "raw_evidence":
        from apps.evidence.models import EvidenceAsset

        return _evidence_candidate(EvidenceAsset.objects.get(id=item.entity_id))
    if item.entity_type == "article_revision" and item.policy_code == "unpublished_revision":
        return _manual_archive_candidate(
            policy_code=item.policy_code,
            entity_type=item.entity_type,
            entity_id=item.entity_id,
        )
    if item.entity_type == "audit_event" and item.policy_code == "audit_event":
        return _manual_archive_candidate(
            policy_code=item.policy_code,
            entity_type=item.entity_type,
            entity_id=item.entity_id,
        )
    if item.entity_type == "published_evidence_snapshot":
        from apps.publishing.models import PublishedEvidenceSnapshot

        return _published_evidence_candidate(
            PublishedEvidenceSnapshot.objects.get(id=item.entity_id)
        )
    if item.entity_type == "published_visualization_snapshot":
        from apps.publishing.models import PublishedVisualizationSnapshot

        return _published_visualization_candidate(
            PublishedVisualizationSnapshot.objects.get(id=item.entity_id)
        )
    if item.entity_type == "public_delivery_asset":
        from apps.publishing.models import PublicDeliveryAsset

        return _public_delivery_candidate(PublicDeliveryAsset.objects.get(id=item.entity_id))
    if item.entity_type == "remote_media":
        from apps.publishing.models import RemoteMedia

        return _wordpress_media_candidate(RemoteMedia.objects.get(id=item.entity_id))
    raise ValueError("unsupported_retention_candidate")


def _preview_request_hash(*, scope: str, cutoff_at, request_key: str, reason: str, user_id) -> str:
    return canonical_hash(
        {
            "schemaVersion": "retention-preview-request-v1",
            "scope": scope,
            "cutoffAt": cutoff_at.isoformat(),
            "requestKey": request_key,
            "reason": reason,
            "requestedBy": str(user_id),
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def verify_retention_object_precondition(*, item, storage):
    if not item.object_key or not item.object_version or not item.object_checksum:
        raise ValueError("stale_retention_candidate: incomplete object identity")
    observed = storage.head(
        key=item.object_key,
        version_id=item.object_version,
    )
    if (
        observed.version_id != item.object_version
        or observed.checksum_sha256 != item.object_checksum
        or observed.size != item.byte_size
    ):
        raise ValueError("stale_retention_candidate: object identity changed")
    return observed


def delete_exact_retention_object(*, item, storage) -> str:
    if not storage.version_exists(
        key=item.object_key,
        version_id=item.object_version,
    ):
        return canonical_hash(
            {
                "schemaVersion": "retention-delete-result-v1",
                "candidateHash": getattr(item, "candidate_hash", None),
                "objectKey": item.object_key,
                "objectVersion": item.object_version,
                "outcome": "already_absent",
            },
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        )
    verify_retention_object_precondition(item=item, storage=storage)
    try:
        storage.delete_version(
            key=item.object_key,
            version_id=item.object_version,
        )
    except Exception:
        remains = storage.version_exists(
            key=item.object_key,
            version_id=item.object_version,
        )
        if remains:
            raise
    else:
        remains = storage.version_exists(
            key=item.object_key,
            version_id=item.object_version,
        )
    if remains:
        raise ValueError("retention_object_delete_unconfirmed")
    return canonical_hash(
        {
            "schemaVersion": "retention-delete-result-v1",
            "candidateHash": getattr(item, "candidate_hash", None),
            "objectKey": item.object_key,
            "objectVersion": item.object_version,
            "outcome": "deleted",
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def execute_retention_candidate(*, item, storage) -> RetentionExecutionOutcome:
    handler = RETENTION_CATEGORY_HANDLERS.get(item.policy_code)
    if handler is None:
        raise ValueError("unsupported_retention_candidate")
    if handler == "manual_archive_required":
        return RetentionExecutionOutcome(
            state=RetentionBatchItem.State.HELD,
            reason_code="manual_archive_required",
        )
    if handler == "exact_object_delete":
        return RetentionExecutionOutcome(
            state=RetentionBatchItem.State.PURGED,
            result_hash=delete_exact_retention_object(
                item=item,
                storage=storage,
            ),
        )
    if handler == "source_text_tombstone":
        from apps.collection.models import SourceItem

        tombstoned_at = timezone.now()
        updated = SourceItem.objects.retention_tombstone(
            source_item_id=item.entity_id,
            tombstoned_at=tombstoned_at,
        )
        if updated != 1:
            raise ValueError("stale_retention_candidate")
        return RetentionExecutionOutcome(
            state=RetentionBatchItem.State.PURGED,
            result_hash=canonical_hash(
                {
                    "schemaVersion": "retention-source-tombstone-v1",
                    "candidateHash": item.candidate_hash,
                    "sourceItemId": str(item.entity_id),
                    "tombstonedAt": tombstoned_at.isoformat(),
                },
                schema_version=CANONICAL_HASH_SCHEMA_V1,
            ),
        )
    from apps.publishing.services import schedule_orphan_media_cleanup_locked

    if handler == "public_delivery_delete_lease":
        operation = schedule_orphan_media_cleanup_locked(
            public_delivery_asset_id=item.entity_id,
        )
    elif handler == "wordpress_media_delete_lease":
        operation = schedule_orphan_media_cleanup_locked(
            remote_media_id=item.entity_id,
        )
    else:
        raise ValueError("unsupported_retention_handler")
    return RetentionExecutionOutcome(
        state=RetentionBatchItem.State.DELETION_PENDING,
        reason_code=(
            "remote_cleanup_queued"
            if operation is not None
            else "remote_cleanup_grace_or_dependency"
        ),
        cleanup_operation_id=(str(operation.id) if operation is not None else None),
        result_hash=(
            canonical_hash(
                {
                    "schemaVersion": "retention-remote-cleanup-v1",
                    "candidateHash": getattr(item, "candidate_hash", None),
                    "operationId": str(operation.id),
                    "state": operation.state,
                },
                schema_version=CANONICAL_HASH_SCHEMA_V1,
            )
            if operation is not None
            else ""
        ),
    )


def load_retention_policy() -> dict:
    path = settings.BASE_DIR.parent / "config" / "retention" / "default.json"
    if not path.exists():
        path = Path("config/retention/default.json")
    policy = json.loads(path.read_text(encoding="utf-8"))
    categories = policy.get("categories")
    if not isinstance(categories, dict) or set(categories) != SUPPORTED_RETENTION_CATEGORIES:
        raise ValueError("retention policy categories are incomplete")
    return policy


def _is_held(entity_type: str, entity_id) -> bool:
    now = timezone.now()
    return (
        RetentionHold.objects.filter(scope_type=entity_type, active=True)
        .filter(Q(scope_id__isnull=True) | Q(scope_id=entity_id))
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .exists()
    )


def retention_authorization_request_hash(
    *,
    batch_id,
    expected_version: int,
    expected_preview_hash: str,
    authorization_request_key: str,
    authorization_reason: str,
    reauth_proof_id,
    actor_id,
) -> str:
    return canonical_hash(
        {
            "schemaVersion": "retention-authorization-v1",
            "batchId": str(batch_id),
            "expectedVersion": expected_version,
            "previewHash": expected_preview_hash,
            "requestKey": authorization_request_key,
            "reason": authorization_reason,
            "reauthProofId": str(reauth_proof_id),
            "actorId": str(actor_id),
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _retention_batch_audit_material(batch: RetentionBatch) -> dict:
    return {
        "id": str(batch.id),
        "state": batch.state,
        "version": batch.row_version,
        "scope": batch.scope,
        "previewHash": batch.preview_manifest_hash,
        "expectedItemCount": batch.expected_item_count,
        "counters": batch.counters,
    }


def _require_retention_admin_context(*, audit_context, user, request_key, reason):
    if audit_context is None:
        return
    if (
        audit_context.actor_type != "admin"
        or audit_context.actor_id != user.pk
        or audit_context.request_key != request_key
        or audit_context.reason_code != reason
    ):
        raise ValueError("retention audit context does not match request")


@transaction.atomic
def create_retention_preview(
    *,
    request_key: str,
    user,
    scope: str = "raw_evidence",
    cutoff_at=None,
    reason: str = "retention policy preview",
    audit_context=None,
) -> RetentionBatch:
    if scope not in SUPPORTED_RETENTION_SCOPES:
        raise ValueError("unsupported_retention_scope")
    if not isinstance(request_key, str) or not 8 <= len(request_key) <= 200:
        raise ValueError("invalid_retention_request_key")
    if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500:
        raise ValueError("invalid_retention_reason")
    reason = reason.strip()
    _require_retention_admin_context(
        audit_context=audit_context,
        user=user,
        request_key=request_key,
        reason=reason,
    )
    policy = load_retention_policy()
    if cutoff_at is None:
        cutoff_at = timezone.now() - timedelta(days=int(policy["rawEvidenceDays"]))
    if timezone.is_naive(cutoff_at):
        raise ValueError("retention_cutoff_must_be_timezone_aware")
    request_hash = _preview_request_hash(
        scope=scope,
        cutoff_at=cutoff_at,
        request_key=request_key,
        reason=reason,
        user_id=user.pk,
    )
    existing = RetentionBatch.objects.filter(request_key=request_key).first()
    if existing:
        if existing.request_hash != request_hash or existing.requested_by_id != user.pk:
            raise ValueError("retention_request_key_conflict")
        if audit_context is not None:
            from apps.audit.services import require_audit_replay

            require_audit_replay(
                context=audit_context,
                action="retention_batch.previewed",
                entity=existing,
                identity_key=request_key,
                request_hash=request_hash,
            )
        return existing
    candidates = _collect_retention_candidates(scope=scope, cutoff_at=cutoff_at)
    manifest = [
        {
            "policyCode": row.policy_code,
            "entityType": row.entity_type,
            "entityId": str(row.entity_id),
            "candidateHash": row.candidate_hash,
        }
        for row in candidates
    ]
    batch = RetentionBatch.objects.create(
        policy_version=int(policy["version"]),
        policy_hash=canonical_hash(
            policy, schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
        scope=scope,
        request_hash=request_hash,
        preview_reason=reason,
        cutoff_at=cutoff_at,
        request_key=request_key,
        preview_manifest_hash=canonical_hash(
            manifest, schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
        counters={
            "candidate": sum(not blocking_dependency_codes(row.dependency_manifest) for row in candidates),
            "held": sum(bool(blocking_dependency_codes(row.dependency_manifest)) for row in candidates),
            "purged": 0,
            "failed": 0,
        },
        expected_item_count=len(candidates),
        expected_byte_count=sum(row.byte_size for row in candidates),
        requested_by=user,
    )
    RetentionBatchItem.objects.bulk_create(
        [
            RetentionBatchItem(
                batch=batch,
                policy_code=row.policy_code,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                object_key=row.object_key,
                object_version=row.object_version,
                object_checksum=row.object_checksum,
                byte_size=row.byte_size,
                candidate_hash=row.candidate_hash,
                precondition_hash=row.candidate_hash,
                dependency_manifest=list(row.dependency_manifest),
                state=(
                    RetentionBatchItem.State.HELD
                    if blocking_dependency_codes(row.dependency_manifest)
                    else RetentionBatchItem.State.CANDIDATE
                ),
                reason_code=(
                    "blocking_dependency"
                    if blocking_dependency_codes(row.dependency_manifest)
                    else ""
                ),
                hold_reason=",".join(blocking_dependency_codes(row.dependency_manifest)),
            )
            for row in candidates
        ]
    )
    if audit_context is not None:
        from apps.audit.services import record_audit_event

        record_audit_event(
            context=audit_context,
            action="retention_batch.previewed",
            entity=batch,
            identity_key=request_key,
            material_schema_version="retention-batch-audit-v1",
            before_material={"state": "not_created"},
            after_material=_retention_batch_audit_material(batch),
            metadata={
                "request_hash": request_hash,
                "result": "created",
                "state": batch.state,
                "version": batch.row_version,
                "count": batch.expected_item_count,
            },
        )
    return batch


@transaction.atomic
def approve_retention_batch(
    batch_id,
    *,
    expected_version: int,
    expected_preview_hash: str | None = None,
    authorized_by=None,
    authorization_request_key: str | None = None,
    authorization_reason: str | None = None,
    reauth_proof_id=None,
    storage=None,
    audit_context=None,
) -> RetentionBatch:
    if storage is None:
        from adapters.storage.s3 import S3ObjectStorage

        storage = S3ObjectStorage()
    batch = RetentionBatch.objects.select_for_update().get(id=batch_id)
    _require_retention_admin_context(
        audit_context=audit_context,
        user=authorized_by or batch.requested_by,
        request_key=authorization_request_key,
        reason=authorization_reason,
    )
    before_material = _retention_batch_audit_material(batch)
    if (
        batch.row_version != expected_version
        or batch.state != RetentionBatch.State.PREVIEW
        or (
            expected_preview_hash is not None
            and batch.preview_manifest_hash != expected_preview_hash
        )
    ):
        raise ValueError("stale_retention_batch")
    items = list(batch.items.select_for_update().order_by("id"))
    for item in items:
        current = _candidate_for_item(item)
        if current.candidate_hash != item.candidate_hash:
            raise ValueError("stale_retention_candidate")
        if item.state == RetentionBatchItem.State.CANDIDATE and item.object_key:
            verify_retention_object_precondition(item=item, storage=storage)
    batch.state = RetentionBatch.State.APPROVED
    batch.row_version += 1
    batch.approved_at = timezone.now()
    batch.authorized_by = authorized_by or batch.requested_by
    batch.reauth_proof_id = reauth_proof_id
    batch.authorization_request_key = authorization_request_key
    batch.authorization_reason = authorization_reason
    batch.save(
        update_fields=[
            "state",
            "row_version",
            "approved_at",
            "authorized_by",
            "reauth_proof_id",
            "authorization_request_key",
            "authorization_reason",
        ]
    )
    if audit_context is not None:
        from apps.audit.services import record_audit_event

        request_hash = retention_authorization_request_hash(
            batch_id=batch.id,
            expected_version=expected_version,
            expected_preview_hash=(
                expected_preview_hash or batch.preview_manifest_hash
            ),
            authorization_request_key=authorization_request_key,
            authorization_reason=authorization_reason,
            reauth_proof_id=reauth_proof_id,
            actor_id=batch.authorized_by_id,
        )
        record_audit_event(
            context=audit_context,
            action="retention_batch.authorized",
            entity=batch,
            identity_key=authorization_request_key,
            material_schema_version="retention-batch-audit-v1",
            before_material=before_material,
            after_material=_retention_batch_audit_material(batch),
            metadata={
                "request_hash": request_hash,
                "result": "authorized",
                "state": batch.state,
                "version": batch.row_version,
                "count": batch.expected_item_count,
                "reauth_proof_id": str(reauth_proof_id),
            },
        )
    return batch


@transaction.atomic
def resume_failed_retention_batch(
    batch_id,
    *,
    expected_version: int,
    expected_preview_hash: str,
    authorized_by,
    authorization_request_key: str,
    authorization_reason: str,
    reauth_proof_id,
    storage=None,
    audit_context=None,
) -> RetentionBatch:
    if storage is None:
        from adapters.storage.s3 import S3ObjectStorage

        storage = S3ObjectStorage()
    batch = RetentionBatch.objects.select_for_update().get(id=batch_id)
    _require_retention_admin_context(
        audit_context=audit_context,
        user=authorized_by,
        request_key=authorization_request_key,
        reason=authorization_reason,
    )
    before_material = _retention_batch_audit_material(batch)
    if (
        batch.state != RetentionBatch.State.FAILED
        or batch.row_version != expected_version
        or batch.preview_manifest_hash != expected_preview_hash
    ):
        raise ValueError("stale_retention_batch")
    failed_items = list(
        batch.items.select_for_update()
        .filter(state=RetentionBatchItem.State.FAILED)
        .order_by("id")
    )
    if not failed_items:
        raise ValueError("failed_retention_batch_has_no_failed_items")
    for item in failed_items:
        current = _candidate_for_item(item)
        if current.candidate_hash != item.candidate_hash:
            item.state = RetentionBatchItem.State.HELD
            item.reason_code = "dependency_changed_before_resume"
            item.hold_reason = ",".join(
                blocking_dependency_codes(current.dependency_manifest)
            )
        else:
            if item.object_key:
                verify_retention_object_precondition(item=item, storage=storage)
            item.state = RetentionBatchItem.State.CANDIDATE
            item.lease_generation += 1
            item.reason_code = ""
            item.hold_reason = ""
        item.result_hash = ""
        item.error_code = ""
        item.error_detail_redacted = ""
        item.remediation = ""
        item.processed_at = None
        item.tombstone_at = None
        item.cleanup_operation_id = None
        item.save(
            update_fields=(
                "state",
                "lease_generation",
                "reason_code",
                "hold_reason",
                "result_hash",
                "error_code",
                "error_detail_redacted",
                "remediation",
                "processed_at",
                "tombstone_at",
                "cleanup_operation_id",
            )
        )
    counts = {
        state: batch.items.filter(state=state).count()
        for state in RetentionBatchItem.State.values
    }
    open_count = counts[RetentionBatchItem.State.CANDIDATE] + counts[
        RetentionBatchItem.State.DELETION_PENDING
    ]
    batch.state = RetentionBatch.State.APPROVED
    batch.row_version += 1
    batch.approved_at = timezone.now()
    batch.authorized_by = authorized_by
    batch.reauth_proof_id = reauth_proof_id
    batch.authorization_request_key = authorization_request_key
    batch.authorization_reason = authorization_reason
    batch.counters = {
        "candidate": counts[RetentionBatchItem.State.CANDIDATE],
        "deletionPending": counts[RetentionBatchItem.State.DELETION_PENDING],
        "held": counts[RetentionBatchItem.State.HELD],
        "purged": counts[RetentionBatchItem.State.PURGED],
        "skipped": counts[RetentionBatchItem.State.SKIPPED],
        "failed": counts[RetentionBatchItem.State.FAILED],
    }
    batch.processed_count = sum(counts.values()) - open_count
    batch.skipped_hold_count = counts[RetentionBatchItem.State.HELD]
    batch.failed_count = counts[RetentionBatchItem.State.FAILED]
    batch.completed_at = None
    batch.failure_cursor = None
    batch.error_code = None
    batch.error_detail_redacted = None
    batch.remediation = None
    batch.save(
        update_fields=(
            "state",
            "row_version",
            "approved_at",
            "authorized_by",
            "reauth_proof_id",
            "authorization_request_key",
            "authorization_reason",
            "counters",
            "processed_count",
            "skipped_hold_count",
            "failed_count",
            "completed_at",
            "failure_cursor",
            "error_code",
            "error_detail_redacted",
            "remediation",
        )
    )
    if audit_context is not None:
        from apps.audit.services import record_audit_event

        request_hash = retention_authorization_request_hash(
            batch_id=batch.id,
            expected_version=expected_version,
            expected_preview_hash=expected_preview_hash,
            authorization_request_key=authorization_request_key,
            authorization_reason=authorization_reason,
            reauth_proof_id=reauth_proof_id,
            actor_id=batch.authorized_by_id,
        )
        record_audit_event(
            context=audit_context,
            action="retention_batch.authorized",
            entity=batch,
            identity_key=authorization_request_key,
            material_schema_version="retention-batch-audit-v1",
            before_material=before_material,
            after_material=_retention_batch_audit_material(batch),
            metadata={
                "request_hash": request_hash,
                "result": "resumed",
                "state": batch.state,
                "version": batch.row_version,
                "count": batch.expected_item_count,
                "reauth_proof_id": str(reauth_proof_id),
            },
        )
    return batch


def _project_retention_batch_locked(batch: RetentionBatch) -> RetentionBatch:
    counts = {
        state: batch.items.filter(state=state).count()
        for state in RetentionBatchItem.State.values
    }
    open_count = counts[RetentionBatchItem.State.CANDIDATE] + counts[
        RetentionBatchItem.State.DELETION_PENDING
    ]
    batch.counters = {
        "candidate": counts[RetentionBatchItem.State.CANDIDATE],
        "deletionPending": counts[RetentionBatchItem.State.DELETION_PENDING],
        "held": counts[RetentionBatchItem.State.HELD],
        "purged": counts[RetentionBatchItem.State.PURGED],
        "skipped": counts[RetentionBatchItem.State.SKIPPED],
        "failed": counts[RetentionBatchItem.State.FAILED],
    }
    batch.processed_count = sum(counts.values()) - open_count
    batch.skipped_hold_count = counts[RetentionBatchItem.State.HELD]
    batch.failed_count = counts[RetentionBatchItem.State.FAILED]
    if open_count:
        batch.state = RetentionBatch.State.RUNNING
        batch.completed_at = None
    else:
        batch.state = (
            RetentionBatch.State.FAILED
            if batch.failed_count
            else RetentionBatch.State.COMPLETED
        )
        batch.completed_at = timezone.now()
    batch.save(
        update_fields=(
            "counters",
            "processed_count",
            "skipped_hold_count",
            "failed_count",
            "state",
            "completed_at",
        )
    )
    return batch


@transaction.atomic
def finalize_retention_media_cleanup(*, operation=None, operation_id=None) -> None:
    if operation is None:
        from apps.publishing.models import MediaDeliveryOperation

        operation = MediaDeliveryOperation.objects.get(id=operation_id)
    batch_ids = sorted(
        set(
            RetentionBatchItem.objects.filter(
                cleanup_operation_id=operation.id,
                state=RetentionBatchItem.State.DELETION_PENDING,
            ).values_list("batch_id", flat=True)
        ),
        key=str,
    )
    batches = {
        row.id: row
        for row in RetentionBatch.objects.select_for_update()
        .filter(id__in=batch_ids)
        .order_by("id")
    }
    items = list(
        RetentionBatchItem.objects.select_for_update().filter(
            cleanup_operation_id=operation.id,
            state=RetentionBatchItem.State.DELETION_PENDING,
        )
        .order_by("id")
    )
    for item in items:
        if operation.state == "succeeded":
            item.state = RetentionBatchItem.State.PURGED
            item.tombstone_at = timezone.now()
            item.result_hash = operation.result_hash
            item.reason_code = "remote_cleanup_succeeded"
        elif operation.state in {
            "manual_required",
            "delivery_failed",
        }:
            item.state = RetentionBatchItem.State.FAILED
            item.error_code = str(operation.error_code or operation.state)[:100]
            item.error_detail_redacted = "Remote cleanup requires remediation."
            item.processed_at = timezone.now()
        elif operation.state == "superseded":
            item.state = RetentionBatchItem.State.HELD
            item.reason_code = "remote_reference_restored"
            item.hold_reason = "Remote media was referenced again before deletion."
            item.processed_at = timezone.now()
        else:
            continue
        item.processed_at = item.processed_at or timezone.now()
        item.save(
            update_fields=(
                "state",
                "tombstone_at",
                "result_hash",
                "reason_code",
                "hold_reason",
                "error_code",
                "error_detail_redacted",
                "processed_at",
            )
        )
    for batch in batches.values():
        _project_retention_batch_locked(batch)


@transaction.atomic
def execute_retention_batch(batch_id, *, storage=None) -> RetentionBatch:
    from apps.evidence.models import EvidenceAsset

    if storage is None:
        from adapters.storage.s3 import S3ObjectStorage

        storage = S3ObjectStorage()

    batch = RetentionBatch.objects.select_for_update().get(id=batch_id)
    if batch.state not in {RetentionBatch.State.APPROVED, RetentionBatch.State.RUNNING}:
        return batch
    batch.state = RetentionBatch.State.RUNNING
    batch.started_at = batch.started_at or timezone.now()
    batch.save(update_fields=["state", "started_at"])
    held = purged = failed = 0
    for item in batch.items.select_for_update().filter(state=RetentionBatchItem.State.CANDIDATE):
        current = _candidate_for_item(item)
        if current.candidate_hash != item.candidate_hash:
            item.state = RetentionBatchItem.State.HELD
            item.reason_code = "dependency_changed_after_preview"
            item.hold_reason = ",".join(blocking_dependency_codes(current.dependency_manifest))
            held += 1
        else:
            try:
                outcome = execute_retention_candidate(item=item, storage=storage)
                item.result_hash = outcome.result_hash
                item.reason_code = outcome.reason_code
                item.cleanup_operation_id = outcome.cleanup_operation_id
                if item.entity_type == "evidence_asset" and outcome.state == RetentionBatchItem.State.PURGED:
                    EvidenceAsset.objects.filter(id=item.entity_id).update(
                        extracted_text=None, structured_data=None, object_key=None, object_version=None
                    )
                item.state = outcome.state
                if outcome.state == RetentionBatchItem.State.PURGED:
                    item.tombstone_at = timezone.now()
                    purged += 1
            except Exception as exc:
                item.state = RetentionBatchItem.State.FAILED
                item.reason_code = "purge_failed"
                item.error_code = type(exc).__name__[:100]
                item.error_detail_redacted = "Retention candidate could not be purged."
                failed += 1
        item.processed_at = timezone.now()
        item.save(
            update_fields=[
                "state",
                "reason_code",
                "hold_reason",
                "result_hash",
                "error_code",
                "error_detail_redacted",
                "processed_at",
                "tombstone_at",
                "cleanup_operation_id",
            ]
        )
    return _project_retention_batch_locked(batch)


@shared_task
def execute_retention_batch_task(batch_id: str):
    batch = execute_retention_batch(batch_id)
    return {"batchId": str(batch.id), "state": batch.state, "counters": batch.counters}
