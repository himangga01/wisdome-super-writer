from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from apps.local_content.contracts import CollectionWindow
from apps.local_content.http import HtmlResponse
from apps.local_content.sources.lh import LH_LIST, LhPublicCollector

SEOUL = ZoneInfo("Asia/Seoul")
FIXTURES = Path(__file__).parents[1] / "fixtures" / "lh"
DETAIL_URL = (
    "https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancInfo.do?"
    "aisTpCd=05&ccrCnntSysDsCd=02&mi=1026&panId=0000061158&uppAisTpCd=05"
)
SECOND_DETAIL_URL = (
    "https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancInfo.do?"
    "aisTpCd=01&ccrCnntSysDsCd=01&mi=1026&panId=0000061159&uppAisTpCd=01"
)


@dataclass(frozen=True)
class PostCall:
    url: str
    data: dict[str, str]


class FixtureFetcher:
    def __init__(self, list_pages: dict[str, str], details: dict[str, str]) -> None:
        self.list_pages = list_pages
        self.details = details
        self.posts: list[PostCall] = []
        self.gets: list[str] = []

    def post(self, url: str, *, data: dict[str, str]) -> HtmlResponse:
        self.posts.append(PostCall(url, dict(data)))
        return HtmlResponse(
            url=url,
            status_code=200,
            content_type="text/html",
            body=self.list_pages[data["currPage"]],
            fetched_at=datetime(2026, 8, 28, tzinfo=SEOUL),
        )

    def get(self, url: str) -> HtmlResponse:
        self.gets.append(url)
        return HtmlResponse(
            url=url,
            status_code=200,
            content_type="text/html",
            body=self.details[url],
            fetched_at=datetime(2026, 8, 28, tzinfo=SEOUL),
        )


@pytest.fixture
def window() -> CollectionWindow:
    return CollectionWindow(
        start=datetime(2026, 8, 22, tzinfo=SEOUL),
        end=datetime(2026, 8, 28, 23, 59, tzinfo=SEOUL),
    )


@pytest.fixture
def fetcher() -> FixtureFetcher:
    return FixtureFetcher(
        {
            "1": (FIXTURES / "notice-list-page-1.html").read_text(encoding="utf-8"),
            "2": (FIXTURES / "notice-list-page-2.html").read_text(encoding="utf-8"),
        },
        {
            DETAIL_URL: (FIXTURES / "notice-detail.html").read_text(encoding="utf-8"),
            SECOND_DETAIL_URL: '<main data-lh-notice-detail="true"></main>',
        },
    )


def test_lh_collector_posts_exact_publication_window_and_paginates(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    report = LhPublicCollector(fetcher).collect(window)

    assert report.errors == ()
    assert fetcher.posts[0].url == LH_LIST
    assert fetcher.posts[0].data["schTy"] == "0"
    assert fetcher.posts[0].data["startDt"] == "2026-08-22"
    assert fetcher.posts[0].data["endDt"] == "2026-08-28"
    assert fetcher.posts[0].data["listCo"] == "50"
    assert fetcher.posts[0].data["viewType"] == "srch"
    assert fetcher.posts[0].data["mi"] == "1026"
    assert [call.data["currPage"] for call in fetcher.posts] == ["1", "2"]


def test_lh_identity_and_detail_url_are_derived_from_official_row(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    notice = LhPublicCollector(fetcher).collect(window).notices[0]

    assert notice.external_id == "lh:02:0000061158:05:05"
    assert notice.canonical_url == DETAIL_URL
    assert notice.category == "임대주택"
    assert notice.region == "서울특별시"
    assert notice.application_start is not None
    assert notice.application_start.isoformat() == "2026-09-01"
    assert notice.application_end is not None
    assert notice.application_end.isoformat() == "2026-09-03"
    assert notice.supply_count == 120
    assert notice.price_summary == "보증금 10,000,000원 / 월임대료 350,000원"
    assert notice.eligibility_summary == ("무주택세대구성원",)
    assert fetcher.gets == [DETAIL_URL, SECOND_DETAIL_URL]


def test_lh_collector_keeps_non_residential_rows_for_selection_layer(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    report = LhPublicCollector(fetcher).collect(window)

    assert [notice.category for notice in report.notices] == ["임대주택", "토지"]


def test_lh_collector_rejects_repeated_page_signature(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fetcher.list_pages["2"] = fetcher.list_pages["1"].replace(
        'data-current-page="1"', 'data-current-page="2"', 1
    )

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("repeated list page",)


def test_lh_collector_accepts_explicit_zero_results(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fetcher.list_pages = {
        "1": (
            '<main data-lh-notice-list="true" data-empty-results="true" '
            'data-current-page="1" data-last-page="1" data-total-count="0"></main>'
        )
    }

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ()
    assert len(fetcher.posts) == 1


def test_lh_collector_rejects_an_old_corrected_notice(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fetcher.list_pages["1"] = fetcher.list_pages["1"].replace(
        "2026. 08. 25.", "2026. 08. 20.", 1
    )

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("publication date outside requested window",)


def test_lh_detail_leaves_optional_values_empty_with_warnings(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fetcher.details[DETAIL_URL] = '<main data-lh-notice-detail="true"><dl></dl></main>'

    notice = LhPublicCollector(fetcher).collect(window).notices[0]

    assert notice.application_start is None
    assert notice.application_end is None
    assert notice.supply_count is None
    assert notice.price_summary is None
    assert notice.eligibility_summary == ()
    assert "application schedule not found" in notice.warnings
    assert "supply count not found" in notice.warnings
    assert "eligibility summary not found" in notice.warnings
