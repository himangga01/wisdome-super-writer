from __future__ import annotations

import re

from .base import (
    EvidenceInput,
    GeneratedArticle,
    GeneratedBlock,
    GeneratedClaim,
)


def _clean(text: str, limit: int = 700) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def _first_atomic_sentence(text: str, limit: int = 160) -> str:
    cleaned = _clean(text, limit * 4)
    match = re.search(r".+?[.!?。](?:\s|$)", cleaned)
    sentence = match.group(0).strip() if match else cleaned
    if len(sentence) > limit:
        raise ValueError("Evidence sentence exceeds the atomic quotation limit")
    return sentence


class SourceGroundedTemplateGenerator:
    """Deterministic MVP writer that never creates facts outside supplied evidence."""

    def generate(self, *, topic: str, evidence: list[EvidenceInput], article_type: str) -> GeneratedArticle:
        if not evidence:
            raise ValueError("At least one publishable evidence item is required")
        textual_evidence = [item for item in evidence if item.text.strip()]
        if not textual_evidence:
            raise ValueError("At least one evidence item with text is required")
        lead = textual_evidence[0]
        title = _clean(lead.title, 180)
        if not title:
            raise ValueError("Evidence title is required")
        summary_sources = textual_evidence[:3]
        summary_parts = [_first_atomic_sentence(item.text) for item in summary_sources]
        summary = " ".join(
            f"{statement} [S{index}]"
            for index, statement in enumerate(summary_parts, start=1)
        )[:600]

        title_span = title
        background_span = _first_atomic_sentence(lead.text)
        lead_is_corporate = lead.authority_tier == "primary_corporate"
        claims: list[GeneratedClaim] = [
            GeneratedClaim(
                block_id="title",
                statement=title,
                claim_type="company_claim" if lead_is_corporate else "fact",
                evidence_ids=(lead.evidence_id,),
                citation_marker="T1",
                source_spans=((lead.evidence_id, title_span),),
                semantic_key=None,
                actor=lead.publisher if lead_is_corporate else None,
                attribution=(f"{lead.publisher} 공식 발표" if lead_is_corporate else None),
            )
        ]
        blocks = [
            GeneratedBlock(
                block_id="title",
                block_type="company_claim" if lead_is_corporate else "fact",
                content=f"{title} [T1]",
            ),
            GeneratedBlock(
                block_id="summary",
                block_type=(
                    "company_claim"
                    if summary_sources
                    and all(
                        item.authority_tier == "primary_corporate"
                        for item in summary_sources
                    )
                    else "fact"
                ),
                content=summary,
            ),
            GeneratedBlock(
                block_id="background-1",
                block_type="background",
                content=f"{background_span} [B1]",
            )
        ]
        for index, item in enumerate(summary_sources, start=1):
            source_span = _first_atomic_sentence(item.text)
            statement = source_span
            marker = f"S{index}"
            is_corporate = item.authority_tier == "primary_corporate"
            claims.append(
                GeneratedClaim(
                    block_id="summary",
                    statement=statement,
                    claim_type="company_claim" if is_corporate else "fact",
                    evidence_ids=(item.evidence_id,),
                    citation_marker=marker,
                    source_spans=((item.evidence_id, source_span),),
                    semantic_key=None,
                    actor=item.publisher if is_corporate else None,
                    attribution=(f"{item.publisher} 공식 발표" if is_corporate else None),
                )
            )
        claims.append(
            GeneratedClaim(
                block_id="background-1",
                statement=background_span,
                claim_type="company_claim" if lead_is_corporate else "fact",
                evidence_ids=(lead.evidence_id,),
                citation_marker="B1",
                source_spans=((lead.evidence_id, background_span),),
                semantic_key=None,
                actor=lead.publisher if lead_is_corporate else None,
                attribution=(
                    f"{lead.publisher} 공식 발표"
                    if lead_is_corporate
                    else None
                ),
            )
        )
        source_lines = [
            f"- [T1] [{lead.title}]({lead.url}) · {lead.publisher}",
        ]
        for index, item in enumerate(summary_sources, start=1):
            date = f" · {item.published_at}" if item.published_at else ""
            source_lines.append(
                f"- [S{index}] [{item.title}]({item.url}) · {item.publisher}{date}"
            )
        source_lines.append(
            f"- [B1] [{lead.title}]({lead.url}) · {lead.publisher}"
        )
        blocks.append(
            GeneratedBlock(
                block_id="sources",
                block_type="sources",
                content="\n".join(source_lines),
            )
        )
        return GeneratedArticle(
            title=title,
            summary=summary,
            body_blocks=tuple(blocks),
            claims=tuple(claims),
        )
