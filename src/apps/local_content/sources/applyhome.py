from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from selectolax.parser import HTMLParser, Node

from apps.local_content.contracts import CollectionWindow, HousingNotice, SourceRunReport
from apps.local_content.http import HtmlResponse

APT_LIST = "https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancListView.do"
REMAINING_LIST = "https://www.applyhome.co.kr/ai/aia/selectAPTRemndrLttotPblancListView.do"

_SOURCE_KEY = "applyhome"
_OFFICIAL_HOST = "www.applyhome.co.kr"
_SEOUL = ZoneInfo("Asia/Seoul")
_DETAIL_PATHS = {
    "apt": "/ai/aia/selectAPTLttotPblancDetailView.do",
    "remaining": "/ai/aia/selectAPTRemndrLttotPblancDetailView.do",
}
_DETAIL_QUERY_KEYS = ("houseManageNo", "pblancNo", "houseSecd")
_DATE_PATTERN = re.compile(
    r"(?P<year>\d{4})\s*[./-]\s*(?P<month>\d{1,2})\s*[./-]\s*(?P<day>\d{1,2})"
)
_WHITESPACE = re.compile(r"\s+")


class HtmlFetcher(Protocol):
    def get(self, url: str) -> HtmlResponse: ...


class _ParseFailure(ValueError):
    pass


@dataclass(frozen=True)
class _ListedNotice:
    category: str
    house_manage_no: str
    pblanc_no: str
    canonical_url: str
    title: str
    published_at: datetime
    region: str | None
    status: str

    @property
    def external_id(self) -> str:
        return f"applyhome:{self.category}:{self.house_manage_no}:{self.pblanc_no}"


class ApplyHomePublicCollector:
    """Collect public ApplyHome notice pages through the bounded HTML fetcher."""

    parser_version = "applyhome-public-html-v1"

    def __init__(self, fetcher: HtmlFetcher) -> None:
        self._fetcher = fetcher

    def collect(self, window: CollectionWindow) -> SourceRunReport:
        try:
            listed = tuple(
                record
                for category, url in (("apt", APT_LIST), ("remaining", REMAINING_LIST))
                for record in self._parse_list(self._fetcher.get(url).body, category)
            )
            self._require_unique_identities(listed)
            in_window = tuple(
                record for record in listed if window.contains_publication(record.published_at)
            )
            notices = tuple(self._notice_from_listed(record) for record in in_window)
        except _ParseFailure as exc:
            return SourceRunReport(source_key=_SOURCE_KEY, errors=(str(exc),))
        except Exception as exc:
            return SourceRunReport(
                source_key=_SOURCE_KEY,
                errors=(f"official ApplyHome fetch failed: {exc.__class__.__name__}",),
            )
        return SourceRunReport(source_key=_SOURCE_KEY, notices=notices)

    def _notice_from_listed(self, listed: _ListedNotice) -> HousingNotice:
        try:
            detail = self._fetcher.get(listed.canonical_url).body
        except Exception as exc:
            raise _ParseFailure(f"detail fetch failed: {exc.__class__.__name__}") from None
        application_start, application_end, supply_count, warnings = self._parse_detail(detail)
        fields = {
            "external_id": listed.external_id,
            "canonical_url": listed.canonical_url,
            "title": listed.title,
            "published_at": listed.published_at.isoformat(),
            "application_start": application_start.isoformat() if application_start else None,
            "application_end": application_end.isoformat() if application_end else None,
            "supply_count": supply_count,
        }
        return HousingNotice(
            source_key=_SOURCE_KEY,
            external_id=listed.external_id,
            canonical_url=listed.canonical_url,
            title=listed.title,
            publisher="ApplyHome",
            category=listed.category,
            region=listed.region,
            status=listed.status,
            published_at=listed.published_at,
            application_start=application_start,
            application_end=application_end,
            supply_count=supply_count,
            source_checksum=_checksum(fields),
            parser_version=self.parser_version,
            warnings=warnings,
        )

    def _parse_list(self, body: str, category: str) -> tuple[_ListedNotice, ...]:
        document = HTMLParser(body)
        root = document.css_first(f'[data-notice-list="{category}"]')
        if root is None:
            raise _ParseFailure("list parser drift")
        rows = tuple(root.css("[data-house-manage-no][data-pblanc-no]"))
        if not rows:
            raise _ParseFailure("empty result page")
        return tuple(self._parse_list_row(row, category) for row in rows)

    def _parse_list_row(self, row: Node, category: str) -> _ListedNotice:
        house_manage_no = _required_attr(row, "data-house-manage-no")
        pblanc_no = _required_attr(row, "data-pblanc-no")
        detail_link = _find_detail_link(row, category)
        canonical_url = _canonical_detail_url(
            detail_link,
            category=category,
            house_manage_no=house_manage_no,
            pblanc_no=pblanc_no,
        )
        title = _normalized_text(row.css_first(".notice-link"))
        if not title:
            title = _label_value(row, ("공고명", "모집공고명", "주택명"))
        if not title:
            raise _ParseFailure("missing notice title")
        published = _label_value(row, ("공고일", "공고일자", "모집공고일", "게시일"))
        if not published:
            raise _ParseFailure("missing publication date")
        published_at = _parse_published_at(published)
        return _ListedNotice(
            category=category,
            house_manage_no=house_manage_no,
            pblanc_no=pblanc_no,
            canonical_url=canonical_url,
            title=title,
            published_at=published_at,
            region=_label_value(row, ("공급지역", "지역")),
            status=_label_value(row, ("상태", "공고상태")) or "published",
        )

    @staticmethod
    def _require_unique_identities(records: Iterable[_ListedNotice]) -> None:
        seen: dict[str, _ListedNotice] = {}
        for record in records:
            existing = seen.get(record.external_id)
            if existing is not None:
                if existing != record:
                    raise _ParseFailure("conflicting notice identity")
                raise _ParseFailure("repeated notice identity")
            seen[record.external_id] = record

    @staticmethod
    def _parse_detail(body: str) -> tuple[date | None, date | None, int | None, tuple[str, ...]]:
        document = HTMLParser(body)
        root = document.css_first("[data-notice-detail]")
        if root is None:
            raise _ParseFailure("detail parser drift")
        schedule = _label_value(root, ("청약신청기간", "신청기간"))
        dates = tuple(
            _parse_date(match.group()) for match in _DATE_PATTERN.finditer(schedule or "")
        )
        application_start = dates[0] if dates else None
        application_end = dates[1] if len(dates) > 1 else application_start
        supply = _label_value(root, ("공급세대수", "공급규모", "공급수"))
        supply_count = _parse_supply_count(supply) if supply else None
        warnings: list[str] = []
        if application_start is None:
            warnings.append("application schedule not found")
        if supply_count is None:
            warnings.append("supply count not found")
        return application_start, application_end, supply_count, tuple(warnings)


