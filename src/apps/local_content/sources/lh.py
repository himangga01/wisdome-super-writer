from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from selectolax.parser import HTMLParser, Node

from apps.local_content.contracts import CollectionWindow, HousingNotice, SourceRunReport
from apps.local_content.http import HtmlResponse

LH_LIST = "https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancList.do"
_DETAIL_PATH = "https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancInfo.do"
_SOURCE_KEY = "lh"
_SEOUL = ZoneInfo("Asia/Seoul")
_MAX_LIST_PAGES = 100
_DATE_PATTERN = re.compile(
    r"(?P<year>\d{4})\s*[./-]\s*(?P<month>\d{1,2})\s*[./-]\s*(?P<day>\d{1,2})"
)
_WHITESPACE = re.compile(r"\s+")
_SCHEDULE_SEPARATOR = re.compile(r"[.\s]*(?:~|∼|–|—|to)[.\s]*", re.IGNORECASE)


class HtmlFetcher(Protocol):
    def get(self, url: str) -> HtmlResponse: ...

    def post(self, url: str, *, data: Mapping[str, str]) -> HtmlResponse: ...


class _ParseFailure(ValueError):
    pass


@dataclass(frozen=True)
class _ListedNotice:
    ccr: str
    pan_id: str
    upp: str
    ais: str
    category: str
    title: str
    region: str | None
    published_at: datetime
    deadline: date | None
    status: str

    @property
    def external_id(self) -> str:
        return f"lh:{self.ccr}:{self.pan_id}:{self.upp}:{self.ais}"

    @property
    def canonical_url(self) -> str:
        query = {
            "aisTpCd": self.ais,
            "ccrCnntSysDsCd": self.ccr,
            "mi": "1026",
            "panId": self.pan_id,
            "uppAisTpCd": self.upp,
        }
        return f"{_DETAIL_PATH}?{urlencode(sorted(query.items()))}"


@dataclass(frozen=True)
class _ParsedListPage:
    records: tuple[_ListedNotice, ...]
    current_page: int
    last_page: int
    total_count: int
    explicit_empty: bool


