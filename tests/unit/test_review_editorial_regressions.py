import uuid

import pytest

from adapters.generators.base import EvidenceInput
from adapters.generators.template import SourceGroundedTemplateGenerator
from apps.editorial.policies import load_editorial_policy
from apps.editorial.quality import evaluate_editorial_quality
from apps.editorial.services import validate_manual_claim_bindings


@pytest.mark.parametrize(
    "price,expected",
    [("3억 원", "passed"), ("8억 원", "failed"), ("3만 원", "failed"), ("300,000,000원", "passed")],
)
def test_manual_factual_price_must_be_supported_by_its_actual_cited_span(price, expected):
    evidence_id = str(uuid.uuid4())
    title = "공식 청약 공고"
    source = "분양가는 3억 원입니다."
    row = {
        "evidenceId": evidence_id,
        "sourceTitle": title,
        "sourceUrl": "https://www.applyhome.co.kr/notice",
        "publisher": "한국부동산원",
        "sourceText": title + "\n" + source,
        "authorityTier": "primary_official",
        "independenceGroup": "official-source",
        "publishedAt": "2026-10-03T10:00:00+09:00",
        "freshnessCutoff": "2026-10-02T10:00:00+09:00",
        "publishable": True,
        "rightsStatus": "allowed",
        "locatorType": "structured_path",
        "locator": {
            "locator_type": "structured_path",
            "path_type": "json_pointer",
            "path": "/body_text",
        },
        "originIdentityHash": "official-origin",
    }
    policy = load_editorial_policy("housing_subscription")
    generated = SourceGroundedTemplateGenerator().generate(
        topic="housing_subscription",
        article_type="housing_notice",
        evidence=[
            EvidenceInput(
                evidence_id=evidence_id,
                title=title,
                url=row["sourceUrl"],
                publisher=row["publisher"],
                text=source,
                published_at=row["publishedAt"],
                authority_tier="primary_official",
            )
        ],
    )
    edited = "분양가는 " + price + "입니다."
    blocks = [
        {
            "id": block.block_id,
            "type": block.block_type,
            "content": block.content.replace(source, edited)
            if block.block_id == "summary"
            else block.content,
        }
        for block in generated.body_blocks
    ]
    bindings = [
        {
            "claimRef": "claim-" + str(index),
            "blockId": claim.block_id,
            "statement": edited if claim.block_id == "summary" else claim.statement,
            "claimType": claim.claim_type,
            "evidenceIds": list(claim.evidence_ids),
            "citationMarker": claim.citation_marker,
            "sourceSpans": dict(claim.source_spans),
            "semanticKey": "price" if claim.block_id == "summary" else None,
            "actor": claim.actor,
            "attribution": claim.attribution,
            "derivedFromClaimRefs": [],
            "horizon": None,
            "uncertaintyNote": None,
        }
        for index, claim in enumerate(generated.claims)
    ]
    claims = validate_manual_claim_bindings(
        blocks=blocks,
        claim_bindings=bindings,
        evidence_manifest=[row],
        policy=policy,
        claim_scope_id=uuid.uuid4(),
    )
    report = evaluate_editorial_quality(
        policy=policy,
        blocks=blocks,
        claims=claims,
        evidence=[row],
        exclusions=[],
        visuals=[],
    )
    assert report.state == expected
    if expected == "failed":
        assert any(
            check.code == "all_publishable_claims_grounded" and check.result == "failed"
            for check in report.checks
        )
