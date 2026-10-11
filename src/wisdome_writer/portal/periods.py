"""Explicit KST calendar selection; unknown dates never acquire a guessed month."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")
BASES = ("published", "collected", "schedule")


def as_local_date(value) -> date | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        try:
            return value.astimezone(SEOUL).date()
        except (ValueError, OverflowError):
            return None
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) <= 40:
        try:
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                return date.fromisoformat(value)
            return as_local_date(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class PeriodSelection:
    year: int | None = None
    month: int | None = None
    basis: str = "published"

    def __post_init__(self):
        if self.year is not None and (type(self.year) is not int or not 1900 <= self.year <= 2100):
            raise ValueError("연도는 1900~2100년 범위로 선택해 주세요.")
        if self.month is not None and (
            self.year is None or type(self.month) is not int or not 1 <= self.month <= 12
        ):
            raise ValueError("월을 조회하려면 연도와 1~12월을 함께 선택해 주세요.")
        if self.basis not in BASES:
            raise ValueError("날짜 기준을 확인해 주세요.")

    @property
    def active(self):
        return self.year is not None

    @property
    def start(self):
        return datetime(self.year, self.month or 1, 1, tzinfo=SEOUL) if self.active else None

    @property
    def end(self):
        if not self.active:
            return None
        if self.month is None or self.month == 12:
            return datetime(self.year + 1, 1, 1, tzinfo=SEOUL)
        return datetime(self.year, self.month + 1, 1, tzinfo=SEOUL)

    @property
    def label(self):
        if not self.active:
            return "전체 기간"
        return f"{self.year}년 {self.month}월" if self.month else f"{self.year}년 전체"

    def matches(self, value):
        if not self.active:
            return True
        day = as_local_date(value)
        return day is not None and self.start.date() <= day < self.end.date()

    def matches_range(self, start, end):
        if not self.active:
            return True
        start, end = as_local_date(start), as_local_date(end)
        if start is None or end is None:
            return self.matches(start or end)
        return start <= end and start < self.end.date() and end >= self.start.date()


def selection_from_query(query, *, default_basis="published", allowed_bases=BASES):
    for key in ("year", "month", "basis"):
        if hasattr(query, "getlist") and len(query.getlist(key)) > 1:
            raise ValueError("날짜 조건은 항목별로 하나씩 선택해 주세요.")
    year = query.get("year", "")
    month = query.get("month", "")
    basis = query.get("basis", default_basis) or default_basis
    if year and not re.fullmatch(r"[0-9]{4}", year):
        raise ValueError("연도를 네 자리 숫자로 선택해 주세요.")
    if month and not re.fullmatch(r"[0-9]{1,2}", month):
        raise ValueError("월을 1~12월 중에서 선택해 주세요.")
    if basis not in allowed_bases:
        raise ValueError("이 화면에서 사용할 수 없는 날짜 기준입니다.")
    return PeriodSelection(int(year) if year else None, int(month) if month else None, basis)


def period_context(request, selection, *, basis_labels=None, available_years=()):
    labels = basis_labels or {
        "published": "공고·기사 날짜",
        "collected": "수집 날짜",
        "schedule": "특별공급·1순위 일정",
    }
    today = datetime.now(SEOUL).date()
    years = set(range(today.year - 8, today.year + 2))
    years.update(year for year in available_years if type(year) is int and 1900 <= year <= 2100)
    if selection.year is not None:
        years.add(selection.year)
    preserved = [
        {"name": key, "value": request.GET[key]}
        for key in ("q", "region", "kind", "mode", "stage", "inventory")
        if key in request.GET
    ]

    def link(year=None, month=None):
        query = request.GET.copy()
        for key in ("year", "month", "basis"):
            query.pop(key, None)
        if year is not None:
            query["year"] = str(year)
        if month is not None:
            query["month"] = str(month)
        query["basis"] = selection.basis
        return request.path + "?" + query.urlencode()

    previous, following = None, None
    if selection.active:
        if selection.month is None:
            if selection.year > 1900:
                previous = link(selection.year - 1)
            if selection.year < 2100:
                following = link(selection.year + 1)
        else:
            py, pm = (
                (selection.year - 1, 12)
                if selection.month == 1
                else (selection.year, selection.month - 1)
            )
            ny, nm = (
                (selection.year + 1, 1)
                if selection.month == 12
                else (selection.year, selection.month + 1)
            )
            if py >= 1900:
                previous = link(py, pm)
            if ny <= 2100:
                following = link(ny, nm)
    return {
        "label": selection.label,
        "basis_label": labels.get(selection.basis, selection.basis),
        "basis_options": list(labels.items()),
        "years": sorted(years, reverse=True),
        "months": [(number, f"{number}월") for number in range(1, 13)],
        "preserved": preserved,
        "hidden": [
            {"name": "year", "value": str(selection.year) if selection.year else ""},
            {"name": "month", "value": str(selection.month) if selection.month else ""},
            {"name": "basis", "value": selection.basis},
        ],
        "current_url": link(today.year, today.month),
        "all_url": link(),
        "previous_url": previous,
        "next_url": following,
        "year_links": [(year, link(year)) for year in sorted(years, reverse=True)],
        "month_links": [
            (month, link(selection.year or today.year, month)) for month in range(1, 13)
        ],
    }
