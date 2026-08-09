from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from django.core.exceptions import ValidationError

from apps.evidence.models import validate_evidence_locator
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)

from .policies import (
    EditorialPolicy,
    REQUIRED_GATE_CODES,
    VISUAL_GATE_CODE,
)


_GROUNDED = "all_publishable_claims_grounded"
_HIGH_RISK = "high_risk_verification_satisfied"
_INDEPENDENCE = "claim_independence_satisfied"
_FRESHNESS = "source_freshness_satisfied"
_ELIGIBILITY = "evidence_publish_eligibility_current"
_QUOTATION = "quotation_limits_satisfied"
_TYPE_SEPARATION = "claim_types_separated_and_attributed"
_DUPLICATE = "duplicate_or_conflict_resolved"
_READABILITY = "korean_readability_and_repetition"
_EXAGGERATION = "no_exaggeration_or_false_experience"


@dataclass(frozen=True)
class EditorialQualityCheck:
    code: str
    version: str
    result: str
    blocking: bool
    score: float | None
    details: Mapping[str, Any]


@dataclass(frozen=True)
class EditorialQualityReport:
    state: str
    checks: tuple[EditorialQualityCheck, ...]
    gate_manifest_hash: str
    report_hash: str


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _result(code, policy, passed, *, details, score=None):
    check = policy.check(code)
    return EditorialQualityCheck(
        code=code,
        version=str(check["version"]),
        result="passed" if passed else "failed",
        blocking=bool(check["blocking"]),
        score=score,
        details=details,
    )


def _atomic_factual_sentences(content: object) -> list[str]:
    text = str(content or "")
    lines = [
        re.sub(r"^(?:#{1,6}|[-*+])\s+", "", line.strip())
        for line in text.splitlines()
    ]
    lines = [line for line in lines if line not in {"---", "***", "___"}]
    prose = " ".join(
        line for line in lines
        if line
    )
    prose = re.sub(r"\s*\[[A-Za-z0-9_-]+\]", "", prose)
    return [
        value.strip()
        for value in re.split(r"(?<=[.!?。])\s+", prose)
        if value.strip()
    ]


