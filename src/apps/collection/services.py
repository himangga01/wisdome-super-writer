from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from adapters.sources import build_source_adapter
from apps.topics.services import current_registry
from wisdome_writer.infrastructure.outbox import enqueue_event

from .models import CollectionRun, RunSourceItem, RunState, RunStep, SourceCollectionAttempt, SourceItem


def _hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


@transaction.atomic
def create_run(*, topic_code: str, window_start: datetime, window_end: datetime, user=None, trigger="manual"):
    registry = current_registry(topic_code)
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
            "display_id": f"RUN-{timezone.now():%Y%m%d}-{secrets.token_hex(3).upper()}",
            "topic_code": topic_code,
            "trigger": trigger,
            "window_start": window_start,
            "window_end": window_end,
            "source_registry": registry,
            "registry_manifest_hash": registry.manifest_hash,
            "requested_by": user,
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
    run.state = RunState.COLLECTING
    run.started_at = run.started_at or timezone.now()
    run.save(update_fields=["state", "started_at"])
    step, _ = RunStep.objects.get_or_create(run=run, name="collect", attempt_no=1)
    step.state = "running"
    step.started_at = timezone.now()
    step.save(update_fields=["state", "started_at"])
    collected = 0
    failed = 0
    memberships = run.source_registry.memberships.select_related("source_snapshot__source").filter(enabled=True)
    for membership in memberships:
        if run.stop_requested_at:
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
            for record in records:
                _, created = _persist_record(run, attempt, snapshot, record)
                collected += int(created)
            attempt.state = "succeeded"
            attempt.response_count = len(records)
            attempt.response_checksum = _hash([record.content_hash for record in records])
        except Exception as exc:  # source isolation is intentional; details remain redacted
            failed += 1
            attempt.state = "failed"
            attempt.error_code = exc.__class__.__name__
            attempt.error_detail_redacted = str(exc)[:500]
        attempt.finished_at = timezone.now()
        attempt.save()
    with transaction.atomic():
        run = CollectionRun.objects.select_for_update().get(pk=run.pk)
        step = RunStep.objects.select_for_update().get(pk=step.pk)
        if run.stop_requested_at:
            run.state = RunState.STOPPED
            run.completed_at = timezone.now()
        else:
            run.state = RunState.EXTRACTING
        run.counters = {
            **run.counters,
            "sources": memberships.count(),
            "items": collected,
            "sourceFailures": failed,
        }
        run.save(update_fields=["state", "completed_at", "counters"])
        step.state = "succeeded" if failed < memberships.count() else "failed"
        step.output_count = collected
        step.finished_at = timezone.now()
        step.save(update_fields=["state", "output_count", "finished_at"])
        if run.state == RunState.EXTRACTING:
            enqueue_event(
                event_type="run.evidence_requested",
                aggregate_type="collection_run",
                aggregate_id=run.id,
                job_id=run.id,
                dedupe_key=f"run.evidence_requested:{run.id}",
                payload={"run_id": str(run.id)},
            )
    return run
