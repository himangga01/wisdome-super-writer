from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.audit.services import (
    AuditContext,
    record_audit_event,
    require_worker_event,
)
from apps.collection.models import (
    CollectionRun,
    RecoveryState,
    RunState,
    RunStep,
)
from apps.collection.services import (
    project_run_terminal_observation,
    project_step_terminal_observation,
)
from wisdome_writer.infrastructure.outbox import (
    CURRENT_EVENT_CONSUMER_LEASE_GENERATION,
    CURRENT_EVENT_CONSUMER_LEASE_TOKEN,
    CURRENT_EVENT_CONSUMER_NAME,
    CURRENT_EVENT_CORRELATION_ID,
    CURRENT_EVENT_ID,
    enqueue_event,
)
from wisdome_writer.infrastructure.models import OutboxMessage

from .clustering import (
    article_identity_for_verification,
    cluster_run_items,
    generation_manifest_for_verifications,
    verify_event_cluster,
)
from .models import DraftArticle, EventCluster, EventClusterVerification
from .services import build_source_grounded_draft


def _worker_audit_context(reason_code: str) -> AuditContext:
    return AuditContext.for_worker(
        correlation_id=CURRENT_EVENT_CORRELATION_ID.get(),
        event_key=CURRENT_EVENT_ID.get(),
        consumer_name=CURRENT_EVENT_CONSUMER_NAME.get(),
        lease_token=CURRENT_EVENT_CONSUMER_LEASE_TOKEN.get(),
        lease_generation=CURRENT_EVENT_CONSUMER_LEASE_GENERATION.get(),
        reason_code=reason_code,
    )


def _generation_work_units(run, verifications):
    current = [
        row for row in verifications if row.origin_run_id == run.id
    ]
    units = [
        (row, [row])
        for row in current
        if row.decision in {"verified_notice", "verified_breaking"}
    ]
    digest_rows = sorted(
        (
            row
            for row in current
            if row.decision in {"daily_digest_candidate", "held"}
        ),
        key=lambda row: (row.decision == "held", str(row.id)),
    )
    if digest_rows:
        units.append((digest_rows[0], digest_rows))
    return units


def _generation_payload(run, primary, verification_rows):
    verification_ids, manifest_hash, _ = (
        generation_manifest_for_verifications(
            run=run,
            primary_verification=primary,
            verifications=verification_rows,
        )
    )
    return {
        "verification_id": str(primary.id),
        "run_id": str(run.id),
        "verification_ids": verification_ids,
        "generation_manifest_hash": manifest_hash,
    }


def _complete_run_without_generation(run, *, alias: str) -> bool:
    if run.state != RunState.VALIDATING:
        return False
    finished_at = timezone.now()
    run.state = RunState.COMPLETED
    run.completed_at = finished_at
    run.error_summary = None
    run.recovery_state = RecoveryState.NOT_REQUIRED
    run.next_recovery_at = None
    project_run_terminal_observation(
        run,
        finished_at=finished_at,
        stage="editorial",
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
            "duration_ms",
            "terminal_impact",
            "recovery_state",
            "next_recovery_at",
        ),
        using=alias,
    )
    return True


@shared_task
def request_run_clustering(run_id: str):
    audit_context = _worker_audit_context("evidence-ready clustering request")
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        run = (
            CollectionRun.objects.using(alias)
            .select_for_update()
            .get(pk=run_id)
        )
        require_worker_event(
            context=audit_context,
            topic="run.evidence_ready",
            aggregate_id=run.id,
            payload_identity={"run_id": str(run.id)},
        )
        if run.state not in {
            RunState.VALIDATING,
            RunState.DRAFTING,
            RunState.AWAITING_APPROVAL,
        }:
            return {"runId": str(run.id), "state": run.state}
        enqueue_event(
            event_type="editorial.cluster_requested",
            aggregate_type="collection_run",
            aggregate_id=run.id,
            job_id=run.id,
            dedupe_key=f"editorial.cluster_requested:{run.id}",
            correlation_id=audit_context.correlation_id,
            payload={"run_id": str(run.id)},
        )
        return {"runId": str(run.id), "state": "cluster_requested"}


