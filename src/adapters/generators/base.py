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


@dataclass(frozen=True)
class GeneratedArticle:
    title: str
    summary: str
    body_markdown: str
    claims: tuple[dict, ...]


class ArticleGenerator(Protocol):
    def generate(self, *, topic: str, evidence: list[EvidenceInput], article_type: str) -> GeneratedArticle: ...
