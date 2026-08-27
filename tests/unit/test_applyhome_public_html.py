from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from apps.local_content.contracts import CollectionWindow
from apps.local_content.http import HtmlResponse
from apps.local_content.sources.applyhome import (
    APT_LIST,
    REMAINING_LIST,
    ApplyHomePublicCollector,
)

SEOUL = ZoneInfo("Asia/Seoul")
FIXTURES = Path(__file__).parents[1] / "fixtures" / "applyhome"
APT_DETAIL_URL = (
    "https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancDetailView.do?"
    "houseManageNo=2026000001&pblancNo=2026000001&houseSecd=01"
)
REMAINING_DETAIL_URL = (
    "https://www.applyhome.co.kr/ai/aia/selectAPTRemndrLttotPblancDetailView.do?"
    "houseManageNo=2026940001&pblancNo=2026940001&houseSecd=01"
)


class FixtureFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get(self, url: str) -> HtmlResponse:
        return HtmlResponse(
            url=url,
            status_code=200,
            content_type="text/html",
            body=self.pages[url],
            fetched_at=datetime(2026, 9, 3, tzinfo=SEOUL),
        )


@pytest.fixture
def window() -> CollectionWindow:
    return CollectionWindow(
        start=datetime(2026, 8, 28, tzinfo=SEOUL),
        end=datetime(2026, 9, 3, 23, 59, tzinfo=SEOUL),
    )


@pytest.fixture
def fixture_fetcher() -> FixtureFetcher:
    apt_list = (FIXTURES / "apt-list.html").read_text(encoding="utf-8")
    remaining_list = (FIXTURES / "remaining-list.html").read_text(encoding="utf-8")
    apt_detail = (FIXTURES / "apt-detail.html").read_text(encoding="utf-8")
    return FixtureFetcher(
        {
            APT_LIST: apt_list,
            REMAINING_LIST: remaining_list,
            APT_DETAIL_URL: apt_detail,
            REMAINING_DETAIL_URL: (
                '<main data-notice-detail="remaining"><h1>테스트 잔여세대 공고</h1></main>'
            ),
        }
    )


def test_applyhome_collector_keeps_only_publication_dates_in_window(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert [row.external_id for row in report.notices] == [
        "applyhome:apt:2026000001:2026000001",
        "applyhome:remaining:2026940001:2026940001",
    ]
    assert all(window.contains_publication(row.published_at) for row in report.notices)


def test_applyhome_detail_extracts_only_explicit_schedule_and_supply(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    notice = ApplyHomePublicCollector(fixture_fetcher).collect(window).notices[0]

    assert notice.application_start.isoformat() == "2026-09-07"
    assert notice.application_end.isoformat() == "2026-09-09"
    assert notice.supply_count == 1147
    assert notice.price_summary is None


@pytest.mark.parametrize(
    ("replacement", "expected"),
    [
        ("2026. 09. 01.", "bad publication date"),
        ('<a class="notice-link"', "missing detail link"),
        ("https://example.invalid/detail", "detail URL host is not approved"),
    ],
)
def test_applyhome_collector_fails_closed_for_invalid_list_records(
    fixture_fetcher: FixtureFetcher,
    window: CollectionWindow,
    replacement: str,
    expected: str,
) -> None:
    page = fixture_fetcher.pages[APT_LIST]
    if expected == "bad publication date":
        page = page.replace(replacement, "not-a-date", 1)
    elif expected == "missing detail link":
        page = page.replace(replacement, '<span class="notice-link"', 1).replace(
            "</a>", "</span>", 1
        )
    else:
        page = page.replace(
            "/ai/aia/selectAPTLttotPblancDetailView.do?houseManageNo=2026000001&amp;pblancNo=2026000001&amp;houseSecd=01",
            replacement,
            1,
        )
    fixture_fetcher.pages[APT_LIST] = page

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == (expected,)


def test_applyhome_collector_fails_closed_for_repeated_identity(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    row = fixture_fetcher.pages[APT_LIST].split("<article", 2)[1].split("</article>", 1)[0]
    fixture_fetcher.pages[APT_LIST] = fixture_fetcher.pages[APT_LIST].replace(
        "</main>", f"<article{row}</article></main>"
    )

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("repeated notice identity",)


def test_applyhome_collector_fails_closed_for_conflicting_identity(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    page = fixture_fetcher.pages[APT_LIST]
    first_row = page.split("<article", 2)[1].split("</article>", 1)[0]
    conflicting_row = first_row.replace("houseSecd=01", "houseSecd=02", 1)
    fixture_fetcher.pages[APT_LIST] = page.replace(
        "</main>", f"<article{conflicting_row}</article></main>"
    )

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("conflicting notice identity",)


def test_applyhome_collector_fails_closed_for_empty_result_page(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fixture_fetcher.pages[APT_LIST] = '<main data-notice-list="apt"></main>'

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("empty result page",)


def test_applyhome_collector_normalizes_title_and_removes_hidden_duplicate(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fixture_fetcher.pages[APT_LIST] = fixture_fetcher.pages[APT_LIST].replace(
        "테스트 아파트 모집공고</a>",
        '  테스트 아파트 <span class="sr-only">테스트 아파트</span> 모집공고 </a>',
        1,
    )

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices[0].title == "테스트 아파트 모집공고"
