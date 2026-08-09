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
    _require_intent_revision_publishable,
    _require_revision_for_commands,
    create_publication_intent,
)


class CorrectionWorkflowError(ValueError):
    """Raised when a verified correction cannot be safely mapped to channels."""


@dataclass(frozen=True)
class CorrectionPlan:
    target_snapshots: list[dict[str, Any]]
    target_commands: list[dict[str, Any]]


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

    wordpress_target_id = next(
        (
            str(row.target_id)
            for row in publications
            if row.target.channel == ChannelCode.WORDPRESS
        ),
        None,
    )
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
        if target.channel == ChannelCode.BLOGGER and action != PublicationAction.UNPUBLISH:
            if not wordpress_target_id:
                raise CorrectionWorkflowError(
                    "Blogger corrections require the canonical WordPress target."
                )
            dependency = wordpress_target_id
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
    existing_intent = (
        PublicationIntent.objects.select_related(
            "article_revision__generation_attempt"
        )
        .filter(article_revision=revision, request_key=request_key)
        .first()
    )
    if case.kind != "retraction":
        if existing_intent is None:
            _require_revision_for_commands(
                revision,
                [{"resolvedAction": PublicationAction.UPDATE}],
            )
        else:
            _require_intent_revision_publishable(existing_intent)
    article = (
        DraftArticle.objects.select_for_update()
        .select_related("current_revision__generation_attempt")
        .get(id=case.article_id)
    )
    revision = article.current_revision

    plan = build_correction_plan(case)
    latest = PublicationIntent.objects.filter(article_id=case.article_id).order_by("-created_at").first()
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
    }
    intent = create_publication_intent(
        str(case.article_id),
        payload,
        user=user,
        audit_context=audit_context,
    )
    if case.state != CorrectionCase.State.APPLYING:
        case.state = CorrectionCase.State.APPLYING
        case.save(update_fields=["state"])
    return intent


@transaction.atomic
def complete_correction_if_terminal(case_id: str) -> bool:
    """Close a case after every frozen target command reaches its terminal state."""
    case = CorrectionCase.objects.select_for_update().get(id=case_id)
    intent = (
        PublicationIntent.objects.filter(correction_case_id=case.id)
        .order_by("-created_at")
        .first()
    )
    if not intent:
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
        for row in Publication.objects.filter(
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