def _required_attr(node: Node, attribute: str) -> str:
    value = _normalize(node.attributes.get(attribute))
    if not value:
        raise _ParseFailure(f"missing {attribute}")
    return value


def _find_detail_link(row: Node, category: str) -> str:
    links = tuple(row.css("a.notice-link[href]")) or tuple(row.css("a[href]"))
    for link in links:
        href = _normalize(link.attributes.get("href"))
        if href:
            return href
    raise _ParseFailure("missing detail link")


def _canonical_detail_url(
    href: str,
    *,
    category: str,
    house_manage_no: str,
    pblanc_no: str,
) -> str:
    parsed = urlsplit(urljoin(f"https://{_OFFICIAL_HOST}", href))
    try:
        port = parsed.port
    except ValueError:
        raise _ParseFailure("detail URL host is not approved") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != _OFFICIAL_HOST
        or parsed.username
        or parsed.password
    ):
        raise _ParseFailure("detail URL host is not approved")
    if port not in (None, 443):
        raise _ParseFailure("detail URL host is not approved")
    if parsed.path != _DETAIL_PATHS[category] or parsed.fragment:
        raise _ParseFailure("detail URL path is not approved")
    try:
        values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise _ParseFailure("detail URL query is not approved") from None
    if set(values) != set(_DETAIL_QUERY_KEYS) or any(
        len(values[key]) != 1 for key in _DETAIL_QUERY_KEYS
    ):
        raise _ParseFailure("detail URL query is not approved")
    if values["houseManageNo"][0] != house_manage_no or values["pblancNo"][0] != pblanc_no:
        raise _ParseFailure("detail URL identity does not match list record")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", values["houseSecd"][0]):
        raise _ParseFailure("detail URL query is not approved")
    return urlunsplit(
        (
            "https",
            _OFFICIAL_HOST,
            parsed.path,
            urlencode([(key, values[key][0]) for key in _DETAIL_QUERY_KEYS]),
            "",
        )
    )


def _label_value(node: Node, labels: tuple[str, ...]) -> str | None:
    expected = {_normalize(label) for label in labels}
    for label in node.css("dt, th, [data-label]"):
        label_text = _normalize(label.attributes.get("data-label") or label.text(separator=" "))
        if label_text not in expected:
            continue
        if label.tag in {"dt", "th"}:
            sibling = label.next
            if sibling is not None and sibling.tag in {"dd", "td"}:
                value = _normalized_text(sibling)
                if value:
                    return value
        value = _normalize(label.attributes.get("data-value"))
        if value:
            return value
    return None


def _normalized_text(node: Node | None) -> str | None:
    if node is None:
        return None
    parsed = HTMLParser(node.html)
    for hidden in parsed.css("[aria-hidden='true'], .sr-only, .visually-hidden, .blind, .hidden"):
        hidden.decompose()
    visible = parsed.css_first(node.tag)
    return _normalize(visible.text(separator=" ") if visible is not None else "")


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _WHITESPACE.sub(" ", value).strip()
    return normalized or None


def _parse_published_at(value: str) -> datetime:
    parsed = _parse_date(value)
    if parsed is None:
        raise _ParseFailure("bad publication date")
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=_SEOUL)


def _parse_date(value: str) -> date | None:
    match = _DATE_PATTERN.search(value)
    if match is None:
        return None
    try:
        return datetime(
            int(match["year"]), int(match["month"]), int(match["day"]), tzinfo=_SEOUL
        ).date()
    except ValueError:
        return None


def _parse_supply_count(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def _checksum(value: dict[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
