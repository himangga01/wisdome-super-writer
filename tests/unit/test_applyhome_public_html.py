from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from apps.local_content.contracts import CollectionWindow
from apps.local_content.http import HtmlResponse, OfficialHtmlFetcher
from apps.local_content.sources import applyhome
from apps.local_content.sources.applyhome import APT_LIST, REMAINING_LIST, ApplyHomePublicCollector
from wisdome_writer.infrastructure import http_safety

SEOUL = ZoneInfo("Asia/Seoul")
FIXTURES = Path(__file__).parents[1] / "fixtures" / "applyhome"
LIVE_REGRESSIONS = Path(__file__).parents[1] / "fixtures" / "live-regressions"
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


@pytest.mark.parametrize(
    ("category", "fixture_name", "expected_id", "expected_path"),
    [
        (
            "apt",
            "applyhome-apt-list-2026-08-28.html",
            "applyhome:apt:2026000401:2026000401",
            "/ai/aia/selectAPTLttotPblancDetail.do",
        ),
        (
            "remaining",
            "applyhome-remaining-list-2026-08-28.html",
            "applyhome:remaining:2026940401:2026940401",
            "/ai/aia/selectAPTRemndrLttotPblancDetailView.do",
        ),
    ],
)
def test_applyhome_parses_sanitized_live_table_shape(
    category: str,
    fixture_name: str,
    expected_id: str,
    expected_path: str,
) -> None:
    body = (LIVE_REGRESSIONS / fixture_name).read_text(encoding="utf-8")

    page = ApplyHomePublicCollector(FixtureFetcher({}))._parse_list(body, category)

    assert (page.current_page, page.last_page, page.total_count) == (1, 1, 1)
    assert len(page.records) == 1
    assert page.records[0].external_id == expected_id
    assert page.records[0].canonical_url.startswith(
        f"https://www.applyhome.co.kr{expected_path}?"
    )
    assert page.records[0].published_at.isoformat() == "2026-08-28T00:00:00+09:00"


def test_applyhome_paginates_two_sanitized_official_table_pages() -> None:
    first = (
        LIVE_REGRESSIONS / "applyhome-apt-list-two-page-1.html"
    ).read_text(encoding="utf-8")
    match = re.search(r"<tr data-pbno[\s\S]*?</tr>", first)
    assert match is not None
    rows = "".join(
        match.group().replace("2026100001", f"{2026100001 + offset}").replace(
            "공식 표 공고 1", f"공식 표 공고 {offset + 1}"
        )
        for offset in range(10)
    )
    first = first.replace(match.group(), rows, 1)
    second = (
        LIVE_REGRESSIONS / "applyhome-apt-list-two-page-2.html"
    ).read_text(encoding="utf-8")
    collector = ApplyHomePublicCollector(
        FixtureFetcher({APT_PAGE_1: first, APT_PAGE_2: second})
    )

    records = collector._collect_list_pages("apt", APT_LIST)

    assert len(records) == 11
    assert records[0].external_id == "applyhome:apt:2026100001:2026100001"
    assert records[-1].external_id == "applyhome:apt:2026100011:2026100011"


def test_applyhome_parses_sanitized_live_detail_shape() -> None:
    body = (LIVE_REGRESSIONS / "applyhome-apt-detail-2026-08-28.html").read_text(
        encoding="utf-8"
    )

    application_start, application_end, supply_count, warnings = (
        ApplyHomePublicCollector._parse_detail(body, listed=_live_record("apt"))
    )

    assert application_start.isoformat() == "2026-09-07"
    assert application_end.isoformat() == "2026-09-08"
    assert supply_count == 22
    assert warnings == ("price summary not found", "eligibility summary not found")


def test_applyhome_binds_attachmentless_official_detail_by_title_and_publication_date() -> None:
    body = (
        LIVE_REGRESSIONS / "applyhome-apt-detail-title-date-2026-08-28.html"
    ).read_text(encoding="utf-8")

    listed = _live_record("apt")
    result = ApplyHomePublicCollector._parse_detail(
        body,
        listed=listed,
        observed_detail_url=listed.canonical_url,
    )

    assert result[2] == 22


