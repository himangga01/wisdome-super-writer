from __future__ import annotations

import re

from .base import EvidenceInput, GeneratedArticle


def _clean(text: str, limit: int = 700) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


class SourceGroundedTemplateGenerator:
    """Deterministic MVP writer that never creates facts outside supplied evidence."""

    def generate(self, *, topic: str, evidence: list[EvidenceInput], article_type: str) -> GeneratedArticle:
        if not evidence:
            raise ValueError("At least one publishable evidence item is required")
        lead = evidence[0]
        title_prefix = "청약 공고" if topic == "housing_subscription" else "반도체 브리핑"
        title = f"{title_prefix}: {lead.title}"[:180]
        summary_parts = [_clean(item.text, 220) for item in evidence[:3] if item.text]
        summary = " ".join(summary_parts)[:600] or "공식 출처의 최신 자료를 확인했습니다."

        if topic == "housing_subscription":
            sections = [
                ("핵심 요약", summary),
                ("청약 일정과 공급 정보", "아래 원문 공고의 일정·공급·자격 내용을 신청 전에 다시 확인하세요."),
                ("확인할 점", "공고가 정정될 수 있으므로 신청 직전 공식 공고문과 청약 시스템의 최신 상태를 확인해야 합니다."),
            ]
        else:
            sections = [
                ("핵심 요약", summary),
                ("확인된 사실", "아래 내용은 표시된 정부·기업·산업 출처에서 확인된 범위만 정리했습니다."),
                ("산업적 의미", "기업 발표와 전망은 확정 사실과 구분해 해석해야 하며 투자 판단의 근거로 단독 사용해서는 안 됩니다."),
            ]

        claims: list[dict] = []
        body = []
        for heading, paragraph in sections:
            body.extend([f"## {heading}", "", paragraph, ""])
        body.extend(["## 근거별 내용", ""])
        for index, item in enumerate(evidence, start=1):
            statement = _clean(item.text)
            if not statement:
                continue
            marker = f"S{index}"
            body.extend([f"### {item.title}", "", f"{statement} [{marker}]", ""])
            claims.append(
                {
                    "text": statement,
                    "claimType": "fact",
                    "evidenceId": item.evidence_id,
                    "citationMarker": marker,
                }
            )
        body.extend(["## 출처", ""])
        for index, item in enumerate(evidence, start=1):
            date = f" · {item.published_at}" if item.published_at else ""
            body.append(f"- [S{index}] [{item.title}]({item.url}) · {item.publisher}{date}")
        return GeneratedArticle(title=title, summary=summary, body_markdown="\n".join(body), claims=tuple(claims))
