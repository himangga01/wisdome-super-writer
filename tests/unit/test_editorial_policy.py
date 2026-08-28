import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

REQUIRED_GATES = {
    "all_publishable_claims_grounded",
    "high_risk_verification_satisfied",
    "claim_independence_satisfied",
    "source_freshness_satisfied",
    "evidence_publish_eligibility_current",
    "quotation_limits_satisfied",
    "claim_types_separated_and_attributed",
    "duplicate_or_conflict_resolved",
    "korean_readability_and_repetition",
    "no_exaggeration_or_false_experience",
}

EXACT_EDITORIAL_GATES = REQUIRED_GATES
CLAIM_SCOPE_ID = "00000000-0000-0000-0000-000000000099"


def policy_document(*, topic="housing_subscription", version="1.0.0"):
    configs = {
        "quotation_limits_satisfied": {"maxCitationChars": 160},
        "claim_types_separated_and_attributed": {},
        "duplicate_or_conflict_resolved": {},
        "no_exaggeration_or_false_experience": {"forbiddenPhrases": ["수익 보장"]},
        "source_freshness_satisfied": {"clock": "current_publication_time"},
        "high_risk_verification_satisfied": {"required": True},
        "claim_independence_satisfied": {"primaryOrIndependentCount": 2},
        "korean_readability_and_repetition": {
            "maxSentenceChars": 180,
            "maxParagraphChars": 800,
        },
        "evidence_publish_eligibility_current": {"allowed": ["allowed", "attribution_required"]},
        "all_publishable_claims_grounded": {"allClaimsRequireEvidence": True},
    }
    return {
        "schemaVersion": "editorial-policy-v1",
        "policyKey": f"{topic}-editorial",
        "topicCode": topic,
        "policyVersion": version,
        "language": "ko-KR",
        "claimTypes": [
            "fact",
            "company_claim",
            "interpretation",
            "outlook",
        ],
        "requiredBlockTypes": ["fact", "background", "sources"],
        "highImpactFields": [
            "price",
            "application_start",
            "application_end",
            "eligibility",
        ],
        "checks": [
            {
                "code": code,
                "version": "1",
                "blocking": True,
                "config": configs[code],
            }
            for code in sorted(REQUIRED_GATES)
        ],
    }


