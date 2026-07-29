from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.accounts.services import consume_reauthentication_proof
from apps.audit.models import AuditEvent
from apps.audit.services import (
    AuditContext,
    audit_event_id,
    record_audit_event,
    require_audit_replay,
)
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


def _registry_material(registry: SourceRegistrySnapshot) -> dict:
    return {
        "schema_version": "source-registry-audit-v1",
        "registry_id": str(registry.id),
        "topic_code": registry.topic_code,
        "version": registry.version,
        "state": registry.state,
        "manifest_hash": registry.manifest_hash,
        "row_version": registry.row_version,
        "approved_by_id": (
            str(registry.approved_by_id) if registry.approved_by_id else None
        ),
    }


def _normalized_registry_import(data: dict) -> dict:
    sources = []
    for raw in data["sources"]:
        sources.append(
            {
                "key": raw["key"],
                "definition": {
                    "display_name": raw["displayName"],
                    "owner_name": raw["ownerName"],
                    "base_url": raw["baseUrl"],
                    "authority_tier": raw["authorityTier"],
                    "access_method": raw["accessMethod"],
                    "independence_group": raw["independenceGroup"],
                    "enabled": raw.get("enabled", True),
                },
                "config": {
                    "entrypoints": raw.get("entrypoints", []),
                    "allowedContentTypes": raw.get(
                        "allowedContentTypes", []
                    ),
                    "rightsStatus": raw.get(
                        "rightsStatus", "internal_analysis_only"
                    ),
                    "termsUrl": raw.get("termsUrl"),
                    "pollMinutes": raw.get("pollMinutes", 60),
                    "rateLimitPerMinute": raw.get(
                        "rateLimitPerMinute", 10
                    ),
                    "adapter": raw.get("adapter", "public_html"),
                },
            }
        )
    return {
        "schema_version": "source-registry-import-request-v1",
        "topic_code": data["topicCode"],
        "title": data["title"],
        "policy_version": int(data.get("policyVersion", 1)),
        "freshness_minutes": int(data.get("freshnessMinutes", 1440)),
        "policy": data.get("policy", {}),
        "sources": sources,
    }


