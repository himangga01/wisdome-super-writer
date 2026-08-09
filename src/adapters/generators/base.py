from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EvidenceInput:
    evidence_id: str
    title: str
    url: str
    publisher: str
    text: str
    published_at: str | None
    authority_tier: str | None = None
    semantic_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratedBlock:
    block_id: str
    block_type: str
    content: str


@dataclass(frozen=True)
class GeneratedClaim:
    block_id: str
    statement: str
    claim_type: str
    evidence_ids: tuple[str, ...]
    citation_marker: str
    source_spans: tuple[tuple[str, str], ...]
    high_impact: bool = False
    semantic_key: str | None = None
    actor: str | None = None
    attribution: str | None = None
    derived_from_claim_ids: tuple[str, ...] = ()
    horizon: str | None = None
    uncertainty_note: str | None = None


@dataclass(frozen=True)
class GeneratedArticle:
    title: str
    summary: str
    body_blocks: tuple[GeneratedBlock, ...]
    claims: tuple[GeneratedClaim, ...]

    @property
    def body_markdown(self) -> str:
        return "\n\n".join(block.content for block in self.body_blocks)


class ArticleGenerator(Protocol):
    def generate(self, *, topic: str, evidence: list[EvidenceInput], article_type: str) -> GeneratedArticle: ...
