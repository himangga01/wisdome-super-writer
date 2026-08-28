from __future__ import annotations

import re
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
LIVE_REGRESSIONS = Path(__file__).parents[1] / "fixtures" / "live-regressions"
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
            body=self.details.get(url, '<main data-lh-notice-detail="true"></main>'),
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
            "1": _page_with_fifty_records(
                (FIXTURES / "notice-list-page-1.html").read_text(encoding="utf-8")
            ),
            "2": (FIXTURES / "notice-list-page-2.html")
            .read_text(encoding="utf-8")
            .replace('data-total-count="2"', 'data-total-count="51"'),
        },
        {
            DETAIL_URL: (FIXTURES / "notice-detail.html").read_text(encoding="utf-8"),
            SECOND_DETAIL_URL: '<main data-lh-notice-detail="true"></main>',
        },
    )


def test_lh_parses_sanitized_live_table_shape() -> None:
    body = (LIVE_REGRESSIONS / "lh-list-2026-08-28.html").read_text(encoding="utf-8")

    page = LhPublicCollector(FixtureFetcher({}, {}))._parse_list(body)

    assert (page.current_page, page.last_page, page.total_count) == (1, 1, 1)
    assert len(page.records) == 1
    notice = page.records[0]
    assert notice.external_id == "lh:03:2015122300099999:06:08"
    assert notice.title == "검증용 공공임대"
    assert notice.category == "공공임대"
    assert notice.region == "전북특별자치도"
    assert notice.published_at.isoformat() == "2026-08-28T00:00:00+09:00"
    assert notice.deadline.isoformat() == "2026-09-15"


def test_lh_parses_sanitized_live_detail_without_attachment_bytes() -> None:
    body = (LIVE_REGRESSIONS / "lh-detail-2026-08-28.html").read_text(encoding="utf-8")

    (
        application_start,
        application_end,
        supply_count,
        price_summary,
        eligibility,
        facts,
        warnings,
    ) = LhPublicCollector._parse_detail(body)

    assert application_start is None
    assert application_end is None
    assert supply_count == 80
    assert price_summary is None
    assert eligibility == ()
    assert facts == ()
    assert warnings == (
        "application schedule not found",
        "price summary not found",
        "eligibility summary not found",
    )


def test_lh_parses_sanitized_short_supply_header_variant() -> None:
    body = (LIVE_REGRESSIONS / "lh-detail-2026-08-28.html").read_text(
        encoding="utf-8"
    ).replace("금회공급 세대수 (예비자 포함)", "금회공급 세대수")

    result = LhPublicCollector._parse_detail(body)

    assert result[2] == 80


def test_lh_accepts_sparse_official_detail_with_explicit_missing_field_warnings() -> None:
    body = (LIVE_REGRESSIONS / "lh-detail-sparse-2026-08-28.html").read_text(
        encoding="utf-8"
    )

    result = LhPublicCollector._parse_detail(body)

    assert result[:6] == (None, None, None, None, (), ())
    assert result[6] == (
        "application schedule not found",
        "supply count not found",
        "price summary not found",
        "eligibility summary not found",
    )


def test_lh_sums_multiple_verified_supply_tables() -> None:
    body = (LIVE_REGRESSIONS / "lh-detail-2026-08-28.html").read_text(
        encoding="utf-8"
    )
    second_table = """
    <table>
      <thead><tr><th>주택형</th><th>금회공급 세대수</th></tr></thead>
      <tbody><tr><th>000018</th><td>20</td></tr></tbody>
    </table>
    """

    result = LhPublicCollector._parse_detail(body + second_table)

    assert result[2] == 100


def _page_with_fifty_records(page: str) -> str:
    row = re.search(r"<article[\s\S]*?</article>", page)
    assert row is not None
    copies = []
    for offset in range(1, 50):
        copies.append(
            row.group().replace("0000061158", f"{61158 + offset:010d}", 1)
        )
    return page.replace('data-total-count="2"', 'data-total-count="51"').replace(
        "</main>", f"{''.join(copies)}</main>", 1
    )


def _list_rows(page: str) -> list[str]:
    return re.findall(r"<article[\s\S]*?</article>", page)


