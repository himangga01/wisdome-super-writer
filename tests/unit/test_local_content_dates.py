from dataclasses import FrozenInstanceError
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from adapters.sources.housing.applyhome import ApplyHomeAdapter
from apps.local_content.contracts import (
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


def test_publication_later_on_the_last_kst_day_is_admitted():
    window = seven_day_window(datetime(2026, 8, 28, 15, 30, tzinfo=SEOUL))
    later_today = datetime(2026, 8, 28, 23, 59, tzinfo=SEOUL)

    assert published_in_window(later_today, window) is True


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


def test_applyhome_urban_officetel_endpoint_maps_without_row_fallback():
    category = ApplyHomeAdapter._category(
        "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/getUrbtyOfctlLttotPblancDetail",
        {},
    )

    assert category == "urban_officetel"
