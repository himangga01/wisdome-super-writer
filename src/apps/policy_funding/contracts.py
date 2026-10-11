"""Closed, bounded notice contracts with separate observation and semantic identity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

from apps.policy_funding.catalog import REGIONS, load_catalog, source_by_id

KINDS = {
    "direct_loan": "직접대출",
    "agency_loan": "대리대출",
    "guarantee": "신용보증",
    "interest_subsidy": "이자 지원",
    "guarantee_fee": "보증료 지원",
    "grant": "지원금",
    "policy_funding": "정책자금",
}


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FundingNotice:
    source_id: str
    external_id: str
    title: str
    issuer: str
    official_url: str
    published_on: date | None
    application_period: str | None
    regions: tuple[str, ...]
    kinds: tuple[str, ...]
    evidence_text: str
    fetched_at: datetime
    attachments: tuple[tuple[str, str], ...] = ()
    origin_url: str | None = None
    application_url: str | None = None
    listing_state: str = "unknown"
    provenance: str = "official_fetch"

    def __post_init__(self) -> None:
        source = source_by_id(self.source_id)
        source.validate_url(self.official_url)
        parts = urlsplit(self.official_url)
        query = parse_qs(parts.query, strict_parsing=True)
        expected = (
            ("pblancId", "/sii/siia/selectSIIA200Detail.do")
            if source.adapter == "bizinfo"
            else ("bltwtrSeq", "/ols/man/SMAN052M/page.do")
        )
        if (
            not source.enabled
            or query.get(expected[0]) != [self.external_id]
            or parts.path != expected[1]
        ):
            raise ValueError("Notice URL differs from source identity")
        if not isinstance(self.fetched_at, datetime) or self.fetched_at.utcoffset() is None:
            raise ValueError("Funding observations require an aware timestamp")
        if self.published_on is not None and type(self.published_on) is not date:
            raise ValueError("Invalid notice publication date")
        for value, maximum in (
            (self.external_id, 100),
            (self.title, 500),
            (self.issuer, 300),
            (self.evidence_text, 40000),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError("Notice text is empty or too long")
        if self.application_period is not None and (
            not isinstance(self.application_period, str) or len(self.application_period) > 2000
        ):
            raise ValueError("Invalid application-period text")
        if (
            not isinstance(self.regions, tuple)
            or not self.regions
            or not set(self.regions) <= {"national", "unspecified", *REGIONS}
            or not isinstance(self.kinds, tuple)
            or not self.kinds
            or not set(self.kinds) <= set(KINDS)
        ):
            raise ValueError("Invalid notice region or funding kind")
        if self.listing_state not in {"open_list", "ended_list", "unknown"}:
            raise ValueError("Unknown official listing state")
        if self.provenance not in {"official_fetch", "fixture"}:
            raise ValueError("Unknown funding provenance")
        if len(self.attachments) > 20:
            raise ValueError("Too many attachment references")
        for label, url in self.attachments:
            if not isinstance(label, str) or len(label) > 300:
                raise ValueError("Invalid attachment reference")
            validate_reference_url(url)
        if self.origin_url is not None:
            validate_reference_url(self.origin_url)
        if self.application_url is not None:
            validate_reference_url(self.application_url)

    @property
    def stable_id(self) -> str:
        return content_hash([self.source_id, self.external_id])

    @property
    def content_hash(self) -> str:
        data = self.to_dict()
        del data["fetched_at"]
        del data["provenance"]
        return content_hash(data)

    def to_dict(self) -> dict:
        value = asdict(self)
        value["fetched_at"] = self.fetched_at.isoformat()
        value["published_on"] = self.published_on.isoformat() if self.published_on else None
        for key in ("regions", "kinds", "attachments"):
            value[key] = [list(item) if isinstance(item, tuple) else item for item in value[key]]
        return value

    @classmethod
    def from_dict(cls, value: dict) -> FundingNotice:
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise ValueError("Unknown or missing funding notice fields")
        if any(not isinstance(value[key], list) for key in ("regions", "kinds", "attachments")):
            raise ValueError("Funding list fields must be arrays")
        try:
            return cls(
                **{
                    **value,
                    "fetched_at": datetime.fromisoformat(value["fetched_at"]),
                    "published_on": date.fromisoformat(value["published_on"])
                    if value["published_on"]
                    else None,
                    "regions": tuple(value["regions"]),
                    "kinds": tuple(value["kinds"]),
                    "attachments": tuple(tuple(item) for item in value["attachments"]),
                }
            )
        except (TypeError, KeyError, AttributeError) as exc:
            raise ValueError("Malformed funding notice") from exc


def validate_reference_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("Invalid official reference")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
        or "\\" in url
        or any(ord(c) < 32 for c in url)
    ):
        raise ValueError("Unsafe official reference")
    hosts = {host for source in load_catalog() for host in source.allowed_hosts}
    if parsed.hostname not in hosts and not (parsed.hostname or "").endswith(".go.kr"):
        raise ValueError("Unreviewed reference host")
    return url
