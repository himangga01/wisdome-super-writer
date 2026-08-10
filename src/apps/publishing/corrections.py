from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.audit.services import AuditContext
from apps.editorial.models import CorrectionCase, DraftArticle

from .models import (
    ApprovalMode,
    ChannelCode,
    Publication,
    PublicationAction,
    PublicationIntent,
)
from .services import (
    _StaleIntentConflict,
    _raise_persisted_stale_intent,
    _require_revision_for_commands,
    create_publication_intent,
    resolve_current_publication_intent,
)


class CorrectionWorkflowError(ValueError):
    """Raised when a verified correction cannot be safely mapped to channels."""


@dataclass(frozen=True)
class CorrectionPlan:
    target_snapshots: list[dict[str, Any]]
    target_commands: list[dict[str, Any]]


def _canonical_wordpress_target_id(
    blogger_publication: Publication,
    publications: list[Publication],
) -> str:
    matches = [
        str(row.target.id)
        for row in publications
        if row.target.channel == ChannelCode.WORDPRESS
        and row.target.role == "primary_canonical"
        and row.target.environment == blogger_publication.target.environment
    ]
    if len(matches) != 1:
        raise CorrectionWorkflowError(
            "Blogger corrections require one same-environment primary WordPress target."
        )
    return matches[0]


def _resolved_action(case: CorrectionCase, publication: Publication) -> str:
    capabilities = publication.target.capabilities or {}
    if case.kind == "retraction":
        if capabilities.get(PublicationAction.UNPUBLISH):
            return PublicationAction.UNPUBLISH
        if capabilities.get(PublicationAction.MARK_WITHDRAWN):
            return PublicationAction.MARK_WITHDRAWN
        raise CorrectionWorkflowError(
            f"{publication.target.display_name} target cannot unpublish or mark a withdrawal."
        )
    if not capabilities.get(PublicationAction.UPDATE):
        raise CorrectionWorkflowError(
            f"{publication.target.display_name} target does not support post updates."
        )
    return PublicationAction.UPDATE


def build_correction_plan(case: CorrectionCase) -> CorrectionPlan:
    """Freeze channel-specific update/withdrawal actions for an existing article."""
    publications = list(
        Publication.objects.filter(
            article_id=case.article_id,
            remote_post_id__isnull=False,
        )
        .select_related("target")
        .order_by("target__channel", "target_id")
    )
    if not publications:
        raise CorrectionWorkflowError("No existing remote publication is available to correct.")

    refs: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    for publication in publications:
        target = publication.target
        if not target.current_snapshot_id or not target.current_config_hash:
            raise CorrectionWorkflowError(
                f"{target.display_name} target has no active immutable snapshot."
            )
        action = _resolved_action(case, publication)
        dependency = None
        if target.channel == ChannelCode.BLOGGER:
            dependency = _canonical_wordpress_target_id(
                publication,
                publications,
            )
        refs.append(
            {
                "targetId": str(target.id),
                "targetSnapshotId": str(target.current_snapshot_id),
                "targetConfigHash": target.current_config_hash,
            }
        )
        commands.append(
            {
                "targetId": str(target.id),
                "targetSnapshotId": str(target.current_snapshot_id),
                "targetConfigHash": target.current_config_hash,
                "resolvedAction": action,
                "canonicalDependencyTargetId": dependency,
            }
        )
    return CorrectionPlan(target_snapshots=refs, target_commands=commands)


def prepare_verified_correction(
    case_id: str,
    *,
    user,
    request_key: str,
    audit_context: AuditContext,
) -> PublicationIntent:
    try:
        return _prepare_verified_correction_atomic(
            case_id,
            user=user,
            request_key=request_key,
            audit_context=audit_context,
        )
    except _StaleIntentConflict as exc:
        _raise_persisted_stale_intent(exc)


def _frozen_correction_intent_payload(
    intent: PublicationIntent,
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        "revisionNo": intent.revision_no,
        "expectedRevisionContentHash": intent.revision_content_hash,
        "correctionCaseId": str(intent.correction_case_id),
        "targetSnapshots": intent.target_snapshot_refs,
        "targetCommands": [
            {
                "targetId": str(row["targetId"]),
                "targetSnapshotId": str(row["targetSnapshotId"]),
                "targetConfigHash": row["targetConfigHash"],
                "resolvedAction": row["resolvedAction"],
                "canonicalDependencyTargetId": (
                    str(row["canonicalDependencyTargetId"])
                    if row.get("canonicalDependencyTargetId")
                    else None
                ),
            }
            for row in intent.target_commands
        ],
        "approvalMode": intent.approval_mode,
        "autoPublishValidationRefs": intent.auto_publish_validation_refs,
        "autoPublishActivationRefs": intent.auto_publish_activation_refs,
        "expectedLatestIntentId": (
            str(intent.supersedes_intent_id) if intent.supersedes_intent_id else None
        ),
        "requestKey": intent.request_key,
        "reason": reason,
    }