def write_policy(root: Path, document: dict, *, raw: str | None = None):
    path = root / f"{document['topicCode']}.json"
    path.write_text(
        raw if raw is not None else json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def valid_evidence(*, evidence_id="e-1", authority="primary_government"):
    published = datetime(2026, 8, 8, tzinfo=UTC)
    return {
        "evidenceId": evidence_id,
        "selectionState": "selected",
        "publishable": True,
        "locatorType": "structured_path",
        "locator": {
            "locator_type": "structured_path",
            "path_type": "json_pointer",
            "path": "/price",
        },
        "rightsStatus": "allowed",
        "attributionText": None,
        "publishedAt": published.isoformat(),
        "freshnessCutoff": (published - timedelta(days=1)).isoformat(),
        "authorityTier": authority,
        "independenceGroup": "origin-1",
        "originIdentityHash": "a" * 64,
        "publisher": "공식 기관",
        "sourceTitle": "공식 공고",
        "sourceUrl": "https://example.test/notice",
        "semanticFields": {"price": "3억 원"},
        "reviewApproved": True,
        "sourceText": "공식 공고에 분양가 3억 원으로 기재됐다.",
    }


def valid_claim(*, claim_type="fact", evidence_ids=("e-1",), high_impact=False):
    return {
        "claimId": "00000000-0000-0000-0000-000000000001",
        "blockId": "facts-1",
        "statement": "분양가는 3억 원으로 공고됐다.",
        "claimType": claim_type,
        "evidenceIds": list(evidence_ids),
        "citationMarker": "S1",
        "highImpact": high_impact,
        "sourceSpans": {"e-1": "분양가 3억 원"},
    }


def body_blocks():
    return [
        {"id": "facts-1", "type": "fact", "content": "분양가는 3억 원으로 공고됐다. [S1]"},
        {"id": "background-1", "type": "background", "content": "---"},
        {
            "id": "sources-1",
            "type": "sources",
            "content": "- [S1] [공식 공고](https://example.test/notice) · 공식 기관",
        },
    ]


def test_policy_loader_uses_rfc8785_and_accepts_key_order_changes():
    from apps.editorial.policies import load_editorial_policy

    document = policy_document()
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        first_root = Path(first)
        second_root = Path(second)
        write_policy(first_root, document)
        reordered = dict(reversed(list(document.items())))
        write_policy(second_root, reordered)

        first_policy = load_editorial_policy("housing_subscription", root=first_root)
        second_policy = load_editorial_policy("housing_subscription", root=second_root)

    assert first_policy.config_hash == second_policy.config_hash
    assert first_policy.release_document_hash != second_policy.release_document_hash
    assert first_policy.material_hash != second_policy.material_hash
    assert first_policy.document == document


def test_policy_loader_rejects_duplicate_json_members():
    from apps.editorial.policies import EditorialPolicyError, load_editorial_policy

    document = policy_document()
    raw = json.dumps(document, ensure_ascii=False).replace(
        '"policyVersion": "1.0.0"',
        '"policyVersion": "1.0.0", "policyVersion": "9.9.9"',
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_policy(root, document, raw=raw)
        with pytest.raises(EditorialPolicyError, match="duplicate"):
            load_editorial_policy("housing_subscription", root=root)


def test_policy_loader_rejects_unknown_gate_configuration():
    from apps.editorial.policies import EditorialPolicyError, load_editorial_policy

    document = policy_document()
    document["checks"][0]["config"]["unknown"] = True
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_policy(root, document)
        with pytest.raises(EditorialPolicyError, match="config"):
            load_editorial_policy("housing_subscription", root=root)


@pytest.mark.parametrize("missing_code", sorted(REQUIRED_GATES))
def test_policy_loader_requires_every_blocking_gate(missing_code):
    from apps.editorial.policies import EditorialPolicyError, load_editorial_policy

    document = policy_document()
    document["checks"] = [
        row for row in document["checks"] if row["code"] != missing_code
    ]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_policy(root, document)
        with pytest.raises(EditorialPolicyError, match=missing_code):
            load_editorial_policy("housing_subscription", root=root)


def test_template_generator_returns_atomic_typed_claims_without_raw_700_char_copy():
    from adapters.generators.base import EvidenceInput, GeneratedClaim
    from adapters.generators.template import SourceGroundedTemplateGenerator

    source = "원문 문장입니다. " * 100
    generated = SourceGroundedTemplateGenerator().generate(
        topic="housing_subscription",
        article_type="housing_notice",
        evidence=[
            EvidenceInput(
                evidence_id="e-1",
                title="공고",
                url="https://example.test/notice",
                publisher="공식 기관",
                text=source,
                published_at="2026-08-08T00:00:00+00:00",
            )
        ],
    )

    assert len(generated.claims) >= 2
    assert isinstance(generated.claims[0], GeneratedClaim)
    assert generated.claims[0].evidence_ids == ("e-1",)
    assert len(generated.claims[0].statement) <= 160
    assert generated.body_blocks[-1].block_type == "sources"
    claim_blocks = {claim.block_id for claim in generated.claims}
    assert {"title", "summary"} <= claim_blocks
    assert all(
        block.block_type not in {"fact", "company_claim"}
        or block.block_id in claim_blocks
        for block in generated.body_blocks
    )


def test_body_block_taxonomy_uses_contract_singular_fact_type():
    from apps.editorial.services import validate_body_blocks

    assert validate_body_blocks(
        [{"id": "fact-1", "type": "fact", "content": "검증된 사실"}]
    )[0]["type"] == "fact"
    with pytest.raises(ValueError, match="body blocks"):
        validate_body_blocks(
            [{"id": "facts-1", "type": "facts", "content": "구형 타입"}]
        )


def test_verification_snapshot_freezes_article_contract_and_role():
    from datetime import date
    from types import SimpleNamespace
    from uuid import UUID

    from apps.editorial.services import _verification_snapshot

    lead_id = UUID("00000000-0000-0000-0000-000000000001")
    support_id = UUID("00000000-0000-0000-0000-000000000002")
    common = {
        "cluster_id": UUID("00000000-0000-0000-0000-000000000003"),
        "version": 1,
        "decision": "verified_notice",
        "article_type": "housing_notice",
        "category": "housing_notice",
        "local_event_date": date(2026, 8, 9),
        "evidence_manifest_hash": "a" * 64,
        "rule_manifest_hash": "b" * 64,
        "result_manifest_hash": "c" * 64,
        "policy_version": "1",
        "policy_hash": "d" * 64,
    }
    rows = [
        SimpleNamespace(id=lead_id, **common),
        SimpleNamespace(id=support_id, **common),
    ]

    snapshot = _verification_snapshot(
        rows,
        primary_verification_id=lead_id,
    )

    assert snapshot[0]["role"] == "lead"
    assert snapshot[1]["role"] == "supporting"
    assert snapshot[0]["articleType"] == "housing_notice"
    assert snapshot[0]["category"] == "housing_notice"
    assert snapshot[0]["localEventDate"] == "2026-08-09"


def test_template_generator_rejects_empty_evidence_instead_of_inventing_fallback():
    from adapters.generators.base import EvidenceInput
    from adapters.generators.template import SourceGroundedTemplateGenerator

    with pytest.raises(ValueError, match="text"):
        SourceGroundedTemplateGenerator().generate(
            topic="housing_subscription",
            article_type="housing_notice",
            evidence=[
                EvidenceInput(
                    evidence_id="e-1",
                    title="공고",
                    url="https://example.test/notice",
                    publisher="공식 기관",
                    text="",
                    published_at=None,
                )
            ],
        )


def test_template_output_passes_the_same_shared_editorial_evaluator():
    from adapters.generators.base import EvidenceInput
    from adapters.generators.template import SourceGroundedTemplateGenerator
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from apps.editorial.services import normalize_generated_claims
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(
            document, schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
    )
    evidence = valid_evidence()
    generated = SourceGroundedTemplateGenerator().generate(
        topic="housing_subscription",
        article_type="housing_notice",
        evidence=[
            EvidenceInput(
                evidence_id="e-1",
                title=evidence["sourceTitle"],
                url="https://example.test/notice",
                publisher="공식 기관",
                text=evidence["sourceText"],
                published_at=evidence["publishedAt"],
            )
        ],
    )
    blocks = [
        {"id": row.block_id, "type": row.block_type, "content": row.content}
        for row in generated.body_blocks
    ]
    claims = normalize_generated_claims(
        claims=generated.claims,
        evidence_manifest=[evidence],
        policy=policy,
        claim_scope_id=CLAIM_SCOPE_ID,
    )
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=blocks,
        claims=claims,
        evidence=[evidence],
        exclusions=[],
        visuals=[],
    )

    assert report.state == "passed"


def test_server_normalizes_corporate_fact_and_policy_high_impact():
    from adapters.generators.base import GeneratedClaim
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.services import normalize_generated_claims
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document(topic="semiconductor_news")
    document["highImpactFields"] = ["mass_production"]
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    rows = normalize_generated_claims(
        claims=(
            GeneratedClaim(
                block_id="facts-1",
                statement="양산을 시작했다고 회사가 발표했다.",
                claim_type="fact",
                evidence_ids=("e-1",),
                citation_marker="S1",
                source_spans=(("e-1", "양산을 시작"),),
                semantic_key="mass_production",
            ),
        ),
        evidence_manifest=[
            {
                "evidenceId": "e-1",
                "authorityTier": "primary_corporate",
                "publisher": "반도체 기업",
                "sourceText": "회사는 양산을 시작했다고 발표했다.",
                "locator": {"path": "/mass_production"},
            }
        ],
        policy=policy,
        claim_scope_id=CLAIM_SCOPE_ID,
    )

    assert rows[0]["claimType"] == "company_claim"
    assert rows[0]["highImpact"] is True


def test_manual_claim_bindings_must_match_the_referenced_body_block():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.services import validate_manual_claim_bindings
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    with pytest.raises(ValueError, match="body block"):
        validate_manual_claim_bindings(
            blocks=body_blocks(),
            claim_bindings=[
                {
                    "claimRef": "claim-1",
                    "blockId": "facts-1",
                    "statement": "본문에 없는 주장",
                    "claimType": "fact",
                    "evidenceIds": ["e-1"],
                    "citationMarker": "S1",
                    "sourceSpans": {"e-1": "분양가 3억 원"},
                    "semanticKey": "price",
                    "actor": None,
                    "attribution": None,
                    "derivedFromClaimRefs": [],
                    "horizon": None,
                    "uncertaintyNote": None,
                }
            ],
            evidence_manifest=[valid_evidence()],
            policy=policy,
            claim_scope_id=CLAIM_SCOPE_ID,
        )
    invalid_blocks = body_blocks()
    invalid_blocks[0] = {**invalid_blocks[0], "unexpected": True}
    with pytest.raises(ValueError, match="body blocks are not exact"):
        validate_manual_claim_bindings(
            blocks=invalid_blocks,
            claim_bindings=[
                {
                    "claimRef": "claim-1",
                    "blockId": "facts-1",
                    "statement": valid_claim()["statement"],
                    "claimType": "fact",
                    "evidenceIds": ["e-1"],
                    "citationMarker": "S1",
                    "sourceSpans": {"e-1": "분양가는 3억원"},
                    "semanticKey": "price",
                    "actor": None,
                    "attribution": None,
                    "derivedFromClaimRefs": [],
                    "horizon": None,
                    "uncertaintyNote": None,
                }
            ],
            evidence_manifest=[valid_evidence()],
            policy=policy,
            claim_scope_id=CLAIM_SCOPE_ID,
        )


def test_quality_evaluator_always_returns_the_ten_required_blocking_results():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(
            document, schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
    )
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=body_blocks(),
        claims=[valid_claim()],
        evidence=[valid_evidence()],
        exclusions=[],
        visuals=[],
    )

    assert {row.code for row in report.checks} == REQUIRED_GATES
    assert all(row.blocking for row in report.checks)


def test_quality_source_coverage_fails_for_unknown_evidence():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=body_blocks(),
        claims=[valid_claim(evidence_ids=("not-frozen",))],
        evidence=[valid_evidence()],
        exclusions=[],
        visuals=[],
    )

    check = next(row for row in report.checks if row.code == "all_publishable_claims_grounded")
    assert check.result == "failed"
    assert report.state == "failed"


