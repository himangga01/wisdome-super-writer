from __future__ import annotations

import html
import inspect
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone as dt_timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection, transaction
from django.db.models import Max
from django.utils.module_loading import import_string
from django.utils import timezone

from adapters.publishers.blogger import BloggerOAuthClient, BloggerPublisher
from adapters.publishers.wordpress import WordPressPublisher
from apps.accounts.services import consume_reauthentication_proof
from apps.audit.models import AuditEvent
from apps.audit.redaction import validate_stored_metadata
from apps.audit.services import (
    AuditContext,
    audit_event_id,
    record_audit_event,
    require_audit_replay,
    require_worker_event,
)
from apps.editorial.services import require_revision_publishable
from wisdome_writer.domain.concurrency import (
    canonical_request_hash,
    require_idempotent_match,
)
from wisdome_writer.domain.errors import (
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    RequestKeyConflict,
)
from wisdome_writer.domain.hashing import sha256_hex

from .contracts import PublishCommand, PublisherError, RenderedArticle, RenderedMedia
from .models import (
    Approval,
    ApprovalMode,
    ArticleChannelRender,
    AutoPublishActivation,
    AutoPublishValidation,
    AutoPublishValidationDecision,
    ChannelCode,
    ChannelRole,
    Publication,
    PublicationAction,
    PublicationApprovalHead,
    PublicationAttempt,
    PublicationExecutionObservation,
    PublicationReconcileGeneration,
    PublicationRecoveryState,
    PublicationIntent,
    PublicationMedia,
    PublicationTarget,
    PublicationTargetSnapshot,
    PublicDeliveryAsset,
    RemoteMedia,
    TargetCanaryRun,
    TargetDisconnectDecision,
    TargetEnvironment,
    ValidationState,
)


PUBLISHER_CONTRACT_VERSION = "publisher-v1"
ADAPTER_MANIFESTS = {
    ChannelCode.WORDPRESS: sha256_hex(
        {
            "channel": "wordpress",
            "implementation": "adapters.publishers.wordpress.client.WordPressPublisher",
            "contract": PUBLISHER_CONTRACT_VERSION,
            "api": "wp-json/wp/v2",
        }
    ),
    ChannelCode.BLOGGER: sha256_hex(
        {
            "channel": "blogger",
            "implementation": "adapters.publishers.blogger.client.BloggerPublisher",
            "contract": PUBLISHER_CONTRACT_VERSION,
            "api": "blogger-v3",
        }
    ),
}
PUBLISHING_AUDIT_MATERIAL_VERSION = "publishing-state-v1"
_TARGET_CREATE_NAMESPACE = uuid.UUID("e28b5933-26ae-4e6d-83b5-1cbe64f26ba0")


def _require_audit_actor(audit_context: AuditContext, expected: str) -> None:
    if audit_context.actor_type != expected:
        raise Forbidden(f"{expected} audit provenance is required")


def _worker_audit_replay(
    audit_context: AuditContext,
    *,
    entity,
    candidates: Iterable[
        tuple[str, str, dict[str, Any] | None]
    ],
) -> AuditEvent | None:
    if audit_context.actor_type != "worker" or audit_context.event_key is None:
        return None
    for action, identity_key, metadata_expected in candidates:
        expected_id = audit_event_id(
            action=action,
            entity=entity,
            identity_key=identity_key,
        )
        if not AuditEvent.objects.using(
            audit_context.database_alias
        ).filter(id=expected_id).exists():
            continue
        return require_audit_replay(
            context=audit_context,
            action=action,
            entity=entity,
            identity_key=identity_key,
            metadata_expected=metadata_expected,
        )
    return None


def _require_worker_event(
    audit_context: AuditContext,
    *,
    topic: str,
    aggregate_id,
    payload_identity: dict[str, str],
):
    try:
        return require_worker_event(
            context=audit_context,
            topic=topic,
            aggregate_id=aggregate_id,
            payload_identity=payload_identity,
        )
    except ValueError as exc:
        raise Conflict(str(exc)) from exc


def _audit_state(entity, **extra: Any) -> dict[str, Any]:
    material: dict[str, Any] = {
        "entityType": entity._meta.label_lower,
        "entityId": str(entity.pk),
    }
    for field_name in (
        "state",
        "status",
        "decision",
        "version",
        "attempt_no",
        "reconcile_attempt_no",
        "current_snapshot_version",
        "current_config_hash",
        "connection_state",
        "preflight_state",
        "canary_state",
        "pilot_state",
        "auto_publish_enabled",
        "decision_version",
        "intent_hash",
        "approval_subject_hash",
        "activation_hash",
        "material_hash",
        "result_identity",
        "result_state",
        "remote_state",
        "last_error_code",
        "error_code",
    ):
        if hasattr(entity, field_name):
            value = getattr(entity, field_name)
            if isinstance(value, uuid.UUID):
                value = str(value)
            material[field_name] = value
    if isinstance(entity, PublicationTarget):
        material.update(
            {
                "display_name_hash": sha256_hex(entity.display_name),
                "username_ref_identity_hash": (
                    sha256_hex(entity.username_ref)
                    if entity.username_ref
                    else None
                ),
                "credential_ref_identity_hash": (
                    sha256_hex(entity.credential_ref)
                    if entity.credential_ref
                    else None
                ),
            }
        )
    material.update(extra)
    return material


def _state_transition_manifests(
    rows: Iterable[dict[str, Any]],
    *,
    state_field: str,
    next_state: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    before_rows = sorted(
        (
            {
                "id": str(row["id"]),
                "state": row[state_field],
            }
            for row in rows
        ),
        key=lambda row: row["id"],
    )
    after_rows = [
        {"id": row["id"], "state": next_state}
        for row in before_rows
    ]
    return (
        {
            "count": len(before_rows),
            "manifestHash": sha256_hex(before_rows),
        },
        {
            "count": len(after_rows),
            "manifestHash": sha256_hex(after_rows),
        },
    )


def _publication_media_state_manifest(publication_id) -> dict[str, Any]:
    rows = [
        {
            "id": str(row["id"]),
            "bindingState": row["binding_state"],
            "remoteMediaId": _id(row["remote_media_id"]),
            "publicDeliveryAssetId": _id(
                row["public_delivery_asset_id"]
            ),
        }
        for row in PublicationMedia.objects.filter(
            publication_id=publication_id
        )
        .order_by("id")
        .values(
            "id",
            "binding_state",
            "remote_media_id",
            "public_delivery_asset_id",
        )
    ]
    return {
        "count": len(rows),
        "manifestHash": sha256_hex(rows),
    }


def _record_publishing_audit(
    *,
    audit_context: AuditContext,
    action: str,
    entity,
    identity_key: str,
    before_material: dict[str, Any] | None,
    after_material: dict[str, Any] | None,
    metadata: dict[str, Any] | None = None,
):
    return record_audit_event(
        context=audit_context,
        action=action,
        entity=entity,
        identity_key=identity_key,
        material_schema_version=PUBLISHING_AUDIT_MATERIAL_VERSION,
        before_material=before_material,
        after_material=after_material,
        metadata=metadata,
    )


def _request_hash(payload: dict[str, Any]) -> str:
    return sha256_hex(payload)


def _approval_request_hash(
    *,
    article_id,
    target_id,
    payload: dict[str, Any],
) -> str:
    """Bind approval idempotency to both route identity and canonical body."""

    return sha256_hex(
        {
            "schemaVersion": "approval-request-v2",
            "path": {
                "articleId": str(article_id),
                "targetId": str(target_id),
            },
            "body": payload,
        }
    )


def _approval_replay_request_hash(
    *,
    stored_hash: str,
    current_hash: str,
    payload: dict[str, Any],
) -> str:
    """Admit exact v2 replay first, then the historical body-only identity."""

    if stored_hash == current_hash:
        return current_hash
    legacy_hash = _request_hash(payload)
    if stored_hash == legacy_hash:
        return legacy_hash
    raise Conflict("request key was reused for a different approval decision")


def _legacy_admin_request_hash(
    *,
    actor_id: Any,
    request_key: str,
    reason: str,
    action: str,
    payload: dict[str, Any],
) -> str:
    return _request_hash(
        {
            "schemaVersion": "publishing-admin-request-v1",
            "action": action,
            "actorId": str(actor_id),
            "requestKey": request_key,
            "reason": reason,
            "payload": payload,
        }
    )


def _publication_dispatch_audit_identity(
    *,
    intent_id,
    request_key: str,
) -> str:
    return sha256_hex(
        {
            "schemaVersion": "publication-dispatch-audit-identity-v1",
            "intentId": str(intent_id),
            "requestKey": request_key,
        }
    )


def _admin_request_hash(
    *,
    audit_context: AuditContext,
    action: str,
    payload: dict[str, Any],
) -> str:
    if audit_context.actor_type != AuditEvent.ActorType.ADMIN:
        raise Forbidden("admin audit provenance is required")
    if (
        "requestKey" in payload
        and payload["requestKey"] != audit_context.request_key
    ):
        raise Forbidden("requestKey differs from audit provenance")
    if (
        "reason" in payload
        and payload["reason"] != audit_context.reason_code
    ):
        raise Forbidden("reason differs from audit provenance")
    return _legacy_admin_request_hash(
        actor_id=audit_context.actor_id,
        request_key=audit_context.request_key,
        reason=audit_context.reason_code,
        action=action,
        payload=payload,
    )


def _canary_request_hash(
    *,
    target_id: Any,
    policy_version: str,
    reason: str,
    request_key: str,
) -> str:
    return canonical_request_hash(
        operation_id="canaryPublicationTarget",
        path="/targets/{targetId}/canary",
        payload={
            "path": {"targetId": str(target_id)},
            "query": {},
            "body": {
                "confirmIsolatedTestTarget": True,
                "policyVersion": policy_version,
                "reason": reason,
                "requestKey": request_key,
            },
        },
    )


def _has_request_audit(
    *,
    audit_context: AuditContext,
    action: str,
    entity,
) -> bool:
    return any(
        event.metadata_redacted.get("request_key")
        == audit_context.request_key
        for event in AuditEvent.objects.using(
            audit_context.database_alias
        ).filter(
            action=action,
            entity_type=entity._meta.label_lower,
            entity_id=entity.pk,
        )
    )


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


def _validated_remote_url(value: str | None) -> str | None:
    if value in {None, ""}:
        return value
    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise Conflict("publisher remote URL is invalid") from exc
    del port
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(character) < 32 for character in str(value))
    ):
        raise Conflict("publisher remote URL must be an HTTP(S) URL")
    return str(value)


_CONTENT_PUBLICATION_ACTIONS = {
    PublicationAction.CREATE,
    PublicationAction.UPDATE,
}
_APPROVAL_MATERIAL_VERSION = "approval-subject-v3"
_APPROVAL_DECISION_MATERIAL_VERSION = "approval-decision-v1"

_STALEABLE_INTENT_STATES = {
    PublicationIntent.State.DRAFT,
    PublicationIntent.State.AWAITING_APPROVAL,
    PublicationIntent.State.APPROVED,
}
_REVOKE_BLOCKING_ATTEMPT_STATES = frozenset(
    {
        PublicationAttempt.State.RUNNING,
        PublicationAttempt.State.UNKNOWN_OUTCOME,
        PublicationAttempt.State.RECONCILING,
    }
)
_REVOKE_STALEABLE_ATTEMPT_STATES = frozenset(
    {
        PublicationAttempt.State.QUEUED,
        PublicationAttempt.State.RETRYABLE_FAILED,
    }
)


def _intent_state_can_be_marked_stale(state: str) -> bool:
    return state in _STALEABLE_INTENT_STATES


def _lock_revoke_attempts_for_article_target(
    *,
    article_id,
    target_id,
) -> tuple[list[PublicationAttempt], list[PublicationAttempt]]:
    scope = {
        "publication_intent__article_id": article_id,
        "publication__target_id": target_id,
        "state__in": (
            _REVOKE_BLOCKING_ATTEMPT_STATES
            | _REVOKE_STALEABLE_ATTEMPT_STATES
        ),
    }
    publication_ids = list(
        PublicationAttempt.objects.filter(**scope)
        .order_by("publication_id")
        .values_list("publication_id", flat=True)
        .distinct()
    )
    list(
        Publication.objects.select_for_update()
        .filter(id__in=publication_ids)
        .order_by("id")
    )
    rows = list(
        PublicationAttempt.objects.select_for_update()
        .select_related("publication")
        .filter(**scope)
        .order_by("id")
    )
    return (
        [row for row in rows if row.state in _REVOKE_BLOCKING_ATTEMPT_STATES],
        [row for row in rows if row.state in _REVOKE_STALEABLE_ATTEMPT_STATES],
    )


def _latest_approvals_allow_dispatch(
    target_ids: Iterable[str],
    decisions_by_target: dict[str, str],
) -> bool:
    expected = {str(target_id) for target_id in target_ids}
    observed = {str(target_id) for target_id in decisions_by_target}
    return expected == observed and all(
        decisions_by_target[target_id] == Approval.Decision.APPROVED
        for target_id in expected
    )


def _render_approval_material(
    render: ArticleChannelRender,
    *,
    intent: PublicationIntent,
    target: PublicationTarget,
) -> dict[str, Any]:
    expected_content_hash = sha256_hex(
        {"title": render.title, "body": render.body_html}
    )
    expected_template_hash = sha256_hex(
        {
            "channel": target.channel,
            "title": render.title,
            "body": render.body_html,
            "revision": str(intent.article_revision_id),
        }
    )
    expected_source_manifest_hash = sha256_hex(
        {
            "inputEvidenceManifestHash": intent.input_evidence_manifest_hash,
            "sourceLinks": render.source_links,
        }
    )
    if (
        render.content_hash != expected_content_hash
        or render.template_hash != expected_template_hash
        or render.source_manifest_hash != expected_source_manifest_hash
    ):
        raise Conflict("approval render material is stale")
    return {
        "renderId": str(render.id),
        "contentHash": render.content_hash,
        "templateHash": render.template_hash,
        "sourceManifestHash": render.source_manifest_hash,
        "mediaManifestHash": sha256_hex(render.media_manifest),
    }


def _validate_approval_action_subject(
    *,
    intent: PublicationIntent,
    target: PublicationTarget,
    command: dict[str, Any],
    subject: dict[str, Any],
    render: ArticleChannelRender | None,
    publication: Publication | None,
    decision_reason: str,
    using: str = "default",
) -> None:
    action = command["resolvedAction"]
    common = {
        "targetId": str(target.id),
        "targetSnapshotId": str(command["targetSnapshotId"]),
        "targetConfigHash": command["targetConfigHash"],
        "action": action,
    }
    if action == PublicationAction.UNPUBLISH:
        allowed = {
            "kind",
            "action",
            "targetId",
            "targetSnapshotId",
            "targetConfigHash",
            "remotePostId",
            "observedRemoteState",
            "reason",
            "affectedTargetIds",
            "correctionEvidenceManifestHash",
        }
        if set(subject) != allowed or subject.get("kind") != "unpublish_command":
            raise InvalidInput("unpublish approval subject has an invalid shape")
        if any(subject.get(key) != value for key, value in common.items()):
            raise Conflict("unpublish approval subject differs from the frozen command")
        if render is not None:
            raise Conflict("unpublish approval cannot reference a content render")
        if publication is None or publication.remote_post_id != subject.get("remotePostId"):
            raise Conflict("unpublish approval differs from the current remote post")
        observed_remote_state = (
            Publication.State.MARKED_WITHDRAWN
            if publication.state == Publication.State.MARKED_WITHDRAWN
            else publication.remote_state
        )
        if subject.get("observedRemoteState") != observed_remote_state:
            raise Conflict("unpublish approval remote state is stale")
        expected_targets = sorted(
            str(row["targetId"])
            for row in intent.target_commands
            if isinstance(row, dict) and row.get("targetId")
        )
        affected_targets = subject.get("affectedTargetIds")
        if (
            not isinstance(affected_targets, list)
            or len(affected_targets) != len(set(affected_targets))
            or sorted(str(value) for value in affected_targets) != expected_targets
        ):
            raise Conflict("unpublish approval affected target set is stale")
        unpublish_reason = str(subject.get("reason", ""))
        if (
            unpublish_reason != unpublish_reason.strip()
            or len(unpublish_reason) < 3
            or len(unpublish_reason) > 500
        ):
            raise InvalidInput("unpublish reason must contain 3 to 500 characters")
        if not intent.correction_case_id:
            raise Conflict("unpublish approval requires a frozen correction case")
        CorrectionCase = apps.get_model("editorial", "CorrectionCase")
        case = (
            CorrectionCase.objects.using(using)
            .filter(id=intent.correction_case_id)
            .first()
        )
        if (
            case is None
            or case.article_id != intent.article_id
            or case.state not in {"verified", "applying"}
            or case.subject_hash != subject.get("correctionEvidenceManifestHash")
        ):
            raise Conflict("unpublish correction evidence is stale")
        return

    allowed = {
        "kind",
        "action",
        "renderId",
        "targetId",
        "targetSnapshotId",
        "targetConfigHash",
        "templateHash",
        "sourceManifestHash",
    }
    if set(subject) != allowed or subject.get("kind") != "content_preview":
        raise InvalidInput("content approval subject has an invalid shape")
    if any(subject.get(key) != value for key, value in common.items()):
        raise Conflict("content approval subject differs from the frozen command")
    if render is None:
        raise Conflict("content approval requires an exact preview render")
    if (
        str(subject.get("renderId")) != str(render.id)
        or render.publication_intent_id != intent.id
        or render.article_revision_id != intent.article_revision_id
        or str(render.target_id) != str(target.id)
        or str(render.target_snapshot_id) != str(command["targetSnapshotId"])
        or render.target_config_hash != command["targetConfigHash"]
        or render.render_stage != ArticleChannelRender.Stage.PREVIEW
        or subject.get("templateHash") != render.template_hash
        or subject.get("sourceManifestHash") != render.source_manifest_hash
    ):
        raise Conflict("content approval render differs from the frozen subject")
    _render_approval_material(render, intent=intent, target=target)


def _approval_subject_hash(
    *,
    intent: PublicationIntent,
    target: PublicationTarget,
    command: dict[str, Any],
    subject: dict[str, Any],
    render: ArticleChannelRender | None,
    publication: Publication | None,
    using: str = "default",
) -> str:
    _validate_approval_action_subject(
        intent=intent,
        target=target,
        command=command,
        subject=subject,
        render=render,
        publication=publication,
        decision_reason=(
            str(subject.get("reason", ""))
            if command["resolvedAction"] == PublicationAction.UNPUBLISH
            else ""
        ),
        using=using,
    )
    revision = intent.article_revision
    if (
        revision.id != intent.article_revision_id
        or revision.revision_no != intent.revision_no
        or revision.content_hash != intent.revision_content_hash
    ):
        raise Conflict("approval revision material differs from the frozen intent")
    return sha256_hex(
        {
            "schemaVersion": _APPROVAL_MATERIAL_VERSION,
            "intentId": str(intent.id),
            "intentHash": intent.intent_hash,
            "articleRevisionId": str(intent.article_revision_id),
            "revisionNo": intent.revision_no,
            "revisionContentHash": intent.revision_content_hash,
            "editorialPolicyHash": revision.editorial_policy_hash,
            "qualityGateManifestHash": intent.quality_gate_manifest_hash,
            "qualityReportHash": intent.quality_report_hash,
            "inputEvidenceManifestHash": intent.input_evidence_manifest_hash,
            "targetId": str(target.id),
            "targetAction": command["resolvedAction"],
            "targetSnapshotId": str(command["targetSnapshotId"]),
            "targetConfigHash": command["targetConfigHash"],
            "targetCommandHash": command["targetCommandHash"],
            "actionSubject": subject,
            "renderMaterial": (
                _render_approval_material(
                    render,
                    intent=intent,
                    target=target,
                )
                if render is not None
                else None
            ),
        }
    )


def _approval_decision_hash(
    *,
    subject_hash: str,
    decision: str,
    head_version: int,
    supersedes_approval_id,
    request_hash: str,
    actor_type: str,
    actor_id,
    event_key,
    decision_reason: str,
) -> str:
    return sha256_hex(
        {
            "schemaVersion": _APPROVAL_DECISION_MATERIAL_VERSION,
            "subjectHash": subject_hash,
            "decision": decision,
            "headVersion": head_version,
            "supersedesApprovalId": _id(supersedes_approval_id),
            "requestHash": request_hash,
            "actorType": actor_type,
            "actorId": _id(actor_id),
            "eventKey": _id(event_key),
            "reason": decision_reason,
        }
    )


def _validate_approval_transition(
    previous_decision: str | None,
    requested_decision: str,
) -> None:
    allowed = {
        None: {Approval.Decision.APPROVED, Approval.Decision.REJECTED},
        Approval.Decision.REJECTED: {Approval.Decision.APPROVED},
        Approval.Decision.APPROVED: {Approval.Decision.REVOKED},
        Approval.Decision.REVOKED: set(),
    }
    if requested_decision not in allowed.get(previous_decision, set()):
        raise Conflict("approval decision transition is not allowed")


def _validate_approval_mode_actor(
    *,
    mode: str,
    decision: str,
    audit_context: AuditContext,
) -> None:
    actor_type = audit_context.actor_type
    if mode == ApprovalMode.MANUAL:
        if actor_type != "admin":
            raise Conflict("manual approval decisions require an administrator")
        return
    if mode != ApprovalMode.VALIDATED_AUTO:
        raise Conflict("approval mode is unsupported")
    if decision == Approval.Decision.APPROVED:
        if actor_type != "worker":
            raise Conflict("validated-auto approval requires a worker decision")
        return
    if decision in {Approval.Decision.REJECTED, Approval.Decision.REVOKED}:
        if actor_type != "admin":
            raise Conflict("validated-auto safety decisions require an administrator")
        return
    raise Conflict("approval decision is unsupported")


def _validate_approval_cas(
    latest: Approval | None,
    *,
    expected_latest_approval_id,
    expected_head_version: int,
) -> None:
    observed_id = _id(latest.id if latest else None)
    observed_version = latest.head_version if latest else 0
    if (
        observed_id != _id(expected_latest_approval_id)
        or observed_version != expected_head_version
    ):
        raise Conflict("approval head changed; reload the current decision")


def _approval_matches_frozen_subject(
    approval: Approval,
    *,
    intent: PublicationIntent,
    target_id,
    command: dict[str, Any],
    using: str = "default",
) -> bool:
    if (
        getattr(approval, "publication_intent_id", None) != intent.id
        or str(getattr(approval, "article_revision_id", ""))
        != str(intent.article_revision_id)
        or str(getattr(approval, "target_id", "")) != str(target_id)
        or getattr(approval, "target_action", None) != command["resolvedAction"]
        or str(getattr(approval, "target_snapshot_id", ""))
        != str(command["targetSnapshotId"])
        or getattr(approval, "target_config_hash", None)
        != command["targetConfigHash"]
        or getattr(approval, "quality_report_hash", None)
        != intent.quality_report_hash
    ):
        return False
    try:
        if approval.approval_material_version == "approval-subject-v1":
            expected = sha256_hex(
                {
                    "intentId": str(intent.id),
                    "articleRevisionId": str(intent.article_revision_id),
                    "targetId": str(target_id),
                    "targetAction": command["resolvedAction"],
                    "targetSnapshotId": str(command["targetSnapshotId"]),
                    "targetConfigHash": command["targetConfigHash"],
                    "subject": approval.action_subject,
                    "qualityReportHash": intent.quality_report_hash,
                }
            )
            return approval.approval_subject_hash == expected
        if approval.approval_material_version == "approval-subject-v2":
            expected = sha256_hex(
                {
                    "schemaVersion": "approval-subject-v2",
                    "intentId": str(intent.id),
                    "articleRevisionId": str(intent.article_revision_id),
                    "targetId": str(target_id),
                    "targetAction": command["resolvedAction"],
                    "targetSnapshotId": str(command["targetSnapshotId"]),
                    "targetConfigHash": command["targetConfigHash"],
                    "subject": approval.action_subject,
                    "qualityReportHash": intent.quality_report_hash,
                    "decision": approval.decision,
                    "headVersion": approval.head_version,
                    "supersedesApprovalId": _id(approval.supersedes_approval_id),
                }
            )
            return approval.approval_subject_hash == expected
        if approval.approval_material_version != _APPROVAL_MATERIAL_VERSION:
            return False
        target = approval.target
        render = approval.article_channel_render
        publication = None
        if command["resolvedAction"] == PublicationAction.UNPUBLISH:
            publication = Publication.objects.using(using).filter(
                article_id=intent.article_id,
                target_id=target_id,
            ).first()
        expected = _approval_subject_hash(
            intent=intent,
            target=target,
            command=command,
            subject=approval.action_subject,
            render=render,
            publication=publication,
            using=using,
        )
        return approval.approval_subject_hash == expected
    except (AttributeError, Conflict, InvalidInput, TypeError, ValueError):
        return False


def _latest_approval_locked(
    *,
    intent: PublicationIntent,
    target_id,
) -> Approval | None:
    head = (
        PublicationApprovalHead.objects.select_for_update()
        .filter(publication_intent=intent, target_id=target_id)
        .first()
    )
    if head is not None:
        latest = Approval.objects.select_for_update().get(
            id=head.latest_approval_id
        )
        if (
            latest.publication_intent_id != intent.id
            or str(latest.target_id) != str(target_id)
            or latest.admin_id is None
            or latest.head_version != head.version
            or latest.approval_subject_hash != head.subject_hash
            or latest.decision_hash
            != _approval_decision_hash(
                subject_hash=latest.approval_subject_hash,
                decision=latest.decision,
                head_version=latest.head_version,
                supersedes_approval_id=latest.supersedes_approval_id,
                request_hash=latest.request_hash,
                actor_type=latest.decision_actor_type,
                actor_id=latest.decision_actor_id,
                event_key=latest.decision_event_key,
                decision_reason=latest.decision_reason,
            )
        ):
            raise Conflict("approval head does not match its immutable decision")
        return latest
    if not Approval.objects.filter(
        publication_intent=intent,
        target_id=target_id,
    ).exists():
        return None
    raise Conflict("approval head is missing; migration or manual repair is required")


@dataclass(frozen=True)
class ApprovalDecisionProjection:
    current_head_approval_id: uuid.UUID
    current_head_version: int
    current_head_decision: str
    current_head_subject_hash: str
    current_head_decision_hash: str
    current_head_updated_at: Any
    is_current: bool
    dispatch_eligible: bool


def evaluate_approval_decision_readonly(
    *,
    approval_id,
    using: str = "default",
) -> ApprovalDecisionProjection:
    """Recompute the current-head and dispatch projection without taking locks."""

    approval = (
        Approval.objects.using(using)
        .select_related(
            "article_revision__generation_attempt",
            "publication_intent__article_revision__generation_attempt",
            "target",
            "article_channel_render",
        )
        .get(id=approval_id)
    )
    head = (
        PublicationApprovalHead.objects.using(using)
        .select_related("latest_approval")
        .filter(
            publication_intent_id=approval.publication_intent_id,
            target_id=approval.target_id,
        )
        .first()
    )
    if head is None:
        raise Conflict("approval head is missing; migration or manual repair is required")
    current = head.latest_approval
    head_integrity = (
        current.publication_intent_id == approval.publication_intent_id
        and current.target_id == approval.target_id
        and current.admin_id is not None
        and current.head_version == head.version
        and current.approval_subject_hash == head.subject_hash
        and current.decision_hash
        == _approval_decision_hash(
            subject_hash=current.approval_subject_hash,
            decision=current.decision,
            head_version=current.head_version,
            supersedes_approval_id=current.supersedes_approval_id,
            request_hash=current.request_hash,
            actor_type=current.decision_actor_type,
            actor_id=current.decision_actor_id,
            event_key=current.decision_event_key,
            decision_reason=current.decision_reason,
        )
    )
    if not head_integrity:
        raise Conflict("approval head does not match its immutable decision")
    is_current = current.id == approval.id
    dispatch_eligible = False
    if is_current and current.decision == Approval.Decision.APPROVED:
        intent = approval.publication_intent
        revision = intent.article_revision
        command = next(
            (
                row
                for row in intent.target_commands
                if isinstance(row, dict)
                and str(row.get("targetId")) == str(approval.target_id)
            ),
            None,
        )
        try:
            if command is None:
                raise Conflict("publication intent has no command for this target")
            material = _intent_material_data(intent, revision)
            refs = _target_ref_map(intent.target_snapshot_refs)
            commands = _command_map(intent.target_commands)
            latest_intent_id = (
                PublicationIntent.objects.using(using)
                .filter(article_id=intent.article_id)
                .order_by("-created_at")
                .values_list("id", flat=True)
                .first()
            )
            content_action = command["resolvedAction"] in _CONTENT_PUBLICATION_ACTIONS
            intent_integrity = (
                intent.revision_no == revision.revision_no
                and intent.revision_content_hash == material["revisionContentHash"]
                and _id(intent.generation_attempt_id)
                == material["generationAttemptId"]
                and intent.input_evidence_manifest_hash
                == material["inputEvidenceManifestHash"]
                and intent.generation_pipeline_manifest_hash
                == material["generationPipelineManifestHash"]
                and intent.quality_gate_manifest_hash
                == material["qualityGateManifestHash"]
                and intent.quality_report_hash == material["qualityReportHash"]
                and intent.intent_hash
                == _intent_hash(material, revision, refs, commands)
            )
            target = approval.target
            target_integrity = (
                str(target.current_snapshot_id)
                == str(command["targetSnapshotId"])
                and target.current_config_hash == command["targetConfigHash"]
                and str(approval.target_snapshot_id)
                == str(command["targetSnapshotId"])
                and approval.target_config_hash == command["targetConfigHash"]
                and approval.target_action == command["resolvedAction"]
            )
            live_target_eligible = _validated_auto_live_eligible(
                intent=intent,
                target=target,
                using=using,
            )
            approval_integrity = (
                approval.policy_snapshot_hash == revision.editorial_policy_hash
                and approval.quality_report_hash == intent.quality_report_hash
                and _approval_matches_frozen_subject(
                    approval,
                    intent=intent,
                    target_id=target.id,
                    command=command,
                    using=using,
                )
            )
            _require_revision_for_commands(revision, intent.target_commands)
            dispatch_eligible = bool(
                intent_integrity
                and target_integrity
                and live_target_eligible
                and approval_integrity
                and intent.state
                in {
                    PublicationIntent.State.APPROVED,
                    PublicationIntent.State.DISPATCHED,
                }
                and (not content_action or latest_intent_id == intent.id)
                and not _kill_switch_enabled()
            )
        except (AttributeError, Conflict, InvalidInput, TypeError, ValueError):
            dispatch_eligible = False
    return ApprovalDecisionProjection(
        current_head_approval_id=current.id,
        current_head_version=head.version,
        current_head_decision=current.decision,
        current_head_subject_hash=current.approval_subject_hash,
        current_head_decision_hash=current.decision_hash,
        current_head_updated_at=head.updated_at,
        is_current=is_current,
        dispatch_eligible=dispatch_eligible,
    )


