from __future__ import annotations

import hashlib
import json

from django.db import transaction

from apps.collection.models import SourceItem

from .models import CorrectionCase, DraftArticle


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@transaction.atomic
def detect_correction_cases() -> list[CorrectionCase]:
    created_cases: list[CorrectionCase] = []
    changed = SourceItem.objects.filter(supersedes__isnull=False).select_related("supersedes")
    for item in changed:
        article_ids = (
            DraftArticle.objects.filter(
                current_revision__claims__evidence_links__evidence__source_item=item.supersedes
            )
            .values_list("id", flat=True)
            .distinct()
        )
        subject_hash = _hash(
            {
                "source": str(item.source_id),
                "old": item.supersedes.source_version_hash,
                "new": item.source_version_hash,
                "status": item.discovery_status,
            }
        )
        for article_id in article_ids:
            case, created = CorrectionCase.objects.get_or_create(
                article_id=article_id,
                subject_hash=subject_hash,
                defaults={
                    "source_item": item,
                    "prior_source_item": item.supersedes,
                    "kind": "retraction" if item.discovery_status == "retracted" else "correction",
                    "diff_summary": {
                        "oldContentHash": item.supersedes.content_hash,
                        "newContentHash": item.content_hash,
                    },
                },
            )
            if created:
                created_cases.append(case)
    return created_cases