def test_quality_type_separation_blocks_corporate_only_fact():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document(topic="semiconductor_news")
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=body_blocks(),
        claims=[valid_claim(claim_type="fact")],
        evidence=[valid_evidence(authority="primary_corporate")],
        exclusions=[],
        visuals=[],
    )

    check = next(
        row for row in report.checks if row.code == "claim_types_separated_and_attributed"
    )
    assert check.result == "failed"


def test_quality_reclassifies_high_impact_from_policy_and_locator_material():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    evidence = valid_evidence(authority="secondary_media")
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=body_blocks(),
        claims=[valid_claim(high_impact=False)],
        evidence=[evidence],
        exclusions=[],
        visuals=[],
    )

    check = next(row for row in report.checks if row.code == "claim_independence_satisfied")
    assert check.result == "failed"


def test_quality_rejects_source_span_not_present_in_frozen_evidence_text():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    claim = valid_claim()
    claim["sourceSpans"] = {"e-1": "원문에 존재하지 않는 인용"}
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=body_blocks(),
        claims=[claim],
        evidence=[valid_evidence()],
        exclusions=[],
        visuals=[],
    )

    check = next(row for row in report.checks if row.code == "quotation_limits_satisfied")
    assert check.result == "failed"


def test_visual_provenance_is_folded_into_rights_gate():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=body_blocks(),
        claims=[valid_claim()],
        evidence=[valid_evidence()],
        exclusions=[],
        visuals=[
            {
                "blockId": "facts-1",
                "evidenceId": "e-1",
                "rightsStatus": "unknown",
                "altText": "",
                "caption": "공고 이미지",
                "locator": {},
            }
        ],
    )

    check = next(row for row in report.checks if row.code == "visual_rights_and_alt_text")
    assert check.result == "failed"