def _require_exact_dispatch_targets(
    *,
    requested_ids: Iterable[str],
    expected_refs: dict[str, dict[str, Any]],
    intent_commands: dict[str, dict[str, Any]],
    intent_refs: dict[str, dict[str, Any]],
) -> None:
    requested = {str(target_id) for target_id in requested_ids}
    if not (
        requested
        == set(expected_refs)
        == set(intent_commands)
        == set(intent_refs)
    ):
        raise InvalidInput(
            "dispatch targets must exactly match the frozen publication intent"
        )
    for target_id in requested:
        expected = expected_refs[target_id]
        frozen = intent_refs[target_id]
        if (
            str(expected.get("targetSnapshotId"))
            != str(frozen.get("targetSnapshotId"))
            or expected.get("targetConfigHash")
            != frozen.get("targetConfigHash")
        ):
            raise Conflict(
                "dispatch target snapshot differs from the frozen publication intent"
            )


def _require_current_dispatch_approval_locked(
    *,
    intent: PublicationIntent,
    target: PublicationTarget,
    command: dict[str, Any],
    frozen_ref: dict[str, Any],
) -> Approval:
    if (
        str(target.current_snapshot_id)
        != str(frozen_ref["targetSnapshotId"])
        or target.current_config_hash != frozen_ref["targetConfigHash"]
        or str(command["targetSnapshotId"])
        != str(frozen_ref["targetSnapshotId"])
        or command["targetConfigHash"] != frozen_ref["targetConfigHash"]
    ):
        raise Conflict("target snapshot changed after publication approval")
    approval = _latest_approval_locked(
        intent=intent,
        target_id=target.id,
    )
    if (
        approval is None
        or approval.decision != Approval.Decision.APPROVED
        or approval.target_action != command["resolvedAction"]
        or not _approval_matches_frozen_subject(
            approval,
            intent=intent,
            target_id=target.id,
            command=command,
        )
    ):
        raise Conflict("target requires a current immutable approval")
    return approval


def _approval_requires_current_target_snapshot(
    decision: str,
    action: str,
) -> bool:
    del action
    return decision not in {
        Approval.Decision.REJECTED,
        Approval.Decision.REVOKED,
    }


def _requires_article_external_write_fence(action: str) -> bool:
    return action in {
        PublicationAction.CREATE,
        PublicationAction.UPDATE,
        PublicationAction.MARK_WITHDRAWN,
    }


def _lock_open_intents_referencing(
    *,
    field_name: str,
    reference: dict[str, str],
) -> list[PublicationIntent]:
    rows = list(
        PublicationIntent.objects.select_for_update()
        .filter(
            state__in=_STALEABLE_INTENT_STATES,
        )
        .order_by("id")
    )
    matched = []
    for intent in rows:
        refs = getattr(intent, field_name, None)
        if not isinstance(refs, list):
            continue
        if any(
            isinstance(item, dict)
            and all(item.get(key) == value for key, value in reference.items())
            for item in refs
        ):
            matched.append(intent)
    return matched


def _lock_target_intent_fences(
    target_ids: Iterable[str],
) -> list[PublicationTargetIntentFence]:
    normalized = sorted({str(target_id) for target_id in target_ids})
    if not normalized:
        return []
    existing_ids = set(
        PublicationTargetIntentFence.objects.filter(
            target_id__in=normalized
        ).values_list("target_id", flat=True)
    )
    missing = [
        PublicationTargetIntentFence(target_id=target_id)
        for target_id in normalized
        if target_id not in {str(value) for value in existing_ids}
    ]
    if missing:
        PublicationTargetIntentFence.objects.bulk_create(
            missing,
            ignore_conflicts=True,
        )
    rows = list(
        PublicationTargetIntentFence.objects.select_for_update()
        .filter(target_id__in=normalized)
        .order_by("target_id")
    )
    if len(rows) != len(normalized):
        raise Conflict("publication target intent fence is incomplete")
    return rows


def _lock_open_target_intents(target_id) -> list[PublicationIntent]:
    return _lock_open_intents_referencing(
        field_name="target_snapshot_refs",
        reference={"targetId": str(target_id)},
    )


def _intent_target_ids(intent: PublicationIntent) -> list[str]:
    return sorted(
        {
            str(row["targetId"])
            for row in intent.target_commands
            if isinstance(row, dict) and row.get("targetId")
        }
    )


def _stale_locked_intents(
    intents: Iterable[PublicationIntent],
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = [
        {"id": intent.id, "state": intent.state}
        for intent in intents
        if _intent_state_can_be_marked_stale(intent.state)
    ]
    before, after = _state_transition_manifests(
        rows,
        state_field="state",
        next_state=PublicationIntent.State.STALE,
    )
    ids = [row["id"] for row in rows]
    if ids:
        PublicationIntent.objects.filter(
            id__in=ids,
            state__in=_STALEABLE_INTENT_STATES,
        ).update(state=PublicationIntent.State.STALE)
    return before, after


def _assert_target_has_no_active_external_write(target_id) -> None:
    active = (
        PublicationAttempt.objects.select_for_update()
        .filter(
            publication__target_id=target_id,
            resolved_action__in={
                PublicationAction.CREATE,
                PublicationAction.UPDATE,
                PublicationAction.MARK_WITHDRAWN,
            },
            state__in={
                PublicationAttempt.State.RUNNING,
                PublicationAttempt.State.UNKNOWN_OUTCOME,
                PublicationAttempt.State.RECONCILING,
            },
        )
        .order_by("id")
        .exists()
    )
    if active:
        raise Conflict(
            "publication target cannot change during an active external write"
        )


def _lock_article_external_write_fence(article_id):
    CollectionRun = apps.get_model("collection", "CollectionRun")
    DraftArticle = apps.get_model("editorial", "DraftArticle")
    try:
        run_id = DraftArticle.objects.values_list(
            "source_run_id", flat=True
        ).get(id=article_id)
    except DraftArticle.DoesNotExist as exc:
        raise NotFound("publication article does not exist") from exc
    CollectionRun.objects.select_for_update().get(id=run_id)
    return DraftArticle.objects.select_for_update().get(id=article_id)


def _set_article_external_write_fence_locked(
    attempt: PublicationAttempt,
) -> None:
    if not _requires_article_external_write_fence(attempt.resolved_action):
        return
    article = _lock_article_external_write_fence(
        attempt.publication_intent.article_id
    )
    if article.current_revision_id != attempt.article_revision_id:
        raise Conflict("publication attempt revision is no longer current")
    if article.state != "publishing":
        article.state = "publishing"
        article.save(update_fields=("state", "updated_at"))


def _lock_publication_attempt_domain(
    preliminary: PublicationAttempt,
) -> PublicationAttempt:
    PublicationIntent.objects.select_for_update().get(
        id=preliminary.publication_intent_id
    )
    PublicationTarget.objects.select_for_update().get(
        id=preliminary.publication.target_id
    )
    Approval.objects.select_for_update().get(id=preliminary.approval_id)
    Publication.objects.select_for_update().get(
        id=preliminary.publication_id
    )
    return (
        PublicationAttempt.objects.select_for_update()
        .select_related(
            "publication__target",
            "publication_intent",
            "approval__article_channel_render",
        )
        .get(id=preliminary.id)
    )


def _publication_run_terminal_state(
    attempt_states: Iterable[str],
) -> tuple[str, str, str | None] | None:
    states = list(attempt_states)
    terminal = {
        PublicationAttempt.State.SUCCEEDED,
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
        PublicationAttempt.State.STALE,
    }
    if not states or any(state not in terminal for state in states):
        return None
    if all(state == PublicationAttempt.State.SUCCEEDED for state in states):
        return "completed", "not_required", None
    return "failed", "manual_required", "publication_terminal_failure"


def _mark_origin_run_publishing_locked(intent: PublicationIntent) -> None:
    run_id = intent.origin_collection_run_id
    if run_id is None:
        return
    CollectionRun = apps.get_model("collection", "CollectionRun")
    run = CollectionRun.objects.select_for_update().get(id=run_id)
    if run.state == "awaiting_approval":
        run.state = "publishing"
        run.save(update_fields=("state",))


def _terminalize_unreachable_dependents_locked(
    attempt: PublicationAttempt,
) -> None:
    if (
        attempt.publication.target.channel != ChannelCode.WORDPRESS
        or attempt.state
        not in {
            PublicationAttempt.State.PERMANENT_FAILED,
            PublicationAttempt.State.MANUAL_REQUIRED,
            PublicationAttempt.State.STALE,
        }
    ):
        return
    dependent_target_ids = {
        str(command["targetId"])
        for command in attempt.publication_intent.target_commands
        if (
            isinstance(command, dict)
            and str(command.get("canonicalDependencyTargetId"))
            == str(attempt.publication.target_id)
        )
    }
    if not dependent_target_ids:
        return
    dependent_publications = list(
        Publication.objects.select_for_update()
        .filter(
            article_id=attempt.publication.article_id,
            target_id__in=dependent_target_ids,
        )
        .order_by("target_id", "id")
    )
    publications_by_id = {
        row.id: row for row in dependent_publications
    }
    dependents = list(
        PublicationAttempt.objects.select_for_update()
        .filter(
            publication_intent=attempt.publication_intent,
            publication_id__in=publications_by_id,
            state=PublicationAttempt.State.QUEUED,
        )
        .order_by("id")
    )
    now = timezone.now()
    for dependent in dependents:
        dependent.state = PublicationAttempt.State.MANUAL_REQUIRED
        dependent.finished_at = now
        dependent.error_code = "canonical_dependency_failed"
        dependent.recovery_state = PublicationRecoveryState.MANUAL_REQUIRED
        dependent.next_recovery_at = None
        dependent.terminal_impact = _publication_terminal_impact(
            stage="canonical_dependency",
            final_state=dependent.state,
            error_code=dependent.error_code,
        )
        dependent.save(
            update_fields=(
                "state",
                "finished_at",
                "error_code",
                "recovery_state",
                "next_recovery_at",
                "terminal_impact",
            )
        )
        publication = publications_by_id[dependent.publication_id]
        publication.state = Publication.State.MANUAL_REQUIRED
        publication.last_error_code = dependent.error_code
        publication.save(
            update_fields=("state", "last_error_code", "updated_at")
        )


def _project_origin_run_terminal_locked(
    attempt: PublicationAttempt,
) -> None:
    run_id = attempt.publication_intent.origin_collection_run_id
    if run_id is None:
        return
    CollectionRun = apps.get_model("collection", "CollectionRun")
    run = CollectionRun.objects.select_for_update().get(id=run_id)
    if run.state != "publishing":
        return
    _terminalize_unreachable_dependents_locked(attempt)
    dispatched_intents = list(
        PublicationIntent.objects.select_for_update()
        .filter(
            origin_collection_run_id=run_id,
            state=PublicationIntent.State.DISPATCHED,
        )
        .order_by("id")
    )
    attempts = list(
        PublicationAttempt.objects.select_for_update()
        .filter(
            publication_intent_id__in=[
                row.id for row in dispatched_intents
            ]
        )
        .order_by("id")
    )
    expected_count = sum(
        len(row.target_commands) for row in dispatched_intents
    )
    if len(attempts) != expected_count:
        return
    projection = _publication_run_terminal_state(
        row.state for row in attempts
    )
    if projection is None:
        return
    final_state, recovery_state, error_code = projection
    from apps.collection.services import project_run_terminal_observation

    now = timezone.now()
    run.state = final_state
    run.error_summary = (
        None
        if error_code is None
        else {
            "code": error_code,
            "failedAttemptIds": [
                str(row.id)
                for row in attempts
                if row.state != PublicationAttempt.State.SUCCEEDED
            ],
        }
    )
    project_run_terminal_observation(
        run,
        finished_at=now,
        stage="publishing",
        final_state=final_state,
        affected_count=len(attempts),
        error_code=error_code,
        recovery_state=recovery_state,
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
        )
    )


def _converge_revoked_attempt_redelivery_locked(
    attempt: PublicationAttempt,
    *,
    audit_context: AuditContext,
) -> None:
    if not (
        attempt.state == PublicationAttempt.State.STALE
        and attempt.error_code == "approval_revoked"
    ):
        raise Conflict("terminal publication attempt has no matching audit event")
    identity_key = (
        f"{audit_context.event_key}:attempt-skipped:{attempt.attempt_no}"
    )
    replay = _worker_audit_replay(
        audit_context,
        entity=attempt,
        candidates=(
            (
                "publication_attempt.skipped",
                identity_key,
                {
                    "attempt": attempt.attempt_no,
                    "error_code": "approval_revoked",
                },
            ),
        ),
    )
    if replay is None:
        state = _audit_state(attempt)
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_attempt.skipped",
            entity=attempt,
            identity_key=identity_key,
            before_material=state,
            after_material=state,
            metadata={
                "publication_attempt_id": str(attempt.id),
                "attempt": attempt.attempt_no,
                "result": "skipped",
                "error_code": attempt.error_code,
                "state": attempt.state,
            },
        )
    _release_article_external_write_fence_locked(attempt)


def _release_article_external_write_fence_locked(
    attempt: PublicationAttempt,
) -> None:
    if _requires_article_external_write_fence(attempt.resolved_action):
        article = _lock_article_external_write_fence(
            attempt.publication_intent.article_id
        )
        active_external_write = PublicationAttempt.objects.filter(
            publication_intent__article_id=article.id,
            resolved_action__in={
                PublicationAction.CREATE,
                PublicationAction.UPDATE,
                PublicationAction.MARK_WITHDRAWN,
            },
            state__in={
                PublicationAttempt.State.RUNNING,
                PublicationAttempt.State.UNKNOWN_OUTCOME,
                PublicationAttempt.State.RECONCILING,
            },
        ).exists()
        next_state = "publishing" if active_external_write else (
            "published"
            if Publication.objects.filter(
                article_id=article.id,
                remote_post_id__isnull=False,
            ).exists()
            else "review_ready"
        )
        if article.state != next_state:
            article.state = next_state
            article.save(update_fields=("state", "updated_at"))
    _project_origin_run_terminal_locked(attempt)


def _require_manual_revalidation_provenance(revision) -> None:
    if getattr(revision, "provenance_kind", "generated") != "admin_edit":
        return
    from wisdome_writer.infrastructure.models import (
        OutboxConsumerReceipt,
        OutboxMessage,
    )
    from wisdome_writer.infrastructure.outbox import compute_material_hash

    if (
        revision.revalidation_event_key is None
        or revision.revalidation_generation <= 0
        or revision.revalidation_lease_token is None
    ):
        raise Conflict("manual revision revalidation provenance is incomplete")
    expected_payload = {
        "article_id": str(revision.article_id),
        "article_revision_id": str(revision.id),
        "editorial_policy_snapshot_id": str(
            revision.editorial_policy_snapshot_id
        ),
        "editorial_policy_material_hash": revision.editorial_policy_hash,
        "verification_manifest_hash": revision.verification_manifest_hash,
        "input_evidence_manifest_hash": revision.evidence_manifest_hash,
        "excluded_material_manifest_hash": revision.exclusion_manifest_hash,
    }
    expected_policy_versions = {
        "editorialPolicyVersion": revision.editorial_policy_version,
        "editorialPolicyHash": revision.editorial_policy_hash,
    }
    alias = revision._state.db or "default"
    event = OutboxMessage.objects.using(alias).filter(
        id=revision.revalidation_event_key,
        topic="editorial.revalidate_requested",
        event_version=1,
        aggregate_type="article_revision",
        aggregate_id=revision.id,
        job_id=revision.article.source_run_id,
        message_key=f"editorial.revalidate_requested:{revision.id}",
        status=OutboxMessage.Status.PUBLISHED,
    ).first()
    if (
        event is None
        or event.payload != expected_payload
        or event.policy_versions != expected_policy_versions
        or event.immutable_material_hash != compute_material_hash(event)
    ):
        raise Conflict("manual revision revalidation event provenance is invalid")
    receipt = OutboxConsumerReceipt.objects.using(alias).filter(
        event=event,
        consumer_name="manual-article-revalidation",
    ).first()
    if (
        receipt is None
        or receipt.state != OutboxConsumerReceipt.State.SUCCEEDED
        or receipt.lease_generation < revision.revalidation_generation
    ):
        raise Conflict("manual revision revalidation receipt is not complete")


class _StaleIntentConflict(Conflict):
    def __init__(self, intent_id, detail: str):
        super().__init__(detail)
        self.intent_id = intent_id


def _revision_publication_material(revision) -> dict[str, Any]:
    generation_attempt = getattr(revision, "generation_attempt", None)
    return {
        "revisionContentHash": revision.content_hash,
        "generationAttemptId": _id(revision.generation_attempt_id),
        "inputEvidenceManifestHash": revision.evidence_manifest_hash,
        "generationPipelineManifestHash": (
            generation_attempt.generation_pipeline_manifest_hash
            if generation_attempt is not None
            else None
        ),
        "qualityGateManifestHash": revision.quality_gate_manifest_hash,
        "qualityReportHash": revision.quality_report_hash,
        "editorialPolicyHash": revision.editorial_policy_hash,
        "verificationManifestHash": revision.verification_manifest_hash,
        "exclusionManifestHash": revision.exclusion_manifest_hash,
        "claimManifestHash": revision.claim_manifest_hash,
        "revalidationGeneration": revision.revalidation_generation,
    }


def _commands_require_publishable_revision(commands: Iterable[dict[str, Any]]) -> bool:
    return any(
        command.get("resolvedAction") in _CONTENT_PUBLICATION_ACTIONS
        for command in commands
    )


def _require_revision_for_commands(
    revision,
    commands: Iterable[dict[str, Any]],
    *,
    approval_decision: str | None = None,
) -> None:
    if approval_decision in {
        Approval.Decision.REJECTED,
        Approval.Decision.REVOKED,
    }:
        return
    if not _commands_require_publishable_revision(commands):
        return
    _require_manual_revalidation_provenance(revision)
    try:
        require_revision_publishable(revision)
    except ValueError as exc:
        raise Conflict(str(exc)) from exc


def _intent_material_data(intent: PublicationIntent, revision) -> dict[str, Any]:
    return {
        "approvalMode": intent.approval_mode,
        "autoPublishValidationRefs": intent.auto_publish_validation_refs,
        "autoPublishActivationRefs": intent.auto_publish_activation_refs,
        "correctionCaseId": _id(intent.correction_case_id),
        **_revision_publication_material(revision),
    }


def _require_intent_revision_publishable(
    intent: PublicationIntent,
    *,
    approval_decision: str | None = None,
) -> PublicationIntent:
    if approval_decision in {
        Approval.Decision.REJECTED,
        Approval.Decision.REVOKED,
    } or not _commands_require_publishable_revision(intent.target_commands):
        return intent
    try:
        _require_revision_for_commands(
            intent.article_revision,
            intent.target_commands,
            approval_decision=approval_decision,
        )
        locked_intent = (
            PublicationIntent.objects.select_for_update()
            .select_related("article_revision__generation_attempt")
            .get(id=intent.id)
        )
        revision = locked_intent.article_revision
        material = _intent_material_data(locked_intent, revision)
        refs = _target_ref_map(locked_intent.target_snapshot_refs)
        commands = _command_map(locked_intent.target_commands)
        if (
            locked_intent.revision_no != revision.revision_no
            or locked_intent.revision_content_hash != material["revisionContentHash"]
            or _id(locked_intent.generation_attempt_id)
            != material["generationAttemptId"]
            or locked_intent.input_evidence_manifest_hash
            != material["inputEvidenceManifestHash"]
            or locked_intent.generation_pipeline_manifest_hash
            != material["generationPipelineManifestHash"]
            or locked_intent.quality_gate_manifest_hash
            != material["qualityGateManifestHash"]
            or locked_intent.quality_report_hash != material["qualityReportHash"]
            or locked_intent.intent_hash
            != _intent_hash(material, revision, refs, commands)
        ):
            raise Conflict("publication intent editorial material is stale")
        return locked_intent
    except Conflict as exc:
        raise _StaleIntentConflict(intent.id, str(exc)) from exc


def _persist_stale_intent(intent_id) -> None:
    with transaction.atomic():
        intent = PublicationIntent.objects.select_for_update().get(id=intent_id)
        if _intent_state_can_be_marked_stale(intent.state):
            intent.state = PublicationIntent.State.STALE
            intent.save(update_fields=["state"])


def _raise_persisted_stale_intent(exc: _StaleIntentConflict):
    if connection.in_atomic_block:
        raise exc
    _persist_stale_intent(exc.intent_id)
    raise Conflict(str(exc)) from exc


def _secret_resolver():
    from wisdome_writer.infrastructure.secrets import SecretResolver

    return SecretResolver()


def _resolve_secret(resolver: Any, reference: str | None) -> Any:
    if not reference:
        raise InvalidInput("발행 대상의 비밀 저장소 참조가 없습니다.")
    if hasattr(resolver, "resolve"):
        return resolver.resolve(reference)
    if hasattr(resolver, "get"):
        return resolver.get(reference)
    raise InvalidInput("구성된 비밀 저장소 resolver가 값을 읽을 수 없습니다.")


def publisher_for_target(target: PublicationTarget, *, resolver: Any | None = None):
    resolver = resolver or _secret_resolver()
    credential = _resolve_secret(resolver, target.credential_ref)
    if target.channel == ChannelCode.WORDPRESS:
        username = _resolve_secret(resolver, target.username_ref)
        return WordPressPublisher(
            base_url=target.base_url,
            username=str(username),
            application_password=str(credential),
            write_guard=_assert_external_writes_allowed,
        )
    if target.channel == ChannelCode.BLOGGER:
        access_token = credential.get("access_token") if isinstance(credential, dict) else credential
        if not access_token:
            raise InvalidInput("Blogger OAuth access token을 확인할 수 없습니다.")
        return BloggerPublisher(
            blog_id=str(target.remote_blog_id),
            access_token=str(access_token),
            write_guard=_assert_external_writes_allowed,
        )
    raise InvalidInput("지원하지 않는 발행 채널입니다.")


def _blogger_oauth_client(*, redirect_uri: str, resolver: Any | None = None) -> BloggerOAuthClient:
    resolver = resolver or _secret_resolver()
    client_id_ref = getattr(settings, "BLOGGER_OAUTH_CLIENT_ID_REF", None)
    client_secret_ref = getattr(settings, "BLOGGER_OAUTH_CLIENT_SECRET_REF", None)
    if not client_id_ref or not client_secret_ref:
        raise InvalidInput("Blogger OAuth client secret refs가 구성되지 않았습니다.")
    return BloggerOAuthClient(
        client_id=str(_resolve_secret(resolver, client_id_ref)),
        client_secret=str(_resolve_secret(resolver, client_secret_ref)),
        redirect_uri=redirect_uri,
    )


def start_blogger_oauth(
    target_id: str,
    *,
    user,
    redirect_uri: str,
    audit_context: AuditContext,
) -> dict[str, Any]:
    _require_audit_actor(audit_context, "admin")
    if audit_context.actor_id != user.pk:
        raise Forbidden("OAuth audit actor differs from the administrator")
    target = PublicationTarget.objects.using(
        audit_context.database_alias
    ).get(id=target_id, channel=ChannelCode.BLOGGER)
    nonce = sha256_hex(
        {
            "schemaVersion": "blogger-oauth-operation-v1",
            "targetId": str(target.id),
            "adminId": str(user.id),
            "requestKey": audit_context.request_key,
        }
    )[:32]
    request_hash = _request_hash(
        {
            "targetId": str(target.id),
            "targetSnapshotId": _id(target.current_snapshot_id),
            "targetSnapshotVersion": target.current_snapshot_version,
            "targetConfigHash": target.current_config_hash,
            "redirectUriHash": sha256_hex(redirect_uri),
            "adminId": str(user.id),
            "requestKey": audit_context.request_key,
            "reason": audit_context.reason_code,
            "nonce": nonce,
        }
    )
    state = signing.dumps(
        {
            "targetId": str(target.id),
            "targetSnapshotId": _id(target.current_snapshot_id),
            "targetSnapshotVersion": target.current_snapshot_version,
            "targetConfigHash": target.current_config_hash,
            "adminId": str(user.id),
            "nonce": nonce,
            "correlationId": str(audit_context.correlation_id),
            "requestKey": audit_context.request_key,
            "reason": audit_context.reason_code,
            "requestHash": request_hash,
            "redirectUriHash": sha256_hex(redirect_uri),
        },
        salt="publishing.blogger.oauth",
        compress=True,
    )
    client = _blogger_oauth_client(redirect_uri=redirect_uri)
    try:
        authorization_url = client.authorization_url(state=state)
    finally:
        client.close()
    return {
        "authorizationUrl": authorization_url,
        "expiresAt": (timezone.now() + timedelta(minutes=10)).isoformat(),
    }


def complete_blogger_oauth(
    *,
    code: str,
    state: str,
    request,
    redirect_uri: str,
) -> PublicationTarget:
    try:
        state_data = signing.loads(
            state,
            salt="publishing.blogger.oauth",
            max_age=600,
        )
    except signing.BadSignature as exc:
        raise Forbidden("Blogger OAuth state가 유효하지 않거나 만료되었습니다.") from exc
    user = request.user
    if str(state_data.get("adminId")) != str(user.id):
        raise Forbidden("OAuth를 시작한 관리자 session과 다릅니다.")
    audit_context = AuditContext.for_admin_continuation(
        request=request,
        correlation_id=state_data.get("correlationId"),
        reason_code=state_data.get("reason"),
        request_key=state_data.get("requestKey"),
    )
    expected_request_hash = _request_hash(
        {
            "targetId": str(state_data.get("targetId")),
            "targetSnapshotId": state_data.get("targetSnapshotId"),
            "targetSnapshotVersion": state_data.get("targetSnapshotVersion"),
            "targetConfigHash": state_data.get("targetConfigHash"),
            "redirectUriHash": sha256_hex(redirect_uri),
            "adminId": str(user.id),
            "requestKey": audit_context.request_key,
            "reason": audit_context.reason_code,
            "nonce": state_data.get("nonce"),
        }
    )
    if (
        state_data.get("requestHash") != expected_request_hash
        or state_data.get("redirectUriHash") != sha256_hex(redirect_uri)
    ):
        raise Forbidden("OAuth state provenance is invalid")
    with transaction.atomic(using=audit_context.database_alias):
        target = PublicationTarget.objects.using(
            audit_context.database_alias
        ).select_for_update().get(
            id=state_data["targetId"], channel=ChannelCode.BLOGGER
        )
        existing = next(
            (
                event
                for event in AuditEvent.objects.using(
                    audit_context.database_alias
                ).filter(
                    action="publication_target.oauth_connected",
                    entity_type=target._meta.label_lower,
                    entity_id=target.id,
                )
                if event.metadata_redacted.get("request_key")
                == audit_context.request_key
            ),
            None,
        )
        if existing is not None:
            require_audit_replay(
                context=audit_context,
                action="publication_target.oauth_connected",
                entity=target,
                identity_key=f"oauth:{state_data['nonce']}",
                request_hash=expected_request_hash,
            )
            return target
        if (
            _id(target.current_snapshot_id)
            != state_data.get("targetSnapshotId")
            or target.current_snapshot_version
            != state_data.get("targetSnapshotVersion")
            or target.current_config_hash
            != state_data.get("targetConfigHash")
        ):
            raise Conflict("OAuth target changed after authorization started")
        fence = (
            target.current_snapshot_id,
            target.current_snapshot_version,
            target.current_config_hash,
        )
    client = _blogger_oauth_client(redirect_uri=redirect_uri)
    try:
        token_payload = client.exchange_code(code)
    finally:
        client.close()
    store_path = getattr(settings, "BLOGGER_OAUTH_TOKEN_STORE", None)
    if not store_path:
        raise InvalidInput("Blogger OAuth token secret-store writer가 구성되지 않았습니다.")
    token_store = import_string(store_path)
    token_store_parameters = inspect.signature(token_store).parameters
    token_store_kwargs = {
        "target_id": str(target.id),
        "token_payload": token_payload,
    }
    if (
        "operation_key" in token_store_parameters
        or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in token_store_parameters.values()
        )
    ):
        token_store_kwargs["operation_key"] = state_data["nonce"]
    credential_ref = token_store(**token_store_kwargs)
    if not credential_ref:
        raise InvalidInput("OAuth token store가 credential reference를 반환하지 않았습니다.")
    with transaction.atomic(using=audit_context.database_alias):
        _lock_target_intent_fences((state_data["targetId"],))
        locked_intents = _lock_open_target_intents(
            state_data["targetId"]
        )
        target = PublicationTarget.objects.using(
            audit_context.database_alias
        ).select_for_update().get(
            id=state_data["targetId"], channel=ChannelCode.BLOGGER
        )
        if fence != (
            target.current_snapshot_id,
            target.current_snapshot_version,
            target.current_config_hash,
        ):
            raise Conflict("OAuth 교환 중 target snapshot이 변경되었습니다.")
        before_material = _audit_state(target)
        target.credential_ref = str(credential_ref)
        target.connection_state = PublicationTarget.ConnectionState.PENDING
        target.preflight_state = ValidationState.NOT_RUN
        target.auto_publish_enabled = False
        target.save()
        _snapshot_locked(target)
        _stale_locked_intents(locked_intents)
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.oauth_connected",
            entity=target,
            identity_key=f"oauth:{state_data['nonce']}",
            before_material=before_material,
            after_material=_audit_state(target),
            metadata={
                "request_hash": expected_request_hash,
                "target_id": str(target.id),
                "state": target.connection_state,
            },
        )
        return target


def _target_material(target: PublicationTarget) -> dict[str, Any]:
    return {
        "targetId": str(target.id),
        "channel": target.channel,
        "role": target.role,
        "environment": target.environment,
        "remoteBlogId": target.remote_blog_id,
        "baseUrl": target.base_url.rstrip("/"),
        "usernameRefIdentityHash": sha256_hex(target.username_ref or "") if target.username_ref else None,
        "credentialRefIdentityHash": sha256_hex(target.credential_ref or ""),
        "capabilities": target.capabilities,
        "connectionState": target.connection_state,
        "preflightState": target.preflight_state,
        "canaryState": target.canary_state,
        "pilotState": target.pilot_state,
        "canaryTargetId": _id(target.canary_target_id),
        "canaryPolicyVersion": target.canary_policy_version,
        "publisherContractVersion": PUBLISHER_CONTRACT_VERSION,
        "publisherAdapterManifestHash": ADAPTER_MANIFESTS[target.channel],
    }


