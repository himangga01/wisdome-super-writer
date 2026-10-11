from datetime import UTC, date, datetime

import pytest
from django.http import QueryDict


def test_kst_month_boundaries_are_not_utc_month_boundaries():
    from wisdome_writer.portal.periods import PeriodSelection

    period = PeriodSelection(2026, 10, "published")
    assert period.matches(datetime(2026, 9, 30, 15, tzinfo=UTC))
    assert not period.matches(datetime(2026, 9, 30, 14, 59, tzinfo=UTC))
    assert not period.matches(datetime(2026, 10, 31, 15, tzinfo=UTC))
    assert period.start.isoformat() == "2026-10-01T00:00:00+09:00"
    assert period.end.isoformat() == "2026-11-01T00:00:00+09:00"


def test_all_year_only_unknown_and_application_range_semantics():
    from wisdome_writer.portal.periods import PeriodSelection

    assert PeriodSelection().matches(None)
    assert PeriodSelection(2026).matches(date(2026, 1, 1))
    assert not PeriodSelection(2026).matches(date(2025, 12, 31))
    month = PeriodSelection(2026, 10, "schedule")
    assert not month.matches(None)
    assert not month.matches(datetime(2026, 10, 1))
    assert month.matches_range("2026-09-30", "2026-10-02")
    assert month.matches_range(None, "2026-10-05")
    assert not month.matches_range("2026-09-30", None)
    assert not month.matches_range("2026-10-10", "2026-10-01")


@pytest.mark.parametrize(
    "query",
    [
        "year=2026&month=0",
        "year=2026&month=13",
        "month=10",
        "year=26&month=10",
        "year=abc",
        "year=2026&year=2025",
        "month=10&month=11&year=2026",
        "basis=unknown",
    ],
)
def test_invalid_or_repeated_date_parameters_fail(query):
    from wisdome_writer.portal.periods import selection_from_query

    with pytest.raises(ValueError):
        selection_from_query(QueryDict(query))


def test_empty_and_valid_inputs_have_explicit_default_basis():
    from wisdome_writer.portal.periods import selection_from_query

    period = selection_from_query(QueryDict(""), default_basis="schedule")
    assert not period.active and period.basis == "schedule"
    period = selection_from_query(QueryDict("year=2026&month=10"), default_basis="published")
    assert period.label == "2026년 10월"


def test_period_links_keep_other_filters_and_search_hidden_fields():
    from django.test import RequestFactory

    from wisdome_writer.portal.periods import PeriodSelection, period_context

    request = RequestFactory().get("/housing/notices/?year=2026&month=10&q=서울&stage=first")
    context = period_context(request, PeriodSelection(2026, 10, "schedule"))
    assert "year=2026" in context["previous_url"] and "month=9" in context["previous_url"]
    assert "stage=first" in context["previous_url"]
    assert {"name": "q", "value": "서울"} in context["preserved"]
    assert {"name": "year", "value": "2026"} in context["hidden"]
