import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/policy_funding"
STAMP = datetime(2026, 10, 5, tzinfo=UTC)


def test_ols_direct_application_window_keeps_both_days_and_daily_hours():
    from apps.policy_funding.parsing import NoticeLink, parse_ols_detail

    title = "2026년 10월 신용취약소상공인자금 신청안내"
    period = "‘26. 10. 12.(월) ~ 10. 13.(화), 10:00 ~ 18:00"
    # Minimal reproduction of the reviewed OLS 409 heading/window structure.
    body = (
        f'<div class="board_view"><h2 class="view_title">{title}</h2></div>'
        f'<div id="cntnDiv">□ 신청기간 : {period}'
        ' * 대표자 출생연도 구분 없이 양일 접수 가능 □ 유의사항 : 심사 필요</div>'
    )
    row = parse_ols_detail(
        body,
        NoticeLink(
            "409", title,
            "https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=409&bbsTypeCd=01",
            None, loan_route="직접대출",
        ),
        source_id="semas_direct",
        fetched_at=STAMP,
    )
    assert row.application_period == period
    assert "유의사항" not in row.application_period


def test_bizinfo_list_keeps_notice_identity_and_listing_state():
    from apps.policy_funding.parsing import parse_bizinfo_list

    page = parse_bizinfo_list((FIXTURES / "bizinfo-list-N.html").read_text(encoding="utf-8"))
    assert page[0].external_id == "PBLN_000000000126965"
    assert page[0].official_url.endswith("?pblancId=PBLN_000000000126965")
    assert page[0].published_on.isoformat() == "2026-10-02"
    assert page[1].issuer == "경기도"


def test_bizinfo_detail_keeps_issuer_origin_separate_from_application():
    from apps.policy_funding.parsing import parse_bizinfo_detail, parse_bizinfo_list

    link = parse_bizinfo_list((FIXTURES / "bizinfo-list-N.html").read_text(encoding="utf-8"))[0]
    notice = parse_bizinfo_detail(
        (FIXTURES / "bizinfo-detail.html").read_text(encoding="utf-8"),
        link,
        fetched_at=STAMP,
        listing_state="open_list",
    )
    assert notice.issuer == "중소벤처기업부"
    assert notice.origin_url.startswith("https://www.mss.go.kr/")
    assert notice.application_url.startswith("https://ols.semas.or.kr/")
    assert notice.application_period == "예산 소진시까지"
    assert notice.listing_state == "open_list"
    assert "소상공인" in notice.evidence_text
    assert notice.provenance == "official_fetch"


def test_ols_search_and_detail_keep_the_actual_opening_and_warning():
    from apps.policy_funding.parsing import parse_ols_detail, parse_ols_list

    links, pages = parse_ols_list(
        json.loads((FIXTURES / "ols-list.json").read_text(encoding="utf-8"))
    )
    assert len(links) == 4 and pages == 1
    link = next(row for row in links if row.external_id == "404")
    notice = parse_ols_detail(
        (FIXTURES / "ols-404.html").read_text(encoding="utf-8"),
        link,
        source_id="semas_agency",
        fetched_at=STAMP,
    )
    assert "2026. 10. 6." in notice.application_period
    assert "10:00" in notice.application_period
    assert "보증하지 않습니다" in notice.evidence_text
    assert notice.kinds == ("agency_loan",)


@pytest.mark.parametrize("body", ["<html>접근이 차단되었습니다</html>", "<html></html>"])
def test_inaccessible_or_changed_list_is_not_an_empty_success(body):
    from apps.policy_funding.parsing import parse_bizinfo_list

    with pytest.raises(ValueError):
        parse_bizinfo_list(body)


def test_wrong_host_detail_link_is_rejected():
    from apps.policy_funding.parsing import parse_bizinfo_list

    body = (FIXTURES / "bizinfo-list-N.html").read_text(encoding="utf-8")
    body = body.replace('href="/sii/', 'href="https://evil.example/sii/')
    with pytest.raises(ValueError):
        parse_bizinfo_list(body)


def test_agency_notice_cannot_be_mislabeled_as_a_direct_loan():
    from apps.policy_funding.parsing import parse_ols_detail, parse_ols_list

    links, _ = parse_ols_list(json.loads((FIXTURES / "ols-list.json").read_text(encoding="utf-8")))
    link = next(row for row in links if row.external_id == "404")
    with pytest.raises(ValueError):
        parse_ols_detail(
            (FIXTURES / "ols-404.html").read_text(encoding="utf-8"),
            link,
            source_id="semas_direct",
            fetched_at=STAMP,
        )


def test_collector_fetches_open_and_ended_partitions_and_reports_detail_failure():
    from apps.policy_funding.collectors import CollectionLimits, collect_source
    from apps.policy_funding.transport import RetrievedPage

    calls = []

    class Fetcher:
        request_count = 0

        def get(self, url, *, params=None):
            self.request_count += 1
            calls.append((url, params))
            if url.endswith("selectSIIA200View.do"):
                body = (FIXTURES / f"bizinfo-list-{params['schEndAt']}.html").read_text(
                    encoding="utf-8"
                )
            elif "PBLN_000000000126965" in url:
                body = (FIXTURES / "bizinfo-detail.html").read_text(encoding="utf-8")
            else:
                raise ValueError("detail unavailable")
            return RetrievedPage(url, body, STAMP)

    result = collect_source(
        "bizinfo_finance", CollectionLimits(pages=1, notices=10), fetcher=Fetcher()
    )
    assert {params["schEndAt"] for _, params in calls if params} == {"N", "Y"}
    assert len(result.notices) == 1
    assert result.errors
    assert result.coverage_limited is True
    assert result.status == "partial"


def test_small_detail_budget_is_shared_between_open_and_ended_notices():
    from apps.policy_funding.collectors import CollectionLimits, collect_source
    from apps.policy_funding.parsing import parse_bizinfo_list
    from apps.policy_funding.transport import RetrievedPage

    lists = {
        state: (FIXTURES / f"bizinfo-list-{state}.html").read_text(encoding="utf-8")
        for state in ("N", "Y")
    }
    links = {
        link.official_url: link for body in lists.values() for link in parse_bizinfo_list(body)
    }

    class Fetcher:
        request_count = 0

        def get(self, url, *, params=None):
            self.request_count += 1
            if params:
                body = lists[params["schEndAt"]]
            else:
                link = links[url]
                body = f'<h2 class="title">{link.title}</h2><div class="view_cont"><ul>'
                body += (
                    '<li><span class="s_title">소관부처·지자체</span>'
                    f'<div class="txt">{link.issuer}</div></li>'
                )
                body += (
                    '<li><span class="s_title">사업개요</span><div '
                    'class="txt">정책자금 지원 '
                    "안내</div></li></ul></div>"
                )
            return RetrievedPage(url, body, STAMP)

    result = collect_source(
        "bizinfo_finance", CollectionLimits(pages=1, notices=2), fetcher=Fetcher()
    )
    assert len(result.notices) == 2
    assert {row.listing_state for row in result.notices} == {"open_list", "ended_list"}
