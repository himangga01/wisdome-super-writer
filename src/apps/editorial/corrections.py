from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.collection.models import (
    RunSourceItem,
    SourceItem,
    SourceItemStatus,
)
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)

from .models import CorrectionCase, DraftArticle


def _hash(value) -> str:
    return canonical_hash(
        value,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _create_cases_for_change(
    *,
    item: SourceItem,
    prior_item: SourceItem,
    evidence_item: SourceItem | None = None,
    article_ids=None,
    kind_override: str | None = None,
) -> list[CorrectionCase]:
    created_cases: list[CorrectionCase] = []
    if article_ids is None:
        article_ids = (
            DraftArticle.objects.filter(
                revisions__claims__evidence_links__evidence__source_item=(
                    evidence_item or prior_item
                )
            )
            .values_list("id", flat=True)
            .distinct()
        )
    kind = kind_override or {
        SourceItemStatus.RETRACTED: "retraction",
        SourceItemStatus.UNAVAILABLE: "source_unavailable",
    }.get(item.status, "correction")
    subject_hash = _hash(
        {
            "schemaVersion": "source-correction-subject-v1",
            "source": str(item.source_id),
            "externalId": item.external_id,
            "old": prior_item.source_version_hash,
            "new": item.source_version_hash,
            "status": item.status,
            "kind": kind,
        }
    )
    for article_id in article_ids:
        case, created = CorrectionCase.objects.get_or_create(
            article_id=article_id,
            subject_hash=subject_hash,
            defaults={
                "source_item": item,
                "prior_source_item": prior_item,
                "kind": kind,
                "diff_summary": {
                    "oldContentHash": prior_item.content_hash,
                    "newContentHash": item.content_hash,
                },
            },
        )
        if created:
            created_cases.append(case)
    return created_cases


def _prior_observations(
    observation: RunSourceItem,
) -> list[RunSourceItem]:
    observations: list[RunSourceItem] = []
    seen = {observation.id}
    previous_id = observation.previous_run_source_item_id
    while previous_id is not None:
        if previous_id in seen or len(observations) >= 10000:
            raise ValueError(
                "Source observation lineage is cyclic or exceeds its bound."
            )
        seen.add(previous_id)
        previous = (
            RunSourceItem.objects.select_related("source_item")
            .get(pk=previous_id)
        )
        if (
            previous.source_item.source_id
            != observation.source_item.source_id
            or previous.source_item.external_id
            != observation.source_item.external_id
        ):
            raise ValueError(
                "Source observation lineage crosses stable identity."
            )
        observations.append(previous)
        previous_id = previous.previous_run_source_item_id
    return observations


def _lineage_article_ids(
    prior_observations: list[RunSourceItem],
) -> list:
    evidence_item_ids = [
        prior.source_item_id
        for prior in prior_observations
        if prior.source_item.status
        in {
            SourceItemStatus.ACTIVE,
            SourceItemStatus.CORRECTED,
        }
    ]
    if not evidence_item_ids:
        return []
    return list(
        DraftArticle.objects.filter(
            revisions__claims__evidence_links__evidence__source_item_id__in=(
                evidence_item_ids
            )
        )
        .values_list("id", flat=True)
        .distinct()
    )


@transaction.atomic
def detect_correction_cases() -> list[CorrectionCase]:
    created_cases: list[CorrectionCase] = []
    changed = SourceItem.objects.filter(supersedes__isnull=False).select_related("supersedes")
    for item in changed:
        created_cases.extend(
            _create_cases_for_change(
                item=item,
                prior_item=item.supersedes,
            )
        )
    return created_cases


@transaction.atomic
def detect_correction_cases_for_observation(
    observation: RunSourceItem,
) -> list[CorrectionCase]:
    is_terminal = observation.source_item.status in {
        SourceItemStatus.RETRACTED,
        SourceItemStatus.UNAVAILABLE,
    }
    is_restored = (
        observation.discovery_kind == "restored"
        and observation.source_item.status
        == SourceItemStatus.ACTIVE
    )
    if not is_terminal and not is_restored:
        return []
    prior_observations = _prior_observations(observation)
    if not prior_observations:
        return []
    article_ids = _lineage_article_ids(prior_observations)
    if not article_ids:
        return []
    prior_item = prior_observations[0].source_item
    if is_restored:
        CorrectionCase.objects.filter(
            article_id__in=article_ids,
            source_item__source_id=observation.source_item.source_id,
            source_item__external_id=observation.source_item.external_id,
            kind__in={"retraction", "source_unavailable"},
            state__in={
                CorrectionCase.State.DETECTED,
                CorrectionCase.State.VERIFYING,
                CorrectionCase.State.VERIFIED,
            },
        ).update(
            state=CorrectionCase.State.REJECTED,
            completed_at=timezone.now(),
        )
    return _create_cases_for_change(
        item=observation.source_item,
        prior_item=prior_item,
        article_ids=article_ids,
        kind_override=("restoration" if is_restored else None),
    )
