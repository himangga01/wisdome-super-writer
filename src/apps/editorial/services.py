from __future__ import annotations

import json
from dataclasses import dataclass
import uuid
from datetime import timedelta

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from adapters.generators import SourceGroundedTemplateGenerator
from adapters.generators.base import EvidenceInput, GeneratedClaim
from apps.audit.models import AuditEvent
from apps.audit.services import (
    AuditContext,
    audit_event_id,
    record_audit_event,
    require_audit_replay,
    require_worker_event,
)
from apps.collection.models import (
    CollectionRun,
    RecoveryState,
    RunSourceItem,
    RunState,
    SourceItem,
)
from apps.collection.services import project_run_terminal_observation
from apps.evidence.models import EvidenceAsset
from apps.evidence.services import (
    calculate_publishable,
    calculate_review_subject_hash,
    evidence_content_hash,
)
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)
from wisdome_writer.infrastructure.outbox import enqueue_event

from .clustering import (
    article_identity_for_verification,
    generation_manifest_for_verifications,
)
from .models import (
    ArticleEventCluster,
    ArticleRevision,
    Claim,
    ClaimEvidence,
    DraftArticle,
    EditorialPolicySnapshot,
    EventCluster,
    EventClusterVerification,
    GenerationAttempt,
    QualityCheck,
)
from .policies import (
    EditorialPolicy,
    load_editorial_policy,
    resolve_editorial_policy_snapshot,
)
from .quality import evaluate_editorial_quality


_MANUAL_REVISION_NAMESPACE = uuid.UUID(
    "28968123-3e2c-4123-b754-983470383ba2"
)
_GENERATED_REVISION_NAMESPACE = uuid.UUID(
    "d1046238-4249-40f4-976a-ad5fb71d25ce"
)
_BODY_BLOCK_FIELDS = frozenset({"id", "type", "content"})
_BODY_BLOCK_TYPES = frozenset(
    {
        "fact",
        "company_claim",
        "background",
        "interpretation",
        "outlook",
        "caution",
        "sources",
    }
)
_SEMANTIC_ALIASES = {
    "price": ("분양가", "공급가", "금액", "가격"),
    "application_start": ("신청 시작", "접수 시작", "청약 시작"),
    "application_end": ("신청 마감", "접수 마감", "청약 마감"),
    "eligibility": ("자격", "대상", "요건"),
    "regulation": ("규제", "법령", "규정"),
    "export_control": ("수출 통제", "수출통제"),
    "supply_disruption": ("공급 차질", "가동 중단", "생산 중단"),
    "earnings": ("실적", "매출", "영업이익"),
    "mass_production": ("양산", "대량 생산"),
}


@dataclass(frozen=True)
class PublishabilityDecision:
    publishable: bool
    blocking_codes: tuple[str, ...]
    policy_current: bool
    evidence_current: bool
    current_policy_snapshot_id: uuid.UUID | None
    current_policy_material_hash: str | None


