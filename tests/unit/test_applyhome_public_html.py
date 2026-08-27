from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from apps.local_content.contracts import CollectionWindow
from apps.local_content.http import HtmlResponse
from apps.local_content.sources import applyhome
from apps.local_content.sources.applyhome import APT_LIST, REMAINING_LIST, ApplyHomePublicCollector

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
APT_DETAIL_URL_2 = (
    "https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancDetailView.do?"
    "houseManageNo=2026000002&pblancNo=2026000002&houseSecd=01"
)
APT_PAGE_1 = f"{APT_LIST}?pageIndex=1"
APT_PAGE_2 = f"{APT_LIST}?pageIndex=2"
REMAINING_PAGE_1 = f"{REMAINING_LIST}?pageIndex=1"


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
    return FixtureFetcher(
        {
            APT_PAGE_1: (FIXTURES / "apt-list.html").read_text(encoding="utf-8"),
            APT_PAGE_2: (FIXTURES / "apt-list-page-2.html").read_text(encoding="utf-8"),
            REMAINING_PAGE_1: (FIXTURES / "remaining-list.html").read_text(encoding="utf-8"),
            APT_DETAIL_URL: (FIXTURES / "apt-detail.html").read_text(encoding="utf-8"),
            APT_DETAIL_URL_2: '<main data-notice-detail="apt"></main>',
            REMAINING_DETAIL_URL: '<main data-notice-detail="remaining"></main>',
        }
    )