def test_applyhome_real_fetcher_preserves_redirected_detail_identity_out_of_band(
    monkeypatch: pytest.MonkeyPatch,
    window: CollectionWindow,
) -> None:
    monkeypatch.setattr(
        http_safety.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (
                http_safety.socket.AF_INET,
                http_safety.socket.SOCK_STREAM,
                http_safety.socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", port),
            )
        ],
    )
    list_body = (LIVE_REGRESSIONS / "applyhome-apt-list-2026-08-28.html").read_bytes()
    detail_body = (
        LIVE_REGRESSIONS / "applyhome-apt-detail-title-date-2026-08-28.html"
    ).read_bytes()
    observed_detail_requests = 0

    def official_transport(request: httpx.Request) -> httpx.Response:
        nonlocal observed_detail_requests
        path = request.url.path
        query = request.url.query.decode()
        if path.endswith("selectAPTLttotPblancListView.do"):
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=list_body,
            )
        if path.endswith("selectAPTLttotPblancDetail.do"):
            observed_detail_requests += 1
            if "transportRedirect=1" not in query:
                return httpx.Response(
                    302,
                    headers={
                        "Location": (
                            f"{path}?houseManageNo=2026000401&"
                            "pblancNo=2026000401&transportRedirect=1"
                        )
                    },
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=detail_body,
            )
        if path.endswith("selectAPTRemndrLttotPblancListView.do"):
            empty = (
                '<main data-notice-list="remaining" data-current-page="1" '
                'data-last-page="1" data-total-count="0" data-empty-results="true"></main>'
            )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=empty.encode(),
            )
        raise AssertionError(f"unexpected official path {path}")

    collector = ApplyHomePublicCollector(
        OfficialHtmlFetcher(
            allowed_hosts={"www.applyhome.co.kr"},
            path_prefixes=("/ai/aia/",),
            transport=httpx.MockTransport(official_transport),
        )
    )

    report = collector.collect(window)

    assert report.errors == ()
    assert observed_detail_requests == 2
    assert [notice.external_id for notice in report.notices] == [
        "applyhome:apt:2026000401:2026000401"
    ]
    assert report.notices[0].warnings == (
        "price summary not found",
        "eligibility summary not found",
    )


def test_applyhome_public_detail_rejects_mixed_matching_and_wrong_identity() -> None:
    body = (LIVE_REGRESSIONS / "applyhome-apt-detail-2026-08-28.html").read_text(
        encoding="utf-8"
    )
    body += (
        '<a href="https://static.applyhome.co.kr/ai/aia/getAtchmnfl.do?'
        'houseManageNo=9999999999&amp;pblancNo=9999999999&amp;atchmnflSeqNo=2">'
        "혼합 식별자</a>"
    )

    with pytest.raises(ValueError, match="identity"):
        ApplyHomePublicCollector._parse_detail(body, listed=_live_record("apt"))


def test_applyhome_parses_sanitized_remaining_detail_schedule() -> None:
    body = (
        LIVE_REGRESSIONS / "applyhome-remaining-detail-2026-08-28.html"
    ).read_text(encoding="utf-8")

    application_start, application_end, supply_count, warnings = (
        ApplyHomePublicCollector._parse_detail(body, listed=_live_record("remaining"))
    )

    assert application_start.isoformat() == "2026-09-02"
    assert application_end.isoformat() == "2026-09-08"
    assert supply_count == 4
    assert warnings == ("price summary not found", "eligibility summary not found")


@pytest.mark.parametrize(
    "fixture_name",
    [
        "applyhome-detail-error-2026-08-28.html",
        "applyhome-detail-sparse-2026-08-28.html",
        "applyhome-detail-mismatch-2026-08-28.html",
    ],
)
def test_applyhome_public_detail_rejects_unbound_or_incomplete_templates(
    fixture_name: str,
) -> None:
    body = (LIVE_REGRESSIONS / fixture_name).read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="detail"):
        ApplyHomePublicCollector._parse_detail(body, listed=_live_record("apt"))


def _live_record(category: str):  # type: ignore[no-untyped-def]
    fixture_name = (
        "applyhome-apt-list-2026-08-28.html"
        if category == "apt"
        else "applyhome-remaining-list-2026-08-28.html"
    )
    body = (LIVE_REGRESSIONS / fixture_name).read_text(encoding="utf-8")
    collector = ApplyHomePublicCollector(FixtureFetcher({}))
    return collector._parse_list(body, category).records[0]


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


def test_applyhome_detail_fetch_failure_keeps_list_notice_with_blocking_marker(
    fixture_fetcher: FixtureFetcher,
    window: CollectionWindow,
) -> None:
    del fixture_fetcher.pages[APT_DETAIL_URL]

    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)

    assert report.errors == ()
    assert [notice.external_id for notice in report.notices] == [
        "applyhome:apt:2026000001:2026000001",
        "applyhome:apt:2026000002:2026000002",
        "applyhome:remaining:2026940001:2026940001",
    ]
    assert report.notices[0].warnings == ("DETAIL_COLLECTION_FAILED",)
    assert report.notices[0].application_start is None
    assert report.notices[0].supply_count is None


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
