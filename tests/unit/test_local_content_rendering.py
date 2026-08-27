from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from apps.local_content.contracts import (
    CollectionWindow,
    HousingCollectionResult,
    HousingNotice,
    SourceRunReport,
)
from apps.local_content.rendering import (
    UNKNOWN_VALUE,
    RenderValidationError,
    render_detailed_article,
    render_weekly_index,
)

SEOUL = ZoneInfo("Asia/Seoul")


def _notice(**overrides: object) -> HousingNotice:
    fields: dict[str, object] = {
        "source_key": "applyhome",
        "external_id": "applyhome:apt:2026000001:2026000001",
        "canonical_url": "https://www.applyhome.co.kr/notice/2026000001",
        "title": '서울 "해오름": A단지 주택공급',
        "publisher": "청약홈",
        "category": "apt",
        "region": "서울특별시",
        "status": "공고중",
        "published_at": datetime(2026, 8, 28, 9, 0, tzinfo=SEOUL),
        "application_start": date(2026, 9, 7),
        "application_end": date(2026, 9, 9),
        "announcement_date": date(2026, 9, 16),
        "supply_count": 1147,
        "price_summary": "분양가 공고문 참조",
        "eligibility_summary": ("서울특별시 거주자", "무주택 세대구성원"),
        "restriction_summary": ("재당첨 제한은 공고문 기준",),
        "facts": (("주택형", "59㎡, 84㎡"),),
        "source_checksum": "a" * 64,
        "parser_version": "applyhome-html-v1",
    }
    fields.update(overrides)
    return HousingNotice(**fields)  # type: ignore[arg-type]


def _result(*notices: HousingNotice) -> HousingCollectionResult:
    window = CollectionWindow(
        start=datetime(2026, 8, 22, tzinfo=SEOUL),
        end=datetime(2026, 8, 28, 23, 59, tzinfo=SEOUL),
    )
    reports = (
        SourceRunReport(source_key="applyhome", notices=notices),
        SourceRunReport(source_key="lh", warnings=("일부 선택 항목 누락",)),
    )
    return HousingCollectionResult(window=window, notices=notices, source_reports=reports)


def test_detail_article_uses_approved_information_order() -> None:
    rendered = render_detailed_article(_notice())

    headings = [
        line for line in rendered.factual_markdown.splitlines() if line.startswith("## ")
    ]
    assert headings == [
        "## 한눈에 보기",
        "## 공식 공고",
        "## 위치와 공급 규모",
        "## 청약 일정",
        "## 비용과 자금 확인",
        "## 신청 자격과 제한사항",
        "## 신청 전 체크리스트",
        "## 반드시 다시 확인할 내용",
        "## 출처와 이미지 정보",
    ]
    assert "수익 보장" not in rendered.factual_markdown
    assert "대출 가능" not in rendered.factual_markdown


def test_unknown_values_use_exact_notice_confirmation_wording() -> None:
    rendered = render_detailed_article(
        _notice(
            region=None,
            application_start=None,
            application_end=None,
            announcement_date=None,
            supply_count=None,
            price_summary=None,
            eligibility_summary=(),
            restriction_summary=(),
            facts=(),
        )
    )

    assert UNKNOWN_VALUE == "공고문에서 직접 확인 필요"
    assert rendered.factual_markdown.count(UNKNOWN_VALUE) >= 7
    assert "예상" not in rendered.factual_markdown


def test_detail_output_separates_yaml_prose_facts_sources_and_protected_anchors() -> None:
    rendered = render_detailed_article(_notice())

    frontmatter = yaml.safe_load(rendered.frontmatter)
    assert frontmatter["title"] == '서울 "해오름": A단지 주택공급'
    assert frontmatter["source_checksum"] == "a" * 64
    assert [block.block_id for block in rendered.prose_blocks] == [
        "intro",
        "context",
        "strategy",
    ]
    assert rendered.sources[0].url == "https://www.applyhome.co.kr/notice/2026000001"
    assert rendered.protected_anchors == (
        '서울 "해오름": A단지 주택공급',
        "청약홈",
        "서울특별시",
        "apt",
        "공고중",
        "서울특별시 거주자",
        "무주택 세대구성원",
        "재당첨 제한은 공고문 기준",
    )
    assert "![" not in "\n".join(block.markdown for block in rendered.prose_blocks)
    assert "![" in rendered.factual_markdown


def test_detail_render_is_deterministic_and_does_not_mutate_notice() -> None:
    notice = _notice()

    first = render_detailed_article(notice)
    second = render_detailed_article(notice)

    assert first == second
    assert notice.eligibility_summary == ("서울특별시 거주자", "무주택 세대구성원")
    assert first.to_markdown().encode("utf-8").decode("utf-8") == first.to_markdown()


def test_detail_document_interleaves_only_prose_at_stable_information_slots() -> None:
    document = render_detailed_article(_notice()).to_markdown()

    assert document.count("<!-- WSW:block:intro -->") == 1
    assert document.count("<!-- WSW:block:context -->") == 1
    assert document.count("<!-- WSW:block:strategy -->") == 1
    assert (
        document.index("<!-- WSW:block:intro -->")
        < document.index("![")
        < document.index("## 신청 자격과 제한사항")
        < document.index("<!-- WSW:block:context -->")
        < document.index("## 신청 전 체크리스트")
        < document.index("<!-- WSW:block:strategy -->")
        < document.index("## 반드시 다시 확인할 내용")
    )


