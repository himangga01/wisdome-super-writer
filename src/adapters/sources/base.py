from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)

from .manifests import (
    adapter_execution_manifest,
    adapter_execution_manifest_hash,
)


@dataclass(frozen=True)
class SourceAttachment:
    url: str
    title: str
    mime_type: str | None = None
    external_id: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None
    rights_status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CollectedSourceRecord:
    external_id: str
    canonical_url: str
    title: str
    publisher: str
    published_at: datetime | None
    collected_at: datetime
    body_text: str
    modified_at: datetime | None = None
    status: str = "active"
    reconciliation_only: bool = False
    raw_checksum: str | None = None
    http_metadata: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: tuple[SourceAttachment, ...] = ()

    def __post_init__(self) -> None:
        if not self.external_id.strip():
            raise ValueError("Collected source records require a stable external identity.")
        if self.status not in {
            "active",
            "corrected",
            "retracted",
            "unavailable",
        }:
            raise ValueError("Collected source record has an unsupported status.")

    @property
    def content_hash(self) -> str:
        material = {
            "url": self.canonical_url,
            "title": self.title,
            "body": self.body_text,
            "attachments": [
                {
                    "url": item.url,
                    "title": item.title,
                    "mimeType": item.mime_type,
                    "externalId": item.external_id,
                    "sizeBytes": item.size_bytes,
                    "checksum": item.checksum,
                    "metadata": item.metadata,
                }
                for item in self.attachments
            ],
            "metadata": self.metadata,
        }
        return canonical_hash(
            material,
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        )


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
        "source-definition-snapshot-v3",
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


def source_adapter_version(source_snapshot) -> str:
    material = _frozen_snapshot_material(source_snapshot)
    return _verified_adapter_execution(material)[0]


def source_adapter_implementation_manifest_hash(
    source_snapshot,
) -> str:
    material = _frozen_snapshot_material(source_snapshot)
    return _verified_adapter_execution(material)[1]


def source_record_hosts(source_snapshot) -> frozenset[str]:
    material = _frozen_snapshot_material(source_snapshot)
    base_host = (
        urlsplit(str(material["baseUrl"])).hostname or ""
    ).rstrip(".").lower()
    raw_hosts = (
        material.get("externalConfig", {}).get("recordHosts", [])
        if isinstance(material.get("externalConfig"), dict)
        else []
    )
    if not isinstance(raw_hosts, list) or any(
        not isinstance(host, str) or not host
        for host in raw_hosts
    ):
        raise ValueError(
            "Frozen source snapshot has invalid recordHosts."
        )
    hosts = {
        host.rstrip(".").encode("idna").decode("ascii").lower()
        for host in raw_hosts
    }
    if base_host:
        hosts.add(base_host)
    return frozenset(hosts)


def source_attachment_content_types(
    source_snapshot,
) -> frozenset[str]:
    material = _frozen_snapshot_material(source_snapshot)
    external_config = material.get("externalConfig")
    if not isinstance(external_config, dict):
        external_config = {}
    raw_types = external_config.get(
        "attachmentContentTypes",
        material.get("allowedMimeTypes", []),
    )
    if not isinstance(raw_types, list) or any(
        not isinstance(value, str) or not value.strip()
        for value in raw_types
    ):
        raise ValueError(
            "Frozen source snapshot has invalid attachment MIME types."
        )
    return frozenset(
        value.split(";", 1)[0].strip().lower()
        for value in raw_types
    )


def source_reconciliation_days(source_snapshot) -> int:
    material = _frozen_snapshot_material(source_snapshot)
    external_config = material.get("externalConfig")
    if not isinstance(external_config, dict):
        return 0
    value = external_config.get("reconciliationDays", 0)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 3660
    ):
        raise ValueError(
            "Frozen source snapshot has invalid reconciliationDays."
        )
    return value


def _verified_adapter_execution(
    material: dict[str, Any],
) -> tuple[str, str]:
    adapter_key = str(material.get("adapterKey", ""))
    access_method = str(material.get("accessMethod", ""))
    current_manifest = adapter_execution_manifest(
        adapter_key,
        access_method=access_method,
    )
    current_hash = adapter_execution_manifest_hash(current_manifest)
    schema_version = material.get("schemaVersion")
    if schema_version == "source-definition-snapshot-v3":
        stored_version = material.get("adapterVersion")
        stored_hash = material.get(
            "adapterImplementationManifestHash"
        )
        if (
            stored_version != current_manifest["adapterVersion"]
            or stored_hash != current_hash
        ):
            raise ValueError(
                "Frozen source snapshot adapter implementation is not "
                "available in this deployment."
            )
        return str(stored_version), str(stored_hash)

    # Pre-v3 snapshots never bound an implementation. They remain executable
    # only where the current deployment still provides their exact legacy v1
    # public-page implementation.
    if current_manifest["adapterVersion"] != "v1":
        raise ValueError(
            "Legacy source snapshot has no frozen adapter implementation; "
            "create and approve a current snapshot before collection."
        )
    return "v1", current_hash


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


def build_source_adapter(
    source_snapshot,
    *,
    runtime_mode: str = "collection",
    reconciliation_external_ids: tuple[str, ...] = (),
) -> SourceAdapter:
    from .http import OpenDataJsonAdapter, PublicHtmlAdapter, RssAdapter
    from .housing import ApplyHomeAdapter, LhApplyAdapter

    if runtime_mode not in {"collection", "source_check"}:
        raise ValueError("Unsupported source adapter runtime mode.")
    source_adapter_version(source_snapshot)
    source, config = _snapshot_adapter_input(source_snapshot)
    config["_runtimeMode"] = runtime_mode
    config["_reconciliationExternalIds"] = list(
        reconciliation_external_ids
    )
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
