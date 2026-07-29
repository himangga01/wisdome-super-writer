from __future__ import annotations

import base64
import uuid

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from adapters.publishers.wordpress import WordPressPublisher
from adapters.storage import S3ObjectStorage
from apps.audit.services import AuditContext
from wisdome_writer.domain.errors import Conflict
from wisdome_writer.domain.hashing import sha256_hex
from wisdome_writer.infrastructure.outbox import (
    CURRENT_EVENT_CONSUMER_LEASE_GENERATION,
    CURRENT_EVENT_CONSUMER_LEASE_TOKEN,
    CURRENT_EVENT_CONSUMER_NAME,
    CURRENT_EVENT_CORRELATION_ID,
    CURRENT_EVENT_ID,
    PermanentEventError,
    enqueue_event,
)

from .contracts import PublishCommand, PublisherError, RenderedArticle
from .models import (
    Publication,
    PublicationAttempt,
    PublicationMedia,
    PublicDeliveryAsset,
    TargetDisconnectDecision,
)
from .services import (
    _kill_switch_enabled,
    begin_attempt,
    begin_canary_run,
    begin_remote_media_reconcile,
    begin_reconcile,
    begin_target_credential_revoke,
    finalize_reconcile_delivery_failure,
    persist_canary_run_result,
    persist_publish_result,
    persist_remote_media_reconcile_result,
    persist_target_credential_revoke_result,
    publisher_error_result,
    publisher_for_target,
    run_target_preflight,
)


def _worker_audit_context() -> AuditContext:
    event_id = CURRENT_EVENT_ID.get()
    correlation_id = CURRENT_EVENT_CORRELATION_ID.get()
    consumer_name = CURRENT_EVENT_CONSUMER_NAME.get()
    lease_token = CURRENT_EVENT_CONSUMER_LEASE_TOKEN.get()
    lease_generation = CURRENT_EVENT_CONSUMER_LEASE_GENERATION.get()
    if (
        event_id is None
        or correlation_id is None
        or consumer_name is None
        or lease_token is None
        or lease_generation is None
    ):
        raise PermanentEventError("audit_event_context_missing")
    try:
        return AuditContext.for_worker(
            correlation_id=correlation_id,
            event_key=event_id,
            consumer_name=consumer_name,
            lease_token=lease_token,
            lease_generation=lease_generation,
        )
    except ValueError as exc:
        raise PermanentEventError("audit_event_context_invalid") from exc


@shared_task(name="apps.publishing.tasks.dispatch_scheduled_run_publication")
def dispatch_scheduled_run_publication(run_id: str):
    from .automation import dispatch_validated_schedule_run

    audit_context = _worker_audit_context()
    rows = dispatch_validated_schedule_run(
        run_id,
        audit_context=audit_context,
    )
    return {"runId": run_id, "attemptIds": [str(row.id) for row in rows]}


@shared_task(
    name="apps.publishing.tasks.run_target_preflight",
    acks_late=True,
)
def run_target_preflight_task(
    target_id: str,
    target_snapshot_id: str,
    target_config_hash: str,
):
    audit_context = _worker_audit_context()
    target, applied = run_target_preflight(
        target_id,
        expected_snapshot_id=target_snapshot_id,
        expected_config_hash=target_config_hash,
        audit_context=audit_context,
    )
    return {
        "targetId": str(target.id),
        "connectionState": target.connection_state,
        "preflightState": target.preflight_state,
        "snapshotId": str(target.current_snapshot_id),
        "applied": applied,
    }


@shared_task(
    name="apps.publishing.tasks.revoke_target_credentials",
    acks_late=True,
)
def revoke_target_credentials(decision_id: str):
    audit_context = _worker_audit_context()
    prepared = begin_target_credential_revoke(
        decision_id,
        audit_context=audit_context,
    )
    if prepared is None:
        decision = TargetDisconnectDecision.objects.get(id=decision_id)
        return {"decisionId": decision_id, "state": decision.state}
    decision, target, fence = prepared
    adapter = publisher_for_target(target)
    try:
        adapter.revoke_credentials()
        outcome_hash = sha256_hex({"targetId": str(target.id), "result": "revoked"})
    except PublisherError as exc:
        outcome_hash = sha256_hex(
            {
                "targetId": str(target.id),
                "result": exc.code,
                "status": exc.http_status,
            }
        )
        decision = persist_target_credential_revoke_result(
            fence,
            succeeded=False,
            outcome_hash=outcome_hash,
            error_code=exc.code,
            unknown_outcome=exc.category == "unknown_outcome",
            audit_context=audit_context,
        )
        return {"decisionId": decision_id, "state": decision.state}
    finally:
        adapter.close()
    decision = persist_target_credential_revoke_result(
        fence,
        succeeded=True,
        outcome_hash=outcome_hash,
        audit_context=audit_context,
    )
    return {"decisionId": decision_id, "state": decision.state}