def test_publishable_projection_recomputes_live_material_instead_of_trusting_hashes():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from apps.editorial.services import validate_publishable_projection_material
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(
            document, schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
    )
    blocks = body_blocks()
    claims = [valid_claim()]
    evidence = [valid_evidence()]
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=blocks,
        claims=claims,
        evidence=evidence,
        exclusions=[],
        visuals=[],
    )
    checks = [
        {
            "code": row.code,
            "version": row.version,
            "result": row.result,
            "blocking": row.blocking,
            "score": row.score,
            "details": dict(row.details),
            "detailsHash": canonical_hash(
                row.details, schema_version=CANONICAL_HASH_SCHEMA_V1
            ),
        }
        for row in report.checks
    ]
    projection = {
        "policyKey": policy.policy_key,
        "policyVersion": policy.policy_version,
        "policyHash": policy.material_hash,
        "policyDocument": policy.document,
        "bodyBlocks": blocks,
        "claimBindings": claims,
        "claimManifestHash": canonical_hash(
            claims, schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
        "evidenceManifest": evidence,
        "evidenceManifestHash": canonical_hash(
            evidence, schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
        "exclusionManifest": [],
        "exclusionManifestHash": canonical_hash(
            [], schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
        "qualityGateManifestHash": report.gate_manifest_hash,
        "qualityReportHash": report.report_hash,
        "qualityState": "passed",
        "claimGraphState": "passed",
    }

    validate_publishable_projection_material(
        projection=projection,
        current_policy=policy,
        live_evidence_manifest=evidence,
        persisted_claims=claims,
        persisted_quality_checks=checks,
        visuals=[],
    )

    stale_live = [{**evidence[0], "rightsStatus": "unknown"}]
    with pytest.raises(ValueError, match="live evidence"):
        validate_publishable_projection_material(
            projection=projection,
            current_policy=policy,
            live_evidence_manifest=stale_live,
            persisted_claims=claims,
            persisted_quality_checks=checks,
            visuals=[],
        )


def test_publishable_projection_rejects_changed_current_policy_release():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.services import validate_publishable_projection_material
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    frozen = policy_document()
    current = policy_document(version="1.0.1")
    current_policy = EditorialPolicy(
        document=current,
        material_hash=canonical_hash(
            current, schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
    )
    projection = {
        "policyKey": frozen["policyKey"],
        "policyVersion": frozen["policyVersion"],
        "policyHash": canonical_hash(
            frozen, schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
        "policyDocument": frozen,
        "bodyBlocks": [],
        "claimBindings": [],
        "claimManifestHash": canonical_hash(
            [], schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
        "evidenceManifest": [],
        "evidenceManifestHash": canonical_hash(
            [], schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
        "exclusionManifest": [],
        "exclusionManifestHash": canonical_hash(
            [], schema_version=CANONICAL_HASH_SCHEMA_V1
        ),
        "qualityGateManifestHash": "a" * 64,
        "qualityReportHash": "b" * 64,
        "qualityState": "passed",
        "claimGraphState": "passed",
    }

    with pytest.raises(ValueError, match="current editorial policy"):
        validate_publishable_projection_material(
            projection=projection,
            current_policy=current_policy,
            live_evidence_manifest=[],
            persisted_claims=[],
            persisted_quality_checks=[],
            visuals=[],
        )


def test_korean_unclassified_claim_is_fail_closed_as_high_impact():
    from adapters.generators.base import GeneratedClaim
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.services import normalize_generated_claims
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    rows = normalize_generated_claims(
        claims=(
            GeneratedClaim(
                block_id="facts-1",
                statement="신청은 다음 달부터 시작한다.",
                claim_type="fact",
                evidence_ids=("e-1",),
                citation_marker="S1",
                source_spans=(("e-1", "신청은 다음 달부터 시작한다."),),
            ),
        ),
        evidence_manifest=[valid_evidence()],
        policy=policy,
        claim_scope_id=CLAIM_SCOPE_ID,
    )

    assert rows[0]["highImpact"] is True
    assert rows[0]["semanticKey"] == "unclassified_high_risk"


def test_korean_high_impact_phrase_is_server_classified_to_policy_semantic_key():
    from adapters.generators.base import GeneratedClaim
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.services import normalize_generated_claims
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    rows = normalize_generated_claims(
        claims=(
            GeneratedClaim(
                block_id="facts-1",
                statement="분양가는 3억 원으로 공고됐다.",
                claim_type="fact",
                evidence_ids=("e-1",),
                citation_marker="S1",
                source_spans=(("e-1", "분양가 3억 원"),),
            ),
        ),
        evidence_manifest=[valid_evidence()],
        policy=policy,
        claim_scope_id=CLAIM_SCOPE_ID,
    )

    assert rows[0]["semanticKey"] == "price"
    assert rows[0]["highImpact"] is True


@pytest.mark.parametrize(
    ("claim_type", "overrides", "missing_field"),
    [
        ("company_claim", {}, "actor"),
        ("interpretation", {}, "derivedFromClaimRefs"),
        ("outlook", {"actor": "분석 주체"}, "horizon"),
    ],
)
def test_discriminated_claim_contract_rejects_missing_type_material(
    claim_type, overrides, missing_field
):
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.services import validate_manual_claim_bindings
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    binding = {
        "claimRef": "claim-1",
        "blockId": "facts-1",
        "statement": "분양가는 3억 원으로 공고됐다.",
        "claimType": claim_type,
        "evidenceIds": ["e-1"],
        "citationMarker": "S1",
        "sourceSpans": {"e-1": "분양가 3억 원"},
        "semanticKey": "price",
        "actor": None,
        "attribution": None,
        "derivedFromClaimRefs": [],
        "horizon": None,
        "uncertaintyNote": None,
        **overrides,
    }
    with pytest.raises(ValueError, match=missing_field):
        validate_manual_claim_bindings(
            blocks=body_blocks(),
            claim_bindings=[binding],
            evidence_manifest=[valid_evidence()],
            policy=policy,
            claim_scope_id=CLAIM_SCOPE_ID,
        )


def test_interpretation_derives_from_deterministic_claim_uuid_not_marker():
    from adapters.generators.base import GeneratedClaim
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.services import (
        deterministic_generated_claim_id,
        normalize_generated_claims,
    )
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    fact = GeneratedClaim(
        block_id="facts-1",
        statement="분양가는 3억 원으로 공고됐다.",
        claim_type="fact",
        evidence_ids=("e-1",),
        citation_marker="S1",
        source_spans=(("e-1", "분양가 3억 원"),),
        semantic_key="price",
    )
    fact_id = deterministic_generated_claim_id(
        fact,
        claim_scope_id=CLAIM_SCOPE_ID,
    )
    assert fact_id != deterministic_generated_claim_id(
        fact,
        claim_scope_id="00000000-0000-0000-0000-000000000098",
    )
    interpretation = GeneratedClaim(
        block_id="interpretation-1",
        statement="공급 조건은 비교 검토가 필요하다.",
        claim_type="interpretation",
        evidence_ids=("e-1",),
        citation_marker="S2",
        source_spans=(("e-1", "분양가 3억 원"),),
        semantic_key="price",
        derived_from_claim_ids=(fact_id,),
    )

    rows = normalize_generated_claims(
        claims=(fact, interpretation),
        evidence_manifest=[valid_evidence()],
        policy=policy,
        claim_scope_id=CLAIM_SCOPE_ID,
    )

    assert rows[0]["claimId"] == fact_id
    assert rows[1]["derivedFromClaimIds"] == [fact_id]
    with pytest.raises(ValueError, match="derivedFromClaimIds"):
        normalize_generated_claims(
            claims=(
                fact,
                GeneratedClaim(
                    **{
                        **interpretation.__dict__,
                        "derived_from_claim_ids": ("S1",),
                    }
                ),
            ),
            evidence_manifest=[valid_evidence()],
            policy=policy,
            claim_scope_id=CLAIM_SCOPE_ID,
        )


def test_atomic_coverage_rejects_second_unbound_factual_sentence():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    blocks = body_blocks()
    blocks[0]["content"] += " 신청은 내일부터 시작한다."
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=blocks,
        claims=[valid_claim()],
        evidence=[valid_evidence()],
        exclusions=[],
        visuals=[],
    )

    check = next(
        row for row in report.checks
        if row.code in {"source_coverage", "all_publishable_claims_grounded"}
    )
    assert check.result == "failed"


def test_title_claim_requires_exact_source_title_and_marker_binding():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    evidence = {**valid_evidence(), "sourceTitle": "공식 모집 공고"}
    title_claim = {
        **valid_claim(),
        "blockId": "title",
        "statement": "청약 공고: 임의 제목",
        "citationMarker": "T1",
        "sourceSpans": {"e-1": "공식 모집 공고"},
    }
    blocks = body_blocks() + [
        {"id": "title", "type": "fact", "content": "청약 공고: 임의 제목 [T1]"}
    ]
    blocks[2]["content"] += "\n- [T1] 공식 모집 공고"
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=blocks,
        claims=[valid_claim(), title_claim],
        evidence=[evidence],
        exclusions=[],
        visuals=[],
    )
    check = next(
        row for row in report.checks
        if row.code in {"source_coverage", "all_publishable_claims_grounded"}
    )
    assert check.result == "failed"


def test_policy_release_binds_raw_bytes_and_actual_implementation_manifest():
    from apps.editorial.policies import load_editorial_policy

    document = policy_document()
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        first_root = Path(first)
        second_root = Path(second)
        write_policy(first_root, document, raw=json.dumps(document, ensure_ascii=False))
        write_policy(
            second_root,
            document,
            raw=json.dumps(document, ensure_ascii=False, indent=2),
        )
        compact = load_editorial_policy("housing_subscription", root=first_root)
        pretty = load_editorial_policy("housing_subscription", root=second_root)

    assert compact.config_hash == pretty.config_hash
    assert compact.release_document_hash != pretty.release_document_hash
    assert compact.implementation_manifest_hash
    assert {
        row["path"] for row in compact.implementation_manifest["files"]
    } >= {
        "src/apps/editorial/policies.py",
        "src/apps/editorial/quality.py",
        "src/adapters/generators/template.py",
    }


def test_quality_uses_exact_ten_codes_and_conditional_visual_gate():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    no_visual = evaluate_editorial_quality(
        policy=policy,
        blocks=body_blocks(),
        claims=[valid_claim()],
        evidence=[valid_evidence()],
        exclusions=[],
        visuals=[],
    )
    with_visual = evaluate_editorial_quality(
        policy=policy,
        blocks=body_blocks(),
        claims=[valid_claim()],
        evidence=[valid_evidence()],
        exclusions=[],
        visuals=[{
            "blockId": "facts-1",
            "evidenceId": "e-1",
            "rightsStatus": "allowed",
            "altText": "공고 표",
            "caption": "분양가는 3억 원으로 공고됐다. [V1]",
            "locator": {"page_index": 0},
            "captionClaimMarker": "V1",
        }],
    )

    assert {row.code for row in no_visual.checks} == EXACT_EDITORIAL_GATES
    assert {row.code for row in with_visual.checks} == (
        EXACT_EDITORIAL_GATES | {"visual_rights_and_alt_text"}
    )


def test_unresolved_conflict_blocks_quality_even_when_not_linked():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=body_blocks(),
        claims=[valid_claim()],
        evidence=[valid_evidence()],
        exclusions=[{
            "selectionState": "conflicting",
            "classification": "conflicting",
            "resolutionState": "unresolved",
            "evidenceIds": ["e-2"],
        }],
        visuals=[],
    )
    check = next(
        row for row in report.checks
        if row.code in {"duplicate_conflict", "duplicate_or_conflict_resolved"}
    )
    assert check.result == "failed"


def test_manual_request_hash_changes_with_normalized_claim_bindings():
    from apps.editorial.services import manual_revision_request_hash

    common = {
        "article_id": "00000000-0000-0000-0000-000000000001",
        "base_revision_no": 1,
        "content_hash": "a" * 64,
        "request_key": "manual-request-1",
        "reason": "correction",
        "actor_id": "1",
    }
    first = manual_revision_request_hash(
        **common,
        normalized_claim_bindings=[valid_claim()],
    )
    changed = [{**valid_claim(), "sourceSpans": {"e-1": "다른 실제 인용"}}]
    second = manual_revision_request_hash(
        **common,
        normalized_claim_bindings=changed,
    )
    assert first != second


def test_draft_generation_run_guard_rejects_stop_requested_run():
    from types import SimpleNamespace

    from apps.editorial.services import _require_draft_generation_run_active

    with pytest.raises(ValueError, match="not eligible"):
        _require_draft_generation_run_active(
            SimpleNamespace(state="validating", stop_requested_at=object())
        )


def test_publishable_projection_accepts_real_composite_release_policy_hash():
    from apps.editorial.policies import load_editorial_policy
    from apps.editorial.quality import evaluate_editorial_quality
    from apps.editorial.services import validate_publishable_projection_material
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_policy(root, document)
        policy = load_editorial_policy("housing_subscription", root=root)
    blocks = body_blocks()
    claims = [valid_claim()]
    evidence = [valid_evidence()]
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=blocks,
        claims=claims,
        evidence=evidence,
        exclusions=[],
        visuals=[],
    )
    checks = [
        {
            "code": row.code,
            "version": row.version,
            "result": row.result,
            "blocking": row.blocking,
            "score": row.score,
            "details": dict(row.details),
            "detailsHash": canonical_hash(
                row.details, schema_version=CANONICAL_HASH_SCHEMA_V1
            ),
        }
        for row in report.checks
    ]
    validate_publishable_projection_material(
        projection={
            "policyKey": policy.policy_key,
            "policyVersion": policy.policy_version,
            "policyHash": policy.material_hash,
            "policyDocument": policy.document,
            "releaseDocumentHash": policy.release_document_hash,
            "configHash": policy.config_hash,
            "implementationManifest": policy.implementation_manifest,
            "implementationManifestHash": policy.implementation_manifest_hash,
            "bodyBlocks": blocks,
            "claimBindings": claims,
            "claimManifestHash": canonical_hash(
                claims, schema_version=CANONICAL_HASH_SCHEMA_V1
            ),
            "evidenceManifest": evidence,
            "evidenceManifestHash": canonical_hash(
                evidence, schema_version=CANONICAL_HASH_SCHEMA_V1
            ),
            "exclusionManifest": [],
            "exclusionManifestHash": canonical_hash(
                [], schema_version=CANONICAL_HASH_SCHEMA_V1
            ),
            "qualityGateManifestHash": report.gate_manifest_hash,
            "qualityReportHash": report.report_hash,
            "qualityState": "passed",
            "claimGraphState": "passed",
        },
        current_policy=policy,
        live_evidence_manifest=evidence,
        persisted_claims=claims,
        persisted_quality_checks=checks,
        visuals=[],
    )


def test_supplied_low_risk_semantic_key_cannot_override_server_classification():
    from adapters.generators.base import GeneratedClaim
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.services import normalize_generated_claims
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    rows = normalize_generated_claims(
        claims=(
            GeneratedClaim(
                block_id="facts-1",
                statement="분양가는 3억 원으로 공고됐다.",
                claim_type="fact",
                evidence_ids=("e-1",),
                citation_marker="S1",
                source_spans=(("e-1", "분양가 3억 원"),),
                semantic_key="background",
            ),
        ),
        evidence_manifest=[
            {
                **valid_evidence(),
                "sourceText": "공식 공고에 분양가 3억 원으로 기재됐다.",
                "locator": {"path": "/price"},
            }
        ],
        policy=policy,
        claim_scope_id=CLAIM_SCOPE_ID,
    )
    assert rows[0]["semanticKey"] == "price"
    assert rows[0]["highImpact"] is True


def test_atomic_coverage_rejects_unbound_bullet_and_background_assertions():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    blocks = body_blocks()
    blocks[0]["content"] += "\n- 신청 마감일은 8월 31일이다."
    blocks[1]["content"] = "분양가는 9억 원이다."
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=blocks,
        claims=[valid_claim()],
        evidence=[valid_evidence()],
        exclusions=[],
        visuals=[],
    )
    grounded = next(
        row for row in report.checks
        if row.code == "all_publishable_claims_grounded"
    )
    assert grounded.result == "failed"
    assert len(grounded.details["uncoveredFactAssertions"]) == 2


def test_claim_type_must_match_its_body_block_type():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    fact = valid_claim()
    interpretation = {
        **valid_claim(),
        "claimId": "00000000-0000-0000-0000-000000000002",
        "statement": "가격 비교가 필요하다는 해석이다.",
        "claimType": "interpretation",
        "citationMarker": "S2",
        "derivedFromClaimIds": [fact["claimId"]],
        "semanticKey": "price",
    }
    blocks = body_blocks()
    blocks[0]["content"] += " 가격 비교가 필요하다는 해석이다. [S2]"
    blocks[2]["content"] += "\n- [S2] 공식 공고"
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=blocks,
        claims=[fact, interpretation],
        evidence=[valid_evidence()],
        exclusions=[],
        visuals=[],
    )
    typed = next(
        row for row in report.checks
        if row.code == "claim_types_separated_and_attributed"
    )
    assert typed.result == "failed"


def test_source_marker_requires_exact_frozen_title_url_and_publisher_entry():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    blocks = body_blocks()
    blocks[2]["content"] = "- [S1] [다른 자료](https://attacker.test/) · 다른 발행자"
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=blocks,
        claims=[valid_claim()],
        evidence=[{**valid_evidence(), "sourceUrl": "https://example.test/notice"}],
        exclusions=[],
        visuals=[],
    )
    grounded = next(
        row for row in report.checks
        if row.code == "all_publishable_claims_grounded"
    )
    assert grounded.result == "failed"