def _with_list_rows(page: str, rows: list[str]) -> str:
    without_rows = re.sub(r"<article[\s\S]*?</article>", "", page)
    return without_rows.replace("</main>", f"{''.join(rows)}</main>", 1)


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


def test_lh_collector_posts_complete_stable_form_payload(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    LhPublicCollector(fetcher).collect(window)

    assert fetcher.posts[0].data == {
        "schTy": "0",
        "startDt": "2026-08-22",
        "endDt": "2026-08-28",
        "currPage": "1",
        "listCo": "50",
        "viewType": "srch",
        "mi": "1026",
        "schTxt": "",
        "schSido": "",
        "schSigungu": "",
        "schUppAisTpCd": "",
        "schAisTpCd": "",
    }


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
    assert fetcher.gets[0] == DETAIL_URL
    assert len(fetcher.gets) == 51


def test_lh_detail_fetch_failure_keeps_list_notice_with_blocking_marker(
    fetcher: FixtureFetcher,
    window: CollectionWindow,
) -> None:
    original_get = fetcher.get

    def fail_one_detail(url: str) -> HtmlResponse:
        if url == DETAIL_URL:
            raise OSError("sensitive transport detail")
        return original_get(url)

    fetcher.get = fail_one_detail  # type: ignore[method-assign]

    report = LhPublicCollector(fetcher).collect(window)

    assert report.errors == ()
    assert len(report.notices) == 51
    assert report.notices[0].external_id == "lh:02:0000061158:05:05"
    assert report.notices[0].warnings == ("DETAIL_COLLECTION_FAILED",)
    assert report.notices[0].application_start is None
    assert report.notices[0].supply_count is None


def test_lh_collector_keeps_non_residential_rows_for_selection_layer(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices[-1].category == "토지"
    assert all(notice.category == "임대주택" for notice in report.notices[:-1])


def test_lh_collector_rejects_repeated_page_signature(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fetcher.list_pages["1"] = fetcher.list_pages["1"].replace(
        'data-total-count="51"', 'data-total-count="100"', 1
    )
    fetcher.list_pages["2"] = fetcher.list_pages["1"].replace(
        'data-current-page="1"', 'data-current-page="2"', 1
    )

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("repeated list page",)


@pytest.mark.parametrize(
    ("page", "replacement", "expected", "error"),
    [
        (
            "1",
            'data-total-count="51"',
            'data-total-count="50"',
            "invalid list pagination material",
        ),
        ("2", 'data-last-page="2"', 'data-last-page="3"', "list pagination material changed"),
    ],
)
def test_lh_collector_rejects_changed_pagination_metadata(
    fetcher: FixtureFetcher,
    window: CollectionWindow,
    page: str,
    replacement: str,
    expected: str,
    error: str,
) -> None:
    fetcher.list_pages[page] = fetcher.list_pages[page].replace(replacement, expected, 1)

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == (error,)


@pytest.mark.parametrize(
    ("page", "row_count"),
    [("1", 49), ("1", 51), ("2", 0), ("2", 2)],
)
def test_lh_collector_rejects_invalid_page_cardinality(
    fetcher: FixtureFetcher, window: CollectionWindow, page: str, row_count: int
) -> None:
    rows = _list_rows(fetcher.list_pages[page])
    selected = rows[:row_count]
    if row_count > len(rows):
        selected.append(rows[-1])
    fetcher.list_pages[page] = _with_list_rows(fetcher.list_pages[page], selected)

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("invalid list page cardinality",)


def test_lh_collector_rejects_page_index_mismatch(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fetcher.list_pages["2"] = fetcher.list_pages["2"].replace(
        'data-current-page="2"', 'data-current-page="1"', 1
    )

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("list page index does not match official material",)


def test_lh_collector_rejects_truncated_pagination(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fetcher.list_pages["2"] = _with_list_rows(fetcher.list_pages["2"], [])

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("invalid list page cardinality",)


def test_lh_collector_enforces_page_cap(
    fetcher: FixtureFetcher, window: CollectionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.local_content.sources import lh

    monkeypatch.setattr(lh, "_MAX_LIST_PAGES", 1)

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("list pagination exceeded page cap",)


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


def test_lh_collector_rejects_malformed_explicit_empty_result(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fetcher.list_pages = {
        "1": (
            '<main data-lh-notice-list="true" data-empty-results="true" '
            'data-current-page="1" data-last-page="2" data-total-count="0"></main>'
        )
    }

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("invalid explicit empty result page",)


@pytest.mark.parametrize("total_count", ["51", "0"])
def test_lh_collector_rejects_empty_marker_with_actual_rows(
    fetcher: FixtureFetcher, window: CollectionWindow, total_count: str
) -> None:
    fetcher.list_pages["1"] = fetcher.list_pages["1"].replace(
        'data-lh-notice-list="true"', 'data-lh-notice-list="true" data-empty-results="true"', 1
    ).replace('data-total-count="51"', f'data-total-count="{total_count}"', 1)

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("contradictory explicit empty result page",)


def test_lh_collector_rejects_an_old_corrected_notice(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fetcher.list_pages["1"] = fetcher.list_pages["1"].replace(
        "2026. 08. 25.", "2026. 08. 20.", 1
    )

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("publication date outside requested window",)


def test_lh_collector_rejects_repeated_identity(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    first = _list_rows(fetcher.list_pages["1"])[0]
    fetcher.list_pages["2"] = fetcher.list_pages["2"].replace(
        _list_rows(fetcher.list_pages["2"])[0], first, 1
    )

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("repeated notice identity",)


def test_lh_collector_rejects_conflicting_identity(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    first = _list_rows(fetcher.list_pages["1"])[0].replace("공고중", "마감", 1)
    fetcher.list_pages["2"] = fetcher.list_pages["2"].replace(
        _list_rows(fetcher.list_pages["2"])[0], first, 1
    )

    report = LhPublicCollector(fetcher).collect(window)

    assert report.notices == ()
    assert report.errors == ("conflicting notice identity",)


def test_lh_collector_normalizes_hidden_accessibility_text(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fetcher.list_pages["1"] = fetcher.list_pages["1"].replace(
        'class="notice-title">', 'class="notice-title"><span class="sr-only">duplicate</span>', 1
    )

    notice = LhPublicCollector(fetcher).collect(window).notices[0]

    assert "duplicate" not in notice.title


def test_lh_checksum_changes_for_region_or_status(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    original = LhPublicCollector(fetcher).collect(window).notices[0]
    changed_region = fetcher.list_pages["1"].replace("서울특별시", "경기도", 1)
    fetcher.list_pages["1"] = changed_region
    region_changed = LhPublicCollector(fetcher).collect(window).notices[0]
    fetcher.list_pages["1"] = changed_region.replace("공고중", "마감", 1)
    status_changed = LhPublicCollector(fetcher).collect(window).notices[0]

    assert region_changed.source_checksum != original.source_checksum
    assert status_changed.source_checksum != region_changed.source_checksum


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


def test_lh_detail_accepts_one_explicitly_labelled_schedule_range(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    notice = LhPublicCollector(fetcher).collect(window).notices[0]

    assert notice.application_start is not None
    assert notice.application_end is not None


def test_lh_detail_rejects_multiple_schedule_periods_as_ambiguous(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    fetcher.details[DETAIL_URL] = (FIXTURES / "notice-detail.html").read_text(
        encoding="utf-8"
    ).replace(
        "2026. 09. 01. ~ 2026. 09. 03.",
        "2026. 09. 01. ~ 2026. 09. 03. / 2026. 09. 04. ~ 2026. 09. 05.",
    )

    notice = LhPublicCollector(fetcher).collect(window).notices[0]

    assert notice.application_start is None
    assert notice.application_end is None
    assert "application schedule ambiguous" in notice.warnings


def test_lh_attachment_is_internal_analysis_only_observation_without_fetch(
    fetcher: FixtureFetcher, window: CollectionWindow
) -> None:
    notice = LhPublicCollector(fetcher).collect(window).notices[0]

    assert notice.facts == (
        ("attachment", "/lhapply/file/download.do?fileId=example|internal_analysis_only"),
    )
    assert all("download.do" not in url for url in fetcher.gets)
