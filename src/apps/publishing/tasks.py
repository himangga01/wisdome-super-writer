from __future__ import annotations

import base64
import uuid
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from adapters.publishers.wordpress import WordPressPublisher
from adapters.storage import S3ObjectStorage
from wisdome_writer.domain.errors import Conflict
from wisdome_writer.domain.hashing import sha256_hex
from wisdome_writer.infrastructure.outbox import enqueue_event

from .contracts import PublishCommand, PublisherError, RenderedArticle
from .models import (
    PublicationAttempt,
    PublicationMedia,
    PublicationTarget,
    PublicDeliveryAsset,
    RemoteMedia,
    TargetCanaryRun,
    TargetDisconnectDecision,
    ValidationState,
)
from .services import (
    _kill_switch_enabled,
    _snapshot_locked,
    begin_attempt,
    begin_reconcile,
    persist_publish_result,
    publisher_error_result,
    publisher_for_target,
    run_target_preflight,
)


@shared_task(name="apps.publishing.tasks.dispatch_scheduled_run_publication")
def dispatch_scheduled_run_publication(run_id: str):
    from apps.collection.models import CollectionRun, RunState
    from apps.scheduling.services import release_waiting_for_topic
    from .automation import dispatch_validated_schedule_run

    try:
        rows = dispatch_validated_schedule_run(run_id)
        return {"runId": run_id, "attemptIds": [str(row.id) for row in rows]}
    except Exception as exc:
        run = CollectionRun.objects.get(id=run_id)
        run.state = RunState.FAILED
        run.error_summary = {"stage": "auto_publish", "code": exc.__class__.__name__}
        run.finished_at = timezone.now() if hasattr(run, "finished_at") else None
        update_fields = ["state", "error_summary"]
        if hasattr(run, "finished_at"):
            update_fields.append("finished_at")
        run.save(update_fields=update_fields)
        release_waiting_for_topic(run.topic_code)
        raise


def _retry_countdown(attempt_no: int, retry_after: int | None = None) -> int:
    if retry_after is not None:
        return min(max(retry_after, 5), 3600)
    return min(15 * (2 ** max(attempt_no - 1, 0)), 1800)


@shared_task(
    bind=True,
    name="apps.publishing.tasks.run_target_preflight",
    max_retries=3,
    acks_late=True,
)
def run_target_preflight_task(self, target_id: str):
    try:
        target = run_target_preflight(target_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries + 1))
    return {
        "targetId": str(target.id),
        "connectionState": target.connection_state,
        "preflightState": target.preflight_state,
        "snapshotId": str(target.current_snapshot_id),
    }


@shared_task(
    bind=True,
    name="apps.publishing.tasks.revoke_target_credentials",
    max_retries=4,
    acks_late=True,
)
def revoke_target_credentials(self, decision_id: str):
    with transaction.atomic():
        decision = TargetDisconnectDecision.objects.select_for_update().select_related("target").get(
            id=decision_id
        )
        if decision.state == TargetDisconnectDecision.State.COMPLETED:
            return {"decisionId": decision_id, "state": decision.state}
        decision.state = TargetDisconnectDecision.State.REVOKING
        decision.save(update_fields=["state"])
        target = decision.target
    adapter = publisher_for_target(target)
    try:
        adapter.revoke_credentials()
        outcome_hash = sha256_hex({"targetId": str(target.id), "result": "revoked"})
    except PublisherError as exc:
        with transaction.atomic():
            decision = TargetDisconnectDecision.objects.select_for_update().get(id=decision_id)
            decision.state = (
                TargetDisconnectDecision.State.RECONCILING
                if exc.category == "unknown_outcome"
                else TargetDisconnectDecision.State.FAILED
            )
            decision.remote_result_hash = sha256_hex(
                {"targetId": str(target.id), "result": exc.code, "status": exc.http_status}
            )
            decision.save(update_fields=["state", "remote_result_hash"])
        if exc.category in {"retryable", "unknown_outcome"}:
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries + 1))
        return {"decisionId": decision_id, "state": decision.state}
    finally:
        adapter.close()
    with transaction.atomic():
        decision = TargetDisconnectDecision.objects.select_for_update().get(id=decision_id)
        target = PublicationTarget.objects.select_for_update().get(id=decision.target_id)
        target.credential_ref = None
        target.username_ref = None
        target.save(update_fields=["credential_ref", "username_ref", "updated_at"])
        _snapshot_locked(target)
        decision.state = TargetDisconnectDecision.State.COMPLETED
        decision.remote_result_hash = outcome_hash
        decision.save(update_fields=["state", "remote_result_hash"])
    return {"decisionId": decision_id, "state": decision.state}