def test_manual_interpretation_claim_ref_maps_to_deterministic_fact_uuid():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.services import validate_manual_claim_bindings
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    blocks = [
        {"id": "facts-1", "type": "fact", "content": "분양가는 3억 원이다. [S1]"},
        {
            "id": "interpretation-1",
            "type": "interpretation",
            "content": "가격 비교가 필요하다는 해석이다. [S2]",
        },
        {
            "id": "sources-1",
            "type": "sources",
            "content": "- [S1] 공식 공고\n- [S2] 공식 공고",
        },
    ]
    common = {
        "evidenceIds": ["e-1"],
        "sourceSpans": {"e-1": "분양가 3억 원"},
        "semanticKey": "price",
        "actor": None,
        "attribution": None,
        "horizon": None,
        "uncertaintyNote": None,
    }
    rows = validate_manual_claim_bindings(
        blocks=blocks,
        claim_bindings=[
            {
                **common,
                "claimRef": "fact-price",
                "blockId": "facts-1",
                "statement": "분양가는 3억 원이다.",
                "claimType": "fact",
                "citationMarker": "S1",
                "derivedFromClaimRefs": [],
            },
            {
                **common,
                "claimRef": "interpret-price",
                "blockId": "interpretation-1",
                "statement": "가격 비교가 필요하다는 해석이다.",
                "claimType": "interpretation",
                "citationMarker": "S2",
                "derivedFromClaimRefs": ["fact-price"],
            },
        ],
        evidence_manifest=[
            {
                **valid_evidence(),
                "sourceText": "공식 공고의 분양가 3억 원",
            }
        ],
        policy=policy,
        claim_scope_id=CLAIM_SCOPE_ID,
    )
    assert rows[1]["derivedFromClaimIds"] == [rows[0]["claimId"]]