@shared_task
def cluster_and_verify_run(run_id: str):
    audit_context = _worker_audit_context("event clustering and verification")
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        run = (
            CollectionRun.objects.using(alias)
            .select_for_update()
            .get(pk=run_id)
        )
        require_worker_event(
            context=audit_context,
            topic="editorial.cluster_requested",
            aggregate_id=run.id,
            payload_identity={"run_id": str(run.id)},
        )
        clusters = cluster_run_items(run.id, using=alias)
        verifications = [
            verify_event_cluster(cluster.id, run_id=run.id, using=alias)
            for cluster in clusters
        ]
        generation_units = _generation_work_units(run, verifications)
        if not generation_units:
            _complete_run_without_generation(run, alias=alias)
        for verification, verification_rows in generation_units:
            payload = _generation_payload(
                run,
                verification,
                verification_rows,
            )
            enqueue_event(
                event_type="editorial.generate_requested",
                aggregate_type="event_cluster_verification",
                aggregate_id=verification.id,
                job_id=run.id,
                dedupe_key=(
                    "editorial.generate_requested:"
                    f"{verification.id}:{run.id}"
                ),
                correlation_id=audit_context.correlation_id,
                payload=payload,
            )
        return {
            "runId": str(run.id),
            "clusterIds": [str(row.id) for row in clusters],
            "verificationIds": [str(row.id) for row in verifications],
            "generationVerificationIds": [
                str(row.id) for row, _ in generation_units
            ],
        }


def _load_generation_verifications(
    *,
    alias: str,
    verification_id: str,
    verification_ids: list[str],
):
    if (
        verification_ids != sorted(verification_ids)
        or len(verification_ids) != len(set(verification_ids))
        or verification_id not in verification_ids
    ):
        raise ValueError("generation verification IDs are not canonical")
    rows = list(
        EventClusterVerification.objects.using(alias)
        .filter(id__in=verification_ids)
        .select_related("cluster", "origin_run")
    )
    by_id = {str(row.id): row for row in rows}
    if set(by_id) != set(verification_ids):
        raise ValueError("generation verification set is incomplete")
    return by_id[verification_id], [by_id[value] for value in verification_ids]


def _generation_is_superseded(alias: str, verification_rows) -> bool:
    for row in verification_rows:
        latest_id = (
            EventClusterVerification.objects.using(alias)
            .filter(cluster_id=row.cluster_id)
            .order_by("-version")
            .values_list("id", flat=True)
            .first()
        )
        if latest_id != row.id:
            return True
    return False


def _generation_events_for_run(run, *, alias: str):
    return list(
        OutboxMessage.objects.using(alias)
        .filter(topic="editorial.generate_requested", job_id=run.id)
        .order_by("message_key")
    )


def _all_run_generation_events_superseded(run, *, alias: str) -> bool:
    generation_events = _generation_events_for_run(run, alias=alias)
    if not generation_events:
        return False
    for event in generation_events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        verification_ids = payload.get("verification_ids")
        if (
            str(payload.get("run_id")) != str(run.id)
            or not isinstance(verification_ids, list)
            or not verification_ids
        ):
            return False
        rows = list(
            EventClusterVerification.objects.using(alias)
            .filter(id__in=verification_ids)
        )
        if len(rows) != len(verification_ids):
            return False
        if not _generation_is_superseded(alias, rows):
            return False
    return True


