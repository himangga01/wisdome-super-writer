from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from apps.local_content.http import HtmlResponse
from apps.local_content.sources.applyhome import ApplyHomePublicCollector

FIXTURES = Path(__file__).parents[1] / "fixtures" / "live-regressions"
SEOUL = ZoneInfo("Asia/Seoul")


class DetailFetcher:
    def __init__(self, body):
        self.body = body

    def get(self, url):
        return HtmlResponse(
            url=url,
            status_code=200,
            content_type="text/html",
            body=self.body,
            fetched_at=datetime(2026, 10, 8, tzinfo=SEOUL),
        )


def _notice(schedule):
    body = (FIXTURES / "applyhome-apt-detail-2026-08-28.html").read_text("utf-8")
    body = body[: body.index('<table class="tbl_st tbl_row">')] + schedule
    collector = ApplyHomePublicCollector(DetailFetcher(body))
    listing = (FIXTURES / "applyhome-apt-list-2026-08-28.html").read_text("utf-8")
    record = collector._parse_list(listing, "apt").records[0]
    return collector._notice_from_listed(record)


def test_collector_preserves_special_first_and_region_specific_source_dates():
    notice = _notice(
        '<table class="tbl_st"><tr><th>모집공고일</th><td>2026-08-28</td></tr>'
        '<tr><th rowspan="3">청약접수</th><th>구분</th>'
        "<th>해당지역</th><th>기타지역</th></tr>"
        "<tr><td>특별공급</td><td>2026-10-05</td><td>2026-10-05</td></tr>"
        '<tr><td>1순위</td><td id="rnk1CrsRceptPd">2026-10-06</td>'
        "<td>2026-10-07</td></tr></table>"
    )

    assert notice.facts == (
        ("특별공급 · 해당지역", "2026-10-05"),
        ("특별공급 · 기타지역", "2026-10-05"),
        ("1순위 · 해당지역", "2026-10-06"),
        ("1순위 · 기타지역", "2026-10-07"),
    )
    assert notice.published_at.date().isoformat() == "2026-08-28"


def test_known_first_priority_marker_is_explicit_even_when_row_label_is_generic():
    notice = _notice(
        '<table class="tbl_st"><tr><th>청약접수</th>'
        '<td id="rnk1CrsRceptPd">2026-10-06</td></tr></table>'
    )

    assert notice.facts == (("1순위", "2026-10-06"),)


@pytest.mark.parametrize(
    "label",
    ["청약접수", "청약기간", "신청기간", "모집공고일", "2순위", "특별공급 자격", "1순위 대상"],
)
def test_generic_or_second_priority_dates_do_not_become_special_or_first(label):
    notice = _notice(f'<table class="tbl_st"><tr><th>{label}</th><td>2026-10-06</td></tr></table>')

    assert notice.facts == ()


def test_missing_stage_date_remains_an_explicit_source_label_with_unknown_date():
    notice = _notice(
        '<table class="tbl_st"><tr><th>특별공급</th><td>미정</td></tr>'
        "<tr><th>1순위 (기타지역)</th><td>공고문 확인</td></tr></table>"
    )

    assert notice.facts == (("특별공급", "미정"), ("1순위 (기타지역)", "공고문 확인"))


def test_changed_stage_schedule_changes_notice_checksum():
    early = _notice('<table class="tbl_st"><tr><th>특별공급</th><td>2026-10-05</td></tr></table>')
    corrected = _notice(
        '<table class="tbl_st"><tr><th>특별공급</th><td>2026-11-05</td></tr></table>'
    )

    assert early.source_checksum != corrected.source_checksum


def test_stage_spanning_two_columns_still_keeps_its_confirmed_date():
    notice = _notice(
        '<table class="tbl_st"><tr><th colspan="2">1순위</th>'
        '<td id="rnk1CrsRceptPd">2026-10-06</td></tr></table>'
    )

    assert notice.facts == (("1순위", "2026-10-06"),)


def test_row_oriented_regions_stay_associated_with_their_dates():
    notice = _notice(
        '<table class="tbl_st"><tr><th rowspan="2">1순위</th>'
        "<td>해당지역</td><td>2026-10-06</td></tr>"
        "<tr><td>기타지역</td><td>2026-10-07</td></tr></table>"
    )

    assert notice.facts == (
        ("1순위 · 해당지역", "2026-10-06"),
        ("1순위 · 기타지역", "2026-10-07"),
    )


def test_explicit_first_marker_retains_row_region_with_a_generic_schedule_label():
    notice = _notice(
        '<table class="tbl_st"><tr><th>청약접수</th><td>해당지역</td>'
        '<td id="rnk1CrsRceptPd">2026-10-06</td></tr></table>'
    )

    assert notice.facts == (("1순위 · 해당지역", "2026-10-06"),)
