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
_LEGACY_DETAIL_PATHS = {
    "apt": "/ai/aia/selectAPTLttotPblancDetailView.do",
    "remaining": "/ai/aia/selectAPTRemndrLttotPblancDetailView.do",
}
_PUBLIC_DETAIL_PATHS = {
    "apt": "/ai/aia/selectAPTLttotPblancDetail.do",
    "remaining": "/ai/aia/selectAPTRemndrLttotPblancDetailView.do",
}
_LEGACY_DETAIL_QUERY_KEYS = ("houseManageNo", "pblancNo", "houseSecd")
_PUBLIC_DETAIL_QUERY_KEYS = ("houseManageNo", "pblancNo")
_PUBLIC_PAGE_SIZE = 10
_MAX_LIST_PAGES = 100
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


@dataclass(frozen=True)
class _ParsedListPage:
    records: tuple[_ListedNotice, ...]
    current_page: int
    last_page: int
    total_count: int | None
    explicit_empty: bool


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
                for record in self._collect_list_pages(category, url)
            )
            self._require_unique_identities(listed)
            in_window = tuple(
                record for record in listed if window.contains_publication(record.published_at)
            )
            notices_list: list[HousingNotice] = []
            for record in in_window:
                try:
                    notices_list.append(self._notice_from_listed(record))
                except _ParseFailure:
                    notices_list.append(self._notice_without_detail(record))
            notices = tuple(notices_list)
        except _ParseFailure as exc:
            return SourceRunReport(source_key=_SOURCE_KEY, errors=(str(exc),))
        except Exception as exc:
            return SourceRunReport(
                source_key=_SOURCE_KEY,
                errors=(f"official ApplyHome fetch failed: {exc.__class__.__name__}",),
            )
        return SourceRunReport(source_key=_SOURCE_KEY, notices=notices)

    def _collect_list_pages(self, category: str, list_url: str) -> tuple[_ListedNotice, ...]:
        records: list[_ListedNotice] = []
        signatures: set[tuple[tuple[str, str], ...]] = set()
        expected_last: int | None = None
        expected_total: int | None = None
        for page_index in range(1, _MAX_LIST_PAGES + 1):
            page = self._parse_list(
                self._fetcher.get(_list_page_url(list_url, page_index)).body,
                category,
            )
            if page.current_page != page_index:
                raise _ParseFailure("list page index does not match official material")
            if expected_last is None:
                expected_last = page.last_page
            elif page.last_page != expected_last:
                raise _ParseFailure("list pagination material changed")
            if page.total_count is not None:
                if expected_total is None:
                    expected_total = page.total_count
                elif page.total_count != expected_total:
                    raise _ParseFailure("list pagination material changed")
            if page.explicit_empty:
                if page_index != 1 or page.last_page != 1 or page.total_count != 0:
                    raise _ParseFailure("invalid explicit empty result page")
                return ()
            signature = tuple((record.external_id, record.canonical_url) for record in page.records)
            if signature in signatures:
                raise _ParseFailure("repeated list page")
            signatures.add(signature)
            records.extend(page.records)
            if page_index == page.last_page:
                if expected_total is None or len(records) != expected_total:
                    raise _ParseFailure("list pagination truncated")
                return tuple(records)
        raise _ParseFailure("list pagination exceeded page cap")

    def _notice_from_listed(self, listed: _ListedNotice) -> HousingNotice:
        try:
            detail = self._fetcher.get(listed.canonical_url).body
        except Exception as exc:
            raise _ParseFailure(f"detail fetch failed: {exc.__class__.__name__}") from None
        application_start, application_end, supply_count, warnings = self._parse_detail(
            detail,
            listed=listed,
        )
        fields = {
            "source_key": _SOURCE_KEY,
            "external_id": listed.external_id,
            "canonical_url": listed.canonical_url,
            "title": listed.title,
            "publisher": "ApplyHome",
            "category": listed.category,
            "region": listed.region,
            "status": listed.status,
            "published_at": listed.published_at.isoformat(),
            "application_start": application_start.isoformat() if application_start else None,
            "application_end": application_end.isoformat() if application_end else None,
            "deadline": None,
            "announcement_date": None,
            "supply_count": supply_count,
            "price_summary": None,
            "eligibility_summary": (),
            "restriction_summary": (),
            "facts": (),
            "parser_version": self.parser_version,
            "warnings": warnings,
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

    def _notice_without_detail(self, listed: _ListedNotice) -> HousingNotice:
        warnings = ("DETAIL_COLLECTION_FAILED",)
        fields = {
            "source_key": _SOURCE_KEY,
            "external_id": listed.external_id,
            "canonical_url": listed.canonical_url,
            "title": listed.title,
            "publisher": "ApplyHome",
            "category": listed.category,
            "region": listed.region,
            "status": listed.status,
            "published_at": listed.published_at.isoformat(),
            "application_start": None,
            "application_end": None,
            "deadline": None,
            "announcement_date": None,
            "supply_count": None,
            "price_summary": None,
            "eligibility_summary": (),
            "restriction_summary": (),
            "facts": (),
            "parser_version": self.parser_version,
            "warnings": warnings,
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
            source_checksum=_checksum(fields),
            parser_version=self.parser_version,
            warnings=warnings,
        )

    def _parse_list(self, body: str, category: str) -> _ParsedListPage:
        document = HTMLParser(body)
        root = document.css_first(f'[data-notice-list="{category}"]')
        if root is None:
            return self._parse_public_table(document, category)
        rows = tuple(root.css("[data-house-manage-no][data-pblanc-no]"))
        if not rows and _normalize(root.attributes.get("data-empty-results")) != "true":
            raise _ParseFailure("empty result page")
        current_page = _required_nonnegative_int(root, "data-current-page", minimum=1)
        last_page = _required_nonnegative_int(root, "data-last-page", minimum=1)
        total_count = _required_nonnegative_int(root, "data-total-count", minimum=0)
        if current_page > last_page:
            raise _ParseFailure("invalid list pagination material")
        if not rows:
            return _ParsedListPage((), current_page, last_page, total_count, True)
        if total_count == 0:
            raise _ParseFailure("invalid list pagination material")
        return _ParsedListPage(
            tuple(self._parse_list_row(row, category) for row in rows),
            current_page,
            last_page,
            total_count,
            False,
        )

    def _parse_public_table(self, document: HTMLParser, category: str) -> _ParsedListPage:
        rows = tuple(document.css("table.tbl_st tbody tr[data-pbno][data-hmno]"))
        pager = document.css_first("#paging")
        active = pager.css_first("a.active") if pager is not None else None
        if not rows or pager is None or active is None:
            raise _ParseFailure("list parser drift")
        current_page = _ascii_int(_normalized_text(active), minimum=1)
        advertised_pages = [current_page]
        for link in pager.css("a[href]"):
            try:
                values = parse_qs(
                    urlsplit(link.attributes.get("href") or "").query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
            except ValueError:
                continue
            page_values = values.get("pageIndex", ())
            if len(page_values) == 1:
                advertised_pages.append(_ascii_int(page_values[0], minimum=1))
        last_page = max(advertised_pages)
        if current_page > last_page or len(rows) > _PUBLIC_PAGE_SIZE:
            raise _ParseFailure("invalid list pagination material")
        if current_page < last_page and len(rows) != _PUBLIC_PAGE_SIZE:
            raise _ParseFailure("invalid list page cardinality")
        total_count = (
            (last_page - 1) * _PUBLIC_PAGE_SIZE + len(rows)
            if current_page == last_page
            else None
        )
        return _ParsedListPage(
            records=tuple(self._parse_public_table_row(row, category) for row in rows),
            current_page=current_page,
            last_page=last_page,
            total_count=total_count,
            explicit_empty=False,
        )

    def _parse_public_table_row(self, row: Node, category: str) -> _ListedNotice:
        cells = tuple(row.css("td"))
        expected_cells = 11 if category == "apt" else 8
        if len(cells) != expected_cells:
            raise _ParseFailure("invalid list row cardinality")
        house_manage_no = _required_attr(row, "data-hmno")
        pblanc_no = _required_attr(row, "data-pbno")
        title_index = 3 if category == "apt" else 2
        published_index = 6 if category == "apt" else 4
        title = _normalized_text(cells[title_index].css_first("a"))
        if not title:
            raise _ParseFailure("missing notice title")
        published = _table_cell_text(cells[published_index])
        if not published:
            raise _ParseFailure("missing publication date")
        detail_path = _PUBLIC_DETAIL_PATHS[category]
        detail_query = urlencode(
            (("houseManageNo", house_manage_no), ("pblancNo", pblanc_no))
        )
        canonical_url = _canonical_detail_url(
            f"{detail_path}?{detail_query}",
            category=category,
            house_manage_no=house_manage_no,
            pblanc_no=pblanc_no,
        )
        return _ListedNotice(
            category=category,
            house_manage_no=house_manage_no,
            pblanc_no=pblanc_no,
            canonical_url=canonical_url,
            title=title,
            published_at=_parse_published_at(published),
            region=_table_cell_text(cells[0]),
            status="published",
        )

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
    def _parse_detail(
        body: str,
        *,
        listed: _ListedNotice | None = None,
    ) -> tuple[date | None, date | None, int | None, tuple[str, ...]]:
        document = HTMLParser(body)
        root = document.css_first("[data-notice-detail]")
        if root is None:
            if listed is None:
                raise _ParseFailure("detail identity is required")
            _require_public_detail_identity(document, listed)
            return _parse_public_detail(document)
        schedule = _label_value(root, ("청약신청기간", "신청기간"))
        dates = tuple(
            _parse_date(match.group()) for match in _DATE_PATTERN.finditer(schedule or "")
        )
        application_start = dates[0] if dates else None
        application_end = dates[1] if len(dates) > 1 else None
        supply = _label_value(root, ("공급세대수", "공급규모", "공급수"))
        supply_count = _parse_supply_count(supply) if supply else None
        warnings: list[str] = []
        if application_start is None:
            warnings.append("application schedule not found")
        elif application_end is None:
            warnings.append("application end date not found")
        if supply_count is None:
            warnings.append("supply count not found")
        warnings.extend(("price summary not found", "eligibility summary not found"))
        return application_start, application_end, supply_count, tuple(warnings)


def _parse_public_detail(
    document: HTMLParser,
) -> tuple[date | None, date | None, int | None, tuple[str, ...]]:
    if not document.css("table.tbl_st"):
        raise _ParseFailure("detail parser drift")
    application_start = _parse_date(
        _table_cell_text(document.css_first("#rnk1CrsRceptPd")) or ""
    )
    application_end = _parse_date(
        _table_cell_text(document.css_first("#rnk2CrsRceptPd")) or ""
    )
    if application_start is None:
        schedule = _public_table_label_value(document, ("청약접수", "청약기간"))
        dates = tuple(
            parsed
            for match in _DATE_PATTERN.finditer(schedule or "")
            if (parsed := _parse_date(match.group())) is not None
        )
        application_start = dates[0] if dates else None
        application_end = dates[1] if len(dates) > 1 else None
    supply = _public_table_label_value(document, ("공급규모", "공급세대수"))
    supply_count = _parse_supply_count(supply) if supply else None
    warnings: list[str] = []
    if application_start is None:
        warnings.append("application schedule not found")
    elif application_end is None:
        warnings.append("application end date not found")
    if supply_count is None:
        warnings.append("supply count not found")
    warnings.extend(("price summary not found", "eligibility summary not found"))
    return application_start, application_end, supply_count, tuple(warnings)


def _require_public_detail_identity(
    document: HTMLParser,
    listed: _ListedNotice,
) -> None:
    visible_text = _normalize(document.text(separator=" ")) or ""
    if (_normalize(listed.title) or "") not in visible_text:
        raise _ParseFailure("detail title does not match list record")
    matched_identity = False
    observed_identity = False
    for link in document.css("a[href]"):
        href = _normalize(link.attributes.get("href"))
        if not href:
            continue
        try:
            parsed = urlsplit(urljoin("https://www.applyhome.co.kr", href))
            values = parse_qs(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError:
            continue
        if (
            parsed.path == "/ai/aia/getAtchmnfl.do"
            and ("houseManageNo" in values or "pblancNo" in values)
        ):
            observed_identity = True
        if (
            parsed.scheme == "https"
            and parsed.hostname in {_OFFICIAL_HOST, "static.applyhome.co.kr"}
            and parsed.path == "/ai/aia/getAtchmnfl.do"
            and values.get("houseManageNo") == [listed.house_manage_no]
            and values.get("pblancNo") == [listed.pblanc_no]
        ):
            matched_identity = True
            break
    if matched_identity:
        return
    if observed_identity:
        raise _ParseFailure("detail identity does not match list record")
    published = _parse_date(
        _public_table_label_value(document, ("모집공고일", "공고일")) or ""
    )
    if published != listed.published_at.astimezone(_SEOUL).date():
        raise _ParseFailure("detail publication identity does not match list record")


def _public_table_label_value(document: HTMLParser, labels: tuple[str, ...]) -> str | None:
    expected = {_normalize(label) for label in labels}
    for row in document.css("table tr"):
        cells = tuple(row.css("th, td"))
        if len(cells) < 2 or _table_cell_text(cells[0]) not in expected:
            continue
        return _table_cell_text(cells[1])
    return None


def _table_cell_text(node: Node | None) -> str | None:
    return _normalize(node.text(separator=" ") if node is not None else "")


def _required_attr(node: Node, attribute: str) -> str:
    value = _normalize(node.attributes.get(attribute))
    if not value:
        raise _ParseFailure(f"missing {attribute}")
    return value


def _required_nonnegative_int(node: Node, attribute: str, *, minimum: int) -> int:
    raw = _required_attr(node, attribute)
    if not raw.isascii() or not raw.isdigit():
        raise _ParseFailure("invalid list pagination material")
    value = int(raw)
    if value < minimum:
        raise _ParseFailure("invalid list pagination material")
    return value


def _ascii_int(value: str | None, *, minimum: int) -> int:
    if value is None or not value.isascii() or not value.isdigit():
        raise _ParseFailure("invalid list pagination material")
    parsed = int(value)
    if parsed < minimum:
        raise _ParseFailure("invalid list pagination material")
    return parsed


def _list_page_url(list_url: str, page_index: int) -> str:
    return f"{list_url}?{urlencode({'pageIndex': page_index})}"


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
    allowed_paths = {
        _LEGACY_DETAIL_PATHS[category],
        _PUBLIC_DETAIL_PATHS[category],
    }
    if parsed.path not in allowed_paths or parsed.fragment:
        raise _ParseFailure("detail URL path is not approved")
    try:
        values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise _ParseFailure("detail URL query is not approved") from None
    observed_keys = set(values)
    if (
        parsed.path == _PUBLIC_DETAIL_PATHS[category]
        and observed_keys == set(_PUBLIC_DETAIL_QUERY_KEYS)
    ):
        query_keys = _PUBLIC_DETAIL_QUERY_KEYS
    elif (
        parsed.path == _LEGACY_DETAIL_PATHS[category]
        and observed_keys == set(_LEGACY_DETAIL_QUERY_KEYS)
    ):
        query_keys = _LEGACY_DETAIL_QUERY_KEYS
    else:
        raise _ParseFailure("detail URL query is not approved")
    if any(
        len(values[key]) != 1 for key in query_keys
    ):
        raise _ParseFailure("detail URL query is not approved")
    if values["houseManageNo"][0] != house_manage_no or values["pblancNo"][0] != pblanc_no:
        raise _ParseFailure("detail URL identity does not match list record")
    if "houseSecd" in values and not re.fullmatch(
        r"[A-Za-z0-9_-]+", values["houseSecd"][0]
    ):
        raise _ParseFailure("detail URL query is not approved")
    return urlunsplit(
        (
            "https",
            _OFFICIAL_HOST,
            parsed.path,
            urlencode([(key, values[key][0]) for key in query_keys]),
            "",
        )
    )


def _label_value(node: Node, labels: tuple[str, ...]) -> str | None:
    expected = {_normalize(label) for label in labels}
    for label in node.css("dt, th, [data-label]"):
        label_text = _normalize(label.attributes.get("data-label")) or _normalized_text(label)
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
    match = re.fullmatch(r"\s*(\d{1,3}(?:,\d{3})*|\d+)\s*(?:세대|호)?\s*", value)
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))


def _checksum(value: dict[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
