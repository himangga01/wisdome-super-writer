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


@dataclass(frozen=True)
class FrozenSourceDefinition:
    id: Any
    topic_code: str
    display_name: str
    publisher: str
    owner_name: str
    editorial_control_name: str
    base_url: str
    authority_tier: str
    access_method: str
    independence_group: str
    enabled: bool


def source_adapter_key(source_snapshot) -> str:
    config = source_snapshot.config
    return str(config.get("adapterKey") or config.get("adapter") or "public_html")


def _snapshot_adapter_input(source_snapshot) -> tuple[FrozenSourceDefinition, dict[str, Any]]:
    material = source_snapshot.config
    if material.get("schemaVersion") in {
        "source-definition-snapshot-v2",
        "source-definition-snapshot-legacy-v1",
    }:
        frozen_source = FrozenSourceDefinition(
            id=source_snapshot.source_id,
            topic_code=str(material["topic"]),
            display_name=str(material["name"]),
            publisher=str(material["publisher"]),
            owner_name=str(material["ownerName"]),
            editorial_control_name=str(material["editorialControlName"]),
            base_url=str(material["baseUrl"]),
            authority_tier=str(material["authorityTier"]),
            access_method=str(material["accessMethod"]),
            independence_group=str(material["independenceGroupId"]),
            enabled=bool(material["enabled"]),
        )
        adapter_config = dict(material.get("externalConfig") or {})
        adapter_config.update(
            {
                "adapter": material["adapterKey"],
                "adapterKey": material["adapterKey"],
                "allowedContentTypes": list(material.get("allowedMimeTypes") or []),
                "rightsStatus": material.get("defaultRightsStatus"),
                "termsUrl": material.get("termsUrl"),
                "robotsUrl": material.get("robotsUrl"),
                "licenseUrl": material.get("licenseUrl"),
                "pollIntervalSeconds": material.get("pollIntervalSeconds"),
                "rateLimitPolicy": dict(material.get("rateLimitPolicy") or {}),
                "secretRef": material.get("secretRef"),
            }
        )
        return frozen_source, adapter_config

    # This branch only supports a database that has not yet applied the T009
    # migration. The migration freezes these values into every legacy snapshot.
    source = source_snapshot.source
    return (
        FrozenSourceDefinition(
            id=source.id,
            topic_code=source.topic_code,
            display_name=source.display_name,
            publisher=getattr(source, "publisher", source.owner_name),
            owner_name=source.owner_name,
            editorial_control_name=getattr(
                source,
                "editorial_control_name",
                source.owner_name,
            ),
            base_url=source.base_url,
            authority_tier=source.authority_tier,
            access_method=source.access_method,
            independence_group=source.independence_group,
            enabled=source.enabled,
        ),
        dict(material),
    )


def build_source_adapter(source_snapshot) -> SourceAdapter:
    from .http import OpenDataJsonAdapter, PublicHtmlAdapter, RssAdapter
    from .housing import ApplyHomeAdapter, LhApplyAdapter

    source, config = _snapshot_adapter_input(source_snapshot)
    adapter_name = source_adapter_key(source_snapshot)
    kwargs = {"source": source, "config": config}
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