def _snapshot_locked(target: PublicationTarget) -> PublicationTargetSnapshot:
    material = _target_material(target)
    config_hash = sha256_hex(material)
    existing = PublicationTargetSnapshot.objects.filter(target=target, config_hash=config_hash).first()
    if existing:
        target.current_snapshot_id = existing.id
        target.current_snapshot_version = existing.version
        target.current_config_hash = existing.config_hash
        target.publisher_contract_version = existing.publisher_contract_version
        target.publisher_adapter_manifest_hash = existing.publisher_adapter_manifest_hash
        target.save(
            update_fields=[
                "current_snapshot_id",
                "current_snapshot_version",
                "current_config_hash",
                "publisher_contract_version",
                "publisher_adapter_manifest_hash",
                "updated_at",
            ]
        )
        return existing
    version = (
        PublicationTargetSnapshot.objects.filter(target=target).aggregate(value=Max("version"))["value"] or 0
    ) + 1
    snapshot = PublicationTargetSnapshot.objects.create(
        target=target,
        version=version,
        channel=target.channel,
        role=target.role,
        environment=target.environment,
        remote_blog_id=target.remote_blog_id,
        base_url=target.base_url.rstrip("/"),
        username_ref_identity_hash=material["usernameRefIdentityHash"],
        credential_ref_identity_hash=material["credentialRefIdentityHash"],
        capabilities=target.capabilities,
        connection_state=target.connection_state,
        preflight_state=target.preflight_state,
        canary_state=target.canary_state,
        pilot_state=target.pilot_state,
        canary_target_id=target.canary_target_id,
        canary_policy_version=target.canary_policy_version,
        publisher_contract_version=PUBLISHER_CONTRACT_VERSION,
        publisher_adapter_manifest_hash=ADAPTER_MANIFESTS[target.channel],
        config_hash=config_hash,
    )
    target.current_snapshot_id = snapshot.id
    target.current_snapshot_version = snapshot.version
    target.current_config_hash = snapshot.config_hash
    target.publisher_contract_version = snapshot.publisher_contract_version
    target.publisher_adapter_manifest_hash = snapshot.publisher_adapter_manifest_hash
    target.save(
        update_fields=[
            "current_snapshot_id",
            "current_snapshot_version",
            "current_config_hash",
            "publisher_contract_version",
            "publisher_adapter_manifest_hash",
            "updated_at",
        ]
    )
    return snapshot


@transaction.atomic
def create_target(
    data: dict[str, Any], *, audit_context: AuditContext
) -> PublicationTarget:
    _require_audit_actor(audit_context, "admin")
    request_hash = _admin_request_hash(
        audit_context=audit_context,
        action="publication_target.created",
        payload=data,
    )
    target_id = uuid.uuid5(
        _TARGET_CREATE_NAMESPACE,
        str(audit_context.request_key),
    )
    get_user_model().objects.select_for_update().get(
        pk=audit_context.actor_id
    )
    existing = PublicationTarget.objects.select_for_update().filter(
        pk=target_id
    ).first()
    if existing is not None:
        require_audit_replay(
            context=audit_context,
            action="publication_target.created",
            entity=existing,
            identity_key=audit_context.request_key,
            request_hash=request_hash,
        )
        return existing
    channel = data.get("channel")
    role = data.get("channelRole") or data.get("role")
    if (channel, role) not in {
        (ChannelCode.WORDPRESS, ChannelRole.PRIMARY),
        (ChannelCode.BLOGGER, ChannelRole.SECONDARY),
    }:
        raise InvalidInput("WordPress는 대표 원문, Blogger는 보조 배포 역할이어야 합니다.")
    base_url = str(data.get("baseUrl", "")).rstrip("/")
    if not base_url.startswith("https://"):
        raise InvalidInput("발행 대상은 HTTPS URL이어야 합니다.")
    default_capabilities = (
        WordPressPublisher.capabilities.as_dict()
        if channel == ChannelCode.WORDPRESS
        else BloggerPublisher.capabilities.as_dict()
    )
    target = PublicationTarget(
        id=target_id,
        channel=channel,
        role=role,
        environment=data.get("environment"),
        display_name=data.get("displayName", "").strip(),
        base_url=base_url,
        remote_blog_id=data.get("remoteBlogId"),
        canary_target_id=data.get("canaryTargetId"),
        username_ref=data.get("usernameRef"),
        credential_ref=data.get("credentialRef"),
        capabilities=default_capabilities,
        publisher_contract_version=PUBLISHER_CONTRACT_VERSION,
        publisher_adapter_manifest_hash=ADAPTER_MANIFESTS[channel],
    )
    target.full_clean()
    target.save()
    PublicationTargetIntentFence.objects.create(target=target)
    _snapshot_locked(target)
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.created",
        entity=target,
        identity_key=audit_context.request_key or f"target-created:{target.id}",
        before_material=None,
        after_material=_audit_state(target),
        metadata={
            "request_hash": request_hash,
            "target_id": str(target.id),
            "target_type": target.channel,
            "state": target.connection_state,
        },
    )
    return target


@transaction.atomic
def update_target(
    target_id: str,
    data: dict[str, Any],
    *,
    audit_context: AuditContext,
) -> PublicationTarget:
    _require_audit_actor(audit_context, "admin")
    _lock_target_intent_fences((target_id,))
    changed_connection = bool(
        {"canaryTargetId", "usernameRef", "credentialRef"}.intersection(data)
    )
    locked_intents = (
        _lock_open_intents_referencing(
            field_name="target_snapshot_refs",
            reference={"targetId": str(target_id)},
        )
        if changed_connection
        else []
    )
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_hash = _admin_request_hash(
        audit_context=audit_context,
        action="publication_target.updated",
        payload={"targetId": str(target.id), "changes": data},
    )
    if _has_request_audit(
        audit_context=audit_context,
        action="publication_target.updated",
        entity=target,
    ):
        require_audit_replay(
            context=audit_context,
            action="publication_target.updated",
            entity=target,
            identity_key=audit_context.request_key,
            request_hash=request_hash,
        )
        return target
    if changed_connection:
        _assert_target_has_no_active_external_write(target.id)
    immutable = {"channel", "channelRole", "role", "environment", "baseUrl", "remoteBlogId"}
    if immutable.intersection(data):
        raise InvalidInput("채널, 역할, 환경, base URL과 remote blog ID는 변경할 수 없습니다.")
    before_material = _audit_state(target)
    field_map = {
        "displayName": "display_name",
        "canaryTargetId": "canary_target_id",
        "usernameRef": "username_ref",
        "credentialRef": "credential_ref",
    }
    for external_name, field_name in field_map.items():
        if external_name in data:
            setattr(target, field_name, data[external_name])
    empty_before, empty_after = _state_transition_manifests(
        [],
        state_field="state",
        next_state=PublicationIntent.State.STALE,
    )
    transition_before = {
        "validations": empty_before,
        "intents": empty_before,
    }
    transition_after = {
        "validations": empty_after,
        "intents": empty_after,
    }
    if changed_connection:
        target.connection_state = PublicationTarget.ConnectionState.PENDING
        target.preflight_state = ValidationState.STALE
        target.pilot_state = ValidationState.STALE
        target.auto_publish_enabled = False
        target.latest_auto_publish_activation_id = None
        validation_query = (
            AutoPublishValidation.objects.select_for_update()
            .filter(
                target=target,
                status=AutoPublishValidation.State.PASSED,
            )
            .order_by("id")
        )
        validation_rows = list(validation_query.values("id", "status"))
        validation_before, validation_after = _state_transition_manifests(
            validation_rows,
            state_field="status",
            next_state=AutoPublishValidation.State.STALE,
        )
        validation_query.update(
            status=AutoPublishValidation.State.STALE,
            invalidated_at=timezone.now(),
            invalidation_reason="target_configuration_changed",
        )
        intent_before, intent_after = _stale_locked_intents(locked_intents)
        transition_before = {
            "validations": validation_before,
            "intents": intent_before,
        }
        transition_after = {
            "validations": validation_after,
            "intents": intent_after,
        }
    target.full_clean()
    target.save()
    _snapshot_locked(target)
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.updated",
        entity=target,
        identity_key=(
            audit_context.request_key
            or f"target-updated:{audit_context.correlation_id}"
        ),
        before_material={
            **before_material,
            "staleTransitions": transition_before,
        },
        after_material=_audit_state(
            target,
            staleTransitions=transition_after,
        ),
        metadata={
            "request_hash": request_hash,
            "target_id": str(target.id),
            "target_type": target.channel,
            "state": target.connection_state,
        },
    )
    return target


@dataclass(frozen=True)
class TargetPreflightFence:
    target_id: uuid.UUID
    target_snapshot_id: uuid.UUID
    target_config_hash: str
    target_snapshot_version: int


@transaction.atomic
def request_target_preflight(
    target_id: str,
    *,
    audit_context: AuditContext,
):
    _require_audit_actor(audit_context, "admin")
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_hash = _admin_request_hash(
        audit_context=audit_context,
        action="publication_target.preflight_requested",
        payload={
            "targetId": str(target.id),
            "targetSnapshotId": _id(target.current_snapshot_id),
            "targetConfigHash": target.current_config_hash,
        },
    )
    dedupe_key = (
        f"publication.preflight_requested:{target.id}:"
        f"{target.current_snapshot_version}"
    )
    if _has_request_audit(
        audit_context=audit_context,
        action="publication_target.preflight_requested",
        entity=target,
    ):
        require_audit_replay(
            context=audit_context,
            action="publication_target.preflight_requested",
            entity=target,
            identity_key=audit_context.request_key,
            request_hash=request_hash,
        )
        from wisdome_writer.infrastructure.models import OutboxMessage

        event = OutboxMessage.objects.filter(message_key=dedupe_key).first()
        if event is None:
            raise Conflict(
                "preflight replay has an audit event but no matching outbox event"
            )
        return event
    event = _enqueue_event(
        "publication.preflight_requested",
        {
            "target_id": str(target.id),
            "target_snapshot_id": str(target.current_snapshot_id),
            "target_config_hash": target.current_config_hash,
        },
        dedupe_key=dedupe_key,
        aggregate_type="publication_target",
        aggregate_id=target.id,
        job_id=target.id,
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.preflight_requested",
        entity=target,
        identity_key=audit_context.request_key or f"preflight-request:{event.id}",
        before_material=_audit_state(target),
        after_material=_audit_state(target),
        metadata={
            "request_hash": request_hash,
            "target_id": str(target.id),
            "state": "queued",
        },
    )
    return event


@transaction.atomic
def begin_target_preflight(
    target_id: str,
    *,
    expected_snapshot_id: str,
    expected_config_hash: str,
    audit_context: AuditContext,
) -> tuple[PublicationTarget, TargetPreflightFence] | None:
    _require_audit_actor(audit_context, "worker")
    _require_worker_event(
        audit_context,
        topic="publication.preflight_requested",
        aggregate_id=target_id,
        payload_identity={
            "target_id": str(target_id),
            "target_snapshot_id": expected_snapshot_id,
            "target_config_hash": expected_config_hash,
        },
    )
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    if _worker_audit_replay(
        audit_context,
        entity=target,
        candidates=(
            (
                "publication_target.preflight",
                f"{audit_context.event_key}:preflight-result",
                None,
            ),
            (
                "publication_target.preflight_stale_before_call",
                f"{audit_context.event_key}:preflight-stale-before",
                None,
            ),
            (
                "publication_target.preflight_stale_after_call",
                f"{audit_context.event_key}:preflight-stale-after",
                None,
            ),
        ),
    ):
        return None
    if (
        not target.current_snapshot_id
        or str(target.current_snapshot_id) != str(expected_snapshot_id)
        or target.current_config_hash != expected_config_hash
    ):
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.preflight_stale_before_call",
            entity=target,
            identity_key=f"{audit_context.event_key}:preflight-stale-before",
            before_material={
                "targetId": str(target.id),
                "eventKey": audit_context.event_key,
                "expectedSnapshotId": expected_snapshot_id,
                "expectedConfigHash": expected_config_hash,
                "classification": "stale_before_call",
            },
            after_material={
                "targetId": str(target.id),
                "eventKey": audit_context.event_key,
                "expectedSnapshotId": expected_snapshot_id,
                "expectedConfigHash": expected_config_hash,
                "classification": "stale_before_call",
            },
            metadata={
                "target_id": str(target.id),
                "result": "stale",
            },
        )
        return None
    return target, TargetPreflightFence(
        target_id=target.id,
        target_snapshot_id=target.current_snapshot_id,
        target_config_hash=target.current_config_hash,
        target_snapshot_version=target.current_snapshot_version,
    )


@transaction.atomic
def persist_target_preflight_result(
    fence: TargetPreflightFence,
    result,
    *,
    audit_context: AuditContext,
) -> tuple[PublicationTarget, bool]:
    _require_audit_actor(audit_context, "worker")
    _require_worker_event(
        audit_context,
        topic="publication.preflight_requested",
        aggregate_id=fence.target_id,
        payload_identity={
            "target_id": str(fence.target_id),
            "target_snapshot_id": str(fence.target_snapshot_id),
            "target_config_hash": fence.target_config_hash,
        },
    )
    _lock_target_intent_fences((fence.target_id,))
    locked_intents = _lock_open_intents_referencing(
        field_name="target_snapshot_refs",
        reference={"targetId": str(fence.target_id)},
    )
    target = PublicationTarget.objects.select_for_update().get(id=fence.target_id)
    _assert_target_has_no_active_external_write(target.id)
    if (
        target.current_snapshot_id != fence.target_snapshot_id
        or target.current_config_hash != fence.target_config_hash
        or target.current_snapshot_version != fence.target_snapshot_version
    ):
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.preflight_stale_after_call",
            entity=target,
            identity_key=f"{audit_context.event_key}:preflight-stale-after",
            before_material={
                "targetId": str(target.id),
                "eventKey": audit_context.event_key,
                "expectedSnapshotId": str(fence.target_snapshot_id),
                "expectedConfigHash": fence.target_config_hash,
                "classification": "stale_after_call",
            },
            after_material={
                "targetId": str(target.id),
                "eventKey": audit_context.event_key,
                "expectedSnapshotId": str(fence.target_snapshot_id),
                "expectedConfigHash": fence.target_config_hash,
                "classification": "stale_after_call",
            },
            metadata={
                "target_id": str(target.id),
                "result": "stale",
            },
        )
        return target, False
    before_material = _audit_state(target)
    validation_before, validation_after = _state_transition_manifests(
        [],
        state_field="status",
        next_state=AutoPublishValidation.State.STALE,
    )
    target.capabilities = result.capabilities.as_dict()
    target.preflight_state = ValidationState.PASSED if result.passed else ValidationState.FAILED
    if result.passed:
        target.connection_state = PublicationTarget.ConnectionState.VERIFIED
    else:
        target.connection_state = PublicationTarget.ConnectionState.BLOCKED
        target.auto_publish_enabled = False
        validation_query = (
            AutoPublishValidation.objects.select_for_update()
            .filter(
                target=target,
                status=AutoPublishValidation.State.PASSED,
            )
            .order_by("id")
        )
        validation_rows = list(validation_query.values("id", "status"))
        validation_before, validation_after = _state_transition_manifests(
            validation_rows,
            state_field="status",
            next_state=AutoPublishValidation.State.STALE,
        )
        validation_query.update(
            status=AutoPublishValidation.State.STALE,
            invalidated_at=timezone.now(),
            invalidation_reason="target_preflight_failed",
        )
    if not result.passed and result.error_code in {
        "wordpress_auth_or_capability_denied",
        "blogger_token_expired",
        "blogger_scope_or_owner_denied",
    }:
        target.connection_state = PublicationTarget.ConnectionState.EXPIRED
        target.auto_publish_enabled = False
    target.last_preflight_at = timezone.now()
    target.save()
    _snapshot_locked(target)
    intent_before, intent_after = _stale_locked_intents(locked_intents)
    result_hash = sha256_hex(
        {
            "passed": result.passed,
            "remoteIdentity": result.remote_identity,
            "remoteUrl": result.remote_url,
            "capabilities": result.capabilities.as_dict(),
            "checks": result.checks,
            "errorCode": result.error_code,
        }
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.preflight",
        entity=target,
        identity_key=f"{audit_context.event_key}:preflight-result",
        before_material={
            **before_material,
            "staleValidations": validation_before,
            "staleIntents": intent_before,
        },
        after_material={
            **_audit_state(target),
            "resultHash": result_hash,
            "staleValidations": validation_after,
            "staleIntents": intent_after,
        },
        metadata={
            "target_id": str(target.id),
            "result": "passed" if result.passed else "failed",
            "result_hash": result_hash,
            "error_code": result.error_code,
        },
    )
    _enqueue_event(
        "publishing.target_preflight.completed",
        {
            "target_id": str(target.id),
            "target_snapshot_id": str(fence.target_snapshot_id),
            "target_config_hash": fence.target_config_hash,
            "result_hash": result_hash,
            "passed": result.passed,
        },
        dedupe_key=(
            f"publishing.target_preflight.completed:{target.id}:"
            f"{fence.target_snapshot_id}:{result_hash}"
        ),
        aggregate_type="publication_target",
        aggregate_id=target.id,
        job_id=target.id,
    )
    return target, True


def run_target_preflight(
    target_id: str,
    *,
    expected_snapshot_id: str,
    expected_config_hash: str,
    audit_context: AuditContext,
    resolver: Any | None = None,
) -> tuple[PublicationTarget, bool]:
    _require_audit_actor(audit_context, "worker")
    prepared = begin_target_preflight(
        target_id,
        expected_snapshot_id=expected_snapshot_id,
        expected_config_hash=expected_config_hash,
        audit_context=audit_context,
    )
    if prepared is None:
        return PublicationTarget.objects.get(id=target_id), False
    target, fence = prepared
    adapter = publisher_for_target(target, resolver=resolver)
    try:
        result = adapter.preflight_connection()
    finally:
        adapter.close()
    return persist_target_preflight_result(
        fence,
        result,
        audit_context=audit_context,
    )


@transaction.atomic
def create_canary_run(
    target_id: str,
    *,
    policy_version: str,
    reason: str,
    request_key: str,
    user,
    audit_context: AuditContext,
) -> TargetCanaryRun:
    _require_audit_actor(audit_context, "admin")
    if audit_context.actor_id != user.pk:
        raise Forbidden("canary audit actor differs from the administrator")
    if audit_context.request_key != request_key:
        raise Forbidden("requestKey differs from audit provenance")
    if audit_context.reason_code != reason:
        raise Forbidden("reason differs from audit provenance")

    normalized_target_id = str(uuid.UUID(str(target_id)))
    request_hash = _canary_request_hash(
        target_id=normalized_target_id,
        policy_version=policy_version,
        reason=reason,
        request_key=request_key,
    )

    def replay(existing: TargetCanaryRun) -> TargetCanaryRun:
        canonical_stored_material_hash = _canary_request_hash(
            target_id=existing.target_id,
            policy_version=existing.policy_version,
            reason=existing.reason,
            request_key=existing.request_key,
        )
        require_idempotent_match(
            stored_hash=canonical_stored_material_hash,
            expected_hash=request_hash,
        )
        if existing.requested_by_id != user.pk:
            raise RequestKeyConflict(
                "The request key is already bound to another administrator"
            )
        legacy_stored_material_hash = _legacy_admin_request_hash(
            actor_id=existing.requested_by_id,
            request_key=existing.request_key,
            reason=existing.reason,
            action="publication_target.canary_requested",
            payload={
                "targetId": str(existing.target_id),
                "policyVersion": existing.policy_version,
                "reason": existing.reason,
                "requestKey": existing.request_key,
            },
        )
        event = require_audit_replay(
            context=audit_context,
            action="publication_target.canary_requested",
            entity=existing,
            identity_key=f"canary-request:{existing.id}",
        )
        metadata = validate_stored_metadata(
            action=event.action,
            metadata_schema_version=event.metadata_schema_version,
            redaction_policy_version=event.redaction_policy_version,
            redaction_policy_hash_value=event.redaction_policy_hash,
            metadata=event.metadata_redacted,
        )
        stored_request_hash = metadata.get("request_hash")
        if stored_request_hash == canonical_stored_material_hash:
            matched_request_hash = canonical_stored_material_hash
        elif stored_request_hash == legacy_stored_material_hash:
            matched_request_hash = legacy_stored_material_hash
        else:
            raise RequestKeyConflict(
                "The stored canary request hash is not compatible with this replay"
            )
        require_audit_replay(
            context=audit_context,
            action="publication_target.canary_requested",
            entity=existing,
            identity_key=f"canary-request:{existing.id}",
            request_hash=matched_request_hash,
        )
        return existing

    existing = TargetCanaryRun.objects.filter(
        target_id=normalized_target_id,
        request_key=request_key,
    ).first()
    if existing:
        return replay(existing)

    target = PublicationTarget.objects.select_for_update().get(
        id=normalized_target_id
    )
    existing = TargetCanaryRun.objects.filter(
        target=target,
        request_key=request_key,
    ).first()
    if existing:
        return replay(existing)
    if target.environment != TargetEnvironment.TEST:
        raise Forbidden("쓰기가 발생하는 canary는 격리된 test target에서만 실행할 수 있습니다.")
    if target.preflight_state != ValidationState.PASSED:
        raise Conflict("읽기 전용 preflight를 먼저 통과해야 합니다.")
    snapshot = PublicationTargetSnapshot.objects.get(id=target.current_snapshot_id)
    run = TargetCanaryRun.objects.create(
        target=target,
        target_snapshot=snapshot,
        policy_version=policy_version,
        request_key=request_key,
        reason=reason,
        requested_by=user,
    )
    _enqueue_event(
        "publishing.target_canary.requested",
        {
            "canary_run_id": str(run.id),
            "target_id": str(target.id),
        },
        dedupe_key=f"target-canary:{run.id}",
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.canary_requested",
        entity=run,
        identity_key=f"canary-request:{run.id}",
        before_material=None,
        after_material=_audit_state(
            run,
            targetId=str(target.id),
            policyVersion=policy_version,
        ),
        metadata={
            "request_hash": request_hash,
            "target_id": str(target.id),
            "state": run.state,
            "policy_version": policy_version,
        },
    )
    return run


@dataclass(frozen=True)
class TargetCanaryFence:
    run_id: uuid.UUID
    target_id: uuid.UUID
    target_snapshot_id: uuid.UUID
    target_config_hash: str


@transaction.atomic
def begin_canary_run(
    run_id: str,
    *,
    audit_context: AuditContext,
) -> tuple[TargetCanaryRun, TargetCanaryFence | None]:
    _require_audit_actor(audit_context, "worker")
    target_id = TargetCanaryRun.objects.values_list(
        "target_id", flat=True
    ).get(id=run_id)
    _require_worker_event(
        audit_context,
        topic="publishing.target_canary.requested",
        aggregate_id=target_id,
        payload_identity={
            "canary_run_id": str(run_id),
            "target_id": str(target_id),
        },
    )
    run = (
        TargetCanaryRun.objects.select_for_update()
        .select_related("target", "target_snapshot")
        .get(id=run_id)
    )
    if _worker_audit_replay(
        audit_context,
        entity=run,
        candidates=(
            (
                "publication_target.canary_completed",
                f"{audit_context.event_key}:canary-result",
                (
                    {"result_hash": run.report_hash}
                    if run.report_hash
                    else None
                ),
            ),
        ),
    ):
        return run, None
    if run.state in {
        TargetCanaryRun.State.PASSED,
        TargetCanaryRun.State.FAILED,
        TargetCanaryRun.State.CLEANUP_REQUIRED,
    }:
        raise Conflict(
            "terminal canary result has no matching audit event for this worker event"
        )
    if run.target.current_snapshot_id != run.target_snapshot_id:
        before_material = _audit_state(run)
        run.state = TargetCanaryRun.State.FAILED
        run.report_hash = sha256_hex(
            {
                "runId": str(run.id),
                "targetSnapshotId": str(run.target_snapshot_id),
                "result": "target_snapshot_stale",
            }
        )
        run.stage_results = [{"code": "target_snapshot_stale", "passed": False}]
        run.finished_at = timezone.now()
        run.save(
            update_fields=(
                "state",
                "report_hash",
                "stage_results",
                "finished_at",
            )
        )
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.canary_completed",
            entity=run,
            identity_key=f"{audit_context.event_key}:canary-result",
            before_material=before_material,
            after_material=_audit_state(run, resultHash=run.report_hash),
            metadata={
                "target_id": str(run.target_id),
                "result": "stale",
                "result_hash": run.report_hash,
                "state": run.state,
                "policy_version": run.policy_version,
            },
        )
        return run, None
    if run.state == TargetCanaryRun.State.QUEUED:
        before_material = _audit_state(run)
        run.state = TargetCanaryRun.State.RUNNING
        run.save(update_fields=("state",))
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.canary_started",
            entity=run,
            identity_key=f"{audit_context.event_key}:canary-start",
            before_material=before_material,
            after_material=_audit_state(run),
            metadata={
                "target_id": str(run.target_id),
                "result": "started",
                "state": run.state,
                "policy_version": run.policy_version,
            },
        )
    elif run.state == TargetCanaryRun.State.RUNNING:
        if not _worker_audit_replay(
            audit_context,
            entity=run,
            candidates=(
                (
                    "publication_target.canary_started",
                    f"{audit_context.event_key}:canary-start",
                    None,
                ),
            ),
        ):
            raise Conflict(
                "running canary has no matching started audit event"
            )
        before_material = _audit_state(run)
        run.state = TargetCanaryRun.State.CLEANUP_REQUIRED
        run.stage_results = [
            {
                "code": "worker_redelivery_after_external_call_boundary",
                "passed": False,
            }
        ]
        run.report_hash = sha256_hex(run.stage_results)
        run.finished_at = timezone.now()
        run.save(
            update_fields=(
                "state",
                "stage_results",
                "report_hash",
                "finished_at",
            )
        )
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.canary_completed",
            entity=run,
            identity_key=f"{audit_context.event_key}:canary-result",
            before_material=before_material,
            after_material=_audit_state(
                run,
                resultHash=run.report_hash,
            ),
            metadata={
                "target_id": str(run.target_id),
                "result": "unknown",
                "result_hash": run.report_hash,
                "state": run.state,
                "policy_version": run.policy_version,
            },
        )
        return run, None
    return run, TargetCanaryFence(
        run_id=run.id,
        target_id=run.target_id,
        target_snapshot_id=run.target_snapshot_id,
        target_config_hash=run.target_snapshot.config_hash,
    )


@transaction.atomic
def persist_canary_run_result(
    fence: TargetCanaryFence,
    *,
    stages: list[dict[str, object]],
    passed: bool,
    audit_context: AuditContext,
) -> TargetCanaryRun:
    _require_audit_actor(audit_context, "worker")
    _lock_target_intent_fences((fence.target_id,))
    locked_intents = _lock_open_target_intents(fence.target_id)
    _require_worker_event(
        audit_context,
        topic="publishing.target_canary.requested",
        aggregate_id=fence.target_id,
        payload_identity={
            "canary_run_id": str(fence.run_id),
            "target_id": str(fence.target_id),
        },
    )
    run = (
        TargetCanaryRun.objects.select_for_update()
        .select_related("target")
        .get(id=fence.run_id)
    )
    input_result_hash = sha256_hex(
        {
            "stages": stages,
            "passed": passed,
        }
    )
    if _worker_audit_replay(
        audit_context,
        entity=run,
        candidates=(
            (
                "publication_target.canary_completed",
                f"{audit_context.event_key}:canary-result",
                {"request_hash": input_result_hash},
            ),
        ),
    ):
        return run
    before_material = _audit_state(run)
    target = PublicationTarget.objects.select_for_update().get(id=fence.target_id)
    _assert_target_has_no_active_external_write(target.id)
    target_before_material = _audit_state(target)
    fenced = (
        run.target_snapshot_id == fence.target_snapshot_id
        and target.current_snapshot_id == fence.target_snapshot_id
        and target.current_config_hash == fence.target_config_hash
    )
    if not fenced:
        stages = [*stages, {"code": "target_snapshot_stale_after_call", "passed": False}]
        passed = False
    report_hash = sha256_hex(stages)
    run.stage_results = stages
    run.report_hash = report_hash
    run.state = (
        TargetCanaryRun.State.PASSED
        if passed
        else TargetCanaryRun.State.FAILED
    )
    if any(str(row.get("code", "")).endswith("cleanup_pending") for row in stages):
        run.state = TargetCanaryRun.State.CLEANUP_REQUIRED
    run.finished_at = timezone.now()
    run.save(
        update_fields=(
            "stage_results",
            "report_hash",
            "state",
            "finished_at",
        )
    )
    if fenced:
        target.canary_state = (
            ValidationState.PASSED if passed else ValidationState.FAILED
        )
        if passed:
            target.canary_policy_version = run.policy_version
            target.connection_state = PublicationTarget.ConnectionState.VERIFIED
        target.last_canary_at = timezone.now()
        target.save()
        _snapshot_locked(target)
        _stale_locked_intents(locked_intents)
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.canary_completed",
        entity=run,
        identity_key=f"{audit_context.event_key}:canary-result",
        before_material={
            "run": before_material,
            "target": target_before_material,
        },
        after_material={
            "run": _audit_state(run, resultHash=report_hash),
            "target": _audit_state(target),
        },
        metadata={
            "target_id": str(run.target_id),
            "request_hash": input_result_hash,
            "result": "passed" if passed else "failed",
            "result_hash": report_hash,
            "state": run.state,
            "policy_version": run.policy_version,
        },
    )
    return run


def _validation_material(data: dict[str, Any], target_id: str) -> dict[str, Any]:
    return {
        "targetId": target_id,
        "topic": data["topic"],
        "targetSnapshotId": data["targetSnapshotId"],
        "targetConfigHash": data["targetConfigHash"],
        "sourceRegistrySnapshotId": data["sourceRegistrySnapshotId"],
        "registryManifestHash": data["registryManifestHash"],
        "sourceAdapterManifestHash": data["sourceAdapterManifestHash"],
        "extractionProfileManifestHash": data["extractionProfileManifestHash"],
        "generationPipelineManifestHash": data["generationPipelineManifestHash"],
        "topicPolicyVersion": data["topicPolicyVersion"],
        "editorialPolicyHash": data["editorialPolicyHash"],
        "qualityGateManifestHash": data["qualityGateManifestHash"],
        "renderContractVersion": data["renderContractVersion"],
        "channelContractVersion": data["channelContractVersion"],
        "publisherAdapterManifestHash": data["publisherAdapterManifestHash"],
        "testReportObjectKey": data["testReportObjectKey"],
        "testReportObjectVersion": data["testReportObjectVersion"],
        "testReportHash": data["testReportHash"],
    }


