"""Fixture-backed parsers for the researched public notice interfaces."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from selectolax.parser import HTMLParser

from apps.policy_funding.catalog import REGIONS, source_by_id
from apps.policy_funding.contracts import FundingNotice, validate_reference_url


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().replace("<", "＜").replace(">", "＞")


def _tree(body: str) -> HTMLParser:
    if not isinstance(body, str) or len(body) > 5 * 1024 * 1024:
        raise ValueError("Official document exceeds its bounds")
    tree = HTMLParser(body)
    for node in tree.css("script,style,iframe,object,embed,noscript"):
        node.decompose()
    return tree


def _text(node) -> str:
    return clean_text(node.text(separator=" ", strip=True)) if node else ""


def _date(value: str) -> date | None:
    match = re.search(r"\b(20\d{2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})\b", value)
    return date(*map(int, match.groups())) if match else None


def classify_regions(title: str, issuer: str) -> tuple[str, ...]:
    # Explicit province scope wins over a same-named municipality in another province.
    bracket = re.match(r"\[([^]]+)\]", title)
    aliases = {label: key for key, label in REGIONS.items()}
    aliases.update(
        {
            "서울특별시": "seoul",
            "부산광역시": "busan",
            "대구광역시": "daegu",
            "인천광역시": "incheon",
            "광주광역시": "gwangju",
            "대전광역시": "daejeon",
            "울산광역시": "ulsan",
            "세종특별자치시": "sejong",
            "경기도": "gyeonggi",
            "강원특별자치도": "gangwon",
            "강원도": "gangwon",
            "충청북도": "chungbuk",
            "충청남도": "chungnam",
            "전북특별자치도": "jeonbuk",
            "전라북도": "jeonbuk",
            "전라남도": "jeonnam",
            "경상북도": "gyeongbuk",
            "경상남도": "gyeongnam",
            "제주특별자치도": "jeju",
        }
    )
    municipalities = {"양산": "gyeongnam", "포항": "gyeongbuk", "영천": "gyeongbuk"}
    if bracket:
        declared = bracket[1].strip()
        tokens = re.split(r"[\s,·ㆍ/]+", declared)
        explicit = tuple(sorted({aliases[token] for token in tokens if token in aliases}))
        if explicit:
            return explicit
        if declared.removesuffix("시") in municipalities:
            return (municipalities[declared.removesuffix("시")],)
    for alias, region in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if issuer == alias or len(alias) > 2 and issuer.startswith(alias):
            return (region,)
    if issuer.removesuffix("시") in municipalities:
        return (municipalities[issuer.removesuffix("시")],)
    national = {
        "중소벤처기업부",
        "소상공인시장진흥공단",
        "중소벤처기업진흥공단",
        "산업통상부",
        "산업통상자원부",
        "고용노동부",
        "과학기술정보통신부",
    }
    if not bracket and issuer in national:
        return ("national",)
    return ("unspecified",)


def classify_kinds(title: str, summary: str = "") -> tuple[str, ...]:
    material = title + " " + summary
    mapping = {
        "직접대출": "direct_loan",
        "대리대출": "agency_loan",
        "특례보증": "guarantee",
        "신용보증": "guarantee",
        "이차보전": "interest_subsidy",
        "이자지원": "interest_subsidy",
        "이자 지원": "interest_subsidy",
    }
    kinds = {kind for text, kind in mapping.items() if text in material}
    fee_support = re.search(r"보증료(?:를|의|\s|일부|전액|감면|납부|최초|[0-9년분])*지원", material)
    fee_excluded = re.search(
        r"보증료[^.!?\n]{0,35}(?:제외|미지원|불가|불포함|아니|아닙|않|지원하지|지원\s*(?:은|이)?\s*없|본인\s*부담|신청인\s*부담)",
        material,
    )
    if fee_support and not fee_excluded:
        kinds.add("guarantee_fee")
    return tuple(sorted(kinds)) or ("policy_funding",)


@dataclass(frozen=True)
class NoticeLink:
    external_id: str
    title: str
    official_url: str
    published_on: date | None
    application_period: str | None = None
    issuer: str = "소상공인시장진흥공단"
    loan_route: str | None = None


def parse_bizinfo_list(body: str) -> tuple[NoticeLink, ...]:
    tree = _tree(body)
    table = tree.css_first(".table_Type_1")
    if table is None:
        raise ValueError("Bizinfo notice table missing; source may be blocked or changed")
    rows = []
    source = source_by_id("bizinfo_finance")
    for row in table.css("tbody tr"):
        cells = row.css("td")
        if len(cells) == 1 and any(word in _text(cells[0]) for word in ("없습니다", "없음")):
            continue
        if len(cells) != 8:
            raise ValueError("Bizinfo notice row schema changed")
        link = cells[2].css_first("a[href]")
        if link is None:
            raise ValueError("Bizinfo notice link missing")
        full = urljoin(source.entry_url, link.attributes["href"])
        source.validate_url(full)
        identity = parse_qs(urlsplit(full).query).get("pblancId", [])
        if len(identity) != 1 or not re.fullmatch(r"PBLN_\d{8,30}", identity[0]):
            raise ValueError("Bizinfo notice identity invalid")
        if _text(cells[1]) != "금융":
            continue
        canonical = "https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?" + urlencode(
            {"pblancId": identity[0]}
        )
        rows.append(
            NoticeLink(
                identity[0],
                _text(link),
                canonical,
                _date(_text(cells[6])),
                _text(cells[3]) or None,
                _text(cells[4]),
            )
        )
    if not table.css("tbody tr"):
        raise ValueError("Bizinfo table has no explicit rows or empty result")
    return tuple(rows)


def _reference(node, base: str) -> str | None:
    if node is None or not node.attributes.get("href"):
        return None
    value = urljoin(base, node.attributes["href"])
    try:
        return validate_reference_url(value)
    except ValueError:
        return None


def parse_bizinfo_detail(
    body: str, link: NoticeLink, *, fetched_at: datetime, listing_state: str
) -> FundingNotice:
    tree = _tree(body)
    title = _text(tree.css_first("h2.title"))
    if title != clean_text(link.title):
        raise ValueError("Bizinfo detail title differs from its list identity")
    detail = {}
    application_url = None
    for row in tree.css(".view_cont > ul > li"):
        label, text = _text(row.css_first("span.s_title")), _text(row.css_first("div.txt"))
        if label and text:
            detail[label] = text
        if label == "사업신청 사이트":
            application_url = _reference(row.css_first("a[href]"), link.official_url)
    if not detail.get("사업개요") or not detail.get("소관부처·지자체"):
        raise ValueError("Bizinfo official program fields missing")
    evidence = "\n".join(f"{label}: {value}" for label, value in detail.items())
    issuer = detail["소관부처·지자체"]
    attachments = []
    for node in tree.css("a[href]"):
        if "fileDown.do" in node.attributes["href"]:
            reference = _reference(node, link.official_url)
            if reference and (_text(node), reference) not in attachments:
                attachments.append((_text(node)[:300] or "공식 첨부파일", reference))
    return FundingNotice(
        source_id="bizinfo_finance",
        external_id=link.external_id,
        title=title,
        issuer=issuer,
        official_url=link.official_url,
        published_on=link.published_on,
        application_period=detail.get("신청기간") or link.application_period,
        regions=classify_regions(title, issuer),
        kinds=classify_kinds(title, detail["사업개요"]),
        evidence_text=evidence,
        fetched_at=fetched_at,
        attachments=tuple(attachments[:20]),
        origin_url=_reference(tree.css_first("a#barogagi"), link.official_url),
        application_url=application_url,
        listing_state=listing_state,
    )


def parse_ols_list(value: dict) -> tuple[tuple[NoticeLink, ...], int]:
    if not isinstance(value, dict) or not isinstance(value.get("result"), list):
        raise ValueError("OLS search schema changed")
    meta = value.get("pagination", {})
    pages = meta.get("vTotalPage")
    total = meta.get("totalCount")
    if (
        type(pages) is not int
        or type(total) is not int
        or not 0 <= pages <= 10000
        or total < len(value["result"])
        or len(value["result"]) > 100
    ):
        raise ValueError("OLS pagination is invalid")
    rows = []
    for item in value["result"]:
        identity = str(item.get("bltwtrSeq", ""))
        title = item.get("bltwtrTitNm")
        if (
            not identity.isascii()
            or not identity.isdecimal()
            or len(identity) > 12
            or item.get("bbsTypeCd") != "01"
            or not isinstance(title, str)
            or not title.strip()
        ):
            raise ValueError("OLS notice identity invalid")
        url = "https://ols.semas.or.kr/ols/man/SMAN052M/page.do?" + urlencode(
            {"bltwtrSeq": identity, "bbsTypeCd": "01"}
        )
        rows.append(
            NoticeLink(
                identity,
                clean_text(title),
                url,
                _date(str(item.get("frstRegDt", ""))),
                loan_route=item.get("loanSeCdNm"),
            )
        )
    return tuple(rows), pages


def parse_ols_detail(
    body: str, link: NoticeLink, *, source_id: str, fetched_at: datetime
) -> FundingNotice:
    tree = _tree(body)
    title = _text(tree.css_first(".board_view .view_title"))
    evidence = _text(tree.css_first("#cntnDiv"))
    if title != clean_text(link.title) or not evidence:
        raise ValueError("OLS detail identity or content is missing")
    expected_route = "대리대출" if source_id == "semas_agency" else "직접대출"
    if link.loan_route and link.loan_route != expected_route:
        raise ValueError("OLS loan route differs from requested source")
    period = re.search(
        r"(?:^|[□■○●])\s*(?:접수|신청)기간\s*[:：]\s*(.+?)(?:\s*[□■○●]|\s*\*|$)",
        evidence,
    )
    return FundingNotice(
        source_id=source_id,
        external_id=link.external_id,
        title=title,
        issuer="소상공인시장진흥공단",
        official_url=link.official_url,
        published_on=link.published_on,
        application_period=period[1].strip() if period else None,
        regions=("national",),
        kinds=("agency_loan",) if source_id == "semas_agency" else ("direct_loan",),
        evidence_text=evidence,
        fetched_at=fetched_at,
        application_url="https://ols.semas.or.kr/ols/man/SMAN010M/page.do",
    )