def test_visual_caption_requires_exact_atomic_claim_statement():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=body_blocks(),
        claims=[valid_claim()],
        evidence=[valid_evidence()],
        exclusions=[],
        visuals=[
            {
                "blockId": "facts-1",
                "evidenceId": "e-1",
                "rightsStatus": "allowed",
                "rightsBasisUrl": "https://example.test/rights",
                "altText": "공고 이미지",
                "caption": "매출이 열 배 증가했다. [S1]",
                "locator": {"page_index": 0},
                "captionClaimMarker": "S1",
            }
        ],
    )
    visual = next(
        row for row in report.checks
        if row.code == "visual_rights_and_alt_text"
    )
    assert visual.result == "failed"


def test_publishable_projection_rejects_incomplete_exclusion_evidence_material():
    from apps.editorial.policies import EditorialPolicy
    from apps.editorial.quality import evaluate_editorial_quality
    from apps.editorial.services import validate_publishable_projection_material
    from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

    document = policy_document()
    policy = EditorialPolicy(
        document=document,
        material_hash=canonical_hash(document, schema_version=CANONICAL_HASH_SCHEMA_V1),
    )
    blocks = body_blocks()
    claims = [valid_claim()]
    evidence = [valid_evidence()]
    exclusions = [
        {
            "classification": "duplicate",
            "resolutionState": "resolved",
            "evidenceIds": ["excluded-evidence"],
            "evidenceMaterial": [],
        }
    ]
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=blocks,
        claims=claims,
        evidence=evidence,
        exclusions=exclusions,
        visuals=[],
    )
    checks = [
        {
            "code": row.code,
            "version": row.version,
            "result": row.result,
            "blocking": row.blocking,
            "score": row.score,
            "details": dict(row.details),
            "detailsHash": canonical_hash(
                row.details, schema_version=CANONICAL_HASH_SCHEMA_V1
            ),
        }
        for row in report.checks
    ]
    with pytest.raises(ValueError, match="exclusion"):
        validate_publishable_projection_material(
            projection={
                "policyKey": policy.policy_key,
                "policyVersion": policy.policy_version,
                "policyHash": policy.material_hash,
                "policyDocument": policy.document,
                "bodyBlocks": blocks,
                "claimBindings": claims,
                "claimManifestHash": canonical_hash(
                    claims, schema_version=CANONICAL_HASH_SCHEMA_V1
                ),
                "evidenceManifest": evidence,
                "evidenceManifestHash": canonical_hash(
                    evidence, schema_version=CANONICAL_HASH_SCHEMA_V1
                ),
                "exclusionManifest": exclusions,
                "exclusionManifestHash": canonical_hash(
                    exclusions, schema_version=CANONICAL_HASH_SCHEMA_V1
                ),
                "qualityGateManifestHash": report.gate_manifest_hash,
                "qualityReportHash": report.report_hash,
                "qualityState": "passed",
                "claimGraphState": "passed",
            },
            current_policy=policy,
            live_evidence_manifest=evidence,
            persisted_claims=claims,
            persisted_quality_checks=checks,
            visuals=[],
        )


def test_revalidate_requested_route_uses_exact_seven_field_abi():
    from wisdome_writer.infrastructure.event_routes import (
        payload_schema_for,
        route_for,
    )

    expected = (
        "article_id",
        "article_revision_id",
        "editorial_policy_snapshot_id",
        "editorial_policy_material_hash",
        "verification_manifest_hash",
        "input_evidence_manifest_hash",
        "excluded_material_manifest_hash",
    )
    route = route_for("editorial.revalidate_requested", 1)
    schema = payload_schema_for("editorial.revalidate_requested", 1)
    assert route.argument_keys == expected
    assert tuple(schema.fields) == expected


def test_quality_failed_run_never_enqueues_auto_publication():
    from types import SimpleNamespace

    from apps.editorial.tasks import _maybe_enqueue_auto_publication

    run = SimpleNamespace(
        state="failed",
        stop_requested_at=None,
        trigger="schedule",
        approval_mode="validated_auto",
    )
    context = SimpleNamespace(database_alias="default")
    assert _maybe_enqueue_auto_publication(run, context) is False


def test_revision_origin_run_is_independent_from_reused_article_origin():
    from types import SimpleNamespace

    from apps.editorial.services import revision_origin_run_id

    revision = SimpleNamespace(
        origin_run_id="run-b",
        article=SimpleNamespace(source_run_id="run-a"),
        event_verification=SimpleNamespace(origin_run_id="run-b"),
    )
    assert revision_origin_run_id(revision) == "run-b"


def test_visual_evidence_placement_requires_frozen_rights_basis():
    import uuid

    from apps.editorial.models import VisualPlacement

    row = VisualPlacement(
        source_evidence_id=uuid.uuid4(),
        block_id="facts-1",
        display_order=0,
        locator_snapshot={"page_index": 0},
        rights_status_snapshot="allowed",
        attribution_snapshot=None,
        alt_text_snapshot="공고 이미지",
        caption="분양가는 3억 원이다. [S1]",
        caption_claim_marker="S1",
        presentation_hash="a" * 64,
    )
    with pytest.raises(ValidationError, match="rights basis"):
        row.full_clean(
            exclude={"revision", "source_evidence"},
            validate_unique=False,
            validate_constraints=False,
        )


def test_0003_postgresql_guards_freeze_full_lineage_and_terminal_children():
    import importlib
    from types import SimpleNamespace

    migration = importlib.import_module(
        "apps.editorial.migrations.0003_editorial_policy_runtime"
    )
    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql):
            statements.append(sql)

    editor = SimpleNamespace(
        connection=SimpleNamespace(
            vendor="postgresql",
            cursor=Cursor,
        ),
        quote_name=lambda value: value,
    )
    migration.install_editorial_policy_guards(None, editor)
    sql = "\n".join(statements)
    for field in (
        "origin_run_id",
        "generation_attempt_id",
        "base_revision_id",
        "provenance_kind",
        "claim_manifest_hash",
        "revalidation_event_key",
        "generator_version",
        "object_version",
    ):
        assert field in sql
    assert "editorial_reject_terminal_child_insert" in sql
    assert "editorial_claimevidence" in sql
    assert "Terminal ArticleRevision cannot accept children" in sql


