from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from adapters.sources import (
    build_source_adapter,
    source_adapter_implementation_manifest_hash,
    source_adapter_key,
    source_adapter_version,
    source_reconciliation_days,
)
from apps.topics.models import SourceDefinition
from apps.topics.services import current_registry
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)
from wisdome_writer.infrastructure.outbox import enqueue_event
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
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _bounded_count(value: int) -> int:
    return min(max(int(value), 0), 2147483647)


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
):
    registry = current_registry(topic_code, for_update=True)
    resolved_correlation_id = (
        uuid.UUID(str(correlation_id))
        if correlation_id is not None
        else current_correlation_uuid()
    )
    resolved_retry_count = _bounded_count(retry_count)
    fingerprint = _hash(
        {
            "topic": topic_code,
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "trigger": trigger,
            "registry": str(registry.id),
            "registryHash": registry.manifest_hash,
        }
    )
    run, created = CollectionRun.objects.get_or_create(
        request_fingerprint=fingerprint,
        defaults={
            "correlation_id": resolved_correlation_id,
            "display_id": f"RUN-{timezone.now():%Y%m%d}-{secrets.token_hex(3).upper()}",
            "topic_code": topic_code,
            "trigger": trigger,
            "window_start": window_start,
            "window_end": window_end,
            "source_registry": registry,
            "registry_manifest_hash": registry.manifest_hash,
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
        and status == SourceItemStatus.ACTIVE
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
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run.pk)
        if run.stop_requested_at:
            step, _ = RunStep.objects.select_for_update().get_or_create(
                run=run,
                name="collect",
                attempt_no=1,
            )
            now = timezone.now()
            step.state = "stopped"
            step.error_code = "stop_requested"
            step.error_detail_redacted = "collection stopped by request"
            project_step_terminal_observation(
                step,
                run,
                finished_at=now,
                final_state=step.state,
                affected_count=step.input_count,
                error_code=step.error_code,
                recovery_state=RecoveryState.STOPPED,
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
            run.state = RunState.STOPPED
            project_run_terminal_observation(
                run,
                finished_at=now,
                stage="collect",
                final_state=run.state,
                error_code="stop_requested",
                recovery_state=RecoveryState.STOPPED,
            )
            run.save(
                update_fields=[
                    "state",
                    "completed_at",
                    "duration_ms",
                    "terminal_impact",
                    "recovery_state",
                    "next_recovery_at",
                ]
            )
            return run
        if run.state not in {
            RunState.QUEUED,
            RunState.COLLECTING,
        }:
            return run
        now = timezone.now()
        run.state = RunState.COLLECTING
        run.started_at = run.started_at or now
        run.recovery_state = RecoveryState.IN_PROGRESS
        run.next_recovery_at = None
        run.save(
            update_fields=[
                "state",
                "started_at",
                "recovery_state",
                "next_recovery_at",
            ]
        )
        step, _ = RunStep.objects.select_for_update().get_or_create(
            run=run,
            name="collect",
            attempt_no=1,
        )
        step.state = "running"
        begin_step_observation(step, run, started_at=now)
        step.save(
            update_fields=[
                "correlation_id",
                "worker_task_id",
                "state",
                "started_at",
                "finished_at",
                "duration_ms",
                "retry_count",
                "retry_at",
                "terminal_impact",
                "recovery_state",
            ]
        )
    collected = 0
    failed = 0
    memberships = run.source_registry.memberships.select_related("source_snapshot__source").filter(enabled=True)
    for membership in memberships:
        current_run_state = (
            CollectionRun.objects.filter(pk=run.pk)
            .values("state", "stop_requested_at")
            .first()
        )
        if (
            current_run_state is None
            or current_run_state["state"]
            not in {RunState.COLLECTING, RunState.STOPPING}
        ):
            break
        if current_run_state["stop_requested_at"] is not None:
            break
        snapshot = membership.source_snapshot
        expected_adapter_name = source_adapter_key(snapshot)
        expected_adapter_version = source_adapter_version(snapshot)
        expected_implementation_hash = (
            source_adapter_implementation_manifest_hash(snapshot)
        )
        expected_config_hash = snapshot.frozen_config_hash
        expected_request_fingerprint = canonical_hash(
            {
                "schemaVersion": "source-collection-request-v1",
                "runId": str(run.id),
                "sourceSnapshotId": str(snapshot.id),
                "sourceConfigHash": expected_config_hash,
                "adapterName": expected_adapter_name,
                "adapterVersion": expected_adapter_version,
                "adapterImplementationManifestHash": (
                    expected_implementation_hash
                ),
                "windowStart": run.window_start.isoformat(),
                "windowEnd": run.window_end.isoformat(),
            },
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        )
        with transaction.atomic():
            attempt, created_attempt = (
                SourceCollectionAttempt.objects.get_or_create(
                    run=run,
                    source_snapshot=snapshot,
                    defaults={
                        "adapter_name": expected_adapter_name,
                        "adapter_version": expected_adapter_version,
                        "adapter_implementation_manifest_hash": (
                            expected_implementation_hash
                        ),
                        "adapter_config_hash": expected_config_hash,
                        "request_fingerprint": (
                            expected_request_fingerprint
                        ),
                        "request_window_start": run.window_start,
                        "request_window_end": run.window_end,
                    },
                )
            )
            attempt = (
                SourceCollectionAttempt.objects.select_for_update()
                .get(pk=attempt.pk)
            )
            legacy_provenance_fields = (
                attempt.adapter_implementation_manifest_hash,
                attempt.adapter_config_hash,
                attempt.request_fingerprint,
                attempt.request_window_start,
                attempt.request_window_end,
            )
            if (
                not created_attempt
                and attempt.state != "succeeded"
                and all(
                    value is None
                    for value in legacy_provenance_fields
                )
            ):
                attempt.adapter_name = expected_adapter_name
                attempt.adapter_version = expected_adapter_version
                attempt.adapter_implementation_manifest_hash = (
                    expected_implementation_hash
                )
                attempt.adapter_config_hash = expected_config_hash
                attempt.request_fingerprint = (
                    expected_request_fingerprint
                )
                attempt.request_window_start = run.window_start
                attempt.request_window_end = run.window_end
                attempt.save(
                    update_fields=(
                        "adapter_name",
                        "adapter_version",
                        "adapter_implementation_manifest_hash",
                        "adapter_config_hash",
                        "request_fingerprint",
                        "request_window_start",
                        "request_window_end",
                    )
                )
        expected_attempt_material = (
            expected_adapter_name,
            expected_adapter_version,
            expected_implementation_hash,
            expected_config_hash,
            expected_request_fingerprint,
            run.window_start,
            run.window_end,
        )
        actual_attempt_material = (
            attempt.adapter_name,
            attempt.adapter_version,
            attempt.adapter_implementation_manifest_hash,
            attempt.adapter_config_hash,
            attempt.request_fingerprint,
            attempt.request_window_start,
            attempt.request_window_end,
        )
        if actual_attempt_material != expected_attempt_material:
            failed += 1
            with transaction.atomic():
                locked_attempt = (
                    SourceCollectionAttempt.objects.select_for_update()
                    .get(pk=attempt.pk)
                )
                if locked_attempt.state != "succeeded":
                    locked_attempt.state = "failed"
                    locked_attempt.error_code = (
                        "attempt_provenance_mismatch"
                    )
                    locked_attempt.error_detail_redacted = (
                        "collection attempt provenance is inconsistent"
                    )
                    locked_attempt.finished_at = timezone.now()
                    locked_attempt.save(
                        update_fields=(
                            "state",
                            "error_code",
                            "error_detail_redacted",
                            "finished_at",
                        )
                    )
            continue
        succeeded_replay_count: int | None = None
        with transaction.atomic():
            attempt = (
                SourceCollectionAttempt.objects.select_for_update()
                .get(pk=attempt.pk)
            )
            if attempt.state == "succeeded":
                succeeded_replay_count = (
                    RunSourceItem.objects.filter(
                        collection_attempt=attempt,
                    )
                    .exclude(
                        discovery_kind=(
                            SourceDiscoveryKind.UNCHANGED
                        )
                    )
                    .count()
                )
            else:
                attempt.state = "running"
                attempt.started_at = (
                    attempt.started_at or timezone.now()
                )
                attempt.error_code = None
                attempt.error_detail_redacted = None
                attempt.save(
                    update_fields=(
                        "state",
                        "started_at",
                        "error_code",
                        "error_detail_redacted",
                    )
                )
        if succeeded_replay_count is not None:
            collected += succeeded_replay_count
            continue
        try:
            reconciliation_cutoff = (
                run.window_start
                - timedelta(
                    days=source_reconciliation_days(snapshot)
                )
            )
            reconciliation_external_ids = list(
                RunSourceItem.objects.filter(
                    source_item__source_id=snapshot.source_id,
                    collection_attempt__state="succeeded",
                    discovered_at__gte=reconciliation_cutoff,
                )
                .values_list(
                    "source_item__external_id",
                    flat=True,
                )
                .distinct()
                .order_by("source_item__external_id")[:10001]
            )
            if len(reconciliation_external_ids) > 10000:
                raise ValueError(
                    "Source reconciliation identity budget was exhausted."
                )
            records = build_source_adapter(
                snapshot,
                reconciliation_external_ids=(
                    tuple(reconciliation_external_ids)
                ),
            ).collect(
                since=run.window_start,
                until=run.window_end,
            )
            if CollectionRun.objects.filter(
                pk=run.pk,
                stop_requested_at__isnull=False,
            ).exists():
                raise _CollectionStopRequested
            response_checksum = _records_response_checksum(records)
            source_collected = 0
            changed_links: list[RunSourceItem] = []
            with transaction.atomic():
                locked_run = (
                    CollectionRun.objects.select_for_update()
                    .get(pk=run.pk)
                )
                if locked_run.state not in {
                    RunState.COLLECTING,
                    RunState.STOPPING,
                }:
                    raise _CollectionRunProgressed
                if locked_run.stop_requested_at is not None:
                    raise _CollectionStopRequested
                locked_attempt = (
                    SourceCollectionAttempt.objects.select_for_update()
                    .get(pk=attempt.pk)
                )
                if locked_attempt.state == "succeeded":
                    if (
                        locked_attempt.response_count != len(records)
                        or locked_attempt.response_checksum
                        != response_checksum
                    ):
                        raise _CollectionReplayConflict(
                            "Concurrent collection responses conflict."
                        )
                    source_collected = (
                        RunSourceItem.objects.filter(
                            collection_attempt=locked_attempt,
                        )
                        .exclude(
                            discovery_kind=(
                                SourceDiscoveryKind.UNCHANGED
                            )
                        )
                        .count()
                    )
                else:
                    source_collected = 0
                    for record in records:
                        link, changed = _persist_record(
                            run,
                            locked_attempt,
                            snapshot,
                            record,
                        )
                        source_collected += int(changed)
                        if changed and link is not None:
                            changed_links.append(link)
                    locked_attempt.state = "succeeded"
                    locked_attempt.response_count = len(records)
                    locked_attempt.response_checksum = (
                        response_checksum
                    )
                    locked_attempt.error_code = None
                    locked_attempt.error_detail_redacted = None
                    locked_attempt.finished_at = timezone.now()
                    locked_attempt.save(
                        update_fields=(
                            "state",
                            "response_count",
                            "response_checksum",
                            "error_code",
                            "error_detail_redacted",
                            "finished_at",
                        )
                    )
                    for link in changed_links:
                        enqueue_event(
                            event_type="source.item_changed",
                            aggregate_type="run_source_item",
                            aggregate_id=link.id,
                            job_id=run.id,
                            dedupe_key=(
                                f"source.item_changed:{link.id}"
                            ),
                            payload={
                                "source_collection_attempt_id": str(
                                    locked_attempt.id
                                ),
                                "run_id": str(run.id),
                                "run_source_item_id": str(link.id),
                                "source_item_id": str(
                                    link.source_item_id
                                ),
                                "change_kind": (
                                    link.discovery_kind
                                ),
                            },
                            correlation_id=run.correlation_id,
                        )
            collected += source_collected
        except _CollectionRunProgressed:
            break
        except _CollectionStopRequested:
            with transaction.atomic():
                locked_attempt = (
                    SourceCollectionAttempt.objects.select_for_update()
                    .get(pk=attempt.pk)
                )
                if locked_attempt.state != "succeeded":
                    locked_attempt.state = "stopped"
                    locked_attempt.error_code = "stop_requested"
                    locked_attempt.error_detail_redacted = (
                        "collection stopped at a source persistence boundary"
                    )
                    locked_attempt.finished_at = timezone.now()
                    locked_attempt.save(
                        update_fields=(
                            "state",
                            "error_code",
                            "error_detail_redacted",
                            "finished_at",
                        )
                    )
        except Exception as exc:  # source isolation is intentional; details remain redacted
            failed += 1
            with transaction.atomic():
                locked_attempt = (
                    SourceCollectionAttempt.objects.select_for_update()
                    .get(pk=attempt.pk)
                )
                if locked_attempt.state != "succeeded":
                    locked_attempt.state = "failed"
                    locked_attempt.error_code = exc.__class__.__name__
                    locked_attempt.error_detail_redacted = (
                        "source collection failed"
                    )
                    locked_attempt.finished_at = timezone.now()
                    locked_attempt.save(
                        update_fields=(
                            "state",
                            "error_code",
                            "error_detail_redacted",
                            "finished_at",
                        )
                    )
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run.pk)
        if run.state not in {
            RunState.COLLECTING,
            RunState.STOPPING,
        }:
            return run
        step = RunStep.objects.select_for_update().get(pk=step.pk)
        now = timezone.now()
        source_count = memberships.count()
        membership_snapshot_ids = memberships.values_list(
            "source_snapshot_id",
            flat=True,
        )
        succeeded_source_count = (
            SourceCollectionAttempt.objects.filter(
                run=run,
                source_snapshot_id__in=membership_snapshot_ids,
                state="succeeded",
            )
            .values("source_snapshot_id")
            .distinct()
            .count()
        )
        failed = max(source_count - succeeded_source_count, 0)
        collected = (
            RunSourceItem.objects.filter(
                run=run,
                collection_attempt__state="succeeded",
            )
            .exclude(
                discovery_kind=SourceDiscoveryKind.UNCHANGED
            )
            .count()
        )
        if run.stop_requested_at:
            run.state = RunState.STOPPED
            project_run_terminal_observation(
                run,
                finished_at=now,
                stage="collect",
                final_state=run.state,
                affected_count=failed,
                error_code="stop_requested",
                recovery_state=RecoveryState.STOPPED,
            )
        else:
            run.state = RunState.EXTRACTING
        run.counters = {
            **run.counters,
            "sources": source_count,
            "items": collected,
            "sourceFailures": failed,
        }
        run.save(
            update_fields=[
                "state",
                "completed_at",
                "counters",
                "duration_ms",
                "terminal_impact",
                "recovery_state",
                "next_recovery_at",
            ]
        )
        if run.state == RunState.STOPPED:
            step.state = "stopped"
            step.error_code = "stop_requested"
            step.error_detail_redacted = "collection stopped by request"
        else:
            step.state = "succeeded" if failed < source_count else "failed"
            if step.state == "failed":
                step.error_code = (
                    "no_enabled_sources"
                    if source_count == 0
                    else "all_sources_failed"
                )
                step.error_detail_redacted = (
                    "collection did not produce a successful source result"
                )
        step.output_count = collected
        impact_error_code = step.error_code
        if step.state == "succeeded" and failed:
            impact_error_code = "partial_source_failure"
        project_step_terminal_observation(
            step,
            run,
            finished_at=now,
            final_state=step.state,
            affected_count=failed,
            error_code=impact_error_code,
            recovery_state=(
                RecoveryState.MANUAL_REQUIRED
                if step.state == "failed"
                else (
                    RecoveryState.STOPPED
                    if step.state == "stopped"
                    else RecoveryState.NOT_REQUIRED
                )
            ),
        )
        step.save(
            update_fields=[
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
            ]
        )
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
