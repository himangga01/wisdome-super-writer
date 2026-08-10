from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import F, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from adapters.sources import (
    build_source_adapter,
    ReconciliationSourceRecord,
    source_adapter_implementation_manifest_hash,
    source_adapter_key,
    source_adapter_version,
    source_reconciliation_days,
)
from adapters.sources.errors import SourceAccessError
from apps.topics.models import SourceDefinition, SourceRegistrySnapshot, TopicPolicy
from apps.topics.services import (
    current_registry,
    registry_manifest_hash as calculate_registry_manifest_hash,
)
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)
from wisdome_writer.infrastructure.outbox import enqueue_event
from wisdome_writer.infrastructure.event_routes import (
    SOURCE_COLLECTION_MAX_ATTEMPTS,
)
from wisdome_writer.observability import (
    current_correlation_uuid,
    current_task_id,
    elapsed_milliseconds,
)

from .models import (
    CollectionRun,
    RecoveryState,
    RunSourceItem,
    RunState,
    RunStep,
    SourceCollectionAttempt,
    SourceCollectionAttemptState,
    SourceCollectionFailureCategory,
    SourceCollectionObservation,
    SourceDiscoveryKind,
    SourceItem,
    SourceItemStatus,
)


class _CollectionStopRequested(Exception):
    pass


class _CollectionRunProgressed(Exception):
    pass


class _CollectionReplayConflict(Exception):
    pass