def _all_run_generation_events_resolved(run, *, alias: str) -> bool:
    generation_events = _generation_events_for_run(run, alias=alias)
    if not generation_events:
        return False
    for event in generation_events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        verification_id = str(payload.get("verification_id"))
        verification_ids = payload.get("verification_ids")
        if (
            str(payload.get("run_id")) != str(run.id)
            or not isinstance(verification_ids, list)
            or not verification_ids
        ):
            return False
        try:
            primary, rows = _load_generation_verifications(
                alias=alias,
                verification_id=verification_id,
                verification_ids=verification_ids,
            )
        except (TypeError, ValueError):
            return False
        try:
            _, expected_manifest_hash, _ = (
                generation_manifest_for_verifications(
                    run=run,
                    primary_verification=primary,
                    verifications=rows,
                )
            )
        except ValueError:
            return False
        if payload.get("generation_manifest_hash") != expected_manifest_hash:
            return False
        if _generation_is_superseded(alias, rows):
            continue
        try:
            identity = article_identity_for_verification(primary)
        except ValueError:
            return False
        if not DraftArticle.objects.using(alias).filter(
            article_identity_key=identity,
            current_revision__isnull=False,
        ).exists():
            return False
    return True


def _maybe_complete_reused_run(run, *, alias: str) -> bool:
    if run.articles.using(alias).filter(
        current_revision__isnull=False
    ).exists():
        return False
    if not _all_run_generation_events_resolved(run, alias=alias):
        return False
    return _complete_run_without_generation(run, alias=alias)


def _maybe_enqueue_auto_publication(run, audit_context):
    if not (
        run.trigger == "schedule"
        and run.approval_mode == "validated_auto"
    ):
        return False
    alias = audit_context.database_alias
    generation_events = _generation_events_for_run(run, alias=alias)
    expected_primary_ids = {
        str(event.payload.get("verification_id"))
        for event in generation_events
        if isinstance(event.payload, dict)
        and str(event.payload.get("run_id")) == str(run.id)
    }
    completed_articles = list(
        run.articles.using(alias)
        .select_for_update()
        .filter(
            source_verification_id__in=expected_primary_ids,
            current_revision__isnull=False,
        )
        .order_by("id")
    )
    if len(completed_articles) != len(expected_primary_ids):
        return False
    run_owned_articles = list(
        run.articles.using(alias)
        .select_for_update()
        .filter(current_revision__isnull=False)
        .order_by("id")
    )
    if len(run_owned_articles) != 1:
        return False
    dedupe_key = f"publication.scheduled_run_requested:{run.id}"
    if OutboxMessage.objects.using(alias).filter(
        message_key=dedupe_key
    ).exists():
        return False
    enqueue_event(
        event_type="publication.scheduled_run_requested",
        aggregate_type="collection_run",
        aggregate_id=run.id,
        job_id=run.id,
        dedupe_key=dedupe_key,
        correlation_id=audit_context.correlation_id,
        payload={"run_id": str(run.id)},
    )
    return True


@shared_task
def generate_verification_draft(
    verification_id: str,
    run_id: str,
    verification_ids: list[str],
    generation_manifest_hash: str,
):
    audit_context = _worker_audit_context(
        "verified event source grounded draft generation"
    )
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        run = (
            CollectionRun.objects.using(alias)
            .select_for_update()
            .get(id=run_id)
        )
        verification, verification_rows = _load_generation_verifications(
            alias=alias,
            verification_id=verification_id,
            verification_ids=verification_ids,
        )
        require_worker_event(
            context=audit_context,
            topic="editorial.generate_requested",
            aggregate_id=verification.id,
            payload_identity={
                "verification_id": str(verification.id),
                "run_id": str(run.id),
                "verification_ids": verification_ids,
                "generation_manifest_hash": generation_manifest_hash,
            },
        )
        list(
            EventCluster.objects.using(alias)
            .select_for_update()
            .filter(id__in={row.cluster_id for row in verification_rows})
            .order_by("id")
        )
        _, expected_manifest_hash, _ = generation_manifest_for_verifications(
            run=run,
            primary_verification=verification,
            verifications=verification_rows,
        )
        if expected_manifest_hash != generation_manifest_hash:
            raise ValueError("generation manifest no longer matches its frozen rows")
        if _generation_is_superseded(alias, verification_rows):
            if _all_run_generation_events_superseded(run, alias=alias):
                _complete_run_without_generation(run, alias=alias)
            return {
                "runId": str(run.id),
                "verificationId": str(verification.id),
                "state": "superseded",
            }
        article = build_source_grounded_draft(
            run,
            verification=verification,
            verification_rows=verification_rows,
            generation_manifest_hash=generation_manifest_hash,
            audit_context=audit_context,
        )
        if article.source_run_id != run.id:
            _maybe_complete_reused_run(run, alias=alias)
        _maybe_enqueue_auto_publication(run, audit_context)
    return {
        "runId": str(run.id),
        "verificationId": str(verification.id),
        "articleId": str(article.id),
        "state": article.state,
    }


