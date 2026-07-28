from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from adapters.generators import SourceGroundedTemplateGenerator
from adapters.generators.base import EvidenceInput
from apps.collection.models import CollectionRun, RunState
from apps.evidence.models import EvidenceAsset
from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash

from .models import ArticleRevision, Claim, ClaimEvidence, DraftArticle, GenerationAttempt, QualityCheck


def _article_type(topic_code: str) -> str:
    return "housing_notice" if topic_code == "housing_subscription" else "semiconductor_daily_digest"


@transaction.atomic
def build_source_grounded_draft(run: CollectionRun) -> DraftArticle:
    evidence_rows = list(
        EvidenceAsset.objects.filter(origin_run_source_item__run=run, publishable=True)
        .select_related("source_item")
        .order_by("source_item__published_at", "id")
    )
    inputs = [
        EvidenceInput(
            evidence_id=str(row.id),
            title=row.source_item.title,
            url=row.source_item.canonical_url,
            publisher=row.source_item.publisher,
            text=row.extracted_text or row.source_item.body_text,
            published_at=row.source_item.published_at.isoformat() if row.source_item.published_at else None,
        )
        for row in evidence_rows
    ]
    identity = canonical_hash(
        {"run": str(run.id), "topic": run.topic_code},
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    article, _ = DraftArticle.objects.get_or_create(
        article_identity_key=identity,
        defaults={"topic_code": run.topic_code, "article_type": _article_type(run.topic_code), "source_run": run},
    )
    if article.current_revision_id:
        return article
    input_hash = canonical_hash(
        [item.__dict__ for item in inputs],
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    attempt = GenerationAttempt.objects.create(article=article, input_manifest_hash=input_hash)
    try:
        generated = SourceGroundedTemplateGenerator().generate(
            topic=run.topic_code, evidence=inputs, article_type=article.article_type
        )
        claim_hash = canonical_hash(
            generated.claims, schema_version=CANONICAL_HASH_SCHEMA_V1
        )
        revision = ArticleRevision.objects.create(
            article=article,
            revision_no=1,
            generation_attempt=attempt,
            title=generated.title,
            summary=generated.summary,
            body_markdown=generated.body_markdown,
            input_manifest_hash=input_hash,
            claim_manifest_hash=claim_hash,
            quality_manifest_hash=canonical_hash(
                {"policy": "source-grounded-v1", "claims": claim_hash},
                schema_version=CANONICAL_HASH_SCHEMA_V1,
            ),
        )
        evidence_by_id = {str(row.id): row for row in evidence_rows}
        for position, raw in enumerate(generated.claims):
            claim = Claim.objects.create(
                revision=revision,
                claim_type=raw["claimType"],
                text=raw["text"],
                position=position,
                citation_marker=raw["citationMarker"],
            )
            ClaimEvidence.objects.create(claim=claim, evidence=evidence_by_id[raw["evidenceId"]])
        checks = {
            "all_fact_claims_sourced": all(claim.evidence_links.exists() for claim in revision.claims.all()),
            "has_source_section": "## 출처" in revision.body_markdown,
            "no_false_experience": not any(
                phrase in revision.body_markdown for phrase in ("직접 취재", "제가 경험", "관계자와 인터뷰")
            ),
        }
        for code, passed in checks.items():
            QualityCheck.objects.create(revision=revision, code=code, state="passed" if passed else "failed")
        revision.quality_state = "passed" if all(checks.values()) else "failed"
        revision.save(update_fields=["quality_state"])
        article.current_revision = revision
        article.state = "review_ready" if revision.quality_state == "passed" else "blocked"
        article.save(update_fields=["current_revision", "state", "updated_at"])
        attempt.state = "succeeded"
        attempt.finished_at = timezone.now()
        attempt.save(update_fields=["state", "finished_at"])
        run.state = RunState.AWAITING_APPROVAL
        run.save(update_fields=["state"])
        return article
    except Exception as exc:
        attempt.state = "failed"
        attempt.error_detail_redacted = str(exc)[:500]
        attempt.finished_at = timezone.now()
        attempt.save(update_fields=["state", "error_detail_redacted", "finished_at"])
        run.state = RunState.FAILED
        run.error_summary = {"stage": "drafting", "code": exc.__class__.__name__}
        run.save(update_fields=["state", "error_summary"])
        raise


@transaction.atomic
def create_manual_revision(article: DraftArticle, *, title: str, summary: str, body_markdown: str, user):
    previous = article.current_revision
    revision = ArticleRevision.objects.create(
        article=article,
        revision_no=(previous.revision_no if previous else 0) + 1,
        title=title,
        summary=summary,
        body_markdown=body_markdown,
        provenance_kind="admin_edit",
        input_manifest_hash=(
            previous.input_manifest_hash
            if previous
            else canonical_hash({}, schema_version=CANONICAL_HASH_SCHEMA_V1)
        ),
        claim_manifest_hash=canonical_hash(
            {"requiresRevalidation": True},
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        ),
        quality_manifest_hash=canonical_hash(
            {"policy": "manual-edit-v1"},
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        ),
        quality_state="pending",
        created_by=user,
    )
    article.current_revision = revision
    article.state = "draft"
    article.save(update_fields=["current_revision", "state", "updated_at"])
    return revision
