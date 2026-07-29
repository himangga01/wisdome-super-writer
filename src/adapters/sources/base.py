from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)


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


_FROZEN_SOURCE_SCHEMA_VERSIONS = frozenset(
    {
        "source-definition-snapshot-v2",
        "source-definition-snapshot-legacy-v1",
    }
)


def _frozen_snapshot_material(source_snapshot) -> dict[str, Any]:
    material = source_snapshot.frozen_config
    if (
        not isinstance(material, dict)
        or material.get("schemaVersion")
        not in _FROZEN_SOURCE_SCHEMA_VERSIONS
    ):
        raise ValueError(
            "Source adapters require a frozen source snapshot schema."
        )
    actual_hash = canonical_hash(
        material,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    if actual_hash != source_snapshot.frozen_config_hash:
        raise ValueError(
            "Frozen source snapshot hash is inconsistent."
        )
    return material


def source_adapter_key(source_snapshot) -> str:
    material = _frozen_snapshot_material(source_snapshot)
    adapter_key = material.get("adapterKey")
    if not isinstance(adapter_key, str) or not adapter_key:
        raise ValueError(
            "Frozen source snapshot has no adapter key."
        )
    return adapter_key


def _snapshot_adapter_input(
    source_snapshot,
) -> tuple[FrozenSourceDefinition, dict[str, Any]]:
    material = _frozen_snapshot_material(source_snapshot)
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
