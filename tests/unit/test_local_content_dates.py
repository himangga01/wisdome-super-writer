from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from adapters.sources.housing.applyhome import ApplyHomeAdapter
from apps.local_content.contracts import (
    CollectionWindow,
    HousingCollectionResult,
    HousingNotice,
    SourceRunReport,
)
from apps.local_content.dates import published_in_window, seven_day_window

SEOUL = ZoneInfo("Asia/Seoul")


def test_seven_day_window_uses_inclusive_kst_calendar_dates():
    window = seven_day_window(datetime(2026, 8, 28, 15, 30, tzinfo=SEOUL))

    assert window.start.isoformat() == "2026-08-22T00:00:00+09:00"
    assert window.end.isoformat() == "2026-08-28T15:30:00+09:00"


def test_kst_midnight_publication_on_the_last_day_is_admitted():
    window = seven_day_window(datetime(2026, 8, 28, 15, 30, tzinfo=SEOUL))
    date_only_publication = datetime(2026, 8, 28, 0, 0, tzinfo=SEOUL)

    assert published_in_window(date_only_publication, window) is True


def test_publication_later_than_execution_time_is_rejected():
    window = seven_day_window(datetime(2026, 8, 28, 15, 30, tzinfo=SEOUL))
    later_today = datetime(2026, 8, 28, 15, 31, tzinfo=SEOUL)

    assert published_in_window(later_today, window) is False


def test_seven_day_window_converts_aware_utc_execution_time_to_kst():
    window = seven_day_window(datetime(2026, 8, 28, 15, 30, tzinfo=UTC))

    assert window.start.isoformat() == "2026-08-23T00:00:00+09:00"
    assert window.end.isoformat() == "2026-08-29T00:30:00+09:00"


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: seven_day_window(datetime(2026, 8, 28, 15, 30)), "now must be timezone-aware"),
        (
            lambda: CollectionWindow(
                start=datetime(2026, 8, 22),
                end=datetime(2026, 8, 28, 15, 30, tzinfo=SEOUL),
            ),
            "start must be timezone-aware",
        ),
        (
            lambda: CollectionWindow(
                start=datetime(2026, 8, 22, tzinfo=SEOUL),
                end=datetime(2026, 8, 28, 15, 30),
            ),
            "end must be timezone-aware",
        ),
        (
            lambda: published_in_window(
                datetime(2026, 8, 28, 9, 0),
                seven_day_window(datetime(2026, 8, 28, 15, 30, tzinfo=SEOUL)),
            ),
            "published_at must be timezone-aware",
        ),
        (
            lambda: HousingNotice(
                source_key="applyhome",
                external_id="applyhome:apt:2026000001:2026000001",
                canonical_url="https://www.applyhome.co.kr/notice",
                title="Sample housing notice",
                publisher="ApplyHome",
                category="apt",
                region=None,
                status="published",
                published_at=datetime(2026, 8, 28, 9, 0),
            ),
            "published_at must be timezone-aware",
        ),
    ],
)
def test_local_content_dates_reject_naive_datetimes(factory, match):
    with pytest.raises(ValueError, match=match):
        factory()


def test_modification_date_cannot_admit_old_publication():
    window = seven_day_window(datetime(2026, 8, 28, 15, 30, tzinfo=SEOUL))
    old_publication = datetime(2026, 8, 21, 23, 59, tzinfo=SEOUL)

    assert published_in_window(old_publication, window) is False


def test_local_contracts_are_immutable_and_preserve_collection_state():
    window = seven_day_window(datetime(2026, 8, 28, 15, 30, tzinfo=SEOUL))
    notice = HousingNotice(
        source_key="applyhome",
        external_id="applyhome:apt:2026000001:2026000001",
        canonical_url="https://www.applyhome.co.kr/notice",
        title="Sample housing notice",
        publisher="ApplyHome",
        category="apt",
        region=None,
        status="published",
        published_at=datetime(2026, 8, 28, 9, 0, tzinfo=SEOUL),
    )
    source_report = SourceRunReport(source_key="applyhome", notices=(notice,))
    result = HousingCollectionResult(
        window=window,
        notices=(notice,),
        source_reports=(source_report,),
    )

    assert result.complete is True
    assert result.notices[0].external_id == "applyhome:apt:2026000001:2026000001"
    with pytest.raises(FrozenInstanceError):
        notice.title = "Changed"


def test_contracts_copy_mutable_collection_inputs_into_tuples():
    window = seven_day_window(datetime(2026, 8, 28, 15, 30, tzinfo=SEOUL))
    eligibility = ["first-time buyer"]
    restrictions = ["residency requirement"]
    fact_pair = ["supply", "1147"]
    facts = [fact_pair]
    notice_warnings = ["missing price"]
    notice = HousingNotice(
        source_key="applyhome",
        external_id="applyhome:apt:2026000001:2026000001",
        canonical_url="https://www.applyhome.co.kr/notice",
        title="Sample housing notice",
        publisher="ApplyHome",
        category="apt",
        region=None,
        status="published",
        published_at=datetime(2026, 8, 28, 9, 0, tzinfo=SEOUL),
        eligibility_summary=eligibility,
        restriction_summary=restrictions,
        facts=facts,
        warnings=notice_warnings,
    )
    report_notices = [notice]
    report_warnings = ["list warning"]
    report_errors = ["list error"]
    source_report = SourceRunReport(
        source_key="applyhome",
        notices=report_notices,
        warnings=report_warnings,
        errors=report_errors,
    )
    result_notices = [notice]
    result_reports = [source_report]
    conflicts = [notice]
    result = HousingCollectionResult(
        window=window,
        notices=result_notices,
        source_reports=result_reports,
        conflicts=conflicts,
    )

    eligibility.append("later eligibility")
    restrictions.append("later restriction")
    fact_pair[0] = "changed"
    facts.append(["new", "fact"])
    notice_warnings.append("later notice warning")
    report_notices.clear()
    report_warnings.append("later report warning")
    report_errors.clear()
    result_notices.clear()
    result_reports.clear()
    conflicts.clear()

    assert notice.eligibility_summary == ("first-time buyer",)
    assert notice.restriction_summary == ("residency requirement",)
    assert notice.facts == (("supply", "1147"),)
    assert notice.warnings == ("missing price",)
    assert source_report.notices == (notice,)
    assert source_report.warnings == ("list warning",)
    assert source_report.errors == ("list error",)
    assert result.notices == (notice,)
    assert result.source_reports == (source_report,)
    assert result.conflicts == (notice,)


def test_applyhome_urban_officetel_endpoint_maps_without_row_fallback():
    category = ApplyHomeAdapter._category(
        "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/getUrbtyOfctlLttotPblancDetail",
        {},
    )

    assert category == "urban_officetel"
