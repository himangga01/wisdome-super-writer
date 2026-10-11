"""Reviewed official sources; discovery entries do not imply live coverage."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

REGIONS = {
    "seoul": "서울",
    "busan": "부산",
    "daegu": "대구",
    "incheon": "인천",
    "gwangju": "광주",
    "daejeon": "대전",
    "ulsan": "울산",
    "sejong": "세종",
    "gyeonggi": "경기",
    "gangwon": "강원",
    "chungbuk": "충북",
    "chungnam": "충남",
    "jeonbuk": "전북",
    "jeonnam": "전남",
    "gyeongbuk": "경북",
    "gyeongnam": "경남",
    "jeju": "제주",
}
CATALOG_PATH = Path(__file__).resolve().parents[3] / "config/policy-funding/sources.json"


@dataclass(frozen=True)
class FundingSource:
    source_id: str
    name: str
    authority: str
    authority_url: str
    regions: tuple[str, ...]
    entry_url: str
    allowed_hosts: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    adapter: str | None
    enabled: bool
    access_note: str
    verified_on: str

    def validate_url(self, url: str) -> str:
        if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url):
            raise ValueError("Invalid official URL")
        try:
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or parts.hostname not in self.allowed_hosts
                or parts.port not in (None, 443)
                or parts.username
                or parts.password
                or parts.fragment
                or "\\" in url
            ):
                raise ValueError("Official URL authority is not allowed")
            path = parts.path
            for _ in range(4):
                decoded = unquote(path, errors="strict")
                if any(piece in {".", ".."} for piece in decoded.split("/")) or "\\" in decoded:
                    raise ValueError("Official URL path traverses")
                if decoded == path:
                    break
                if decoded.count("/") != path.count("/"):
                    raise ValueError("Official URL path is ambiguous")
                path = decoded
            else:
                raise ValueError("Official URL is excessively encoded")
            if path != "/robots.txt" and not any(
                path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
                for prefix in self.allowed_paths
            ):
                raise ValueError("Official URL route is not allowed")
        except (TypeError, UnicodeError) as exc:
            raise ValueError("Invalid official URL") from exc
        return url


def load_catalog(path: Path | None = None) -> tuple[FundingSource, ...]:
    payload = json.loads((path or CATALOG_PATH).read_text(encoding="utf-8"))
    if (
        set(payload) != {"schema_version", "region_note", "sources"}
        or payload["schema_version"] != 1
    ):
        raise ValueError("Unknown funding catalog schema")
    rows = []
    seen = set()
    for data in payload["sources"]:
        source = FundingSource(
            **{
                **data,
                "regions": tuple(data["regions"]),
                "allowed_hosts": tuple(data["allowed_hosts"]),
                "allowed_paths": tuple(data["allowed_paths"]),
            }
        )
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", source.source_id)
            or source.source_id in seen
            or not set(source.regions) <= {"national", *REGIONS}
            or type(source.enabled) is not bool
            or source.enabled
            and source.adapter not in {"ols", "bizinfo"}
        ):
            raise ValueError("Invalid funding source")
        source.validate_url(source.entry_url)
        if not source.authority_url.startswith("https://"):
            raise ValueError("Source authority evidence is required")
        seen.add(source.source_id)
        rows.append(source)
    return tuple(rows)


def source_by_id(source_id: str) -> FundingSource:
    for source in load_catalog():
        if source.source_id == source_id:
            return source
    raise ValueError("Unknown funding source")