def test_applyhome_collector_keeps_only_publication_dates_in_window(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert [row.external_id for row in report.notices] == [
        "applyhome:apt:2026000001:2026000001",
        "applyhome:apt:2026000002:2026000002",
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
    page = fixture_fetcher.pages[APT_PAGE_1]
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
    fixture_fetcher.pages[APT_PAGE_1] = page

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == (expected,)


def test_applyhome_collector_fails_closed_for_repeated_identity(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    row = fixture_fetcher.pages[APT_PAGE_1].split("<article", 2)[1].split("</article>", 1)[0]
    fixture_fetcher.pages[APT_PAGE_1] = fixture_fetcher.pages[APT_PAGE_1].replace(
        "</main>", f"<article{row}</article></main>"
    )
    fixture_fetcher.pages[APT_PAGE_1] = fixture_fetcher.pages[APT_PAGE_1].replace(
        'data-total-count="3"', 'data-total-count="4"', 1
    )
    fixture_fetcher.pages[APT_PAGE_2] = fixture_fetcher.pages[APT_PAGE_2].replace(
        'data-total-count="3"', 'data-total-count="4"', 1
    )

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("repeated notice identity",)


def test_applyhome_collector_fails_closed_for_conflicting_identity(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    page = fixture_fetcher.pages[APT_PAGE_1]
    first_row = page.split("<article", 2)[1].split("</article>", 1)[0]
    conflicting_row = first_row.replace("houseSecd=01", "houseSecd=02", 1)
    fixture_fetcher.pages[APT_PAGE_1] = page.replace(
        "</main>", f"<article{conflicting_row}</article></main>"
    )
    fixture_fetcher.pages[APT_PAGE_1] = fixture_fetcher.pages[APT_PAGE_1].replace(
        'data-total-count="3"', 'data-total-count="4"', 1
    )
    fixture_fetcher.pages[APT_PAGE_2] = fixture_fetcher.pages[APT_PAGE_2].replace(
        'data-total-count="3"', 'data-total-count="4"', 1
    )

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("conflicting notice identity",)


def test_applyhome_collector_fails_closed_for_empty_result_page(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fixture_fetcher.pages[APT_PAGE_1] = '<main data-notice-list="apt"></main>'

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("empty result page",)


def test_applyhome_collector_normalizes_title_and_removes_hidden_duplicate(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fixture_fetcher.pages[APT_PAGE_1] = fixture_fetcher.pages[APT_PAGE_1].replace(
        'class="notice-link">',
        'class="notice-link"><span class="sr-only">duplicate</span>',
        1,
    )

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert "duplicate" not in report.notices[0].title


def test_applyhome_collector_accepts_explicit_empty_result_marker(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fixture_fetcher.pages[APT_PAGE_1] = (
        '<main data-notice-list="apt" data-empty-results="true" '
        'data-current-page="1" data-last-page="1" data-total-count="0"></main>'
    )
    fixture_fetcher.pages.pop(APT_PAGE_2)

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert [notice.category for notice in report.notices] == ["remaining"]
    assert report.errors == ()


def test_applyhome_collector_fails_closed_for_repeated_list_page(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    repeated = fixture_fetcher.pages[APT_PAGE_1].replace(
        'data-current-page="1"', 'data-current-page="2"', 1
    )
    fixture_fetcher.pages[APT_PAGE_2] = repeated

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("repeated list page",)


def test_applyhome_collector_fails_closed_for_truncated_list_pages(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fixture_fetcher.pages[APT_PAGE_1] = fixture_fetcher.pages[APT_PAGE_1].replace(
        'data-total-count="3"', 'data-total-count="4"', 1
    )
    fixture_fetcher.pages[APT_PAGE_2] = fixture_fetcher.pages[APT_PAGE_2].replace(
        'data-total-count="3"', 'data-total-count="4"', 1
    )

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("list pagination truncated",)


def test_applyhome_collector_rejects_changed_pagination_material(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fixture_fetcher.pages[APT_PAGE_2] = fixture_fetcher.pages[APT_PAGE_2].replace(
        'data-total-count="3"', 'data-total-count="4"', 1
    )

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("list pagination material changed",)


def test_applyhome_collector_enforces_page_cap(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(applyhome, "_MAX_LIST_PAGES", 1)

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("list pagination exceeded page cap",)


def test_applyhome_detail_leaves_end_empty_for_one_explicit_application_date(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fixture_fetcher.pages[APT_DETAIL_URL] = fixture_fetcher.pages[APT_DETAIL_URL].replace(
        "2026. 09. 07. ~ 2026. 09. 09.", "2026. 09. 07.", 1
    )

    notice = ApplyHomePublicCollector(fixture_fetcher).collect(window).notices[0]

    assert notice.application_start.isoformat() == "2026-09-07"
    assert notice.application_end is None
    assert "application end date not found" in notice.warnings


def test_applyhome_detail_does_not_infer_supply_from_multiple_numbers(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fixture_fetcher.pages[APT_DETAIL_URL] = fixture_fetcher.pages[APT_DETAIL_URL].replace(
        "1,147세대", "1,147세대 중 12세대", 1
    )

    notice = ApplyHomePublicCollector(fixture_fetcher).collect(window).notices[0]

    assert notice.supply_count is None
    assert "supply count not found" in notice.warnings


def test_applyhome_checksum_changes_for_region_or_status(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    original = ApplyHomePublicCollector(fixture_fetcher).collect(window).notices[0]
    changed_region = fixture_fetcher.pages[APT_PAGE_1].replace("서울특별시", "부산광역시", 1)
    fixture_fetcher.pages[APT_PAGE_1] = changed_region
    region_changed = ApplyHomePublicCollector(fixture_fetcher).collect(window).notices[0]
    fixture_fetcher.pages[APT_PAGE_1] = changed_region.replace("공고중", "마감", 1)
    status_changed = ApplyHomePublicCollector(fixture_fetcher).collect(window).notices[0]

    assert region_changed.source_checksum != original.source_checksum
    assert status_changed.source_checksum != region_changed.source_checksum


def test_applyhome_normalizes_hidden_duplicate_in_label(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fixture_fetcher.pages[APT_DETAIL_URL] = fixture_fetcher.pages[APT_DETAIL_URL].replace(
        "<dt>", '<dt><span class="sr-only">duplicate</span>', 1
    )

    notice = ApplyHomePublicCollector(fixture_fetcher).collect(window).notices[0]

    assert notice.application_start.isoformat() == "2026-09-07"


def test_applyhome_marks_unavailable_price_and_eligibility_as_warnings(
    fixture_fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    notice = ApplyHomePublicCollector(fixture_fetcher).collect(window).notices[0]

    assert "price summary not found" in notice.warnings
    assert "eligibility summary not found" in notice.warnings
