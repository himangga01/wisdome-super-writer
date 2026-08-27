from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from apps.local_content.contracts import CollectionWindow, HousingNotice, SourceRunReport
from apps.local_content.selection import is_residential, needs_detailed_article
from apps.local_content.workflow import merge_source_reports

SEOUL = ZoneInfo("Asia/Seoul")


@pytest.fixture
def window() -> CollectionWindow:
    return CollectionWindow(
        start=datetime(2026, 8, 22, tzinfo=SEOUL),
        end=datetime(2026, 8, 28, 23, 59, tzinfo=SEOUL),
    )


@pytest.fixture
def notice_factory():
    def make_notice(**overrides: object) -> HousingNotice:
        fields: dict[str, object] = {
            "source_key": "applyhome",
            "external_id": "applyhome:apt:1:1",
            "canonical_url": "https://www.applyhome.co.kr/notice/1",
            "title": "주택 공급 공고",
            "publisher": "ApplyHome",
            "category": "apt",
            "region": "서울",
            "status": "published",
            "published_at": datetime(2026, 8, 28, 9, tzinfo=SEOUL),
            "source_checksum": "a" * 64,
        }
        fields.update(overrides)
        return HousingNotice(**fields)  # type: ignore[arg-type]

    return make_notice


def report(*notices: HousingNotice, **overrides: object) -> SourceRunReport:
    fields: dict[str, object] = {
        "source_key": notices[0].source_key if notices else "applyhome",
        "notices": notices,
    }
    fields.update(overrides)
    return SourceRunReport(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "category",
    [
        "분양주택",
        "공공분양",
        "국민임대",
        "영구임대",
        "통합공공임대",
        "행복주택",
        "매입임대",
        "remaining",
    ],
)
def test_residential_categories_are_included(category: str, notice_factory) -> None:
    assert is_residential(notice_factory(category=category))


@pytest.mark.parametrize(
    "category",
    [
        "토지",
        "공공임대상가(추첨)",
        "산업시설용지",
        "주차장용지",
        "어린이집 운영자",
        "비주거용 경매",
    ],
)
def test_non_residential_categories_are_excluded(category: str, notice_factory) -> None:
    assert not is_residential(notice_factory(category=category))


def test_unknown_category_is_not_admitted_by_title_keywords(notice_factory) -> None:
    notice = notice_factory(category="알 수 없는 공고", title="무순위 잔여세대 공급")

    assert is_residential(notice) is False
    assert needs_detailed_article(notice) is False


@pytest.mark.parametrize("category", ["apt", "분양주택", "공공분양"])
def test_sale_categories_always_need_detailed_article(category: str, notice_factory) -> None:
    assert needs_detailed_article(notice_factory(category=category, title="일반 공급"))


@pytest.mark.parametrize("keyword", ["무순위", "잔여세대", "임의공급", "취소분", "불법행위재공급"])
def test_only_approved_title_keywords_select_residential_details(
    keyword: str, notice_factory
) -> None:
    assert needs_detailed_article(notice_factory(category="국민임대", title=f"주택 {keyword} 공고"))


def test_unapproved_title_keyword_does_not_select_detail_article(notice_factory) -> None:
    notice = notice_factory(category="국민임대", title="긴급 특별 우선 공급")

    assert needs_detailed_article(notice) is False


def test_remaining_category_needs_an_approved_detail_keyword(notice_factory) -> None:
    notice = notice_factory(category="remaining", title="일반 공급 공고")

    assert needs_detailed_article(notice) is False


def test_selected_id_selects_a_residential_detail_article(notice_factory) -> None:
    notice = notice_factory(category="국민임대", external_id="lh:notice:1")

    assert needs_detailed_article(notice, selected_ids=("lh:notice:1",))


def test_conflicting_same_source_identity_is_quarantined(
    notice_factory, window: CollectionWindow
) -> None:
    first = notice_factory(external_id="n1", source_checksum="a" * 64)
    second = notice_factory(external_id="n1", source_checksum="b" * 64)

    result = merge_source_reports((report(first, second),), window)

    assert result.notices == ()
    assert [notice.external_id for notice in result.conflicts] == ["n1", "n1"]


def test_same_source_identity_and_checksum_are_deduplicated(
    notice_factory, window: CollectionWindow
) -> None:
    first = notice_factory(external_id="n1")
    duplicate = notice_factory(external_id="n1")

    result = merge_source_reports((report(first, duplicate),), window)

    assert result.notices == (first,)
    assert result.conflicts == ()
    assert result.excluded_count == 0


def test_same_external_id_from_different_sources_is_not_fuzzy_merged(
    notice_factory, window: CollectionWindow
) -> None:
    applyhome = notice_factory(source_key="applyhome", external_id="same", title="ApplyHome 공고")
    lh = notice_factory(
        source_key="lh",
        external_id="same",
        title="LH 공고",
        publisher="LH",
        source_checksum="b" * 64,
    )

    result = merge_source_reports((report(applyhome), report(lh)), window)

    assert {notice.source_key for notice in result.notices} == {"applyhome", "lh"}
    assert result.conflicts == ()


def test_merge_excludes_non_residential_and_out_of_window_notices(
    notice_factory, window: CollectionWindow
) -> None:
    land = notice_factory(external_id="land", category="토지")
    old = notice_factory(
        external_id="old",
        published_at=datetime(2026, 8, 21, 23, 59, tzinfo=SEOUL),
    )

    result = merge_source_reports((report(land, old),), window)

    assert result.notices == ()
    assert result.excluded_count == 2


def test_merge_preserves_source_health_and_orders_notices_deterministically(
    notice_factory, window: CollectionWindow
) -> None:
    older = notice_factory(
        external_id="z",
        publisher="Zed",
        published_at=datetime(2026, 8, 27, 9, tzinfo=SEOUL),
    )
    latest_zed = notice_factory(external_id="z2", publisher="Zed")
    latest_applyhome = notice_factory(external_id="a", publisher="ApplyHome")
    failed_report = report(
        latest_zed,
        older,
        source_key="lh",
        warnings=("list warning",),
        errors=("list error",),
    )
    successful_report = report(latest_applyhome, source_key="applyhome")

    result = merge_source_reports((failed_report, successful_report), window)

    assert result.source_reports == (successful_report, failed_report)
    assert result.complete is False
    assert result.source_reports[1].warnings == ("list warning",)
    assert result.source_reports[1].errors == ("list error",)
    assert [notice.external_id for notice in result.notices] == ["a", "z2", "z"]
