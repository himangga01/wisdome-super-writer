from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from adapters.sources import build_source_adapter
from apps.topics.services import current_registry
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
    SourceItem,
)


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
    registry = current_registry(topic_code)
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


@transaction.atomic
def _persist_record(run, attempt, snapshot, record):
    previous = (
        SourceItem.objects.filter(source=snapshot.source, external_id=record.external_id)
        .order_by("-first_collected_at")
        .first()
    )
    version_hash = _hash(
        {"content": record.content_hash, "publishedAt": record.published_at, "status": "active"}
    )
    item, created = SourceItem.objects.get_or_create(
        source=snapshot.source,
        external_id=record.external_id,
        source_version_hash=version_hash,
        defaults={
            "canonical_url": record.canonical_url,
            "title": record.title[:1000],
            "publisher": record.publisher[:300],
            "published_at": record.published_at,
            "first_collected_at": record.collected_at,
            "content_hash": record.content_hash,
            "body_text": record.body_text,
            "metadata": record.metadata,
            "attachments": [value.__dict__ for value in record.attachments],
            "supersedes": previous if previous and previous.source_version_hash != version_hash else None,
        },
    )
    kind = "new_version" if created else "unchanged"
    link, _ = RunSourceItem.objects.get_or_create(
        run=run,
        source_item=item,
        defaults={
            "collection_attempt": attempt,
            "source_snapshot": snapshot,
            "discovery_kind": kind,
        },
    )
    return link, created


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
        if CollectionRun.objects.filter(
            pk=run.pk,
            stop_requested_at__isnull=False,
        ).exists():
            break
        snapshot = membership.source_snapshot
        attempt, _ = SourceCollectionAttempt.objects.get_or_create(
            run=run,
            source_snapshot=snapshot,
            defaults={"adapter_name": snapshot.config.get("adapter", "public_html")},
        )
        attempt.state = "running"
        attempt.started_at = timezone.now()
        attempt.save(update_fields=["state", "started_at"])
        try:
            records = build_source_adapter(snapshot).collect(since=run.window_start, until=run.window_end)
            if CollectionRun.objects.filter(
                pk=run.pk,
                stop_requested_at__isnull=False,
            ).exists():
                attempt.state = "stopped"
                attempt.error_code = "stop_requested"
                attempt.error_detail_redacted = (
                    "collection stopped after the active source request"
                )
            else:
                for record in records:
                    if CollectionRun.objects.filter(
                        pk=run.pk,
                        stop_requested_at__isnull=False,
                    ).exists():
                        attempt.state = "stopped"
                        attempt.error_code = "stop_requested"
                        attempt.error_detail_redacted = (
                            "collection stopped while persisting source records"
                        )
                        break
                    _, created = _persist_record(
                        run,
                        attempt,
                        snapshot,
                        record,
                    )
                    collected += int(created)
            if attempt.state != "stopped":
                attempt.state = "succeeded"
            attempt.response_count = len(records)
            attempt.response_checksum = _hash([record.content_hash for record in records])
        except Exception as exc:  # source isolation is intentional; details remain redacted
            failed += 1
            attempt.state = "failed"
            attempt.error_code = exc.__class__.__name__
            attempt.error_detail_redacted = "source collection failed"
        attempt.finished_at = timezone.now()
        attempt.save()
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run.pk)
        step = RunStep.objects.select_for_update().get(pk=step.pk)
        now = timezone.now()
        source_count = memberships.count()
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
