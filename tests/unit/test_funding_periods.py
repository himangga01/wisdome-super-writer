from dataclasses import replace
from datetime import UTC, date, datetime

from tests.unit.test_policy_funding_workflow import notice
from wisdome_writer.portal.periods import PeriodSelection


def test_funding_month_filter_precedes_latest_identity_dedup(tmp_path):
    from apps.policy_funding.storage import period_notices
    from apps.policy_funding.workflow import publish_collection

    older = replace(
        notice(), published_on=date(2026, 9, 20), fetched_at=datetime(2026, 9, 20, tzinfo=UTC)
    )
    newer = replace(
        older,
        title=older.title + " 변경",
        published_on=date(2026, 10, 6),
        fetched_at=datetime(2026, 10, 8, tzinfo=UTC),
    )
    publish_collection(tmp_path, (older,), (), run_date=date(2026, 9, 20), mode="fixture")
    publish_collection(tmp_path, (newer,), (), run_date=date(2026, 10, 8), mode="fixture")
    september = period_notices(tmp_path, PeriodSelection(2026, 9), mode="fixture")
    october = period_notices(tmp_path, PeriodSelection(2026, 10), mode="fixture")
    assert [row.title for row in september["notices"]] == [older.title]
    assert [row.title for row in october["notices"]] == [newer.title]
    assert period_notices(tmp_path, PeriodSelection(2026, 10))["notices"] == ()


def test_funding_missing_posting_date_is_not_inferred_from_capture(tmp_path):
    from apps.policy_funding.storage import period_notices
    from apps.policy_funding.workflow import publish_collection

    unknown = replace(notice(), published_on=None, fetched_at=datetime(2026, 10, 8, tzinfo=UTC))
    publish_collection(tmp_path, (unknown,), (), run_date=date(2026, 10, 8), mode="fixture")
    result = period_notices(tmp_path, PeriodSelection(2026, 10), mode="fixture")
    assert result["notices"] == () and result["unknown_date_count"] == 1
    assert len(period_notices(tmp_path, PeriodSelection(), mode="fixture")["notices"]) == 1
