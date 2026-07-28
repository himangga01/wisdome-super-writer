from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.utils import timezone
from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

from .models import (
    SourceDefinition,
    SourceDefinitionSnapshot,
    SourceRegistryMembership,
    SourceRegistrySnapshot,
    TopicPolicy,
)


@dataclass(frozen=True)
class RegistryImportResult:
    topic_code: str
    registry_id: str
    source_count: int
    created: bool


@transaction.atomic
def import_registry_manifest(path: str | Path) -> RegistryImportResult:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    topic = data["topicCode"]
    policy_material = data.get("policy", {})
    policy_hash = canonical_hash(
        policy_material, schema_version=CANONICAL_HASH_SCHEMA_V1
    )
    TopicPolicy.objects.get_or_create(
        code=topic,
        version=int(data.get("policyVersion", 1)),
        defaults={
            "title": data["title"],
            "freshness_minutes": int(data.get("freshnessMinutes", 1440)),
            "policy": policy_material,
            "policy_hash": policy_hash,
        },
    )

    snapshots: list[SourceDefinitionSnapshot] = []
    for raw in data["sources"]:
        source, _ = SourceDefinition.objects.update_or_create(
            topic_code=topic,
            key=raw["key"],
            defaults={
                "display_name": raw["displayName"],
                "owner_name": raw["ownerName"],
                "base_url": raw["baseUrl"],
                "authority_tier": raw["authorityTier"],
                "access_method": raw["accessMethod"],
                "independence_group": raw["independenceGroup"],
                "enabled": raw.get("enabled", True),
            },
        )
        config = {
            "entrypoints": raw.get("entrypoints", []),
            "allowedContentTypes": raw.get("allowedContentTypes", []),
            "rightsStatus": raw.get("rightsStatus", "internal_analysis_only"),
            "termsUrl": raw.get("termsUrl"),
            "pollMinutes": raw.get("pollMinutes", 60),
            "rateLimitPerMinute": raw.get("rateLimitPerMinute", 10),
            "adapter": raw.get("adapter", "public_html"),
        }
        config_hash = canonical_hash(config, schema_version=CANONICAL_HASH_SCHEMA_V1)
        snapshot, _ = SourceDefinitionSnapshot.objects.get_or_create(
            source=source,
            config_hash=config_hash,
            defaults={"version": source.current_snapshot_version, "config": config},
        )
        snapshots.append(snapshot)

    manifest = [
        {"sourceSnapshotId": str(s.id), "configHash": s.config_hash, "enabled": s.source.enabled}
        for s in sorted(snapshots, key=lambda row: str(row.source_id))
    ]
    manifest_hash = canonical_hash(manifest, schema_version=CANONICAL_HASH_SCHEMA_V1)
    registry, created = SourceRegistrySnapshot.objects.get_or_create(
        topic_code=topic,
        manifest_hash=manifest_hash,
        defaults={"version": SourceRegistrySnapshot.objects.filter(topic_code=topic).count() + 1},
    )
    if created:
        SourceRegistryMembership.objects.bulk_create(
            [
                SourceRegistryMembership(
                    registry=registry,
                    source_snapshot=snapshot,
                    enabled=snapshot.source.enabled,
                    display_order=index,
                )
                for index, snapshot in enumerate(snapshots)
            ]
        )
    return RegistryImportResult(topic, str(registry.id), len(snapshots), created)


@transaction.atomic
def approve_registry(registry: SourceRegistrySnapshot, admin) -> SourceRegistrySnapshot:
    if registry.state != SourceRegistrySnapshot.State.DRAFT:
        return registry
    now = timezone.now()
    SourceDefinitionSnapshot.objects.filter(
        id__in=registry.memberships.values_list("source_snapshot_id", flat=True),
        state=SourceDefinitionSnapshot.State.DRAFT,
    ).update(state=SourceDefinitionSnapshot.State.APPROVED, approved_by=admin, approved_at=now)
    SourceRegistrySnapshot.objects.filter(
        topic_code=registry.topic_code,
        state=SourceRegistrySnapshot.State.APPROVED,
    ).update(state=SourceRegistrySnapshot.State.RETIRED)
    registry.state = SourceRegistrySnapshot.State.APPROVED
    registry.approved_by = admin
    registry.approved_at = now
    registry.save(update_fields=["state", "approved_by", "approved_at"])
    return registry


def current_registry(topic_code: str) -> SourceRegistrySnapshot:
    return SourceRegistrySnapshot.objects.prefetch_related(
        "memberships__source_snapshot__source"
    ).get(topic_code=topic_code, state=SourceRegistrySnapshot.State.APPROVED)
