from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.audit.services import (
    AuditContext,
    record_audit_event,
    require_audit_replay,
)
from apps.collection.models import CollectionRun, RecoveryState, RunState, RunStep
from apps.scheduling.models import ScheduleDispatch
from wisdome_writer.domain.errors import Conflict, InvalidInput

from .models import (
    Approval,
    ApprovalMode,
    AutoPublishActivation,
    AutoPublishValidation,
    ChannelCode,
    Publication,
    PublicationIntent,
    PublicationTarget,
)
from .services import (
    _StaleIntentConflict,
    _latest_approval_locked,
    _lock_target_intent_fences,
    _raise_persisted_stale_intent,
    _require_intent_revision_publishable,
    _require_worker_event,
    _require_revision_for_commands,
    create_publication_intent,
    decide_approval,
    dispatch_publication,
)


def _require_frozen_schedule(schedule_dispatch: ScheduleDispatch):
    schedule = schedule_dispatch.schedule
    if int(schedule.version) != int(schedule_dispatch.schedule_version):
        raise Conflict("schedule version changed after this run was dispatched")
    if not schedule.enabled:
        raise Conflict("schedule was disabled after this run was dispatched")
    return schedule


def _lock_automation_targets(
    requested_ids: list[str],
) -> dict[str, PublicationTarget]:
    _lock_target_intent_fences(requested_ids)
    return {
        str(row.id): row
        for row in PublicationTarget.objects.select_for_update()
        .filter(id__in=requested_ids)
        .order_by("id")
    }