def _hash(value) -> str:
    return canonical_hash(
        value,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _bounded_count(value: int) -> int:
    return min(max(int(value), 0), 2147483647)


def _collection_run_request_fingerprint(
    *,
    topic_code: str,
    window_start: datetime,
    window_end: datetime,
    trigger: str,
    registry_id,
    registry_hash: str,
    policy_id,
    policy_version: int,
    policy_hash: str,
    freshness_minutes: int,
    allowed_authority_tiers: list[str],
    requested_target_ids: list[str] | None = None,
    approval_mode: str = "manual",
    execution_material_hash: str | None = None,
) -> str:
    """Build the immutable identity for one collection-run request."""

    material = {
        "topic": topic_code,
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
        "trigger": trigger,
        "registry": str(registry_id),
        "registryHash": registry_hash,
        "topicPolicy": str(policy_id),
        "policyVersion": policy_version,
        "policyHash": policy_hash,
        "freshnessMinutes": freshness_minutes,
        "allowedAuthorityTiers": allowed_authority_tiers,
    }
    if (
        execution_material_hash is not None
        or requested_target_ids
        or approval_mode != "manual"
    ):
        material.update(
            {
                "requestedTargetIds": sorted(
                    str(value) for value in (requested_target_ids or [])
                ),
                "approvalMode": approval_mode,
                "executionMaterialHash": execution_material_hash,
            }
        )
    return _hash(material)


def _terminal_impact(
    *,
    scope: str,
    stage: str,
    final_state: str,
    affected_count: int,
    error_code: str | None,
) -> dict:
    return {
        "scope": scope,
        "stage": str(stage)[:64],
        "final_state": str(final_state)[:32],
        "affected_count": _bounded_count(affected_count),
        "error_code": (
            str(error_code)[:100]
            if error_code
            else None
        ),
    }


def begin_step_observation(
    step: RunStep,
    run: CollectionRun,
    *,
    started_at,
) -> None:
    step.correlation_id = run.correlation_id
    task_id = current_task_id()
    if task_id:
        step.worker_task_id = str(task_id)[:255]
    step.retry_count = _bounded_count(
        max(
            step.retry_count,
            step.attempt_no - 1,
        )
    )
    step.started_at = step.started_at or started_at
    step.finished_at = None
    step.duration_ms = None
    step.retry_at = None
    step.terminal_impact = {}
    step.recovery_state = (
        RecoveryState.AUTOMATIC_RETRY
        if step.retry_count
        else RecoveryState.IN_PROGRESS
    )


def project_step_terminal_observation(
    step: RunStep,
    run: CollectionRun,
    *,
    finished_at,
    final_state: str,
    affected_count: int = 0,
    error_code: str | None = None,
    recovery_state: str = RecoveryState.NOT_REQUIRED,
) -> None:
    step.correlation_id = run.correlation_id
    task_id = current_task_id()
    if task_id:
        step.worker_task_id = str(task_id)[:255]
    step.retry_count = _bounded_count(
        max(
            step.retry_count,
            step.attempt_no - 1,
        )
    )
    step.finished_at = step.finished_at or finished_at
    step.duration_ms = elapsed_milliseconds(
        step.started_at,
        step.finished_at,
    )
    step.retry_at = None
    step.terminal_impact = _terminal_impact(
        scope="step",
        stage=step.name,
        final_state=final_state,
        affected_count=affected_count,
        error_code=error_code,
    )
    step.recovery_state = recovery_state


def project_run_terminal_observation(
    run: CollectionRun,
    *,
    finished_at,
    stage: str,
    final_state: str,
    affected_count: int = 0,
    error_code: str | None = None,
    recovery_state: str = RecoveryState.NOT_REQUIRED,
) -> None:
    run.completed_at = run.completed_at or finished_at
    run.duration_ms = elapsed_milliseconds(
        run.started_at,
        run.completed_at,
    )
    run.terminal_impact = _terminal_impact(
        scope="run",
        stage=stage,
        final_state=final_state,
        affected_count=affected_count,
        error_code=error_code,
    )
    run.recovery_state = recovery_state
    run.next_recovery_at = None


@transaction.atomic
def create_run(
    *,
    topic_code: str,
    window_start: datetime,
    window_end: datetime,
    user=None,
    trigger="manual",
    correlation_id=None,
    retry_count: int = 0,
    requested_target_ids: list[str] | None = None,
    approval_mode: str = "manual",
    execution_material_hash: str | None = None,
    source_registry_id=None,
    expected_registry_manifest_hash: str | None = None,
    topic_policy_id=None,
    expected_policy_version: int | None = None,
    expected_policy_hash: str | None = None,
):
    if (
        not isinstance(window_start, datetime)
        or not isinstance(window_end, datetime)
        or timezone.is_naive(window_start)
        or timezone.is_naive(window_end)
    ):
        raise ValueError(
            "Collection windows require timezone-aware datetimes."
        )
    if window_start >= window_end:
        raise ValueError(
            "Collection window_start must be earlier than window_end."
        )
    if window_end > timezone.now() + timedelta(minutes=5):
        raise ValueError(
            "Collection window_end cannot be in the future."
        )
    if source_registry_id is None:
        registry = current_registry(topic_code, for_update=True)
    else:
        registry = (
            SourceRegistrySnapshot.objects.select_for_update()
            .filter(pk=source_registry_id, topic_code=topic_code)
            .first()
        )
        if (
            registry is None
            or expected_registry_manifest_hash is None
            or registry.manifest_hash != expected_registry_manifest_hash
            or calculate_registry_manifest_hash(registry)
            != expected_registry_manifest_hash
        ):
            raise ValueError("The frozen source registry is inconsistent.")
    if topic_policy_id is None:
        policy = (
            TopicPolicy.objects.select_for_update()
            .filter(code=topic_code, active=True)
            .order_by("-version", "-created_at")
            .first()
        )
    else:
        policy = (
            TopicPolicy.objects.select_for_update()
            .filter(pk=topic_policy_id, code=topic_code)
            .first()
        )
        if (
            policy is None
            or expected_policy_version is None
            or expected_policy_hash is None
            or policy.version != expected_policy_version
            or policy.policy_hash != expected_policy_hash
        ):
            raise ValueError("The frozen topic policy is inconsistent.")
    if policy is None:
        raise ValueError(
            "An active topic policy is required before collection."
        )
    if _hash(policy.policy) != policy.policy_hash:
        raise ValueError(
            "The active topic policy hash is inconsistent."
        )
    allowed_authority_tiers = policy.policy.get(
        "allowedAuthorityTiers"
    )
    if (
        not isinstance(allowed_authority_tiers, list)
        or not allowed_authority_tiers
        or not all(
            isinstance(value, str) and value
            for value in allowed_authority_tiers
        )
    ):
        raise ValueError(
            "The topic policy requires explicit allowedAuthorityTiers."
        )
    allowed_authority_tiers = list(
        dict.fromkeys(allowed_authority_tiers)
    )
    freshness_cutoff = window_end - timedelta(
        minutes=policy.freshness_minutes
    )
    resolved_correlation_id = (
        uuid.UUID(str(correlation_id))
        if correlation_id is not None
        else current_correlation_uuid()
    )
    resolved_retry_count = _bounded_count(retry_count)
    frozen_target_ids = sorted(
        str(value) for value in (requested_target_ids or [])
    )
    fingerprint = _collection_run_request_fingerprint(
        topic_code=topic_code,
        window_start=window_start,
        window_end=window_end,
        trigger=trigger,
        registry_id=registry.id,
        registry_hash=registry.manifest_hash,
        policy_id=policy.id,
        policy_version=policy.version,
        policy_hash=policy.policy_hash,
        freshness_minutes=policy.freshness_minutes,
        allowed_authority_tiers=allowed_authority_tiers,
        requested_target_ids=frozen_target_ids,
        approval_mode=approval_mode,
        execution_material_hash=execution_material_hash,
    )
    run, created = CollectionRun.objects.get_or_create(
        request_fingerprint=fingerprint,
        defaults={
            "correlation_id": resolved_correlation_id,
            "display_id": f"RUN-{timezone.now():%Y%m%d}-{secrets.token_hex(3).upper()}",
            "topic_code": topic_code,
            "trigger": trigger,
            "approval_mode": approval_mode,
            "window_start": window_start,
            "window_end": window_end,
            "source_registry": registry,
            "registry_manifest_hash": registry.manifest_hash,
            "topic_policy": policy,
            "policy_version": policy.version,
            "policy_hash": policy.policy_hash,
            "freshness_minutes": policy.freshness_minutes,
            "allowed_authority_tiers": (
                allowed_authority_tiers
            ),
            "freshness_cutoff": freshness_cutoff,
            "requested_target_ids": frozen_target_ids,
            "requested_by": user,
            "retry_count": resolved_retry_count,
        },
    )
    return run, created


def _version_http_metadata(value) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    meaningful_keys = {"etag", "lastModified", "contentType"}
    for key, item in value.items():
        if key in meaningful_keys:
            result[key] = item
        elif isinstance(item, dict):
            nested = _version_http_metadata(item)
            if nested:
                result[key] = nested
    return result


def _record_version_hash(record) -> str:
    material = {
        "schemaVersion": "source-item-version-v1",
        "externalId": record.external_id,
        "canonicalUrl": record.canonical_url,
        "title": record.title,
        "publisher": record.publisher,
        "publishedAt": (
            record.published_at.isoformat()
            if record.published_at
            else None
        ),
        "modifiedAt": (
            record.modified_at.isoformat()
            if record.modified_at
            else None
        ),
        "language": "ko",
        "status": record.status,
        "contentHash": record.content_hash,
        "rawChecksum": record.raw_checksum,
        "httpMetadata": _version_http_metadata(
            record.http_metadata or {}
        ),
    }
    return canonical_hash(
        material,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _records_response_checksum(records) -> str:
    return canonical_hash(
        sorted(
            (
                record.external_id,
                _record_version_hash(record),
                record.status,
            )
            for record in records
        ),
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _legacy_record_hashes(record) -> tuple[str, str]:
    legacy_content_material = {
        "url": record.canonical_url,
        "title": record.title,
        "body": record.body_text,
        "attachments": [
            {
                "url": item.url,
                "title": item.title,
                "mime_type": item.mime_type,
            }
            for item in record.attachments
        ],
        "metadata": record.metadata,
    }
    encoded = json.dumps(
        legacy_content_material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_content_hash = hashlib.sha256(
        encoded.encode("utf-8")
    ).hexdigest()
    legacy_version_hash = _hash(
        {
            "content": legacy_content_hash,
            "publishedAt": record.published_at,
            "status": "active",
        }
    )
    return legacy_content_hash, legacy_version_hash


def _legacy_compatible_item(snapshot, record):
    if (
        record.status != SourceItemStatus.ACTIVE
        or record.modified_at is not None
    ):
        return None
    legacy_content_hash, legacy_version_hash = _legacy_record_hashes(
        record
    )
    return (
        SourceItem.objects.filter(
            source_id=snapshot.source_id,
            external_id=record.external_id,
            source_version_hash=legacy_version_hash,
            source_version_schema="legacy-source-item-version-v0",
            content_hash=legacy_content_hash,
            canonical_url=record.canonical_url,
            title=record.title[:1000],
            publisher=record.publisher[:300],
            published_at=record.published_at,
            body_text=record.body_text,
            status=SourceItemStatus.ACTIVE,
        )
        .order_by("first_collected_at", "id")
        .first()
    )


def _discovery_kind(*, previous_link, item, status: str) -> str:
    if (
        previous_link is not None
        and previous_link.source_item.status
        in {
            SourceItemStatus.RETRACTED,
            SourceItemStatus.UNAVAILABLE,
        }
        and status
        in {
            SourceItemStatus.ACTIVE,
            SourceItemStatus.CORRECTED,
        }
    ):
        return SourceDiscoveryKind.RESTORED
    if (
        previous_link is not None
        and previous_link.source_item_id == item.id
    ):
        return SourceDiscoveryKind.UNCHANGED
    if status == SourceItemStatus.CORRECTED:
        return SourceDiscoveryKind.CORRECTED
    if status == SourceItemStatus.RETRACTED:
        return SourceDiscoveryKind.RETRACTED
    if status == SourceItemStatus.UNAVAILABLE:
        return SourceDiscoveryKind.UNAVAILABLE
    return SourceDiscoveryKind.NEW_VERSION


@transaction.atomic
def _persist_record(run, attempt, snapshot, record):
    if record.status not in SourceItemStatus.values:
        raise ValueError("Source record status is not supported.")

    SourceDefinition.objects.select_for_update().only("id").get(
        pk=snapshot.source_id
    )
    current_run_link = (
        RunSourceItem.objects.select_related("source_item")
        .filter(
            run=run,
            source_item__source_id=snapshot.source_id,
            source_item__external_id=record.external_id,
        )
        .first()
    )
    previous_link = (
        RunSourceItem.objects.select_related("source_item")
        .filter(
            source_item__source_id=snapshot.source_id,
            source_item__external_id=record.external_id,
            collection_attempt__state="succeeded",
        )
        .exclude(run=run)
        .order_by("-discovered_at", "-id")
        .first()
    )
    previous_item = (
        previous_link.source_item
        if previous_link is not None
        else None
    )
    if (
        record.reconciliation_only
        and previous_link is None
    ):
        return None, False
    version_hash = _record_version_hash(record)
    metadata = {
        **record.metadata,
        "_collection": {
            "rawChecksum": record.raw_checksum,
            "http": record.http_metadata,
            "policy": {
                "topicPolicyId": str(run.topic_policy_id),
                "policyVersion": run.policy_version,
                "policyHash": run.policy_hash,
                "freshnessCutoff": (
                    run.freshness_cutoff.isoformat()
                ),
                "authorityTier": attempt.authority_tier,
                "freshnessDecision": "eligible",
            },
        },
    }
    item = (
        SourceItem.objects.filter(
            source_id=snapshot.source_id,
            external_id=record.external_id,
            source_version_hash=version_hash,
            source_version_schema="nfc-rfc8785-source-item-version-v1",
        )
        .order_by("first_collected_at", "id")
        .first()
    )
    legacy_item = None
    if (
        previous_link is not None
        and previous_link.source_item.source_version_schema
        == "legacy-source-item-version-v0"
    ):
        legacy_item = _legacy_compatible_item(snapshot, record)
    baseline_cutover = (
        legacy_item is not None
        and previous_link is not None
        and previous_link.source_item_id == legacy_item.id
    )
    if (
        current_run_link is not None
        and current_run_link.source_item.source_version_schema
        == "legacy-source-item-version-v0"
        and legacy_item is not None
        and current_run_link.source_item_id == legacy_item.id
    ):
        return current_run_link, False
    if item is None:
        item = SourceItem.objects.create(
            source_id=snapshot.source_id,
            external_id=record.external_id,
            source_version_hash=version_hash,
            source_version_schema=(
                "nfc-rfc8785-source-item-version-v1"
            ),
            canonical_url=record.canonical_url,
            title=record.title[:1000],
            publisher=record.publisher[:300],
            published_at=record.published_at,
            modified_at=record.modified_at,
            first_collected_at=record.collected_at,
            content_hash=record.content_hash,
            body_text=record.body_text,
            metadata=metadata,
            attachments=[
                value.__dict__ for value in record.attachments
            ],
            status=record.status,
            supersedes=(
                previous_item
                if (
                    previous_item is not None
                    and previous_item.source_version_hash != version_hash
                )
                else None
            ),
        )
    kind = _discovery_kind(
        previous_link=previous_link,
        item=item,
        status=record.status,
    )
    if baseline_cutover:
        kind = SourceDiscoveryKind.UNCHANGED
    if current_run_link is not None:
        if current_run_link.source_item_id != item.id:
            raise ValueError(
                "A collection run returned conflicting versions for one "
                "stable source identity."
            )
        return (
            current_run_link,
            current_run_link.discovery_kind
            != SourceDiscoveryKind.UNCHANGED,
        )
    link = RunSourceItem.objects.create(
        run=run,
        source_item=item,
        collection_attempt=attempt,
        source_snapshot=snapshot,
        previous_run_source_item=previous_link,
        discovery_kind=kind,
    )
    return link, kind != SourceDiscoveryKind.UNCHANGED


def collect_run(run: CollectionRun) -> CollectionRun:
    """Compatibility entrypoint for the durable source-level fan-out."""
    return dispatch_collection_run(run)

_SOURCE_TERMINAL_STATES = frozenset(
    {
        SourceCollectionAttemptState.SUCCEEDED,
        SourceCollectionAttemptState.FAILED,
        SourceCollectionAttemptState.SKIPPED,
    }
)


def _verify_frozen_run_policy(run: CollectionRun) -> None:
    policy = run.topic_policy
    allowed = policy.policy.get("allowedAuthorityTiers")
    if (
        policy.policy.get("schemaVersion")
        == "collection-run-policy-legacy-sentinel-v1"
        or policy.policy.get("runtimeEligible") is False
        or policy.code != run.topic_code
        or policy.version != run.policy_version
        or policy.policy_hash != run.policy_hash
        or _hash(policy.policy) != run.policy_hash
        or policy.freshness_minutes != run.freshness_minutes
        or not isinstance(allowed, list)
        or list(dict.fromkeys(allowed))
        != run.allowed_authority_tiers
        or run.freshness_cutoff
        != run.window_end
        - timedelta(minutes=run.freshness_minutes)
    ):
        raise SourceAccessError(
            code="collection_policy_material_mismatch",
            category="security",
            detail="Collection run topic policy material is inconsistent.",
            remediation="Create a new run from an approved current policy.",
        )
    if (
        run.source_registry.manifest_hash
        != run.registry_manifest_hash
    ):
        raise SourceAccessError(
            code="collection_registry_material_mismatch",
            category="security",
            detail="Collection run registry manifest is inconsistent.",
            remediation="Create a new run from an approved registry snapshot.",
        )


def _source_attempt_material(
    run: CollectionRun,
    snapshot,
) -> dict:
    frozen = snapshot.frozen_config
    if not isinstance(frozen, dict):
        raise SourceAccessError(
            code="source_snapshot_material_missing",
            category="security",
            detail="Source snapshot frozen material is unavailable.",
            remediation="Re-approve the source snapshot before collection.",
        )
    access_policy = frozen.get("accessPolicy")
    access_policy_hash = frozen.get("accessPolicyHash")
    if (
        not isinstance(access_policy, dict)
        or access_policy_hash != _hash(access_policy)
    ):
        raise SourceAccessError(
            code="source_access_policy_material_mismatch",
            category="security",
            detail="Source access policy material is inconsistent.",
            remediation="Re-approve the source snapshot before collection.",
        )
    rights_policy = frozen.get("rightsPolicy")
    rights_policy_hash = frozen.get("rightsPolicyHash")
    if (
        not isinstance(rights_policy, dict)
        or rights_policy_hash != _hash(rights_policy)
    ):
        raise SourceAccessError(
            code="source_rights_policy_material_mismatch",
            category="security",
            detail="Source rights policy material is inconsistent.",
            remediation="Re-approve the source snapshot before collection.",
        )
    authority_tier = frozen.get("authorityTier")
    if not isinstance(authority_tier, str) or not authority_tier:
        raise SourceAccessError(
            code="source_authority_material_missing",
            category="security",
            detail="Source authority tier is unavailable.",
            remediation="Re-approve the source snapshot before collection.",
        )
    adapter_name = source_adapter_key(snapshot)
    adapter_version = source_adapter_version(snapshot)
    implementation_hash = (
        source_adapter_implementation_manifest_hash(snapshot)
    )
    request_fingerprint = canonical_hash(
        {
            "schemaVersion": "source-collection-request-v2",
            "runId": str(run.id),
            "sourceSnapshotId": str(snapshot.id),
            "sourceConfigHash": snapshot.frozen_config_hash,
            "accessPolicyHash": access_policy_hash,
            "rightsPolicyHash": rights_policy_hash,
            "adapterName": adapter_name,
            "adapterVersion": adapter_version,
            "adapterImplementationManifestHash": (
                implementation_hash
            ),
            "windowStart": run.window_start.isoformat(),
            "windowEnd": run.window_end.isoformat(),
            "topicPolicyId": str(run.topic_policy_id),
            "topicPolicyVersion": run.policy_version,
            "topicPolicyHash": run.policy_hash,
            "freshnessCutoff": run.freshness_cutoff.isoformat(),
            "authorityTier": authority_tier,
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    return {
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
        "adapter_implementation_manifest_hash": (
            implementation_hash
        ),
        "adapter_config_hash": snapshot.frozen_config_hash,
        "request_fingerprint": request_fingerprint,
        "request_window_start": run.window_start,
        "request_window_end": run.window_end,
        "access_policy_hash": access_policy_hash,
        "rights_policy_hash": rights_policy_hash,
        "authority_tier": authority_tier,
        "freshness_cutoff": run.freshness_cutoff,
    }


def _attempt_material_matches(
    attempt: SourceCollectionAttempt,
    expected: dict,
) -> bool:
    return all(
        getattr(attempt, field) == value
        for field, value in expected.items()
    )


def _record_source_observation(
    attempt: SourceCollectionAttempt,
    *,
    outcome: str,
    delivery_attempt_no: int,
) -> None:
    SourceCollectionObservation.objects.get_or_create(
        attempt=attempt,
        delivery_attempt_no=max(int(delivery_attempt_no), 1),
        defaults={
            "outcome": outcome,
            "failure_category": attempt.failure_category,
            "error_code": attempt.error_code,
            "error_detail_redacted": (
                attempt.error_detail_redacted
            ),
            "http_status": attempt.http_status,
            "retry_count": attempt.retry_count,
            "retry_at": attempt.retry_at,
            "retry_after_seconds": (
                attempt.retry_after_seconds
            ),
            "request_count": attempt.request_count,
            "freshness_excluded_count": (
                attempt.freshness_excluded_count
            ),
            "duration_ms": attempt.duration_ms,
            "access_policy_hash": (
                attempt.access_policy_hash
            ),
            "authority_tier": attempt.authority_tier,
            "freshness_cutoff": attempt.freshness_cutoff,
        },
    )


def _record_interrupted_source_observations(
    attempt: SourceCollectionAttempt,
    *,
    before_delivery_attempt_no: int,
) -> None:
    for delivery_attempt_no in range(
        1,
        max(int(before_delivery_attempt_no), 1),
    ):
        SourceCollectionObservation.objects.get_or_create(
            attempt=attempt,
            delivery_attempt_no=delivery_attempt_no,
            defaults={
                "outcome": (
                    SourceCollectionAttemptState.RETRY_SCHEDULED
                ),
                "failure_category": (
                    SourceCollectionFailureCategory.INFRASTRUCTURE
                ),
                "error_code": "source_delivery_interrupted",
                "error_detail_redacted": (
                    "source delivery ended before outcome settlement"
                ),
                "http_status": None,
                "retry_count": delivery_attempt_no,
                "retry_at": None,
                "retry_after_seconds": None,
                "request_count": attempt.request_count,
                "freshness_excluded_count": (
                    attempt.freshness_excluded_count
                ),
                "duration_ms": attempt.duration_ms,
                "access_policy_hash": attempt.access_policy_hash,
                "authority_tier": attempt.authority_tier,
                "freshness_cutoff": attempt.freshness_cutoff,
            },
        )


def _enqueue_collection_finalizer(
    run: CollectionRun,
    *,
    cause: str,
) -> None:
    enqueue_event(
        event_type="run.collection_finalize_requested",
        aggregate_type="collection_run",
        aggregate_id=run.id,
        job_id=run.id,
        dedupe_key=(
            f"run.collection_finalize_requested:{run.id}:{cause}"
        ),
        payload={"run_id": str(run.id)},
        correlation_id=run.correlation_id,
    )


@transaction.atomic
def dispatch_collection_run(run: CollectionRun) -> CollectionRun:
    run = (
        CollectionRun.objects.select_for_update()
        .select_related("source_registry", "topic_policy")
        .get(pk=run.pk)
    )
    if run.state not in {
        RunState.QUEUED,
        RunState.COLLECTING,
        RunState.STOPPING,
    }:
        return run
    _verify_frozen_run_policy(run)
    now = timezone.now()
    step, _ = RunStep.objects.select_for_update().get_or_create(
        run=run,
        name="collect",
        attempt_no=1,
    )
    if run.stop_requested_at is not None:
        step.state = "stopped"
        step.error_code = "stop_requested"
        step.error_detail_redacted = (
            "collection stopped before source dispatch"
        )
        project_step_terminal_observation(
            step,
            run,
            finished_at=now,
            final_state=step.state,
            affected_count=step.input_count,
            error_code=step.error_code,
            recovery_state=RecoveryState.STOPPED,
        )
        step.save()
        run.state = RunState.STOPPED
        project_run_terminal_observation(
            run,
            finished_at=now,
            stage="collect",
            final_state=run.state,
            error_code="stop_requested",
            recovery_state=RecoveryState.STOPPED,
        )
        run.save()
        return run

    run.state = RunState.COLLECTING
    run.started_at = run.started_at or now
    run.recovery_state = RecoveryState.IN_PROGRESS
    run.next_recovery_at = None
    run.save(
        update_fields=(
            "state",
            "started_at",
            "recovery_state",
            "next_recovery_at",
        )
    )
    step.state = "running"
    begin_step_observation(step, run, started_at=now)
    memberships = list(
        run.source_registry.memberships.select_related(
            "source_snapshot__source"
        )
        .filter(enabled=True)
        .order_by("source_definition_id")
    )
    step.input_count = len(memberships)
    for membership in memberships:
        snapshot = membership.source_snapshot
        expected = _source_attempt_material(run, snapshot)
        attempt, _ = SourceCollectionAttempt.objects.get_or_create(
            run=run,
            source_snapshot=snapshot,
            defaults=expected,
        )
        attempt = SourceCollectionAttempt.objects.select_for_update().get(
            pk=attempt.pk
        )
        if not _attempt_material_matches(attempt, expected):
            if attempt.state == SourceCollectionAttemptState.SUCCEEDED:
                raise ValueError(
                    "Succeeded collection attempt provenance is inconsistent."
                )
            attempt.state = SourceCollectionAttemptState.FAILED
            attempt.failure_category = (
                SourceCollectionFailureCategory.POLICY
            )
            attempt.error_code = "attempt_provenance_mismatch"
            attempt.error_detail_redacted = (
                "collection attempt provenance is inconsistent"
            )
            attempt.finished_at = now
            attempt.duration_ms = elapsed_milliseconds(
                attempt.started_at,
                now,
            )
            attempt.save()
            _record_source_observation(
                attempt,
                outcome=attempt.state,
                delivery_attempt_no=attempt.retry_count + 1,
            )
            continue
        if attempt.state not in _SOURCE_TERMINAL_STATES:
            enqueue_event(
                event_type="source.collect_requested",
                aggregate_type="source_collection_attempt",
                aggregate_id=attempt.id,
                job_id=run.id,
                dedupe_key=f"source.collect_requested:{attempt.id}",
                payload={
                    "source_collection_attempt_id": str(
                        attempt.id
                    )
                },
                correlation_id=run.correlation_id,
            )
    step.fanout_completed_at = now
    step.save()
    _enqueue_collection_finalizer(run, cause="dispatch")
    return run


def _reconciliation_records_for_attempt(
    run: CollectionRun,
    attempt: SourceCollectionAttempt,
    snapshot,
) -> tuple[ReconciliationSourceRecord, ...]:
    if attempt.started_at is None:
        raise ValueError(
            "Source reconciliation requires a fixed attempt cutoff."
        )
    reconciliation_cutoff = run.window_start - timedelta(
        days=source_reconciliation_days(snapshot)
    )
    external_config = snapshot.frozen_config.get(
        "externalConfig",
        {},
    )
    identity_namespace = (
        external_config.get("identityNamespace")
        if isinstance(external_config, dict)
        else None
    )
    filters = {
        "source_snapshot__source_id": snapshot.source_id,
        "source_snapshot__frozen_config__adapterKey": (
            attempt.adapter_name
        ),
    }
    if isinstance(identity_namespace, str) and identity_namespace:
        filters[
            "source_snapshot__frozen_config__externalConfig__"
            "identityNamespace"
        ] = identity_namespace
    else:
        filters[
            "source_snapshot__frozen_config__externalConfig__"
            "identityNamespace__isnull"
        ] = True
    links = list(
        RunSourceItem.objects.filter(
            source_item__source_id=snapshot.source_id,
            collection_attempt__state=(
                SourceCollectionAttemptState.SUCCEEDED
            ),
            discovered_at__gte=reconciliation_cutoff,
            discovered_at__lt=attempt.started_at,
            **filters,
        )
        .exclude(run=run)
        .select_related("source_item")
        .annotate(
            _reconciliation_rank=Window(
                expression=RowNumber(),
                partition_by=[F("source_item__external_id")],
                order_by=[
                    F("discovered_at").desc(),
                    F("id").desc(),
                ],
            )
        )
        .filter(_reconciliation_rank=1)
        .order_by("source_item__external_id")[:10001]
    )
    if len(links) > 10000:
        raise ValueError(
            "Source reconciliation identity budget was exhausted."
        )
    return tuple(
        ReconciliationSourceRecord(
            external_id=link.source_item.external_id,
            canonical_url=link.source_item.canonical_url,
            title=link.source_item.title,
            published_at=link.source_item.published_at,
            modified_at=link.source_item.modified_at,
            status=link.source_item.status,
            metadata={
                key: value
                for key, value in link.source_item.metadata.items()
                if key != "_collection"
            },
            body_text=link.source_item.body_text,
            attachments=tuple(link.source_item.attachments),
        )
        for link in links
    )


def _fresh_records(
    run: CollectionRun,
    records,
) -> tuple[list, int]:
    accepted = []
    excluded = 0
    for record in records:
        if (
            record.reconciliation_only
            or record.status
            in {
                SourceItemStatus.RETRACTED,
                SourceItemStatus.UNAVAILABLE,
            }
        ):
            accepted.append(record)
            continue
        if (
            (record.modified_at or record.published_at) is None
            or (record.modified_at or record.published_at)
            < run.freshness_cutoff
            or (record.modified_at or record.published_at)
            > run.window_end
        ):
            excluded += 1
            continue
        accepted.append(record)
    return accepted, excluded

def _source_error_category(exc: SourceAccessError) -> str:
    if exc.category in SourceCollectionFailureCategory.values:
        return exc.category
    return SourceCollectionFailureCategory.INFRASTRUCTURE


def collect_source_attempt(
    attempt_id,
    *,
    delivery_attempt_no: int | None = None,
) -> SourceCollectionAttempt:
    adapter = None
    with transaction.atomic():
        attempt = (
            SourceCollectionAttempt.objects.select_for_update()
            .select_related(
                "run__source_registry",
                "run__topic_policy",
                "source_snapshot__source",
            )
            .get(pk=attempt_id)
        )
        run = attempt.run
        snapshot = attempt.source_snapshot
        resolved_delivery_attempt_no = max(
            int(delivery_attempt_no or attempt.retry_count + 1),
            1,
        )
        if attempt.state in _SOURCE_TERMINAL_STATES:
            _record_interrupted_source_observations(
                attempt,
                before_delivery_attempt_no=(
                    resolved_delivery_attempt_no
                ),
            )
            _record_source_observation(
                attempt,
                outcome=attempt.state,
                delivery_attempt_no=resolved_delivery_attempt_no,
            )
            _enqueue_collection_finalizer(
                run,
                cause=f"source:{attempt.id}:{attempt.state}",
            )
            return attempt
        _record_interrupted_source_observations(
            attempt,
            before_delivery_attempt_no=resolved_delivery_attempt_no,
        )
        try:
            _verify_frozen_run_policy(run)
            expected = _source_attempt_material(run, snapshot)
            if not _attempt_material_matches(attempt, expected):
                raise SourceAccessError(
                    code="source_attempt_provenance_mismatch",
                    category="security",
                    detail="Collection attempt immutable material changed.",
                    remediation="Create a new run from approved frozen material.",
                )
        except SourceAccessError as exc:
            now = timezone.now()
            attempt.state = SourceCollectionAttemptState.FAILED
            attempt.failure_category = _source_error_category(exc)
            attempt.error_code = exc.code[:100]
            attempt.error_detail_redacted = (
                "source collection was blocked by frozen material validation"
            )
            attempt.http_status = exc.http_status
            attempt.retry_at = None
            attempt.retry_after_seconds = exc.retry_after_seconds
            attempt.finished_at = now
            attempt.duration_ms = elapsed_milliseconds(
                attempt.started_at,
                now,
            )
            attempt.save()
            _record_source_observation(
                attempt,
                outcome=attempt.state,
                delivery_attempt_no=resolved_delivery_attempt_no,
            )
            _enqueue_collection_finalizer(
                run,
                cause=f"source:{attempt.id}:material",
            )
            return attempt
        if run.stop_requested_at is not None:
            now = timezone.now()
            attempt.state = SourceCollectionAttemptState.SKIPPED
            attempt.failure_category = (
                SourceCollectionFailureCategory.POLICY
            )
            attempt.error_code = "stop_requested"
            attempt.error_detail_redacted = (
                "collection stopped before source request"
            )
            attempt.finished_at = now
            attempt.duration_ms = elapsed_milliseconds(
                attempt.started_at,
                now,
            )
            attempt.save()
            _record_source_observation(
                attempt,
                outcome=attempt.state,
                delivery_attempt_no=resolved_delivery_attempt_no,
            )
            _enqueue_collection_finalizer(
                run,
                cause=f"source:{attempt.id}:stopped",
            )
            return attempt
        if run.state != RunState.COLLECTING:
            raise ValueError(
                "Source collection requires a collecting run."
            )
        if attempt.authority_tier not in run.allowed_authority_tiers:
            now = timezone.now()
            attempt.state = SourceCollectionAttemptState.FAILED
            attempt.failure_category = (
                SourceCollectionFailureCategory.AUTHORITY
            )
            attempt.error_code = "source_authority_not_allowed"
            attempt.error_detail_redacted = (
                "source authority is outside the frozen topic policy"
            )
            attempt.finished_at = now
            attempt.duration_ms = elapsed_milliseconds(
                attempt.started_at,
                now,
            )
            attempt.save()
            _record_source_observation(
                attempt,
                outcome=attempt.state,
                delivery_attempt_no=resolved_delivery_attempt_no,
            )
            _enqueue_collection_finalizer(
                run,
                cause=f"source:{attempt.id}:authority",
            )
            return attempt
        attempt.state = SourceCollectionAttemptState.RUNNING
        attempt.started_at = attempt.started_at or timezone.now()
        attempt.retry_at = None
        attempt.save(
            update_fields=(
                "state",
                "started_at",
                "retry_at",
            )
        )

    try:
        reconciliation_records = _reconciliation_records_for_attempt(
            run,
            attempt,
            snapshot,
        )
        adapter = build_source_adapter(
            snapshot,
            runtime_mode="collection",
            operation_id=str(attempt.id),
            reconciliation_records=reconciliation_records,
            reconciliation_external_ids=tuple(
                item.external_id for item in reconciliation_records
            ),
        )
        try:
            records = adapter.collect(
                since=run.window_start,
                until=run.window_end,
            )
        except BaseException:
            try:
                adapter.close()
            except SourceAccessError:
                pass
            raise
        else:
            adapter.close()
        accepted_records, excluded_count = _fresh_records(
            run,
            records,
        )
        response_checksum = _records_response_checksum(records)
        changed_links: list[RunSourceItem] = []
        with transaction.atomic():
            locked_run = (
                CollectionRun.objects.select_for_update()
                .get(pk=run.pk)
            )
            locked_attempt = (
                SourceCollectionAttempt.objects.select_for_update()
                .get(pk=attempt.pk)
            )
            if locked_run.stop_requested_at is not None:
                raise _CollectionStopRequested
            if locked_run.state != RunState.COLLECTING:
                raise _CollectionRunProgressed
            if (
                locked_attempt.state
                == SourceCollectionAttemptState.SUCCEEDED
            ):
                if (
                    locked_attempt.response_count != len(records)
                    or locked_attempt.response_checksum
                    != response_checksum
                ):
                    raise _CollectionReplayConflict(
                        "Concurrent collection responses conflict."
                    )
                return locked_attempt
            for record in accepted_records:
                link, changed = _persist_record(
                    locked_run,
                    locked_attempt,
                    snapshot,
                    record,
                )
                if changed and link is not None:
                    changed_links.append(link)
            now = timezone.now()
            locked_attempt.state = (
                SourceCollectionAttemptState.SUCCEEDED
            )
            locked_attempt.failure_category = (
                SourceCollectionFailureCategory.FRESHNESS
                if excluded_count
                else ""
            )
            locked_attempt.freshness_excluded_count = excluded_count
            locked_attempt.response_count = len(records)
            locked_attempt.response_checksum = response_checksum
            locked_attempt.error_code = (
                "records_excluded_by_freshness"
                if excluded_count
                else None
            )
            locked_attempt.error_detail_redacted = (
                f"{excluded_count} records were outside the frozen freshness window"
                if excluded_count
                else None
            )
            locked_attempt.http_status = None
            locked_attempt.retry_at = None
            locked_attempt.retry_after_seconds = None
            locked_attempt.request_count = _bounded_count(
                getattr(adapter, "request_count", 0)
            )
            locked_attempt.finished_at = now
            locked_attempt.duration_ms = elapsed_milliseconds(
                locked_attempt.started_at,
                now,
            )
            locked_attempt.save()
            _record_source_observation(
                locked_attempt,
                outcome=locked_attempt.state,
                delivery_attempt_no=resolved_delivery_attempt_no,
            )
            for link in changed_links:
                enqueue_event(
                    event_type="source.item_changed",
                    aggregate_type="run_source_item",
                    aggregate_id=link.id,
                    job_id=locked_run.id,
                    dedupe_key=f"source.item_changed:{link.id}",
                    payload={
                        "source_collection_attempt_id": str(
                            locked_attempt.id
                        ),
                        "run_id": str(locked_run.id),
                        "run_source_item_id": str(link.id),
                        "source_item_id": str(
                            link.source_item_id
                        ),
                        "change_kind": link.discovery_kind,
                    },
                    correlation_id=locked_run.correlation_id,
                )
            if excluded_count:
                locked_run.counters = {
                    **locked_run.counters,
                    "freshnessExcluded": (
                        int(
                            locked_run.counters.get(
                                "freshnessExcluded",
                                0,
                            )
                        )
                        + excluded_count
                    ),
                }
                locked_run.save(update_fields=("counters",))
            _enqueue_collection_finalizer(
                locked_run,
                cause=f"source:{locked_attempt.id}:succeeded",
            )
            return locked_attempt
    except _CollectionRunProgressed:
        return SourceCollectionAttempt.objects.get(pk=attempt.pk)
    except _CollectionStopRequested:
        return finalize_source_attempt_delivery_failure(
            attempt.id,
            "stop_requested",
            delivery_attempt_no=resolved_delivery_attempt_no,
        )
    except SourceAccessError as exc:
        with transaction.atomic():
            locked_attempt = (
                SourceCollectionAttempt.objects.select_for_update()
                .select_related("run", "source_snapshot")
                .get(pk=attempt.pk)
            )
            if locked_attempt.state in _SOURCE_TERMINAL_STATES:
                return locked_attempt
            now = timezone.now()
            retryable = bool(exc.retryable)
            delivery_exhausted = (
                retryable
                and resolved_delivery_attempt_no
                >= SOURCE_COLLECTION_MAX_ATTEMPTS
            )
            locked_attempt.failure_category = (
                _source_error_category(exc)
            )
            locked_attempt.error_code = exc.code[:100]
            locked_attempt.error_detail_redacted = (
                "source collection failed; operator remediation is "
                f"required: {exc.remediation}"
            )[:500]
            locked_attempt.http_status = exc.http_status
            locked_attempt.retry_after_seconds = (
                exc.retry_after_seconds
            )
            locked_attempt.request_count = _bounded_count(
                getattr(adapter, "request_count", 0)
                if adapter is not None
                else locked_attempt.request_count
            )
            locked_attempt.finished_at = None if retryable else now
            locked_attempt.duration_ms = elapsed_milliseconds(
                locked_attempt.started_at,
                now,
            )
            if retryable:
                exc.retryable = True
                locked_attempt.retry_count = max(
                    locked_attempt.retry_count,
                    resolved_delivery_attempt_no,
                )
                locked_attempt.state = (
                    SourceCollectionAttemptState.RETRY_SCHEDULED
                )
                locked_attempt.retry_at = (
                    None
                    if delivery_exhausted
                    else now
                    + timedelta(
                        seconds=max(
                            int(exc.retry_after_seconds or 1),
                            1,
                        )
                    )
                )
            else:
                locked_attempt.retry_count = max(
                    locked_attempt.retry_count,
                    resolved_delivery_attempt_no - 1,
                )
                locked_attempt.state = (
                    SourceCollectionAttemptState.FAILED
                )
                locked_attempt.retry_at = None
            locked_attempt.save()
            if not delivery_exhausted:
                _record_source_observation(
                    locked_attempt,
                    outcome=locked_attempt.state,
                    delivery_attempt_no=resolved_delivery_attempt_no,
                )
            if not retryable:
                _enqueue_collection_finalizer(
                    locked_attempt.run,
                    cause=(
                        f"source:{locked_attempt.id}:failed"
                    ),
                )
                return locked_attempt
        raise
    except Exception as exc:
        unexpected_error = SourceAccessError(
            code="source_collection_unexpected_failure",
            category="infrastructure",
            detail="Source collection ended unexpectedly.",
            remediation="Review the source worker and retry the delivery.",
            retryable=True,
        )
        delivery_exhausted = (
            resolved_delivery_attempt_no
            >= SOURCE_COLLECTION_MAX_ATTEMPTS
        )
        with transaction.atomic():
            locked_attempt = (
                SourceCollectionAttempt.objects.select_for_update()
                .get(pk=attempt.pk)
            )
            if locked_attempt.state in _SOURCE_TERMINAL_STATES:
                return locked_attempt
            now = timezone.now()
            locked_attempt.state = (
                SourceCollectionAttemptState.RETRY_SCHEDULED
            )
            locked_attempt.failure_category = (
                SourceCollectionFailureCategory.INFRASTRUCTURE
            )
            locked_attempt.error_code = unexpected_error.code
            locked_attempt.error_detail_redacted = (
                "source collection ended unexpectedly"
            )
            locked_attempt.http_status = None
            locked_attempt.retry_count = max(
                locked_attempt.retry_count,
                resolved_delivery_attempt_no,
            )
            locked_attempt.retry_at = (
                None
                if delivery_exhausted
                else now + timedelta(seconds=1)
            )
            locked_attempt.retry_after_seconds = None
            locked_attempt.request_count = _bounded_count(
                getattr(adapter, "request_count", 0)
                if adapter is not None
                else locked_attempt.request_count
            )
            locked_attempt.finished_at = None
            locked_attempt.duration_ms = elapsed_milliseconds(
                locked_attempt.started_at,
                now,
            )
            locked_attempt.save()
            if not delivery_exhausted:
                _record_source_observation(
                    locked_attempt,
                    outcome=locked_attempt.state,
                    delivery_attempt_no=resolved_delivery_attempt_no,
                )
        raise unexpected_error from exc


@transaction.atomic
def finalize_source_attempt_delivery_failure(
    attempt_id,
    error_code: str,
    *,
    delivery_attempt_no: int | None = None,
) -> SourceCollectionAttempt:
    attempt = (
        SourceCollectionAttempt.objects.select_for_update()
        .select_related("run")
        .get(pk=attempt_id)
    )
    if attempt.state == SourceCollectionAttemptState.SUCCEEDED:
        return attempt
    now = timezone.now()
    resolved_delivery_attempt_no = max(
        int(delivery_attempt_no or attempt.retry_count + 1),
        1,
    )
    stopped = attempt.run.stop_requested_at is not None
    _record_interrupted_source_observations(
        attempt,
        before_delivery_attempt_no=resolved_delivery_attempt_no,
    )
    attempt.state = (
        SourceCollectionAttemptState.SKIPPED
        if stopped
        else SourceCollectionAttemptState.FAILED
    )
    if not attempt.failure_category:
        attempt.failure_category = (
            SourceCollectionFailureCategory.POLICY
            if stopped
            else SourceCollectionFailureCategory.INFRASTRUCTURE
        )
    attempt.error_code = (
        "stop_requested"
        if stopped
        else (attempt.error_code or str(error_code)[:100])
    )
    attempt.error_detail_redacted = (
        "collection stopped after source delivery"
        if stopped
        else (
            attempt.error_detail_redacted
            or "source collection delivery was exhausted"
        )
    )
    attempt.retry_at = None
    attempt.retry_count = max(
        attempt.retry_count,
        resolved_delivery_attempt_no - 1,
    )
    attempt.finished_at = now
    attempt.duration_ms = elapsed_milliseconds(
        attempt.started_at,
        now,
    )
    attempt.save()
    _record_source_observation(
        attempt,
        outcome=attempt.state,
        delivery_attempt_no=resolved_delivery_attempt_no,
    )
    _enqueue_collection_finalizer(
        attempt.run,
        cause=f"source:{attempt.id}:terminal",
    )
    return attempt


@transaction.atomic
def finalize_collection_run_state(
    run_id,
) -> CollectionRun:
    run = (
        CollectionRun.objects.select_for_update()
        .select_related("source_registry", "topic_policy")
        .get(pk=run_id)
    )
    if run.state in {
        RunState.EXTRACTING,
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.STOPPED,
    }:
        return run
    step = (
        RunStep.objects.select_for_update()
        .filter(run=run, name="collect", attempt_no=1)
        .first()
    )
    if step is None or step.fanout_completed_at is None:
        return run
    membership_ids = list(
        run.source_registry.memberships.filter(
            enabled=True
        ).values_list("source_snapshot_id", flat=True)
    )
    attempts = list(
        SourceCollectionAttempt.objects.select_for_update().filter(
            run=run,
            source_snapshot_id__in=membership_ids,
        )
    )
    if len(attempts) != len(membership_ids) or any(
        attempt.state not in _SOURCE_TERMINAL_STATES
        for attempt in attempts
    ):
        return run
    now = timezone.now()
    succeeded = sum(
        attempt.state == SourceCollectionAttemptState.SUCCEEDED
        for attempt in attempts
    )
    failed = len(attempts) - succeeded
    freshness_excluded = sum(
        attempt.freshness_excluded_count
        for attempt in attempts
    )
    collected = (
        RunSourceItem.objects.filter(
            run=run,
            collection_attempt__state=(
                SourceCollectionAttemptState.SUCCEEDED
            ),
        )
        .exclude(discovery_kind=SourceDiscoveryKind.UNCHANGED)
        .count()
    )
    run.counters = {
        **run.counters,
        "sources": len(membership_ids),
        "items": collected,
        "sourceFailures": failed,
        "sourceSuccesses": succeeded,
        "freshnessExcluded": freshness_excluded,
    }
    step.output_count = collected
    if run.stop_requested_at is not None:
        run.state = RunState.STOPPED
        step.state = "stopped"
        step.error_code = "stop_requested"
        step.error_detail_redacted = (
            "collection stopped by request"
        )
        recovery_state = RecoveryState.STOPPED
        run.error_summary = None
    elif succeeded == 0:
        run.state = RunState.FAILED
        step.state = "failed"
        step.error_code = (
            "no_enabled_sources"
            if not membership_ids
            else "all_sources_failed"
        )
        step.error_detail_redacted = (
            "collection did not produce a successful source result"
        )
        recovery_state = RecoveryState.MANUAL_REQUIRED
        run.error_summary = {
            "stage": "collect",
            "code": step.error_code,
        }
    else:
        run.state = RunState.EXTRACTING
        step.state = "succeeded"
        step.error_code = (
            "partial_source_failure"
            if failed
            else (
                "records_excluded_by_freshness"
                if freshness_excluded
                else None
            )
        )
        step.error_detail_redacted = (
            "one or more source collections failed"
            if failed
            else (
                "one or more records were outside the frozen freshness window"
                if freshness_excluded
                else None
            )
        )
        recovery_state = RecoveryState.NOT_REQUIRED
        run.error_summary = (
            {
                "stage": "collect",
                "code": step.error_code,
                "sourceFailures": failed,
                "freshnessExcluded": freshness_excluded,
            }
            if failed or freshness_excluded
            else None
        )
    project_step_terminal_observation(
        step,
        run,
        finished_at=now,
        final_state=step.state,
        affected_count=failed,
        error_code=step.error_code,
        recovery_state=recovery_state,
    )
    step.save()
    if run.state in {RunState.FAILED, RunState.STOPPED}:
        project_run_terminal_observation(
            run,
            finished_at=now,
            stage="collect",
            final_state=run.state,
            affected_count=failed,
            error_code=step.error_code,
            recovery_state=recovery_state,
        )
    run.save()
    if run.state == RunState.EXTRACTING:
        enqueue_event(
            event_type="run.evidence_requested",
            aggregate_type="collection_run",
            aggregate_id=run.id,
            job_id=run.id,
            dedupe_key=f"run.evidence_requested:{run.id}",
            payload={"run_id": str(run.id)},
            correlation_id=run.correlation_id,
        )
    return run
