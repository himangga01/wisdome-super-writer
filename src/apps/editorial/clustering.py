from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.collection.models import CollectionRun, RunSourceItem, SourceItemStatus
from apps.evidence.models import EvidenceAsset
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)

from .models import EventCluster, EventClusterItem, EventClusterVerification


KST = ZoneInfo("Asia/Seoul")
BREAKING_CATEGORIES = {
    "regulation_export_control",
    "factory_supply_disruption",
    "merger_or_material_earnings",
    "critical_technology_or_mass_production",
}
_CATEGORY_ALIASES = {
    "government_control": "regulation_export_control",
    "factory_disruption": "factory_supply_disruption",
    "fab_supply_disruption": "factory_supply_disruption",
    "major_ma": "merger_or_material_earnings",
    "material_earnings": "merger_or_material_earnings",
    "ma_material_earnings": "merger_or_material_earnings",
    "technology_production": "critical_technology_or_mass_production",
    "core_technology_mass_production": "critical_technology_or_mass_production",
}
_PRIMARY_AUTHORITY_TIERS = {
    "primary_official",
    "primary_regulatory",
}
_TERMINAL_SOURCE_STATUSES = {
    SourceItemStatus.RETRACTED,
    SourceItemStatus.UNAVAILABLE,
}


def _hash(material) -> str:
    return canonical_hash(
        material,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _metadata(item: RunSourceItem) -> dict:
    value = item.source_item.metadata
    return value if isinstance(value, dict) else {}


def _first_text(material: dict, *keys: str) -> str | None:
    for key in keys:
        value = material.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _correction_id(item: RunSourceItem) -> str | None:
    metadata = _metadata(item)
    correction_id = _first_text(
        metadata,
        "correction_id",
        "correctionId",
        "correctionDocumentId",
    )
    if correction_id:
        return correction_id
    return None


def _housing_identity_material(item: RunSourceItem) -> dict[str, str | None]:
    return {
        "authority": item.source_item.publisher.strip(),
        "noticeId": item.source_item.external_id.strip(),
        "correctionId": _correction_id(item),
    }


def housing_cluster_key(item: RunSourceItem) -> str:
    return _hash(
        {
            "schemaVersion": "housing-event-key-v1",
            "authority": item.source_item.publisher.strip(),
            "noticeId": item.source_item.external_id.strip(),
        }
    )


def _event_date(item: RunSourceItem) -> date:
    metadata = _metadata(item)
    raw = _first_text(metadata, "eventDate", "event_date", "officialDate")
    if raw:
        if len(raw) == 10:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if timezone.is_naive(parsed):
                parsed = parsed.replace(tzinfo=KST)
            return parsed.astimezone(KST).date()
        except ValueError:
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                pass
    occurred_at = item.source_item.published_at or item.discovered_at
    if timezone.is_naive(occurred_at):
        occurred_at = occurred_at.replace(tzinfo=KST)
    return occurred_at.astimezone(KST).date()


def _semiconductor_identity_material(item: RunSourceItem) -> dict[str, str]:
    metadata = _metadata(item)
    source_context = metadata.get("sourceContext")
    source_context = source_context if isinstance(source_context, dict) else {}
    content_kinds = metadata.get("contentKinds")
    fallback_action = (
        str(content_kinds[0])
        if isinstance(content_kinds, list) and content_kinds
        else "announcement"
    )
    subject = _first_text(
        metadata,
        "eventSubject",
        "subject",
        "issuer",
        "company",
    ) or _first_text(source_context, "ownerName", "publisher")
    action = _first_text(
        metadata,
        "eventAction",
        "action",
    ) or fallback_action
    announcement_id = _first_text(
        metadata,
        "officialAnnouncementId",
        "announcementId",
    ) or item.source_item.external_id
    return {
        "subject": subject or item.source_item.publisher,
        "action": action,
        "officialAnnouncementId": announcement_id,
        "eventDateKst": _event_date(item).isoformat(),
    }


def _semiconductor_semantics(
    item: RunSourceItem,
) -> tuple[dict[str, str], list[str]]:
    metadata = _metadata(item)
    required = {
        "subject": "eventSubject",
        "action": "eventAction",
        "officialAnnouncementId": "officialAnnouncementId",
        "breakingCategory": "breakingCategory",
    }
    values: dict[str, str] = {}
    missing: list[str] = []
    for normalized, source_key in required.items():
        value = metadata.get(source_key)
        if isinstance(value, str) and value.strip():
            values[normalized] = value.strip()
        else:
            missing.append(source_key)
    return values, missing


def semiconductor_cluster_key(item: RunSourceItem) -> str:
    return _hash(
        {
            "schemaVersion": "semiconductor-event-key-v1",
            **_semiconductor_identity_material(item),
        }
    )


def _origin_identity(item: RunSourceItem) -> str:
    metadata = _metadata(item)
    return _first_text(
        metadata,
        "originIdentity",
        "syndicationOriginIdentity",
    ) or item.source_item.external_id


def _authority_tier(member: EventClusterItem) -> str:
    material = member.run_source_item.source_snapshot.frozen_config
    if not isinstance(material, dict):
        return ""
    return str(material.get("authorityTier") or "")


def _housing_priority(member: EventClusterItem) -> tuple[int, str, str]:
    item = member.run_source_item
    metadata = _metadata(item)
    structured = metadata.get("structured")
    structured = structured if isinstance(structured, dict) else {}
    has_detail = bool(
        structured.get("detailPage")
        or (
            isinstance(structured.get("api"), dict)
            and structured["api"].get("detailPage")
        )
    )
    attachments = item.source_item.attachments
    has_document = isinstance(attachments, list) and any(
        isinstance(row, dict)
        and str(row.get("mime_type") or row.get("mimeType") or "").lower()
        in {
            "application/pdf",
            "application/haansofthwp",
            "application/x-hwp",
            "application/vnd.hancom.hwp",
            "application/hwp+zip",
            "application/vnd.hancom.hwpx",
        }
        for row in attachments
    )
    access_method = ""
    frozen = item.source_snapshot.frozen_config
    if isinstance(frozen, dict):
        access_method = str(frozen.get("accessMethod") or "")
    if _correction_id(item) and has_document:
        priority = 4
        code = "latest_correction_document"
    elif has_detail:
        priority = 3
        code = "authority_detail"
    elif access_method in {"public_api", "open_data_api"}:
        priority = 2
        code = "structured_api"
    else:
        priority = 1
        code = "aggregator"
    occurred = item.source_item.modified_at or item.source_item.published_at
    return priority, occurred.isoformat() if occurred else "", code


def _normalized_category(member: EventClusterItem) -> str:
    semantics, missing = _semiconductor_semantics(member.run_source_item)
    if missing:
        return "uncategorized"
    category = semantics["breakingCategory"]
    return _CATEGORY_ALIASES.get(category, category)


def breaking_decision(
    *,
    category: str,
    primary_count: int,
    independent_origin_count: int,
) -> str:
    if category not in BREAKING_CATEGORIES:
        return "daily_digest_candidate"
    if primary_count >= 1 or independent_origin_count >= 2:
        return "verified_breaking"
    return "held"


def cluster_run_items(
    run_id,
    *,
    using: str = "default",
) -> list[EventCluster]:
    with transaction.atomic(using=using):
        run = (
            CollectionRun.objects.using(using)
            .select_for_update()
            .get(pk=run_id)
        )
        items = list(
            RunSourceItem.objects.using(using)
            .filter(
                run=run,
                collection_attempt__state="succeeded",
            )
            .select_related(
                "source_item",
                "source_snapshot",
                "collection_attempt",
            )
            .order_by("discovered_at", "id")
        )
        clusters: dict[str, EventCluster] = {}
        keyed_items: list[tuple[str, RunSourceItem]] = []
        for item in items:
            if run.topic_code == "housing_subscription":
                canonical_key = housing_cluster_key(item)
            elif run.topic_code == "semiconductor_news":
                canonical_key = semiconductor_cluster_key(item)
            else:
                raise ValueError("unsupported editorial clustering topic")
            keyed_items.append((canonical_key, item))
        for canonical_key, item in sorted(
            keyed_items,
            key=lambda row: (
                row[0],
                row[1].discovered_at,
                str(row[1].id),
            ),
        ):
            cluster, _ = EventCluster.objects.using(using).get_or_create(
                topic_code=run.topic_code,
                canonical_key=canonical_key,
                defaults={
                    "title": item.source_item.title,
                    "verification_state": "candidate",
                    "source_item_ids": [str(item.source_item_id)],
                },
            )
            cluster = (
                EventCluster.objects.using(using)
                .select_for_update()
                .get(pk=cluster.pk)
            )
            source_item_ids = set(cluster.source_item_ids or [])
            source_item_ids.add(str(item.source_item_id))
            ordered_source_item_ids = sorted(source_item_ids)
            if cluster.source_item_ids != ordered_source_item_ids:
                cluster.source_item_ids = ordered_source_item_ids
                cluster.save(update_fields=["source_item_ids"], using=using)
            EventClusterItem.objects.using(using).get_or_create(
                cluster=cluster,
                run_source_item=item,
                defaults={
                    "origin_identity_hash": _hash(
                        {
                            "schemaVersion": "origin-identity-v1",
                            "identity": _origin_identity(item),
                        }
                    ),
                    "independence_group": item.source_snapshot.independence_group[:120],
                    "role": "candidate",
                    "selection_state": "candidate",
                    "decision_reason": "awaiting_verification",
                },
            )
            clusters[str(cluster.id)] = cluster
        return sorted(clusters.values(), key=lambda row: row.canonical_key)


def _select_members(
    cluster: EventCluster,
    members: list[EventClusterItem],
    *,
    using: str,
) -> dict[str, str | None]:
    if cluster.topic_code == "housing_subscription":
        latest_member = max(
            members,
            key=lambda row: (
                row.run_source_item.run.created_at,
                str(row.run_source_item.run_id),
                row.run_source_item.discovered_at,
                str(row.run_source_item_id),
            ),
        )
        terminal_head = (
            latest_member.run_source_item.source_item.status
            if latest_member.run_source_item.source_item.status
            in _TERMINAL_SOURCE_STATUSES
            else None
        )
        if terminal_head:
            for member in members:
                status = member.run_source_item.source_item.status
                member.role = "terminal" if status in _TERMINAL_SOURCE_STATUSES else "historical"
                member.selection_state = "excluded"
                member.decision_reason = (
                    f"excluded_source_{status}"
                    if status in _TERMINAL_SOURCE_STATUSES
                    else "excluded_terminal_lineage_head"
                )
                member.save(
                    update_fields=["role", "selection_state", "decision_reason"],
                    using=using,
                )
            return {
                "terminal_head": str(terminal_head),
                "identity_error": None,
            }
        if (
            latest_member.run_source_item.source_item.status
            == SourceItemStatus.CORRECTED
            and _correction_id(latest_member.run_source_item) is None
        ):
            for member in members:
                member.role = (
                    "correction_head"
                    if member.pk == latest_member.pk
                    else "historical"
                )
                member.selection_state = "excluded"
                member.decision_reason = (
                    "excluded_correction_identity_missing"
                    if member.pk == latest_member.pk
                    else "excluded_unidentified_correction_head"
                )
                member.save(
                    update_fields=["role", "selection_state", "decision_reason"],
                    using=using,
                )
            return {
                "terminal_head": None,
                "identity_error": "official_correction_identity_missing",
            }
        eligible = [
            row
            for row in members
            if row.run_source_item.source_item.status
            not in _TERMINAL_SOURCE_STATUSES
        ]
        ranked = sorted(
            eligible,
            key=lambda row: (
                _housing_priority(row)[0],
                _housing_priority(row)[1],
                str(row.run_source_item_id),
            ),
            reverse=True,
        )
        if not ranked:
            return {
                "terminal_head": "no_active_member",
                "identity_error": None,
            }
        selected = ranked[0]
        selected_hash = selected.run_source_item.source_item.source_version_hash
        for member in members:
            status = member.run_source_item.source_item.status
            if status in _TERMINAL_SOURCE_STATUSES:
                member.role = "terminal"
                member.selection_state = "excluded"
                member.decision_reason = f"excluded_source_{status}"
                member.save(
                    update_fields=["role", "selection_state", "decision_reason"],
                    using=using,
                )
                continue
            _, _, priority_code = _housing_priority(member)
            if member.pk == selected.pk:
                role = "primary"
                state = "selected"
                reason = f"selected_{priority_code}"
            elif member.origin_identity_hash == selected.origin_identity_hash:
                role = "duplicate"
                state = "excluded"
                reason = "excluded_duplicate_origin"
            elif member.run_source_item.source_item.source_version_hash != selected_hash:
                role = "conflicting"
                state = "conflicting"
                reason = f"conflict_lower_priority_{priority_code}"
            else:
                role = "supporting"
                state = "included"
                reason = f"included_matching_{priority_code}"
            member.role = role
            member.selection_state = state
            member.decision_reason = reason
            member.save(
                update_fields=["role", "selection_state", "decision_reason"],
                using=using,
            )
        return {"terminal_head": None, "identity_error": None}

    seen_origins: set[str] = set()
    ranked = sorted(
        (
            row
            for row in members
            if row.run_source_item.source_item.status
            not in _TERMINAL_SOURCE_STATUSES
        ),
        key=lambda row: (
            _authority_tier(row) in _PRIMARY_AUTHORITY_TIERS,
            row.run_source_item.source_item.published_at
            or row.run_source_item.discovered_at,
            str(row.run_source_item_id),
        ),
        reverse=True,
    )
    for member in members:
        status = member.run_source_item.source_item.status
        if status in _TERMINAL_SOURCE_STATUSES:
            member.role = "terminal"
            member.selection_state = "excluded"
            member.decision_reason = f"excluded_source_{status}"
            member.save(
                update_fields=["role", "selection_state", "decision_reason"],
                using=using,
            )
    for member in ranked:
        if member.origin_identity_hash in seen_origins:
            member.role = "duplicate"
            member.selection_state = "excluded"
            member.decision_reason = "excluded_duplicate_syndication_origin"
        else:
            seen_origins.add(member.origin_identity_hash)
            _, semantic_missing = _semiconductor_semantics(
                member.run_source_item
            )
            member.role = (
                "primary"
                if _authority_tier(member) in _PRIMARY_AUTHORITY_TIERS
                else "supporting"
            )
            member.selection_state = "selected"
            member.decision_reason = (
                "selected_independent_origin"
                if not semantic_missing
                else "selected_semantic_fields_incomplete"
            )
        member.save(
            update_fields=["role", "selection_state", "decision_reason"],
            using=using,
        )
    return {
        "terminal_head": (
            "all_members_terminal" if not ranked else None
        ),
        "identity_error": None,
    }


def _independence_counts(
    members: list[EventClusterItem],
    *,
    breaking_category: str | None = None,
) -> tuple[int, int]:
    included = [
        row
        for row in members
        if row.selection_state in {"selected", "included"}
        and (
            breaking_category is None
            or (
                not _semiconductor_semantics(row.run_source_item)[1]
                and _normalized_category(row) == breaking_category
            )
        )
    ]
    parents = list(range(len(included)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left in range(len(included)):
        for right in range(left + 1, len(included)):
            if (
                included[left].independence_group
                == included[right].independence_group
                or included[left].origin_identity_hash
                == included[right].origin_identity_hash
            ):
                union(left, right)
    groups: dict[int, list[EventClusterItem]] = {}
    for index, member in enumerate(included):
        groups.setdefault(find(index), []).append(member)
    primary_count = sum(
        any(_authority_tier(member) in _PRIMARY_AUTHORITY_TIERS for member in group)
        for group in groups.values()
    )
    return primary_count, len(groups)


def _evidence_manifest(
    members: list[EventClusterItem],
    *,
    using: str,
    breaking_category: str | None = None,
) -> list[dict]:
    evidence_by_run_item: dict[str, list[dict]] = {}
    evidence_rows = (
        EvidenceAsset.objects.using(using)
        .filter(origin_run_source_item_id__in=[row.run_source_item_id for row in members])
        .order_by("origin_run_source_item_id", "id")
    )
    for evidence in evidence_rows:
        evidence_by_run_item.setdefault(
            str(evidence.origin_run_source_item_id), []
        ).append(
            {
                "evidenceId": str(evidence.id),
                "contentHash": evidence.evidence_content_hash,
                "checksum": evidence.checksum,
                "reviewSubjectHash": evidence.review_subject_hash,
                "publishable": evidence.publishable,
            }
        )
    manifest = []
    for member in sorted(members, key=lambda row: str(row.run_source_item_id)):
        item = member.run_source_item
        semantic_values, semantic_missing = (
            _semiconductor_semantics(item)
            if item.run.topic_code == "semiconductor_news"
            else ({}, [])
        )
        identity = (
            _housing_identity_material(item)
            if item.run.topic_code == "housing_subscription"
            else _semiconductor_identity_material(item)
        )
        manifest.append(
            {
                "runSourceItemId": str(item.id),
                "sourceItemId": str(item.source_item_id),
                "sourceSnapshotId": str(item.source_snapshot_id),
                "sourceSnapshotHash": item.source_snapshot.frozen_config_hash,
                "runId": str(item.run_id),
                "sourceStatus": item.source_item.status,
                "identity": identity,
                "semanticFields": semantic_values,
                "semanticComplete": not semantic_missing,
                "semanticMissingFields": semantic_missing,
                "breakingThresholdEligible": bool(
                    item.run.topic_code == "semiconductor_news"
                    and member.selection_state in {"selected", "included"}
                    and not semantic_missing
                    and breaking_category is not None
                    and _normalized_category(member) == breaking_category
                ),
                "originIdentityHash": member.origin_identity_hash,
                "independenceGroup": member.independence_group,
                "authorityTier": _authority_tier(member),
                "role": member.role,
                "selectionState": member.selection_state,
                "decisionReason": member.decision_reason,
                "evidence": evidence_by_run_item.get(str(item.id), []),
            }
        )
    return manifest


def verify_event_cluster(
    cluster_id,
    *,
    run_id=None,
    using: str = "default",
) -> EventClusterVerification:
    with transaction.atomic(using=using):
        cluster = (
            EventCluster.objects.using(using)
            .select_for_update()
            .get(pk=cluster_id)
        )
        if run_id is None:
            target_run = None
        else:
            target_run = (
                CollectionRun.objects.using(using)
                .select_for_update()
                .get(pk=run_id)
            )
            existing = (
                EventClusterVerification.objects.using(using)
                .filter(cluster=cluster, origin_run=target_run)
                .first()
            )
            if existing is not None:
                return existing
        latest = (
            EventClusterVerification.objects.using(using)
            .filter(cluster=cluster)
            .select_related("origin_run")
            .order_by("-version")
            .first()
        )
        if target_run is not None and latest is not None:
            target_causal_key = (target_run.created_at, str(target_run.id))
            head_causal_key = (
                latest.origin_run.created_at,
                str(latest.origin_run_id),
            )
            if target_causal_key < head_causal_key:
                return latest
        all_members = list(
            EventClusterItem.objects.using(using)
            .filter(cluster=cluster)
            .select_related(
                "run_source_item__run__topic_policy",
                "run_source_item__source_item",
                "run_source_item__source_snapshot",
            )
            .order_by("run_source_item__discovered_at", "run_source_item_id")
        )
        if not all_members:
            raise ValueError("event cluster has no run-source members")
        if target_run is None:
            target_run = max(
                (row.run_source_item.run for row in all_members),
                key=lambda row: (row.created_at, str(row.id)),
            )
            existing = (
                EventClusterVerification.objects.using(using)
                .filter(cluster=cluster, origin_run=target_run)
                .first()
            )
            if existing is not None:
                return existing
        target_causal_key = (target_run.created_at, str(target_run.id))
        members = [
            row
            for row in all_members
            if (
                row.run_source_item.run.created_at,
                str(row.run_source_item.run_id),
            )
            <= target_causal_key
        ]
        if not any(row.run_source_item.run_id == target_run.id for row in members):
            raise ValueError("verification run is not a member of the event cluster")
        selection_result = _select_members(cluster, members, using=using)
        run = target_run
        if cluster.topic_code == "housing_subscription":
            primary_count, independent_count = _independence_counts(members)
            if selection_result["identity_error"]:
                category = "housing_correction"
                article_type = "housing_correction"
                decision = "rejected"
                decision_reason = selection_result["identity_error"]
            elif selection_result["terminal_head"]:
                category = "housing_notice"
                article_type = "housing_notice"
                decision = "rejected"
                decision_reason = (
                    "official_notice_lineage_"
                    f"{selection_result['terminal_head']}"
                )
            else:
                selected = next(
                    row for row in members if row.selection_state == "selected"
                )
                correction_id = _correction_id(selected.run_source_item)
                category = "housing_correction" if correction_id else "housing_notice"
                article_type = category
                decision = "verified_notice"
                decision_reason = "official_notice_identity_verified"
        else:
            category_member = next(
                (
                    row
                    for row in sorted(
                        members,
                        key=lambda candidate: (
                            not _semiconductor_semantics(
                                candidate.run_source_item
                            )[1],
                            candidate.role == "primary",
                            candidate.run_source_item.source_item.published_at
                            or candidate.run_source_item.discovered_at,
                        ),
                        reverse=True,
                    )
                    if row.selection_state in {"selected", "included"}
                ),
                None,
            )
            if category_member is None:
                category = "uncategorized"
                primary_count = 0
                independent_count = 0
                decision = "rejected"
                article_type = "semiconductor_daily_digest"
                decision_reason = "all_semiconductor_members_terminal"
            else:
                _, semantic_missing = _semiconductor_semantics(
                    category_member.run_source_item
                )
                category = _normalized_category(category_member)
                if semantic_missing:
                    primary_count = 0
                    independent_count = 0
                    decision = "daily_digest_candidate"
                    decision_reason = (
                        "semantic_fields_incomplete:"
                        + ",".join(semantic_missing)
                    )
                else:
                    primary_count, independent_count = _independence_counts(
                        members,
                        breaking_category=category,
                    )
                    decision = breaking_decision(
                        category=category,
                        primary_count=primary_count,
                        independent_origin_count=independent_count,
                    )
                    decision_reason = {
                        "verified_breaking": "primary_or_two_independent_origins",
                        "held": "breaking_source_threshold_not_met",
                        "daily_digest_candidate": "non_breaking_category",
                    }[decision]
                article_type = (
                    "semiconductor_breaking"
                    if decision == "verified_breaking"
                    else "semiconductor_daily_digest"
                )
        manifest = _evidence_manifest(
            members,
            using=using,
            breaking_category=(
                category if cluster.topic_code == "semiconductor_news" else None
            ),
        )
        manifest_hash = _hash(
            {
                "schemaVersion": "event-cluster-evidence-manifest-v1",
                "clusterId": str(cluster.id),
                "members": manifest,
            }
        )
        conflict_manifest = [
            {
                "runSourceItemId": row["runSourceItemId"],
                "reason": row["decisionReason"],
            }
            for row in manifest
            if row["selectionState"] == "conflicting"
        ]
        excluded_source_manifest = [
            {
                "runSourceItemId": row["runSourceItemId"],
                "sourceStatus": row["sourceStatus"],
                "reason": row["decisionReason"],
            }
            for row in manifest
            if row["selectionState"] == "excluded"
        ]
        rule_manifest_hash = _hash(
            {
                "schemaVersion": "event-cluster-verification-rule-v1",
                "breakingCategories": sorted(BREAKING_CATEGORIES),
                "breakingThreshold": {
                    "primarySourceCount": 1,
                    "independentOriginCount": 2,
                    "requiresSemanticComplete": True,
                    "requiresMatchingCategory": True,
                },
                "housingConflictPriority": [
                    "latest_correction_document",
                    "authority_detail",
                    "structured_api",
                    "aggregator",
                ],
                "primaryCorporateSelfClaimCounts": False,
                "missingOfficialCorrectionIdentity": "rejected",
                "terminalStatuses": sorted(_TERMINAL_SOURCE_STATUSES),
            }
        )
        immutable_result = {
            "decision": decision,
            "article_type": article_type,
            "category": category,
            "primary_source_count": primary_count,
            "independent_origin_count": independent_count,
            "decision_reason": decision_reason,
            "policy_version": str(run.policy_version),
            "policy_hash": run.policy_hash,
            "local_event_date": timezone.localtime(
                run.window_end,
                KST,
            ).date(),
            "evidence_manifest_hash": manifest_hash,
            "rule_manifest_hash": rule_manifest_hash,
        }
        result_manifest_hash = _hash(
            {
                "schemaVersion": "event-cluster-verification-result-v1",
                **{
                    key: (
                        value.isoformat() if isinstance(value, date) else value
                    )
                    for key, value in immutable_result.items()
                },
                "conflicts": conflict_manifest,
                "excludedSources": excluded_source_manifest,
            }
        )
        verification = EventClusterVerification.objects.using(using).create(
            cluster=cluster,
            origin_run=run,
            version=(latest.version + 1 if latest else 1),
            evidence_manifest=manifest,
            conflict_manifest=conflict_manifest,
            excluded_source_manifest=excluded_source_manifest,
            result_manifest_hash=result_manifest_hash,
            supersedes=latest,
            **immutable_result,
        )
        cluster.verification_state = verification.decision
        cluster.save(update_fields=["verification_state"], using=using)
        return verification


def generation_manifest_for_verifications(
    *,
    run: CollectionRun,
    primary_verification: EventClusterVerification,
    verifications: list[EventClusterVerification],
) -> tuple[list[str], str, dict]:
    if not verifications:
        raise ValueError("generation manifest requires at least one verification")
    ordered = sorted(verifications, key=lambda row: str(row.id))
    verification_ids = [str(row.id) for row in ordered]
    if len(verification_ids) != len(set(verification_ids)):
        raise ValueError("generation manifest contains duplicate verifications")
    if len({row.cluster_id for row in ordered}) != len(ordered):
        raise ValueError("generation manifest contains duplicate clusters")
    if any(row.origin_run_id != run.id for row in ordered):
        raise ValueError("generation verification does not belong to its run")
    if str(primary_verification.id) not in verification_ids:
        raise ValueError("primary verification is absent from generation manifest")
    if any(row.cluster.topic_code != run.topic_code for row in ordered):
        raise ValueError("generation verification topic does not match its run")
    expected_local_date = timezone.localtime(run.window_end, KST).date()
    if any(
        row.policy_version != str(run.policy_version)
        or row.policy_hash != run.policy_hash
        or row.local_event_date != expected_local_date
        for row in ordered
    ):
        raise ValueError("generation verification policy or date fence is stale")
    if primary_verification.decision in {
        "verified_notice",
        "verified_breaking",
    }:
        if len(ordered) != 1:
            raise ValueError(
                "notice and breaking generation require one verification"
            )
    elif primary_verification.decision in {
        "daily_digest_candidate",
        "held",
    }:
        if any(
            row.decision not in {"daily_digest_candidate", "held"}
            for row in ordered
        ):
            raise ValueError(
                "daily digest generation contains an ineligible decision"
            )
    else:
        raise ValueError("primary verification is not eligible for generation")
    material = {
        "schemaVersion": "editorial-generation-manifest-v1",
        "runId": str(run.id),
        "primaryVerificationId": str(primary_verification.id),
        "verifications": [
            {
                "verificationId": str(row.id),
                "clusterId": str(row.cluster_id),
                "decision": row.decision,
                "evidenceManifestHash": row.evidence_manifest_hash,
                "ruleManifestHash": row.rule_manifest_hash,
                "resultManifestHash": row.result_manifest_hash,
                "policyVersion": row.policy_version,
                "policyHash": row.policy_hash,
                "localEventDate": row.local_event_date.isoformat(),
            }
            for row in ordered
        ],
    }
    return verification_ids, _hash(material), material


def article_identity_for_verification(
    verification: EventClusterVerification,
) -> str:
    if verification.cluster.topic_code == "housing_subscription":
        selected = next(
            (
                row
                for row in verification.evidence_manifest
                if row.get("selectionState") == "selected"
            ),
            None,
        )
        if selected is None:
            raise ValueError("housing verification has no selected notice identity")
        return _hash(
            {
                "schemaVersion": "housing-article-identity-v1",
                **selected["identity"],
            }
        )
    if verification.decision == "verified_breaking":
        return verification.cluster.canonical_key
    return _hash(
        {
            "schemaVersion": "semiconductor-daily-digest-identity-v1",
            "localDateKst": verification.local_event_date.isoformat(),
            "policyVersion": verification.policy_version,
        }
    )