def finalize_scheduled_publication_delivery_failure(
    run_id: str,
    error_code: str,
    *,
    audit_context: AuditContext,
):
    """Fail-close a scheduled publication delivery that never reached dispatch."""

    _require_worker_event(
        audit_context,
        topic="publication.scheduled_run_requested",
        aggregate_id=run_id,
        payload_identity={"run_id": str(run_id)},
    )
    alias = audit_context.database_alias
    redacted_code = str(error_code or "scheduled_publication_delivery_exhausted")[:100]
    with transaction.atomic(using=alias):
        run = (
            CollectionRun.objects.using(alias)
            .select_for_update()
            .filter(pk=run_id)
            .first()
        )
        if run is None:
            return {"runId": str(run_id), "state": "missing"}
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.STOPPED}:
            return {"runId": str(run.id), "state": run.state}
        article = (
            run.articles.select_for_update()
            .first()
        )
        schedule_dispatch = (
            ScheduleDispatch.objects.using(alias)
            .select_for_update()
            .filter(collection_run=run)
            .first()
        )
        if article is not None and schedule_dispatch is not None:
            request_key = f"schedule:{schedule_dispatch.id}:intent"
            existing = (
                PublicationIntent.objects.using(alias)
                .filter(article_id=article.id, request_key=request_key)
                .first()
            )
            if existing is not None and existing.state == PublicationIntent.State.DISPATCHED:
                attempts = list(existing.attempts.all())
                if len(attempts) == len(existing.target_commands):
                    require_audit_replay(
                        context=audit_context,
                        action="collection_run.publication_started",
                        entity=run,
                        identity_key=(
                            f"{audit_context.event_key}:collection-run-publication"
                        ),
                    )
                    return {"runId": str(run.id), "state": run.state}

        from apps.collection.services import (
            project_run_terminal_observation,
            project_step_terminal_observation,
        )

        now = timezone.now()
        step, _ = (
            RunStep.objects.using(alias)
            .select_for_update()
            .get_or_create(
                run=run,
                name="publish",
                attempt_no=1,
                defaults={"correlation_id": run.correlation_id},
            )
        )
        step.state = "failed"
        step.error_code = redacted_code
        step.error_detail_redacted = "scheduled publication delivery exhausted"
        step.retry_at = None
        step.lease_owner = ""
        step.lease_token = None
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
                "state",
                "error_code",
                "error_detail_redacted",
                "finished_at",
                "duration_ms",
                "retry_at",
                "terminal_impact",
                "recovery_state",
                "lease_owner",
                "lease_token",
            ),
            using=alias,
        )
        run.state = RunState.FAILED
        run.error_summary = {
            "stage": "publishing",
            "code": redacted_code,
        }
        project_run_terminal_observation(
            run,
            finished_at=now,
            stage="publishing",
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
        return {"runId": str(run.id), "state": run.state}


def _validate_automation_material(schedule, targets: dict[str, PublicationTarget]) -> None:
    target_ids = set(targets)
    validation_refs = schedule.auto_publish_validation_refs
    activation_refs = schedule.auto_publish_activation_refs
    if {str(row.get("targetId")) for row in validation_refs} != target_ids:
        raise InvalidInput("일정의 자동발행 validation target 집합이 현재 target과 다릅니다.")
    if {str(row.get("targetId")) for row in activation_refs} != target_ids:
        raise InvalidInput("일정의 자동발행 activation target 집합이 현재 target과 다릅니다.")
    for ref in validation_refs:
        target = targets[str(ref["targetId"])]
        validation = AutoPublishValidation.objects.get(
            id=ref["validationId"],
            target=target,
            status=AutoPublishValidation.State.PASSED,
        )
        if (
            str(validation.target_snapshot_id) != str(ref["targetSnapshotId"])
            or validation.material_hash != ref["materialHash"]
            or str(target.current_snapshot_id) != str(ref["targetSnapshotId"])
        ):
            raise Conflict("자동발행 validation material이 현재 target과 일치하지 않습니다.")
    for ref in activation_refs:
        target = targets[str(ref["targetId"])]
        activation = AutoPublishActivation.objects.get(
            id=ref["activationId"],
            target=target,
            decision=AutoPublishActivation.Decision.ENABLED,
        )
        if (
            str(target.latest_auto_publish_activation_id) != str(activation.id)
            or not target.auto_publish_enabled
            or activation.version != int(ref["version"])
            or activation.activation_hash != ref["activationHash"]
            or str(activation.target_snapshot_id) != str(ref["targetSnapshotId"])
        ):
            raise Conflict("자동발행 activation이 만료되었거나 현재 target과 다릅니다.")


def dispatch_validated_schedule_run(
    run_id: str,
    *,
    audit_context: AuditContext,
):
    try:
        return _dispatch_validated_schedule_run_atomic(
            run_id,
            audit_context=audit_context,
        )
    except _StaleIntentConflict as exc:
        _raise_persisted_stale_intent(exc)


@transaction.atomic
def _dispatch_validated_schedule_run_atomic(
    run_id: str,
    *,
    audit_context: AuditContext,
):
    if audit_context.actor_type != "worker":
        raise Conflict("schedule publication requires worker audit provenance")
    _require_worker_event(
        audit_context,
        topic="publication.scheduled_run_requested",
        aggregate_id=run_id,
        payload_identity={"run_id": str(run_id)},
    )
    run = CollectionRun.objects.select_for_update().get(id=run_id)
    if run.approval_mode != ApprovalMode.VALIDATED_AUTO:
        return []
    article = run.articles.select_for_update().select_related("current_revision").get()
    run_before_material = {
        "runId": str(run.id),
        "state": run.state,
        "topicCode": run.topic_code,
    }
    article_before_material = {
        "articleId": str(article.id),
        "state": article.state,
        "currentRevisionId": str(article.current_revision_id),
    }
    schedule_dispatch = (
        ScheduleDispatch.objects.select_for_update()
        .select_related("schedule")
        .get(collection_run=run)
    )
    request_key = f"schedule:{schedule_dispatch.id}:intent"
    replay_intent = (
        PublicationIntent.objects.select_related(
            "article_revision__generation_attempt"
        )
        .filter(article_id=article.id, request_key=request_key)
        .first()
    )
    if (
        replay_intent is not None
        and replay_intent.state == PublicationIntent.State.DISPATCHED
    ):
        if (
            str(replay_intent.origin_collection_run_id) != str(run.id)
            or replay_intent.approval_mode != ApprovalMode.VALIDATED_AUTO
        ):
            raise Conflict("stored schedule publication replay identity is invalid")
        replay_attempts = list(replay_intent.attempts.all())
        if len(replay_attempts) != len(replay_intent.target_commands):
            raise Conflict("stored schedule publication replay is incomplete")
        require_audit_replay(
            context=audit_context,
            action="collection_run.publication_started",
            entity=run,
            identity_key=(
                f"{audit_context.event_key}:collection-run-publication"
            ),
        )
        return replay_attempts
    schedule = _require_frozen_schedule(schedule_dispatch)
    revision = article.current_revision
    if revision is None or revision.quality_state != "passed":
        raise Conflict("자동발행할 current revision이 품질 gate를 통과하지 못했습니다.")
    intent = (
        PublicationIntent.objects.select_related(
            "article_revision__generation_attempt"
        )
        .filter(article_revision=revision, request_key=request_key)
        .first()
    )
    if intent is None:
        _require_revision_for_commands(
            revision,
            [{"resolvedAction": "create"}],
        )
    else:
        intent = _require_intent_revision_publishable(intent)
    requested_ids = [str(value) for value in run.requested_target_ids]
    targets = _lock_automation_targets(requested_ids)
    if not requested_ids or set(targets) != set(requested_ids):
        raise InvalidInput("일정에 존재하지 않는 발행 target이 포함되어 있습니다.")
    _validate_automation_material(schedule, targets)
    wordpress_ids = [key for key, row in targets.items() if row.channel == ChannelCode.WORDPRESS]
    if any(row.channel == ChannelCode.BLOGGER for row in targets.values()) and not wordpress_ids:
        raise InvalidInput("Blogger 자동발행 일정에는 대표 WordPress target이 필요합니다.")

    target_refs = []
    commands = []
    for target_id in sorted(targets):
        target = targets[target_id]
        if not target.current_snapshot_id or not target.current_config_hash:
            raise Conflict("발행 target snapshot이 준비되지 않았습니다.")
        existing_publication = Publication.objects.filter(article_id=article.id, target=target).first()
        action = "update" if existing_publication and existing_publication.remote_post_id else "create"
        target_refs.append(
            {
                "targetId": target_id,
                "targetSnapshotId": str(target.current_snapshot_id),
                "targetConfigHash": target.current_config_hash,
            }
        )
        commands.append(
            {
                "targetId": target_id,
                "targetSnapshotId": str(target.current_snapshot_id),
                "targetConfigHash": target.current_config_hash,
                "resolvedAction": action,
                "canonicalDependencyTargetId": (
                    wordpress_ids[0] if target.channel == ChannelCode.BLOGGER else None
                ),
            }
        )

    if intent is None:
        latest = PublicationIntent.objects.filter(article_id=article.id).order_by("-created_at").first()
        intent = create_publication_intent(
            str(article.id),
            {
                "revisionNo": revision.revision_no,
                "expectedRevisionContentHash": revision.content_hash,
                "correctionCaseId": None,
                "targetSnapshots": target_refs,
                "targetCommands": commands,
                "approvalMode": ApprovalMode.VALIDATED_AUTO,
                "autoPublishValidationRefs": schedule.auto_publish_validation_refs,
                "autoPublishActivationRefs": schedule.auto_publish_activation_refs,
                "expectedLatestIntentId": str(latest.id) if latest else None,
                "requestKey": request_key,
            },
            user=run.requested_by,
            audit_context=audit_context,
        )

    for command in intent.target_commands:
        target_id = str(command["targetId"])
        latest_approval = _latest_approval_locked(
            intent=intent,
            target_id=target_id,
        )
        if latest_approval and latest_approval.decision == Approval.Decision.APPROVED:
            continue
        render = intent.renders.get(target_id=target_id, render_stage="preview")
        decide_approval(
            str(article.id),
            target_id,
            {
                "revisionNo": intent.revision_no,
                "publicationIntentId": str(intent.id),
                "expectedLatestApprovalId": str(latest_approval.id) if latest_approval else None,
                "expectedHeadVersion": latest_approval.head_version if latest_approval else 0,
                "requestKey": f"schedule:{schedule_dispatch.id}:approve:{target_id}",
                "reauthProofId": None,
                "actionSubject": {
                    "kind": "content_preview",
                    "action": command["resolvedAction"],
                    "renderId": str(render.id),
                    "targetId": target_id,
                    "targetSnapshotId": str(command["targetSnapshotId"]),
                    "targetConfigHash": command["targetConfigHash"],
                    "templateHash": render.template_hash,
                    "sourceManifestHash": render.source_manifest_hash,
                },
                "decision": Approval.Decision.APPROVED,
                "decisionReason": "검증 완료 자동발행 일정",
            },
            user=run.requested_by,
            audit_context=audit_context,
        )
    intent.refresh_from_db()
    if intent.state == PublicationIntent.State.DISPATCHED:
        require_audit_replay(
            context=audit_context,
            action="collection_run.publication_started",
            entity=run,
            identity_key=(
                f"{audit_context.event_key}:collection-run-publication"
            ),
        )
        return list(intent.attempts.all())
    attempts = dispatch_publication(
        str(article.id),
        {
            "revisionNo": intent.revision_no,
            "publicationIntentId": str(intent.id),
            "targetIds": [row["targetId"] for row in intent.target_snapshot_refs],
            "expectedTargetSnapshots": intent.target_snapshot_refs,
            "publishAt": None,
            "requestKey": f"schedule:{schedule_dispatch.id}:publish",
        },
        audit_context=audit_context,
    )
    run.state = RunState.PUBLISHING
    run.save(update_fields=["state"])
    record_audit_event(
        context=audit_context,
        action="collection_run.publication_started",
        entity=run,
        identity_key=(
            f"{audit_context.event_key}:collection-run-publication"
        ),
        material_schema_version="collection-run-publication-audit-v1",
        before_material={
            "run": run_before_material,
            "article": article_before_material,
        },
        after_material={
            "run": {
                "runId": str(run.id),
                "state": run.state,
                "topicCode": run.topic_code,
            },
            "article": {
                "articleId": str(article.id),
                "state": article.state,
                "currentRevisionId": str(article.current_revision_id),
            },
            "publicationIntentId": str(intent.id),
            "attemptCount": len(attempts),
        },
        metadata={
            "article_id": str(article.id),
            "collection_run_id": str(run.id),
            "intent_id": str(intent.id),
            "count": len(attempts),
            "result": "publication_started",
            "state": run.state,
        },
    )
    return attempts