@shared_task(
    bind=True,
    name="apps.publishing.tasks.execute_publication_attempt",
    max_retries=6,
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_publication_attempt(self, attempt_id: str):
    try:
        attempt, command = begin_attempt(attempt_id)
    except Conflict as exc:
        return {"attemptId": attempt_id, "state": "stale", "code": exc.code}
    if attempt.state == PublicationAttempt.State.SUCCEEDED:
        return {"attemptId": attempt_id, "state": "succeeded"}
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
                    category="retryable",
                    http_status=result.http_status,
                )
    except PublisherError as exc:
        error = exc
        result = publisher_error_result(exc)
    finally:
        adapter.close()
    with transaction.atomic():
        persisted = persist_publish_result(attempt_id, result)
        if persisted.state == PublicationAttempt.State.UNKNOWN_OUTCOME:
            enqueue_event(
                event_type="publication.reconcile_requested",
                aggregate_type="publication_attempt",
                aggregate_id=persisted.id,
                job_id=persisted.id,
                dedupe_key=(
                    f"publication.reconcile_requested:{persisted.id}:"
                    f"{persisted.attempt_no}:automatic"
                ),
                available_at=timezone.now() + timedelta(seconds=10),
                payload={
                    "publication_attempt_id": str(persisted.id),
                    "channel": persisted.publication.target.channel,
                },
            )
    if persisted.state == PublicationAttempt.State.RETRYABLE_FAILED:
        if persisted.attempt_no >= self.max_retries + 1:
            return {"attemptId": attempt_id, "state": persisted.state, "code": persisted.error_code}
        PublicationAttempt.objects.filter(id=attempt_id).update(attempt_no=persisted.attempt_no + 1)
        raise self.retry(
            exc=error or RuntimeError(persisted.error_code),
            countdown=_retry_countdown(
                persisted.attempt_no,
                error.retry_after_seconds if error else None,
            ),
        )
    return {"attemptId": attempt_id, "state": persisted.state, "remotePostId": persisted.publication.remote_post_id}


@shared_task(
    bind=True,
    name="apps.publishing.tasks.reconcile_publication_attempt",
    max_retries=4,
    acks_late=True,
)
def reconcile_publication_attempt(self, attempt_id: str):
    try:
        attempt, command = begin_reconcile(attempt_id)
    except Conflict as exc:
        return {"attemptId": attempt_id, "state": "ignored", "code": exc.code}
    if attempt.state == PublicationAttempt.State.SUCCEEDED:
        return {"attemptId": attempt_id, "state": "succeeded"}
    adapter = publisher_for_target(attempt.publication.target)
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
    finally:
        adapter.close()
    persisted = persist_publish_result(attempt_id, result)
    if persisted.state == PublicationAttempt.State.RETRYABLE_FAILED:
        raise self.retry(countdown=_retry_countdown(persisted.attempt_no))
    return {"attemptId": attempt_id, "state": persisted.state}


@shared_task(
    bind=True,
    name="apps.publishing.tasks.run_target_canary",
    max_retries=1,
    acks_late=True,
)
def run_target_canary(self, canary_run_id: str):
    with transaction.atomic():
        run = TargetCanaryRun.objects.select_for_update().select_related("target").get(id=canary_run_id)
        if run.state == TargetCanaryRun.State.PASSED:
            return {"canaryRunId": canary_run_id, "state": run.state}
        if run.target.current_snapshot_id != run.target_snapshot_id:
            run.state = TargetCanaryRun.State.FAILED
            run.stage_results = [{"code": "target_snapshot_stale", "passed": False}]
            run.finished_at = timezone.now()
            run.save(update_fields=["state", "stage_results", "finished_at"])
            return {"canaryRunId": canary_run_id, "state": run.state}
        run.state = TargetCanaryRun.State.RUNNING
        run.save(update_fields=["state"])

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
            correlation_id=str(run.id),
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

    with transaction.atomic():
        run = TargetCanaryRun.objects.select_for_update().select_related("target").get(id=canary_run_id)
        run.stage_results = stages
        run.report_hash = sha256_hex(stages)
        run.state = TargetCanaryRun.State.PASSED if passed else TargetCanaryRun.State.FAILED
        if any(row["code"].endswith("cleanup_pending") for row in stages):
            run.state = TargetCanaryRun.State.CLEANUP_REQUIRED
        run.finished_at = timezone.now()
        run.save(update_fields=["stage_results", "report_hash", "state", "finished_at"])
        target = PublicationTarget.objects.select_for_update().get(id=run.target_id)
        target.canary_state = ValidationState.PASSED if passed else ValidationState.FAILED
        target.canary_policy_version = run.policy_version if passed else target.canary_policy_version
        target.last_canary_at = timezone.now()
        if passed:
            target.connection_state = PublicationTarget.ConnectionState.VERIFIED
        target.save()
        _snapshot_locked(target)
    return {"canaryRunId": canary_run_id, "state": run.state, "reportHash": run.report_hash}


@shared_task(
    bind=True,
    name="apps.publishing.tasks.delete_public_delivery_asset",
    max_retries=5,
    acks_late=True,
)
def delete_public_delivery_asset(self, asset_id: str, expected_lease_generation: int):
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
    try:
        S3ObjectStorage().delete(key=object_key, version_id=object_version or None)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries + 1))
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
def reconcile_remote_media(remote_media_id: str):
    remote = RemoteMedia.objects.select_related("target").get(id=remote_media_id)
    if remote.state not in {RemoteMedia.State.RECONCILING, RemoteMedia.State.UPLOADING}:
        return {"remoteMediaId": remote_media_id, "state": remote.state}
    adapter = publisher_for_target(remote.target)
    try:
        marker = f"wisdome-media:{remote.id}"
        result = adapter.find_media(remote.remote_lookup_key, marker)
    finally:
        adapter.close()
    with transaction.atomic():
        remote = RemoteMedia.objects.select_for_update().get(id=remote_media_id)
        if result.status == "succeeded":
            remote.remote_media_id = result.remote_post_id
            remote.remote_source_url = result.remote_url
            remote.state = RemoteMedia.State.AVAILABLE
        else:
            remote.state = RemoteMedia.State.FAILED
        remote.last_reconciled_at = timezone.now()
        remote.last_reconcile_hash = sha256_hex(
            {"status": result.status, "remoteMediaId": result.remote_post_id, "url": result.remote_url}
        )
        remote.save()
    return {"remoteMediaId": remote_media_id, "state": remote.state}
