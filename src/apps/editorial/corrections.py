from __future__ import annotations

import re
import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.accounts.services import consume_reauthentication_proof
from apps.audit.services import (
    AuditContext,
    record_audit_event,
    require_audit_replay,
)
from apps.collection.models import (
    RunSourceItem,
    SourceDiscoveryKind,
    SourceItem,
    SourceItemStatus,
)
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)

from .models import (
    ArticleRevision,
    CorrectionCase,
    CorrectionDecision,
    DraftArticle,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _hash(value) -> str:
    return canonical_hash(
        value,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def correction_decision_request_hash(
    *,
    case_id: uuid.UUID | str,
    decision: str,
    subject_hash: str,
    diff_manifest_hash: str,
    corrected_revision_id: uuid.UUID | str | None,
    expected_latest_decision_id: uuid.UUID | str | None,
    expected_decision_version: int,
    request_key: str,
    reason: str,
    actor_id: uuid.UUID | str,
    reauth_proof_id: uuid.UUID | str,
) -> str:
    return _hash(
        {
            "schemaVersion": "correction-decision-request-v1",
            "correctionCaseId": str(uuid.UUID(str(case_id))),
            "decision": str(decision).strip(),
            "subjectHash": str(subject_hash).strip(),
            "diffManifestHash": str(diff_manifest_hash).strip(),
            "correctedRevisionId": (
                str(uuid.UUID(str(corrected_revision_id)))
                if corrected_revision_id is not None
                else None
            ),
            "expectedLatestDecisionId": (
                str(uuid.UUID(str(expected_latest_decision_id)))
                if expected_latest_decision_id is not None
                else None
            ),
            "expectedDecisionVersion": int(expected_decision_version),
            "requestKey": str(request_key).strip(),
            "reason": str(reason).strip(),
            "actorId": str(uuid.UUID(str(actor_id))),
            "reauthProofId": str(uuid.UUID(str(reauth_proof_id))),
        }
    )


def validate_corrected_revision(
    *,
    case: CorrectionCase,
    revision: ArticleRevision,
) -> None:
    article = case.article
    if (
        revision.article_id != case.article_id
        or article.current_revision_id != revision.id
        or revision.claim_graph_state != "passed"
        or revision.quality_state != "passed"
    ):
        raise ValueError("corrected revision is not the current passed article revision")
    source_item_ids: set[str] = set()

    def collect_source_ids(value: Any) -> None:
        if isinstance(value, dict):
            source_id = value.get("sourceItemId")
            if source_id:
                source_item_ids.add(str(source_id))
            for child in value.values():
                collect_source_ids(child)
        elif isinstance(value, list):
            for child in value:
                collect_source_ids(child)

    collect_source_ids(getattr(revision, "evidence_manifest", []))
    collect_source_ids(getattr(revision, "exclusion_manifest", []))
    collect_source_ids(getattr(revision, "verification_manifest", []))
    if str(case.source_item_id) not in source_item_ids:
        raise ValueError("corrected revision does not include the changed source")
    from .services import require_revision_publishable

    require_revision_publishable(revision)


def _decision_material(decision: CorrectionDecision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "id": str(decision.id),
        "decision": decision.decision,
        "subjectHash": decision.subject_hash,
        "diffManifestHash": decision.diff_manifest_hash,
        "correctedRevisionId": (
            str(decision.corrected_revision_id)
            if decision.corrected_revision_id
            else None
        ),
        "headVersion": decision.head_version,
        "supersedesDecisionId": (
            str(decision.supersedes_id) if decision.supersedes_id else None
        ),
        "requestHash": decision.request_hash,
    }


def _require_correction_actor(user: Any) -> None:
    if (
        user is None
        or not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
        or not getattr(user, "is_staff", False)
        or getattr(user, "pk", None) is None
    ):
        raise ValueError("an active staff administrator is required")


@transaction.atomic
def decide_correction_case(
    *,
    case_id: uuid.UUID | str,
    decision: str,
    expected_subject_hash: str,
    expected_diff_manifest_hash: str,
    corrected_revision_id: uuid.UUID | str | None,
    expected_latest_decision_id: uuid.UUID | str | None,
    expected_decision_version: int,
    request_key: str,
    reason: str,
    reauth_proof_id: uuid.UUID | str,
    user: Any,
    request: Any,
    audit_context: AuditContext,
) -> tuple[CorrectionDecision, bool]:
    _require_correction_actor(user)
    normalized_decision = str(decision).strip()
    if normalized_decision not in CorrectionDecision.Decision.values:
        raise ValueError("correction decision is invalid")
    normalized_subject = str(expected_subject_hash).strip()
    normalized_diff = str(expected_diff_manifest_hash).strip()
    normalized_key = str(request_key).strip()
    normalized_reason = str(reason).strip()
    if (
        not _SHA256_RE.fullmatch(normalized_subject)
        or not _SHA256_RE.fullmatch(normalized_diff)
        or not (8 <= len(normalized_key) <= 200)
        or not (3 <= len(normalized_reason) <= 500)
        or audit_context.request_key != normalized_key
        or audit_context.reason_code != normalized_reason
    ):
        raise ValueError("correction decision material is invalid")
    try:
        normalized_version = int(expected_decision_version)
        if normalized_version < 0:
            raise ValueError
        normalized_case_id = uuid.UUID(str(case_id))
        normalized_revision_id = (
            uuid.UUID(str(corrected_revision_id))
            if corrected_revision_id is not None
            else None
        )
        normalized_expected_id = (
            uuid.UUID(str(expected_latest_decision_id))
            if expected_latest_decision_id is not None
            else None
        )
        normalized_proof_id = uuid.UUID(str(reauth_proof_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("correction decision identity is invalid") from exc
    if (
        normalized_decision == CorrectionDecision.Decision.VERIFIED
        and normalized_revision_id is None
    ) or (
        normalized_decision == CorrectionDecision.Decision.REJECTED
        and normalized_revision_id is not None
    ):
        raise ValueError("correction decision revision binding is invalid")

    request_hash = correction_decision_request_hash(
        case_id=normalized_case_id,
        decision=normalized_decision,
        subject_hash=normalized_subject,
        diff_manifest_hash=normalized_diff,
        corrected_revision_id=normalized_revision_id,
        expected_latest_decision_id=normalized_expected_id,
        expected_decision_version=normalized_version,
        request_key=normalized_key,
        reason=normalized_reason,
        actor_id=user.pk,
        reauth_proof_id=normalized_proof_id,
    )
    case = (
        # Retain the mutable article head lock without locking nullable joins.
        CorrectionCase.objects.select_for_update(of=("self", "article"))
        .select_related(
            "article",
            "source_item",
            "prior_source_item",
            "latest_decision",
        )
        .get(pk=normalized_case_id)
    )
    existing = (
        CorrectionDecision.objects.filter(
            correction_case=case,
            request_key=normalized_key,
        )
        .select_related("corrected_revision")
        .first()
    )
    if existing is not None:
        if (
            existing.request_hash != request_hash
            or existing.decided_by_id != user.pk
            or existing.reauth_proof_id != normalized_proof_id
        ):
            raise ValueError("correction request key is bound to different material")
        require_audit_replay(
            context=audit_context,
            action="correction_case.decided",
            entity=existing,
            identity_key=f"correction-decision:{existing.id}",
            request_hash=request_hash,
        )
        return existing, False

    current = case.latest_decision
    if (
        case.decision_version != normalized_version
        or (current.id if current else None) != normalized_expected_id
    ):
        raise ValueError("correction decision head is stale")
    if normalized_subject != case.subject_hash or normalized_diff != _hash(
        case.diff_summary
    ):
        raise ValueError("correction case material is stale")
    if case.state in {
        CorrectionCase.State.APPLYING,
        CorrectionCase.State.COMPLETED,
        CorrectionCase.State.FAILED,
    }:
        raise ValueError("correction case can no longer be decided")
    if current is not None and current.decision == normalized_decision:
        raise ValueError("a new request cannot repeat the current correction decision")

    revision = None
    if normalized_revision_id is not None:
        revision = ArticleRevision.objects.get(pk=normalized_revision_id)
        validate_corrected_revision(case=case, revision=revision)

    consume_reauthentication_proof(
        request=request,
        proof_id=normalized_proof_id,
        action_scope="correction_decision",
        entity_type="correction_case",
        entity_id=case.id,
    )
    before_material = _decision_material(current)
    row = CorrectionDecision.objects.create(
        correction_case=case,
        decision=normalized_decision,
        subject_hash=normalized_subject,
        diff_manifest_hash=normalized_diff,
        corrected_revision=revision,
        supersedes=current,
        head_version=normalized_version + 1,
        request_key=normalized_key,
        request_hash=request_hash,
        reauth_proof_id=normalized_proof_id,
        decision_reason=normalized_reason,
        decided_by=user,
    )
    case.latest_decision = row
    case.decision_version = row.head_version
    case.corrected_revision = revision
    case.failure_summary = {}
    if normalized_decision == CorrectionDecision.Decision.VERIFIED:
        case.state = CorrectionCase.State.VERIFIED
        case.verified_at = row.decided_at or timezone.now()
        case.dispatched_at = None
        case.completed_at = None
    else:
        case.state = CorrectionCase.State.REJECTED
        case.verified_at = None
        case.dispatched_at = None
        case.completed_at = row.decided_at or timezone.now()
    case.save(
        update_fields=(
            "latest_decision",
            "decision_version",
            "corrected_revision",
            "state",
            "verified_at",
            "dispatched_at",
            "completed_at",
            "failure_summary",
        )
    )
    record_audit_event(
        context=audit_context,
        action="correction_case.decided",
        entity=row,
        identity_key=f"correction-decision:{row.id}",
        material_schema_version="correction-decision-v1",
        before_material=before_material,
        after_material=_decision_material(row),
        metadata={
            "correction_case_id": str(case.id),
            "decision_id": str(row.id),
            "decision": row.decision,
            "head_version": row.head_version,
            "request_hash": row.request_hash,
            "corrected_revision_id": (
                str(row.corrected_revision_id)
                if row.corrected_revision_id
                else None
            ),
        },
    )
    return row, True


def _create_cases_for_change(
    *,
    item: SourceItem,
    prior_item: SourceItem,
    evidence_item: SourceItem | None = None,
    article_ids=None,
    kind_override: str | None = None,
) -> list[CorrectionCase]:
    cases: list[CorrectionCase] = []
    if article_ids is None:
        article_ids = (
            DraftArticle.objects.filter(
                revisions__claims__evidence_links__evidence__source_item=(
                    evidence_item or prior_item
                )
            )
            .values_list("id", flat=True)
            .distinct()
        )
    kind = kind_override or {
        SourceItemStatus.RETRACTED: "retraction",
        SourceItemStatus.UNAVAILABLE: "source_unavailable",
    }.get(item.status, "correction")
    subject_hash = _hash(
        {
            "schemaVersion": "source-correction-subject-v1",
            "source": str(item.source_id),
            "externalId": item.external_id,
            "old": prior_item.source_version_hash,
            "new": item.source_version_hash,
            "status": item.status,
            "kind": kind,
        }
    )
    for article_id in article_ids:
        supersedes = (
            CorrectionCase.objects.filter(
                article_id=article_id,
                source_item__source_id=item.source_id,
                source_item__external_id=item.external_id,
            )
            .exclude(subject_hash=subject_hash)
            .order_by("-detected_at", "-id")
            .first()
        )
        case, created = CorrectionCase.objects.get_or_create(
            article_id=article_id,
            subject_hash=subject_hash,
            defaults={
                "source_item": item,
                "prior_source_item": prior_item,
                "kind": kind,
                "supersedes": supersedes,
                "diff_summary": {
                    "oldContentHash": prior_item.content_hash,
                    "newContentHash": item.content_hash,
                    "oldSourceVersionHash": prior_item.source_version_hash,
                    "newSourceVersionHash": item.source_version_hash,
                    "priorSourceUrl": prior_item.canonical_url,
                    "currentSourceUrl": item.canonical_url,
                },
            },
        )
        cases.append(case)
    return cases


def _prior_observations(
    observation: RunSourceItem,
) -> list[RunSourceItem]:
    observations: list[RunSourceItem] = []
    seen = {observation.id}
    previous_id = observation.previous_run_source_item_id
    while previous_id is not None:
        if previous_id in seen or len(observations) >= 10000:
            raise ValueError(
                "Source observation lineage is cyclic or exceeds its bound."
            )
        seen.add(previous_id)
        previous = (
            RunSourceItem.objects.select_related("source_item")
            .get(pk=previous_id)
        )
        if (
            previous.source_item.source_id
            != observation.source_item.source_id
            or previous.source_item.external_id
            != observation.source_item.external_id
        ):
            raise ValueError(
                "Source observation lineage crosses stable identity."
            )
        observations.append(previous)
        previous_id = previous.previous_run_source_item_id
    return observations


def _lineage_article_ids(
    prior_observations: list[RunSourceItem],
) -> list:
    evidence_item_ids = [
        prior.source_item_id
        for prior in prior_observations
        if prior.source_item.status
        in {
            SourceItemStatus.ACTIVE,
            SourceItemStatus.CORRECTED,
        }
    ]
    if not evidence_item_ids:
        return []
    return list(
        DraftArticle.objects.filter(
            revisions__claims__evidence_links__evidence__source_item_id__in=(
                evidence_item_ids
            )
        )
        .values_list("id", flat=True)
        .distinct()
    )


@transaction.atomic
def detect_correction_cases() -> list[CorrectionCase]:
    created_cases: list[CorrectionCase] = []
    changed = SourceItem.objects.filter(supersedes__isnull=False).select_related("supersedes")
    for item in changed:
        created_cases.extend(
            _create_cases_for_change(
                item=item,
                prior_item=item.supersedes,
            )
        )
    return created_cases


@transaction.atomic
def detect_correction_cases_for_observation(
    observation: RunSourceItem,
) -> list[CorrectionCase]:
    is_terminal = observation.source_item.status in {
        SourceItemStatus.RETRACTED,
        SourceItemStatus.UNAVAILABLE,
    }
    is_restored = (
        observation.discovery_kind == "restored"
        and observation.source_item.status
        == SourceItemStatus.ACTIVE
    )
    is_corrected = observation.discovery_kind in {
        SourceDiscoveryKind.CORRECTED,
        SourceDiscoveryKind.NEW_VERSION,
    }
    if not is_terminal and not is_restored and not is_corrected:
        return []
    prior_observations = _prior_observations(observation)
    if not prior_observations:
        return []
    article_ids = _lineage_article_ids(prior_observations)
    if not article_ids:
        return []
    prior_item = prior_observations[0].source_item
    return _create_cases_for_change(
        item=observation.source_item,
        prior_item=prior_item,
        article_ids=article_ids,
        kind_override=("restoration" if is_restored else None),
    )