def _source_definition_manifest_hash(sources) -> str:
    material = [
        {
            "id": str(source.id),
            "key": source.key,
            "display_name": source.display_name,
            "owner_name": source.owner_name,
            "base_url": source.base_url,
            "authority_tier": source.authority_tier,
            "access_method": source.access_method,
            "independence_group": source.independence_group,
            "enabled": source.enabled,
            "current_snapshot_version": source.current_snapshot_version,
        }
        for source in sorted(sources, key=lambda row: row.key)
    ]
    return canonical_hash(
        material,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _definitions_match_import(
    *,
    sources: list[SourceDefinition],
    normalized_sources: list[dict],
) -> bool:
    current = {source.key: source for source in sources}
    if any(item["key"] not in current for item in normalized_sources):
        return False
    return all(
        all(
            getattr(current[item["key"]], field) == value
            for field, value in item["definition"].items()
        )
        for item in normalized_sources
    )


def _registry_import_material(
    registry: SourceRegistrySnapshot,
    *,
    request_hash: str,
    source_definition_manifest_hash: str,
) -> dict:
    return {
        **_registry_material(registry),
        "request_hash": request_hash,
        "source_definition_manifest_hash": (
            source_definition_manifest_hash
        ),
    }


def _registry_approval_material(
    registry: SourceRegistrySnapshot,
    *,
    source_snapshots: list[SourceDefinitionSnapshot],
    retired_registries: list[SourceRegistrySnapshot],
) -> dict:
    source_manifest = [
        {
            "source_snapshot_id": str(snapshot.id),
            "config_hash": snapshot.config_hash,
            "state": snapshot.state,
        }
        for snapshot in sorted(source_snapshots, key=lambda row: str(row.id))
    ]
    retired_manifest = [
        {
            "registry_id": str(retired.id),
            "manifest_hash": retired.manifest_hash,
            "state": retired.state,
        }
        for retired in sorted(retired_registries, key=lambda row: str(row.id))
    ]
    return {
        **_registry_material(registry),
        "source_snapshot_count": len(source_manifest),
        "source_snapshot_manifest_hash": canonical_hash(
            source_manifest,
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        ),
        "retired_registry_count": len(retired_manifest),
        "retired_registry_manifest_hash": canonical_hash(
            retired_manifest,
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        ),
    }


def _audit_for_registry_request(
    *,
    alias: str,
    registry: SourceRegistrySnapshot,
    action: str,
    request_key: str,
) -> AuditEvent | None:
    return AuditEvent.objects.using(alias).filter(
        id=audit_event_id(
            action=action,
            entity=registry,
            identity_key=request_key,
        )
    ).first()


def import_registry_manifest(
    path: str | Path,
    *,
    audit_context: AuditContext,
) -> RegistryImportResult:
    if audit_context.actor_type != AuditEvent.ActorType.SYSTEM:
        raise ValueError(
            "source registry import requires explicit system provenance"
        )
    # Repository file I/O and JSON parsing happen before the transaction.
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    normalized = _normalized_registry_import(data)
    request_hash = canonical_hash(
        normalized,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    topic = normalized["topic_code"]
    policy_material = normalized["policy"]
    policy_hash = canonical_hash(
        policy_material, schema_version=CANONICAL_HASH_SCHEMA_V1
    )
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        current_sources = list(
            SourceDefinition.objects.using(alias)
            .select_for_update()
            .filter(topic_code=topic)
            .order_by("id")
        )
        current_by_key = {source.key: source for source in current_sources}
        existing_policy = TopicPolicy.objects.using(alias).filter(
            code=topic,
            version=normalized["policy_version"],
        ).first()
        import_events = AuditEvent.objects.using(alias).filter(
            action="source_registry.imported",
            entity_type=SourceRegistrySnapshot._meta.label_lower,
        )
        for event in import_events:
            if event.metadata_redacted.get("request_hash") != request_hash:
                continue
            registry = (
                SourceRegistrySnapshot.objects.using(alias)
                .prefetch_related(
                    "memberships__source_snapshot__source"
                )
                .filter(pk=event.entity_id, topic_code=topic)
                .first()
            )
            if registry is None or not _definitions_match_import(
                sources=current_sources,
                normalized_sources=normalized["sources"],
            ):
                continue
            if existing_policy is None or any(
                (
                    existing_policy.title != normalized["title"],
                    existing_policy.freshness_minutes
                    != normalized["freshness_minutes"],
                    existing_policy.policy != policy_material,
                    existing_policy.policy_hash != policy_hash,
                )
            ):
                continue
            memberships = list(registry.memberships.all())
            if len(memberships) != len(normalized["sources"]):
                continue
            expected_members = {
                item["key"]: {
                    "config_hash": canonical_hash(
                        item["config"],
                        schema_version=CANONICAL_HASH_SCHEMA_V1,
                    ),
                    "enabled": item["definition"]["enabled"],
                    "display_order": index,
                }
                for index, item in enumerate(normalized["sources"])
            }
            if all(
                member.source_snapshot.source.key in expected_members
                and member.source_snapshot.config_hash
                == expected_members[
                    member.source_snapshot.source.key
                ]["config_hash"]
                and member.enabled
                == expected_members[
                    member.source_snapshot.source.key
                ]["enabled"]
                and member.display_order
                == expected_members[
                    member.source_snapshot.source.key
                ]["display_order"]
                for member in memberships
            ):
                require_audit_replay(
                    context=audit_context,
                    action="source_registry.imported",
                    entity=registry,
                    event_id=event.id,
                    request_hash=request_hash,
                )
                return RegistryImportResult(
                    topic,
                    str(registry.id),
                    len(normalized["sources"]),
                    False,
                )

        before_definition_manifest_hash = _source_definition_manifest_hash(
            current_sources
        )
        policy, policy_created = TopicPolicy.objects.using(alias).get_or_create(
            code=topic,
            version=normalized["policy_version"],
            defaults={
                "title": normalized["title"],
                "freshness_minutes": normalized["freshness_minutes"],
                "policy": policy_material,
                "policy_hash": policy_hash,
            },
        )
        if not policy_created and any(
            (
                policy.title != normalized["title"],
                policy.freshness_minutes
                != normalized["freshness_minutes"],
                policy.policy != policy_material,
                policy.policy_hash != policy_hash,
            )
        ):
            raise ValueError(
                "the imported topic policy version already has different material"
            )

        snapshots: list[SourceDefinitionSnapshot] = []
        for item in normalized["sources"]:
            source = current_by_key.get(item["key"])
            if source is None:
                source = SourceDefinition.objects.using(alias).create(
                    topic_code=topic,
                    key=item["key"],
                    **item["definition"],
                )
                current_sources.append(source)
                current_by_key[source.key] = source
            changed_fields = [
                field
                for field, value in item["definition"].items()
                if getattr(source, field) != value
            ]
            for field in changed_fields:
                setattr(source, field, item["definition"][field])

            config = item["config"]
            config_hash = canonical_hash(
                config, schema_version=CANONICAL_HASH_SCHEMA_V1
            )
            snapshot = SourceDefinitionSnapshot.objects.using(alias).filter(
                source=source,
                config_hash=config_hash,
            ).first()
            if snapshot is None:
                latest_version = (
                    SourceDefinitionSnapshot.objects.using(alias)
                    .filter(source=source)
                    .order_by("-version")
                    .values_list("version", flat=True)
                    .first()
                )
                next_version = (latest_version or 0) + 1
                if source.current_snapshot_version != next_version:
                    source.current_snapshot_version = next_version
                    changed_fields.append("current_snapshot_version")
                snapshot = SourceDefinitionSnapshot.objects.using(
                    alias
                ).create(
                    source=source,
                    config_hash=config_hash,
                    version=next_version,
                    config=config,
                )
            if changed_fields:
                source.save(
                    update_fields=tuple(dict.fromkeys(
                        [*changed_fields, "updated_at"]
                    )),
                    using=alias,
                )
            snapshots.append(snapshot)

        manifest = [
            {
                "sourceSnapshotId": str(snapshot.id),
                "configHash": snapshot.config_hash,
                "enabled": snapshot.source.enabled,
            }
            for snapshot in sorted(
                snapshots, key=lambda row: str(row.source_id)
            )
        ]
        manifest_hash = canonical_hash(
            manifest, schema_version=CANONICAL_HASH_SCHEMA_V1
        )
        registry, created = SourceRegistrySnapshot.objects.using(
            alias
        ).get_or_create(
            topic_code=topic,
            manifest_hash=manifest_hash,
            defaults={
                "version": SourceRegistrySnapshot.objects.using(alias)
                .filter(topic_code=topic)
                .count()
                + 1
            },
        )
        if created:
            SourceRegistryMembership.objects.using(alias).bulk_create(
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
        after_definition_manifest_hash = _source_definition_manifest_hash(
            current_sources
        )
        record_audit_event(
            context=audit_context,
            action="source_registry.imported",
            entity=registry,
            identity_key=canonical_hash(
                {
                    "schema_version": "source-registry-import-identity-v1",
                    "registry_id": str(registry.id),
                    "request_hash": request_hash,
                    "before_source_definition_manifest_hash": (
                        before_definition_manifest_hash
                    ),
                },
                schema_version=CANONICAL_HASH_SCHEMA_V1,
            ),
            material_schema_version="source-registry-audit-v1",
            before_material={
                "schema_version": "source-registry-audit-v1",
                "topic_code": topic,
                "state": "before_import",
                "source_definition_manifest_hash": (
                    before_definition_manifest_hash
                ),
            },
            after_material=_registry_import_material(
                registry,
                request_hash=request_hash,
                source_definition_manifest_hash=(
                    after_definition_manifest_hash
                ),
            ),
            metadata={
                "request_hash": request_hash,
                "result": "imported",
                "count": len(snapshots),
                "manifest_hash": manifest_hash,
                "registry_snapshot_id": str(registry.id),
                "version": registry.version,
            },
        )
        return RegistryImportResult(
            topic, str(registry.id), len(snapshots), created
        )


def approve_registry(
    *,
    registry_id,
    admin,
    request,
    reauth_proof_id,
    audit_context: AuditContext,
) -> SourceRegistrySnapshot:
    if not audit_context.request_key:
        raise ValueError("request_key is required")
    if audit_context.reason_code is None:
        raise ValueError("reason is required")
    if str(admin.pk) != str(audit_context.actor_id):
        raise ValueError("audit actor does not match the approving administrator")

    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        registry = (
            SourceRegistrySnapshot.objects.using(alias)
            .select_for_update()
            .get(pk=registry_id)
        )
        request_hash = canonical_hash(
            {
                "schema_version": "source-registry-approval-request-v1",
                "registry_id": str(registry.id),
                "manifest_hash": registry.manifest_hash,
                "decision": "approved",
                "request_key": audit_context.request_key,
                "reason": audit_context.reason_code,
                "admin_id": str(admin.pk),
            },
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        )
        replay = _audit_for_registry_request(
            alias=alias,
            registry=registry,
            action="source_registry.approved",
            request_key=audit_context.request_key,
        )
        if replay is not None:
            require_audit_replay(
                context=audit_context,
                action="source_registry.approved",
                entity=registry,
                identity_key=audit_context.request_key,
                request_hash=request_hash,
            )
            return registry
        if registry.state != SourceRegistrySnapshot.State.DRAFT:
            raise ValueError("registry is no longer an approvable draft")

        source_ids = list(
            registry.memberships.values_list(
                "source_snapshot_id", flat=True
            )
        )
        source_snapshots = list(
            SourceDefinitionSnapshot.objects.using(alias)
            .select_for_update()
            .filter(id__in=source_ids)
            .order_by("id")
        )
        retired_registries = list(
            SourceRegistrySnapshot.objects.using(alias)
            .select_for_update()
            .filter(
                topic_code=registry.topic_code,
                state=SourceRegistrySnapshot.State.APPROVED,
            )
            .exclude(pk=registry.pk)
            .order_by("id")
        )
        before_material = _registry_approval_material(
            registry,
            source_snapshots=source_snapshots,
            retired_registries=retired_registries,
        )
        consume_reauthentication_proof(
            request=request,
            proof_id=reauth_proof_id,
            action_scope="registry_decision",
            entity_type="source_registry_snapshot",
            entity_id=registry.id,
        )
        now = timezone.now()
        approved_source_count = (
            SourceDefinitionSnapshot.objects.using(alias)
            .filter(
                id__in=source_ids,
                state=SourceDefinitionSnapshot.State.DRAFT,
            )
            .update(
                state=SourceDefinitionSnapshot.State.APPROVED,
                approved_by=admin,
                approved_at=now,
            )
        )
        (
            SourceRegistrySnapshot.objects.using(alias)
            .filter(
                id__in=[row.id for row in retired_registries],
            )
            .update(state=SourceRegistrySnapshot.State.RETIRED)
        )
        registry.state = SourceRegistrySnapshot.State.APPROVED
        registry.approved_by = admin
        registry.approved_at = now
        registry.save(
            update_fields=["state", "approved_by", "approved_at"],
            using=alias,
        )
        source_snapshots = list(
            SourceDefinitionSnapshot.objects.using(alias).filter(
                id__in=source_ids
            )
        )
        retired_registries = list(
            SourceRegistrySnapshot.objects.using(alias).filter(
                id__in=[row.id for row in retired_registries]
            )
        )
        record_audit_event(
            context=audit_context,
            action="source_registry.approved",
            entity=registry,
            identity_key=audit_context.request_key,
            material_schema_version="source-registry-audit-v1",
            before_material=before_material,
            after_material=_registry_approval_material(
                registry,
                source_snapshots=source_snapshots,
                retired_registries=retired_registries,
            ),
            metadata={
                "request_hash": request_hash,
                "decision": "approved",
                "count": approved_source_count,
                "manifest_hash": registry.manifest_hash,
                "registry_snapshot_id": str(registry.id),
                "version": registry.version,
                "reauth_proof_id": str(reauth_proof_id),
            },
        )
        return registry


def current_registry(topic_code: str) -> SourceRegistrySnapshot:
    return SourceRegistrySnapshot.objects.prefetch_related(
        "memberships__source_snapshot__source"
    ).get(topic_code=topic_code, state=SourceRegistrySnapshot.State.APPROVED)