@transaction.atomic
def create_auto_publish_validation(
    target_id: str,
    data: dict[str, Any],
    *,
    audit_context: AuditContext,
) -> AutoPublishValidation:
    _require_audit_actor(audit_context, "admin")
    if (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden(
            "validation request provenance differs from the audit context"
        )
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_key = data["requestKey"]
    material = _validation_material(data, str(target.id))
    request_hash = _request_hash({"requestKey": request_key, **material})
    existing = AutoPublishValidation.objects.filter(target=target, request_key=request_key).first()
    if existing:
        if existing.request_hash != request_hash:
            raise Conflict("같은 request key가 다른 validation payload에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="auto_publish_validation.created",
            entity=existing,
            identity_key=f"validation-created:{existing.id}",
            request_hash=request_hash,
        )
        return existing
    if str(target.current_snapshot_id) != str(data["targetSnapshotId"]):
        raise Conflict("현재 target snapshot과 validation 대상이 다릅니다.")
    if target.current_config_hash != data["targetConfigHash"]:
        raise Conflict("현재 target config hash와 validation 대상이 다릅니다.")
    if ADAPTER_MANIFESTS[target.channel] != data["publisherAdapterManifestHash"]:
        raise Conflict("현재 배포된 publisher adapter material과 다릅니다.")
    material_hash = sha256_hex(material)
    same_material = AutoPublishValidation.objects.filter(
        target=target, material_hash=material_hash
    ).first()
    if same_material:
        return same_material
    validation = AutoPublishValidation.objects.create(
        target=target,
        topic_code=data["topic"],
        target_snapshot_id=data["targetSnapshotId"],
        target_config_hash=data["targetConfigHash"],
        source_registry_snapshot_id=data["sourceRegistrySnapshotId"],
        registry_manifest_hash=data["registryManifestHash"],
        source_adapter_manifest_hash=data["sourceAdapterManifestHash"],
        extraction_profile_manifest_hash=data["extractionProfileManifestHash"],
        generation_pipeline_manifest_hash=data["generationPipelineManifestHash"],
        topic_policy_version=data["topicPolicyVersion"],
        editorial_policy_hash=data["editorialPolicyHash"],
        quality_gate_manifest_hash=data["qualityGateManifestHash"],
        render_contract_version=data["renderContractVersion"],
        channel_contract_version=data["channelContractVersion"],
        publisher_adapter_manifest_hash=data["publisherAdapterManifestHash"],
        test_report_object_key=data["testReportObjectKey"],
        test_report_object_version=data["testReportObjectVersion"],
        test_report_hash=data["testReportHash"],
        material_hash=material_hash,
        request_key=request_key,
        request_hash=request_hash,
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="auto_publish_validation.created",
        entity=validation,
        identity_key=f"validation-created:{validation.id}",
        before_material=None,
        after_material=_audit_state(validation),
        metadata={
            "target_id": str(target.id),
            "validation_id": str(validation.id),
            "material_hash": material_hash,
            "request_hash": request_hash,
            "state": validation.status,
        },
    )
    return validation


@transaction.atomic
def decide_auto_publish_validation(
    target_id: str,
    validation_id: str,
    data: dict[str, Any],
    *,
    request,
    audit_context: AuditContext,
) -> tuple[AutoPublishValidationDecision, bool]:
    _require_audit_actor(audit_context, "admin")
    _lock_target_intent_fences((target_id,))
    user = request.user
    if audit_context.actor_id != user.pk:
        raise Forbidden("validation audit actor differs from the administrator")
    if (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden(
            "validation decision provenance differs from the audit context"
        )
    locked_intents = (
        _lock_open_intents_referencing(
            field_name="auto_publish_validation_refs",
            reference={"validationId": str(validation_id)},
        )
        if data.get("decision")
        == AutoPublishValidationDecision.Decision.REVOKED
        else []
    )
    target = PublicationTarget.objects.select_for_update().get(
        id=target_id
    )
    validation = AutoPublishValidation.objects.select_for_update().get(
        id=validation_id,
        target=target,
    )
    validation.target = target
    request_hash = _request_hash(data)
    existing = validation.decisions.filter(request_key=data["requestKey"]).first()
    if existing:
        if existing.request_hash != request_hash or existing.decided_by_id != user.pk:
            raise Conflict("같은 request key가 다른 decision payload에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="auto_publish_validation.decided",
            entity=validation,
            identity_key=f"validation-decision:{existing.id}",
            request_hash=request_hash,
        )
        return existing, False
    before_material = _audit_state(validation)
    target_before_material = _audit_state(target)
    intent_before, intent_after = _state_transition_manifests(
        [],
        state_field="state",
        next_state=PublicationIntent.State.STALE,
    )
    if _id(validation.latest_decision_id) != _id(data.get("expectedLatestDecisionId")):
        raise Conflict("validation decision이 갱신되었습니다. 다시 불러오세요.")
    consume_reauthentication_proof(
        request=request,
        proof_id=data["reauthProofId"],
        action_scope="auto_publish_change",
        entity_type="auto_publish_validation",
        entity_id=validation.id,
    )
    if validation.target.current_snapshot_id != validation.target_snapshot_id:
        raise Conflict("validation target snapshot이 이미 만료되었습니다.")
    if validation.target.current_config_hash != validation.target_config_hash:
        raise Conflict("validation target config가 이미 만료되었습니다.")
    version = validation.decision_version + 1
    decision_hash = sha256_hex(
        {
            "validationId": str(validation.id),
            "version": version,
            "decision": data["decision"],
            "supersedes": _id(validation.latest_decision_id),
            "materialHash": validation.material_hash,
            "reason": data["reason"],
        }
    )
    decision = AutoPublishValidationDecision.objects.create(
        validation=validation,
        version=version,
        decision=data["decision"],
        supersedes_decision_id=validation.latest_decision_id,
        request_key=data["requestKey"],
        request_hash=request_hash,
        decision_hash=decision_hash,
        reauth_proof_id=data["reauthProofId"],
        decided_by=user,
        reason=data["reason"],
    )
    validation.latest_decision_id = decision.id
    validation.decision_version = version
    validation.status = (
        AutoPublishValidation.State.PASSED
        if decision.decision == AutoPublishValidationDecision.Decision.PASSED
        else AutoPublishValidation.State.REVOKED
    )
    validation.save(update_fields=["latest_decision_id", "decision_version", "status"])
    if decision.decision == AutoPublishValidationDecision.Decision.REVOKED:
        target.auto_publish_enabled = False
        target.save(update_fields=["auto_publish_enabled", "updated_at"])
        intent_before, intent_after = _stale_locked_intents(locked_intents)
    _record_publishing_audit(
        audit_context=audit_context,
        action="auto_publish_validation.decided",
        entity=validation,
        identity_key=f"validation-decision:{decision.id}",
        before_material={
            "validation": before_material,
            "target": target_before_material,
            "staleIntents": intent_before,
        },
        after_material={
            "validation": _audit_state(
                validation,
                decisionHash=decision_hash,
            ),
            "target": _audit_state(target),
            "staleIntents": intent_after,
        },
        metadata={
            "target_id": str(validation.target_id),
            "validation_id": str(validation.id),
            "decision_id": str(decision.id),
            "decision": decision.decision,
            "decision_hash": decision_hash,
            "state": validation.status,
            "version": decision.version,
            "reauth_proof_id": str(decision.reauth_proof_id),
        },
    )
    return decision, True


def _normalized_validation_refs(refs: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    normalized = [
        {
            "targetId": str(ref["targetId"]),
            "targetSnapshotId": str(ref["targetSnapshotId"]),
            "validationId": str(ref["validationId"]),
            "materialHash": ref["materialHash"],
        }
        for ref in refs
    ]
    return sorted(normalized, key=lambda row: (row["targetId"], row["validationId"]))


@transaction.atomic
def set_auto_publish(
    target_id: str,
    data: dict[str, Any],
    *,
    request,
    audit_context: AuditContext,
) -> tuple[AutoPublishActivation, bool]:
    _require_audit_actor(audit_context, "admin")
    _lock_target_intent_fences((target_id,))
    user = request.user
    if audit_context.actor_id != user.pk:
        raise Forbidden("activation audit actor differs from the administrator")
    if (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden(
            "activation provenance differs from the audit context"
        )
    disabling_activation_id = (
        data.get("expectedLatestActivationId")
        if not bool(data.get("enabled"))
        else None
    )
    locked_intents = (
        _lock_open_intents_referencing(
            field_name="auto_publish_activation_refs",
            reference={"activationId": str(disabling_activation_id)},
        )
        if disabling_activation_id is not None
        else []
    )
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_hash = _request_hash(data)
    existing = target.activations.filter(request_key=data["requestKey"]).first()
    if existing:
        if existing.request_hash != request_hash or existing.decided_by_id != user.pk:
            raise Conflict("같은 request key가 다른 activation payload에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="auto_publish_activation.decided",
            entity=target,
            identity_key=f"auto-activation:{existing.id}",
            request_hash=request_hash,
        )
        return existing, False
    before_material = _audit_state(target)
    intent_before, intent_after = _state_transition_manifests(
        [],
        state_field="state",
        next_state=PublicationIntent.State.STALE,
    )
    if _id(target.latest_auto_publish_activation_id) != _id(data.get("expectedLatestActivationId")):
        raise Conflict("자동발행 상태가 갱신되었습니다. 다시 불러오세요.")
    consume_reauthentication_proof(
        request=request,
        proof_id=data["reauthProofId"],
        action_scope="auto_publish_change",
        entity_type="publication_target",
        entity_id=target.id,
    )
    enabled = bool(data["enabled"])
    refs = _normalized_validation_refs(data.get("validationRefs", []))
    if enabled:
        if target.connection_state != PublicationTarget.ConnectionState.VERIFIED:
            raise Conflict("검증된 연결만 자동발행할 수 있습니다.")
        if target.preflight_state != ValidationState.PASSED:
            raise Conflict("현재 target preflight를 통과해야 합니다.")
        if target.environment == TargetEnvironment.PRODUCTION:
            if not target.canary_target_id or target.canary_target.canary_state != ValidationState.PASSED:
                raise Conflict("동일 채널 test target의 현재 canary가 필요합니다.")
            if target.pilot_state != ValidationState.PASSED:
                raise Conflict("관리자 승인 운영 파일럿 게시가 필요합니다.")
        if not refs:
            raise InvalidInput("활성화에는 최소 한 개의 passed validation이 필요합니다.")
        validation_ids = [ref["validationId"] for ref in refs]
        validations = list(AutoPublishValidation.objects.filter(id__in=validation_ids, target=target))
        if len(validations) != len(validation_ids):
            raise InvalidInput("validation target 또는 ID가 올바르지 않습니다.")
        by_id = {str(row.id): row for row in validations}
        for ref in refs:
            validation = by_id[ref["validationId"]]
            if validation.status != AutoPublishValidation.State.PASSED:
                raise Conflict("passed 상태가 아닌 validation은 활성화할 수 없습니다.")
            if validation.material_hash != ref["materialHash"]:
                raise Conflict("validation material hash가 다릅니다.")
            if validation.target_snapshot_id != target.current_snapshot_id:
                raise Conflict("validation target snapshot이 현재 값과 다릅니다.")
            if ref["targetSnapshotId"] != str(target.current_snapshot_id):
                raise Conflict("validation ref snapshot이 현재 값과 다릅니다.")
    elif refs:
        raise InvalidInput("비활성화 요청에는 validation refs가 없어야 합니다.")
    version = target.auto_publish_activation_version + 1
    validation_manifest_hash = sha256_hex(refs)
    decision = AutoPublishActivation.Decision.ENABLED if enabled else AutoPublishActivation.Decision.REVOKED
    activation_hash = sha256_hex(
        {
            "targetId": str(target.id),
            "targetSnapshotId": str(target.current_snapshot_id),
            "operationalConfigHash": target.current_config_hash,
            "validationRefs": refs,
            "version": version,
            "decision": decision,
            "supersedes": _id(target.latest_auto_publish_activation_id),
        }
    )
    activation = AutoPublishActivation.objects.create(
        target=target,
        target_snapshot_id=target.current_snapshot_id,
        target_operational_config_hash=target.current_config_hash,
        validation_refs=refs,
        validation_manifest_hash=validation_manifest_hash,
        version=version,
        decision=decision,
        supersedes_activation_id=target.latest_auto_publish_activation_id,
        request_key=data["requestKey"],
        request_hash=request_hash,
        activation_hash=activation_hash,
        reauth_proof_id=data["reauthProofId"],
        decided_by=user,
        reason=data["reason"],
    )
    target.latest_auto_publish_activation_id = activation.id
    target.auto_publish_activation_version = version
    target.auto_publish_enabled = enabled
    target.save(
        update_fields=[
            "latest_auto_publish_activation_id",
            "auto_publish_activation_version",
            "auto_publish_enabled",
            "updated_at",
        ]
    )
    if not enabled:
        intent_before, intent_after = _stale_locked_intents(locked_intents)
    _record_publishing_audit(
        audit_context=audit_context,
        action="auto_publish_activation.decided",
        entity=target,
        identity_key=f"auto-activation:{activation.id}",
        before_material={
            **before_material,
            "staleIntents": intent_before,
        },
        after_material=_audit_state(
            target,
            activationHash=activation_hash,
            staleIntents=intent_after,
        ),
        metadata={
            "target_id": str(target.id),
            "activation_id": str(activation.id),
            "decision": activation.decision,
            "decision_hash": activation_hash,
            "enabled": target.auto_publish_enabled,
            "version": activation.version,
            "reauth_proof_id": str(activation.reauth_proof_id),
        },
    )
    return activation, True


def _target_ref_map(refs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for ref in refs:
        key = str(ref["targetId"])
        if key in result:
            raise InvalidInput("target snapshot ref가 중복되었습니다.")
        result[key] = {
            "targetId": key,
            "targetSnapshotId": str(ref["targetSnapshotId"]),
            "targetConfigHash": ref["targetConfigHash"],
        }
    return result


def _command_map(commands: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for command in commands:
        target_id = str(command["targetId"])
        if target_id in result:
            raise InvalidInput("target command가 중복되었습니다.")
        normalized = {
            "targetId": target_id,
            "targetSnapshotId": str(command["targetSnapshotId"]),
            "targetConfigHash": command["targetConfigHash"],
            "resolvedAction": command["resolvedAction"],
            "canonicalDependencyTargetId": _id(command.get("canonicalDependencyTargetId")),
        }
        normalized["targetCommandHash"] = sha256_hex(normalized)
        result[target_id] = normalized
    return result


def _current_revision(
    article_id: str,
    revision_no: int,
    *,
    commands: Iterable[dict[str, Any]],
):
    DraftArticle = apps.get_model("editorial", "DraftArticle")
    ArticleRevision = apps.get_model("editorial", "ArticleRevision")
    try:
        current_revision_id = DraftArticle.objects.values_list(
            "current_revision_id", flat=True
        ).get(id=article_id)
    except DraftArticle.DoesNotExist as exc:
        raise NotFound("글을 찾을 수 없습니다.") from exc
    if not current_revision_id:
        raise Conflict("현재 개정이 없습니다.")
    revision = ArticleRevision.objects.select_related("generation_attempt").get(
        id=current_revision_id
    )
    _require_revision_for_commands(revision, commands)
    article = DraftArticle.objects.select_for_update().get(id=article_id)
    revision = ArticleRevision.objects.select_related("generation_attempt").get(
        id=article.current_revision_id
    )
    if revision.revision_no != revision_no:
        raise Conflict("현재 개정 번호가 바뀌었습니다.")
    if (
        _commands_require_publishable_revision(commands)
        and revision.quality_state != "passed"
    ):
        raise Conflict("차단 품질 검사를 모두 통과해야 발행할 수 있습니다.")
    return article, revision


def create_publication_intent(
    article_id: str,
    data: dict[str, Any],
    *,
    user,
    audit_context: AuditContext,
) -> PublicationIntent:
    try:
        return _create_publication_intent_atomic(
            article_id,
            data,
            user=user,
            audit_context=audit_context,
        )
    except _StaleIntentConflict as exc:
        _raise_persisted_stale_intent(exc)


@transaction.atomic
def _create_publication_intent_atomic(
    article_id: str,
    data: dict[str, Any],
    *,
    user,
    audit_context: AuditContext,
) -> PublicationIntent:
    if audit_context.actor_type not in {"admin", "worker"}:
        raise Forbidden("admin or worker audit provenance is required")
    if audit_context.actor_type == "admin" and audit_context.actor_id != user.pk:
        raise Forbidden("intent audit actor differs from the administrator")
    if audit_context.actor_type == "admin" and (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden("intent provenance differs from the audit context")
    request_key = data["requestKey"]
    commands = _command_map(data["targetCommands"])
    _lock_article_external_write_fence(article_id)
    _lock_target_intent_fences(commands)
    existing = (
        PublicationIntent.objects.select_related("article_revision")
        .filter(article_id=article_id, request_key=request_key)
        .order_by("created_at")
        .first()
    )
    if existing:
        existing = _require_intent_revision_publishable(existing)
        existing_refs = _target_ref_map(data["targetSnapshots"])
        candidate_data = {
            **data,
            **_revision_publication_material(existing.article_revision),
        }
        candidate_hash = _intent_hash(
            candidate_data,
            existing.article_revision,
            existing_refs,
            commands,
        )
        if (
            existing.revision_no != int(data["revisionNo"])
            or existing.revision_content_hash != data["expectedRevisionContentHash"]
            or existing.intent_hash != candidate_hash
        ):
            raise Conflict("같은 request key가 다른 발행 의도에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="publication_intent.created",
            entity=existing,
            identity_key=f"publication-intent:{existing.id}",
            request_hash=existing.intent_hash,
        )
        return existing
    article, revision = _current_revision(
        article_id,
        int(data["revisionNo"]),
        commands=commands.values(),
    )
    revision_content_hash = revision.content_hash
    if revision_content_hash != data["expectedRevisionContentHash"]:
        raise Conflict("현재 개정 본문 hash가 요청 시점과 다릅니다.")
    material_data = {
        **data,
        **_revision_publication_material(revision),
    }
    target_refs = _target_ref_map(data["targetSnapshots"])
    if set(target_refs) != set(commands):
        raise InvalidInput("target snapshot과 command target 집합이 같아야 합니다.")
    latest = (
        PublicationIntent.objects.select_for_update()
        .filter(article_id=article.id)
        .order_by("-created_at")
        .first()
    )
    if _id(latest.id if latest else None) != _id(data.get("expectedLatestIntentId")):
        raise Conflict("발행 의도가 갱신되었습니다. 다시 불러오세요.")
    latest_before_material = _audit_state(latest) if latest else None
    target_rows = {
        str(row.id): row
        for row in PublicationTarget.objects.select_for_update()
        .filter(id__in=target_refs.keys())
        .order_by("id")
    }
    if set(target_rows) != set(target_refs):
        raise InvalidInput("알 수 없는 발행 target이 포함되었습니다.")
    wordpress_ids = [key for key, row in target_rows.items() if row.channel == ChannelCode.WORDPRESS]
    for target_id, target in target_rows.items():
        ref = target_refs[target_id]
        command = commands[target_id]
        if str(target.current_snapshot_id) != ref["targetSnapshotId"]:
            raise Conflict(f"{target.display_name} snapshot이 바뀌었습니다.")
        if target.current_config_hash != ref["targetConfigHash"]:
            raise Conflict(f"{target.display_name} config가 바뀌었습니다.")
        if command["targetSnapshotId"] != ref["targetSnapshotId"] or command["targetConfigHash"] != ref["targetConfigHash"]:
            raise InvalidInput("target command와 snapshot ref가 다릅니다.")
        if command["resolvedAction"] not in target.capabilities or not target.capabilities[command["resolvedAction"]]:
            raise InvalidInput(f"{target.display_name}은 요청한 동작을 지원하지 않습니다.")
        if target.channel == ChannelCode.BLOGGER and command["resolvedAction"] != PublicationAction.UNPUBLISH:
            dependency = command["canonicalDependencyTargetId"]
            if dependency not in wordpress_ids:
                prior_wordpress = Publication.objects.filter(
                    article_id=article.id,
                    target_id=dependency,
                    target__channel=ChannelCode.WORDPRESS,
                    state=Publication.State.PUBLISHED,
                    canonical_ready_at__isnull=False,
                ).exists()
                if not prior_wordpress:
                    raise InvalidInput("Blogger 발행에는 대표 WordPress target이 필요합니다.")
    mode = data["approvalMode"]
    validation_refs = _normalized_validation_refs(data.get("autoPublishValidationRefs", []))
    activation_refs = sorted(
        [
            {
                "targetId": str(ref["targetId"]),
                "targetSnapshotId": str(ref["targetSnapshotId"]),
                "activationId": str(ref["activationId"]),
                "version": int(ref["version"]),
                "activationHash": ref["activationHash"],
            }
            for ref in data.get("autoPublishActivationRefs", [])
        ],
        key=lambda row: row["targetId"],
    )
    if mode == ApprovalMode.MANUAL and (validation_refs or activation_refs):
        raise InvalidInput("manual 발행 의도에는 자동발행 참조를 포함할 수 없습니다.")
    if mode == ApprovalMode.VALIDATED_AUTO:
        if {ref["targetId"] for ref in validation_refs} != set(target_refs):
            raise InvalidInput("validation target 집합이 발행 target과 같아야 합니다.")
        if {ref["targetId"] for ref in activation_refs} != set(target_refs):
            raise InvalidInput("activation target 집합이 발행 target과 같아야 합니다.")
        for ref in activation_refs:
            target = target_rows[ref["targetId"]]
            if not target.auto_publish_enabled or str(target.latest_auto_publish_activation_id) != ref["activationId"]:
                raise Conflict("현재 enabled activation과 발행 의도가 다릅니다.")
            activation = AutoPublishActivation.objects.get(id=ref["activationId"], target=target)
            if activation.activation_hash != ref["activationHash"] or activation.version != ref["version"]:
                raise Conflict("activation material이 다릅니다.")
    normalized_refs = sorted(target_refs.values(), key=lambda row: row["targetId"])
    normalized_commands = sorted(commands.values(), key=lambda row: row["targetId"])
    intent_hash = _intent_hash(material_data, revision, target_refs, commands)
    intent = PublicationIntent.objects.create(
        article_id=article.id,
        article_revision_id=revision.id,
        revision_no=revision.revision_no,
        revision_content_hash=material_data["revisionContentHash"],
        correction_case_id=data.get("correctionCaseId"),
        origin_collection_run_id=getattr(article, "source_run_id", None),
        target_snapshot_refs=normalized_refs,
        target_commands=normalized_commands,
        target_snapshot_manifest_hash=sha256_hex(normalized_refs),
        approval_mode=mode,
        auto_publish_validation_refs=validation_refs,
        auto_validation_manifest_hash=sha256_hex(validation_refs) if validation_refs else None,
        auto_publish_activation_refs=activation_refs,
        auto_activation_manifest_hash=sha256_hex(activation_refs) if activation_refs else None,
        generation_attempt_id=material_data["generationAttemptId"],
        input_evidence_manifest_hash=material_data["inputEvidenceManifestHash"],
        generation_pipeline_manifest_hash=material_data["generationPipelineManifestHash"],
        quality_gate_manifest_hash=material_data["qualityGateManifestHash"],
        quality_report_hash=material_data["qualityReportHash"],
        supersedes_intent_id=latest.id if latest else None,
        intent_hash=intent_hash,
        request_key=request_key,
        state=PublicationIntent.State.AWAITING_APPROVAL,
        created_by=user,
    )
    if latest and latest.state not in {PublicationIntent.State.DISPATCHED, PublicationIntent.State.CANCELLED}:
        latest.state = PublicationIntent.State.STALE
        latest.save(update_fields=["state"])
    for target_id in sorted(target_rows):
        if commands[target_id]["resolvedAction"] != PublicationAction.UNPUBLISH:
            _create_preview_render(intent, revision, target_rows[target_id])
    render_manifest = list(
        intent.renders.order_by("target_id", "id").values(
            "id",
            "target_id",
            "template_hash",
            "content_hash",
            "source_manifest_hash",
        )
    )
    render_manifest_hash = sha256_hex(
        [
            {
                **row,
                "id": str(row["id"]),
                "target_id": str(row["target_id"]),
            }
            for row in render_manifest
        ]
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_intent.created",
        entity=intent,
        identity_key=f"publication-intent:{intent.id}",
        before_material={
            "intent": None,
            "supersededIntent": latest_before_material,
            "renderCount": 0,
            "renderManifestHash": sha256_hex([]),
        },
        after_material={
            "intent": _audit_state(intent),
            "supersededIntent": (
                _audit_state(latest) if latest else None
            ),
            "renderCount": len(render_manifest),
            "renderManifestHash": render_manifest_hash,
        },
        metadata={
            "intent_id": str(intent.id),
            "revision_id": str(intent.article_revision_id),
            "revision_no": intent.revision_no,
            "intent_hash": intent.intent_hash,
            "request_hash": intent.intent_hash,
            "state": intent.state,
            "count": len(target_rows),
        },
    )
    return intent


def _intent_hash(data, revision, target_refs, commands) -> str:
    return sha256_hex(
        {
            "articleRevisionId": str(revision.id),
            "revisionNo": revision.revision_no,
            "revisionContentHash": data["revisionContentHash"],
            "targetSnapshots": sorted(target_refs.values(), key=lambda row: row["targetId"]),
            "targetCommands": sorted(commands.values(), key=lambda row: row["targetId"]),
            "approvalMode": data["approvalMode"],
            "validationRefs": _normalized_validation_refs(data.get("autoPublishValidationRefs", [])),
            "activationRefs": sorted(data.get("autoPublishActivationRefs", []), key=lambda row: str(row["targetId"])),
            "inputEvidenceManifestHash": data["inputEvidenceManifestHash"],
            "generationPipelineManifestHash": data.get("generationPipelineManifestHash"),
            "qualityGateManifestHash": data["qualityGateManifestHash"],
            "qualityReportHash": data["qualityReportHash"],
            "editorialPolicyHash": data["editorialPolicyHash"],
            "verificationManifestHash": data["verificationManifestHash"],
            "exclusionManifestHash": data["exclusionManifestHash"],
            "claimManifestHash": data["claimManifestHash"],
            "revalidationGeneration": data["revalidationGeneration"],
            "correctionCaseId": data.get("correctionCaseId"),
        }
    )


def _markdown_to_html(markdown: str) -> str:
    blocks: list[str] = []
    in_list = False
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            continue
        if line.startswith("### "):
            blocks.append(f"<h3>{_render_markdown_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            blocks.append(f"<h2>{_render_markdown_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            blocks.append(f"<h1>{_render_markdown_inline(line[2:])}</h1>")
        elif line.startswith("- "):
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{_render_markdown_inline(line[2:])}</li>")
        else:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            blocks.append(f"<p>{_render_markdown_inline(line)}</p>")
    if in_list:
        blocks.append("</ul>")
    return "\n".join(blocks)


_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")


def _render_markdown_inline(value: str) -> str:
    """Render only HTTPS/HTTP Markdown links; all other input remains escaped text."""
    rendered: list[str] = []
    cursor = 0
    for match in _MARKDOWN_LINK.finditer(value):
        rendered.append(html.escape(value[cursor : match.start()]))
        label, url = match.groups()
        rendered.append(
            '<a href="{}" rel="noopener noreferrer">{}</a>'.format(
                html.escape(url, quote=True),
                html.escape(label),
            )
        )
        cursor = match.end()
    rendered.append(html.escape(value[cursor:]))
    return "".join(rendered)


def _revision_source_links(revision) -> list[str]:
    values = revision.claims.values_list(
        "evidence_links__evidence__source_item__canonical_url",
        flat=True,
    )
    return sorted({str(value) for value in values if value})


def _create_preview_render(intent, revision, target) -> ArticleChannelRender:
    body = _markdown_to_html(revision.body_markdown)
    source_links = _revision_source_links(revision)
    if revision.claims.exists() and not source_links:
        raise Conflict("게시 주장에 독자가 접근할 수 있는 원출처 URL이 없습니다.")
    canonical_state = ArticleChannelRender.CanonicalState.NOT_APPLICABLE
    if target.channel == ChannelCode.BLOGGER:
        canonical_state = ArticleChannelRender.CanonicalState.PENDING
        body += '\n<p class="canonical-source">원문: {{CANONICAL_WORDPRESS_URL}}</p>'
    template_material = {
        "channel": target.channel,
        "title": revision.title,
        "body": body,
        "revision": str(revision.id),
    }
    template_hash = sha256_hex(template_material)
    source_manifest_hash = sha256_hex(
        {"inputEvidenceManifestHash": intent.input_evidence_manifest_hash, "sourceLinks": source_links}
    )
    return ArticleChannelRender.objects.create(
        publication_intent=intent,
        article_revision_id=revision.id,
        target=target,
        target_snapshot_id=target.current_snapshot_id,
        target_config_hash=target.current_config_hash,
        channel_role=target.role,
        render_stage=ArticleChannelRender.Stage.PREVIEW,
        title=revision.title,
        body_html=body,
        labels=["반도체" if getattr(revision.article, "topic_code", "") == "semiconductor_news" else "청약정보"],
        source_links=source_links,
        included_claim_ids=[str(value) for value in revision.claims.values_list("id", flat=True)],
        canonical_link_state=canonical_state,
        template_hash=template_hash,
        content_hash=sha256_hex({"title": revision.title, "body": body}),
        source_manifest_hash=source_manifest_hash,
    )


def get_article_preview(article_id: str, target_id: str) -> ArticleChannelRender:
    try:
        return _get_article_preview_atomic(article_id, target_id)
    except _StaleIntentConflict as exc:
        _raise_persisted_stale_intent(exc)


@transaction.atomic
def _get_article_preview_atomic(
    article_id: str,
    target_id: str,
) -> ArticleChannelRender:
    intent = (
        PublicationIntent.objects.select_related(
            "article_revision__generation_attempt"
        )
        .filter(article_id=article_id)
        .order_by("-created_at")
        .first()
    )
    if intent is None:
        raise InvalidInput("Create a publication intent before requesting a preview.")
    intent = _require_intent_revision_publishable(intent)
    latest_id = (
        PublicationIntent.objects.filter(article_id=article_id)
        .order_by("-created_at")
        .values_list("id", flat=True)
        .first()
    )
    if latest_id != intent.id or intent.state == PublicationIntent.State.STALE:
        raise _StaleIntentConflict(intent.id, "publication preview is stale")
    return intent.renders.get(
        target_id=target_id,
        render_stage=ArticleChannelRender.Stage.PREVIEW,
    )


def decide_approval(
    article_id: str,
    target_id: str,
    data: dict[str, Any],
    *,
    user,
    audit_context: AuditContext,
    request=None,
) -> tuple[Approval, bool]:
    try:
        return _decide_approval_atomic(
            article_id,
            target_id,
            data,
            user=user,
            audit_context=audit_context,
            request=request,
        )
    except _StaleIntentConflict as exc:
        _raise_persisted_stale_intent(exc)


@transaction.atomic
def _decide_approval_atomic(
    article_id: str,
    target_id: str,
    data: dict[str, Any],
    *,
    user,
    audit_context: AuditContext,
    request=None,
) -> tuple[Approval, bool]:
    if user is None or getattr(user, "pk", None) is None:
        raise Forbidden("approval requires an accountable administrator")
    if audit_context.actor_type not in {"admin", "worker"}:
        raise Forbidden("admin or worker audit provenance is required")
    if audit_context.actor_type == "admin" and audit_context.actor_id != user.pk:
        raise Forbidden("approval audit actor differs from the administrator")
    if audit_context.actor_type == "admin" and audit_context.event_key is not None:
        raise Forbidden("admin approval cannot carry worker event provenance")
    if audit_context.actor_type == "worker" and (
        audit_context.actor_id is not None or not audit_context.event_key
    ):
        raise Forbidden("worker approval requires an exact event key")
    canonical_reason_present = "decisionReason" in data
    decision_reason = str(
        data.get("decisionReason")
        if canonical_reason_present
        else data.get("reason", "")
    ).strip()
    if audit_context.actor_type == "admin" and (
        data.get("requestKey") != audit_context.request_key
        or (
            canonical_reason_present
            and decision_reason != audit_context.reason_code
        )
    ):
        raise Forbidden("approval provenance differs from the audit context")
    if data.get("decision") not in Approval.Decision.values:
        raise InvalidInput("approval decision is invalid")

    request_hash = _approval_request_hash(
        article_id=article_id,
        target_id=target_id,
        payload=data,
    )
    existing = (
        Approval.objects.filter(
            publication_intent_id=data["publicationIntentId"],
            publication_intent__article_id=article_id,
            target_id=target_id,
            request_key=data["requestKey"],
        )
        .select_related("publication_intent", "target")
        .first()
    )
    if existing:
        if (
            audit_context.actor_type == "admin"
            and decision_reason != audit_context.reason_code
        ):
            raise Forbidden("approval provenance differs from the audit context")
        _validate_approval_mode_actor(
            mode=existing.mode,
            decision=existing.decision,
            audit_context=audit_context,
        )
        matched_request_hash = _approval_replay_request_hash(
            stored_hash=existing.request_hash,
            current_hash=request_hash,
            payload=data,
        )
        if (
            existing.admin_id != user.pk
            or existing.decision_actor_type != audit_context.actor_type
            or _id(existing.decision_actor_id) != _id(audit_context.actor_id)
            or _id(existing.decision_event_key) != _id(audit_context.event_key)
        ):
            raise Conflict("request key was reused for a different approval decision")
        require_audit_replay(
            context=audit_context,
            action="publication_approval.decided",
            entity=existing,
            identity_key=f"publication-approval:{existing.id}",
            request_hash=matched_request_hash,
        )
        return existing, False

    if not canonical_reason_present or "reason" in data:
        raise InvalidInput("decisionReason is required for a new approval decision")
    if len(decision_reason) < 3 or len(decision_reason) > 500:
        raise InvalidInput("decisionReason must contain 3 to 500 characters")

    intent_candidate = PublicationIntent.objects.select_related(
        "article_revision__generation_attempt"
    ).get(id=data["publicationIntentId"], article_id=article_id)
    command = next(
        (
            row
            for row in intent_candidate.target_commands
            if str(row["targetId"]) == str(target_id)
        ),
        None,
    )
    if command is None:
        raise InvalidInput("publication intent has no command for this target")
    _lock_article_external_write_fence(article_id)
    _lock_target_intent_fences(_intent_target_ids(intent_candidate))
    intent = (
        PublicationIntent.objects.select_for_update()
        .select_related("article_revision__generation_attempt")
        .get(id=data["publicationIntentId"], article_id=article_id)
    )
    _validate_approval_mode_actor(
        mode=intent.approval_mode,
        decision=data["decision"],
        audit_context=audit_context,
    )
    intent = _require_intent_revision_publishable(
        intent,
        approval_decision=data["decision"],
    )
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    existing = Approval.objects.select_for_update().filter(
        publication_intent=intent,
        target_id=target_id,
        request_key=data["requestKey"],
    ).first()
    if existing:
        _validate_approval_mode_actor(
            mode=existing.mode,
            decision=existing.decision,
            audit_context=audit_context,
        )
        matched_request_hash = _approval_replay_request_hash(
            stored_hash=existing.request_hash,
            current_hash=request_hash,
            payload=data,
        )
        if (
            existing.admin_id != user.pk
            or existing.decision_actor_type != audit_context.actor_type
            or _id(existing.decision_actor_id) != _id(audit_context.actor_id)
            or _id(existing.decision_event_key) != _id(audit_context.event_key)
        ):
            raise Conflict("request key was reused for a different approval decision")
        require_audit_replay(
            context=audit_context,
            action="publication_approval.decided",
            entity=existing,
            identity_key=f"publication-approval:{existing.id}",
            request_hash=matched_request_hash,
        )
        return existing, False

    if int(data.get("revisionNo", 0)) != intent.revision_no:
        raise Conflict("approval revision number differs from the frozen intent")
    command = next(
        (
            row
            for row in intent.target_commands
            if str(row["targetId"]) == str(target_id)
        ),
        None,
    )
    if command is None:
        raise InvalidInput("publication intent has no command for this target")
    latest_intent = (
        PublicationIntent.objects.filter(article_id=article_id)
        .order_by("-created_at")
        .first()
    )
    safety_transition = (
        data["decision"]
        in {Approval.Decision.REJECTED, Approval.Decision.REVOKED}
        or command["resolvedAction"]
        in {PublicationAction.UNPUBLISH, PublicationAction.MARK_WITHDRAWN}
    )
    if not safety_transition and (
        latest_intent is None
        or latest_intent.id != intent.id
        or intent.state == PublicationIntent.State.STALE
    ):
        raise Conflict("only the current publication intent can be approved")
    if (
        _approval_requires_current_target_snapshot(
            data["decision"], command["resolvedAction"]
        )
        and (
            str(target.current_snapshot_id) != str(command["targetSnapshotId"])
            or target.current_config_hash != command["targetConfigHash"]
        )
    ):
        raise Conflict("target snapshot changed after the preview was frozen")

    latest = _latest_approval_locked(intent=intent, target_id=target.id)
    try:
        expected_head_version = int(data["expectedHeadVersion"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidInput("expectedHeadVersion is required") from exc
    _validate_approval_cas(
        latest,
        expected_latest_approval_id=data.get("expectedLatestApprovalId"),
        expected_head_version=expected_head_version,
    )
    _validate_approval_transition(
        latest.decision if latest else None,
        data["decision"],
    )

    intent_before_material = _audit_state(intent)
    subject = data["actionSubject"]
    action = command["resolvedAction"]
    render = None
    publication = None
    render_template_hash = None
    if action == PublicationAction.UNPUBLISH:
        publication = (
            Publication.objects.select_for_update()
            .filter(article_id=article_id, target=target)
            .first()
        )
        source_manifest_hash = subject.get("correctionEvidenceManifestHash", "")
    else:
        render = ArticleChannelRender.objects.select_for_update().get(
            id=subject.get("renderId"),
            publication_intent=intent,
            target=target,
            render_stage=ArticleChannelRender.Stage.PREVIEW,
        )
        render_template_hash = render.template_hash
        source_manifest_hash = render.source_manifest_hash
    _validate_approval_action_subject(
        intent=intent,
        target=target,
        command=command,
        subject=subject,
        render=render,
        publication=publication,
        decision_reason=decision_reason,
    )

    requires_reauthentication = (
        data["decision"] == Approval.Decision.REVOKED
        or (
            action == PublicationAction.UNPUBLISH
            and data["decision"] == Approval.Decision.APPROVED
        )
    )
    if not requires_reauthentication and data.get("reauthProofId") is not None:
        raise InvalidInput("reauthProofId must be null for this decision")

    staleable_revoke_attempts: list[PublicationAttempt] = []
    if data["decision"] == Approval.Decision.REVOKED:
        if request is None or not data.get("reauthProofId"):
            raise Forbidden("approval revoke requires recent reauthentication")
        blocking_attempts, staleable_revoke_attempts = (
            _lock_revoke_attempts_for_article_target(
                article_id=article_id,
                target_id=target.id,
            )
        )
        if blocking_attempts:
            raise Conflict("approval cannot be revoked during an active attempt")
        consume_reauthentication_proof(
            request=request,
            proof_id=data["reauthProofId"],
            action_scope="approval_revoke",
            entity_type="publication_approval",
            entity_id=latest.id,
        )
    elif (
        action == PublicationAction.UNPUBLISH
        and data["decision"] == Approval.Decision.APPROVED
    ):
        if request is None or not data.get("reauthProofId"):
            raise Forbidden("unpublish approval requires recent reauthentication")
        consume_reauthentication_proof(
            request=request,
            proof_id=data["reauthProofId"],
            action_scope="unpublish",
            entity_type="publication",
            entity_id=publication.id,
        )

    head_version = (latest.head_version if latest else 0) + 1
    approval_subject_hash = _approval_subject_hash(
        intent=intent,
        target=target,
        command=command,
        subject=subject,
        render=render,
        publication=publication,
    )
    if latest is not None and latest.approval_subject_hash != approval_subject_hash:
        raise Conflict("approval decision cannot change the frozen subject")
    decision_hash = _approval_decision_hash(
        subject_hash=approval_subject_hash,
        decision=data["decision"],
        head_version=head_version,
        supersedes_approval_id=latest.id if latest else None,
        request_hash=request_hash,
        actor_type=audit_context.actor_type,
        actor_id=audit_context.actor_id,
        event_key=audit_context.event_key,
        decision_reason=decision_reason,
    )
    approval = Approval.objects.create(
        article_revision_id=intent.article_revision_id,
        revision_no=intent.revision_no,
        publication_intent=intent,
        target=target,
        target_action=action,
        article_channel_render=render,
        action_subject=subject,
        target_snapshot_id=command["targetSnapshotId"],
        target_config_hash=command["targetConfigHash"],
        mode=intent.approval_mode,
        decision=data["decision"],
        approval_subject_hash=approval_subject_hash,
        approval_material_version=_APPROVAL_MATERIAL_VERSION,
        decision_hash=decision_hash,
        decision_reason=decision_reason,
        decision_actor_type=audit_context.actor_type,
        decision_actor_id=audit_context.actor_id,
        decision_event_key=audit_context.event_key,
        head_version=head_version,
        supersedes_approval_id=latest.id if latest else None,
        request_key=data["requestKey"],
        request_hash=request_hash,
        reauth_proof_id=data.get("reauthProofId"),
        policy_snapshot_hash=intent.article_revision.editorial_policy_hash,
        quality_report_hash=intent.quality_report_hash,
        render_template_hash=render_template_hash,
        source_manifest_hash=source_manifest_hash,
        admin=user,
    )
    head = PublicationApprovalHead.objects.select_for_update().get(
        publication_intent=intent,
        target=target,
    )
    if (
        head.latest_approval_id != approval.id
        or head.version != head_version
        or head.subject_hash != approval_subject_hash
    ):
        raise Conflict("database approval head did not advance to the new decision")

    if data["decision"] == Approval.Decision.REVOKED:
        reset_publication_ids: set[uuid.UUID] = set()
        for attempt in staleable_revoke_attempts:
            attempt.state = PublicationAttempt.State.STALE
            attempt.finished_at = timezone.now()
            attempt.error_code = "approval_revoked"
            attempt.terminal_impact = _publication_terminal_impact(
                stage="approval_revoke",
                final_state=attempt.state,
                error_code=attempt.error_code,
            )
            attempt.recovery_state = PublicationRecoveryState.STOPPED
            attempt.next_recovery_at = None
            attempt.next_retry_at = None
            attempt.save(
                update_fields=(
                    "state",
                    "finished_at",
                    "error_code",
                    "terminal_impact",
                    "recovery_state",
                    "next_recovery_at",
                    "next_retry_at",
                )
            )
            if attempt.publication_id not in reset_publication_ids:
                publication_row = attempt.publication
                publication_row.state = (
                    Publication.State.PUBLISHED
                    if publication_row.remote_post_id
                    else Publication.State.PENDING
                )
                publication_row.scheduled_for = None
                publication_row.last_error_code = ""
                publication_row.save(
                    update_fields=(
                        "state",
                        "scheduled_for",
                        "last_error_code",
                        "updated_at",
                    )
                )
                reset_publication_ids.add(attempt.publication_id)
        for attempt in staleable_revoke_attempts:
            _project_origin_run_terminal_locked(attempt)

    target_ids = {str(row["targetId"]) for row in intent.target_commands}
    decisions_by_target: dict[str, str] = {}
    for command_row in intent.target_commands:
        current = _latest_approval_locked(
            intent=intent,
            target_id=command_row["targetId"],
        )
        if current:
            decisions_by_target[str(current.target_id)] = current.decision
    if _latest_approvals_allow_dispatch(target_ids, decisions_by_target):
        intent.state = PublicationIntent.State.APPROVED
        intent.save(update_fields=["state"])
    elif intent.state == PublicationIntent.State.APPROVED:
        intent.state = PublicationIntent.State.AWAITING_APPROVAL
        intent.save(update_fields=["state"])

    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_approval.decided",
        entity=approval,
        identity_key=f"publication-approval:{approval.id}",
        before_material={
            "approval": None,
            "intent": intent_before_material,
        },
        after_material={
            "approval": _audit_state(
                approval,
                decision=approval.decision,
                approvalSubjectHash=approval.approval_subject_hash,
                decisionHash=approval.decision_hash,
                headVersion=approval.head_version,
            ),
            "intent": _audit_state(intent),
        },
        metadata={
            "approval_hash": approval.approval_subject_hash,
            "decision_hash": approval.decision_hash,
            "decision": approval.decision,
            "decision_id": str(approval.id),
            "intent_id": str(intent.id),
            "request_hash": request_hash,
            "target_id": str(target.id),
        },
    )
    return approval, True


def _publication_for(article_id: str, target: PublicationTarget) -> Publication:
    publication = (
        Publication.objects.select_for_update()
        .filter(article_id=article_id, target=target)
        .first()
    )
    if publication:
        return publication
    publication = Publication(
        article_id=article_id,
        target=target,
        origin_target_snapshot_id=target.current_snapshot_id,
        remote_lookup_key="pending",
    )
    publication.remote_lookup_key = f"ww-{publication.id.hex}"
    publication.save()
    return publication


def dispatch_publication(
    article_id: str,
    data: dict[str, Any],
    *,
    audit_context: AuditContext,
) -> list[PublicationAttempt]:
    try:
        return _dispatch_publication_atomic(
            article_id,
            data,
            audit_context=audit_context,
        )
    except _StaleIntentConflict as exc:
        _raise_persisted_stale_intent(exc)


@transaction.atomic
def _dispatch_publication_atomic(
    article_id: str,
    data: dict[str, Any],
    *,
    audit_context: AuditContext,
) -> list[PublicationAttempt]:
    if audit_context.actor_type not in {"admin", "worker"}:
        raise Forbidden("admin or worker audit provenance is required")
    if audit_context.actor_type == "admin" and (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden("dispatch provenance differs from the audit context")
    expected = _target_ref_map(data["expectedTargetSnapshots"])
    requested_ids = [str(value) for value in data["targetIds"]]
    if len(requested_ids) != len(set(requested_ids)):
        raise InvalidInput("target IDs에는 중복이 없어야 합니다.")
    if set(expected) != set(requested_ids):
        raise InvalidInput("target IDs와 expected target snapshot 집합이 같아야 합니다.")
    _lock_article_external_write_fence(article_id)
    _lock_target_intent_fences(requested_ids)
    intent_candidate = PublicationIntent.objects.select_related(
        "article_revision__generation_attempt"
    ).get(id=data["publicationIntentId"], article_id=article_id)
    _require_intent_revision_publishable(intent_candidate)
    intent = PublicationIntent.objects.select_for_update().get(
        id=data["publicationIntentId"], article_id=article_id
    )
    command_map = _command_map(intent.target_commands)
    intent_ref_map = _target_ref_map(intent.target_snapshot_refs)
    _require_exact_dispatch_targets(
        requested_ids=requested_ids,
        expected_refs=expected,
        intent_commands=command_map,
        intent_refs=intent_ref_map,
    )
    target_rows = {
        str(row.id): row
        for row in PublicationTarget.objects.select_for_update()
        .filter(id__in=requested_ids)
        .order_by("id")
    }
    if set(target_rows) != set(requested_ids):
        raise InvalidInput("알 수 없는 발행 target이 포함되었습니다.")
    if intent.revision_no != int(data["revisionNo"]):
        raise Conflict("발행 요청 revision과 intent가 다릅니다.")
    dispatch_material = {
        "requestKey": data["requestKey"],
        "publishAt": data.get("publishAt"),
    }
    dispatch_request_hash = _request_hash(
        {
            "schemaVersion": "publication-dispatch-request-v1",
            "intentId": str(intent.id),
            "actorType": audit_context.actor_type,
            "actorId": _id(audit_context.actor_id),
            "provenance": audit_context.provenance_metadata(),
            "reason": audit_context.reason_code,
            "payload": data,
        }
    )
    idempotency_keys = {
        target_id: sha256_hex(
            {
                "intentId": str(intent.id),
                "targetId": target_id,
                "action": command_map[target_id]["resolvedAction"],
                "requestKey": data["requestKey"],
            }
        )
        for target_id in requested_ids
        if target_id in command_map
    }
    replay_rows = list(
        PublicationAttempt.objects.select_related("publication__target").filter(
            idempotency_key__in=idempotency_keys.values()
        )
    )
    if replay_rows:
        replay_by_target = {str(row.publication.target_id): row for row in replay_rows}
        if (
            set(replay_by_target) != set(requested_ids)
            or intent.state != PublicationIntent.State.DISPATCHED
        ):
            raise Conflict("발행 dispatch request가 부분 적용되었거나 다른 payload입니다.")
        for target_id, row in replay_by_target.items():
            ref = expected[target_id]
            command = command_map.get(target_id)
            approval = (
                _require_current_dispatch_approval_locked(
                    intent=intent,
                    target=target_rows[target_id],
                    command=command,
                    frozen_ref=ref,
                )
                if command is not None
                else None
            )
            if (
                command is None
                or row.publication_intent_id != intent.id
                or approval is None
                or row.approval_id != approval.id
                or row.approval_subject_hash
                != approval.approval_subject_hash
                or row.resolved_action != command["resolvedAction"]
                or str(row.target_snapshot_id) != ref["targetSnapshotId"]
                or row.target_config_hash != ref["targetConfigHash"]
                or row.request_fingerprint
                != sha256_hex(
                    {
                        "intentHash": intent.intent_hash,
                        "command": command,
                        "approvalSubjectHash": row.approval_subject_hash,
                        "dispatch": dispatch_material,
                    }
                )
            ):
                raise Conflict("같은 request key가 다른 dispatch payload에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="publication.dispatched",
            entity=intent,
            identity_key=_publication_dispatch_audit_identity(
                intent_id=intent.id,
                request_key=data["requestKey"],
            ),
            request_hash=dispatch_request_hash,
        )
        _mark_origin_run_publishing_locked(intent)
        return [replay_by_target[target_id] for target_id in requested_ids]
    latest = PublicationIntent.objects.filter(article_id=article_id).order_by("-created_at").first()
    content_dispatch = _commands_require_publishable_revision(
        command_map[target_id] for target_id in requested_ids
    )
    if content_dispatch and (
        not latest
        or latest.id != intent.id
        or intent.state != PublicationIntent.State.APPROVED
    ):
        raise Conflict("current approved 발행 의도만 전송할 수 있습니다.")
    if not content_dispatch and intent.state not in {
        PublicationIntent.State.APPROVED,
        PublicationIntent.State.STALE,
    }:
        raise Conflict("withdrawal intent must be approved before dispatch")
    before_material = _audit_state(intent)
    attempts: list[PublicationAttempt] = []
    for target_id in requested_ids:
        target = target_rows.get(target_id)
        command = command_map.get(target_id)
        if not target or not command:
            raise InvalidInput("발행 의도에 없는 target입니다.")
        ref = expected[target_id]
        _require_current_dispatch_approval_locked(
            intent=intent,
            target=target,
            command=command,
            frozen_ref=ref,
        )
        if (
            str(target.current_snapshot_id) != ref["targetSnapshotId"]
            or target.current_config_hash != ref["targetConfigHash"]
            or str(command["targetSnapshotId"]) != ref["targetSnapshotId"]
        ):
            raise Conflict("target snapshot이 변경되어 재승인이 필요합니다.")
        approval = _latest_approval_locked(
            intent=intent,
            target_id=target.id,
        )
        if (
            not approval
            or approval.decision != Approval.Decision.APPROVED
            or approval.approval_subject_hash == ""
        ):
            raise Conflict("target별 current action 승인이 필요합니다.")
        if approval.target_action != command["resolvedAction"]:
            raise Conflict("승인 action과 target command가 다릅니다.")
        publication = _publication_for(article_id, target)
        idempotency_key = idempotency_keys[target_id]
        existing = PublicationAttempt.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            attempts.append(existing)
            continue
        activation_ref = next(
            (row for row in intent.auto_publish_activation_refs if str(row["targetId"]) == target_id), None
        )
        attempt = PublicationAttempt.objects.create(
            publication=publication,
            article_revision_id=intent.article_revision_id,
            publication_intent=intent,
            target_snapshot_id=target.current_snapshot_id,
            target_config_hash=target.current_config_hash,
            resolved_action=command["resolvedAction"],
            target_command_hash=command["targetCommandHash"],
            publisher_contract_version=target.publisher_contract_version,
            publisher_adapter_manifest_hash=target.publisher_adapter_manifest_hash,
            approval=approval,
            approval_subject_hash=approval.approval_subject_hash,
            auto_publish_activation_id=(activation_ref or {}).get("activationId"),
            auto_publish_activation_hash=(activation_ref or {}).get("activationHash"),
            idempotency_key=idempotency_key,
            remote_lookup_key=publication.remote_lookup_key,
            correlation_id=audit_context.correlation_id,
            recovery_state=PublicationRecoveryState.IN_PROGRESS,
            request_fingerprint=sha256_hex(
                {
                    "intentHash": intent.intent_hash,
                    "command": command,
                    "approvalSubjectHash": approval.approval_subject_hash,
                    "dispatch": dispatch_material,
                }
            ),
        )
        attempts.append(attempt)
    intent.state = PublicationIntent.State.DISPATCHED
    intent.save(update_fields=["state"])
    _mark_origin_run_publishing_locked(intent)
    publish_at = data.get("publishAt")
    wordpress = [row for row in attempts if row.publication.target.channel == ChannelCode.WORDPRESS]
    blogger = [row for row in attempts if row.publication.target.channel == ChannelCode.BLOGGER]
    initial = wordpress or [row for row in blogger if _wordpress_dependency_ready(row)]
    for attempt in initial:
        _queue_attempt_on_commit(attempt, publish_at=publish_at)
    attempts_manifest = [
        {
            "attemptId": str(row.id),
            "publicationId": str(row.publication_id),
            "targetId": str(row.publication.target_id),
            "state": row.state,
            "attemptNo": row.attempt_no,
            "resolvedAction": row.resolved_action,
        }
        for row in sorted(attempts, key=lambda item: str(item.id))
    ]
    attempts_hash = sha256_hex(attempts_manifest)
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication.dispatched",
        entity=intent,
        identity_key=_publication_dispatch_audit_identity(
            intent_id=intent.id,
            request_key=data["requestKey"],
        ),
        before_material={
            "intent": before_material,
            "attemptCount": 0,
            "attemptManifestHash": sha256_hex([]),
        },
        after_material={
            "intent": _audit_state(intent),
            "attemptCount": len(attempts_manifest),
            "attemptManifestHash": attempts_hash,
        },
        metadata={
            "intent_id": str(intent.id),
            "count": len(attempts),
            "request_hash": dispatch_request_hash,
            "result_hash": attempts_hash,
            "state": intent.state,
        },
    )
    return attempts


def _queue_attempt_on_commit(attempt: PublicationAttempt, *, publish_at: str | None = None) -> None:
    eta = None
    if publish_at:
        eta = timezone.datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
        if timezone.is_naive(eta):
            eta = timezone.make_aware(eta, dt_timezone.utc)
        attempt.publication.state = Publication.State.SCHEDULED
        attempt.publication.scheduled_for = eta
        attempt.publication.save(update_fields=["state", "scheduled_for", "updated_at"])
    _enqueue_event(
        "publication.requested",
        {
            "publication_attempt_id": str(attempt.id),
        },
        dedupe_key=f"publication.requested:{attempt.id}:{attempt.attempt_no}",
        aggregate_type="publication_attempt",
        aggregate_id=attempt.id,
        job_id=attempt.id,
        available_at=eta,
        correlation_id=attempt.correlation_id,
    )


def _canonical_dependency_target_id(attempt: PublicationAttempt) -> uuid.UUID:
    command = next(
        (
            row
            for row in attempt.publication_intent.target_commands
            if isinstance(row, dict)
            and str(row.get("targetId"))
            == str(attempt.publication.target.id)
        ),
        None,
    )
    try:
        dependency_id = uuid.UUID(
            str((command or {}).get("canonicalDependencyTargetId"))
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise Conflict(
            "Blogger publication has no exact WordPress dependency target"
        ) from exc
    return dependency_id


def _wordpress_dependency_queryset(attempt: PublicationAttempt):
    dependency_id = _canonical_dependency_target_id(attempt)
    return Publication.objects.filter(
        article_id=attempt.publication.article_id,
        target_id=dependency_id,
        target__channel=ChannelCode.WORDPRESS,
        state=Publication.State.PUBLISHED,
        canonical_ready_at__isnull=False,
    )


def _wordpress_dependency_ready(attempt: PublicationAttempt) -> bool:
    return _wordpress_dependency_queryset(attempt).exists()


def _kill_switch_enabled() -> bool:
    from apps.scheduling.controls import is_external_write_blocked

    return is_external_write_blocked()


def _validated_auto_live_eligible(
    *,
    intent: PublicationIntent,
    target: PublicationTarget,
    attempt: PublicationAttempt | None = None,
    using: str = "default",
) -> bool:
    if target.connection_state != PublicationTarget.ConnectionState.VERIFIED:
        return False
    if intent.approval_mode != ApprovalMode.VALIDATED_AUTO:
        return True
    if not target.auto_publish_enabled:
        return False
    activation_ref = next(
        (
            row
            for row in intent.auto_publish_activation_refs
            if isinstance(row, dict)
            and str(row.get("targetId")) == str(target.id)
        ),
        None,
    )
    if activation_ref is None:
        return False
    activation_id = activation_ref.get("activationId")
    if (
        str(target.latest_auto_publish_activation_id) != str(activation_id)
        or (
            attempt is not None
            and (
                str(attempt.auto_publish_activation_id) != str(activation_id)
                or attempt.auto_publish_activation_hash
                != activation_ref.get("activationHash")
            )
        )
    ):
        return False
    activation = (
        AutoPublishActivation.objects.using(using)
        .filter(
            id=activation_id,
            target_id=target.id,
            decision=AutoPublishActivation.Decision.ENABLED,
        )
        .first()
    )
    if (
        activation is None
        or activation.activation_hash != activation_ref.get("activationHash")
        or activation.version != int(activation_ref.get("version", 0))
        or str(activation.target_snapshot_id)
        != str(activation_ref.get("targetSnapshotId"))
        or str(target.current_snapshot_id)
        != str(activation_ref.get("targetSnapshotId"))
    ):
        return False
    validation_ids = [
        str(row.get("validationId"))
        for row in activation.validation_refs
        if isinstance(row, dict) and row.get("validationId")
    ]
    if len(validation_ids) != len(activation.validation_refs):
        return False
    if (
        AutoPublishValidation.objects.using(using)
        .filter(
            id__in=validation_ids,
            status=AutoPublishValidation.State.PASSED,
        )
        .count()
        != len(validation_ids)
    ):
        return False
    if target.environment == TargetEnvironment.PRODUCTION:
        if (
            not target.canary_target_id
            or target.canary_target.canary_state != ValidationState.PASSED
            or target.pilot_state != ValidationState.PASSED
        ):
            return False
    return True


def _require_attempt_origin_run_active(attempt: PublicationAttempt) -> None:
    run_id = attempt.publication_intent.origin_collection_run_id
    if run_id is None:
        raise Conflict("publication attempt has no frozen origin run")
    CollectionRun = apps.get_model("collection", "CollectionRun")
    run = CollectionRun.objects.filter(pk=run_id).first()
    if run is None:
        raise Conflict("publication attempt origin run is missing")
    if attempt.resolved_action in _CONTENT_PUBLICATION_ACTIONS and (
        run.state != "publishing" or run.stop_requested_at is not None
    ):
        raise Conflict("publication attempt origin run is not active")


def _assert_external_writes_allowed() -> None:
    if _kill_switch_enabled():
        raise PublisherError("global_kill_switch_enabled", category="retryable")


def validate_attempt_gate(attempt: PublicationAttempt) -> None:
    _require_attempt_origin_run_active(attempt)
    if _kill_switch_enabled():
        raise Conflict("전역 kill switch가 활성화되어 외부 쓰기가 차단되었습니다.")
    intent = attempt.publication_intent
    latest = PublicationIntent.objects.filter(article_id=intent.article_id).order_by("-created_at").first()
    content_attempt = attempt.resolved_action in _CONTENT_PUBLICATION_ACTIONS
    intent_state_allowed = intent.state in {
        PublicationIntent.State.APPROVED,
        PublicationIntent.State.DISPATCHED,
    }
    if not intent_state_allowed or (
        content_attempt and (not latest or latest.id != intent.id)
    ):
        raise Conflict("publication attempt가 current intent에 속하지 않습니다.")
    target = attempt.publication.target
    if (
        target.current_snapshot_id != attempt.target_snapshot_id
        or target.current_config_hash != attempt.target_config_hash
        or target.publisher_adapter_manifest_hash != attempt.publisher_adapter_manifest_hash
        or ADAPTER_MANIFESTS[target.channel] != attempt.publisher_adapter_manifest_hash
    ):
        raise Conflict("target 또는 publisher adapter snapshot이 변경되었습니다.")
    approval = attempt.approval
    command = next(
        (
            row
            for row in intent.target_commands
            if str(row.get("targetId")) == str(target.id)
        ),
        None,
    )
    latest_approval = _latest_approval_locked(
        intent=intent,
        target_id=target.id,
    )
    if (
        latest_approval is None
        or latest_approval.id != approval.id
        or approval.decision != Approval.Decision.APPROVED
        or approval.approval_subject_hash != attempt.approval_subject_hash
        or approval.target_action != attempt.resolved_action
        or command is None
        or not _approval_matches_frozen_subject(
            approval,
            intent=intent,
            target_id=target.id,
            command=command,
        )
    ):
        raise Conflict("현재 action-specific 승인과 attempt가 다릅니다.")
    if not _validated_auto_live_eligible(
        intent=intent,
        target=target,
        attempt=attempt,
    ):
        raise Conflict(
            "publication target or validated-auto material is no longer eligible"
        )
    if intent.approval_mode == ApprovalMode.VALIDATED_AUTO:
        if not target.auto_publish_enabled:
            raise Conflict("자동발행이 비활성화되었습니다.")
        if target.latest_auto_publish_activation_id != attempt.auto_publish_activation_id:
            raise Conflict("current 자동발행 activation과 attempt가 다릅니다.")
        activation = AutoPublishActivation.objects.get(id=attempt.auto_publish_activation_id)
        if activation.activation_hash != attempt.auto_publish_activation_hash:
            raise Conflict("자동발행 activation hash가 다릅니다.")
        validation_ids = [row["validationId"] for row in activation.validation_refs]
        if AutoPublishValidation.objects.filter(
            id__in=validation_ids, status=AutoPublishValidation.State.PASSED
        ).count() != len(validation_ids):
            raise Conflict("자동발행 validation이 stale 또는 revoked 상태입니다.")
    if target.connection_state != PublicationTarget.ConnectionState.VERIFIED:
        raise Conflict("검증된 target 연결만 발행할 수 있습니다.")
    if target.environment == TargetEnvironment.PRODUCTION and intent.approval_mode == ApprovalMode.VALIDATED_AUTO:
        if (
            not target.canary_target_id
            or target.canary_target.canary_state != ValidationState.PASSED
            or target.pilot_state != ValidationState.PASSED
        ):
            raise Conflict("현재 test canary와 운영 파일럿 게이트가 유효하지 않습니다.")
    if target.channel == ChannelCode.BLOGGER and attempt.resolved_action != PublicationAction.UNPUBLISH:
        if not _wordpress_dependency_ready(attempt):
            raise Conflict("WordPress 대표 원문의 공개 확인을 기다리고 있습니다.")


def _final_render(attempt: PublicationAttempt) -> ArticleChannelRender | None:
    if attempt.resolved_action == PublicationAction.UNPUBLISH:
        return None
    approval_render = attempt.approval.article_channel_render
    if not approval_render:
        raise Conflict("콘텐츠 action에는 승인된 preview render가 필요합니다.")
    target = attempt.publication.target
    body = approval_render.body_html
    canonical_url = None
    canonical_state = ArticleChannelRender.CanonicalState.NOT_APPLICABLE
    if target.channel == ChannelCode.BLOGGER:
        wordpress = _wordpress_dependency_queryset(attempt).first()
        if not wordpress or not wordpress.remote_url:
            raise Conflict("검증된 WordPress 대표 URL이 없습니다.")
        canonical_url = wordpress.remote_url
        canonical_state = ArticleChannelRender.CanonicalState.RESOLVED
        body = body.replace(
            "{{CANONICAL_WORDPRESS_URL}}",
            html.escape(canonical_url, quote=True),
        )
    expected_content_hash = sha256_hex(
        {"title": approval_render.title, "body": body}
    )
    existing = ArticleChannelRender.objects.filter(
        publication_intent=attempt.publication_intent,
        target=target,
        render_stage=ArticleChannelRender.Stage.FINAL,
    ).first()
    if existing:
        expected = {
            "publicationIntentId": str(attempt.publication_intent.id),
            "articleRevisionId": str(approval_render.article_revision_id),
            "targetId": str(target.id),
            "targetSnapshotId": str(approval_render.target_snapshot_id),
            "targetConfigHash": approval_render.target_config_hash,
            "channelRole": approval_render.channel_role,
            "renderStage": ArticleChannelRender.Stage.FINAL,
            "title": approval_render.title,
            "bodyHtml": body,
            "labels": approval_render.labels,
            "sourceLinks": approval_render.source_links,
            "includedClaimIds": approval_render.included_claim_ids,
            "canonicalSourceUrl": canonical_url,
            "canonicalLinkState": canonical_state,
            "templateHash": approval_render.template_hash,
            "contentHash": expected_content_hash,
            "sourceManifestHash": approval_render.source_manifest_hash,
            "mediaManifest": approval_render.media_manifest,
            "correctionHistory": approval_render.correction_history,
        }
        observed = {
            "publicationIntentId": str(existing.publication_intent_id),
            "articleRevisionId": str(existing.article_revision_id),
            "targetId": str(existing.target_id),
            "targetSnapshotId": str(existing.target_snapshot_id),
            "targetConfigHash": existing.target_config_hash,
            "channelRole": existing.channel_role,
            "renderStage": existing.render_stage,
            "title": existing.title,
            "bodyHtml": existing.body_html,
            "labels": existing.labels,
            "sourceLinks": existing.source_links,
            "includedClaimIds": existing.included_claim_ids,
            "canonicalSourceUrl": existing.canonical_source_url,
            "canonicalLinkState": existing.canonical_link_state,
            "templateHash": existing.template_hash,
            "contentHash": existing.content_hash,
            "sourceManifestHash": existing.source_manifest_hash,
            "mediaManifest": existing.media_manifest,
            "correctionHistory": existing.correction_history,
        }
        if observed != expected:
            raise Conflict("final render differs from the approved frozen material")
        return existing
    return ArticleChannelRender.objects.create(
        publication_intent=attempt.publication_intent,
        article_revision_id=approval_render.article_revision_id,
        target=target,
        target_snapshot=approval_render.target_snapshot,
        target_config_hash=approval_render.target_config_hash,
        channel_role=approval_render.channel_role,
        render_stage=ArticleChannelRender.Stage.FINAL,
        title=approval_render.title,
        body_html=body,
        labels=approval_render.labels,
        source_links=approval_render.source_links,
        included_claim_ids=approval_render.included_claim_ids,
        canonical_source_url=canonical_url,
        canonical_link_state=canonical_state,
        template_hash=approval_render.template_hash,
        content_hash=expected_content_hash,
        source_manifest_hash=approval_render.source_manifest_hash,
        media_manifest=approval_render.media_manifest,
        correction_history=approval_render.correction_history,
    )


def _rendered_article(render: ArticleChannelRender) -> RenderedArticle:
    media = tuple(
        RenderedMedia(
            asset_id=str(row["assetId"]),
            delivery_kind=row["deliveryKind"],
            delivery_id=str(row["deliveryId"]),
            delivery_url=row["deliveryUrl"],
            mime_type=row["mimeType"],
            checksum=row["checksum"],
            alt_text=row["altText"],
            caption=row.get("caption", ""),
            attribution=row.get("attribution", ""),
            rights_status=row["rightsStatus"],
        )
        for row in render.media_manifest
    )
    return RenderedArticle(
        article_id=str(render.publication_intent.article_id),
        revision_no=render.publication_intent.revision_no,
        channel_role=render.channel_role,
        render_stage=render.render_stage,
        title=render.title,
        body_html=render.body_html,
        labels=tuple(render.labels),
        source_links=tuple(render.source_links),
        included_claim_ids=tuple(str(value) for value in render.included_claim_ids),
        canonical_source_url=render.canonical_source_url,
        canonical_link_state=render.canonical_link_state,
        template_hash=render.template_hash,
        media=media,
        correction_history=tuple(render.correction_history),
        content_hash=render.content_hash,
        source_manifest_hash=render.source_manifest_hash,
    )


def _command_for_attempt(attempt: PublicationAttempt, render: ArticleChannelRender | None) -> PublishCommand:
    return PublishCommand(
        publication_attempt_id=str(attempt.id),
        action=attempt.resolved_action,
        target_command_hash=attempt.target_command_hash,
        idempotency_key=attempt.idempotency_key,
        remote_lookup_key=attempt.remote_lookup_key,
        target_id=str(attempt.publication.target_id),
        publication_intent_id=str(attempt.publication_intent_id),
        approval_id=str(attempt.approval_id),
        approval_subject_hash=attempt.approval_subject_hash,
        target_snapshot_id=str(attempt.target_snapshot_id),
        target_config_hash=attempt.target_config_hash,
        publisher_contract_version=attempt.publisher_contract_version,
        publisher_adapter_manifest_hash=attempt.publisher_adapter_manifest_hash,
        auto_publish_activation_id=_id(attempt.auto_publish_activation_id),
        auto_publish_activation_hash=attempt.auto_publish_activation_hash,
        remote_post_id=attempt.publication.remote_post_id,
        rendered_article=_rendered_article(render) if render else None,
        publish_at=attempt.publication.scheduled_for,
        requested_at=timezone.now(),
        correlation_id=str(attempt.correlation_id),
    )


def _current_worker_task_id() -> str:
    from wisdome_writer.observability import current_task_id

    return (current_task_id() or "")[:255]


def _observation_duration_ms(
    started_at,
    finished_at,
) -> int | None:
    if (
        started_at is None
        or finished_at is None
        or finished_at < started_at
    ):
        return None
    return int((finished_at - started_at).total_seconds() * 1000)


def _publication_recovery_state(state: str) -> str:
    if state == PublicationAttempt.State.SUCCEEDED:
        return PublicationRecoveryState.NOT_REQUIRED
    if state in {
        PublicationAttempt.State.QUEUED,
        PublicationAttempt.State.RUNNING,
    }:
        return PublicationRecoveryState.IN_PROGRESS
    if state == PublicationAttempt.State.RETRYABLE_FAILED:
        return PublicationRecoveryState.AUTOMATIC_RETRY
    if state in {
        PublicationAttempt.State.UNKNOWN_OUTCOME,
        PublicationAttempt.State.RECONCILING,
    }:
        return PublicationRecoveryState.RECONCILING
    if state in {
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
    }:
        return PublicationRecoveryState.MANUAL_REQUIRED
    return PublicationRecoveryState.STOPPED


def _publication_terminal_impact(
    *,
    stage: str,
    final_state: str,
    error_code: str,
    scope: str = "publication_channel",
) -> dict[str, Any]:
    return {
        "scope": scope,
        "stage": stage,
        "final_state": final_state,
        "affected_count": 1,
        "error_code": (error_code or "")[:100],
    }


def _complete_execution_observation_locked(
    attempt: PublicationAttempt,
    *,
    execution_attempt_no: int,
    finished_at,
    result_state: str,
    error_code: str,
    retry_at=None,
    recovery_state: str,
    terminal_state: str | None = None,
) -> PublicationExecutionObservation:
    observation = (
        PublicationExecutionObservation.objects.select_for_update()
        .filter(
            publication_attempt=attempt,
            execution_attempt_no=execution_attempt_no,
        )
        .first()
    )
    if observation is None:
        raise Conflict("active publication execution observation is missing")
    if observation.finished_at is not None:
        return observation
    duration_ms = _observation_duration_ms(
        observation.started_at,
        finished_at,
    )
    terminal_impact = _publication_terminal_impact(
        stage="execution",
        final_state=terminal_state or result_state,
        error_code=error_code,
    )
    observation.finished_at = finished_at
    observation.duration_ms = duration_ms
    observation.result_state = result_state
    observation.error_code = (error_code or "")[:100]
    observation.retry_at = retry_at
    observation.terminal_impact = terminal_impact
    observation.recovery_state = recovery_state
    observation.save(
        update_fields=(
            "finished_at",
            "duration_ms",
            "result_state",
            "error_code",
            "retry_at",
            "terminal_impact",
            "recovery_state",
        )
    )
    if duration_ms is not None:
        attempt.duration_ms = (attempt.duration_ms or 0) + duration_ms
    if execution_attempt_no > 1:
        attempt.retry_count += 1
    attempt.terminal_impact = terminal_impact
    return observation


def _complete_reconcile_observation_locked(
    attempt: PublicationAttempt,
    generation: PublicationReconcileGeneration,
    *,
    finished_at,
    result_state: str,
    error_code: str,
    next_recovery_at=None,
    recovery_state: str,
) -> None:
    if generation.state == PublicationReconcileGeneration.State.COMPLETED:
        return
    duration_ms = _observation_duration_ms(
        generation.started_at,
        finished_at,
    )
    terminal_impact = _publication_terminal_impact(
        stage="reconcile",
        final_state=result_state,
        error_code=error_code,
    )
    generation.state = PublicationReconcileGeneration.State.COMPLETED
    generation.result_state = result_state
    generation.completed_at = finished_at
    generation.duration_ms = duration_ms
    generation.error_code = (error_code or "")[:100]
    generation.terminal_impact = terminal_impact
    generation.recovery_state = recovery_state
    generation.next_recovery_at = next_recovery_at
    if duration_ms is not None:
        attempt.duration_ms = (attempt.duration_ms or 0) + duration_ms
    attempt.retry_count += 1
    attempt.terminal_impact = terminal_impact


def _project_reconciling_locked(
    attempt: PublicationAttempt,
    *,
    update_counter: bool = False,
    recovery_state: str = PublicationRecoveryState.AUTOMATIC_RETRY,
) -> None:
    terminal_states = {
        PublicationAttempt.State.SUCCEEDED,
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
        PublicationAttempt.State.STALE,
    }
    if attempt.state in terminal_states:
        return
    attempt.state = PublicationAttempt.State.RECONCILING
    attempt.recovery_state = recovery_state
    attempt_fields = ["state", "recovery_state", "next_recovery_at"]
    if update_counter:
        attempt_fields.append("reconcile_attempt_no")
    attempt.save(update_fields=attempt_fields)
    publication = attempt.publication
    publication.state = Publication.State.RECONCILING
    publication.save(update_fields=("state", "updated_at"))


def _manualize_reconcile_attempt_locked(
    attempt: PublicationAttempt,
    *,
    error_code: str,
    now=None,
) -> str:
    now = now or timezone.now()
    preserved_terminal_states = {
        PublicationAttempt.State.SUCCEEDED,
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
        PublicationAttempt.State.STALE,
    }
    if attempt.state in preserved_terminal_states:
        return attempt.state
    attempt.state = PublicationAttempt.State.MANUAL_REQUIRED
    attempt.error_code = error_code[:100]
    attempt.finished_at = now
    attempt.recovery_state = PublicationRecoveryState.MANUAL_REQUIRED
    attempt.next_recovery_at = None
    attempt.terminal_impact = _publication_terminal_impact(
        stage="reconcile",
        final_state=attempt.state,
        error_code=attempt.error_code,
    )
    attempt.save(
        update_fields=(
            "state",
            "error_code",
            "finished_at",
            "recovery_state",
            "next_recovery_at",
            "terminal_impact",
        )
    )
    attempt.publication.state = Publication.State.MANUAL_REQUIRED
    attempt.publication.last_error_code = attempt.error_code
    attempt.publication.save(
        update_fields=(
            "state",
            "last_error_code",
            "updated_at",
        )
    )
    _release_article_external_write_fence_locked(attempt)
    return attempt.state


def _terminalize_reconcile_generation_locked(
    attempt: PublicationAttempt,
    generation: PublicationReconcileGeneration,
    *,
    error_code: str,
) -> None:
    if generation.state == PublicationReconcileGeneration.State.COMPLETED:
        return
    now = timezone.now()
    preserved_before = attempt.state in {
        PublicationAttempt.State.SUCCEEDED,
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
        PublicationAttempt.State.STALE,
    }
    _manualize_reconcile_attempt_locked(
        attempt,
        error_code=error_code,
        now=now,
    )
    terminal_impact = _publication_terminal_impact(
        scope="publication_delivery",
        stage="reconcile_delivery",
        final_state="delivery_failed",
        error_code=error_code,
    )
    # A terminal callback means the reconcile message was never executed by the
    # publisher. Close the delivery generation without inventing a channel
    # result, duration, or retry.
    generation.state = PublicationReconcileGeneration.State.COMPLETED
    generation.result_identity = ""
    generation.result_state = ""
    generation.completed_at = now
    generation.duration_ms = None
    generation.error_code = error_code[:100]
    generation.terminal_impact = terminal_impact
    generation.recovery_state = (
        PublicationRecoveryState.MANUAL_REQUIRED
        if attempt.state == PublicationAttempt.State.MANUAL_REQUIRED
        else _publication_recovery_state(attempt.state)
    )
    generation.next_recovery_at = None
    generation.save(
        update_fields=(
            "state",
            "result_identity",
            "result_state",
            "completed_at",
            "duration_ms",
            "error_code",
            "terminal_impact",
            "recovery_state",
            "next_recovery_at",
        )
    )
    if not preserved_before or attempt.state == PublicationAttempt.State.MANUAL_REQUIRED:
        attempt.terminal_impact = terminal_impact
        attempt.save(update_fields=("terminal_impact",))


def _reconcile_generation_delivery_dead_lettered(
    generation: PublicationReconcileGeneration,
) -> bool:
    source_event = generation.source_event
    return (
        source_event.status == "dead_letter"
        or source_event.consumer_receipts.filter(
            consumer_name="publication-reconcile",
            state="dead_letter",
        ).exists()
    )


def _enqueue_reconcile_locked(
    attempt: PublicationAttempt,
    *,
    available_at=None,
) -> PublicationReconcileGeneration | None:
    if attempt.reconcile_attempt_no:
        current = (
            PublicationReconcileGeneration.objects.select_related(
                "source_event"
            ).filter(
                publication_attempt=attempt,
                generation=attempt.reconcile_attempt_no,
            )
            .order_by("generation")
            .first()
        )
        if (
            current is not None
            and current.state == PublicationReconcileGeneration.State.STARTED
        ):
            if _reconcile_generation_delivery_dead_lettered(current):
                _terminalize_reconcile_generation_locked(
                    attempt,
                    current,
                    error_code=(
                        current.source_event.last_error_code
                        or "reconcile_delivery_dead_letter"
                    ),
                )
            else:
                active = bool(current.worker_task_id)
                attempt.next_recovery_at = (
                    None if active else current.not_before
                )
                _project_reconciling_locked(
                    attempt,
                    recovery_state=(
                        PublicationRecoveryState.RECONCILING
                        if active
                        else PublicationRecoveryState.AUTOMATIC_RETRY
                    ),
                )
            return current
    if attempt.state in {
        PublicationAttempt.State.SUCCEEDED,
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
        PublicationAttempt.State.STALE,
    }:
        return None
    reconcile_attempt_no = attempt.reconcile_attempt_no + 1
    if reconcile_attempt_no > 5:
        return None
    dedupe_key = (
        f"publication.reconcile_requested:{attempt.id}:{reconcile_attempt_no}"
    )
    event = _enqueue_event(
        "publication.reconcile_requested",
        {
            "publication_attempt_id": str(attempt.id),
            "reconcile_attempt_no": reconcile_attempt_no,
        },
        event_version=2,
        dedupe_key=dedupe_key,
        aggregate_type="publication_attempt",
        aggregate_id=attempt.id,
        job_id=attempt.id,
        available_at=available_at,
        correlation_id=attempt.correlation_id,
    )
    generation, created = PublicationReconcileGeneration.objects.get_or_create(
        publication_attempt=attempt,
        generation=reconcile_attempt_no,
        defaults={
            "source_event": event,
            "state": PublicationReconcileGeneration.State.STARTED,
            "not_before": event.not_before,
            "started_at": event.occurred_at,
            "correlation_id": event.correlation_id,
            "recovery_state": PublicationRecoveryState.AUTOMATIC_RETRY,
            "next_recovery_at": event.not_before,
        },
    )
    if not created and generation.source_event_id != event.id:
        raise Conflict("reconcile generation is bound to a different event")
    attempt.reconcile_attempt_no = reconcile_attempt_no
    attempt.next_recovery_at = event.not_before
    _project_reconciling_locked(attempt, update_counter=True)
    return generation


@transaction.atomic
def begin_attempt(
    attempt_id: str,
    *,
    audit_context: AuditContext,
) -> tuple[PublicationAttempt, PublishCommand | None]:
    _require_audit_actor(audit_context, "worker")
    preliminary = PublicationAttempt.objects.select_related(
        "publication__target",
        "publication_intent__article_revision__generation_attempt",
        "approval",
    ).get(id=attempt_id)
    _require_worker_event(
        audit_context,
        topic="publication.requested",
        aggregate_id=preliminary.id,
        payload_identity={"publication_attempt_id": str(preliminary.id)},
    )
    _lock_article_external_write_fence(
        preliminary.publication_intent.article_id
    )
    _lock_target_intent_fences(
        _intent_target_ids(preliminary.publication_intent)
    )
    if preliminary.state in {
        PublicationAttempt.State.QUEUED,
        PublicationAttempt.State.RETRYABLE_FAILED,
    }:
        try:
            intent = _require_intent_revision_publishable(
                preliminary.publication_intent
            )
        except _StaleIntentConflict:
            intent = PublicationIntent.objects.select_for_update().get(
                id=preliminary.publication_intent_id
            )
            if _intent_state_can_be_marked_stale(intent.state):
                intent.state = PublicationIntent.State.STALE
                intent.save(update_fields=["state"])
            PublicationTarget.objects.select_for_update().get(
                id=preliminary.publication.target_id
            )
            Approval.objects.select_for_update().get(id=preliminary.approval_id)
            Publication.objects.select_for_update().get(
                id=preliminary.publication_id
            )
            attempt = PublicationAttempt.objects.select_for_update().get(
                id=preliminary.id
            )
            if attempt.state in {
                PublicationAttempt.State.QUEUED,
                PublicationAttempt.State.RETRYABLE_FAILED,
            }:
                before_material = _audit_state(attempt)
                attempt.state = PublicationAttempt.State.STALE
                attempt.finished_at = timezone.now()
                attempt.error_code = "editorial_eligibility_stale"
                attempt.terminal_impact = _publication_terminal_impact(
                    stage="execution_gate",
                    final_state=attempt.state,
                    error_code=attempt.error_code,
                )
                attempt.recovery_state = PublicationRecoveryState.STOPPED
                attempt.next_recovery_at = None
                attempt.save(
                    update_fields=[
                        "state",
                        "finished_at",
                        "error_code",
                        "terminal_impact",
                        "recovery_state",
                        "next_recovery_at",
                    ]
                )
                _release_article_external_write_fence_locked(attempt)
                _record_publishing_audit(
                    audit_context=audit_context,
                    action="publication_attempt.finished",
                    entity=attempt,
                    identity_key=(
                        f"{audit_context.event_key}:attempt-result:"
                        f"{attempt.attempt_no}"
                    ),
                    before_material=before_material,
                    after_material=_audit_state(attempt),
                    metadata={
                        "publication_attempt_id": str(attempt.id),
                        "attempt": attempt.attempt_no,
                        "result": "stale",
                        "error_code": attempt.error_code,
                        "state": attempt.state,
                    },
                )
            return attempt, None
        intent = PublicationIntent.objects.select_for_update().get(id=intent.id)
        PublicationTarget.objects.select_for_update().get(
            id=preliminary.publication.target_id
        )
        Approval.objects.select_for_update().get(id=preliminary.approval_id)
        Publication.objects.select_for_update().get(id=preliminary.publication_id)
        PublicationAttempt.objects.select_for_update().get(id=preliminary.id)
    else:
        _lock_publication_attempt_domain(preliminary)
    return _begin_attempt_locked(attempt_id, audit_context=audit_context)


@transaction.atomic
def _begin_attempt_locked(
    attempt_id: str,
    *,
    audit_context: AuditContext,
) -> tuple[PublicationAttempt, PublishCommand | None]:
    _require_audit_actor(audit_context, "worker")
    attempt = PublicationAttempt.objects.select_for_update().select_related(
        "publication__target", "publication_intent", "approval__article_channel_render"
    ).get(id=attempt_id)
    publication = attempt.publication
    source_event = _require_worker_event(
        audit_context,
        topic="publication.requested",
        aggregate_id=attempt.id,
        payload_identity={"publication_attempt_id": str(attempt.id)},
    )
    if attempt.state in {
        PublicationAttempt.State.SUCCEEDED,
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
        PublicationAttempt.State.STALE,
    }:
        replay = _worker_audit_replay(
            audit_context,
            entity=attempt,
            candidates=(
                (
                    "publication_attempt.finished",
                    (
                        f"{audit_context.event_key}:attempt-result:"
                        f"{attempt.attempt_no}"
                    ),
                    {"attempt": attempt.attempt_no},
                ),
                (
                    "publication_attempt.reconcile_started",
                    (
                        f"{audit_context.event_key}:delivery-redelivery:"
                        f"{attempt.attempt_no}"
                    ),
                    {"attempt": attempt.attempt_no},
                ),
            ),
        )
        if replay is None:
            _converge_revoked_attempt_redelivery_locked(
                attempt,
                audit_context=audit_context,
            )
        return attempt, None
    if attempt.state == PublicationAttempt.State.RUNNING:
        if not _worker_audit_replay(
            audit_context,
            entity=attempt,
            candidates=(
                (
                    "publication_attempt.started",
                    (
                        f"{audit_context.event_key}:attempt-start:"
                        f"{attempt.attempt_no}"
                    ),
                    {"attempt": attempt.attempt_no},
                ),
            ),
        ):
            raise Conflict(
                "running publication attempt has no matching started audit"
            )
        before_material = {
            "attempt": _audit_state(attempt),
            "publication": _audit_state(publication),
        }
        now = timezone.now()
        attempt.state = PublicationAttempt.State.UNKNOWN_OUTCOME
        attempt.finished_at = now
        attempt.error_code = "delivery_redelivered_after_begin"
        attempt.save(update_fields=["state", "finished_at", "error_code"])
        publication.state = Publication.State.RECONCILING
        publication.remote_state = Publication.RemoteState.UNKNOWN
        publication.last_error_code = attempt.error_code
        publication.save(
            update_fields=(
                "state",
                "remote_state",
                "last_error_code",
                "updated_at",
            )
        )
        generation = _enqueue_reconcile_locked(attempt)
        _complete_execution_observation_locked(
            attempt,
            execution_attempt_no=attempt.attempt_no,
            finished_at=now,
            result_state=PublicationAttempt.State.UNKNOWN_OUTCOME,
            error_code=attempt.error_code,
            retry_at=(
                generation.not_before if generation is not None else None
            ),
            recovery_state=attempt.recovery_state,
            terminal_state=PublicationAttempt.State.UNKNOWN_OUTCOME,
        )
        attempt.save(
            update_fields=(
                "duration_ms",
                "retry_count",
                "terminal_impact",
                "recovery_state",
                "next_recovery_at",
            )
        )
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_attempt.reconcile_started",
            entity=attempt,
            identity_key=(
                f"{audit_context.event_key}:delivery-redelivery:"
                f"{attempt.attempt_no}"
            ),
            before_material=before_material,
            after_material={
                "attempt": _audit_state(attempt),
                "publication": _audit_state(publication),
            },
            metadata={
                "publication_attempt_id": str(attempt.id),
                "attempt": attempt.attempt_no,
                "result": "unknown_outcome",
                "error_code": attempt.error_code,
                "state": attempt.state,
            },
        )
        return attempt, None
    if attempt.state in {
        PublicationAttempt.State.RECONCILING,
        PublicationAttempt.State.UNKNOWN_OUTCOME,
    }:
        if not _worker_audit_replay(
            audit_context,
            entity=attempt,
            candidates=(
                (
                    "publication_attempt.finished",
                    (
                        f"{audit_context.event_key}:attempt-result:"
                        f"{attempt.attempt_no}"
                    ),
                    {"attempt": attempt.attempt_no},
                ),
                (
                    "publication_attempt.reconcile_started",
                    (
                        f"{audit_context.event_key}:delivery-redelivery:"
                        f"{attempt.attempt_no}"
                    ),
                    {"attempt": attempt.attempt_no},
                ),
            ),
        ):
            raise Conflict(
                "reconciling publication attempt has no matching audit event"
            )
        _enqueue_reconcile_locked(attempt)
        return attempt, None
    if attempt.state not in {
        PublicationAttempt.State.QUEUED,
        PublicationAttempt.State.RETRYABLE_FAILED,
    }:
        raise Conflict("terminal publication attempt cannot be executed again")
    try:
        validate_attempt_gate(attempt)
    except Conflict:
        before_material = _audit_state(attempt)
        attempt.state = PublicationAttempt.State.STALE
        attempt.finished_at = timezone.now()
        attempt.error_code = "attempt_gate_stale"
        attempt.terminal_impact = _publication_terminal_impact(
            stage="execution_gate",
            final_state=attempt.state,
            error_code=attempt.error_code,
        )
        attempt.recovery_state = PublicationRecoveryState.STOPPED
        attempt.next_recovery_at = None
        attempt.save(
            update_fields=[
                "state",
                "finished_at",
                "error_code",
                "terminal_impact",
                "recovery_state",
                "next_recovery_at",
            ]
        )
        _release_article_external_write_fence_locked(attempt)
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_attempt.finished",
            entity=attempt,
            identity_key=(
                f"{audit_context.event_key}:attempt-result:"
                f"{attempt.attempt_no}"
            ),
            before_material=before_material,
            after_material=_audit_state(attempt),
            metadata={
                "publication_attempt_id": str(attempt.id),
                "attempt": attempt.attempt_no,
                "result": "stale",
                "error_code": attempt.error_code,
                "state": attempt.state,
            },
        )
        return attempt, None
    render = _final_render(attempt)
    before_material = {
        "attempt": _audit_state(attempt),
        "publication": _audit_state(publication),
    }
    _set_article_external_write_fence_locked(attempt)
    attempt.state = PublicationAttempt.State.RUNNING
    attempt.started_at = timezone.now()
    attempt.finished_at = None
    attempt.error_code = ""
    attempt.recovery_state = PublicationRecoveryState.IN_PROGRESS
    attempt.next_recovery_at = None
    attempt.save(
        update_fields=[
            "state",
            "started_at",
            "finished_at",
            "error_code",
            "recovery_state",
            "next_recovery_at",
        ]
    )
    observation, created = PublicationExecutionObservation.objects.get_or_create(
        publication_attempt=attempt,
        execution_attempt_no=attempt.attempt_no,
        defaults={
            "correlation_id": source_event.correlation_id,
            "source_event": source_event,
            "worker_task_id": _current_worker_task_id(),
            "started_at": attempt.started_at,
            "recovery_state": PublicationRecoveryState.IN_PROGRESS,
        },
    )
    if (
        not created
        and (
            observation.finished_at is not None
            or observation.source_event_id != source_event.id
        )
    ):
        raise Conflict("publication execution observation fence conflicts")
    publication.state = {
        PublicationAction.CREATE: Publication.State.IN_PROGRESS,
        PublicationAction.UPDATE: Publication.State.UPDATING,
        PublicationAction.MARK_WITHDRAWN: Publication.State.MARKING_WITHDRAWN,
        PublicationAction.UNPUBLISH: Publication.State.WITHDRAWING,
    }[attempt.resolved_action]
    publication.save(update_fields=["state", "updated_at"])
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_attempt.started",
        entity=attempt,
        identity_key=(
            f"{audit_context.event_key}:attempt-start:"
            f"{attempt.attempt_no}"
        ),
        before_material=before_material,
        after_material={
            "attempt": _audit_state(attempt),
            "publication": _audit_state(publication),
        },
        metadata={
            "publication_attempt_id": str(attempt.id),
            "attempt": attempt.attempt_no,
            "action": attempt.resolved_action,
            "channel": publication.target.channel,
            "intent_id": str(attempt.publication_intent_id),
            "state": attempt.state,
            "target_id": str(publication.target_id),
        },
    )
    return attempt, _command_for_attempt(attempt, render)


def _publish_result_identity(result) -> str:
    return sha256_hex(
        {
            "status": result.status,
            "remote_post_id": result.remote_post_id,
            "remote_url": result.remote_url,
            "remote_state": result.remote_state,
            "remote_revision": result.remote_revision,
            "scheduled_for": (
                result.scheduled_for.isoformat()
                if result.scheduled_for
                else None
            ),
            "published_at": (
                result.published_at.isoformat()
                if result.published_at
                else None
            ),
            "request_id": result.request_id,
            "reconcile_required": result.reconcile_required,
            "http_status": result.http_status,
            "error_code": result.error_code,
            "error_detail_redacted": result.error_detail_redacted,
        }
    )


@transaction.atomic
def persist_publish_result(
    attempt_id: str,
    result,
    *,
    audit_context: AuditContext,
    expected_reconcile_generation: int | None = None,
    expected_reconcile_event_id: uuid.UUID | str | None = None,
    retry_after_seconds: int | None = None,
) -> PublicationAttempt:
    _require_audit_actor(audit_context, "worker")
    if (
        expected_reconcile_generation is None
    ) != (
        expected_reconcile_event_id is None
    ):
        raise Conflict("reconcile result fence is incomplete")
    expected_event_uuid = None
    if expected_reconcile_generation is None:
        _require_worker_event(
            audit_context,
            topic="publication.requested",
            aggregate_id=attempt_id,
            payload_identity={"publication_attempt_id": str(attempt_id)},
        )
    else:
        try:
            expected_event_uuid = uuid.UUID(
                str(expected_reconcile_event_id)
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise Conflict("reconcile result event fence is invalid") from exc
        if str(expected_event_uuid) != audit_context.event_key:
            raise Conflict(
                "reconcile result event fence differs from audit provenance"
            )
        source_event = _require_worker_event(
            audit_context,
            topic="publication.reconcile_requested",
            aggregate_id=attempt_id,
            payload_identity={
                "publication_attempt_id": str(attempt_id),
            },
        )
        source_payload = (
            source_event.payload
            if isinstance(source_event.payload, dict)
            else {}
        )
        if source_event.event_version == 2:
            if (
                source_payload.get("reconcile_attempt_no")
                != expected_reconcile_generation
            ):
                raise Conflict(
                    "reconcile result generation differs from the worker event"
                )
        elif source_event.event_version != 1:
            raise Conflict("unsupported reconcile event version")
    preliminary = PublicationAttempt.objects.select_related(
        "publication_intent"
    ).get(id=attempt_id)
    _lock_article_external_write_fence(
        preliminary.publication_intent.article_id
    )
    _lock_target_intent_fences(
        _intent_target_ids(preliminary.publication_intent)
    )
    attempt = _lock_publication_attempt_domain(preliminary)
    publication = attempt.publication
    target = publication.target
    before_material = {
        "attempt": _audit_state(attempt),
        "publication": _audit_state(publication),
        "target": _audit_state(target),
        "media": _publication_media_state_manifest(publication.id),
    }
    result_identity = _publish_result_identity(result)
    execution_attempt_no = attempt.attempt_no
    replay_attempt_no = (
        execution_attempt_no - 1
        if (
            expected_reconcile_generation is None
            and attempt.state == PublicationAttempt.State.RETRYABLE_FAILED
            and execution_attempt_no > 1
        )
        else execution_attempt_no
    )
    replay_action = (
        "publication_attempt.reconciled"
        if expected_reconcile_generation is not None
        else "publication_attempt.finished"
    )
    replay_candidates = (
        (
            replay_action,
            (
                f"{audit_context.event_key}:reconcile-result:"
                f"{expected_reconcile_generation}"
            ),
            {
                "reconcile_attempt_no": expected_reconcile_generation,
            },
        ),
    ) if expected_reconcile_generation is not None else (
        (
            replay_action,
            (
                f"{audit_context.event_key}:attempt-result:"
                f"{replay_attempt_no}"
            ),
            {"attempt": replay_attempt_no},
        ),
    )
    replay = _worker_audit_replay(
        audit_context,
        entity=attempt,
        candidates=replay_candidates,
    )
    if replay is not None:
        if replay.metadata_redacted.get("result_hash") != result_identity:
            raise Conflict(
                "worker event was replayed with a different publisher result"
            )
        return attempt
    reconcile_generation = None
    if expected_reconcile_generation is not None:
        reconcile_generation = (
            PublicationReconcileGeneration.objects.select_for_update()
            .filter(
                publication_attempt=attempt,
                generation=expected_reconcile_generation,
                source_event_id=expected_event_uuid,
            )
            .first()
        )
        if (
            reconcile_generation is None
            or reconcile_generation.state
            != PublicationReconcileGeneration.State.STARTED
            or attempt.reconcile_attempt_no
            != expected_reconcile_generation
            or attempt.state != PublicationAttempt.State.RECONCILING
        ):
            raise Conflict("stale reconcile result was fenced")
    elif attempt.state != PublicationAttempt.State.RUNNING:
        raise Conflict(
            "publication result can only finalize the active worker attempt"
        )
    validated_remote_url = (
        _validated_remote_url(result.remote_url)
        if result.status == "succeeded"
        else None
    )
    now = timezone.now()
    attempt.http_status = result.http_status
    attempt.remote_request_id = result.request_id or ""
    attempt.finished_at = now
    attempt.error_code = result.error_code or ""
    attempt.error_detail_redacted = result.error_detail_redacted or ""
    if result.status == "succeeded":
        attempt.state = PublicationAttempt.State.SUCCEEDED
        publication.remote_post_id = result.remote_post_id or publication.remote_post_id
        publication.remote_url = validated_remote_url or publication.remote_url
        publication.remote_state = result.remote_state
        publication.published_revision_no = attempt.publication_intent.revision_no
        publication.last_success_at = now
        publication.last_error_code = ""
        if attempt.resolved_action == PublicationAction.UNPUBLISH:
            publication.state = Publication.State.WITHDRAWN
        elif attempt.resolved_action == PublicationAction.MARK_WITHDRAWN:
            publication.state = Publication.State.MARKED_WITHDRAWN
            publication.published_at = result.published_at or publication.published_at
        else:
            publication.state = Publication.State.PUBLISHED
            publication.published_at = result.published_at or now
        if publication.target.channel == ChannelCode.WORDPRESS and publication.state == Publication.State.PUBLISHED:
            publication.canonical_ready_at = now
        if publication.target.channel == ChannelCode.BLOGGER:
            wordpress = Publication.objects.filter(
                article_id=publication.article_id,
                target__channel=ChannelCode.WORDPRESS,
                state=Publication.State.PUBLISHED,
            ).first()
            publication.canonical_source_url = wordpress.remote_url if wordpress else None
        PublicationMedia.objects.filter(
            publication=publication,
            article_revision_id=attempt.article_revision_id,
            binding_state=PublicationMedia.BindingState.PREPARED,
        ).update(binding_state=PublicationMedia.BindingState.ACTIVE, remote_verified_at=now)
        if (
            target.environment == TargetEnvironment.PRODUCTION
            and attempt.publication_intent.approval_mode == ApprovalMode.MANUAL
            and publication.state in {Publication.State.PUBLISHED, Publication.State.MARKED_WITHDRAWN}
            and target.pilot_state != ValidationState.PASSED
        ):
            target.pilot_state = ValidationState.PASSED
            target.last_pilot_at = now
            target.save(update_fields=["pilot_state", "last_pilot_at", "updated_at"])
            _snapshot_locked(target)
    elif result.status == "unknown_outcome":
        attempt.state = PublicationAttempt.State.UNKNOWN_OUTCOME
        publication.state = Publication.State.RECONCILING
        publication.remote_state = Publication.RemoteState.UNKNOWN
    elif result.status == "retryable_failed":
        attempt.state = PublicationAttempt.State.RETRYABLE_FAILED
        publication.state = Publication.State.RETRYABLE_FAILED
        publication.last_error_code = result.error_code or "publisher_retryable"
    elif result.status == "manual_required":
        attempt.state = PublicationAttempt.State.MANUAL_REQUIRED
        publication.state = Publication.State.MANUAL_REQUIRED
        publication.last_error_code = result.error_code or "manual_reconcile_required"
    else:
        attempt.state = PublicationAttempt.State.PERMANENT_FAILED
        publication.state = Publication.State.PERMANENT_FAILED
        publication.last_error_code = result.error_code or "publisher_permanent"
    retry_audit_action = None
    retry_result = None
    if (
        reconcile_generation is None
        and attempt.state == PublicationAttempt.State.RETRYABLE_FAILED
    ):
        if execution_attempt_no >= 5:
            attempt.state = PublicationAttempt.State.MANUAL_REQUIRED
            attempt.error_code = attempt.error_code or "publication_attempts_exhausted"
            publication.state = Publication.State.MANUAL_REQUIRED
            publication.last_error_code = attempt.error_code
            retry_audit_action = "publication_attempt.retry_exhausted"
            retry_result = "manual_required"
        else:
            attempt.attempt_no = execution_attempt_no + 1
            retry_audit_action = "publication_attempt.retry_scheduled"
            retry_result = "retry_scheduled"
    attempt.recovery_state = _publication_recovery_state(attempt.state)
    attempt.next_recovery_at = None
    attempt.save()
    publication.save()
    if reconcile_generation is not None:
        reconcile_result_state = attempt.state
        reconcile_generation.result_identity = result_identity
        _complete_reconcile_observation_locked(
            attempt,
            reconcile_generation,
            finished_at=now,
            result_state=reconcile_result_state,
            error_code=attempt.error_code,
            recovery_state=attempt.recovery_state,
        )
        reconcile_generation.save(
            update_fields=(
                "state",
                "result_identity",
                "result_state",
                "completed_at",
                "duration_ms",
                "error_code",
                "terminal_impact",
                "recovery_state",
                "next_recovery_at",
            )
        )
        if reconcile_result_state in {
            PublicationAttempt.State.RETRYABLE_FAILED,
            PublicationAttempt.State.UNKNOWN_OUTCOME,
        }:
            if reconcile_generation.generation >= 5:
                _manualize_reconcile_attempt_locked(
                    attempt,
                    error_code=(
                        attempt.error_code
                        or "reconcile_attempts_exhausted"
                    ),
                    now=now,
                )
                retry_audit_action = "publication_attempt.retry_exhausted"
                retry_result = "manual_required"
            else:
                delay = (
                    min(max(retry_after_seconds, 5), 3600)
                    if retry_after_seconds is not None
                    else min(
                        15 * (2 ** max(reconcile_generation.generation - 1, 0)),
                        1800,
                    )
                )
                attempt.next_retry_at = now + timedelta(seconds=delay)
                attempt.save(update_fields=("next_retry_at",))
                next_generation = _enqueue_reconcile_locked(
                    attempt,
                    available_at=attempt.next_retry_at,
                )
                if next_generation is not None:
                    reconcile_generation.recovery_state = (
                        PublicationRecoveryState.AUTOMATIC_RETRY
                    )
                    reconcile_generation.next_recovery_at = (
                        next_generation.not_before
                    )
                retry_audit_action = "publication_attempt.retry_scheduled"
                retry_result = "reconcile_scheduled"
    elif attempt.state == PublicationAttempt.State.UNKNOWN_OUTCOME:
        _enqueue_reconcile_locked(attempt)
    if reconcile_generation is not None:
        reconcile_generation.recovery_state = attempt.recovery_state
        reconcile_generation.next_recovery_at = attempt.next_recovery_at
        reconcile_generation.save(
            update_fields=("recovery_state", "next_recovery_at")
        )
    else:
        _complete_execution_observation_locked(
            attempt,
            execution_attempt_no=execution_attempt_no,
            finished_at=now,
            result_state=(
                result.status
                if result.status
                in {
                    "succeeded",
                    "retryable_failed",
                    "permanent_failed",
                    "unknown_outcome",
                    "manual_required",
                }
                else "permanent_failed"
            ),
            error_code=attempt.error_code,
            retry_at=attempt.next_recovery_at,
            recovery_state=attempt.recovery_state,
            terminal_state=attempt.state,
        )
    attempt.save(
        update_fields=(
            "duration_ms",
            "retry_count",
            "terminal_impact",
            "recovery_state",
            "next_recovery_at",
        )
    )
    _release_article_external_write_fence_locked(attempt)
    audit_action = (
        "publication_attempt.reconciled"
        if reconcile_generation is not None
        else "publication_attempt.finished"
    )
    audit_identity_suffix = (
        f"reconcile-result:{reconcile_generation.generation}"
        if reconcile_generation is not None
        else f"attempt-result:{execution_attempt_no}"
    )
    audit_metadata = {
        "publication_attempt_id": str(attempt.id),
        "attempt": execution_attempt_no,
        "action": attempt.resolved_action,
        "channel": publication.target.channel,
        "error_code": attempt.error_code,
        "intent_id": str(attempt.publication_intent_id),
        "result": result.status,
        "result_hash": result_identity,
        "state": attempt.state,
        "target_id": str(publication.target_id),
    }
    if reconcile_generation is not None:
        audit_metadata.update(
            {
                "reconcile_attempt_no": reconcile_generation.generation,
                "source_event_id": str(reconcile_generation.source_event_id),
            }
        )
    _record_publishing_audit(
        audit_context=audit_context,
        action=audit_action,
        entity=attempt,
        identity_key=f"{audit_context.event_key}:{audit_identity_suffix}",
        before_material=before_material,
        after_material={
            "attempt": _audit_state(
                attempt,
                resultIdentity=result_identity,
                resultStatus=result.status,
            ),
            "publication": _audit_state(publication),
            "target": _audit_state(target),
            "media": _publication_media_state_manifest(publication.id),
        },
        metadata=audit_metadata,
    )
    if retry_audit_action is not None:
        _record_publishing_audit(
            audit_context=audit_context,
            action=retry_audit_action,
            entity=attempt,
            identity_key=(
                f"{audit_context.event_key}:{retry_audit_action}:"
                f"{reconcile_generation.generation if reconcile_generation else execution_attempt_no}"
            ),
            before_material=before_material,
            after_material=_audit_state(attempt),
            metadata={
                "publication_attempt_id": str(attempt.id),
                "attempt": execution_attempt_no,
                "reconcile_attempt_no": (
                    reconcile_generation.generation
                    if reconcile_generation is not None
                    else 0
                ),
                "result": retry_result,
                "result_hash": result_identity,
                "state": attempt.state,
            },
        )
    if attempt.state == PublicationAttempt.State.SUCCEEDED:
        _release_dependents_on_commit(attempt)
        if attempt.publication_intent.correction_case_id:
            correction_case_id = str(attempt.publication_intent.correction_case_id)
            transaction.on_commit(
                lambda value=correction_case_id: _complete_correction(value)
            )
    return attempt


@transaction.atomic
def finalize_publication_delivery_failure(
    attempt_id: str,
    *,
    error_code: str,
    audit_context: AuditContext,
) -> PublicationAttempt | None:
    _require_audit_actor(audit_context, "worker")
    _require_worker_event(
        audit_context,
        topic="publication.requested",
        aggregate_id=attempt_id,
        payload_identity={"publication_attempt_id": str(attempt_id)},
    )
    preliminary = (
        PublicationAttempt.objects.select_related("publication_intent")
        .filter(id=attempt_id)
        .first()
    )
    if preliminary is None:
        return None
    _lock_article_external_write_fence(
        preliminary.publication_intent.article_id
    )
    _lock_target_intent_fences(
        _intent_target_ids(preliminary.publication_intent)
    )
    attempt = _lock_publication_attempt_domain(preliminary)
    audit_identity = (
        f"{audit_context.event_key}:delivery-failure:"
        f"{attempt.attempt_no}"
    )
    replay = _worker_audit_replay(
        audit_context,
        entity=attempt,
        candidates=(
            (
                "publication_attempt.finished",
                audit_identity,
                {"attempt": attempt.attempt_no},
            ),
            (
                "publication_attempt.reconcile_started",
                audit_identity,
                {"attempt": attempt.attempt_no},
            ),
        ),
    )
    if replay is not None:
        return attempt
    if (
        attempt.state == PublicationAttempt.State.STALE
        and attempt.error_code == "approval_revoked"
    ):
        _converge_revoked_attempt_redelivery_locked(
            attempt,
            audit_context=audit_context,
        )
        return attempt
    if attempt.state in {
        PublicationAttempt.State.SUCCEEDED,
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
        PublicationAttempt.State.STALE,
    }:
        raise Conflict(
            "terminal publication attempt has no delivery-failure audit"
        )
    before_material = {
        "attempt": _audit_state(attempt),
        "publication": _audit_state(attempt.publication),
    }
    now = timezone.now()
    effective_error_code = (
        error_code or "publication_delivery_exhausted"
    )[:100]
    if attempt.state in {
        PublicationAttempt.State.RUNNING,
        PublicationAttempt.State.UNKNOWN_OUTCOME,
        PublicationAttempt.State.RECONCILING,
    }:
        if attempt.state == PublicationAttempt.State.RUNNING:
            attempt.state = PublicationAttempt.State.UNKNOWN_OUTCOME
            attempt.finished_at = now
            attempt.error_code = effective_error_code
            attempt.recovery_state = PublicationRecoveryState.RECONCILING
            attempt.next_recovery_at = None
            attempt.save(
                update_fields=(
                    "state",
                    "finished_at",
                    "error_code",
                    "recovery_state",
                    "next_recovery_at",
                )
            )
            _complete_execution_observation_locked(
                attempt,
                execution_attempt_no=attempt.attempt_no,
                finished_at=now,
                result_state=PublicationAttempt.State.UNKNOWN_OUTCOME,
                error_code=attempt.error_code,
                recovery_state=PublicationRecoveryState.RECONCILING,
                terminal_state=PublicationAttempt.State.UNKNOWN_OUTCOME,
            )
            attempt.save(
                update_fields=(
                    "duration_ms",
                    "retry_count",
                    "terminal_impact",
                )
            )
        attempt.publication.state = Publication.State.RECONCILING
        attempt.publication.remote_state = Publication.RemoteState.UNKNOWN
        attempt.publication.last_error_code = effective_error_code
        attempt.publication.save(
            update_fields=(
                "state",
                "remote_state",
                "last_error_code",
                "updated_at",
            )
        )
        _enqueue_reconcile_locked(attempt)
        action = "publication_attempt.reconcile_started"
        result = "unknown_outcome"
    else:
        attempt.state = PublicationAttempt.State.MANUAL_REQUIRED
        attempt.finished_at = now
        attempt.error_code = effective_error_code
        attempt.recovery_state = PublicationRecoveryState.MANUAL_REQUIRED
        attempt.next_recovery_at = None
        attempt.terminal_impact = _publication_terminal_impact(
            scope="publication_delivery",
            stage="delivery",
            final_state=attempt.state,
            error_code=attempt.error_code,
        )
        attempt.save(
            update_fields=(
                "state",
                "finished_at",
                "error_code",
                "recovery_state",
                "next_recovery_at",
                "terminal_impact",
            )
        )
        attempt.publication.state = Publication.State.MANUAL_REQUIRED
        attempt.publication.last_error_code = effective_error_code
        attempt.publication.save(
            update_fields=("state", "last_error_code", "updated_at")
        )
        _release_article_external_write_fence_locked(attempt)
        action = "publication_attempt.finished"
        result = "manual_required"
    _record_publishing_audit(
        audit_context=audit_context,
        action=action,
        entity=attempt,
        identity_key=audit_identity,
        before_material=before_material,
        after_material={
            "attempt": _audit_state(attempt),
            "publication": _audit_state(attempt.publication),
        },
        metadata={
            "publication_attempt_id": str(attempt.id),
            "attempt": attempt.attempt_no,
            "result": result,
            "error_code": effective_error_code,
            "state": attempt.state,
        },
    )
    return attempt


def _complete_correction(case_id: str) -> None:
    from .corrections import complete_correction_if_terminal

    complete_correction_if_terminal(case_id)


def publisher_error_result(error: PublisherError):
    from .contracts import PublishResult

    status = {
        "unknown_outcome": "unknown_outcome",
        "retryable": "retryable_failed",
        "refreshable_auth": "retryable_failed",
        "permanent": "permanent_failed",
    }.get(error.category, "permanent_failed")
    return PublishResult(
        status=status,
        remote_state="unknown",
        reconcile_required=status == "unknown_outcome",
        http_status=error.http_status,
        error_code=error.code,
        error_detail_redacted=error.detail_redacted,
    )


def _release_dependents_on_commit(attempt: PublicationAttempt) -> None:
    if attempt.publication.target.channel != ChannelCode.WORDPRESS:
        return
    dependent_target_ids = {
        str(command["targetId"])
        for command in attempt.publication_intent.target_commands
        if (
            isinstance(command, dict)
            and str(command.get("canonicalDependencyTargetId"))
            == str(attempt.publication.target_id)
        )
    }
    dependent = list(
        PublicationAttempt.objects.filter(
            publication_intent=attempt.publication_intent,
            publication__target__channel=ChannelCode.BLOGGER,
            publication__target_id__in=dependent_target_ids,
            state=PublicationAttempt.State.QUEUED,
        ).values_list("id", "correlation_id")
    )
    for attempt_id, correlation_id in dependent:
        _enqueue_event(
            "publication.requested",
            {
                "publication_attempt_id": str(attempt_id),
            },
            dedupe_key=f"publication.requested:{attempt_id}:1",
            aggregate_type="publication_attempt",
            aggregate_id=attempt_id,
            job_id=attempt_id,
            correlation_id=correlation_id,
        )


@transaction.atomic
def begin_reconcile(
    attempt_id: str,
    expected_reconcile_attempt_no: int | None = None,
    *,
    source_event_id: uuid.UUID | str,
    audit_context: AuditContext,
) -> tuple[
    PublicationAttempt,
    PublicationReconcileGeneration | None,
    PublishCommand | None,
]:
    _require_audit_actor(audit_context, "worker")
    try:
        source_event_uuid = uuid.UUID(str(source_event_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise Conflict("reconcile source event context is invalid") from exc
    if str(source_event_uuid) != audit_context.event_key:
        raise Conflict("reconcile source event differs from audit provenance")
    expected_payload = {"publication_attempt_id": str(attempt_id)}
    if expected_reconcile_attempt_no is not None:
        expected_payload["reconcile_attempt_no"] = str(
            expected_reconcile_attempt_no
        )
    source_event = _require_worker_event(
        audit_context,
        topic="publication.reconcile_requested",
        aggregate_id=attempt_id,
        payload_identity=expected_payload,
    )
    preliminary = PublicationAttempt.objects.select_related(
        "publication_intent"
    ).get(id=attempt_id)
    _lock_article_external_write_fence(
        preliminary.publication_intent.article_id
    )
    _lock_target_intent_fences(
        _intent_target_ids(preliminary.publication_intent)
    )
    attempt = _lock_publication_attempt_domain(preliminary)
    payload = source_event.payload if isinstance(source_event.payload, dict) else {}
    if payload.get("publication_attempt_id") != str(attempt.id):
        raise Conflict("reconcile source event payload does not match the attempt")

    generation = (
        PublicationReconcileGeneration.objects.select_for_update()
        .filter(source_event=source_event)
        .first()
    )
    if generation is not None:
        if generation.publication_attempt_id != attempt.id:
            raise Conflict("reconcile source event is bound to another attempt")
        if (
            expected_reconcile_attempt_no is not None
            and generation.generation != expected_reconcile_attempt_no
        ):
            raise Conflict("reconcile source event generation does not match")
    else:
        if attempt.state in {
            PublicationAttempt.State.SUCCEEDED,
            PublicationAttempt.State.PERMANENT_FAILED,
            PublicationAttempt.State.MANUAL_REQUIRED,
            PublicationAttempt.State.STALE,
        }:
            return attempt, None, None
        if source_event.event_version == 1:
            generation_no = attempt.reconcile_attempt_no + 1
        elif source_event.event_version == 2:
            payload_generation = payload.get("reconcile_attempt_no")
            if (
                type(payload_generation) is not int
                or payload_generation != expected_reconcile_attempt_no
            ):
                raise Conflict("reconcile v2 payload generation does not match")
            generation_no = payload_generation
        else:
            raise Conflict("unsupported reconcile event version")
        if generation_no < 1 or generation_no > 5:
            raise Conflict("reconcile attempt budget is exhausted")
        if generation_no != attempt.reconcile_attempt_no + 1:
            raise Conflict("reconcile generation is not contiguous")
        generation = PublicationReconcileGeneration.objects.create(
            publication_attempt=attempt,
            generation=generation_no,
            source_event=source_event,
            state=PublicationReconcileGeneration.State.STARTED,
            not_before=source_event.not_before,
            started_at=timezone.now(),
            correlation_id=source_event.correlation_id,
            worker_task_id=_current_worker_task_id(),
            recovery_state=PublicationRecoveryState.RECONCILING,
        )
        attempt.reconcile_attempt_no = generation_no
        attempt.save(update_fields=("reconcile_attempt_no",))

    if (
        generation.state == PublicationReconcileGeneration.State.COMPLETED
        or generation.generation < attempt.reconcile_attempt_no
    ):
        completed_audit = _worker_audit_replay(
            audit_context,
            entity=attempt,
            candidates=(
                (
                    "publication_attempt.reconciled",
                    (
                        f"{audit_context.event_key}:reconcile-result:"
                        f"{generation.generation}"
                    ),
                    {
                        "reconcile_attempt_no": generation.generation,
                        "result_hash": generation.result_identity,
                    },
                ),
                (
                    "publication_attempt.reconcile_delivery_failed",
                    (
                        f"{audit_context.event_key}:"
                        "reconcile-delivery-failure:"
                        f"{generation.generation}"
                    ),
                    {
                        "reconcile_attempt_no": generation.generation,
                        "source_event_id": str(source_event.id),
                    },
                ),
            ),
        )
        if completed_audit is None:
            raise Conflict(
                "completed reconcile generation has no matching result audit"
            )
        return attempt, generation, None
    if attempt.state == PublicationAttempt.State.SUCCEEDED:
        return attempt, generation, None
    if attempt.state not in {
        PublicationAttempt.State.UNKNOWN_OUTCOME,
        PublicationAttempt.State.RECONCILING,
        PublicationAttempt.State.RETRYABLE_FAILED,
    }:
        raise Conflict("unknown-outcome attempt만 조정할 수 있습니다.")
    before_material = _audit_state(attempt)
    attempt.state = PublicationAttempt.State.RECONCILING
    attempt.recovery_state = PublicationRecoveryState.RECONCILING
    attempt.next_recovery_at = None
    attempt.save(
        update_fields=["state", "recovery_state", "next_recovery_at"]
    )
    attempt.publication.state = Publication.State.RECONCILING
    attempt.publication.save(update_fields=["state", "updated_at"])
    generation_updates = ["recovery_state", "next_recovery_at"]
    generation.recovery_state = PublicationRecoveryState.RECONCILING
    generation.next_recovery_at = None
    if not generation.worker_task_id:
        generation.worker_task_id = _current_worker_task_id()
        generation.started_at = timezone.now()
        generation_updates.extend(("worker_task_id", "started_at"))
    generation.save(update_fields=generation_updates)
    if not _worker_audit_replay(
        audit_context,
        entity=attempt,
        candidates=(
            (
                "publication_attempt.reconcile_started",
                (
                    f"{audit_context.event_key}:reconcile-start:"
                    f"{generation.generation}"
                ),
                {"reconcile_attempt_no": generation.generation},
            ),
        ),
    ):
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_attempt.reconcile_started",
            entity=attempt,
            identity_key=(
                f"{audit_context.event_key}:reconcile-start:"
                f"{generation.generation}"
            ),
            before_material=before_material,
            after_material=_audit_state(attempt),
            metadata={
                "publication_attempt_id": str(attempt.id),
                "attempt": attempt.attempt_no,
                "reconcile_attempt_no": generation.generation,
                "source_event_id": str(source_event.id),
                "state": attempt.state,
            },
        )
    return (
        attempt,
        generation,
        _command_for_attempt(attempt, _final_render(attempt)),
    )


@transaction.atomic
def finalize_reconcile_delivery_failure(
    attempt_id: str,
    *,
    source_event_id: uuid.UUID | str,
    error_code: str,
    audit_context: AuditContext,
) -> PublicationAttempt | None:
    _require_audit_actor(audit_context, "worker")
    try:
        source_event_uuid = uuid.UUID(str(source_event_id))
    except (ValueError, TypeError, AttributeError):
        return None
    if str(source_event_uuid) != audit_context.event_key:
        raise Conflict("reconcile source event differs from audit provenance")
    source_event = _require_worker_event(
        audit_context,
        topic="publication.reconcile_requested",
        aggregate_id=attempt_id,
        payload_identity={"publication_attempt_id": str(attempt_id)},
    )
    preliminary = PublicationAttempt.objects.select_related(
        "publication_intent"
    ).filter(id=attempt_id).first()
    if preliminary is None:
        return None
    _lock_article_external_write_fence(
        preliminary.publication_intent.article_id
    )
    _lock_target_intent_fences(
        _intent_target_ids(preliminary.publication_intent)
    )
    attempt = _lock_publication_attempt_domain(preliminary)
    if _worker_audit_replay(
        audit_context,
        entity=attempt,
        candidates=(
            (
                "publication_attempt.reconcile_delivery_failed",
                (
                    f"{audit_context.event_key}:"
                    "reconcile-delivery-failure:"
                    f"{attempt.reconcile_attempt_no}"
                ),
                {
                    "reconcile_attempt_no": attempt.reconcile_attempt_no,
                    "source_event_id": str(source_event_uuid),
                    "error_code": (
                        error_code
                        or "reconcile_delivery_exhausted"
                    ),
                },
            ),
        ),
    ):
        return attempt
    before_material = _audit_state(attempt)

    def audit_failure() -> None:
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_attempt.reconcile_delivery_failed",
            entity=attempt,
            identity_key=(
                f"{audit_context.event_key}:reconcile-delivery-failure:"
                f"{attempt.reconcile_attempt_no}"
            ),
            before_material=before_material,
            after_material=_audit_state(attempt),
            metadata={
                "publication_attempt_id": str(attempt.id),
                "attempt": attempt.attempt_no,
                "reconcile_attempt_no": attempt.reconcile_attempt_no,
                "source_event_id": str(source_event_uuid),
                "error_code": error_code or "reconcile_delivery_exhausted",
                "result": "manual_required",
                "state": attempt.state,
            },
        )
    payload = source_event.payload if isinstance(source_event.payload, dict) else {}
    if payload.get("publication_attempt_id") != str(attempt.id):
        _manualize_reconcile_attempt_locked(
            attempt,
            error_code="reconcile_source_event_mismatch",
        )
        audit_failure()
        return attempt

    generation = (
        PublicationReconcileGeneration.objects.select_for_update()
        .filter(source_event=source_event)
        .first()
    )
    if generation is None:
        if source_event.event_version == 1:
            generation_no = attempt.reconcile_attempt_no + 1
        elif source_event.event_version == 2:
            generation_no = payload.get("reconcile_attempt_no")
        else:
            generation_no = None
        if (
            type(generation_no) is not int
            or generation_no < 1
            or generation_no > 5
        ):
            _manualize_reconcile_attempt_locked(
                attempt,
                error_code="reconcile_generation_invalid",
            )
            audit_failure()
            return attempt
        generation = (
            PublicationReconcileGeneration.objects.select_for_update()
            .filter(
                publication_attempt=attempt,
                generation=generation_no,
            )
            .first()
        )
        if (
            generation is not None
            and generation.source_event_id != source_event.id
        ):
            _manualize_reconcile_attempt_locked(
                attempt,
                error_code="reconcile_generation_binding_conflict",
            )
            audit_failure()
            return attempt
        if generation is None:
            generation = PublicationReconcileGeneration.objects.create(
                publication_attempt=attempt,
                generation=generation_no,
                source_event=source_event,
                state=PublicationReconcileGeneration.State.STARTED,
                not_before=source_event.not_before,
                started_at=source_event.occurred_at,
                correlation_id=source_event.correlation_id,
                recovery_state=PublicationRecoveryState.RECONCILING,
            )
        if generation_no > attempt.reconcile_attempt_no:
            attempt.reconcile_attempt_no = generation_no
            attempt.save(update_fields=("reconcile_attempt_no",))

    _terminalize_reconcile_generation_locked(
        attempt,
        generation,
        error_code=error_code or "reconcile_delivery_exhausted",
    )
    audit_failure()
    return attempt


@dataclass(frozen=True)
class RemoteMediaReconcileFence:
    remote_media_id: uuid.UUID
    publication_attempt_id: uuid.UUID
    publication_intent_id: uuid.UUID
    target_id: uuid.UUID
    lease_generation: int
    request_fingerprint: str


def _remote_media_result_hash(result) -> str:
    return sha256_hex(
        {
            "status": result.status,
            "remote_media_id": result.remote_post_id,
            "remote_url": result.remote_url,
            "error_code": getattr(result, "error_code", None),
            "http_status": getattr(result, "http_status", None),
        }
    )


@transaction.atomic
def begin_remote_media_reconcile(
    remote_media_id: str,
    *,
    publication_attempt_id: str,
    publication_intent_id: str,
    audit_context: AuditContext,
) -> tuple[RemoteMedia, RemoteMediaReconcileFence | None]:
    _require_audit_actor(audit_context, "worker")
    _require_worker_event(
        audit_context,
        topic="media.reconcile_requested",
        aggregate_id=remote_media_id,
        payload_identity={
            "remote_media_id": str(remote_media_id),
            "publication_attempt_id": str(publication_attempt_id),
            "publication_intent_id": str(publication_intent_id),
        },
    )
    attempt = PublicationAttempt.objects.select_for_update().get(
        id=publication_attempt_id,
        publication_intent_id=publication_intent_id,
    )
    remote = RemoteMedia.objects.select_for_update().get(id=remote_media_id)
    binding = (
        PublicationMedia.objects.select_for_update()
        .filter(
            publication_id=attempt.publication_id,
            remote_media_id=remote.id,
        )
        .order_by("id")
        .first()
    )
    if binding is None:
        raise Conflict(
            "media reconcile identity does not match the publication attempt"
        )
    result_identity = (
        f"{audit_context.event_key}:remote-media-result:"
        f"{remote.lease_generation}"
    )
    if _worker_audit_replay(
        audit_context,
        entity=remote,
        candidates=(
            (
                "remote_media.reconciled",
                result_identity,
                {
                    "publication_attempt_id": str(attempt.id),
                    "intent_id": str(attempt.publication_intent_id),
                },
            ),
        ),
    ):
        return remote, None
    if remote.state not in {
        RemoteMedia.State.RECONCILING,
        RemoteMedia.State.UPLOADING,
    }:
        raise Conflict(
            "terminal remote media state has no matching reconcile audit"
        )
    start_identity = (
        f"{audit_context.event_key}:remote-media-start:"
        f"{remote.lease_generation}"
    )
    if not _worker_audit_replay(
        audit_context,
        entity=remote,
        candidates=(
            (
                "remote_media.reconcile_started",
                start_identity,
                {
                    "publication_attempt_id": str(attempt.id),
                    "intent_id": str(attempt.publication_intent_id),
                },
            ),
        ),
    ):
        before_material = _audit_state(remote)
        remote.state = RemoteMedia.State.RECONCILING
        remote.save(update_fields=("state",))
        _record_publishing_audit(
            audit_context=audit_context,
            action="remote_media.reconcile_started",
            entity=remote,
            identity_key=start_identity,
            before_material=before_material,
            after_material=_audit_state(remote),
            metadata={
                "remote_media_id": str(remote.id),
                "publication_attempt_id": str(attempt.id),
                "intent_id": str(attempt.publication_intent_id),
                "target_id": str(remote.target_id),
                "result": "started",
                "state": remote.state,
            },
        )
    return remote, RemoteMediaReconcileFence(
        remote_media_id=remote.id,
        publication_attempt_id=attempt.id,
        publication_intent_id=attempt.publication_intent_id,
        target_id=remote.target_id,
        lease_generation=remote.lease_generation,
        request_fingerprint=remote.request_fingerprint,
    )


@transaction.atomic
def persist_remote_media_reconcile_result(
    fence: RemoteMediaReconcileFence,
    result,
    *,
    audit_context: AuditContext,
) -> RemoteMedia:
    _require_audit_actor(audit_context, "worker")
    _require_worker_event(
        audit_context,
        topic="media.reconcile_requested",
        aggregate_id=fence.remote_media_id,
        payload_identity={
            "remote_media_id": str(fence.remote_media_id),
            "publication_attempt_id": str(fence.publication_attempt_id),
            "publication_intent_id": str(fence.publication_intent_id),
        },
    )
    attempt = PublicationAttempt.objects.select_for_update().filter(
        id=fence.publication_attempt_id,
        publication_intent_id=fence.publication_intent_id,
    ).first()
    if attempt is None:
        raise Conflict(
            "media reconcile result no longer matches the publication attempt"
        )
    remote = RemoteMedia.objects.select_for_update().get(
        id=fence.remote_media_id,
    )
    binding = (
        PublicationMedia.objects.select_for_update()
        .filter(
            publication_id=attempt.publication_id,
            remote_media_id=remote.id,
        )
        .order_by("id")
        .first()
    )
    if binding is None:
        raise Conflict(
            "media reconcile result no longer matches the publication attempt"
        )
    result_hash = _remote_media_result_hash(result)
    result_identity = (
        f"{audit_context.event_key}:remote-media-result:"
        f"{fence.lease_generation}"
    )
    replay = _worker_audit_replay(
        audit_context,
        entity=remote,
        candidates=(
            (
                "remote_media.reconciled",
                result_identity,
                {
                    "publication_attempt_id": str(attempt.id),
                    "intent_id": str(attempt.publication_intent_id),
                    "result_hash": result_hash,
                },
            ),
        ),
    )
    if replay is not None:
        return remote
    if (
        remote.target_id != fence.target_id
        or remote.lease_generation != fence.lease_generation
        or remote.request_fingerprint != fence.request_fingerprint
        or remote.state != RemoteMedia.State.RECONCILING
    ):
        raise Conflict("stale remote media reconcile result was fenced")
    before_material = _audit_state(remote)
    if result.status == "succeeded" and result.remote_post_id:
        remote.remote_media_id = result.remote_post_id
        remote.remote_source_url = result.remote_url
        remote.state = RemoteMedia.State.AVAILABLE
        result_code = "available"
    else:
        remote.state = RemoteMedia.State.FAILED
        result_code = "failed"
    remote.last_reconciled_at = timezone.now()
    remote.last_reconcile_hash = result_hash
    remote.save(
        update_fields=(
            "remote_media_id",
            "remote_source_url",
            "state",
            "last_reconciled_at",
            "last_reconcile_hash",
        )
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="remote_media.reconciled",
        entity=remote,
        identity_key=result_identity,
        before_material=before_material,
        after_material=_audit_state(
            remote,
            remoteIdentityHash=(
                sha256_hex(result.remote_post_id)
                if result.remote_post_id
                else None
            ),
            remoteUrlHash=(
                sha256_hex(result.remote_url)
                if result.remote_url
                else None
            ),
            resultHash=result_hash,
        ),
        metadata={
            "remote_media_id": str(remote.id),
            "publication_attempt_id": str(attempt.id),
            "intent_id": str(attempt.publication_intent_id),
            "target_id": str(remote.target_id),
            "result": result_code,
            "result_hash": result_hash,
            "status": result.status,
            "error_code": getattr(result, "error_code", None),
            "state": remote.state,
        },
    )
    return remote


@transaction.atomic
def prepare_wordpress_media(
    *,
    publication_id: str,
    article_revision_id: str,
    evidence_asset_id: str,
    published_evidence_snapshot_id: str | None,
    published_visualization_snapshot_id: str | None,
    block_id: str,
    usage: str,
    asset_checksum: str,
    presentation_hash: str,
    alt_text: str,
    caption: str,
    attribution: str,
) -> tuple[RemoteMedia, PublicationMedia]:
    publication = Publication.objects.select_for_update().select_related("target").get(id=publication_id)
    if publication.target.channel != ChannelCode.WORDPRESS:
        raise InvalidInput("WordPress publication만 remote media를 사용할 수 있습니다.")
    remote, _ = RemoteMedia.objects.select_for_update().get_or_create(
        target=publication.target,
        asset_checksum=asset_checksum,
        presentation_hash=presentation_hash,
        defaults={
            "evidence_asset_id": evidence_asset_id,
            "remote_lookup_key": f"ww-media-{asset_checksum[:20]}-{presentation_hash[:12]}",
            "request_fingerprint": sha256_hex({"checksum": asset_checksum, "presentation": presentation_hash}),
        },
    )
    if remote.state == RemoteMedia.State.ORPHANED:
        remote.state = RemoteMedia.State.PENDING
        remote.orphaned_at = None
        remote.lease_generation += 1
        remote.save(update_fields=["state", "orphaned_at", "lease_generation"])
    binding, _ = PublicationMedia.objects.get_or_create(
        publication=publication,
        article_revision_id=article_revision_id,
        remote_media=remote,
        block_id=block_id,
        defaults={
            "evidence_asset_id": evidence_asset_id,
            "published_evidence_snapshot_id": published_evidence_snapshot_id,
            "published_visualization_snapshot_id": published_visualization_snapshot_id,
            "usage": usage,
            "alt_text_snapshot": alt_text,
            "caption_snapshot": caption,
            "attribution_snapshot": attribution,
            "lease_generation": remote.lease_generation,
        },
    )
    return remote, binding


@transaction.atomic
def prepare_public_delivery(
    *,
    publication_id: str,
    article_revision_id: str,
    evidence_asset_id: str,
    published_evidence_snapshot_id: str | None,
    published_visualization_snapshot_id: str | None,
    block_id: str,
    usage: str,
    asset_checksum: str,
    presentation_hash: str,
    mime_type: str,
    byte_size: int,
    delivery_object_key: str,
    delivery_object_version: str,
    public_url: str,
    rights_status: str,
    alt_text: str,
    caption: str,
    attribution: str,
) -> tuple[PublicDeliveryAsset, PublicationMedia]:
    publication = Publication.objects.select_for_update().select_related("target").get(id=publication_id)
    if publication.target.channel != ChannelCode.BLOGGER:
        raise InvalidInput("Blogger publication만 public delivery asset을 사용합니다.")
    if not public_url.startswith("https://"):
        raise InvalidInput("Public delivery URL은 HTTPS여야 합니다.")
    delivery, _ = PublicDeliveryAsset.objects.select_for_update().get_or_create(
        asset_checksum=asset_checksum,
        presentation_hash=presentation_hash,
        defaults={
            "source_evidence_asset_id": evidence_asset_id,
            "mime_type": mime_type,
            "byte_size": byte_size,
            "delivery_object_key": delivery_object_key,
            "delivery_object_version": delivery_object_version,
            "public_url": public_url,
            "rights_status_snapshot": rights_status,
            "alt_text_snapshot": alt_text,
            "caption_snapshot": caption,
            "attribution_snapshot": attribution,
        },
    )
    if delivery.state in {PublicDeliveryAsset.State.PENDING_DELETE, PublicDeliveryAsset.State.WITHDRAWAL_PENDING}:
        delivery.state = PublicDeliveryAsset.State.AVAILABLE
        delivery.zero_reference_at = None
        delivery.delete_after = None
        delivery.lease_generation += 1
    delivery.active_reference_count += 1
    delivery.save()
    binding, _ = PublicationMedia.objects.get_or_create(
        publication=publication,
        article_revision_id=article_revision_id,
        public_delivery_asset=delivery,
        block_id=block_id,
        defaults={
            "evidence_asset_id": evidence_asset_id,
            "published_evidence_snapshot_id": published_evidence_snapshot_id,
            "published_visualization_snapshot_id": published_visualization_snapshot_id,
            "usage": usage,
            "alt_text_snapshot": alt_text,
            "caption_snapshot": caption,
            "attribution_snapshot": attribution,
            "lease_generation": delivery.lease_generation,
        },
    )
    return delivery, binding


@transaction.atomic
def schedule_public_delivery_deletion(asset_id: str) -> PublicDeliveryAsset:
    asset = PublicDeliveryAsset.objects.select_for_update().get(id=asset_id)
    active = PublicationMedia.objects.filter(
        public_delivery_asset=asset,
        binding_state__in=[PublicationMedia.BindingState.PREPARED, PublicationMedia.BindingState.ACTIVE],
        publication__state__in=[
            Publication.State.SCHEDULED,
            Publication.State.IN_PROGRESS,
            Publication.State.PUBLISHED,
            Publication.State.MARKED_WITHDRAWN,
            Publication.State.RECONCILING,
        ],
    ).count()
    asset.active_reference_count = active
    if active == 0:
        asset.state = PublicDeliveryAsset.State.PENDING_DELETE
        asset.zero_reference_at = timezone.now()
        asset.delete_after = timezone.now() + timedelta(days=30)
    asset.save()
    return asset


def _has_valid_retry_reservation(attempt: PublicationAttempt) -> bool:
    expected_attempt = max(attempt.attempt_no - 1, 0)
    events = AuditEvent.objects.using(attempt._state.db).filter(
        action="publication_attempt.retry_scheduled",
        entity_type=attempt._meta.label_lower,
        entity_id=attempt.id,
    ).order_by("occurred_at", "id")
    for event in events:
        metadata = validate_stored_metadata(
            action=event.action,
            metadata_schema_version=event.metadata_schema_version,
            redaction_policy_version=event.redaction_policy_version,
            redaction_policy_hash_value=event.redaction_policy_hash,
            metadata=event.metadata_redacted,
        )
        if metadata.get("attempt") == expected_attempt:
            return True
    return False


@transaction.atomic
def retry_publication_attempt(
    attempt_id: str,
    *,
    audit_context: AuditContext,
) -> tuple[PublicationAttempt, str]:
    _require_audit_actor(audit_context, "admin")
    attempt = (
        PublicationAttempt.objects.select_for_update()
        .select_related("publication__target")
        .get(id=attempt_id)
    )
    request_hash = _admin_request_hash(
        audit_context=audit_context,
        action="publication_attempt.retry_requested",
        payload={"publicationAttemptId": str(attempt.id)},
    )
    if _has_request_audit(
        audit_context=audit_context,
        action="publication_attempt.retry_requested",
        entity=attempt,
    ):
        replay = require_audit_replay(
            context=audit_context,
            action="publication_attempt.retry_requested",
            entity=attempt,
            identity_key=audit_context.request_key,
            request_hash=request_hash,
        )
        return attempt, str(replay.metadata_redacted.get("result") or "replay")
    before_material = _audit_state(attempt)
    if attempt.state in {
        PublicationAttempt.State.UNKNOWN_OUTCOME,
        PublicationAttempt.State.RECONCILING,
    }:
        _enqueue_reconcile_locked(attempt)
        action = (
            "manual_required"
            if attempt.state == PublicationAttempt.State.MANUAL_REQUIRED
            else "reconcile"
        )
    elif attempt.state == PublicationAttempt.State.RETRYABLE_FAILED:
        next_attempt_was_reserved = _has_valid_retry_reservation(attempt)
        if not next_attempt_was_reserved:
            attempt.attempt_no += 1
        attempt.state = PublicationAttempt.State.QUEUED
        attempt.started_at = None
        attempt.finished_at = None
        attempt.next_retry_at = None
        attempt.recovery_state = PublicationRecoveryState.IN_PROGRESS
        attempt.next_recovery_at = None
        attempt.error_detail_redacted = ""
        attempt.save(
            update_fields=[
                "state",
                "attempt_no",
                "started_at",
                "finished_at",
                "next_retry_at",
                "recovery_state",
                "next_recovery_at",
                "error_detail_redacted",
            ]
        )
        _enqueue_event(
            "publication.requested",
            {"publication_attempt_id": str(attempt.id)},
            dedupe_key=f"publication.requested:{attempt.id}:{attempt.attempt_no}",
            aggregate_type="publication_attempt",
            aggregate_id=attempt.id,
            job_id=attempt.id,
            correlation_id=attempt.correlation_id,
        )
        action = "retry"
    else:
        raise Conflict("only retryable or unknown publication attempts can be retried")
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_attempt.retry_requested",
        entity=attempt,
        identity_key=(
            audit_context.request_key
            or f"retry-request:{audit_context.correlation_id}:{attempt.id}"
        ),
        before_material=before_material,
        after_material=_audit_state(attempt),
        metadata={
            "publication_attempt_id": str(attempt.id),
            "attempt": attempt.attempt_no,
            "request_hash": request_hash,
            "result": action,
            "state": attempt.state,
            "target_id": str(attempt.publication.target_id),
        },
    )
    return attempt, action


@dataclass(frozen=True)
class TargetCredentialRevokeFence:
    decision_id: uuid.UUID
    target_id: uuid.UUID
    target_snapshot_id: uuid.UUID
    target_snapshot_version: int
    target_config_hash: str


@transaction.atomic
def begin_target_credential_revoke(
    decision_id: str,
    *,
    audit_context: AuditContext,
) -> tuple[
    TargetDisconnectDecision,
    PublicationTarget,
    PublicationTargetIntentFence,
    TargetCredentialRevokeFence,
] | None:
    _require_audit_actor(audit_context, "worker")
    target_id = TargetDisconnectDecision.objects.values_list(
        "target_id",
        flat=True,
    ).get(id=decision_id)
    _require_worker_event(
        audit_context,
        topic="publishing.target_disconnect.requested",
        aggregate_id=target_id,
        payload_identity={
            "decision_id": str(decision_id),
            "target_id": str(target_id),
        },
    )
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    decision = TargetDisconnectDecision.objects.select_for_update().get(
        id=decision_id,
        target_id=target.id,
    )
    terminal_recorded = _worker_audit_replay(
        audit_context,
        entity=decision,
        candidates=(
            (
                "publication_target.credentials_revoked",
                (
                    f"{audit_context.event_key}:"
                    "credential-revoke:succeeded"
                ),
                None,
            ),
            (
                "publication_target.credential_revoke_failed",
                (
                    f"{audit_context.event_key}:"
                    "credential-revoke:failed"
                ),
                None,
            ),
        ),
    )
    if terminal_recorded:
        return None
    if decision.state == TargetDisconnectDecision.State.COMPLETED:
        raise Conflict(
            "completed credential revocation has no matching audit event"
        )
    started_recorded = _worker_audit_replay(
        audit_context,
        entity=decision,
        candidates=(
            (
                "publication_target.credential_revoke_started",
                f"{audit_context.event_key}:credential-revoke-start",
                None,
            ),
        ),
    )
    if started_recorded:
        before_material = _audit_state(decision)
        decision.state = TargetDisconnectDecision.State.RECONCILING
        decision.remote_result_hash = sha256_hex(
            {
                "decisionId": str(decision.id),
                "result": "worker_redelivery_after_external_call_boundary",
            }
        )
        decision.save(update_fields=("state", "remote_result_hash"))
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.credential_revoke_failed",
            entity=decision,
            identity_key=f"{audit_context.event_key}:credential-revoke:failed",
            before_material=before_material,
            after_material=_audit_state(
                decision,
                outcomeHash=decision.remote_result_hash,
                errorCode="credential_revoke_outcome_unknown",
            ),
            metadata={
                "target_id": str(target.id),
                "decision_id": str(decision.id),
                "outcome_hash": decision.remote_result_hash,
                "error_code": "credential_revoke_outcome_unknown",
                "result": "unknown",
                "state": decision.state,
            },
        )
        return None
    if decision.state != TargetDisconnectDecision.State.ACCEPTED:
        raise Conflict(
            "credential revocation state has no matching terminal audit event"
        )
    if not started_recorded:
        before_material = _audit_state(decision)
        decision.state = TargetDisconnectDecision.State.REVOKING
        decision.save(update_fields=("state",))
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.credential_revoke_started",
            entity=decision,
            identity_key=f"{audit_context.event_key}:credential-revoke-start",
            before_material=before_material,
            after_material=_audit_state(decision),
            metadata={
                "target_id": str(target.id),
                "decision_id": str(decision.id),
                "result": "started",
                "state": decision.state,
            },
        )
    return (
        decision,
        target,
        TargetCredentialRevokeFence(
            decision_id=decision.id,
            target_id=target.id,
            target_snapshot_id=target.current_snapshot_id,
            target_snapshot_version=target.current_snapshot_version,
            target_config_hash=target.current_config_hash,
        ),
    )


@transaction.atomic
def persist_target_credential_revoke_result(
    fence: TargetCredentialRevokeFence,
    *,
    succeeded: bool,
    outcome_hash: str,
    error_code: str = "",
    unknown_outcome: bool = False,
    audit_context: AuditContext,
) -> TargetDisconnectDecision:
    _require_audit_actor(audit_context, "worker")
    _require_worker_event(
        audit_context,
        topic="publishing.target_disconnect.requested",
        aggregate_id=fence.target_id,
        payload_identity={
            "decision_id": str(fence.decision_id),
            "target_id": str(fence.target_id),
        },
    )
    _lock_target_intent_fences((fence.target_id,))
    locked_intents = _lock_open_target_intents(fence.target_id)
    target = PublicationTarget.objects.select_for_update().get(id=fence.target_id)
    decision = TargetDisconnectDecision.objects.select_for_update().get(
        id=fence.decision_id,
        target_id=target.id,
    )
    fenced = (
        target.current_snapshot_id == fence.target_snapshot_id
        and target.current_snapshot_version == fence.target_snapshot_version
        and target.current_config_hash == fence.target_config_hash
    )
    committed_success = succeeded and fenced
    action = (
        "publication_target.credentials_revoked"
        if committed_success
        else "publication_target.credential_revoke_failed"
    )
    result_code = "revoked" if committed_success else "failed"
    outcome_status = (
        "succeeded"
        if committed_success
        else (
            "unknown_outcome"
            if unknown_outcome
            else (
                "stale_fence"
                if succeeded
                else "failed"
            )
        )
    )
    effective_error_code = error_code
    if succeeded and not fenced:
        effective_error_code = "target_snapshot_stale_after_revoke"
    replay = _worker_audit_replay(
        audit_context,
        entity=decision,
        candidates=(
            (
                action,
                (
                    f"{audit_context.event_key}:credential-revoke:"
                    f"{'succeeded' if committed_success else 'failed'}"
                ),
                {
                    "outcome_hash": outcome_hash,
                    "error_code": effective_error_code,
                    "result": result_code,
                    "status": outcome_status,
                },
            ),
        ),
    )
    if replay is not None:
        return decision
    before_material = _audit_state(decision)
    target_before_material = _audit_state(target)
    if succeeded and fenced:
        target.credential_ref = None
        target.username_ref = None
        target.save(
            update_fields=("credential_ref", "username_ref", "updated_at")
        )
        _snapshot_locked(target)
        _stale_locked_intents(locked_intents)
        decision.state = TargetDisconnectDecision.State.COMPLETED
    else:
        decision.state = (
            TargetDisconnectDecision.State.RECONCILING
            if unknown_outcome or (succeeded and not fenced)
            else TargetDisconnectDecision.State.FAILED
        )
    decision.remote_result_hash = outcome_hash
    decision.save(update_fields=("state", "remote_result_hash"))
    _record_publishing_audit(
        audit_context=audit_context,
        action=action,
        entity=decision,
        identity_key=(
            f"{audit_context.event_key}:credential-revoke:"
            f"{'succeeded' if succeeded and fenced else 'failed'}"
        ),
        before_material={
            "decision": before_material,
            "target": target_before_material,
        },
        after_material={
            "decision": _audit_state(
                decision,
                outcomeHash=outcome_hash,
                errorCode=effective_error_code,
            ),
            "target": _audit_state(target),
        },
        metadata={
            "target_id": str(target.id),
            "decision_id": str(decision.id),
            "outcome_hash": outcome_hash,
            "error_code": effective_error_code,
            "result": result_code,
            "status": outcome_status,
            "state": decision.state,
        },
    )
    return decision


@transaction.atomic
def disconnect_target(
    target_id: str,
    data: dict[str, Any],
    *,
    request,
    audit_context: AuditContext,
) -> TargetDisconnectDecision:
    _require_audit_actor(audit_context, "admin")
    _lock_target_intent_fences((target_id,))
    locked_intents = _lock_open_target_intents(target_id)
    user = request.user
    if audit_context.actor_id != user.pk:
        raise Forbidden("disconnect audit actor differs from the administrator")
    if (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden(
            "disconnect provenance differs from the audit context"
        )
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_hash = _request_hash(data)
    existing = TargetDisconnectDecision.objects.filter(target=target, request_key=data["requestKey"]).first()
    if existing:
        if existing.request_hash != request_hash or existing.decided_by_id != user.pk:
            raise Conflict("같은 request key가 다른 연결 해제 payload에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="publication_target.disconnected",
            entity=target,
            identity_key=f"target-disconnect:{existing.id}",
            request_hash=request_hash,
        )
        return existing
    _assert_target_has_no_active_external_write(target.id)
    if (
        str(target.current_snapshot_id) != str(data["expectedTargetSnapshotId"])
        or target.current_config_hash != data["expectedTargetConfigHash"]
    ):
        raise Conflict("target snapshot이 바뀌었습니다.")
    before_material = _audit_state(target)
    consume_reauthentication_proof(
        request=request,
        proof_id=data["reauthProofId"],
        action_scope="credential_disconnect",
        entity_type="publication_target",
        entity_id=target.id,
    )
    decision = TargetDisconnectDecision.objects.create(
        target=target,
        expected_target_snapshot_id=data["expectedTargetSnapshotId"],
        expected_target_config_hash=data["expectedTargetConfigHash"],
        request_key=data["requestKey"],
        request_hash=request_hash,
        reauth_proof_id=data["reauthProofId"],
        reason=data["reason"],
        decided_by=user,
    )
    target.auto_publish_enabled = False
    target.connection_state = PublicationTarget.ConnectionState.REVOKED
    target.save()
    _snapshot_locked(target)
    _stale_locked_intents(locked_intents)
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.disconnected",
        entity=target,
        identity_key=f"target-disconnect:{decision.id}",
        before_material=before_material,
        after_material=_audit_state(target),
        metadata={
            "target_id": str(target.id),
            "decision_id": str(decision.id),
            "request_hash": request_hash,
            "result": "accepted",
            "state": target.connection_state,
            "reauth_proof_id": str(decision.reauth_proof_id),
        },
    )
    _enqueue_event(
        "publishing.target_disconnect.requested",
        {
            "decision_id": str(decision.id),
            "target_id": str(target.id),
        },
        dedupe_key=f"target-disconnect:{decision.id}",
    )
    return decision

def _enqueue_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_version: int = 1,
    dedupe_key: str,
    aggregate_type: str | None = None,
    aggregate_id=None,
    job_id=None,
    available_at=None,
    correlation_id=None,
):
    from wisdome_writer.infrastructure.outbox import enqueue_event

    resolved_id = (
        aggregate_id
        or payload.get("target_id")
        or payload.get("canary_run_id")
        or payload.get("decision_id")
    )
    return enqueue_event(
        event_type=event_type,
        event_version=event_version,
        aggregate_type=aggregate_type
        or (event_type.split(".")[1] if "." in event_type else "publishing"),
        aggregate_id=uuid.UUID(str(resolved_id)),
        job_id=job_id or resolved_id,
        payload=payload,
        dedupe_key=dedupe_key,
        available_at=available_at,
        correlation_id=correlation_id,
    )