@pytest.mark.parametrize(
    ("task_name", "task_args"),
    [
        (
            "revalidate_manual_revision",
            (
                "00000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000002",
                "00000000-0000-0000-0000-000000000003",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
            ),
        ),
        (
            "finalize_manual_revalidation_delivery_failure",
            (
                "00000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000002",
                "delivery_exhausted",
            ),
        ),
    ],
)
def test_manual_revalidation_claims_event_before_domain_locks(
    task_name,
    task_args,
):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from apps.editorial import tasks

    order = []
    article_query = MagicMock()
    article_query.filter.return_value.values_list.return_value.first.return_value = (
        "00000000-0000-0000-0000-000000000003"
    )
    article = SimpleNamespace(id=task_args[0])
    article_query.select_for_update.return_value.select_related.return_value.get.return_value = (
        article
    )
    article_query.select_for_update.return_value.get.return_value = article
    revision = SimpleNamespace(
        id=task_args[1],
        editorial_policy_snapshot_id=(
            task_args[2] if task_name == "revalidate_manual_revision" else None
        ),
        editorial_policy_hash=(
            task_args[3] if task_name == "revalidate_manual_revision" else ""
        ),
        verification_manifest_hash=(
            task_args[4] if task_name == "revalidate_manual_revision" else ""
        ),
        evidence_manifest_hash=(
            task_args[5] if task_name == "revalidate_manual_revision" else ""
        ),
        exclusion_manifest_hash=(
            task_args[6] if task_name == "revalidate_manual_revision" else ""
        ),
        claim_graph_state="queued",
        quality_state="pending",
    )
    revision_query = MagicMock()
    revision_query.select_for_update.return_value.select_related.return_value.get.return_value = (
        revision
    )
    domain_query = MagicMock()

    def event_fence(**_kwargs):
        order.append("event")

    def run_manager_using(_alias):
        order.append("domain")
        return domain_query

    context = SimpleNamespace(
        database_alias="default",
        event_key="00000000-0000-0000-0000-000000000004",
        worker_lease_generation=1,
        worker_lease_token="00000000-0000-0000-0000-000000000005",
    )
    with (
        patch.object(tasks, "_worker_audit_context", return_value=context),
        patch.object(tasks, "require_worker_event", side_effect=event_fence),
        patch.object(tasks.DraftArticle.objects, "using", return_value=article_query),
        patch.object(tasks.CollectionRun.objects, "using", side_effect=run_manager_using),
        patch.object(tasks.ArticleRevision.objects, "using", return_value=revision_query),
        patch.object(tasks.transaction, "atomic", return_value=nullcontext()),
        patch.object(tasks, "revalidate_manual_revision_locked", return_value=revision),
        patch.object(tasks, "terminalize_manual_revision_locked"),
    ):
        getattr(tasks, task_name).run(*task_args)

    assert order[:2] == ["event", "domain"]