def test_weekly_index_includes_every_notice_and_links_only_selected_details() -> None:
    sale = _notice(external_id="sale", title="분양 공고")
    rental = _notice(
        source_key="lh",
        external_id="rental",
        canonical_url="https://apply.lh.or.kr/notice/rental",
        title="매입임대 공고",
        publisher="한국토지주택공사",
        category="purchase_lease",
        source_checksum="b" * 64,
    )

    rendered = render_weekly_index(_result(rental, sale), selected_ids=("rental",))

    assert "분양 공고" in rendered.factual_markdown
    assert "매입임대 공고" in rendered.factual_markdown
    assert f"./{render_detailed_article(sale).slug}/article.md" in rendered.factual_markdown
    assert f"./{render_detailed_article(rental).slug}/article.md" in rendered.factual_markdown
    assert rendered.factual_markdown.index("매입임대 공고") < rendered.factual_markdown.index(
        "분양 공고"
    )
    assert "일부 선택 항목 누락" in rendered.factual_markdown


def test_notice_specific_official_facts_never_enter_mutable_prose() -> None:
    notice = _notice()
    rendered = render_detailed_article(notice)
    prose = "\n".join(block.markdown for block in rendered.prose_blocks)

    assert notice.title not in prose
    assert notice.publisher not in prose
    assert notice.region not in prose
    assert notice.status not in prose
    assert notice.region in rendered.factual_markdown


def test_whitespace_only_identity_fields_use_the_exact_unknown_value() -> None:
    rendered = render_detailed_article(_notice(title=" \t", publisher="\n", status="  "))

    assert rendered.title == UNKNOWN_VALUE
    assert rendered.sources[0].title == UNKNOWN_VALUE
    assert rendered.sources[0].publisher == UNKNOWN_VALUE
    assert UNKNOWN_VALUE in rendered.factual_markdown
    assert all(anchor.strip() for anchor in rendered.protected_anchors)


@pytest.mark.parametrize(
    "canonical_url",
    [
        "http://www.applyhome.co.kr/notice/1",
        "https://user@www.applyhome.co.kr/notice/1",
        "https://evil.example/notice/1",
        "https://www.applyhome.co.kr/notice/1\n## injected",
        "https://www.applyhome.co.kr/notice/\x01bad",
        "https://www.applyhome.co.kr/notice/%0Ainjected",
    ],
)
def test_detail_rejects_non_official_or_unsafe_canonical_urls(canonical_url: str) -> None:
    with pytest.raises(RenderValidationError, match="canonical URL"):
        render_detailed_article(_notice(canonical_url=canonical_url))


def test_detail_rejects_invalid_source_checksum() -> None:
    with pytest.raises(RenderValidationError, match="source checksum"):
        render_detailed_article(_notice(source_checksum="not-sha256"))


def test_official_url_destination_is_percent_encoded_before_markdown() -> None:
    rendered = render_detailed_article(
        _notice(canonical_url="https://www.applyhome.co.kr/notice/a)>?next=b)>")
    )

    assert rendered.sources[0].url == (
        "https://www.applyhome.co.kr/notice/a%29%3E?next=b%29%3E"
    )
    assert "a)>" not in rendered.factual_markdown
    assert "%29%3E" in rendered.factual_markdown


def test_untrusted_scalar_newlines_cannot_inject_markdown_headings() -> None:
    rendered = render_detailed_article(
        _notice(
            title="정상 공고\n## 악성 제목",
            publisher="공식 기관\n## 악성 기관",
            status="공고중\n## 악성 상태",
        )
    )

    headings = [line for line in rendered.factual_markdown.splitlines() if line.startswith("## ")]
    assert "## 악성 제목" not in headings
    assert "## 악성 기관" not in headings
    assert "## 악성 상태" not in headings
    assert rendered.title == "정상 공고 ## 악성 제목"


def test_whitespace_and_format_only_fact_and_policy_values_use_exact_unknown() -> None:
    rendered = render_detailed_article(
        _notice(
            facts=(("\u200b", " \t"), ("주택형", "\u200b")),
            eligibility_summary=(" \t", "\u200b"),
            restriction_summary=("\u200b",),
        )
    )

    assert f"- {UNKNOWN_VALUE}: {UNKNOWN_VALUE}" in rendered.factual_markdown
    assert f"- 주택형: {UNKNOWN_VALUE}" in rendered.factual_markdown
    assert rendered.factual_markdown.count(f"- {UNKNOWN_VALUE}") >= 4


def test_weekly_category_normalizes_format_only_value_to_exact_unknown() -> None:
    notice = _notice(category="\u200b")

    rendered = render_weekly_index(_result(notice))

    assert f"- {UNKNOWN_VALUE}: 1건" in rendered.factual_markdown
    assert f"| {UNKNOWN_VALUE} |" in rendered.factual_markdown
