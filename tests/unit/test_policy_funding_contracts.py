from datetime import UTC, date, datetime

import pytest


def test_catalog_covers_requested_regions_without_claiming_every_source_is_collected():
    from apps.policy_funding.catalog import REGIONS, load_catalog

    sources = load_catalog()
    assert len(REGIONS) == 17
    assert {region for source in sources for region in source.regions} >= set(REGIONS)
    assert {source.adapter for source in sources if source.enabled} >= {"ols", "bizinfo"}
    assert all(source.authority_url.startswith("https://") for source in sources)
    assert any(not source.enabled and source.access_note for source in sources)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_1",
        "https://www.bizinfo.go.kr.evil.test/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_1",
        "https://user:pass@www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_1",
        "https://www.bizinfo.go.kr:8443/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_1",
        "https://www.bizinfo.go.kr/sii/siia/../../admin",
    ],
)
def test_source_url_validation_rejects_unsafe_urls(url):
    from apps.policy_funding.catalog import source_by_id

    with pytest.raises(ValueError):
        source_by_id("bizinfo_finance").validate_url(url)


def test_notice_identity_keeps_required_query_id_and_hash_ignores_collection_time():
    from apps.policy_funding.contracts import FundingNotice

    data = dict(
        source_id="bizinfo_finance",
        external_id="PBLN_000000000126965",
        title="2026년 정책자금",
        issuer="중소벤처기업부",
        official_url="https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_000000000126965",
        published_on=date(2026, 10, 2),
        application_period="예산 소진시까지",
        regions=("national",),
        kinds=("agency_loan",),
        evidence_text="확인서는 대출 승인이 아닙니다.",
        fetched_at=datetime(2026, 10, 5, tzinfo=UTC),
    )
    first = FundingNotice(**data)
    second = FundingNotice(**{**data, "fetched_at": datetime(2026, 10, 6, tzinfo=UTC)})
    changed = FundingNotice(**{**data, "application_period": "접수 마감"})
    assert first.stable_id == second.stable_id == changed.stable_id
    assert first.content_hash == second.content_hash != changed.content_hash
    assert "pblancId=PBLN_000000000126965" in first.official_url
    assert FundingNotice.from_dict(first.to_dict()) == first
    with pytest.raises(ValueError):
        FundingNotice.from_dict({**first.to_dict(), "loan_approved": True})


def test_notice_rejects_naive_dates_and_invalid_regions():
    from apps.policy_funding.contracts import FundingNotice

    data = dict(
        source_id="semas_agency",
        external_id="404",
        title="공고",
        issuer="소진공",
        official_url="https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=404&bbsTypeCd=01",
        published_on=None,
        application_period=None,
        regions=("national",),
        kinds=("agency_loan",),
        evidence_text="정책자금 공고",
        fetched_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError):
        FundingNotice(**{**data, "fetched_at": datetime(2026, 10, 5)})
    with pytest.raises(ValueError):
        FundingNotice(**{**data, "regions": ("unknown_region",)})