class EditorialModelContractTests(TestCase):
    def test_terminal_generation_attempt_is_database_append_only(self):
        from django.utils import timezone

        from apps.collection.models import CollectionRun
        from apps.editorial.models import DraftArticle, GenerationAttempt
        from apps.editorial.policies import resolve_editorial_policy_snapshot
        from apps.topics.models import SourceRegistrySnapshot, TopicPolicy

        sha = "a" * 64
        now = timezone.now()
        topic_policy = TopicPolicy.objects.create(
            code="housing_subscription",
            version=1,
            title="test",
            freshness_minutes=60,
            policy={},
            policy_hash=sha,
        )
        registry = SourceRegistrySnapshot.objects.create(
            topic_code="housing_subscription",
            version=1,
            manifest_hash=sha,
        )
        run = CollectionRun.objects.create(
            display_id="RUN-T018-ATTEMPT",
            topic_code="housing_subscription",
            window_start=now - timedelta(hours=1),
            window_end=now,
            source_registry=registry,
            registry_manifest_hash=sha,
            topic_policy=topic_policy,
            policy_version=1,
            policy_hash=sha,
            freshness_minutes=60,
            allowed_authority_tiers=["primary_official"],
            freshness_cutoff=now - timedelta(hours=1),
            request_fingerprint="b" * 64,
            state="awaiting_approval",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(root, policy_document())
            snapshot = resolve_editorial_policy_snapshot(
                "housing_subscription",
                root=root,
            )
        article = DraftArticle.objects.create(
            article_identity_key="t018-attempt-article",
            topic_code="housing_subscription",
            article_type="housing_notice",
            source_run=run,
            state="drafting",
        )
        attempt = GenerationAttempt.objects.create(
            article=article,
            origin_run=run,
            input_manifest_hash=sha,
            generation_manifest_hash=sha,
            editorial_policy_snapshot=snapshot,
            editorial_policy_version=snapshot.policy_version,
            editorial_policy_hash=snapshot.material_hash,
            verification_manifest=[],
            verification_manifest_hash=sha,
            evidence_manifest=[],
            evidence_manifest_hash=sha,
            exclusion_manifest=[],
            exclusion_manifest_hash=sha,
            visual_manifest=[],
            visual_manifest_hash=sha,
            generation_pipeline_manifest_hash=sha,
            state="running",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GenerationAttempt.objects.filter(pk=attempt.id).update(
                    generator_version="tampered-v9"
                )
        GenerationAttempt.objects.filter(pk=attempt.id).update(
            state="succeeded",
            output_checksum="c" * 64,
            finished_at=now,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GenerationAttempt.objects.filter(pk=attempt.id).update(
                    state="failed",
                    error_detail_redacted="late failure",
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GenerationAttempt.objects.filter(pk=attempt.id).delete()

    def test_manual_revision_atomically_stales_previous_open_intents(self):
        import uuid

        from django.contrib.auth import get_user_model
        from django.utils import timezone

        from apps.collection.models import CollectionRun
        from apps.editorial.models import (
            ArticleRevision,
            DraftArticle,
        )
        from apps.editorial.policies import resolve_editorial_policy_snapshot
        from apps.editorial.services import (
            _stale_open_publication_intents_locked,
        )
        from apps.publishing.models import PublicationIntent
        from apps.topics.models import SourceRegistrySnapshot, TopicPolicy

        sha = "a" * 64
        now = timezone.now()
        user = get_user_model().objects.create_user(
            email="t018-intents@example.test",
            password="not-used",
            is_staff=True,
        )
        topic_policy = TopicPolicy.objects.create(
            code="housing_subscription",
            version=1,
            title="test",
            freshness_minutes=60,
            policy={},
            policy_hash=sha,
        )
        registry = SourceRegistrySnapshot.objects.create(
            topic_code="housing_subscription",
            version=1,
            manifest_hash=sha,
        )
        run = CollectionRun.objects.create(
            display_id="RUN-T018-INTENTS",
            topic_code="housing_subscription",
            window_start=now - timedelta(hours=1),
            window_end=now,
            source_registry=registry,
            registry_manifest_hash=sha,
            topic_policy=topic_policy,
            policy_version=1,
            policy_hash=sha,
            freshness_minutes=60,
            allowed_authority_tiers=["primary_official"],
            freshness_cutoff=now - timedelta(hours=1),
            request_fingerprint="b" * 64,
            state="awaiting_approval",
        )
        document = policy_document()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(root, document)
            policy_snapshot = resolve_editorial_policy_snapshot(
                "housing_subscription", root=root
            )
        article = DraftArticle.objects.create(
            article_identity_key="t018-intent-article",
            topic_code="housing_subscription",
            article_type="housing_notice",
            source_run=run,
            state="review_ready",
        )
        revision = ArticleRevision.objects.create(
            article=article,
            origin_run=run,
            revision_no=1,
            editorial_policy_snapshot=policy_snapshot,
            editorial_policy_version=policy_snapshot.policy_version,
            editorial_policy_hash=policy_snapshot.material_hash,
            title="title",
            summary="summary",
            body_markdown="body",
            input_manifest_hash=sha,
            claim_manifest_hash=sha,
            quality_manifest_hash=sha,
        )
        article.current_revision = revision
        article.save(update_fields=("current_revision", "updated_at"))
        states = (
            "draft",
            "awaiting_approval",
            "approved",
            "dispatched",
            "cancelled",
            "stale",
        )
        intent_ids = {}
        prior_intent = None
        for state in states:
            intent = PublicationIntent.objects.create(
                article_id=article.id,
                article_revision=revision,
                revision_no=revision.revision_no,
                revision_content_hash=revision.content_hash,
                target_snapshot_refs=[],
                target_commands=[],
                target_snapshot_manifest_hash=sha,
                approval_mode="manual",
                input_evidence_manifest_hash=sha,
                quality_gate_manifest_hash=sha,
                quality_report_hash=sha,
                intent_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                request_key=f"intent-{state}",
                request_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                request_hash_version="publication-intent-request-v1",
                supersedes_intent=prior_intent,
                state=state,
                created_by=user,
            )
            intent_ids[state] = intent.id
            prior_intent = intent

        with self.assertRaisesRegex(RuntimeError, "rollback"), transaction.atomic():
            _stale_open_publication_intents_locked(
                revision_id=revision.id,
                using="default",
            )
            raise RuntimeError("rollback")
        self.assertEqual(
            PublicationIntent.objects.get(pk=intent_ids["approved"]).state,
            "approved",
        )

        with transaction.atomic():
            changed = _stale_open_publication_intents_locked(
                revision_id=revision.id,
                using="default",
            )

        self.assertEqual(changed, 3)
        observed = {
            state: PublicationIntent.objects.get(pk=intent_id).state
            for state, intent_id in intent_ids.items()
        }
        self.assertEqual(observed["draft"], "stale")
        self.assertEqual(observed["awaiting_approval"], "stale")
        self.assertEqual(observed["approved"], "stale")
        self.assertEqual(observed["dispatched"], "dispatched")
        self.assertEqual(observed["cancelled"], "cancelled")
        self.assertEqual(observed["stale"], "stale")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArticleRevision.objects.filter(pk=revision.id).update(
                    provenance_kind="tampered"
                )

        ArticleRevision.objects.filter(pk=revision.id).update(
            claim_graph_state="passed",
            quality_state="passed",
        )
        from apps.editorial.models import Claim

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Claim.objects.create(
                    revision=revision,
                    block_id="late-fact",
                    claim_type="fact",
                    text="late mutation",
                    position=99,
                    citation_marker="LATE",
                    high_impact=False,
                    risk_level="normal",
                    verification_state="verified",
                    subject_hash="d" * 64,
                )

    def test_same_policy_version_with_changed_bytes_is_rejected(self):
        from apps.editorial.policies import (
            EditorialPolicyError,
            resolve_editorial_policy_snapshot,
        )

        document = policy_document()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(root, document)
            first = resolve_editorial_policy_snapshot(
                "housing_subscription", root=root
            )
            document["requiredBlockTypes"].append("caution")
            write_policy(root, document)
            with self.assertRaisesRegex(EditorialPolicyError, "version"):
                resolve_editorial_policy_snapshot(
                    "housing_subscription", root=root
                )
        self.assertEqual(first.policy_version, "1.0.0")

    def test_policy_snapshot_database_guard_rejects_update(self):
        from apps.editorial.policies import resolve_editorial_policy_snapshot

        document = policy_document()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(root, document)
            snapshot = resolve_editorial_policy_snapshot(
                "housing_subscription", root=root
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE editorial_editorialpolicysnapshot "
                    "SET policy_version = %s WHERE id = %s",
                    ["changed", snapshot.id.hex],
                )

    def test_visual_placement_requires_exactly_one_provenance_source(self):
        from apps.editorial.models import VisualPlacement

        row = VisualPlacement(
            block_id="facts-1",
            display_order=0,
            rights_status_snapshot="allowed",
            alt_text_snapshot="설명",
            caption="캡션",
            presentation_hash="a" * 64,
        )
        with self.assertRaises(ValidationError):
            row.full_clean(exclude={"revision"})


class EditorialMigrationContractTests(TransactionTestCase):
    reset_sequences = True

    def _fixture_teardown(self):
        # Django's TransactionTestCase flush deletes rows.  The production
        # append-only triggers correctly reject those deletes, so temporarily
        # remove and immediately reinstall only around the test-runner flush.
        import importlib

        migration = importlib.import_module(
            "apps.editorial.migrations.0003_editorial_policy_runtime"
        )
        with connection.schema_editor() as editor:
            migration.remove_editorial_policy_guards(None, editor)
        try:
            super()._fixture_teardown()
        finally:
            with connection.schema_editor() as editor:
                migration.install_editorial_policy_guards(None, editor)

    def test_0003_quarantines_legacy_revision_and_closes_active_run_step(self):
        from django.utils import timezone

        from apps.collection.models import CollectionRun, RunStep
        from apps.editorial.models import (
            ArticleRevision,
            DraftArticle,
            EditorialPolicySnapshot,
        )
        from apps.topics.models import SourceRegistrySnapshot, TopicPolicy

        sha = "a" * 64
        now = timezone.now()
        topic_policy = TopicPolicy.objects.create(
            code="housing_subscription",
            version=1,
            title="test",
            freshness_minutes=60,
            policy={},
            policy_hash=sha,
        )
        registry = SourceRegistrySnapshot.objects.create(
            topic_code="housing_subscription",
            version=1,
            manifest_hash=sha,
        )
        run = CollectionRun.objects.create(
            display_id="RUN-T018-MIGRATION",
            topic_code="housing_subscription",
            window_start=now - timedelta(hours=1),
            window_end=now,
            source_registry=registry,
            registry_manifest_hash=sha,
            topic_policy=topic_policy,
            policy_version=1,
            policy_hash=sha,
            freshness_minutes=60,
            allowed_authority_tiers=["primary_official"],
            freshness_cutoff=now - timedelta(hours=1),
            request_fingerprint="b" * 64,
            state="drafting",
        )
        step = RunStep.objects.create(
            run=run,
            name="draft",
            attempt_no=1,
            correlation_id=run.correlation_id,
            state="queued",
        )
        snapshot = EditorialPolicySnapshot(
            topic_code="housing_subscription",
            policy_key="legacy-test",
            policy_version="1",
            document={"legacy": True},
            release_document_hash=sha,
            config_hash=sha,
            implementation_manifest={"files": []},
            implementation_manifest_hash=sha,
            material_hash=sha,
        )
        EditorialPolicySnapshot.objects.bulk_create([snapshot])
        article = DraftArticle.objects.create(
            article_identity_key="t018-migration-article",
            topic_code="housing_subscription",
            article_type="housing_notice",
            source_run=run,
            state="drafting",
        )
        ArticleRevision.objects.create(
            article=article,
            origin_run=run,
            revision_no=1,
            editorial_policy_snapshot=snapshot,
            editorial_policy_version="1",
            editorial_policy_hash=sha,
            title="legacy title",
            summary="legacy summary",
            body_markdown="legacy body",
            input_manifest_hash=sha,
            claim_manifest_hash=sha,
            quality_manifest_hash=sha,
        )

        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_target = [("editorial", "0002_event_cluster_verification")]
        under_test = [("editorial", "0003_editorial_policy_runtime")]
        try:
            executor.migrate(old_target)
            executor = MigrationExecutor(connection)
            executor.migrate(under_test)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(latest)

        run.refresh_from_db()
        step.refresh_from_db()
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.recovery_state, "manual_required")
        self.assertEqual(
            run.error_summary["errorCode"],
            "legacy_editorial_material_unproven",
        )
        self.assertEqual(step.state, "failed")
        self.assertIsNone(step.retry_at)
        self.assertEqual(step.lease_owner, "")
        self.assertIsNone(step.lease_token)
