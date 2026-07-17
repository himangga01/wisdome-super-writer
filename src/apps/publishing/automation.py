from __future__ import annotations

from django.db import transaction

from apps.collection.models import CollectionRun, RunState
from apps.scheduling.models import ScheduleDispatch
from wisdome_writer.domain.errors import Conflict, InvalidInput
from wisdome_writer.domain.hashing import sha256_hex

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
from .services import create_publication_intent, decide_approval, dispatch_publication


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


@transaction.atomic
def dispatch_validated_schedule_run(run_id: str):
    run = CollectionRun.objects.select_for_update().get(id=run_id)
    if run.approval_mode != ApprovalMode.VALIDATED_AUTO:
        return []
    article = run.articles.select_for_update().select_related("current_revision").get()
    revision = article.current_revision
    if revision is None or revision.quality_state != "passed":
        raise Conflict("자동발행할 current revision이 품질 gate를 통과하지 못했습니다.")
    schedule_dispatch = ScheduleDispatch.objects.select_related("schedule").get(collection_run=run)
    schedule = schedule_dispatch.schedule
    requested_ids = [str(value) for value in run.requested_target_ids]
    targets = {
        str(row.id): row
        for row in PublicationTarget.objects.select_for_update().filter(id__in=requested_ids)
    }
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

    request_key = f"schedule:{schedule_dispatch.id}:intent"
    intent = PublicationIntent.objects.filter(
        article_revision=revision,
        request_key=request_key,
    ).first()
    if intent is None:
        latest = PublicationIntent.objects.filter(article_id=article.id).order_by("-created_at").first()
        intent = create_publication_intent(
            str(article.id),
            {
                "revisionNo": revision.revision_no,
                "expectedRevisionContentHash": sha256_hex(
                    {
                        "title": revision.title,
                        "summary": revision.summary,
                        "bodyMarkdown": revision.body_markdown,
                    }
                ),
                "correctionCaseId": None,
                "targetSnapshots": target_refs,
                "targetCommands": commands,
                "approvalMode": ApprovalMode.VALIDATED_AUTO,
                "autoPublishValidationRefs": schedule.auto_publish_validation_refs,
                "autoPublishActivationRefs": schedule.auto_publish_activation_refs,
                "expectedLatestIntentId": str(latest.id) if latest else None,
                "requestKey": request_key,
            },
            user=schedule.updated_by,
        )

    for command in intent.target_commands:
        target_id = str(command["targetId"])
        latest_approval = (
            Approval.objects.filter(publication_intent=intent, target_id=target_id)
            .order_by("-decided_at")
            .first()
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
                "reason": "검증 완료 자동발행 일정",
            },
            user=schedule.updated_by,
        )
    intent.refresh_from_db()
    if intent.state == PublicationIntent.State.DISPATCHED:
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
    )
    article.state = "publishing"
    article.save(update_fields=["state", "updated_at"])
    run.state = RunState.PUBLISHING
    run.save(update_fields=["state"])
    return attempts
