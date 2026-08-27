from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class CollectionWindow:
    start: datetime
    end: datetime

    def contains_publication(self, value: datetime) -> bool:
        observed_date = value.astimezone(SEOUL).date()
        return self.start.date() <= observed_date <= self.end.date()


@dataclass(frozen=True)
class HousingNotice:
    source_key: str
    external_id: str
    canonical_url: str
    title: str
    publisher: str
    category: str
    region: str | None
    status: str
    published_at: datetime
    application_start: date | None = None
    application_end: date | None = None
    deadline: date | None = None
    announcement_date: date | None = None
    supply_count: int | None = None
    price_summary: str | None = None
    eligibility_summary: tuple[str, ...] = ()
    restriction_summary: tuple[str, ...] = ()
    facts: tuple[tuple[str, str], ...] = ()
    source_checksum: str = ""
    parser_version: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceRunReport:
    source_key: str
    notices: tuple[HousingNotice, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class HousingCollectionResult:
    window: CollectionWindow
    notices: tuple[HousingNotice, ...]
    source_reports: tuple[SourceRunReport, ...]
    conflicts: tuple[HousingNotice, ...] = ()
    excluded_count: int = 0

    @property
    def complete(self) -> bool:
        return all(report.complete for report in self.source_reports)
