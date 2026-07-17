from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class SourceAttachment:
    url: str
    title: str
    mime_type: str | None = None


@dataclass(frozen=True)
class CollectedSourceRecord:
    external_id: str
    canonical_url: str
    title: str
    publisher: str
    published_at: datetime | None
    collected_at: datetime
    body_text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: tuple[SourceAttachment, ...] = ()

    @property
    def content_hash(self) -> str:
        material = {
            "url": self.canonical_url,
            "title": self.title,
            "body": self.body_text,
            "attachments": [item.__dict__ for item in self.attachments],
            "metadata": self.metadata,
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SourceAdapter(Protocol):
    def collect(self, *, since: datetime, until: datetime) -> list[CollectedSourceRecord]: ...


def build_source_adapter(source_snapshot) -> SourceAdapter:
    from .http import OpenDataJsonAdapter, PublicHtmlAdapter, RssAdapter
    from .housing import ApplyHomeAdapter, LhApplyAdapter

    config = source_snapshot.config
    adapter_name = config.get("adapter", "public_html")
    kwargs = {"source": source_snapshot.source, "config": config}
    adapters = {
        "housing_applyhome": ApplyHomeAdapter,
        "housing_lh": LhApplyAdapter,
        "open_data_json": OpenDataJsonAdapter,
        "rss": RssAdapter,
        "public_html": PublicHtmlAdapter,
    }
    try:
        return adapters[adapter_name](**kwargs)
    except KeyError as exc:
        raise ValueError(f"Unsupported source adapter: {adapter_name}") from exc