@shared_task
def generate_run_draft(run_id: str):
    audit_context = _worker_audit_context("legacy verified draft generation")
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        run = (
            CollectionRun.objects.using(alias)
            .select_for_update()
            .get(id=run_id)
        )
        require_worker_event(
            context=audit_context,
            topic="run.draft_requested",
            aggregate_id=run.id,
            payload_identity={"run_id": str(run.id)},
        )
        verifications = list(
            EventClusterVerification.objects.using(alias)
            .filter(origin_run=run)
            .select_related("cluster", "origin_run")
            .order_by("cluster__canonical_key")
        )
        if not verifications:
            clusters = cluster_run_items(run.id, using=alias)
            verifications = [
                verify_event_cluster(
                    cluster.id,
                    run_id=run.id,
                    using=alias,
                )
                for cluster in clusters
            ]
        generation_units = _generation_work_units(run, verifications)
        if not generation_units:
            _complete_run_without_generation(run, alias=alias)
            return {
                "runId": str(run.id),
                "state": run.state,
                "reason": "no_generation_cluster",
            }
        list(
            EventCluster.objects.using(alias)
            .select_for_update()
            .filter(id__in={row.cluster_id for row in verifications})
            .order_by("id")
        )
        generation_units = [
            (verification, verification_rows)
            for verification, verification_rows in generation_units
            if not _generation_is_superseded(alias, verification_rows)
        ]
        if not generation_units:
            _complete_run_without_generation(run, alias=alias)
            return {
                "runId": str(run.id),
                "state": "superseded",
            }
        if run.state not in {
            RunState.VALIDATING,
            RunState.DRAFTING,
            RunState.AWAITING_APPROVAL,
        }:
            return {"runId": str(run.id), "state": run.state}
        articles = []
        for verification, verification_rows in generation_units:
            _, generation_manifest_hash, _ = (
                generation_manifest_for_verifications(
                    run=run,
                    primary_verification=verification,
                    verifications=verification_rows,
                )
            )
            articles.append(
                build_source_grounded_draft(
                    run,
                    verification=verification,
                    verification_rows=verification_rows,
                    generation_manifest_hash=generation_manifest_hash,
                    audit_context=audit_context,
                    event_topic="run.draft_requested",
                )
            )
        if articles and all(article.source_run_id != run.id for article in articles):
            _complete_run_without_generation(run, alias=alias)
        _maybe_enqueue_auto_publication(run, audit_context)
    return {
        "runId": str(run.id),
        "articleId": str(articles[0].id),
        "articleIds": [str(article.id) for article in articles],
        "state": articles[-1].state,
    }


def _editorial_terminal_material(run) -> dict:
    return {
        "schema_version": "collection-run-editorial-terminal-v1",
        "run_id": str(run.id),
        "state": run.state,
        "error_summary": run.error_summary,
        "recovery_state": run.recovery_state,
        "completed_at": (
            run.completed_at.isoformat() if run.completed_at else None
        ),
    }


