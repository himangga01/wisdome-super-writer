from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path


def test_provided_reference_never_treats_unverified_expansions_as_approved_money():
    from apps.policy_funding.verification import verify_provided_case

    result = verify_provided_case(())
    rows = {row["id"]: row for row in result["claims"]}
    assert rows["pohang_expansion"]["status"] == "unverified"
    assert rows["yeongcheon_expansion"]["status"] == "unverified"
    assert rows["yangsan_youth_start"]["status"] == "different_program_date"
    assert result["input_provenance"] == "user_provided"
    assert result["as_of"] == "2026-10-05"
    assert not result["loan_approval"]


def test_opening_is_only_supported_by_matching_year_program_and_official_live_notice():
    import json

    from apps.policy_funding.parsing import parse_ols_detail, parse_ols_list
    from apps.policy_funding.verification import verify_provided_case

    root = Path(__file__).resolve().parents[1] / "fixtures/policy_funding"
    links, _ = parse_ols_list(json.loads((root / "ols-list.json").read_text(encoding="utf-8")))
    link = next(row for row in links if row.external_id == "404")
    notice = parse_ols_detail(
        (root / "ols-404.html").read_text(encoding="utf-8"),
        link,
        source_id="semas_agency",
        fetched_at=datetime(2026, 10, 5, tzinfo=UTC),
    )

    def status(row):
        return next(
            item
            for item in verify_provided_case((row,))["claims"]
            if item["id"] == "national_opening"
        )["status"]

    assert status(notice) == "supported_by_collected_notice"
    assert status(replace(notice, provenance="fixture")) == "fixture_only"
    assert (
        status(
            replace(
                notice, title=notice.title.replace("2026", "2025"), published_on=date(2025, 9, 11)
            )
        )
        == "unverified"
    )
    assert (
        status(
            replace(
                notice,
                title="2026년 다른 자금",
                external_id="999",
                official_url=notice.official_url.replace("404", "999"),
            )
        )
        == "unverified"
    )
