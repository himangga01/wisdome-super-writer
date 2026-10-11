import json
import socket
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from apps.policy_funding.catalog import source_by_id
from apps.policy_funding.collectors import CollectionLimits, collect_source
from apps.policy_funding.contracts import canonical_json, content_hash
from apps.policy_funding.parsing import (
    classify_kinds,
    classify_regions,
    parse_ols_detail,
    parse_ols_list,
)
from apps.policy_funding.rendering import explain_notice, render_notice
from apps.policy_funding.storage import list_runs
from apps.policy_funding.transport import OfficialFundingFetcher, RetrievedPage
from apps.policy_funding.verification import verify_provided_case
from apps.policy_funding.workflow import publish_collection
from tests.unit.test_policy_funding_workflow import notice
from wisdome_writer.infrastructure import http_safety

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/policy_funding"
STAMP = datetime(2026, 10, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("title", "issuer", "expected"),
    [
        ("[양산] 소상공인 육성자금", "양산시", ("gyeongnam",)),
        ("[경기] 광주시 특례보증", "경기도 광주시", ("gyeonggi",)),
        ("[새도시] 소상공인 자금", "알수없는시", ("unspecified",)),
        ("2026년 소상공인 자금", "중소벤처기업부", ("national",)),
    ],
)
def test_scope_respects_province_hierarchy_and_preserves_unknown(title, issuer, expected):
    assert classify_regions(title, issuer) == expected


@pytest.mark.parametrize(
    ("summary", "supported"),
    [
        ("보증료 연 1%, 신청인 부담", False),
        ("보증료 지원 제외", False),
        ("보증료 지원하지 않습니다", False),
        ("보증료 지원은 없습니다", False),
        ("보증료 지원 불가", False),
        ("보증료 지원 대상이 아닙니다", False),
        ("최초 1년분 보증료 지원", True),
    ],
)
def test_guarantee_fee_requires_affirmative_support(summary, supported):
    assert ("guarantee_fee" in classify_kinds("특례보증", summary)) is supported


def test_robots_forbidden_redirect_is_never_fetched(monkeypatch):
    monkeypatch.setattr(
        http_safety.socket,
        "getaddrinfo",
        lambda host, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ],
    )
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /ols/man/forbidden\n")
        if request.url.path.endswith("page.do"):
            return httpx.Response(302, headers={"Location": "/ols/man/forbidden"})
        return httpx.Response(200, text="forbidden", headers={"Content-Type": "text/html"})

    fetcher = OfficialFundingFetcher(
        source_by_id("semas_agency"), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ValueError):
        fetcher.get("https://ols.semas.or.kr/ols/man/SMAN051M/page.do")
    assert calls == ["/robots.txt", "/ols/man/SMAN051M/page.do"]


@pytest.mark.parametrize("redirect", ["wrong_id", "wrong_board", "missing_board", "repeated_board"])
def test_detail_final_identity_cannot_be_rebound_to_requested_notice(redirect):
    class Fetcher:
        request_count = 0

        def post(self, url, *, data):
            self.request_count += 1
            return RetrievedPage(
                url, (FIXTURES / "ols-list.json").read_text(encoding="utf-8"), STAMP
            )

        def get(self, url, *, params=None):
            self.request_count += 1
            changed_url = {
                "wrong_id": url.replace("407", "999"),
                "wrong_board": url.replace("bbsTypeCd=01", "bbsTypeCd=99"),
                "missing_board": url.replace("&bbsTypeCd=01", ""),
                "repeated_board": url + "&bbsTypeCd=01",
            }[redirect]
            return RetrievedPage(
                changed_url,
                (FIXTURES / "ols-407.html").read_text(encoding="utf-8"),
                STAMP,
            )

    result = collect_source("semas_agency", CollectionLimits(notices=1), fetcher=Fetcher())
    assert result.status == "failed" and not result.notices
    assert result.errors


def test_ended_status_is_explicit_in_explanation_and_markdown():
    ended = replace(notice(), listing_state="ended_list")
    opened = replace(notice(), listing_state="open_list")
    assert "마감" in explain_notice(ended)["listing_label"]
    assert "미확인" in explain_notice(notice())["listing_label"]
    assert render_notice(ended) != render_notice(opened)
    assert "마감" in render_notice(ended)


@pytest.mark.parametrize(
    "change",
    [
        {"title": "2026년 4분기 부산 지역 다른 자금", "regions": ("busan",)},
        {"application_period": "2026. 10. 6. 설명회 10:00, 대출접수 2026. 11. 2. 09:00"},
    ],
)
def test_case_verification_binds_program_region_and_application_event(change):
    links, _ = parse_ols_list(json.loads((FIXTURES / "ols-list.json").read_text(encoding="utf-8")))
    link = next(row for row in links if row.external_id == "404")
    original = parse_ols_detail(
        (FIXTURES / "ols-404.html").read_text(encoding="utf-8"),
        link,
        source_id="semas_agency",
        fetched_at=STAMP,
    )
    result = verify_provided_case((replace(original, **change),))
    assert (
        next(row for row in result["claims"] if row["id"] == "national_opening")["status"]
        == "unverified"
    )


def test_manifest_valid_array_metadata_is_rejected_without_hiding_good_runs(tmp_path):
    from apps.local_content.bundles import ArticleBundleWriter

    good = publish_collection(tmp_path, (notice(),), (), run_date=date(2026, 10, 5), mode="live")
    records = json.loads((good / "notices.json").read_text(encoding="utf-8"))
    observation = json.loads((good / "raw-observations.json").read_text(encoding="utf-8"))
    writer = ArticleBundleWriter(tmp_path)
    lease = writer.reserve_run_directory(date(2026, 10, 5), workflow_id="malformed-review")
    records.update(mode=[], run_name=lease.name)
    observation.update(mode=[], run_name=lease.name, records_hash=content_hash(records))
    writer.write_run(
        date(2026, 10, 5),
        "# Invalid metadata\n",
        (),
        run_directory=lease,
        run_files={
            "notices.json": canonical_json(records),
            "raw-observations.json": canonical_json(observation),
        },
    )
    assert [row["run_name"] for row in list_runs(tmp_path)] == [good.name]


def test_relief_notice_does_not_offer_a_new_loan_application_journey():
    relief = replace(
        notice(), title="홈플러스 피해 소상공인 만기연장·상환유예 안내", kinds=("direct_loan",)
    )
    result = explain_notice(relief)
    assert result["purpose"] == "repayment_relief"
    assert not {"apply", "disbursement"} & {step["id"] for step in result["journey"]}
    assert "기존 대출" in result["plain_summary"]


def test_reference_claims_carry_portable_dated_evidence():
    result = verify_provided_case(())
    claim = next(row for row in result["claims"] if row["id"] == "yangsan_general_start")
    assert claim["evidence"]
    evidence = claim["evidence"][0]
    assert evidence["issuer"] == "양산시"
    assert evidence["source_date"] == "2026-09-28"
    assert evidence["excerpt"] and evidence["locator"]
    assert (
        evidence["attachment_sha256"]
        == "5e7b9902d109f71ab5aa90bb18388c734bf0b921df0ed0ac757fa6667141958c"
    )