def _finalize_editorial_delivery_failure(
    run_id: str,
    error_code: str,
    *,
    audit_context: AuditContext,
    verification_id: str | None = None,
):
    alias = audit_context.database_alias
    redacted_code = str(error_code)[:100]
    now = timezone.now()
    with transaction.atomic(using=alias):
        run = (
            CollectionRun.objects.using(alias)
            .select_for_update()
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
        before_material = _editorial_terminal_material(run)
        step, _ = RunStep.objects.using(alias).select_for_update().get_or_create(
            run=run,
            name="editorial",
            attempt_no=1,
            defaults={"correlation_id": run.correlation_id},
        )
        step.state = "failed"
        step.error_code = redacted_code
        step.error_detail_redacted = "editorial routed delivery exhausted"
        project_step_terminal_observation(
            step,
            run,
            finished_at=now,
            final_state=step.state,
            affected_count=max(step.input_count, 1),
            error_code=redacted_code,
            recovery_state=RecoveryState.MANUAL_REQUIRED,
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
            ),
            using=alias,
        )
        run.state = RunState.FAILED
        run.error_summary = {
            "stage": "editorial",
            "code": redacted_code,
            **(
                {"verificationId": str(verification_id)}
                if verification_id
                else {}
            ),
        }
        project_run_terminal_observation(
            run,
            finished_at=now,
            stage="editorial",
            final_state=run.state,
            affected_count=1,
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
            ),
            using=alias,
        )
        record_audit_event(
            context=audit_context,
            action="collection_run.editorial_failed",
            entity=run,
            identity_key=audit_context.event_key,
            material_schema_version=(
                "collection-run-editorial-terminal-v1"
            ),
            before_material=before_material,
            after_material=_editorial_terminal_material(run),
            metadata={
                "collection_run_id": str(run.id),
                "error_code": redacted_code,
                "result": "failed",
                "state": run.state,
                **(
                    {"verification_id": str(verification_id)}
                    if verification_id
                    else {}
                ),
            },
        )
        return {
            "runId": str(run.id),
            "state": run.state,
            "code": redacted_code,
        }


@shared_task(name="apps.editorial.tasks.finalize_editorial_run_delivery_failure")
def finalize_editorial_run_delivery_failure(
    run_id: str,
    error_code: str,
):
    audit_context = _worker_audit_context(
        "editorial routed delivery exhausted"
    )
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        event = OutboxMessage.objects.using(alias).get(
            pk=audit_context.event_key
        )
        if event.topic not in {
            "run.evidence_ready",
            "editorial.cluster_requested",
        }:
            raise ValueError("unexpected editorial terminal event topic")
        require_worker_event(
            context=audit_context,
            topic=event.topic,
            aggregate_id=run_id,
            payload_identity={},
        )
        return _finalize_editorial_delivery_failure(
            run_id,
            error_code,
            audit_context=audit_context,
        )


@shared_task(
    name="apps.editorial.tasks.finalize_editorial_generation_delivery_failure"
)
def finalize_editorial_generation_delivery_failure(
    run_id: str,
    verification_id: str,
    error_code: str,
):
    audit_context = _worker_audit_context(
        "verified generation delivery exhausted"
    )
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        event = OutboxMessage.objects.using(alias).get(
            pk=audit_context.event_key
        )
        require_worker_event(
            context=audit_context,
            topic="editorial.generate_requested",
            aggregate_id=verification_id,
            payload_identity={
                "run_id": str(run_id),
                "verification_id": str(verification_id),
            },
        )
        run = (
            CollectionRun.objects.using(alias)
            .select_for_update()
            .get(pk=run_id)
        )
        payload = event.payload if isinstance(event.payload, dict) else {}
        frozen_ids = payload.get("verification_ids")
        if isinstance(frozen_ids, list) and frozen_ids:
            try:
                _, verification_rows = _load_generation_verifications(
                    alias=alias,
                    verification_id=str(verification_id),
                    verification_ids=frozen_ids,
                )
            except (TypeError, ValueError):
                verification_rows = []
            if verification_rows:
                list(
                    EventCluster.objects.using(alias)
                    .select_for_update()
                    .filter(
                        id__in={
                            row.cluster_id for row in verification_rows
                        }
                    )
                    .order_by("id")
                )
                if _generation_is_superseded(alias, verification_rows):
                    if _all_run_generation_events_superseded(
                        run,
                        alias=alias,
                    ):
                        _complete_run_without_generation(run, alias=alias)
                    return {
                        "runId": str(run.id),
                        "verificationId": str(verification_id),
                        "state": "superseded",
                    }
        return _finalize_editorial_delivery_failure(
            run_id,
            error_code,
            audit_context=audit_context,
            verification_id=verification_id,
        )