@shared_task(
    name="apps.publishing.tasks.execute_publication_attempt",
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_publication_attempt(attempt_id: str):
    audit_context = _worker_audit_context()
    attempt, command = begin_attempt(
        attempt_id,
        audit_context=audit_context,
    )
    if attempt.state == PublicationAttempt.State.SUCCEEDED:
        return {"attemptId": attempt_id, "state": "succeeded"}
    if attempt.state == PublicationAttempt.State.STALE:
        return {
            "attemptId": attempt_id,
            "state": attempt.state,
            "code": attempt.error_code,
        }
    if command is None:
        return {
            "attemptId": attempt_id,
            "state": attempt.state,
            "reconcileQueued": True,
        }
    adapter = publisher_for_target(attempt.publication.target)
    error: PublisherError | None = None
    try:
        result = adapter.execute(command)
        if (
            result.status == "succeeded"
            and attempt.publication.target.channel == "wordpress"
            and attempt.resolved_action in {"create", "update", "mark_withdrawn"}
        ):
            if not result.remote_url or not adapter.verify_public_url(result.remote_url):
                raise PublisherError(
                    "wordpress_public_url_not_ready",
                    category="unknown_outcome",
                    http_status=result.http_status,
                )
    except PublisherError as exc:
        error = exc
        result = publisher_error_result(exc)
    finally:
        adapter.close()
    persisted = persist_publish_result(
        attempt_id,
        result,
        audit_context=audit_context,
    )
    if persisted.state == PublicationAttempt.State.RETRYABLE_FAILED:
        raise error or RuntimeError(persisted.error_code)
    return {"attemptId": attempt_id, "state": persisted.state, "remotePostId": persisted.publication.remote_post_id}


@shared_task(
    name="apps.publishing.tasks.reconcile_publication_attempt",
    acks_late=True,
)
def reconcile_publication_attempt(
    attempt_id: str,
    reconcile_attempt_no: int | None = None,
):
    audit_context = _worker_audit_context()
    source_event_id = audit_context.event_key
    attempt, generation, command = begin_reconcile(
        attempt_id,
        expected_reconcile_attempt_no=reconcile_attempt_no,
        source_event_id=source_event_id,
        audit_context=audit_context,
    )
    if attempt.state == PublicationAttempt.State.SUCCEEDED:
        return {"attemptId": attempt_id, "state": "succeeded"}
    if command is None or generation is None:
        return {
            "attemptId": attempt_id,
            "state": attempt.state,
            "reconcileAttemptNo": (
                generation.generation
                if generation is not None
                else attempt.reconcile_attempt_no
            ),
            "duplicate": True,
        }
    adapter = publisher_for_target(attempt.publication.target)
    retry_after = None
    try:
        result = adapter.reconcile(command)
        if (
            result.status == "succeeded"
            and attempt.publication.target.channel == "wordpress"
            and result.remote_state == "published"
            and (not result.remote_url or not adapter.verify_public_url(result.remote_url))
        ):
            raise PublisherError("wordpress_public_url_not_ready", category="retryable")
    except PublisherError as exc:
        result = publisher_error_result(exc)
        retry_after = exc.retry_after_seconds
    finally:
        adapter.close()
    persisted = persist_publish_result(
        attempt_id,
        result,
        expected_reconcile_generation=generation.generation,
        expected_reconcile_event_id=source_event_id,
        retry_after_seconds=retry_after,
        audit_context=audit_context,
    )
    return {
        "attemptId": attempt_id,
        "state": persisted.state,
        "reconcileAttemptNo": persisted.reconcile_attempt_no,
    }


@shared_task(
    name="apps.publishing.tasks.finalize_publication_reconcile_failure"
)
def finalize_publication_reconcile_failure(
    attempt_id: str,
    error_code: str,
):
    audit_context = _worker_audit_context()
    source_event_id = audit_context.event_key
    attempt = finalize_reconcile_delivery_failure(
        attempt_id,
        source_event_id=source_event_id,
        error_code=error_code,
        audit_context=audit_context,
    )
    if attempt is None:
        return {"attemptId": attempt_id, "state": "missing"}
    return {
        "attemptId": str(attempt.id),
        "state": attempt.state,
        "reconcileAttemptNo": attempt.reconcile_attempt_no,
    }


@shared_task(
    name="apps.publishing.tasks.run_target_canary",
    acks_late=True,
)
def run_target_canary(canary_run_id: str):
    audit_context = _worker_audit_context()
    run, fence = begin_canary_run(
        canary_run_id,
        audit_context=audit_context,
    )
    if fence is None:
        return {"canaryRunId": canary_run_id, "state": run.state}

    adapter = publisher_for_target(run.target)
    stages: list[dict[str, object]] = []
    remote_post_id = None
    remote_media_id = None
    lookup_key = f"ww-canary-{run.id.hex}"
    base_article = RenderedArticle(
        article_id=str(run.id),
        revision_no=1,
        channel_role=run.target.role,
        render_stage="final",
        title=f"Wisdome Writer 연결 검증 {run.id.hex[:8]}",
        body_html="<p>격리된 발행 연결 검증용 게시물입니다.</p>",
        source_links=(),
        included_claim_ids=(),
        canonical_link_state="not_applicable" if run.target.channel == "wordpress" else "resolved",
        canonical_source_url=run.target.base_url if run.target.channel == "blogger" else None,
        template_hash=sha256_hex({"canary": str(run.id)}),
        content_hash=sha256_hex({"canaryBody": str(run.id)}),
        source_manifest_hash=sha256_hex([]),
        labels=("wisdome-canary",),
    )

    def command(action: str, *, remote_id: str | None = None, revision: int = 1) -> PublishCommand:
        article = base_article
        if revision != 1:
            article = RenderedArticle(
                **{
                    **base_article.__dict__,
                    "revision_no": revision,
                    "body_html": "<p>격리된 발행 연결 검증용 수정 게시물입니다.</p>",
                    "content_hash": sha256_hex({"canaryBody": str(run.id), "revision": revision}),
                }
            )
        return PublishCommand(
            publication_attempt_id=str(uuid.uuid4()),
            action=action,
            target_command_hash=sha256_hex({"action": action, "run": str(run.id)}),
            idempotency_key=f"canary:{run.id}:{action}:{revision}",
            remote_lookup_key=lookup_key,
            target_id=str(run.target_id),
            publication_intent_id=str(run.id),
            approval_id=str(run.id),
            approval_subject_hash=run.target_snapshot.config_hash,
            target_snapshot_id=str(run.target_snapshot_id),
            target_config_hash=run.target_snapshot.config_hash,
            publisher_contract_version=run.target_snapshot.publisher_contract_version,
            publisher_adapter_manifest_hash=run.target_snapshot.publisher_adapter_manifest_hash,
            remote_post_id=remote_id,
            rendered_article=None if action == "unpublish" else article,
            requested_at=timezone.now(),
            correlation_id=str(audit_context.correlation_id),
        )

    try:
        if _kill_switch_enabled():
            raise Conflict("전역 kill switch가 활성화되어 canary 쓰기가 차단되었습니다.")
        created = adapter.execute(command("create"))
        remote_post_id = created.remote_post_id
        stages.append({"code": "create", "passed": created.status == "succeeded"})
        if not remote_post_id:
            raise PublisherError("canary_create_missing_remote_id", category="permanent")
        if _kill_switch_enabled():
            raise Conflict("전역 kill switch가 활성화되어 canary 쓰기가 차단되었습니다.")
        updated = adapter.execute(command("update", remote_id=remote_post_id, revision=2))
        stages.append({"code": "update", "passed": updated.status == "succeeded"})
        fetched = adapter.fetch_remote_state(remote_post_id)
        stages.append({"code": "read_after_write", "passed": fetched.status == "succeeded"})
        if isinstance(adapter, WordPressPublisher):
            if _kill_switch_enabled():
                raise Conflict("전역 kill switch가 활성화되어 canary 쓰기가 차단되었습니다.")
            png = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
            marker = f"wisdome-canary-media:{run.id}"
            media = adapter.upload_media(
                content=png,
                filename="wisdome-canary.png",
                mime_type="image/png",
                remote_lookup_key=f"ww-canary-media-{run.id.hex}",
                alt_text="연결 검증 이미지",
                caption="자동 삭제되는 연결 검증 자산",
                description_marker=marker,
            )
            remote_media_id = media.remote_post_id
            stages.append({"code": "media_upload", "passed": media.status == "succeeded"})
        if _kill_switch_enabled():
            raise Conflict("전역 kill switch가 활성화되어 canary 쓰기가 차단되었습니다.")
        withdrawn = adapter.execute(command("unpublish", remote_id=remote_post_id))
        stages.append({"code": "unpublish", "passed": withdrawn.status == "succeeded"})
        if remote_media_id and isinstance(adapter, WordPressPublisher):
            deleted_media = adapter.delete_media(remote_media_id)
            stages.append({"code": "media_cleanup", "passed": deleted_media.status == "succeeded"})
            remote_media_id = None
        deleted = adapter.delete_post(remote_post_id)
        stages.append({"code": "post_cleanup", "passed": deleted.status == "succeeded"})
        remote_post_id = None
        passed = all(bool(row["passed"]) for row in stages)
    except Exception as exc:
        stages.append({"code": getattr(exc, "code", exc.__class__.__name__), "passed": False})
        passed = False
    finally:
        if remote_media_id and isinstance(adapter, WordPressPublisher):
            try:
                adapter.delete_media(remote_media_id)
            except Exception:
                stages.append({"code": "media_cleanup_pending", "passed": False})
        if remote_post_id:
            try:
                adapter.delete_post(remote_post_id)
            except Exception:
                stages.append({"code": "post_cleanup_pending", "passed": False})
        adapter.close()

    run = persist_canary_run_result(
        fence,
        stages=stages,
        passed=passed,
        audit_context=audit_context,
    )
    return {"canaryRunId": canary_run_id, "state": run.state, "reportHash": run.report_hash}


@shared_task(
    name="apps.publishing.tasks.delete_public_delivery_asset",
    acks_late=True,
)
def delete_public_delivery_asset(asset_id: str, expected_lease_generation: int):
    with transaction.atomic():
        asset = PublicDeliveryAsset.objects.select_for_update().get(id=asset_id)
        if asset.state == PublicDeliveryAsset.State.DELETED:
            return {"assetId": asset_id, "state": asset.state}
        protected = PublicationMedia.objects.filter(
            public_delivery_asset=asset,
            binding_state__in=[PublicationMedia.BindingState.PREPARED, PublicationMedia.BindingState.ACTIVE],
        ).exists()
        if (
            asset.lease_generation != expected_lease_generation
            or protected
            or asset.active_reference_count
            or not asset.delete_after
            or asset.delete_after > timezone.now()
        ):
            return {"assetId": asset_id, "state": "protected"}
        object_key = asset.delivery_object_key
        object_version = asset.delivery_object_version
    S3ObjectStorage().delete(key=object_key, version_id=object_version or None)
    with transaction.atomic():
        asset = PublicDeliveryAsset.objects.select_for_update().get(id=asset_id)
        if asset.lease_generation != expected_lease_generation:
            return {"assetId": asset_id, "state": "lease_changed"}
        asset.state = PublicDeliveryAsset.State.DELETED
        asset.deleted_at = timezone.now()
        asset.delete_reason = "zero_references_after_grace"
        asset.save(update_fields=["state", "deleted_at", "delete_reason"])
    return {"assetId": asset_id, "state": "deleted"}


@shared_task(name="apps.publishing.tasks.reconcile_remote_media")
def reconcile_remote_media(
    remote_media_id: str,
    publication_attempt_id: str,
    publication_intent_id: str,
):
    audit_context = _worker_audit_context()
    remote, fence = begin_remote_media_reconcile(
        remote_media_id,
        publication_attempt_id=publication_attempt_id,
        publication_intent_id=publication_intent_id,
        audit_context=audit_context,
    )
    if fence is None:
        return {"remoteMediaId": remote_media_id, "state": remote.state}
    adapter = publisher_for_target(remote.target)
    try:
        marker = f"wisdome-media:{remote.id}"
        result = adapter.find_media(remote.remote_lookup_key, marker)
    finally:
        adapter.close()
    remote = persist_remote_media_reconcile_result(
        fence,
        result,
        audit_context=audit_context,
    )
    return {"remoteMediaId": remote_media_id, "state": remote.state}