def evaluate_editorial_quality(
    *,
    policy: EditorialPolicy,
    blocks: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    exclusions: Sequence[Mapping[str, Any]],
    visuals: Sequence[Mapping[str, Any]],
) -> EditorialQualityReport:
    by_evidence = {str(row.get("evidenceId")): row for row in evidence}
    block_by_id = {str(row.get("id")): row for row in blocks}
    linked_ids = {
        str(evidence_id)
        for claim in claims
        for evidence_id in claim.get("evidenceIds", [])
    }
    high_impact_fields = {
        str(value).casefold() for value in policy.document["highImpactFields"]
    }
    source_block_text = "\n".join(
        str(row.get("content", ""))
        for row in blocks
        if row.get("type") == "sources"
    )

    def source_entry_is_exact(claim: Mapping[str, Any]) -> bool:
        marker = str(claim.get("citationMarker") or "").strip()
        source_lines = {
            line.strip() for line in source_block_text.splitlines()
            if line.strip()
        }
        for evidence_id in claim.get("evidenceIds", []):
            row = by_evidence.get(str(evidence_id), {})
            expected = (
                f"- [{marker}] [{row.get('sourceTitle')}]"
                f"({row.get('sourceUrl')}) · {row.get('publisher')}"
            )
            if not any(
                line == expected or line.startswith(expected + " · ")
                for line in source_lines
            ):
                return False
        return bool(marker)

    def is_high_impact(claim: Mapping[str, Any]) -> bool:
        semantic_key = str(claim.get("semanticKey", "")).casefold()
        statement = str(claim.get("statement", "")).casefold()
        locator_material = " ".join(
            str(by_evidence.get(str(value), {}).get("locator", {})).casefold()
            for value in claim.get("evidenceIds", [])
        )
        explicit = claim.get("highImpact")
        if explicit is True:
            return True
        return bool(
            not semantic_key
            or semantic_key == "unclassified_high_risk"
            or semantic_key in high_impact_fields
            or any(
                field in statement or field in locator_material
                for field in high_impact_fields
            )
        )
    claims_by_block: dict[str, list[Mapping[str, Any]]] = {}
    for claim in claims:
        claims_by_block.setdefault(str(claim.get("blockId")), []).append(claim)
    uncovered_fact_assertions = sorted(
        f"{row.get('id')}:{sentence}"
        for row in blocks
        if row.get("type") != "sources"
        for sentence in _atomic_factual_sentences(row.get("content"))
        if sentence
        not in {
            str(claim.get("statement", "")).strip()
            for claim in claims_by_block.get(str(row.get("id")), [])
        }
    )
    title_bindings_valid = all(
        claim.get("blockId") != "title"
        or any(
            str(claim.get("statement", "")).strip()
            == str(by_evidence.get(str(evidence_id), {}).get("sourceTitle", "")).strip()
            and str(claim.get("sourceSpans", {}).get(str(evidence_id), "")).strip()
            == str(by_evidence.get(str(evidence_id), {}).get("sourceTitle", "")).strip()
            for evidence_id in claim.get("evidenceIds", [])
        )
        for claim in claims
    )
    claim_id_values = [str(claim.get("claimId") or "") for claim in claims]
    try:
        canonical_claim_ids = all(
            str(uuid.UUID(value)) == value
            for value in claim_id_values
        )
    except (AttributeError, TypeError, ValueError):
        canonical_claim_ids = False
    claim_ids_valid = (
        canonical_claim_ids
        and len(claim_id_values) == len(set(claim_id_values))
        and not uncovered_fact_assertions
        and title_bindings_valid
        and all(
        isinstance(claim.get("evidenceIds"), list)
        and claim["evidenceIds"]
        and len(claim["evidenceIds"]) == len(set(claim["evidenceIds"]))
        and set(map(str, claim["evidenceIds"])) <= set(by_evidence)
        and str(claim.get("blockId")) in block_by_id
        and str(claim.get("statement", ""))
        in str(block_by_id.get(str(claim.get("blockId")), {}).get("content", ""))
        and f"[{claim.get('citationMarker')}]"
        in str(block_by_id.get(str(claim.get("blockId")), {}).get("content", ""))
        and source_entry_is_exact(claim)
        for claim in claims
        )
    )

    corporate_fact_ids = []
    claim_contract_failures = []
    factual_claim_ids = {
        str(claim.get("claimId"))
        for claim in claims
        if claim.get("claimType") in {"fact", "company_claim"}
    }
    for claim in claims:
        claim_type = claim.get("claimType")
        actor = str(claim.get("actor") or "").strip()
        attribution = str(claim.get("attribution") or "").strip()
        derived = claim.get("derivedFromClaimIds") or []
        horizon = str(claim.get("horizon") or "").strip()
        uncertainty = str(claim.get("uncertaintyNote") or "").strip()
        valid_contract = (
            (claim_type == "fact" and not actor and not attribution and not derived and not horizon and not uncertainty)
            or (claim_type == "company_claim" and actor and attribution and not derived and not horizon and not uncertainty)
            or (
                claim_type == "interpretation"
                and isinstance(derived, list)
                and bool(derived)
                and derived == sorted(set(map(str, derived)))
                and set(map(str, derived)) <= factual_claim_ids
                and not actor
                and not attribution
                and not horizon
                and not uncertainty
            )
            or (claim_type == "outlook" and actor and horizon and uncertainty and not derived)
        )
        block_type = str(
            block_by_id.get(str(claim.get("blockId")), {}).get("type")
            or ""
        )
        allowed_block_types = {
            "fact": {"fact", "background", "caution"},
            "company_claim": {"company_claim", "background", "caution"},
            "interpretation": {"interpretation"},
            "outlook": {"outlook"},
        }
        valid_contract = valid_contract and block_type in allowed_block_types.get(
            str(claim_type), set()
        )
        if not valid_contract:
            claim_contract_failures.append(str(claim.get("blockId")))
        if claim.get("claimType") != "fact":
            continue
        rows = [by_evidence.get(str(value)) for value in claim.get("evidenceIds", [])]
        if rows and all(
            row is not None and row.get("authorityTier") == "primary_corporate"
            for row in rows
        ):
            corporate_fact_ids.append(str(claim.get("blockId")))

    missing_high_impact_locators = []
    for claim in claims:
        if not is_high_impact(claim):
            continue
        for evidence_id in claim.get("evidenceIds", []):
            row = by_evidence.get(str(evidence_id), {})
            try:
                validate_evidence_locator(row.get("locatorType"), row.get("locator"))
            except ValidationError:
                missing_high_impact_locators.append(str(evidence_id))

    independence_failures = []
    for claim in claims:
        if not is_high_impact(claim):
            continue
        rows = [
            by_evidence[str(value)]
            for value in claim.get("evidenceIds", [])
            if str(value) in by_evidence
        ]
        primary = any(
            row.get("authorityTier")
            in {
                "primary_government",
                "primary_regulator",
                "primary_official",
                "primary_regulatory",
            }
            for row in rows
        )
        independent = {
            row.get("independenceGroup")
            for row in rows
            if row.get("authorityTier") != "primary_corporate"
            and row.get("independenceGroup")
        }
        independent_required = int(
            policy.check(_INDEPENDENCE)["config"][
                "primaryOrIndependentCount"
            ]
        )
        if not primary and len(independent) < independent_required:
            independence_failures.append(str(claim.get("blockId")))

    stale_ids = []
    for evidence_id in linked_ids:
        row = by_evidence.get(evidence_id, {})
        published_at = _parse_time(row.get("publishedAt"))
        cutoff = _parse_time(row.get("freshnessCutoff"))
        if published_at is None or cutoff is None or published_at < cutoff:
            stale_ids.append(evidence_id)

    rights_failures = []
    for evidence_id in linked_ids:
        row = by_evidence.get(evidence_id, {})
        rights = row.get("rightsStatus")
        if (
            row.get("publishable") is not True
            or rights not in {"allowed", "attribution_required"}
            or (rights == "attribution_required" and not row.get("attributionText"))
        ):
            rights_failures.append(evidence_id)
    visual_rights_failures = []
    for index, visual in enumerate(visuals):
        rights = visual.get("rightsStatus")
        if (
            rights not in {"allowed", "attribution_required"}
            or not str(visual.get("rightsBasisUrl", "")).strip()
            or not str(visual.get("altText", "")).strip()
            or not str(visual.get("caption", "")).strip()
            or not isinstance(visual.get("locator"), dict)
            or not visual.get("locator")
            or (rights == "attribution_required" and not visual.get("attributionText"))
        ):
            visual_rights_failures.append(f"visual:{index}")

    citation_limit = int(
        policy.check(_QUOTATION)["config"].get("maxCitationChars", 160)
    )
    citation_failures = []
    for claim in claims:
        spans = claim.get("sourceSpans")
        if not isinstance(spans, dict):
            citation_failures.append(str(claim.get("blockId")))
            continue
        for evidence_id in claim.get("evidenceIds", []):
            span = spans.get(str(evidence_id))
            frozen_evidence = by_evidence.get(str(evidence_id), {})
            source_text = frozen_evidence.get("sourceText")
            source_title = frozen_evidence.get("sourceTitle")
            span_matches = bool(
                isinstance(span, str)
                and (
                    (
                        isinstance(source_text, str)
                        and unicodedata.normalize("NFC", span)
                        in unicodedata.normalize("NFC", source_text)
                    )
                    or (
                        isinstance(source_title, str)
                        and unicodedata.normalize("NFC", span)
                        == unicodedata.normalize("NFC", source_title)
                    )
                )
            )
            if (
                not isinstance(span, str)
                or not span.strip()
                or len(span) > citation_limit
                or not span_matches
            ):
                citation_failures.append(str(claim.get("blockId")))

    excluded_ids = {
        str(evidence_id)
        for row in exclusions
        for evidence_id in (
            row.get("evidenceIds", [])
            if isinstance(row.get("evidenceIds", []), list)
            else []
        )
    }
    conflicting_linked = sorted(linked_ids & excluded_ids)
    unresolved_conflicts = sorted(
        str(row.get("runSourceItemId") or row.get("sourceItemId") or index)
        for index, row in enumerate(exclusions)
        if row.get("classification", row.get("selectionState")) == "conflicting"
        and row.get("resolutionState") != "resolved"
    )
    duplicate_origins = []
    for claim in claims:
        origins = [
            by_evidence[str(value)].get("originIdentityHash")
            for value in claim.get("evidenceIds", [])
            if str(value) in by_evidence
        ]
        if len([value for value in origins if value]) != len(
            set(value for value in origins if value)
        ):
            duplicate_origins.append(str(claim.get("blockId")))

    required_blocks = set(policy.document["requiredBlockTypes"])
    actual_blocks = {str(row.get("type")) for row in blocks}
    readability_config = policy.check(_READABILITY)["config"]
    max_paragraph = int(readability_config.get("maxParagraphChars", 800))
    max_sentence = int(readability_config.get("maxSentenceChars", 180))
    oversized_blocks = [
        str(row.get("id"))
        for row in blocks
        if len(str(row.get("content", ""))) > max_paragraph
    ]
    forbidden = policy.check(_EXAGGERATION)["config"].get(
        "forbiddenPhrases", []
    )
    full_text = "\n".join(str(row.get("content", "")) for row in blocks)
    oversized_sentences = [
        sentence.strip()[:80]
        for sentence in re.split(r"(?<=[.!?])|\n", full_text)
        if len(sentence.strip()) > max_sentence
    ]
    exaggerations = [phrase for phrase in forbidden if phrase in full_text]

    checks = (
        _result(
            _GROUNDED,
            policy,
            bool(claims) and claim_ids_valid,
            details={
                "invalid": not claim_ids_valid,
                "uncoveredFactAssertions": uncovered_fact_assertions,
                "titleBindingsValid": title_bindings_valid,
            },
        ),
        _result(
            _TYPE_SEPARATION,
            policy,
            not corporate_fact_ids and not claim_contract_failures,
            details={
                "corporateFactBlocks": corporate_fact_ids,
                "invalidContractBlocks": sorted(set(claim_contract_failures)),
            },
        ),
        _result(
            _HIGH_RISK,
            policy,
            not missing_high_impact_locators,
            details={
                "evidenceIds": sorted(set(missing_high_impact_locators))
            },
        ),
        _result(
            _INDEPENDENCE,
            policy,
            not independence_failures,
            details={"blockIds": independence_failures},
        ),
        _result(
            _FRESHNESS,
            policy,
            not stale_ids,
            details={"evidenceIds": sorted(stale_ids)},
        ),
        _result(
            _ELIGIBILITY,
            policy,
            not rights_failures,
            details={"subjects": sorted(rights_failures)},
        ),
        _result(
            _QUOTATION,
            policy,
            not citation_failures,
            details={
                "blockIds": sorted(set(citation_failures)),
                "maxChars": citation_limit,
            },
        ),
        _result(
            _DUPLICATE,
            policy,
            not conflicting_linked and not duplicate_origins and not unresolved_conflicts,
            details={
                "conflictingEvidenceIds": conflicting_linked,
                "duplicateOriginBlocks": duplicate_origins,
                "unresolvedConflicts": unresolved_conflicts,
            },
        ),
        _result(
            _READABILITY,
            policy,
            required_blocks <= actual_blocks
            and not oversized_blocks
            and not oversized_sentences,
            details={
                "missingBlockTypes": sorted(required_blocks - actual_blocks),
                "oversizedBlockIds": oversized_blocks,
                "oversizedSentences": oversized_sentences,
            },
        ),
        _result(
            _EXAGGERATION,
            policy,
            not exaggerations,
            details={"phrases": exaggerations},
        ),
    )
    if visuals:
        caption_markers = {
            str(claim.get("citationMarker")) for claim in claims
        }
        visual_failures = list(visual_rights_failures)
        for index, visual in enumerate(visuals):
            marker = str(visual.get("captionClaimMarker") or "").strip()
            marker_claims = [
                claim for claim in claims
                if str(claim.get("citationMarker")) == marker
            ]
            caption_assertions = _atomic_factual_sentences(
                visual.get("caption", "")
            )
            if (
                not marker
                or marker not in caption_markers
                or f"[{marker}]" not in str(visual.get("caption", ""))
                or len(marker_claims) != 1
                or caption_assertions
                != [str(marker_claims[0].get("statement", "")).strip()]
            ):
                visual_failures.append(f"visual-caption:{index}")
        checks = checks + (
            EditorialQualityCheck(
                code=VISUAL_GATE_CODE,
                version="1",
                result="passed" if not visual_failures else "failed",
                blocking=True,
                score=None,
                details={"subjects": sorted(set(visual_failures))},
            ),
        )
    expected_codes = REQUIRED_GATE_CODES | ({VISUAL_GATE_CODE} if visuals else set())
    if {row.code for row in checks} != expected_codes:
        raise ValueError("editorial quality evaluator gate set is incomplete")
    gate_material = [
        {
            "code": row["code"],
            "version": row["version"],
            "blocking": row["blocking"],
            "config": row["config"],
        }
        for row in policy.document["checks"]
    ]
    if visuals:
        gate_material.append(
            {
                "code": VISUAL_GATE_CODE,
                "version": "1",
                "blocking": True,
                "config": {"captionClaimRequired": True},
            }
        )
    report_material = [
        {
            "code": row.code,
            "version": row.version,
            "result": row.result,
            "blocking": row.blocking,
            "score": row.score,
            "details": dict(row.details),
        }
        for row in checks
    ]
    state = "passed" if all(row.result == "passed" for row in checks) else "failed"
    return EditorialQualityReport(
        state=state,
        checks=checks,
        gate_manifest_hash=canonical_hash(
            gate_material, schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
        report_hash=canonical_hash(
            report_material, schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
    )