class LhPublicCollector:
    """Collect LH public notices using the official form-posted HTML list."""

    parser_version = "lh-public-html-v1"

    def __init__(self, fetcher: HtmlFetcher) -> None:
        self._fetcher = fetcher

    def collect(self, window: CollectionWindow) -> SourceRunReport:
        try:
            listed = self._collect_list_pages(window)
            self._require_unique_identities(listed)
            notices_list: list[HousingNotice] = []
            for record in listed:
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
                errors=(f"official LH fetch failed: {exc.__class__.__name__}",),
            )
        return SourceRunReport(source_key=_SOURCE_KEY, notices=notices)

    def _collect_list_pages(self, window: CollectionWindow) -> tuple[_ListedNotice, ...]:
        records: list[_ListedNotice] = []
        signatures: set[tuple[tuple[str, str], ...]] = set()
        expected_last: int | None = None
        expected_total: int | None = None
        for page_index in range(1, _MAX_LIST_PAGES + 1):
            response = self._fetcher.post(LH_LIST, data=_list_form(window, page_index))
            page = self._parse_list(response.body)
            if page.current_page != page_index:
                raise _ParseFailure("list page index does not match official material")
            if expected_last is None:
                expected_last = page.last_page
                expected_total = page.total_count
            elif page.last_page != expected_last or page.total_count != expected_total:
                raise _ParseFailure("list pagination material changed")
            if page.explicit_empty:
                if page_index != 1 or page.last_page != 1 or page.total_count != 0:
                    raise _ParseFailure("invalid explicit empty result page")
                return ()
            self._validate_page_cardinality(page)
            signature = tuple((record.external_id, record.canonical_url) for record in page.records)
            if signature in signatures:
                raise _ParseFailure("repeated list page")
            signatures.add(signature)
            for record in page.records:
                if not window.contains_publication(record.published_at):
                    raise _ParseFailure("publication date outside requested window")
            records.extend(page.records)
            if page_index == page.last_page:
                if len(records) != page.total_count:
                    raise _ParseFailure("list pagination truncated")
                return tuple(records)
        raise _ParseFailure("list pagination exceeded page cap")

    def _notice_from_listed(self, listed: _ListedNotice) -> HousingNotice:
        try:
            detail = self._fetcher.get(listed.canonical_url).body
        except Exception as exc:
            raise _ParseFailure(f"detail fetch failed: {exc.__class__.__name__}") from None
        (
            application_start,
            application_end,
            supply_count,
            price_summary,
            eligibility_summary,
            facts,
            warnings,
        ) = self._parse_detail(detail, listed=listed)
        fields = {
            "source_key": _SOURCE_KEY,
            "external_id": listed.external_id,
            "canonical_url": listed.canonical_url,
            "title": listed.title,
            "publisher": "LH",
            "category": listed.category,
            "region": listed.region,
            "status": listed.status,
            "published_at": listed.published_at.isoformat(),
            "application_start": application_start.isoformat() if application_start else None,
            "application_end": application_end.isoformat() if application_end else None,
            "deadline": listed.deadline.isoformat() if listed.deadline else None,
            "supply_count": supply_count,
            "price_summary": price_summary,
            "eligibility_summary": eligibility_summary,
            "facts": facts,
            "parser_version": self.parser_version,
            "warnings": warnings,
        }
        return HousingNotice(
            source_key=_SOURCE_KEY,
            external_id=listed.external_id,
            canonical_url=listed.canonical_url,
            title=listed.title,
            publisher="LH",
            category=listed.category,
            region=listed.region,
            status=listed.status,
            published_at=listed.published_at,
            application_start=application_start,
            application_end=application_end,
            deadline=listed.deadline,
            supply_count=supply_count,
            price_summary=price_summary,
            eligibility_summary=eligibility_summary,
            facts=facts,
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
            "publisher": "LH",
            "category": listed.category,
            "region": listed.region,
            "status": listed.status,
            "published_at": listed.published_at.isoformat(),
            "application_start": None,
            "application_end": None,
            "deadline": listed.deadline.isoformat() if listed.deadline else None,
            "supply_count": None,
            "price_summary": None,
            "eligibility_summary": (),
            "facts": (),
            "parser_version": self.parser_version,
            "warnings": warnings,
        }
        return HousingNotice(
            source_key=_SOURCE_KEY,
            external_id=listed.external_id,
            canonical_url=listed.canonical_url,
            title=listed.title,
            publisher="LH",
            category=listed.category,
            region=listed.region,
            status=listed.status,
            published_at=listed.published_at,
            deadline=listed.deadline,
            source_checksum=_checksum(fields),
            parser_version=self.parser_version,
            warnings=warnings,
        )

    def _parse_list(self, body: str) -> _ParsedListPage:
        document = HTMLParser(body)
        root = document.css_first("[data-lh-notice-list]")
        if root is None:
            return self._parse_public_table(document)
        rows = tuple(root.css("[data-id1][data-id2][data-id3][data-id4]"))
        explicit_empty = _normalize(root.attributes.get("data-empty-results")) == "true"
        if explicit_empty and rows:
            raise _ParseFailure("contradictory explicit empty result page")
        current_page = _required_int(root, "data-current-page", minimum=1)
        last_page = _required_int(root, "data-last-page", minimum=1)
        total_count = _required_int(root, "data-total-count", minimum=0)
        if current_page > last_page:
            raise _ParseFailure("invalid list pagination material")
        if not rows:
            return _ParsedListPage(
                (),
                current_page,
                last_page,
                total_count,
                explicit_empty,
            )
        if total_count == 0:
            raise _ParseFailure("invalid list pagination material")
        return _ParsedListPage(
            records=tuple(self._parse_list_row(row) for row in rows),
            current_page=current_page,
            last_page=last_page,
            total_count=total_count,
            explicit_empty=False,
        )

    def _parse_public_table(self, document: HTMLParser) -> _ParsedListPage:
        paging_form = document.css_first('form[name="pagingForm"]')
        identity_selector = (
            "a.wrtancInfoBtn[data-id1][data-id2][data-id3][data-id4]"
        )
        tables = tuple(
            table for table in document.css("table") if table.css_first(identity_selector)
        )
        if paging_form is None or len(tables) != 1:
            raise _ParseFailure("list parser drift")
        table = tables[0]
        current_node = paging_form.css_first('input[name="currPage"]')
        page_size_node = paging_form.css_first('input[name="listCo"]')
        current_page = _required_input_int(current_node, minimum=1)
        page_size = _required_input_int(page_size_node, minimum=1)
        if page_size != 50:
            raise _ParseFailure("invalid list pagination material")
        rows = tuple(
            row
            for row in table.css("tbody tr")
            if row.css_first("a.wrtancInfoBtn[data-id1][data-id2][data-id3][data-id4]")
            is not None
        )
        if not rows:
            raise _ParseFailure("empty result page")
        first_cells = tuple(rows[0].css("td"))
        if len(first_cells) != 9:
            raise _ParseFailure("invalid list row cardinality")
        first_ordinal = _text_int(_table_cell_text(first_cells[0]), minimum=1)
        total_count = (current_page - 1) * page_size + first_ordinal
        last_page = (total_count + page_size - 1) // page_size
        if current_page > last_page:
            raise _ParseFailure("invalid list pagination material")
        records: list[_ListedNotice] = []
        for offset, row in enumerate(rows):
            cells = tuple(row.css("td"))
            if len(cells) != 9:
                raise _ParseFailure("invalid list row cardinality")
            expected_ordinal = first_ordinal - offset
            if _text_int(_table_cell_text(cells[0]), minimum=1) != expected_ordinal:
                raise _ParseFailure("invalid list pagination material")
            records.append(self._parse_public_table_row(row, cells))
        return _ParsedListPage(
            records=tuple(records),
            current_page=current_page,
            last_page=last_page,
            total_count=total_count,
            explicit_empty=False,
        )

    def _parse_public_table_row(
        self,
        row: Node,
        cells: tuple[Node, ...],
    ) -> _ListedNotice:
        del row
        link = cells[2].css_first(
            "a.wrtancInfoBtn[data-id1][data-id2][data-id3][data-id4]"
        )
        if link is None:
            raise _ParseFailure("missing notice identity")
        title = _public_link_title(link)
        if not title:
            raise _ParseFailure("missing notice title")
        published = _table_cell_text(cells[5])
        if not published:
            raise _ParseFailure("missing publication date")
        return _ListedNotice(
            ccr=_required_attr(link, "data-id2"),
            pan_id=_required_attr(link, "data-id1"),
            upp=_required_attr(link, "data-id3"),
            ais=_required_attr(link, "data-id4"),
            category=_table_cell_text(cells[1]) or "unknown",
            title=title,
            region=_table_cell_text(cells[3]),
            published_at=_parse_published_at(published),
            deadline=_parse_date(_table_cell_text(cells[6]) or ""),
            status=_table_cell_text(cells[7]) or "published",
        )

    @staticmethod
    def _validate_page_cardinality(page: _ParsedListPage) -> None:
        expected_last = (page.total_count + 49) // 50
        if page.total_count == 0 or page.last_page != expected_last:
            raise _ParseFailure("invalid list pagination material")
        expected_rows = 50 if page.current_page < page.last_page else page.total_count % 50 or 50
        if len(page.records) != expected_rows:
            raise _ParseFailure("invalid list page cardinality")

    def _parse_list_row(self, row: Node) -> _ListedNotice:
        title = _normalized_text(row.css_first(".notice-title, .title, h2, h3, a"))
        if not title:
            title = _label_value(row, ("공고명", "공고 제목"))
        if not title:
            raise _ParseFailure("missing notice title")
        published = _label_value(row, ("공고일", "공고일자", "게시일"))
        if not published:
            raise _ParseFailure("missing publication date")
        published_at = _parse_published_at(published)
        deadline = _parse_date(_label_value(row, ("접수마감일", "마감일", "신청마감일")) or "")
        return _ListedNotice(
            ccr=_required_attr(row, "data-id1"),
            pan_id=_required_attr(row, "data-id2"),
            upp=_required_attr(row, "data-id3"),
            ais=_required_attr(row, "data-id4"),
            category=_label_value(row, ("공급유형", "공고유형", "유형")) or "unknown",
            title=title,
            region=_label_value(row, ("지역", "공급지역", "소재지")),
            published_at=published_at,
            deadline=deadline,
            status=_label_value(row, ("상태", "공고상태")) or "published",
        )

    @staticmethod
    def _parse_detail(
        body: str,
        *,
        listed: _ListedNotice | None = None,
    ) -> tuple[
        date | None,
        date | None,
        int | None,
        str | None,
        tuple[str, ...],
        tuple[tuple[str, str], ...],
        tuple[str, ...],
    ]:
        document = HTMLParser(body)
        root = document.css_first("[data-lh-notice-detail]")
        if root is None:
            if listed is None:
                raise _ParseFailure("detail identity is required")
            _require_public_detail_identity(document, listed)
            return _parse_public_detail(document)
        schedule = _label_value(root, ("청약접수기간", "신청접수기간", "접수기간"))
        application_start, application_end, schedule_warning = _parse_schedule_range(schedule)
        supply = _label_value(root, ("공급세대수", "공급규모", "공급수"))
        supply_count = _parse_supply_count(supply) if supply else None
        price_summary = _label_value(root, ("임대조건", "임대보증금 및 월임대료", "분양가격"))
        eligibility = _label_value(root, ("신청자격", "입주자격", "자격요건"))
        facts = tuple(
            ("attachment", f"{href}|internal_analysis_only")
            for link in root.css("a[href]")
            if (href := _normalize(link.attributes.get("href")))
            and "attachment" in (_normalize(link.attributes.get("class")) or "").lower()
        )
        warnings: list[str] = []
        if schedule_warning:
            warnings.append(schedule_warning)
        elif application_start is None:
            warnings.append("application schedule not found")
        elif application_end is None:
            warnings.append("application end date not found")
        if supply_count is None:
            warnings.append("supply count not found")
        if price_summary is None:
            warnings.append("price summary not found")
        if eligibility is None:
            warnings.append("eligibility summary not found")
        return (
            application_start,
            application_end,
            supply_count,
            price_summary,
            (eligibility,) if eligibility else (),
            facts,
            tuple(warnings),
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


def _parse_public_detail(
    document: HTMLParser,
) -> tuple[
    date | None,
    date | None,
    int | None,
    str | None,
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    tuple[str, ...],
]:
    supply_headers = (
        "금회공급 세대수 (예비자 포함)",
        "금회공급 세대수(예비자 포함)",
        "금회공급 세대수",
    )
    matching: list[tuple[Node, int]] = []
    for table in document.css("table"):
        headers = tuple(_table_cell_text(node) for node in table.css("thead th"))
        matched = next((header for header in supply_headers if header in headers), None)
        if matched is not None:
            matching.append((table, headers.index(matched)))
    if not matching and _is_sparse_public_detail(document):
        return (
            None,
            None,
            None,
            None,
            (),
            (),
            (
                "application schedule not found",
                "supply count not found",
                "price summary not found",
                "eligibility summary not found",
            ),
        )
    if not matching:
        raise _ParseFailure("detail parser drift")
    counts: list[int] = []
    for table, supply_index in matching:
        table_counts: list[int] = []
        for row in table.css("tbody tr"):
            cells = tuple(row.css("th, td"))
            if len(cells) <= supply_index:
                raise _ParseFailure("invalid detail row cardinality")
            value = _table_cell_text(cells[supply_index])
            if value is None or re.fullmatch(r"\d{1,3}(?:,\d{3})*|\d+", value) is None:
                raise _ParseFailure("invalid supply count")
            table_counts.append(int(value.replace(",", "")))
        if not table_counts:
            raise _ParseFailure("detail parser drift")
        counts.extend(table_counts)
    if not counts:
        raise _ParseFailure("detail parser drift")
    schedule_dates: list[date] = []
    for candidate in document.css("table"):
        headers = tuple(_table_cell_text(node) for node in candidate.css("thead th"))
        if "신청일시" not in headers:
            continue
        schedule_index = headers.index("신청일시")
        for row in candidate.css("tbody tr"):
            cells = tuple(row.css("th, td"))
            if len(cells) <= schedule_index:
                raise _ParseFailure("invalid detail row cardinality")
            schedule_dates.extend(
                parsed
                for match in _DATE_PATTERN.finditer(
                    _table_cell_text(cells[schedule_index]) or ""
                )
                if (parsed := _parse_date(match.group())) is not None
            )
    application_start = min(schedule_dates) if schedule_dates else None
    application_end = max(schedule_dates) if schedule_dates else None
    warnings = ["price summary not found", "eligibility summary not found"]
    if application_start is None:
        warnings.insert(0, "application schedule not found")
    return (
        application_start,
        application_end,
        sum(counts),
        None,
        (),
        (),
        tuple(warnings),
    )


def _require_public_detail_identity(
    document: HTMLParser,
    listed: _ListedNotice,
) -> None:
    expected = {
        "panId": listed.pan_id,
        "ccrCnntSysDsCd": listed.ccr,
        "uppAisTpCd": listed.upp,
        "aisTpCd": listed.ais,
    }
    for name, value in expected.items():
        observed = {
            normalized
            for node in document.css(f'input[type="hidden"][name="{name}"]')
            if (normalized := _normalize(node.attributes.get("value")))
        }
        if observed != {value}:
            raise _ParseFailure("detail identity does not match list record")
    visible_text = _normalize(document.text(separator=" ")) or ""
    if (_normalize(listed.title) or "") not in visible_text:
        raise _ParseFailure("detail title does not match list record")


def _is_sparse_public_detail(document: HTMLParser) -> bool:
    headings = {
        _table_cell_text(node) for node in document.css("h1, h2, h3, h4")
    }
    labels = {_table_cell_text(node) for node in document.css("dl dt")}
    return "공고문" in headings and "공고문" in labels


def _list_form(window: CollectionWindow, page_index: int) -> dict[str, str]:
    start = window.start.astimezone(_SEOUL).date().isoformat()
    end = window.end.astimezone(_SEOUL).date().isoformat()
    return {
        "schTy": "0",
        "startDt": start,
        "endDt": end,
        "currPage": str(page_index),
        "listCo": "50",
        "viewType": "srch",
        "mi": "1026",
        "schTxt": "",
        "schSido": "",
        "schSigungu": "",
        "schUppAisTpCd": "",
        "schAisTpCd": "",
    }


def _required_attr(node: Node, attribute: str) -> str:
    value = _normalize(node.attributes.get(attribute))
    if not value:
        raise _ParseFailure(f"missing {attribute}")
    return value


def _required_int(node: Node, attribute: str, *, minimum: int) -> int:
    raw = _required_attr(node, attribute)
    if not raw.isascii() or not raw.isdigit():
        raise _ParseFailure("invalid list pagination material")
    value = int(raw)
    if value < minimum:
        raise _ParseFailure("invalid list pagination material")
    return value


def _required_input_int(node: Node | None, *, minimum: int) -> int:
    if node is None:
        raise _ParseFailure("invalid list pagination material")
    return _text_int(_normalize(node.attributes.get("value")), minimum=minimum)


def _text_int(value: str | None, *, minimum: int) -> int:
    if value is None or not value.isascii() or not value.isdigit():
        raise _ParseFailure("invalid list pagination material")
    parsed = int(value)
    if parsed < minimum:
        raise _ParseFailure("invalid list pagination material")
    return parsed


def _public_link_title(link: Node) -> str | None:
    parsed = HTMLParser(link.html)
    for hidden in parsed.css(
        "[aria-hidden='true'], .sr-only, .visually-hidden, .blind, .hidden, .day"
    ):
        hidden.decompose()
    visible = parsed.css_first("a")
    return _normalize(visible.text(separator=" ") if visible is not None else "")


def _table_cell_text(node: Node | None) -> str | None:
    return _normalize(node.text(separator=" ") if node is not None else "")


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


def _parse_schedule_range(value: str | None) -> tuple[date | None, date | None, str | None]:
    if value is None:
        return None, None, None
    matches = tuple(_DATE_PATTERN.finditer(value))
    if len(matches) != 2:
        return None, None, "application schedule ambiguous"
    separator = value[matches[0].end() : matches[1].start()]
    if _SCHEDULE_SEPARATOR.fullmatch(separator) is None:
        return None, None, "application schedule ambiguous"
    start = _parse_date(matches[0].group())
    end = _parse_date(matches[1].group())
    if start is None or end is None:
        return None, None, "application schedule ambiguous"
    return start, end, None


def _parse_date(value: str) -> date | None:
    match = _DATE_PATTERN.search(value)
    if match is None:
        return None
    try:
        return date(int(match["year"]), int(match["month"]), int(match["day"]))
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
