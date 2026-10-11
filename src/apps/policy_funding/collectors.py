"""Bounded collectors with explicit source failures and incomplete coverage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from apps.policy_funding.catalog import source_by_id
from apps.policy_funding.contracts import FundingNotice
from apps.policy_funding.parsing import (
    parse_bizinfo_detail,
    parse_bizinfo_list,
    parse_ols_detail,
    parse_ols_list,
)
from apps.policy_funding.transport import OfficialFundingFetcher


@dataclass(frozen=True)
class CollectionLimits:
    pages: int = 1
    notices: int = 20
    seconds: int = 90

    def __post_init__(self):
        if not (
            type(self.pages) is int
            and 1 <= self.pages <= 5
            and type(self.notices) is int
            and 1 <= self.notices <= 100
            and type(self.seconds) is int
            and 1 <= self.seconds <= 300
        ):
            raise ValueError("Invalid policy collection limits")


@dataclass(frozen=True)
class CollectionOutcome:
    source_id: str
    notices: tuple[FundingNotice, ...]
    status: str
    errors: tuple[str, ...]
    request_count: int
    coverage_limited: bool


def collect_source(source_id: str, limits: CollectionLimits, *, fetcher=None) -> CollectionOutcome:
    source = source_by_id(source_id)
    if not source.enabled:
        return CollectionOutcome(source_id, (), "discovery_only", (), 0, True)
    fetcher = fetcher or OfficialFundingFetcher(
        source,
        max_requests=min(150, limits.notices + limits.pages * 2 + 6),
        max_seconds=limits.seconds,
    )
    notices, errors, seen = [], [], set()
    partitions = ("N", "Y") if source.adapter == "bizinfo" else (None,)
    for partition_index, partition in enumerate(partitions):
        partition_budget = limits.notices // len(partitions) + (
            1 if partition_index < limits.notices % len(partitions) else 0
        )
        attempted = 0
        for page_number in range(1, limits.pages + 1):
            try:
                if source.adapter == "bizinfo":
                    page = fetcher.get(
                        source.entry_url,
                        params={
                            "hashCode": "01",
                            "rows": "15",
                            "cpage": str(page_number),
                            "orderGb": "1",
                            "sort": "desc",
                            "schEndAt": partition,
                        },
                    )
                    links = parse_bizinfo_list(page.text)
                    total_pages = limits.pages if len(links) >= 15 else page_number
                else:
                    page = fetcher.post(
                        "https://ols.semas.or.kr/ols/man/SMAN051M/search.do",
                        data={
                            "bltwtrClcd": "",
                            "bltwtrTitNm": "",
                            "searchStd": "1",
                            "pageNo": str(page_number),
                            "type": "2" if source_id == "semas_agency" else "1",
                        },
                    )
                    links, total_pages = parse_ols_list(json.loads(page.text))
            except Exception as exc:
                errors.append(f"list:{partition or 'all'}:{page_number}:{type(exc).__name__}")
                break
            for link in links:
                if link.external_id in seen:
                    continue
                if attempted >= partition_budget:
                    break
                seen.add(link.external_id)
                attempted += 1
                try:
                    detail = fetcher.get(link.official_url)
                    source.validate_url(detail.url)
                    requested, received = urlsplit(link.official_url), urlsplit(detail.url)
                    identity_key = "pblancId" if source.adapter == "bizinfo" else "bltwtrSeq"
                    if (
                        requested.hostname != received.hostname
                        or requested.path != received.path
                        or parse_qs(received.query).get(identity_key) != [link.external_id]
                        or source.adapter == "ols"
                        and parse_qs(received.query).get("bbsTypeCd") != ["01"]
                    ):
                        raise ValueError("Official detail redirect changed notice identity")
                    if source.adapter == "bizinfo":
                        notice = parse_bizinfo_detail(
                            detail.text,
                            link,
                            fetched_at=detail.fetched_at,
                            listing_state="open_list" if partition == "N" else "ended_list",
                        )
                    else:
                        notice = parse_ols_detail(
                            detail.text, link, source_id=source_id, fetched_at=detail.fetched_at
                        )
                    notices.append(notice)
                except Exception as exc:
                    errors.append(f"detail:{link.external_id}:{type(exc).__name__}")
            if page_number >= total_pages or attempted >= partition_budget:
                break
    status = "partial" if errors and notices else "failed" if errors else "completed"
    return CollectionOutcome(
        source_id, tuple(notices), status, tuple(errors), fetcher.request_count, True
    )