def validate_body_blocks(blocks: list[dict]) -> list[dict]:
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("editorial body blocks are not exact")
    normalized = []
    for block in blocks:
        if (
            not isinstance(block, dict)
            or set(block) != _BODY_BLOCK_FIELDS
            or not isinstance(block["id"], str)
            or not block["id"].strip()
            or block["type"] not in _BODY_BLOCK_TYPES
            or not isinstance(block["content"], str)
            or not block["content"].strip()
        ):
            raise ValueError("editorial body blocks are not exact")
        normalized.append(dict(block))
    ids = [row["id"] for row in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("editorial body blocks are not exact")
    return normalized


def _validate_heading_claim_coverage(
    *,
    title: str,
    summary: str,
    blocks: list[dict],
    claims: list[dict],
) -> None:
    block_by_id = {row["id"]: row for row in blocks}
    claim_block_ids = {row["blockId"] for row in claims}
    if (
        "title" not in claim_block_ids
        or "summary" not in claim_block_ids
        or title not in str(block_by_id.get("title", {}).get("content", ""))
        or summary
        not in str(block_by_id.get("summary", {}).get("content", ""))
    ):
        raise ValueError("article title and summary require exact claim coverage")


def deterministic_generated_claim_id(
    claim: GeneratedClaim,
    *,
    claim_scope_id,
) -> str:
    identity_hash = canonical_hash(
        {
            "schemaVersion": "editorial-claim-identity-v1",
            "blockId": claim.block_id,
            "statement": claim.statement,
            "claimType": claim.claim_type,
            "evidenceIds": list(claim.evidence_ids),
            "citationMarker": claim.citation_marker,
            "sourceSpans": dict(claim.source_spans),
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    return str(uuid.uuid5(uuid.UUID(str(claim_scope_id)), identity_hash))


def normalize_generated_claims(
    *,
    claims: tuple[GeneratedClaim, ...],
    evidence_manifest: list[dict],
    policy: EditorialPolicy,
    claim_scope_id,
) -> list[dict]:
    evidence_by_id = {
        str(row["evidenceId"]): row for row in evidence_manifest
    }
    high_impact_fields = {
        str(value).casefold() for value in policy.document["highImpactFields"]
    }
    normalized = []
    for claim in claims:
        if (
            not claim.block_id.strip()
            or not claim.statement.strip()
            or not claim.citation_marker.strip()
        ):
            raise ValueError("generated claim identity is incomplete")
        evidence_ids = list(claim.evidence_ids)
        if (
            not evidence_ids
            or evidence_ids != sorted(evidence_ids)
            or len(evidence_ids) != len(set(evidence_ids))
            or not set(evidence_ids) <= set(evidence_by_id)
        ):
            raise ValueError("generated claim evidence IDs are not canonical")
        if claim.claim_type not in policy.document["claimTypes"]:
            raise ValueError("generated claim type is outside editorial policy")
        claim_type = claim.claim_type
        support = [evidence_by_id[value] for value in evidence_ids]
        if claim_type == "fact" and all(
            row.get("authorityTier") == "primary_corporate"
            for row in support
        ):
            claim_type = "company_claim"
        statement_material = claim.statement.casefold()
        span_material = " ".join(
            str(value).casefold() for _key, value in claim.source_spans
        )
        semantic_values: dict[str, set[str]] = {}
        for row in support:
            for key, value in (row.get("semanticFields") or {}).items():
                semantic_values.setdefault(str(key).casefold(), set()).add(
                    str(value).casefold()
                )
        matches = sorted(
            field
            for field in high_impact_fields
            if field in statement_material
            or any(
                alias.casefold() in statement_material
                or alias.casefold() in span_material
                for alias in _SEMANTIC_ALIASES.get(field, ())
            )
            or any(
                value
                and (
                    value in statement_material
                    or value in span_material
                )
                for value in semantic_values.get(field, set())
            )
        )
        semantic_key = (
            matches[0] if len(matches) == 1 else "unclassified_high_risk"
        )
        high_impact = bool(
            semantic_key == "unclassified_high_risk"
            or semantic_key in high_impact_fields
        )
        actor = claim.actor
        attribution = claim.attribution
        if claim_type == "company_claim":
            corporate_publishers = sorted(
                {
                    str(row.get("publisher") or "").strip()
                    for row in support
                    if row.get("authorityTier") == "primary_corporate"
                    and str(row.get("publisher") or "").strip()
                }
            )
            if actor is None and len(corporate_publishers) == 1:
                actor = corporate_publishers[0]
            if attribution is None and actor:
                attribution = f"{actor} 공식 발표"
        source_spans = dict(claim.source_spans)
        if (
            len(source_spans) != len(claim.source_spans)
            or set(source_spans) != set(evidence_ids)
        ):
            raise ValueError("generated claim source spans are incomplete")
        normalized.append(
            {
                "claimId": deterministic_generated_claim_id(
                    claim,
                    claim_scope_id=claim_scope_id,
                ),
                "blockId": claim.block_id,
                "statement": claim.statement,
                "claimType": claim_type,
                "evidenceIds": evidence_ids,
                "citationMarker": claim.citation_marker,
                "sourceSpans": source_spans,
                "highImpact": high_impact,
                "semanticKey": claim.semantic_key,
                "actor": actor,
                "attribution": attribution,
                "derivedFromClaimIds": list(claim.derived_from_claim_ids),
                "horizon": claim.horizon,
                "uncertaintyNote": claim.uncertainty_note,
            }
        )
        normalized[-1]["semanticKey"] = semantic_key
    identities = [
        (row["blockId"], row["statement"], row["citationMarker"])
        for row in normalized
    ]
    markers = [row["citationMarker"] for row in normalized]
    if (
        len(identities) != len(set(identities))
        or len(markers) != len(set(markers))
    ):
        raise ValueError("generated claim identities are not unique")
    fact_claim_ids = {
        row["claimId"]
        for row in normalized
        if row["claimType"] in {"fact", "company_claim"}
    }
    for row in normalized:
        claim_type = row["claimType"]
        actor = str(row.get("actor") or "").strip()
        attribution = str(row.get("attribution") or "").strip()
        derived = row.get("derivedFromClaimIds") or []
        horizon = str(row.get("horizon") or "").strip()
        uncertainty = str(row.get("uncertaintyNote") or "").strip()
        try:
            derived_ids_are_canonical = all(
                str(uuid.UUID(value)) == value for value in derived
            )
        except (AttributeError, TypeError, ValueError):
            derived_ids_are_canonical = False
        if claim_type == "fact" and any(
            (actor, attribution, derived, horizon, uncertainty)
        ):
            raise ValueError("fact claim contains non-factual type material")
        if claim_type == "company_claim" and (not actor or not attribution):
            raise ValueError("company_claim requires actor and attribution")
        if claim_type == "interpretation" and (
            not isinstance(derived, list)
            or not derived
            or derived != sorted(set(map(str, derived)))
            or not derived_ids_are_canonical
            or not set(map(str, derived)) <= fact_claim_ids
        ):
            raise ValueError("interpretation requires derivedFromClaimIds")
        if claim_type == "outlook" and (
            not actor or not horizon or not uncertainty
        ):
            raise ValueError("outlook requires actor, horizon and uncertaintyNote")
    return normalized


def validate_manual_claim_bindings(
    *,
    blocks: list[dict],
    claim_bindings: list[dict],
    evidence_manifest: list[dict],
    policy: EditorialPolicy,
    claim_scope_id,
) -> list[dict]:
    blocks = validate_body_blocks(blocks)
    block_by_id = {
        str(block.get("id")): block
        for block in blocks
        if isinstance(block, dict)
    }
    if len(block_by_id) != len(blocks) or not blocks:
        raise ValueError("manual revision body blocks are not canonical")
    generated = []
    refs: list[str] = []
    derived_refs: list[tuple[str, ...]] = []
    for binding in claim_bindings:
        if not isinstance(binding, dict):
            raise ValueError("manual claim binding is invalid")
        required = {
            "claimRef",
            "blockId",
            "statement",
            "claimType",
            "evidenceIds",
            "citationMarker",
            "sourceSpans",
            "semanticKey",
            "actor",
            "attribution",
            "derivedFromClaimRefs",
            "horizon",
            "uncertaintyNote",
        }
        if set(binding) != required:
            raise ValueError("manual claim binding fields are not exact")
        claim_ref = str(binding["claimRef"]).strip()
        references = binding["derivedFromClaimRefs"]
        if (
            not claim_ref
            or not isinstance(references, list)
            or any(not isinstance(value, str) or not value.strip() for value in references)
        ):
            raise ValueError("manual claim references are invalid")
        refs.append(claim_ref)
        derived_refs.append(tuple(value.strip() for value in references))
        block = block_by_id.get(str(binding["blockId"]))
        if (
            block is None
            or str(binding["statement"]) not in str(block.get("content", ""))
            or f"[{binding['citationMarker']}]" not in str(block.get("content", ""))
        ):
            raise ValueError("manual claim does not match its body block")
        evidence_ids = binding["evidenceIds"]
        source_spans = binding["sourceSpans"]
        if not isinstance(evidence_ids, list) or not isinstance(source_spans, dict):
            raise ValueError("manual claim evidence binding is invalid")
        generated.append(
            GeneratedClaim(
                block_id=str(binding["blockId"]),
                statement=str(binding["statement"]),
                claim_type=str(binding["claimType"]),
                evidence_ids=tuple(map(str, evidence_ids)),
                citation_marker=str(binding["citationMarker"]),
                source_spans=tuple(
                    sorted(
                        (str(key), str(value))
                        for key, value in source_spans.items()
                    )
                ),
                semantic_key=(
                    str(binding["semanticKey"])
                    if binding["semanticKey"] is not None
                    else None
                ),
                actor=(str(binding["actor"]) if binding["actor"] is not None else None),
                attribution=(
                    str(binding["attribution"])
                    if binding["attribution"] is not None
                    else None
                ),
                derived_from_claim_ids=(),
                horizon=(
                    str(binding["horizon"])
                    if binding["horizon"] is not None
                    else None
                ),
                uncertainty_note=(
                    str(binding["uncertaintyNote"])
                    if binding["uncertaintyNote"] is not None
                    else None
                ),
            )
        )
    if len(refs) != len(set(refs)):
        raise ValueError("manual claim references are not unique")
    claim_ids_by_ref = {
        claim_ref: deterministic_generated_claim_id(
            claim,
            claim_scope_id=claim_scope_id,
        )
        for claim_ref, claim in zip(refs, generated, strict=True)
    }
    factual_refs = {
        claim_ref
        for claim_ref, claim in zip(refs, generated, strict=True)
        if claim.claim_type in {"fact", "company_claim"}
    }
    resolved = []
    for claim, references in zip(generated, derived_refs, strict=True):
        if claim.claim_type == "interpretation":
            if (
                not references
                or tuple(sorted(set(references))) != references
                or not set(references) <= factual_refs
            ):
                raise ValueError(
                    "interpretation requires derivedFromClaimRefs"
                )
        elif references:
            raise ValueError(
                "only interpretation accepts derivedFromClaimRefs"
            )
        resolved.append(
            GeneratedClaim(
                **{
                    **claim.__dict__,
                    "derived_from_claim_ids": tuple(
                        claim_ids_by_ref[value] for value in references
                    ),
                }
            )
        )
    return normalize_generated_claims(
        claims=tuple(resolved),
        evidence_manifest=evidence_manifest,
        policy=policy,
        claim_scope_id=claim_scope_id,
    )


def _hash(value) -> str:
    return canonical_hash(value, schema_version=CANONICAL_HASH_SCHEMA_V1)


def _policy_from_snapshot(snapshot) -> EditorialPolicy:
    return EditorialPolicy(
        document=snapshot.document,
        material_hash=snapshot.material_hash,
        release_document_hash=snapshot.release_document_hash,
        config_hash=snapshot.config_hash,
        implementation_manifest=snapshot.implementation_manifest,
        implementation_manifest_hash=snapshot.implementation_manifest_hash,
    )


def manual_revision_request_hash(
    *,
    article_id,
    base_revision_no: int,
    content_hash: str,
    request_key: str,
    reason: str,
    actor_id,
    normalized_claim_bindings: list[dict],
) -> str:
    return canonical_hash(
        {
            "schema_version": "manual-revision-request-v2",
            "article_id": str(article_id),
            "base_revision_no": base_revision_no,
            "content_hash": content_hash,
            "claim_bindings_hash": _hash(normalized_claim_bindings),
            "request_key": request_key,
            "reason": reason,
            "actor_id": str(actor_id),
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _verification_snapshot(
    rows: list[EventClusterVerification],
    *,
    primary_verification_id=None,
) -> list[dict]:
    return [
        {
            "verificationId": str(row.id),
            "clusterId": str(row.cluster_id),
            "role": (
                "held"
                if row.decision == "held"
                else (
                    "lead"
                    if primary_verification_id is not None
                    and row.id == primary_verification_id
                    else "supporting"
                )
            ),
            "version": row.version,
            "decision": row.decision,
            "articleType": row.article_type,
            "category": row.category,
            "localEventDate": row.local_event_date.isoformat(),
            "evidenceManifestHash": row.evidence_manifest_hash,
            "ruleManifestHash": row.rule_manifest_hash,
            "resultManifestHash": row.result_manifest_hash,
            "policyVersion": row.policy_version,
            "policyHash": row.policy_hash,
        }
        for row in sorted(rows, key=lambda value: str(value.id))
    ]


def _exclusion_snapshot(
    rows: list[EventClusterVerification],
    *,
    using: str,
) -> list[dict]:
    members = []
    run_item_ids = set()
    excluded_evidence_ids = set()
    for verification in rows:
        for member in verification.evidence_manifest:
            if member.get("selectionState") not in {"excluded", "conflicting"}:
                continue
            run_item_ids.add(str(member.get("runSourceItemId")))
            excluded_evidence_ids.update(
                str(row.get("evidenceId"))
                for row in member.get("evidence", [])
                if row.get("evidenceId")
            )
            members.append((verification, member))
    run_items = {
        str(row.id): row
        for row in RunSourceItem.objects.using(using)
        .filter(pk__in=run_item_ids)
        .select_related("source_item")
    }
    excluded_evidence = {
        str(row.id): row
        for row in EvidenceAsset.objects.using(using)
        .filter(pk__in=excluded_evidence_ids)
        .order_by("id")
    }
    if set(excluded_evidence) != excluded_evidence_ids:
        raise ValueError("excluded editorial evidence material is incomplete")
    result = []
    for verification, member in members:
        run_item = run_items.get(str(member.get("runSourceItemId")))
        if run_item is None:
            raise ValueError("excluded editorial source lineage is incomplete")
        source = run_item.source_item
        evidence_ids = sorted(
            str(row["evidenceId"])
            for row in member.get("evidence", [])
            if row.get("evidenceId") is not None
        )
        classification = (
            "duplicate"
            if member.get("role") == "duplicate"
            else member.get("selectionState")
        )
        result.append(
            {
                "verificationId": str(verification.id),
                "clusterId": str(verification.cluster_id),
                "runSourceItemId": str(run_item.id),
                "sourceItemId": str(source.id),
                "selectionState": member.get("selectionState"),
                "classification": classification,
                "resolutionState": (
                    "unresolved"
                    if classification == "conflicting"
                    else "resolved"
                ),
                "reason": member.get("decisionReason"),
                "evidenceIds": evidence_ids,
                "evidenceMaterial": [
                    {
                        "evidenceId": evidence_id,
                        "rightsStatus": excluded_evidence[evidence_id].rights_status,
                        "rightsBasisUrl": excluded_evidence[evidence_id].rights_basis_url,
                        "attributionText": excluded_evidence[evidence_id].attribution_text,
                        "locatorType": excluded_evidence[evidence_id].locator_type,
                        "locator": excluded_evidence[evidence_id].locator,
                        "contentHash": excluded_evidence[evidence_id].evidence_content_hash,
                        "reviewSubjectHash": excluded_evidence[evidence_id].review_subject_hash,
                    }
                    for evidence_id in evidence_ids
                    if evidence_id in excluded_evidence
                ],
                "sourceStatus": source.status,
                "sourceTitle": source.title,
                "sourceUrl": source.canonical_url,
                "publisher": source.publisher,
                "authorityTier": member.get("authorityTier"),
                "originIdentityHash": member.get("originIdentityHash"),
                "independenceGroup": member.get("independenceGroup"),
            }
        )
    return sorted(
        result,
        key=lambda row: (
            row["verificationId"],
            row["classification"],
            row["runSourceItemId"],
        ),
    )


def _evidence_source_text(evidence: EvidenceAsset) -> str:
    if evidence.extracted_text:
        return evidence.extracted_text
    if evidence.structured_data is not None:
        return json.dumps(
            evidence.structured_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return ""


def _frozen_evidence_snapshot(
    *,
    run: CollectionRun,
    evidence_rows: list[EvidenceAsset],
    expected_evidence: dict[str, dict],
) -> list[dict]:
    result = []
    for evidence in evidence_rows:
        expected = expected_evidence[str(evidence.id)]
        evidence.full_clean()
        latest_review_id = (
            evidence.review_decisions.order_by("-decided_at", "-id")
            .values_list("id", flat=True)
            .first()
        )
        if (
            evidence_content_hash(
                text=evidence.extracted_text,
                structured_data=evidence.structured_data,
                checksum=evidence.checksum,
            )
            != evidence.evidence_content_hash
            or calculate_review_subject_hash(evidence)
            != evidence.review_subject_hash
            or calculate_publishable(evidence) is not True
            or evidence.latest_review_decision_id != latest_review_id
        ):
            raise ValueError("editorial evidence validation no longer passes")
        extraction_profile = None
        if evidence.extraction_run_id:
            extraction_profile = evidence.extraction_run.profile_material_hash
        elif evidence.generic_extraction_attempt_id:
            extraction_profile = (
                evidence.generic_extraction_attempt.profile_material_hash
            )
        result.append(
            {
                "evidenceId": str(evidence.id),
                "sourceItemId": str(evidence.source_item_id),
                "runSourceItemId": str(evidence.origin_run_source_item_id),
                "selectionState": "selected",
                "contentHash": evidence.evidence_content_hash,
                "checksum": evidence.checksum,
                "reviewSubjectHash": evidence.review_subject_hash,
                "publishable": evidence.publishable,
                "derivationType": evidence.derivation_type,
                "kind": evidence.kind,
                "parentAssetId": (
                    str(evidence.parent_asset_id)
                    if evidence.parent_asset_id
                    else None
                ),
                "documentExtractionId": (
                    str(evidence.document_extraction_id)
                    if evidence.document_extraction_id
                    else None
                ),
                "extractionRunId": (
                    str(evidence.extraction_run_id)
                    if evidence.extraction_run_id
                    else None
                ),
                "genericExtractionAttemptId": (
                    str(evidence.generic_extraction_attempt_id)
                    if evidence.generic_extraction_attempt_id
                    else None
                ),
                "profileMaterialHash": extraction_profile,
                "extractionMethod": evidence.extraction_method,
                "extractorVersion": evidence.extractor_version,
                "configHash": evidence.extraction_config_hash,
                "resultChecksum": evidence.extraction_result_checksum,
                "validationMode": evidence.validation_mode,
                "confidence": (
                    str(evidence.confidence)
                    if evidence.confidence is not None
                    else None
                ),
                "confidenceDetailHash": _hash(evidence.confidence_detail),
                "calibrationProfileKey": evidence.calibration_profile_key,
                "calibrationProfileVersion": (
                    evidence.calibration_profile_version
                ),
                "calibrationProfileHash": evidence.calibration_profile_hash,
                "locatorType": evidence.locator_type,
                "locator": evidence.locator,
                "rightsStatus": evidence.rights_status,
                "rightsBasisUrl": evidence.rights_basis_url,
                "attributionText": evidence.attribution_text,
                "altText": evidence.alt_text,
                "sourceTitle": evidence.source_item.title,
                "sourceUrl": evidence.source_item.canonical_url,
                "publisher": evidence.source_item.publisher,
                "sourceStatus": evidence.source_item.status,
                "sourceVersionHash": evidence.source_item.source_version_hash,
                "sourceVersionSchema": evidence.source_item.source_version_schema,
                "sourceContentHash": evidence.source_item.content_hash,
                "latestReviewDecisionId": (
                    str(evidence.latest_review_decision_id)
                    if evidence.latest_review_decision_id
                    else None
                ),
                "latestReviewDecision": (
                    evidence.latest_review_decision.decision
                    if evidence.latest_review_decision_id
                    else None
                ),
                "latestReviewDecidedAt": (
                    evidence.latest_review_decision.decided_at.isoformat()
                    if evidence.latest_review_decision_id
                    else None
                ),
                "publishedAt": (
                    evidence.source_item.published_at.isoformat()
                    if evidence.source_item.published_at
                    else None
                ),
                "modifiedAt": (
                    evidence.source_item.modified_at.isoformat()
                    if evidence.source_item.modified_at
                    else None
                ),
                "retrievedAt": evidence.source_item.first_collected_at.isoformat(),
                "freshnessCutoff": run.freshness_cutoff.isoformat(),
                "authorityTier": expected.get("authorityTier"),
                "originIdentityHash": expected.get("originIdentityHash"),
                "independenceGroup": expected.get("independenceGroup"),
                "semanticFields": expected.get("semanticFields") or {},
                "semanticComplete": expected.get("semanticComplete") is True,
                # Evaluation-only source material. The revision manifest hash binds it;
                # retention removes the entire draft envelope together.
                "sourceText": "\n".join(
                    value
                    for value in (
                        evidence.source_item.title.strip(),
                        _evidence_source_text(evidence),
                    )
                    if value
                ),
            }
        )
    return sorted(result, key=lambda row: row["evidenceId"])


def _generated_blocks(generated) -> list[dict]:
    blocks = [
        {
            "id": block.block_id,
            "type": block.block_type,
            "content": block.content,
        }
        for block in generated.body_blocks
    ]
    return validate_body_blocks(blocks)


def _persist_editorial_evaluation(
    *,
    revision: ArticleRevision,
    claim_rows: list[dict],
    evidence_rows: list[EvidenceAsset],
    policy: EditorialPolicy,
    using: str,
) -> None:
    evidence_by_id = {str(row.id): row for row in evidence_rows}
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=revision.body_blocks,
        claims=claim_rows,
        evidence=revision.evidence_manifest,
        exclusions=revision.exclusion_manifest,
        visuals=revision.visual_manifest,
    )
    now = timezone.now()
    for position, material in enumerate(claim_rows):
        subject_hash = _hash(
            {
                "schemaVersion": "editorial-claim-subject-v1",
                **material,
            }
        )
        claim = Claim.objects.using(using).create(
            id=material["claimId"],
            revision=revision,
            block_id=material["blockId"],
            claim_type=material["claimType"],
            text=material["statement"],
            position=position,
            citation_marker=material["citationMarker"],
            high_impact=material["highImpact"],
            risk_level="high" if material["highImpact"] else "normal",
            verification_state=(
                "verified" if report.state == "passed" else "rejected"
            ),
            subject_hash=subject_hash,
        )
        for evidence_id in material["evidenceIds"]:
            frozen = next(
                row
                for row in revision.evidence_manifest
                if row["evidenceId"] == evidence_id
            )
            span = material["sourceSpans"][evidence_id]
            link_material = {
                "schemaVersion": "editorial-claim-evidence-v1",
                "claimSubjectHash": subject_hash,
                "evidence": frozen,
                "relation": "supports",
                "sourceSpanHash": _hash(span),
            }
            ClaimEvidence.objects.using(using).create(
                claim=claim,
                evidence=evidence_by_id[evidence_id],
                relation="supports",
                source_span=span,
                source_span_hash=_hash(span),
                verification_strength="direct",
                checked_at=now,
                frozen_material=link_material,
                frozen_material_hash=_hash(link_material),
            )
    for check in report.checks:
        QualityCheck.objects.using(using).create(
            revision=revision,
            code=check.code,
            check_version=check.version,
            result=check.result,
            score=check.score,
            blocking=check.blocking,
            details=dict(check.details),
            details_hash=_hash(check.details),
        )
    revision.claim_graph_state = (
        "passed" if report.state == "passed" else "blocked"
    )
    revision.quality_state = report.state
    revision.quality_gate_manifest_hash = report.gate_manifest_hash
    revision.quality_report_hash = report.report_hash
    revision.quality_manifest_hash = report.report_hash
    revision.save(
        update_fields=(
            "claim_graph_state",
            "quality_state",
            "quality_gate_manifest_hash",
            "quality_report_hash",
            "quality_manifest_hash",
        ),
        using=using,
    )


def _article_type(topic_code: str) -> str:
    return (
        "housing_notice"
        if topic_code == "housing_subscription"
        else "semiconductor_daily_digest"
    )


def _revision_content_hash(
    *,
    title: str,
    summary: str,
    body_markdown: str,
) -> str:
    return canonical_hash(
        {
            "schema_version": "article-revision-content-v1",
            "title": title,
            "summary": summary,
            "body_markdown": body_markdown,
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _article_material(
    article: DraftArticle,
    revision: ArticleRevision | None,
) -> dict:
    return {
        "schema_version": "article-audit-v1",
        "article_id": str(article.id),
        "article_identity_key": article.article_identity_key,
        "source_run_id": str(article.source_run_id),
        "state": article.state,
        "current_revision_id": (
            str(article.current_revision_id)
            if article.current_revision_id
            else None
        ),
        "revision_no": revision.revision_no if revision else None,
        "revision_content_hash": (
            _revision_content_hash(
                title=revision.title,
                summary=revision.summary,
                body_markdown=revision.body_markdown,
            )
            if revision
            else None
        ),
        "input_manifest_hash": (
            revision.input_manifest_hash if revision else None
        ),
        "claim_manifest_hash": (
            revision.claim_manifest_hash if revision else None
        ),
        "quality_manifest_hash": (
            revision.quality_manifest_hash if revision else None
        ),
        "quality_state": revision.quality_state if revision else None,
    }


def _manual_revision_id(
    *,
    article_id,
    audit_context: AuditContext,
) -> uuid.UUID:
    identity_hash = canonical_hash(
        {
            "schema_version": "manual-revision-identity-v1",
            "article_id": str(article_id),
            "request_key": audit_context.request_key,
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    return uuid.uuid5(_MANUAL_REVISION_NAMESPACE, identity_hash)


def _generated_revision_id(
    *,
    article_id,
    source_event_id,
) -> uuid.UUID:
    identity_hash = canonical_hash(
        {
            "schemaVersion": "generated-revision-identity-v1",
            "articleId": str(article_id),
            "sourceEventId": str(source_event_id),
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    return uuid.uuid5(_GENERATED_REVISION_NAMESPACE, identity_hash)


def _stale_open_publication_intents_locked(
    *,
    revision_id,
    using: str,
) -> int:
    if not transaction.get_connection(using).in_atomic_block:
        raise ValueError("publication intent staleness requires a transaction")
    PublicationIntent = apps.get_model("publishing", "PublicationIntent")
    intents = list(
        PublicationIntent.objects.using(using)
        .select_for_update()
        .filter(article_revision_id=revision_id)
        .order_by("id")
    )
    open_ids = [
        row.id
        for row in intents
        if row.state in {"draft", "awaiting_approval", "approved"}
    ]
    if open_ids:
        PublicationIntent.objects.using(using).filter(pk__in=open_ids).update(
            state="stale"
        )
    return len(open_ids)


def _require_draft_generation_run_active(run: CollectionRun) -> None:
    if (
        run.stop_requested_at is not None
        or run.state
        not in {
            RunState.VALIDATING,
            RunState.DRAFTING,
            RunState.AWAITING_APPROVAL,
        }
    ):
        raise ValueError("collection run is not eligible for draft generation")


def revision_origin_run_id(revision) -> object:
    origin_run_id = getattr(revision, "origin_run_id", None)
    if origin_run_id is None:
        raise ValueError("article revision origin run is missing")
    verification = getattr(revision, "event_verification", None)
    verification_run_id = (
        getattr(verification, "origin_run_id", None)
        if verification is not None
        else None
    )
    if (
        verification_run_id is not None
        and str(verification_run_id) != str(origin_run_id)
    ):
        raise ValueError("article revision origin run lineage is inconsistent")
    return origin_run_id


def build_source_grounded_draft(
    run: CollectionRun,
    *,
    verification: EventClusterVerification,
    verification_rows: list[EventClusterVerification],
    generation_manifest_hash: str,
    audit_context: AuditContext,
    event_topic: str = "editorial.generate_requested",
) -> DraftArticle:
    if audit_context.actor_type != AuditEvent.ActorType.WORKER:
        raise ValueError("draft generation requires worker audit provenance")
    if not audit_context.event_key:
        raise ValueError("worker event key is required")
    alias = audit_context.database_alias
    if run._state.db != alias:
        raise ValueError("collection run and audit database aliases differ")
    if verification._state.db != alias:
        raise ValueError("cluster verification and audit database aliases differ")

    with transaction.atomic(using=alias):
        run = (
            CollectionRun.objects.using(alias)
            .select_for_update()
            .get(pk=run.pk)
        )
        verification = (
            EventClusterVerification.objects.using(alias)
            .select_related("cluster")
            .get(pk=verification.pk)
        )
        if event_topic == "editorial.generate_requested":
            require_worker_event(
                context=audit_context,
                topic=event_topic,
                aggregate_id=verification.id,
                payload_identity={
                    "verification_id": str(verification.id),
                    "run_id": str(run.id),
                    "verification_ids": [
                        str(row.id) for row in verification_rows
                    ],
                    "generation_manifest_hash": generation_manifest_hash,
                },
            )
        else:
            require_worker_event(
                context=audit_context,
                topic=event_topic,
                aggregate_id=run.id,
                payload_identity={"run_id": str(run.id)},
            )
        if verification.decision not in {
            "verified_notice",
            "verified_breaking",
            "daily_digest_candidate",
            "held",
        }:
            raise ValueError("cluster verification is not eligible for generation")
        verification_ids, expected_generation_hash, generation_material = (
            generation_manifest_for_verifications(
                run=run,
                primary_verification=verification,
                verifications=verification_rows,
            )
        )
        if generation_manifest_hash != expected_generation_hash:
            raise ValueError("generation verification manifest hash mismatch")
        identity = article_identity_for_verification(verification)
        article = (
            DraftArticle.objects.using(alias)
            .select_for_update()
            .filter(article_identity_key=identity)
            .first()
        )
        revision_id = (
            _generated_revision_id(
                article_id=article.id,
                source_event_id=audit_context.event_key,
            )
            if article is not None
            else None
        )
        event_revision = (
            ArticleRevision.objects.using(alias)
            .select_for_update(of=("self",))
            .select_related("generation_attempt")
            .filter(pk=revision_id, article=article)
            .first()
            if revision_id is not None
            else None
        )
        if event_revision is not None:
            attempt = event_revision.generation_attempt
            if (
                event_revision.origin_run_id != run.id
                or attempt is None
                or attempt.origin_run_id != run.id
                or attempt.generation_manifest_hash != generation_manifest_hash
                or {
                    str(row.get("verificationId"))
                    for row in event_revision.verification_manifest
                }
                != set(verification_ids)
            ):
                raise ValueError("generated revision replay lineage is inconsistent")
            require_audit_replay(
                context=audit_context,
                action="article.draft_generated",
                entity=event_revision,
                identity_key=audit_context.event_key,
            )
            return article

        if EventClusterVerification.objects.using(alias).filter(
            supersedes_id__in=verification_ids,
        ).exists():
            raise ValueError("generation verification head is no longer current")
        _require_draft_generation_run_active(run)
        policy_snapshot = resolve_editorial_policy_snapshot(
            run.topic_code,
            using=alias,
        )
        policy = _policy_from_snapshot(policy_snapshot)
        run_before_state = run.state
        if run.state == RunState.VALIDATING:
            run.state = RunState.DRAFTING
            run.save(update_fields=["state"], using=alias)
        expected_evidence: dict[str, dict] = {}
        for decision in verification_rows:
            for member in decision.evidence_manifest:
                if member.get("selectionState") not in {"selected", "included"}:
                    continue
                for evidence in member.get("evidence", []):
                    if evidence.get("publishable") is not True:
                        continue
                    evidence_id = str(evidence["evidenceId"])
                    frozen = {
                        **evidence,
                        "runSourceItemId": member.get("runSourceItemId"),
                        "sourceItemId": member.get("sourceItemId"),
                        "authorityTier": member.get("authorityTier"),
                        "originIdentityHash": member.get(
                            "originIdentityHash"
                        ),
                        "independenceGroup": member.get(
                            "independenceGroup"
                        ),
                        "semanticFields": member.get("semanticFields") or {},
                        "semanticComplete": member.get("semanticComplete") is True,
                    }
                    existing_frozen = expected_evidence.setdefault(
                        evidence_id,
                        frozen,
                    )
                    if existing_frozen != frozen:
                        raise ValueError(
                            "frozen generation evidence identity conflicts"
                        )
        evidence_ids = sorted(expected_evidence)
        locked_evidence_ids = {
            str(value)
            for value in EvidenceAsset.objects.using(alias)
            .select_for_update()
            .filter(pk__in=evidence_ids)
            .order_by("id")
            .values_list("id", flat=True)
        }
        if locked_evidence_ids != set(evidence_ids):
            raise ValueError("frozen generation evidence set is incomplete")
        evidence_rows = list(
            EvidenceAsset.objects.using(alias)
            .filter(pk__in=evidence_ids, publishable=True)
            .select_related(
                "source_item",
                "origin_run_source_item",
                "extraction_run",
                "generic_extraction_attempt",
                "document_extraction",
                "latest_review_decision",
            )
            .order_by("source_item__published_at", "id")
        )
        if {str(row.id) for row in evidence_rows} != set(evidence_ids):
            raise ValueError("frozen generation evidence set is incomplete")
        source_item_ids = {row.source_item_id for row in evidence_rows}
        locked_source_ids = {
            value
            for value in SourceItem.objects.using(alias)
            .select_for_update()
            .filter(pk__in=source_item_ids)
            .order_by("id")
            .values_list("id", flat=True)
        }
        if (
            locked_source_ids != source_item_ids
            or SourceItem.objects.using(alias).filter(
                supersedes_id__in=source_item_ids,
            ).exists()
            or any(row.source_item.status != "active" for row in evidence_rows)
        ):
            raise ValueError("generation source lineage is no longer current")
        for row in evidence_rows:
            frozen = expected_evidence[str(row.id)]
            if (
                str(row.origin_run_source_item_id)
                != frozen["runSourceItemId"]
                or str(row.source_item_id) != frozen["sourceItemId"]
                or row.evidence_content_hash != frozen.get("contentHash")
                or row.checksum != frozen.get("checksum")
                or row.review_subject_hash != frozen.get("reviewSubjectHash")
                or calculate_review_subject_hash(row)
                != row.review_subject_hash
            ):
                raise ValueError(
                    "live generation evidence no longer matches frozen verification"
                )
        inputs = [
            EvidenceInput(
                evidence_id=str(row.id),
                title=row.source_item.title,
                url=row.source_item.canonical_url,
                publisher=row.source_item.publisher,
                text=_evidence_source_text(row),
                published_at=(
                    row.source_item.published_at.isoformat()
                    if row.source_item.published_at
                    else None
                ),
                authority_tier=expected_evidence[str(row.id)].get(
                    "authorityTier"
                ),
                semantic_fields=tuple(
                    sorted(
                        str(value)
                        for value in (
                            expected_evidence[str(row.id)].get(
                                "semanticFields"
                            )
                            or {}
                        )
                    )
                ),
            )
            for row in evidence_rows
        ]
        verification_snapshot = _verification_snapshot(
            verification_rows,
            primary_verification_id=verification.id,
        )
        verification_snapshot_hash = _hash(verification_snapshot)
        evidence_snapshot = _frozen_evidence_snapshot(
            run=run,
            evidence_rows=evidence_rows,
            expected_evidence=expected_evidence,
        )
        evidence_snapshot_hash = _hash(evidence_snapshot)
        exclusion_snapshot = _exclusion_snapshot(
            verification_rows,
            using=alias,
        )
        exclusion_snapshot_hash = _hash(exclusion_snapshot)
        article_created = False
        if article is None:
            article, article_created = DraftArticle.objects.using(alias).get_or_create(
                article_identity_key=identity,
                defaults={
                    "topic_code": run.topic_code,
                    "article_type": verification.article_type,
                    "source_run": run,
                    "source_verification": verification,
                },
            )
        if article_created:
            verification_by_id = {
                str(row.id): row for row in verification_rows
            }
            for display_order, verification_id in enumerate(
                verification_ids,
                start=1,
            ):
                row = verification_by_id[verification_id]
                if row.decision == "held":
                    role = "held"
                elif row.id == verification.id:
                    role = "lead"
                else:
                    role = "supporting"
                ArticleEventCluster.objects.using(alias).create(
                    article=article,
                    event_cluster=row.cluster,
                    verification=row,
                    role=role,
                    display_order=display_order,
                    inclusion_reason=row.decision_reason,
                    cluster_snapshot_hash=row.evidence_manifest_hash,
                )

        current_revision = (
            ArticleRevision.objects.using(alias)
            .select_for_update()
            .filter(pk=article.current_revision_id)
            .first()
            if article.current_revision_id
            else None
        )
        before_material = _article_material(article, current_revision)
        pipeline_hash = _hash(
            {
                "schemaVersion": "source-grounded-template-pipeline-v2",
                "generator": "source_grounded_template",
                "generatorVersion": "v3",
                "claimContract": "generated-claim-v2",
                "policyMaterialHash": policy.material_hash,
                "implementationManifestHash": (
                    policy.implementation_manifest_hash
                ),
            }
        )
        input_hash = canonical_hash(
            {
                "schema_version": "generation-input-manifest-v3",
                "generation_manifest": generation_material,
                "verification_manifest_hash": verification_snapshot_hash,
                "editorial_policy": {
                    "key": policy.policy_key,
                    "version": policy.policy_version,
                    "hash": policy.material_hash,
                },
                "evidence_manifest_hash": evidence_snapshot_hash,
                "exclusion_manifest_hash": exclusion_snapshot_hash,
                "generation_pipeline_manifest_hash": pipeline_hash,
            },
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        )
        if current_revision is not None and (
            current_revision.input_manifest_hash == input_hash
            and current_revision.verification_manifest_hash
            == verification_snapshot_hash
            and current_revision.editorial_policy_hash
            == policy.material_hash
            and current_revision.evidence_manifest_hash
            == evidence_snapshot_hash
            and current_revision.exclusion_manifest_hash
            == exclusion_snapshot_hash
            and current_revision.generation_attempt_id is not None
            and current_revision.generation_attempt.generation_pipeline_manifest_hash
            == pipeline_hash
        ):
            replay_audit_id = audit_event_id(
                action="article.draft_generated",
                entity=current_revision,
                identity_key=audit_context.event_key,
            )
            if AuditEvent.objects.using(alias).filter(
                pk=replay_audit_id
            ).exists():
                require_audit_replay(
                    context=audit_context,
                    action="article.draft_generated",
                    entity=current_revision,
                    identity_key=audit_context.event_key,
                )
            else:
                material = _article_material(article, current_revision)
                record_audit_event(
                    context=audit_context,
                    action="article.draft_generated",
                    entity=current_revision,
                    identity_key=audit_context.event_key,
                    material_schema_version="article-audit-v1",
                    before_material=material,
                    after_material=material,
                    metadata={
                        "result": "reused",
                        "state": article.state,
                        "revision_id": str(current_revision.id),
                        "revision_no": current_revision.revision_no,
                        "collection_run_id": str(run.id),
                        "manifest_hash": input_hash,
                        "result_hash": _revision_content_hash(
                            title=current_revision.title,
                            summary=current_revision.summary,
                            body_markdown=current_revision.body_markdown,
                        ),
                    },
                )
            return article
        attempt = GenerationAttempt.objects.using(alias).create(
            article=article,
            origin_run=run,
            generator_version="v3",
            input_manifest_hash=input_hash,
            generation_manifest_hash=generation_manifest_hash,
            editorial_policy_snapshot=policy_snapshot,
            editorial_policy_version=policy.policy_version,
            editorial_policy_hash=policy.material_hash,
            verification_manifest=verification_snapshot,
            verification_manifest_hash=verification_snapshot_hash,
            evidence_manifest=evidence_snapshot,
            evidence_manifest_hash=evidence_snapshot_hash,
            exclusion_manifest=exclusion_snapshot,
            exclusion_manifest_hash=exclusion_snapshot_hash,
            generation_pipeline_manifest_hash=pipeline_hash,
        )
        try:
            if revision_id is None:
                revision_id = _generated_revision_id(
                    article_id=article.id,
                    source_event_id=audit_context.event_key,
                )
            generated = SourceGroundedTemplateGenerator().generate(
                topic=run.topic_code,
                evidence=inputs,
                article_type=article.article_type,
            )
            blocks = _generated_blocks(generated)
            claim_rows = normalize_generated_claims(
                claims=generated.claims,
                evidence_manifest=evidence_snapshot,
                policy=policy,
                claim_scope_id=revision_id,
            )
            claim_hash = _hash(claim_rows)
            _validate_heading_claim_coverage(
                title=generated.title,
                summary=generated.summary,
                blocks=blocks,
                claims=claim_rows,
            )
            content_hash = _hash(
                {
                    "schemaVersion": "article-revision-content-v2",
                    "title": generated.title,
                    "summary": generated.summary,
                    "bodyBlocks": blocks,
                }
            )
            revision = ArticleRevision.objects.using(alias).create(
                id=revision_id,
                article=article,
                origin_run=run,
                revision_no=(
                    current_revision.revision_no + 1
                    if current_revision
                    else 1
                ),
                generation_attempt=attempt,
                base_revision=current_revision,
                event_verification=verification,
                editorial_policy_snapshot=policy_snapshot,
                editorial_policy_version=policy.policy_version,
                editorial_policy_hash=policy.material_hash,
                verification_manifest=verification_snapshot,
                verification_manifest_hash=verification_snapshot_hash,
                evidence_manifest=evidence_snapshot,
                evidence_manifest_hash=evidence_snapshot_hash,
                exclusion_manifest=exclusion_snapshot,
                exclusion_manifest_hash=exclusion_snapshot_hash,
                visual_manifest=[],
                visual_manifest_hash=_hash([]),
                title=generated.title,
                summary=generated.summary,
                body_markdown=generated.body_markdown,
                body_blocks=blocks,
                claim_bindings=claim_rows,
                content_hash=content_hash,
                input_manifest_hash=input_hash,
                claim_manifest_hash=claim_hash,
                quality_manifest_hash=_hash({"state": "pending"}),
                claim_graph_state="running",
                quality_state="pending",
            )
            _persist_editorial_evaluation(
                revision=revision,
                claim_rows=claim_rows,
                evidence_rows=evidence_rows,
                policy=policy,
                using=alias,
            )
            article.current_revision = revision
            article.state = (
                "review_ready"
                if revision.quality_state == "passed"
                else "blocked"
            )
            article.save(
                update_fields=[
                    "current_revision",
                    "state",
                    "updated_at",
                ],
                using=alias,
            )
            attempt.state = "succeeded"
            attempt.output_checksum = content_hash
            attempt.finished_at = timezone.now()
            attempt.save(
                update_fields=["state", "output_checksum", "finished_at"],
                using=alias,
            )
            if revision.quality_state == "passed":
                run.state = RunState.AWAITING_APPROVAL
                run.save(update_fields=["state"], using=alias)
            else:
                now = timezone.now()
                run.state = RunState.FAILED
                run.error_summary = {
                    "stage": "editorial_quality",
                    "code": "editorial_quality_failed",
                    "revisionId": str(revision.id),
                }
                project_run_terminal_observation(
                    run,
                    finished_at=now,
                    stage="editorial_quality",
                    final_state=run.state,
                    affected_count=1,
                    error_code="editorial_quality_failed",
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
                action="article.draft_generated",
                entity=revision,
                identity_key=audit_context.event_key,
                material_schema_version="article-audit-v1",
                before_material={
                    "article": before_material,
                    "run": {
                        "id": str(run.id),
                        "state": run_before_state,
                    },
                },
                after_material={
                    "article": _article_material(article, revision),
                    "run": {
                        "id": str(run.id),
                        "state": run.state,
                    },
                },
                metadata={
                    "result": "generated",
                    "state": article.state,
                    "revision_id": str(revision.id),
                    "revision_no": revision.revision_no,
                    "collection_run_id": str(run.id),
                    "manifest_hash": input_hash,
                    "result_hash": _revision_content_hash(
                        title=revision.title,
                        summary=revision.summary,
                        body_markdown=revision.body_markdown,
                    ),
                },
            )
            return article
        except Exception:
            raise


def create_manual_revision(
    *,
    article_id,
    base_revision_no: int,
    title: str,
    summary: str,
    body_blocks: list[dict] | None = None,
    claim_bindings: list[dict] | None = None,
    body_markdown: str | None = None,
    user,
    audit_context: AuditContext,
) -> tuple[ArticleRevision, bool]:
    if (
        audit_context.actor_type != AuditEvent.ActorType.ADMIN
        or str(audit_context.actor_id) != str(user.pk)
    ):
        raise ValueError(
            "audit actor does not match the revision administrator"
        )
    if not audit_context.request_key:
        raise ValueError("request_key is required")
    if audit_context.reason_code is None:
        raise ValueError("edit reason is required")
    if body_blocks is None or claim_bindings is None:
        raise ValueError(
            "manual revision requires bodyBlocks and claimBindings"
        )
    canonical_markdown = "\n\n".join(
        str(block.get("content", ""))
        for block in body_blocks
        if isinstance(block, dict)
    )
    if body_markdown is not None and body_markdown != canonical_markdown:
        raise ValueError("bodyMarkdown does not match canonical bodyBlocks")

    alias = audit_context.database_alias
    revision_id = _manual_revision_id(
        article_id=article_id,
        audit_context=audit_context,
    )
    content_hash = _hash(
        {
            "schemaVersion": "article-revision-content-v2",
            "title": title,
            "summary": summary,
            "bodyBlocks": body_blocks,
        }
    )
    # Idempotency is scoped to the immutable request payload.  Replays must
    # validate against the stored policy/evidence snapshot before consulting
    # the current release; otherwise a policy rollout would turn an exact
    # retry into a conflict or silently rebind it to new policy material.
    existing_identity = (
        ArticleRevision.objects.using(alias)
        .filter(pk=revision_id)
        .values("article_id", "origin_run_id")
        .first()
    )
    if existing_identity is not None:
        if str(existing_identity["article_id"]) != str(article_id):
            raise ValueError("manual revision request key belongs to another article")
        with transaction.atomic(using=alias):
            CollectionRun.objects.using(alias).select_for_update().get(
                pk=existing_identity["origin_run_id"]
            )
            article = (
                DraftArticle.objects.using(alias)
                .select_for_update()
                .get(pk=article_id)
            )
            existing = (
                ArticleRevision.objects.using(alias)
                .select_for_update()
                .select_related("editorial_policy_snapshot")
                .get(pk=revision_id, article=article)
            )
            replay_policy = _policy_from_snapshot(
                existing.editorial_policy_snapshot
            )
            replay_claims = validate_manual_claim_bindings(
                blocks=body_blocks,
                claim_bindings=claim_bindings,
                evidence_manifest=list(existing.evidence_manifest),
                policy=replay_policy,
                claim_scope_id=revision_id,
            )
            request_hash = manual_revision_request_hash(
                article_id=article_id,
                base_revision_no=base_revision_no,
                content_hash=content_hash,
                normalized_claim_bindings=replay_claims,
                request_key=audit_context.request_key,
                reason=audit_context.reason_code,
                actor_id=user.pk,
            )
            require_audit_replay(
                context=audit_context,
                action="article.revision.created",
                entity=existing,
                identity_key=audit_context.request_key,
                request_hash=request_hash,
            )
            return existing, False

    article_identity = (
        DraftArticle.objects.using(alias)
        .values(
            "source_run_id",
            "topic_code",
            "current_revision_id",
            "current_revision__origin_run_id",
        )
        .get(pk=article_id)
    )
    if article_identity["current_revision_id"] is None:
        raise ValueError("manual revision requires a verified base revision")
    origin_run_id = article_identity["current_revision__origin_run_id"]
    if origin_run_id is None:
        raise ValueError("manual revision origin run is missing")
    with transaction.atomic(using=alias):
        run = (
            CollectionRun.objects.using(alias)
            .select_for_update()
            .get(pk=origin_run_id)
        )
        policy_snapshot = resolve_editorial_policy_snapshot(
            article_identity["topic_code"],
            using=alias,
        )
        article = (
            DraftArticle.objects.using(alias)
            .select_for_update()
            .get(pk=article_id)
        )
        if (
            run.stop_requested_at is not None
            or run.state
            not in {
                RunState.VALIDATING,
                RunState.DRAFTING,
                RunState.AWAITING_APPROVAL,
            }
            or article.state
            in {
                "publishing",
                "published",
                "correction_pending",
                "stopped",
                "failed",
            }
        ):
            raise ValueError("article is not eligible for manual revision")

        previous = (
            ArticleRevision.objects.using(alias)
            .select_for_update()
            .get(pk=article.current_revision_id, article=article)
            if article.current_revision_id
            else None
        )
        current_revision_no = previous.revision_no if previous else 0
        if current_revision_no != base_revision_no:
            raise ValueError("base_revision_no is stale")
        if previous is None or previous.editorial_policy_snapshot_id is None:
            raise ValueError("manual revision requires a verified base revision")
        if previous.origin_run_id != run.id:
            raise ValueError("manual revision origin run lineage changed")
        policy = _policy_from_snapshot(policy_snapshot)
        freshness_cutoff = timezone.now() - timedelta(
            minutes=run.freshness_minutes
        )
        evidence_manifest = [
            {**row, "freshnessCutoff": freshness_cutoff.isoformat()}
            for row in previous.evidence_manifest
        ]
        normalized_claims = validate_manual_claim_bindings(
            blocks=body_blocks,
            claim_bindings=claim_bindings,
            evidence_manifest=evidence_manifest,
            policy=policy,
            claim_scope_id=revision_id,
        )
        request_hash = manual_revision_request_hash(
            article_id=article_id,
            base_revision_no=base_revision_no,
            content_hash=content_hash,
            normalized_claim_bindings=normalized_claims,
            request_key=audit_context.request_key,
            reason=audit_context.reason_code,
            actor_id=user.pk,
        )
        _validate_heading_claim_coverage(
            title=title,
            summary=summary,
            blocks=body_blocks,
            claims=normalized_claims,
        )
        evidence_manifest_hash = _hash(evidence_manifest)
        verification_manifest = list(previous.verification_manifest)
        verification_manifest_hash = _hash(verification_manifest)
        exclusion_manifest = list(previous.exclusion_manifest)
        exclusion_manifest_hash = _hash(exclusion_manifest)
        input_hash = _hash(
            {
                "schemaVersion": "manual-revision-input-v1",
                "baseRevisionId": str(previous.id),
                "verificationManifestHash": verification_manifest_hash,
                "editorialPolicyHash": policy.material_hash,
                "evidenceManifestHash": evidence_manifest_hash,
                "exclusionManifestHash": exclusion_manifest_hash,
                "contentHash": content_hash,
                "claimBindingsHash": _hash(normalized_claims),
            }
        )
        before_material = _article_material(article, previous)
        revision = ArticleRevision.objects.using(alias).create(
            id=revision_id,
            article=article,
            origin_run_id=previous.origin_run_id,
            revision_no=current_revision_no + 1,
            base_revision=previous,
            event_verification=previous.event_verification,
            editorial_policy_snapshot=policy_snapshot,
            editorial_policy_version=policy.policy_version,
            editorial_policy_hash=policy.material_hash,
            verification_manifest=verification_manifest,
            verification_manifest_hash=verification_manifest_hash,
            evidence_manifest=evidence_manifest,
            evidence_manifest_hash=evidence_manifest_hash,
            exclusion_manifest=exclusion_manifest,
            exclusion_manifest_hash=exclusion_manifest_hash,
            visual_manifest=[],
            visual_manifest_hash=_hash([]),
            title=title,
            summary=summary,
            body_markdown=canonical_markdown,
            body_blocks=body_blocks,
            claim_bindings=normalized_claims,
            content_hash=content_hash,
            provenance_kind="admin_edit",
            input_manifest_hash=input_hash,
            claim_manifest_hash=_hash(normalized_claims),
            quality_manifest_hash=_hash({"state": "pending"}),
            claim_graph_state="queued",
            quality_state="pending",
            created_by=user,
        )
        _stale_open_publication_intents_locked(
            revision_id=previous.id,
            using=alias,
        )
        article.current_revision = revision
        article.state = "draft"
        article.save(
            update_fields=[
                "current_revision",
                "state",
                "updated_at",
            ],
            using=alias,
        )
        enqueue_event(
            event_type="editorial.revalidate_requested",
            aggregate_type="article_revision",
            aggregate_id=revision.id,
            job_id=run.id,
            dedupe_key=f"editorial.revalidate_requested:{revision.id}",
            correlation_id=run.correlation_id,
            policy_versions={
                "editorialPolicyVersion": policy.policy_version,
                "editorialPolicyHash": policy.material_hash,
            },
            payload={
                "article_id": str(article.id),
                "article_revision_id": str(revision.id),
                "editorial_policy_snapshot_id": str(policy_snapshot.id),
                "editorial_policy_material_hash": policy.material_hash,
                "verification_manifest_hash": verification_manifest_hash,
                "input_evidence_manifest_hash": evidence_manifest_hash,
                "excluded_material_manifest_hash": exclusion_manifest_hash,
            },
        )
        record_audit_event(
            context=audit_context,
            action="article.revision.created",
            entity=revision,
            identity_key=audit_context.request_key,
            material_schema_version="article-audit-v1",
            before_material=before_material,
            after_material=_article_material(article, revision),
            metadata={
                "request_hash": request_hash,
                "result": "created",
                "state": article.state,
                "revision_id": str(revision.id),
                "revision_no": revision.revision_no,
                "result_hash": content_hash,
            },
        )
        return revision, True


def revalidate_manual_revision_locked(
    *,
    article: DraftArticle,
    revision: ArticleRevision,
    source_event_id,
    lease_generation: int,
    lease_token,
    using: str,
) -> ArticleRevision:
    if article.current_revision_id != revision.id:
        return revision
    if revision.claim_graph_state in {"passed", "blocked"}:
        return revision
    if revision.provenance_kind != "admin_edit":
        raise ValueError("only admin revisions use manual revalidation")
    if (
        revision.origin_run.stop_requested_at is not None
        or revision.origin_run.state
        not in {
            RunState.VALIDATING,
            RunState.DRAFTING,
            RunState.AWAITING_APPROVAL,
        }
    ):
        raise ValueError("manual revalidation run is no longer active")
    if revision_origin_run_id(revision) != revision.origin_run_id:
        raise ValueError("manual revalidation origin run lineage changed")
    if not source_event_id or not lease_token or lease_generation <= 0:
        raise ValueError("manual revalidation requires an outbox lease fence")
    policy = _policy_from_snapshot(revision.editorial_policy_snapshot)
    current_policy = load_editorial_policy(article.topic_code)
    if (
        revision.editorial_policy_version != policy.policy_version
        or revision.editorial_policy_hash != policy.material_hash
        or _hash(policy.document) != policy.config_hash
        or current_policy.document != policy.document
        or current_policy.material_hash != policy.material_hash
        or current_policy.release_document_hash != policy.release_document_hash
        or current_policy.config_hash != policy.config_hash
        or current_policy.implementation_manifest
        != policy.implementation_manifest
        or current_policy.implementation_manifest_hash
        != policy.implementation_manifest_hash
    ):
        raise ValueError("manual revision editorial policy snapshot is invalid")
    evidence_ids = [
        str(row["evidenceId"]) for row in revision.evidence_manifest
    ]
    locked_evidence_ids = {
        str(value)
        for value in EvidenceAsset.objects.using(using)
        .select_for_update()
        .filter(pk__in=evidence_ids)
        .order_by("id")
        .values_list("id", flat=True)
    }
    if locked_evidence_ids != set(evidence_ids):
        raise ValueError("manual revision evidence set is incomplete")
    evidence_rows = list(
        EvidenceAsset.objects.using(using)
        .filter(pk__in=evidence_ids)
        .select_related(
            "source_item",
            "origin_run_source_item",
            "extraction_run",
            "generic_extraction_attempt",
            "document_extraction",
            "latest_review_decision",
        )
        .order_by("id")
    )
    if {str(row.id) for row in evidence_rows} != set(evidence_ids):
        raise ValueError("manual revision evidence set is incomplete")
    expected = {
        str(row["evidenceId"]): {
            "runSourceItemId": row["runSourceItemId"],
            "sourceItemId": row["sourceItemId"],
            "contentHash": row["contentHash"],
            "checksum": row["checksum"],
            "reviewSubjectHash": row["reviewSubjectHash"],
            "authorityTier": row["authorityTier"],
            "originIdentityHash": row["originIdentityHash"],
            "independenceGroup": row["independenceGroup"],
            "semanticFields": row.get("semanticFields") or {},
            "semanticComplete": row.get("semanticComplete") is True,
        }
        for row in revision.evidence_manifest
    }
    observed = _frozen_evidence_snapshot(
        run=article.source_run,
        evidence_rows=evidence_rows,
        expected_evidence=expected,
    )
    observed = [
        {
            **row,
            "freshnessCutoff": next(
                stored["freshnessCutoff"]
                for stored in revision.evidence_manifest
                if stored["evidenceId"] == row["evidenceId"]
            ),
        }
        for row in observed
    ]
    if observed != revision.evidence_manifest:
        raise ValueError("manual revision evidence material is stale")
    revision.claim_graph_state = "running"
    revision.revalidation_event_key = source_event_id
    revision.revalidation_generation = lease_generation
    revision.revalidation_lease_token = lease_token
    revision.save(
        update_fields=(
            "claim_graph_state",
            "revalidation_event_key",
            "revalidation_generation",
            "revalidation_lease_token",
        ),
        using=using,
    )
    _persist_editorial_evaluation(
        revision=revision,
        claim_rows=list(revision.claim_bindings),
        evidence_rows=evidence_rows,
        policy=policy,
        using=using,
    )
    article.state = (
        "review_ready" if revision.quality_state == "passed" else "blocked"
    )
    article.save(update_fields=("state", "updated_at"), using=using)
    return revision


def validate_publishable_projection_material(
    *,
    projection: dict,
    current_policy: EditorialPolicy,
    live_evidence_manifest: list[dict],
    persisted_claims: list[dict],
    persisted_quality_checks: list[dict],
    visuals: list[dict],
) -> None:
    frozen_policy_document = projection.get("policyDocument")
    if (
        projection.get("policyKey") != current_policy.policy_key
        or projection.get("policyVersion") != current_policy.policy_version
        or projection.get("policyHash") != current_policy.material_hash
        or (
            bool(current_policy.release_document_hash)
            and projection.get("releaseDocumentHash")
            != current_policy.release_document_hash
        )
        or (
            bool(current_policy.config_hash)
            and projection.get("configHash") != current_policy.config_hash
        )
        or (
            bool(current_policy.implementation_manifest)
            and projection.get("implementationManifest")
            != current_policy.implementation_manifest
        )
        or (
            bool(current_policy.implementation_manifest_hash)
            and projection.get("implementationManifestHash")
            != current_policy.implementation_manifest_hash
        )
        or frozen_policy_document != current_policy.document
        or (
            bool(current_policy.config_hash)
            and _hash(frozen_policy_document) != current_policy.config_hash
        )
    ):
        raise ValueError("current editorial policy release has changed")
    body_blocks = validate_body_blocks(projection.get("bodyBlocks"))
    evidence_manifest = projection.get("evidenceManifest")
    if (
        not isinstance(evidence_manifest, list)
        or projection.get("evidenceManifestHash") != _hash(evidence_manifest)
        or live_evidence_manifest != evidence_manifest
    ):
        raise ValueError("live evidence material no longer matches revision")
    exclusions = projection.get("exclusionManifest")
    if (
        not isinstance(exclusions, list)
        or projection.get("exclusionManifestHash") != _hash(exclusions)
    ):
        raise ValueError("revision exclusion material is invalid")
    for row in exclusions:
        evidence_ids = row.get("evidenceIds")
        evidence_material = row.get("evidenceMaterial")
        if (
            not isinstance(row, dict)
            or not isinstance(evidence_ids, list)
            or not isinstance(evidence_material, list)
            or evidence_ids != sorted(set(map(str, evidence_ids)))
            or {
                str(value.get("evidenceId"))
                for value in evidence_material
                if isinstance(value, dict)
            }
            != set(evidence_ids)
            or any(
                not isinstance(value, dict)
                or not value.get("rightsStatus")
                or not value.get("locatorType")
                or not isinstance(value.get("locator"), dict)
                or not value.get("locator")
                or not value.get("contentHash")
                or not value.get("reviewSubjectHash")
                for value in evidence_material
            )
        ):
            raise ValueError("revision exclusion evidence material is incomplete")
    claim_bindings = projection.get("claimBindings")
    if (
        not isinstance(claim_bindings, list)
        or projection.get("claimManifestHash") != _hash(claim_bindings)
        or persisted_claims != claim_bindings
    ):
        raise ValueError("persisted claim graph no longer matches revision")
    report = evaluate_editorial_quality(
        policy=current_policy,
        blocks=body_blocks,
        claims=claim_bindings,
        evidence=evidence_manifest,
        exclusions=exclusions,
        visuals=visuals,
    )
    expected_checks = [
        {
            "code": row.code,
            "version": row.version,
            "result": row.result,
            "blocking": row.blocking,
            "score": row.score,
            "details": dict(row.details),
            "detailsHash": _hash(row.details),
        }
        for row in report.checks
    ]
    if (
        report.state != "passed"
        or projection.get("claimGraphState") != "passed"
        or projection.get("qualityState") != "passed"
        or projection.get("qualityGateManifestHash")
        != report.gate_manifest_hash
        or projection.get("qualityReportHash") != report.report_hash
        or sorted(persisted_quality_checks, key=lambda row: row["code"])
        != sorted(expected_checks, key=lambda row: row["code"])
    ):
        raise ValueError("persisted editorial quality projection is invalid")


def _maybe_lock(queryset, *, lock: bool):
    # Callers lock mutable run/article/evidence owners explicitly in order.
    return queryset.select_for_update(of=("self",)) if lock else queryset


def _validate_revision_publishable_by_id(
    *,
    revision_id,
    using: str,
    lock: bool,
    current_policy_override: EditorialPolicy | None = None,
) -> None:
    revision_identity = (
        ArticleRevision.objects.using(using)
        .filter(pk=revision_id)
        .values("article_id", "origin_run_id")
        .get()
    )
    article_id = revision_identity["article_id"]
    run_id = revision_identity["origin_run_id"]
    run = _maybe_lock(
        CollectionRun.objects.using(using),
        lock=lock,
    ).get(pk=run_id)
    article = _maybe_lock(
        DraftArticle.objects.using(using),
        lock=lock,
    ).get(pk=article_id)
    revision = _maybe_lock(
        ArticleRevision.objects.using(using),
        lock=lock,
    ).select_related(
        "editorial_policy_snapshot",
        "event_verification",
    ).get(
        pk=revision_id,
        article=article,
    )
    revision._state.fields_cache["article"] = article
    revision._state.fields_cache["origin_run"] = run
    if revision_origin_run_id(revision) != run.id:
        raise ValueError("article revision origin run lineage changed")
    if (
        run.stop_requested_at is not None
        or run.state
        not in {
            RunState.AWAITING_APPROVAL,
            RunState.PUBLISHING,
            RunState.COMPLETED,
        }
    ):
        raise ValueError("collection run is not eligible for publication")
    if not revision.editorial_policy_snapshot_id:
        raise ValueError("article revision has no editorial policy snapshot")
    current_policy = current_policy_override or load_editorial_policy(
        revision.article.topic_code
    )
    policy_snapshot = revision.editorial_policy_snapshot
    canonical_body_markdown = "\n\n".join(
        row["content"] for row in validate_body_blocks(revision.body_blocks)
    )
    _validate_heading_claim_coverage(
        title=revision.title,
        summary=revision.summary,
        blocks=revision.body_blocks,
        claims=revision.claim_bindings,
    )
    if (
        revision.article.current_revision_id != revision.id
        or policy_snapshot.topic_code != revision.article.topic_code
        or policy_snapshot.policy_version != revision.editorial_policy_version
        or policy_snapshot.material_hash != revision.editorial_policy_hash
        or revision.verification_manifest_hash
        != _hash(revision.verification_manifest)
        or revision.body_markdown != canonical_body_markdown
        or revision.content_hash
        != _hash(
            {
                "schemaVersion": "article-revision-content-v2",
                "title": revision.title,
                "summary": revision.summary,
                "bodyBlocks": revision.body_blocks,
            }
        )
    ):
        raise ValueError("article revision frozen envelope is invalid")
    verification_ids = [
        str(row.get("verificationId"))
        for row in revision.verification_manifest
    ]
    if (
        not verification_ids
        or len(verification_ids) != len(set(verification_ids))
        or any(value == "None" for value in verification_ids)
    ):
        raise ValueError("revision verification manifest is ambiguous")
    cluster_ids = list(
        EventClusterVerification.objects.using(using)
        .filter(pk__in=verification_ids)
        .values_list("cluster_id", flat=True)
    )
    if len(cluster_ids) != len(verification_ids):
        raise ValueError("revision verification manifest is incomplete")
    list(
        _maybe_lock(
            EventCluster.objects.using(using),
            lock=lock,
        )
        .filter(pk__in=cluster_ids)
        .order_by("id")
    )
    verification_rows = list(
        _maybe_lock(
            EventClusterVerification.objects.using(using),
            lock=lock,
        )
        .filter(pk__in=verification_ids)
        .select_related("cluster")
        .order_by("id")
    )
    if (
        {str(row.id) for row in verification_rows} != set(verification_ids)
        or any(row.origin_run_id != run.id for row in verification_rows)
        or _verification_snapshot(
            verification_rows,
            primary_verification_id=revision.event_verification_id,
        )
        != revision.verification_manifest
        or (
            revision.event_verification_id is not None
            and str(revision.event_verification_id) not in verification_ids
        )
        or _exclusion_snapshot(verification_rows, using=using)
        != revision.exclusion_manifest
        or EventClusterVerification.objects.using(using).filter(
            supersedes_id__in=verification_ids,
        ).exists()
    ):
        raise ValueError("live verification material no longer matches revision")
    evidence_ids = [
        str(row.get("evidenceId")) for row in revision.evidence_manifest
    ]
    if (
        not evidence_ids
        or len(evidence_ids) != len(set(evidence_ids))
        or any(value == "None" for value in evidence_ids)
    ):
        raise ValueError("revision evidence manifest is empty or ambiguous")
    locked_evidence_ids = {
        str(value)
        for value in _maybe_lock(
            EvidenceAsset.objects.using(using),
            lock=lock,
        )
        .filter(pk__in=evidence_ids)
        .order_by("id")
        .values_list("id", flat=True)
    }
    if locked_evidence_ids != set(evidence_ids):
        raise ValueError("live revision evidence set is incomplete")
    evidence_rows = list(
        EvidenceAsset.objects.using(using)
        .filter(pk__in=evidence_ids)
        .select_related(
            "source_item",
            "origin_run_source_item",
            "extraction_run",
            "generic_extraction_attempt",
            "document_extraction",
            "latest_review_decision",
        )
        .order_by("id")
    )
    if {str(row.id) for row in evidence_rows} != set(evidence_ids):
        raise ValueError("live revision evidence set is incomplete")
    source_item_ids = {row.source_item_id for row in evidence_rows}
    locked_source_ids = {
        value
        for value in _maybe_lock(
            SourceItem.objects.using(using),
            lock=lock,
        )
        .filter(pk__in=source_item_ids)
        .order_by("id")
        .values_list("id", flat=True)
    }
    if (
        locked_source_ids != source_item_ids
        or SourceItem.objects.using(using).filter(
            supersedes_id__in=source_item_ids,
        ).exists()
        or any(row.source_item.status != "active" for row in evidence_rows)
    ):
        raise ValueError("live source lineage is no longer current")
    current_freshness_cutoff = timezone.now() - timedelta(
        minutes=run.freshness_minutes
    )
    if any(
        max(
            value
            for value in (
                row.source_item.modified_at,
                row.source_item.published_at,
            )
            if value is not None
        )
        < current_freshness_cutoff
        for row in evidence_rows
        if row.source_item.modified_at is not None
        or row.source_item.published_at is not None
    ) or any(
        row.source_item.modified_at is None
        and row.source_item.published_at is None
        for row in evidence_rows
    ):
        raise ValueError("live revision evidence is no longer fresh")
    stored_by_id = {
        str(row["evidenceId"]): row for row in revision.evidence_manifest
    }
    expected = {
        evidence_id: {
            "runSourceItemId": row["runSourceItemId"],
            "sourceItemId": row["sourceItemId"],
            "contentHash": row["contentHash"],
            "checksum": row["checksum"],
            "reviewSubjectHash": row["reviewSubjectHash"],
            "authorityTier": row["authorityTier"],
            "originIdentityHash": row["originIdentityHash"],
            "independenceGroup": row["independenceGroup"],
            "semanticFields": row.get("semanticFields") or {},
            "semanticComplete": row.get("semanticComplete") is True,
        }
        for evidence_id, row in stored_by_id.items()
    }
    live_evidence_manifest = _frozen_evidence_snapshot(
        run=run,
        evidence_rows=evidence_rows,
        expected_evidence=expected,
    )
    live_evidence_manifest = [
        {
            **row,
            "freshnessCutoff": stored_by_id[row["evidenceId"]][
                "freshnessCutoff"
            ],
        }
        for row in live_evidence_manifest
    ]
    persisted_claims = []
    claims = list(
        _maybe_lock(
            revision.claims.using(using),
            lock=lock,
        )
        .prefetch_related("evidence_links")
        .order_by("position", "id")
    )
    binding_by_subject = {
        _hash({"schemaVersion": "editorial-claim-subject-v1", **row}): row
        for row in revision.claim_bindings
    }
    for claim in claims:
        material = binding_by_subject.get(claim.subject_hash)
        if material is None:
            raise ValueError("persisted claim subject is not frozen")
        if (
            str(claim.id) != material["claimId"]
            or claim.block_id != material["blockId"]
            or claim.text != material["statement"]
            or claim.claim_type != material["claimType"]
            or claim.citation_marker != material["citationMarker"]
            or claim.high_impact is not bool(material["highImpact"])
            or claim.risk_level
            != ("high" if material["highImpact"] else "normal")
            or claim.verification_state != "verified"
        ):
            raise ValueError("persisted claim projection is stale")
        links = list(claim.evidence_links.all())
        links_by_id = {str(link.evidence_id): link for link in links}
        if set(links_by_id) != set(material["evidenceIds"]):
            raise ValueError("persisted claim evidence set is stale")
        for evidence_id in material["evidenceIds"]:
            link = links_by_id[evidence_id]
            span = material["sourceSpans"][evidence_id]
            frozen = stored_by_id[evidence_id]
            link_material = {
                "schemaVersion": "editorial-claim-evidence-v1",
                "claimSubjectHash": claim.subject_hash,
                "evidence": frozen,
                "relation": "supports",
                "sourceSpanHash": _hash(span),
            }
            if (
                link.relation != "supports"
                or link.source_span != span
                or link.source_span_hash != _hash(span)
                or link.verification_strength != "direct"
                or link.checked_at is None
                or link.frozen_material != link_material
                or link.frozen_material_hash != _hash(link_material)
            ):
                raise ValueError("persisted claim evidence material is stale")
        persisted_claims.append(material)
    checks = list(
        _maybe_lock(
            revision.quality_checks.using(using),
            lock=lock,
        )
        .order_by("code", "id")
    )
    persisted_quality_checks = [
        {
            "code": row.code,
            "version": row.check_version,
            "result": row.result,
            "blocking": row.blocking,
            "score": row.score,
            "details": row.details,
            "detailsHash": row.details_hash,
        }
        for row in checks
    ]
    visuals = []
    observed_visual_manifest = []
    for placement in (
        _maybe_lock(
            revision.visual_placements.using(using),
            lock=lock,
        )
        .select_related(
            "source_evidence",
            "source_evidence__source_item",
            "visualization",
        )
        .order_by("block_id", "display_order", "id")
    ):
        placement.full_clean()
        visual = {
            "blockId": placement.block_id,
            "evidenceId": (
                str(placement.source_evidence_id)
                if placement.source_evidence_id
                else None
            ),
            "visualizationId": (
                str(placement.visualization_id)
                if placement.visualization_id
                else None
            ),
            "rightsStatus": placement.rights_status_snapshot,
            "rightsBasisUrl": placement.rights_basis_url_snapshot,
            "attributionText": placement.attribution_snapshot,
            "altText": placement.alt_text_snapshot,
            "caption": placement.caption,
            "captionClaimMarker": placement.caption_claim_marker,
            "locator": placement.locator_snapshot,
            "renderProvenance": (
                {
                    "objectKey": placement.render_object_key_snapshot,
                    "objectVersion": placement.render_object_version_snapshot,
                    "checksum": placement.render_checksum_snapshot,
                    "inputManifestHash": (
                        placement.render_input_manifest_hash_snapshot
                    ),
                    "transformHash": (
                        placement.render_transform_hash_snapshot
                    ),
                }
                if placement.visualization_id
                else None
            ),
        }
        presentation_material = {
            "schemaVersion": "editorial-visual-placement-v1",
            **visual,
            "displayOrder": placement.display_order,
        }
        if placement.presentation_hash != _hash(presentation_material):
            raise ValueError("visual placement provenance is stale")
        if placement.source_evidence_id:
            source_id = str(placement.source_evidence_id)
            if source_id not in stored_by_id:
                raise ValueError("visual source is outside frozen evidence set")
            source = placement.source_evidence
            if (
                source.publishable is not True
                or source.rights_status != placement.rights_status_snapshot
                or source.rights_basis_url
                != placement.rights_basis_url_snapshot
                or source.attribution_text != placement.attribution_snapshot
                or source.locator != placement.locator_snapshot
            ):
                raise ValueError("visual evidence rights or locator is stale")
        else:
            render = placement.visualization
            if (
                render is None
                or render.state != "succeeded"
                or not render.object_key
                or not render.object_version
                or not render.checksum
                or render.object_key
                != placement.render_object_key_snapshot
                or render.object_version
                != placement.render_object_version_snapshot
                or render.checksum != placement.render_checksum_snapshot
                or render.input_manifest_hash
                != placement.render_input_manifest_hash_snapshot
                or _hash(render.transform_spec)
                != placement.render_transform_hash_snapshot
            ):
                raise ValueError("visualization output is not complete")
        observed_visual_manifest.append(
            {
                "placementId": str(placement.id),
                **presentation_material,
                "presentationHash": placement.presentation_hash,
            }
        )
        visuals.append(visual)
    if (
        revision.visual_manifest_hash != _hash(revision.visual_manifest)
        or observed_visual_manifest != revision.visual_manifest
    ):
        raise ValueError("visual placement set is outside frozen revision")
    validate_publishable_projection_material(
        projection={
            "policyKey": policy_snapshot.policy_key,
            "policyVersion": revision.editorial_policy_version,
            "policyHash": revision.editorial_policy_hash,
            "policyDocument": policy_snapshot.document,
            "releaseDocumentHash": policy_snapshot.release_document_hash,
            "configHash": policy_snapshot.config_hash,
            "implementationManifest": policy_snapshot.implementation_manifest,
            "implementationManifestHash": policy_snapshot.implementation_manifest_hash,
            "bodyBlocks": revision.body_blocks,
            "claimBindings": revision.claim_bindings,
            "claimManifestHash": revision.claim_manifest_hash,
            "evidenceManifest": revision.evidence_manifest,
            "evidenceManifestHash": revision.evidence_manifest_hash,
            "exclusionManifest": revision.exclusion_manifest,
            "exclusionManifestHash": revision.exclusion_manifest_hash,
            "qualityGateManifestHash": revision.quality_gate_manifest_hash,
            "qualityReportHash": revision.quality_report_hash,
            "qualityState": revision.quality_state,
            "claimGraphState": revision.claim_graph_state,
        },
        current_policy=current_policy,
        live_evidence_manifest=live_evidence_manifest,
        persisted_claims=persisted_claims,
        persisted_quality_checks=persisted_quality_checks,
        visuals=visuals,
    )


def require_revision_publishable(revision: ArticleRevision) -> None:
    using = revision._state.db or "default"
    if not transaction.get_connection(using).in_atomic_block:
        raise ValueError(
            "publishability validation must share the publication transaction"
        )
    _validate_revision_publishable_by_id(
        revision_id=revision.id,
        using=using,
        lock=True,
    )


def evaluate_revision_publishability_readonly(
    *,
    revision_id,
    using: str = "default",
) -> PublishabilityDecision:
    blocking_codes: list[str] = []
    current_policy = None
    current_snapshot_id = None
    current_material_hash = None
    policy_current = False
    evidence_current = False
    try:
        revision = (
            ArticleRevision.objects.using(using)
            .select_related("article", "editorial_policy_snapshot")
            .get(pk=revision_id)
        )
        current_policy = load_editorial_policy(revision.article.topic_code)
        current_material_hash = current_policy.material_hash
        current_snapshot_id = (
            EditorialPolicySnapshot.objects.using(using)
            .filter(
                topic_code=revision.article.topic_code,
                policy_key=current_policy.policy_key,
                policy_version=current_policy.policy_version,
                release_document_hash=current_policy.release_document_hash,
                config_hash=current_policy.config_hash,
                implementation_manifest_hash=(
                    current_policy.implementation_manifest_hash
                ),
                material_hash=current_policy.material_hash,
            )
            .values_list("id", flat=True)
            .first()
        )
        snapshot = revision.editorial_policy_snapshot
        policy_current = bool(
            current_snapshot_id is not None
            and snapshot.id == current_snapshot_id
            and snapshot.document == current_policy.document
            and snapshot.implementation_manifest
            == current_policy.implementation_manifest
        )
    except Exception:
        blocking_codes.append("editorial_policy_not_current")
        return PublishabilityDecision(
            publishable=False,
            blocking_codes=tuple(blocking_codes),
            policy_current=False,
            evidence_current=False,
            current_policy_snapshot_id=None,
            current_policy_material_hash=current_material_hash,
        )

    if not policy_current:
        blocking_codes.append("editorial_policy_not_current")
    quality_terms = (
        "quality",
        "claim",
        "body block",
        "heading",
        "visual placement",
        "visualization",
    )
    try:
        _validate_revision_publishable_by_id(
            revision_id=revision_id,
            using=using,
            lock=False,
            # Evaluate evidence/projection currency independently from release
            # currency.  The caller already receives policy_current above.
            current_policy_override=_policy_from_snapshot(snapshot),
        )
        evidence_current = True
    except Exception as exc:
        reason = str(exc).lower()
        quality_blocked = (
            revision.claim_graph_state != "passed"
            or revision.quality_state != "passed"
            or any(term in reason for term in quality_terms)
        )
        if quality_blocked:
            # Publication validation reaches the live evidence/lineage checks
            # before persisted claim/quality projections, so a typed quality
            # failure does not make evidence_current false.
            evidence_current = True
            blocking_codes.append("editorial_quality_not_publishable")
        else:
            evidence_current = False
            blocking_codes.append("editorial_evidence_not_current")
        return PublishabilityDecision(
            publishable=False,
            blocking_codes=tuple(dict.fromkeys(blocking_codes)),
            policy_current=policy_current,
            evidence_current=evidence_current,
            current_policy_snapshot_id=current_snapshot_id,
            current_policy_material_hash=current_material_hash,
        )
    return PublishabilityDecision(
        publishable=policy_current,
        blocking_codes=() if policy_current else tuple(blocking_codes),
        policy_current=policy_current,
        evidence_current=True,
        current_policy_snapshot_id=current_snapshot_id,
        current_policy_material_hash=current_material_hash,
    )


def terminalize_manual_revision_locked(
    *,
    article: DraftArticle,
    revision: ArticleRevision,
    error_code: str,
    using: str,
) -> ArticleRevision:
    if article.current_revision_id != revision.id:
        return revision
    if revision.claim_graph_state in {"passed", "blocked"}:
        return revision
    details = {"errorCode": str(error_code)[:100]}
    for policy_check in revision.editorial_policy_snapshot.document["checks"]:
        QualityCheck.objects.using(using).create(
            revision=revision,
            code=policy_check["code"],
            check_version=policy_check["version"],
            result="failed",
            blocking=True,
            details=details,
            details_hash=_hash(details),
        )
    report_material = [
        {
            "code": row["code"],
            "version": row["version"],
            "result": "failed",
            "blocking": True,
            "details": details,
        }
        for row in revision.editorial_policy_snapshot.document["checks"]
    ]
    revision.claim_graph_state = "blocked"
    revision.quality_state = "failed"
    revision.quality_gate_manifest_hash = _hash(
        revision.editorial_policy_snapshot.document["checks"]
    )
    revision.quality_report_hash = _hash(report_material)
    revision.quality_manifest_hash = revision.quality_report_hash
    revision.save(
        update_fields=(
            "claim_graph_state",
            "quality_state",
            "quality_gate_manifest_hash",
            "quality_report_hash",
            "quality_manifest_hash",
        ),
        using=using,
    )
    if article.state not in {
        "publishing",
        "published",
        "correction_pending",
        "stopped",
        "failed",
    }:
        article.state = "blocked"
        article.save(update_fields=("state", "updated_at"), using=using)
    return revision
