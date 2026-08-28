"""Deterministic Markdown rendering for normalized local housing notices."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from apps.local_content.contracts import HousingCollectionResult, HousingNotice
from apps.local_content.selection import needs_detailed_article, normalized_category

UNKNOWN_VALUE = "공고문에서 직접 확인 필요"
GENERIC_HERO_ALT = "주거 공고 이해를 위한 일반적인 현대 아파트 도시 전경"
GENERIC_HERO_CAPTION = "이해를 돕기 위한 이미지 · 실제 단지 모습과 다를 수 있음"
SUMMARY_CARD_ALT = "공고 핵심 정보를 정리한 요약 카드"
SUMMARY_CARD_CAPTION = "정규화된 공식 공고 사실 요약"
TIMELINE_ALT = "공고 신청 일정을 정리한 타임라인"
TIMELINE_CAPTION = "정규화된 공식 공고 일정 요약"
_SHA256 = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
_OFFICIAL_SOURCE_HOSTS = {
    "applyhome": "www.applyhome.co.kr",
    "lh": "apply.lh.or.kr",
}
_CATEGORY_LABELS = {
    "sale": "분양주택",
    "public_sale": "공공분양",
    "remaining": "잔여·무순위 공급",
    "public_rental": "공공임대",
    "national_rental": "국민임대",
    "permanent_rental": "영구임대",
    "integrated_public_rental": "통합공공임대",
    "happy_housing": "행복주택",
    "purchase_lease": "매입·전세임대",
    "non_residential": "비주거 공고",
}
_STATUS_LABELS = {
    "active": "공고 중",
    "open": "공고 중",
    "published": "공고 중",
    "corrected": "정정 공고",
    "correction": "정정 공고",
    "amended": "변경 공고",
    "closed": "접수 마감",
    "retracted": "공고 철회",
    "unavailable": "확인 불가",
}
_WARNING_LABELS = {
    "application schedule not found": "신청 일정을 공고문에서 직접 확인해야 합니다.",
    "application end date not found": "신청 종료일을 공고문에서 직접 확인해야 합니다.",
    "application schedule ambiguous": "신청 일정 표기가 모호해 공고문 확인이 필요합니다.",
    "supply count not found": "공급 규모를 공고문에서 직접 확인해야 합니다.",
    "price summary not found": "가격·보증금·임대료 정보를 공고문에서 직접 확인해야 합니다.",
    "eligibility summary not found": "신청 자격을 공고문에서 직접 확인해야 합니다.",
    "DETAIL_COLLECTION_FAILED": "상세 페이지를 확인하지 못해 공고 원문 확인이 필요합니다.",
    "OFFICIAL_API_RECONCILED": "공식 API 정보와 공개 페이지 정보가 일치했습니다.",
}


class RenderValidationError(ValueError):
    """Raised when normalized source material is unsafe to render."""


@dataclass(frozen=True)
class ProseBlock:
    """One prose-only block that Task 9 may humanize independently."""

    block_id: str
    markdown: str


@dataclass(frozen=True)
class ArticleSource:
    """One immutable official-source reference."""

    source_key: str
    title: str
    publisher: str
    url: str
    checksum: str

    def as_dict(self) -> dict[str, str]:
        return {
            "source_key": self.source_key,
            "title": self.title,
            "publisher": self.publisher,
            "url": self.url,
            "checksum": self.checksum,
        }


@dataclass(frozen=True)
class RenderedArticle:
    """Separated deterministic material for one Markdown output."""

    title: str
    slug: str
    frontmatter: str
    prose_blocks: tuple[ProseBlock, ...]
    factual_markdown: str
    sources: tuple[ArticleSource, ...]
    protected_anchors: tuple[str, ...]

    def to_markdown(self) -> str:
        """Compose a deterministic draft without exposing fact blocks to humanization."""

        parts = ["---", self.frontmatter, "---", "", f"# {_markdown_text(self.title)}", ""]
        slot_blocks = {
            block.block_id: block
            for block in self.prose_blocks
            if f"<!-- WSW:slot:{block.block_id} -->" in self.factual_markdown
        }
        leading_blocks = tuple(
            block for block in self.prose_blocks if block.block_id not in slot_blocks
        )
        for block in leading_blocks:
            parts.extend((*_block_document(block), ""))
        factual = self.factual_markdown.rstrip()
        for block_id, block in slot_blocks.items():
            factual = factual.replace(
                f"<!-- WSW:slot:{block_id} -->",
                "\n".join(_block_document(block)),
            )
        parts.append(factual)
        return "\n".join(parts).rstrip() + "\n"


def render_detailed_article(notice: HousingNotice) -> RenderedArticle:
    """Render one normalized notice without inferring any missing fact."""

    canonical_url = _official_url(notice)
    source_checksum = _source_checksum(notice.source_checksum)
    title = _display_value(notice.title)
    slug = notice_slug(notice)
    sources = (_notice_source(notice, canonical_url=canonical_url),)
    frontmatter = _frontmatter(
        (
            ("title", title),
            ("slug", slug),
            ("article_type", "housing_notice_detail"),
            ("source_key", notice.source_key),
            ("external_id", notice.external_id),
            ("source_checksum", source_checksum),
            ("published_at", notice.published_at.isoformat()),
        )
    )
    prose_blocks = (
        ProseBlock(
            "intro",
            "공고를 검토할 때 먼저 확인할 핵심 항목을 차례대로 정리했습니다.",
        ),
        ProseBlock(
            "context",
            "위치와 공급 정보는 공식 공고의 사실 영역에서만 확인해야 합니다.",
        ),
        ProseBlock(
            "strategy",
            "신청 전에는 공식 공고 원문과 실제 신청 화면을 함께 대조해 확인하세요.",
        ),
    )
    return RenderedArticle(
        title=title,
        slug=slug,
        frontmatter=frontmatter,
        prose_blocks=prose_blocks,
        factual_markdown=_detail_facts(
            notice,
            canonical_url=canonical_url,
            source_checksum=source_checksum,
        ),
        sources=sources,
        protected_anchors=_protected_anchors(notice),
    )


def render_weekly_index(
    result: HousingCollectionResult,
    *,
    selected_ids: Collection[str] = (),
    prior_detailed_ids: Collection[str] = (),
) -> RenderedArticle:
    """Render every admitted notice and deterministic links for detailed selections."""

    for notice in result.notices:
        _official_url(notice)
        _source_checksum(notice.source_checksum)
    start = result.window.start.date().isoformat()
    end = result.window.end.date().isoformat()
    title = f"{start}~{end} 주간 주거 공고"
    slug = f"housing-weekly-{end}"
    frontmatter = _frontmatter(
        (
            ("title", title),
            ("slug", slug),
            ("article_type", "housing_notice_weekly_index"),
            ("window_start", result.window.start.isoformat()),
            ("window_end", result.window.end.isoformat()),
            ("complete", result.complete),
        )
    )
    sources = _unique_sources(result.notices)
    blocks = (
        ProseBlock(
            "intro",
            "이번 주 주거 공고를 한곳에서 차례대로 살펴볼 수 있습니다.",
        ),
        ProseBlock(
            "context",
            "모든 항목은 정규화된 공식 공고 정보이며, 상세 작성 여부와 관계없이 표시됩니다.",
        ),
        ProseBlock(
            "strategy",
            "관심 공고는 공식 링크에서 최신 정정 여부와 신청 조건을 다시 확인하세요.",
        ),
    )
    anchors: list[str] = []
    for notice in result.notices:
        anchors.extend(_protected_anchors(notice))
    return RenderedArticle(
        title=title,
        slug=slug,
        frontmatter=frontmatter,
        prose_blocks=blocks,
        factual_markdown=_weekly_facts(
            result,
            selected_ids=selected_ids,
            prior_detailed_ids=prior_detailed_ids,
        ),
        sources=sources,
        protected_anchors=_ordered_unique(anchors),
    )


def notice_slug(notice: HousingNotice) -> str:
    """Return a portable stable slug derived only from official identity."""

    identity = f"{notice.source_key}:{notice.external_id}"
    normalized = unicodedata.normalize("NFKC", identity).casefold()
    base = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    base = (base or "housing-notice")[:68].rstrip("-")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return f"{base}-{digest}"


def _detail_facts(
    notice: HousingNotice,
    *,
    canonical_url: str,
    source_checksum: str,
) -> str:
    title = _display_value(notice.title)
    publisher = _display_value(notice.publisher)
    status = _display_status(notice.status)
    category = _display_category(notice.category)
    lines = [
        f'![{GENERIC_HERO_ALT}](assets/hero.png "{GENERIC_HERO_CAPTION}")',
        "",
        f'![{SUMMARY_CARD_ALT}](assets/summary-card.webp "{SUMMARY_CARD_CAPTION}")',
        "",
        f'![{TIMELINE_ALT}](assets/timeline.webp "{TIMELINE_CAPTION}")',
        "",
        "## 한눈에 보기",
        "",
        _table(
            (
                ("항목", "확인 내용"),
                ("공고명", title),
                ("기관", publisher),
                ("지역", _known(notice.region)),
                ("공고 분류", category),
                ("공고 상태", status),
                ("공급 규모", _supply(notice.supply_count)),
            )
        ),
        "",
        "## 공식 공고",
        "",
        f"- 공식 공고: [{_markdown_text(title)}](<{canonical_url}>)",
        f"- 공고 기관: {_markdown_text(publisher)}",
        f"- 공고일: {notice.published_at.date().isoformat()}",
        f"- 원문 체크섬(SHA-256): `{source_checksum}`",
        "",
        "## 위치와 공급 규모",
        "",
        f"- 지역: {_markdown_text(_known(notice.region))}",
        f"- 공급 규모: {_markdown_text(_supply(notice.supply_count))}",
    ]
    facts = _publishable_facts(notice.facts)
    if facts:
        lines.extend(f"- {_markdown_text(key)}: {_markdown_text(value)}" for key, value in facts)
    else:
        lines.append(f"- 세부 주택 정보: {UNKNOWN_VALUE}")
    lines.extend(
        (
            "",
            "## 청약 일정",
            "",
            _table(
                (
                    ("일정", "날짜"),
                    ("신청 시작", _date_value(notice.application_start)),
                    ("신청 종료", _date_value(notice.application_end)),
                    ("마감일", _date_value(notice.deadline)),
                    ("당첨자 발표", _date_value(notice.announcement_date)),
                )
            ),
            "",
            "## 비용과 자금 확인",
            "",
            f"- 가격·보증금·임대료: {_markdown_text(_known(notice.price_summary))}",
            f"- 자금 조달 조건: {UNKNOWN_VALUE}",
            "",
            "## 신청 자격과 제한사항",
            "",
            "### 신청 자격",
            "",
            *_fact_list(notice.eligibility_summary),
            "",
            "### 제한사항",
            "",
            *_fact_list(notice.restriction_summary),
            "",
            "<!-- WSW:slot:context -->",
            "",
            "## 신청 전 체크리스트",
            "",
            "<!-- WSW:slot:strategy -->",
            "",
            "- 공식 공고 원문에서 신청 자격을 확인합니다.",
            "- 신청 기간과 당첨자 발표일을 다시 확인합니다.",
            "- 가격·보증금·임대료와 납부 일정을 원문에서 확인합니다.",
            "- 정정 공고가 게시됐는지 공식 사이트에서 확인합니다.",
            "",
            "## 반드시 다시 확인할 내용",
            "",
            *_confirmation_items(notice),
            "",
            "## 출처와 이미지 정보",
            "",
            f"- 공식 출처: [{_markdown_text(publisher)}](<{canonical_url}>)",
            f"- 일반 이미지: {GENERIC_HERO_CAPTION}",
            "- 요약·일정 이미지는 위 공식 공고의 정규화된 사실로 로컬 생성합니다.",
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def _weekly_facts(
    result: HousingCollectionResult,
    *,
    selected_ids: Collection[str],
    prior_detailed_ids: Collection[str],
) -> str:
    category_counts = Counter(_display_category(notice.category) for notice in result.notices)
    publication_counts = Counter(
        notice.published_at.date().isoformat() for notice in result.notices
    )
    lines = [
        "## 수집 범위",
        "",
        f"- 시작: {result.window.start.isoformat()}",
        f"- 종료: {result.window.end.isoformat()}",
        f"- 전체 상태: {'완전' if result.complete else '불완전'}",
        f"- 포함 공고: {len(result.notices)}건",
        f"- 격리 충돌: {len(result.conflicts)}건",
        f"- 제외: {result.excluded_count}건",
        "",
        "## 출처 상태",
        "",
    ]
    for report in result.source_reports:
        status = "완전" if report.complete else "오류"
        lines.append(f"- {_markdown_text(report.source_key)}: {status}")
        lines.extend(
            f"  - 경고: {_markdown_text(_source_diagnostic(value, error=False))}"
            for value in report.warnings
        )
        lines.extend(
            f"  - 오류: {_markdown_text(_source_diagnostic(value, error=True))}"
            for value in report.errors
        )
    lines.extend(("", "## 분류별 건수", ""))
    if category_counts:
        lines.extend(
            f"- {_markdown_text(category)}: {count}건"
            for category, count in sorted(category_counts.items(), key=lambda item: item[0])
        )
    else:
        lines.append("- 해당 기간 공고 없음")
    lines.extend(("", "## 발표일별 건수", ""))
    if publication_counts:
        lines.extend(
            f"- {published_on}: {count}건"
            for published_on, count in sorted(publication_counts.items())
        )
    else:
        lines.append("- 해당 기간 공고 없음")
    lines.extend(
        (
            "",
            "## 이번 주 모든 주거 공고",
            "",
            "| 발표일 | 기관 | 지역 | 분류 | 공고 | 상세 |",
            "| --- | --- | --- | --- | --- | --- |",
        )
    )
    for notice in result.notices:
        detail = UNKNOWN_VALUE
        if needs_detailed_article(
            notice,
            selected_ids=selected_ids,
            prior_detailed_ids=prior_detailed_ids,
        ):
            detail = f"[상세 보기](./{notice_slug(notice)}/article.md)"
        canonical_url = _official_url(notice)
        official = f"[{_table_text(_display_value(notice.title))}](<{canonical_url}>)"
        lines.append(
            "| "
            + " | ".join(
                (
                    notice.published_at.date().isoformat(),
                    _table_text(_display_value(notice.publisher)),
                    _table_text(_known(notice.region)),
                    _table_text(_display_category(notice.category)),
                    official,
                    detail,
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## 안내",
            "",
            "- 상세 글이 없는 공고도 공식 링크에서 원문을 확인할 수 있습니다.",
            "- 신청 판단은 최신 공식 공고와 정정 공고를 기준으로 해야 합니다.",
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def _block_document(block: ProseBlock) -> tuple[str, str, str]:
    return (
        f"<!-- WSW:block:{block.block_id} -->",
        block.markdown,
        f"<!-- WSW:endblock:{block.block_id} -->",
    )


def _protected_anchors(notice: HousingNotice) -> tuple[str, ...]:
    candidates: list[str | None] = [
        notice.title,
        notice.publisher,
        notice.region,
        notice.category,
        notice.status,
        *notice.eligibility_summary,
        *notice.restriction_summary,
    ]
    return _ordered_unique(_safe_scalar(value) for value in candidates if value)


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = " ".join(value.split())
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return tuple(result)


def _notice_source(
    notice: HousingNotice,
    *,
    canonical_url: str | None = None,
) -> ArticleSource:
    return ArticleSource(
        source_key=notice.source_key,
        title=_display_value(notice.title),
        publisher=_display_value(notice.publisher),
        url=canonical_url or _official_url(notice),
        checksum=_source_checksum(notice.source_checksum),
    )


def _unique_sources(notices: Iterable[HousingNotice]) -> tuple[ArticleSource, ...]:
    sources: list[ArticleSource] = []
    seen: set[tuple[str, str]] = set()
    for notice in notices:
        key = (notice.source_key, notice.external_id)
        if key not in seen:
            seen.add(key)
            sources.append(_notice_source(notice))
    return tuple(sources)


def _frontmatter(fields: Iterable[tuple[str, object]]) -> str:
    return "\n".join(f"{key}: {_yaml_scalar(value)}" for key, value in fields)


def _yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _table(rows: Iterable[tuple[str, str]]) -> str:
    material = tuple(rows)
    header, *body = material
    lines = [f"| {_table_text(header[0])} | {_table_text(header[1])} |", "| --- | --- |"]
    lines.extend(f"| {_table_text(left)} | {_table_text(right)} |" for left, right in body)
    return "\n".join(lines)


def _table_text(value: str) -> str:
    return _markdown_text(value).replace("|", "&#124;")


def _markdown_text(value: str) -> str:
    return (
        _safe_scalar(str(value))
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _known(value: str | None) -> str:
    return _display_value(value)


def _display_value(value: str | None) -> str:
    normalized = _safe_scalar(value or "")
    return normalized or UNKNOWN_VALUE


def _display_category(value: str) -> str:
    category = normalized_category(value)
    if category is not None:
        return _CATEGORY_LABELS[category]
    normalized = _safe_scalar(value)
    if not normalized:
        return UNKNOWN_VALUE
    return normalized if re.search(r"[가-힣]", normalized) else "기타 주거 공고"


def _display_status(value: str) -> str:
    normalized = _safe_scalar(value)
    mapped = _STATUS_LABELS.get(normalized.casefold())
    if mapped is not None:
        return mapped
    return normalized if re.search(r"[가-힣]", normalized) else "상태 확인 필요"


def _warning_text(value: str) -> str:
    mapped = _WARNING_LABELS.get(value)
    if mapped is not None:
        return mapped
    normalized = _safe_scalar(value)
    if re.search(r"[가-힣]", normalized):
        return normalized
    return "공고의 일부 정보를 원문에서 다시 확인해야 합니다."


def _source_diagnostic(value: str, *, error: bool) -> str:
    mapped = _WARNING_LABELS.get(value)
    if mapped is not None:
        return mapped
    normalized = _safe_scalar(value)
    if re.search(r"[가-힣]", normalized):
        return normalized
    return (
        "공식 출처 수집 상태를 확인해야 합니다."
        if error
        else "공식 출처의 일부 항목을 확인해야 합니다."
    )


def _safe_scalar(value: str) -> str:
    without_controls = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in str(value)
    )
    return " ".join(without_controls.split())


def _source_checksum(value: str) -> str:
    normalized = value.strip()
    if _SHA256.fullmatch(normalized) is None:
        raise RenderValidationError("source checksum must be SHA-256")
    return normalized


def _official_url(notice: HousingNotice) -> str:
    value = notice.canonical_url
    if not value or value != value.strip() or any(
        unicodedata.category(character) in {"Cc", "Cf"} for character in value
    ):
        raise RenderValidationError("canonical URL contains whitespace or control characters")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise RenderValidationError("canonical URL contains invalid percent encoding")
    if any(
        int(match.group(1), 16) < 32 or int(match.group(1), 16) == 127
        for match in re.finditer(r"%([0-9a-fA-F]{2})", value)
    ):
        raise RenderValidationError("canonical URL contains encoded control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RenderValidationError("canonical URL is invalid") from exc
    expected_host = _OFFICIAL_SOURCE_HOSTS.get(notice.source_key.casefold())
    if (
        parsed.scheme != "https"
        or expected_host is None
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise RenderValidationError("canonical URL must use the official HTTPS source host")
    path = quote(parsed.path, safe="/%-._~!$&'*,;=:@+")
    query = quote(parsed.query, safe="/%-._~!$&'*,;=:@?+")
    return urlunsplit(("https", expected_host, path, query, ""))


def _date_value(value: object | None) -> str:
    if value is None:
        return UNKNOWN_VALUE
    return value.isoformat()  # type: ignore[union-attr]


def _supply(value: int | None) -> str:
    return f"{value:,}세대" if value is not None else UNKNOWN_VALUE


def _fact_list(values: Iterable[str]) -> tuple[str, ...]:
    material = tuple(_display_value(value) for value in values)
    if not material:
        return (f"- {UNKNOWN_VALUE}",)
    return tuple(f"- {_markdown_text(value)}" for value in material)


def _publishable_facts(facts: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for key, value in facts:
        normalized_key = _safe_scalar(key).casefold()
        normalized_value = _safe_scalar(value).casefold()
        if normalized_key == "attachment" or "internal_analysis_only" in normalized_value:
            continue
        result.append((_display_value(key), _display_value(value)))
    return tuple(result)


def _confirmation_items(notice: HousingNotice) -> tuple[str, ...]:
    items = [
        f"- 수집 경고: {_markdown_text(_warning_text(warning))}"
        for warning in notice.warnings
    ]
    if notice.price_summary is None:
        items.append(f"- 가격·보증금·임대료: {UNKNOWN_VALUE}")
    if not any(_safe_scalar(value) for value in notice.eligibility_summary):
        items.append(f"- 신청 자격: {UNKNOWN_VALUE}")
    if not any(_safe_scalar(value) for value in notice.restriction_summary):
        items.append(f"- 제한사항: {UNKNOWN_VALUE}")
    items.extend(
        (
            f"- 대출 승인 여부: {UNKNOWN_VALUE}",
            f"- 경쟁률과 당첨 가능성: {UNKNOWN_VALUE}",
            f"- 기대 수익: {UNKNOWN_VALUE}",
        )
    )
    return tuple(items)
