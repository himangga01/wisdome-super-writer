from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")


def _require_timezone_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")


@dataclass(frozen=True)
class CollectionWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _require_timezone_aware(self.start, "start")
        _require_timezone_aware(self.end, "end")

    def contains_publication(self, value: datetime) -> bool:
        _require_timezone_aware(value, "published_at")
        observed = value.astimezone(SEOUL)
        return self.start <= observed <= self.end


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

    def __post_init__(self) -> None:
        _require_timezone_aware(self.published_at, "published_at")
        object.__setattr__(self, "eligibility_summary", tuple(self.eligibility_summary))
        object.__setattr__(self, "restriction_summary", tuple(self.restriction_summary))
        object.__setattr__(self, "facts", tuple(tuple(pair) for pair in self.facts))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True)
class SourceRunReport:
    source_key: str
    notices: tuple[HousingNotice, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "notices", tuple(self.notices))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "errors", tuple(self.errors))

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "notices", tuple(self.notices))
        object.__setattr__(self, "source_reports", tuple(self.source_reports))
        object.__setattr__(self, "conflicts", tuple(self.conflicts))

    @property
    def complete(self) -> bool:
        return all(report.complete for report in self.source_reports)