@transaction.atomic
def _prepare_verified_correction_atomic(
    case_id: str,
    *,
    user,
    request_key: str,
    audit_context: AuditContext,
) -> PublicationIntent:
    """Create an approval-ready, superseding intent for a verified correction case."""
    case = CorrectionCase.objects.select_for_update().get(id=case_id)
    existing_intent = (
        PublicationIntent.objects.select_related(
            "article_revision__generation_attempt"
        )
        .filter(
            article_id=case.article_id,
            request_key=request_key,
        )
        .first()
    )
    if existing_intent is not None:
        if str(existing_intent.correction_case_id) != str(case.id):
            raise CorrectionWorkflowError(
                "Correction request key is bound to different immutable material."
            )
        replay, created = create_publication_intent(
            str(case.article_id),
            _frozen_correction_intent_payload(
                existing_intent,
                reason=audit_context.reason_code,
            ),
            user=user,
            audit_context=audit_context,
        )
        if created or replay.id != existing_intent.id:
            raise CorrectionWorkflowError("Correction intent replay is not exact.")
        replay._correction_intent_created = False
        return replay
    if case.state not in {
        CorrectionCase.State.VERIFIED,
        CorrectionCase.State.APPLYING,
    }:
        raise CorrectionWorkflowError("Only a verified correction can be prepared for publishing.")
    article_candidate = DraftArticle.objects.select_related(
        "current_revision__generation_attempt"
    ).get(id=case.article_id)
    revision = article_candidate.current_revision
    if revision is None:
        raise CorrectionWorkflowError("The corrected article has no current revision.")
    if case.kind != "retraction":
        _require_revision_for_commands(
            revision,
            [{"resolvedAction": PublicationAction.UPDATE}],
        )
    article = (
        DraftArticle.objects.select_for_update()
        .select_related("current_revision__generation_attempt")
        .get(id=case.article_id)
    )
    revision = article.current_revision

    plan = build_correction_plan(case)
    latest = resolve_current_publication_intent(
        case.article_id,
        for_update=True,
    )
    payload = {
        "revisionNo": revision.revision_no,
        "expectedRevisionContentHash": revision.content_hash,
        "correctionCaseId": str(case.id),
        "targetSnapshots": plan.target_snapshots,
        "targetCommands": plan.target_commands,
        "approvalMode": ApprovalMode.MANUAL,
        "autoPublishValidationRefs": [],
        "autoPublishActivationRefs": [],
        "expectedLatestIntentId": str(latest.id) if latest else None,
        "requestKey": request_key,
        "reason": audit_context.reason_code,
    }
    intent, _intent_created = create_publication_intent(
        str(case.article_id),
        payload,
        user=user,
        audit_context=audit_context,
    )
    if case.state != CorrectionCase.State.APPLYING:
        case.state = CorrectionCase.State.APPLYING
        case.save(update_fields=["state"])
    intent._correction_intent_created = _intent_created
    return intent


def _locked_correction_intent_leaf(case: CorrectionCase) -> PublicationIntent | None:
    intents = list(
        PublicationIntent.objects.select_for_update()
        .filter(
            article_id=case.article_id,
            correction_case_id=case.id,
        )
        .order_by("id")
    )
    if not intents:
        return None
    intent_ids = {str(row.id) for row in intents}
    superseded_ids = {
        str(row.supersedes_intent_id)
        for row in intents
        if row.supersedes_intent_id is not None
        and str(row.supersedes_intent_id) in intent_ids
    }
    leaves = [row for row in intents if str(row.id) not in superseded_ids]
    if len(leaves) != 1:
        raise CorrectionWorkflowError(
            "Correction intent lineage is ambiguous and requires manual recovery."
        )
    return leaves[0]


@transaction.atomic
def complete_correction_if_terminal(case_id: str) -> bool:
    """Close a case after every frozen target command reaches its terminal state."""
    case = CorrectionCase.objects.select_for_update().get(id=case_id)
    intent = _locked_correction_intent_leaf(case)
    if not intent or intent.state != PublicationIntent.State.DISPATCHED:
        return False
    terminal_by_action = {
        PublicationAction.UPDATE: {
            Publication.State.PUBLISHED,
            Publication.State.MARKED_WITHDRAWN,
        },
        PublicationAction.MARK_WITHDRAWN: {Publication.State.MARKED_WITHDRAWN},
        PublicationAction.UNPUBLISH: {Publication.State.WITHDRAWN},
    }
    publications = {
        str(row.target_id): row
        for row in Publication.objects.select_for_update().filter(
            article_id=case.article_id,
            target_id__in=[row["targetId"] for row in intent.target_commands],
        )
    }
    complete = all(
        str(command["targetId"]) in publications
        and publications[str(command["targetId"])].state
        in terminal_by_action.get(command["resolvedAction"], set())
        for command in intent.target_commands
    )
    if complete:
        case.state = CorrectionCase.State.COMPLETED
        case.completed_at = timezone.now()
        case.save(update_fields=["state", "completed_at"])
    return complete
