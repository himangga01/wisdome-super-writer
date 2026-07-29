from __future__ import annotations

import uuid

from django.db import transaction
from django.utils import timezone

from adapters.generators import SourceGroundedTemplateGenerator
from adapters.generators.base import EvidenceInput
from apps.audit.models import AuditEvent
from apps.audit.services import (
    AuditContext,
    record_audit_event,
    require_audit_replay,
    require_worker_event,
)
from apps.collection.models import CollectionRun, RunState
from apps.evidence.models import EvidenceAsset
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)

from .models import (
    ArticleRevision,
    Claim,
    ClaimEvidence,
    DraftArticle,
    GenerationAttempt,
    QualityCheck,
)


_MANUAL_REVISION_NAMESPACE = uuid.UUID(
    "28968123-3e2c-4123-b754-983470383ba2"
)


def _article_type(topic_code: str) -> str:
    return (
        "housing_notice"
        if topic_code == "housing_subscription"
        else "semiconductor_daily_digest"
    )


def _revision_content_hash(
    *,
    title: str,
    summary: str,
    body_markdown: str,
) -> str:
    return canonical_hash(
        {
            "schema_version": "article-revision-content-v1",
            "title": title,
            "summary": summary,
            "body_markdown": body_markdown,
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _article_material(
    article: DraftArticle,
    revision: ArticleRevision | None,
) -> dict:
    return {
        "schema_version": "article-audit-v1",
        "article_id": str(article.id),
        "article_identity_key": article.article_identity_key,
        "source_run_id": str(article.source_run_id),
        "state": article.state,
        "current_revision_id": (
            str(article.current_revision_id)
            if article.current_revision_id
            else None
        ),
        "revision_no": revision.revision_no if revision else None,
        "revision_content_hash": (
            _revision_content_hash(
                title=revision.title,
                summary=revision.summary,
                body_markdown=revision.body_markdown,
            )
            if revision
            else None
        ),
        "input_manifest_hash": (
            revision.input_manifest_hash if revision else None
        ),
        "claim_manifest_hash": (
            revision.claim_manifest_hash if revision else None
        ),
        "quality_manifest_hash": (
            revision.quality_manifest_hash if revision else None
        ),
        "quality_state": revision.quality_state if revision else None,
    }


def _manual_revision_id(
    *,
    article_id,
    audit_context: AuditContext,
) -> uuid.UUID:
    identity_hash = canonical_hash(
        {
            "schema_version": "manual-revision-identity-v1",
            "article_id": str(article_id),
            "request_key": audit_context.request_key,
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    return uuid.uuid5(_MANUAL_REVISION_NAMESPACE, identity_hash)


def build_source_grounded_draft(
    run: CollectionRun,
    *,
    audit_context: AuditContext,
) -> DraftArticle:
    if audit_context.actor_type != AuditEvent.ActorType.WORKER:
        raise ValueError("draft generation requires worker audit provenance")
    if not audit_context.event_key:
        raise ValueError("worker event key is required")
    alias = audit_context.database_alias
    if run._state.db != alias:
        raise ValueError("collection run and audit database aliases differ")

    with transaction.atomic(using=alias):
        run = (
            CollectionRun.objects.using(alias)
            .select_for_update()
            .get(pk=run.pk)
        )
        require_worker_event(
            context=audit_context,
            topic="run.draft_requested",
            aggregate_id=run.id,
            payload_identity={"run_id": str(run.id)},
        )
        identity = canonical_hash(
            {"run": str(run.id), "topic": run.topic_code},
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        )
        article = (
            DraftArticle.objects.using(alias)
            .filter(article_identity_key=identity)
            .first()
        )
        if article is not None and article.current_revision_id:
            revision = ArticleRevision.objects.using(alias).get(
                pk=article.current_revision_id
            )
            require_audit_replay(
                context=audit_context,
                action="article.draft_generated",
                entity=revision,
                identity_key=audit_context.event_key,
            )
            return article
        if run.state not in {RunState.VALIDATING, RunState.DRAFTING}:
            raise ValueError(
                "collection run is not eligible for draft generation"
            )
        run_before_state = run.state
        if run.state == RunState.VALIDATING:
            run.state = RunState.DRAFTING
            run.save(update_fields=["state"], using=alias)
        evidence_rows = list(
            EvidenceAsset.objects.using(alias)
            .filter(
                origin_run_source_item__run=run,
                publishable=True,
            )
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
                published_at=(
                    row.source_item.published_at.isoformat()
                    if row.source_item.published_at
                    else None
                ),
            )
            for row in evidence_rows
        ]
        if article is None:
            article, _ = DraftArticle.objects.using(alias).get_or_create(
                article_identity_key=identity,
                defaults={
                    "topic_code": run.topic_code,
                    "article_type": _article_type(run.topic_code),
                    "source_run": run,
                },
            )

        before_material = _article_material(article, None)
        input_hash = canonical_hash(
            [item.__dict__ for item in inputs],
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        )
        attempt = GenerationAttempt.objects.using(alias).create(
            article=article,
            input_manifest_hash=input_hash,
        )
        try:
            generated = SourceGroundedTemplateGenerator().generate(
                topic=run.topic_code,
                evidence=inputs,
                article_type=article.article_type,
            )
            claim_hash = canonical_hash(
                generated.claims,
                schema_version=CANONICAL_HASH_SCHEMA_V1,
            )
            revision = ArticleRevision.objects.using(alias).create(
                article=article,
                revision_no=1,
                generation_attempt=attempt,
                title=generated.title,
                summary=generated.summary,
                body_markdown=generated.body_markdown,
                input_manifest_hash=input_hash,
                claim_manifest_hash=claim_hash,
                quality_manifest_hash=canonical_hash(
                    {
                        "policy": "source-grounded-v1",
                        "claims": claim_hash,
                    },
                    schema_version=CANONICAL_HASH_SCHEMA_V1,
                ),
            )
            evidence_by_id = {str(row.id): row for row in evidence_rows}
            for position, raw in enumerate(generated.claims):
                claim = Claim.objects.using(alias).create(
                    revision=revision,
                    claim_type=raw["claimType"],
                    text=raw["text"],
                    position=position,
                    citation_marker=raw["citationMarker"],
                )
                ClaimEvidence.objects.using(alias).create(
                    claim=claim,
                    evidence=evidence_by_id[raw["evidenceId"]],
                )
            checks = {
                "all_fact_claims_sourced": all(
                    claim.evidence_links.exists()
                    for claim in revision.claims.all()
                ),
                "has_source_section": "## 출처"
                in revision.body_markdown,
                "no_false_experience": not any(
                    phrase in revision.body_markdown
                    for phrase in (
                        "직접 취재",
                        "제가 경험",
                        "관계자와 인터뷰",
                    )
                ),
            }
            for code, passed in checks.items():
                QualityCheck.objects.using(alias).create(
                    revision=revision,
                    code=code,
                    state="passed" if passed else "failed",
                )
            revision.quality_state = (
                "passed" if all(checks.values()) else "failed"
            )
            revision.save(
                update_fields=["quality_state"],
                using=alias,
            )
            article.current_revision = revision
            article.state = (
                "review_ready"
                if revision.quality_state == "passed"
                else "blocked"
            )
            article.save(
                update_fields=[
                    "current_revision",
                    "state",
                    "updated_at",
                ],
                using=alias,
            )
            attempt.state = "succeeded"
            attempt.finished_at = timezone.now()
            attempt.save(
                update_fields=["state", "finished_at"],
                using=alias,
            )
            run.state = RunState.AWAITING_APPROVAL
            run.save(update_fields=["state"], using=alias)
            record_audit_event(
                context=audit_context,
                action="article.draft_generated",
                entity=revision,
                identity_key=audit_context.event_key,
                material_schema_version="article-audit-v1",
                before_material={
                    "article": before_material,
                    "run": {
                        "id": str(run.id),
                        "state": run_before_state,
                    },
                },
                after_material={
                    "article": _article_material(article, revision),
                    "run": {
                        "id": str(run.id),
                        "state": run.state,
                    },
                },
                metadata={
                    "result": "generated",
                    "state": article.state,
                    "revision_id": str(revision.id),
                    "revision_no": revision.revision_no,
                    "collection_run_id": str(run.id),
                    "manifest_hash": input_hash,
                    "result_hash": _revision_content_hash(
                        title=revision.title,
                        summary=revision.summary,
                        body_markdown=revision.body_markdown,
                    ),
                },
            )
            return article
        except Exception:
            raise


def create_manual_revision(
    *,
    article_id,
    base_revision_no: int,
    title: str,
    summary: str,
    body_markdown: str,
    user,
    audit_context: AuditContext,
) -> tuple[ArticleRevision, bool]:
    if (
        audit_context.actor_type != AuditEvent.ActorType.ADMIN
        or str(audit_context.actor_id) != str(user.pk)
    ):
        raise ValueError(
            "audit actor does not match the revision administrator"
        )
    if not audit_context.request_key:
        raise ValueError("request_key is required")
    if audit_context.reason_code is None:
        raise ValueError("edit reason is required")

    alias = audit_context.database_alias
    revision_id = _manual_revision_id(
        article_id=article_id,
        audit_context=audit_context,
    )
    content_hash = _revision_content_hash(
        title=title,
        summary=summary,
        body_markdown=body_markdown,
    )
    request_hash = canonical_hash(
        {
            "schema_version": "manual-revision-request-v1",
            "article_id": str(article_id),
            "base_revision_no": base_revision_no,
            "content_hash": content_hash,
            "request_key": audit_context.request_key,
            "reason": audit_context.reason_code,
            "actor_id": str(user.pk),
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    with transaction.atomic(using=alias):
        article = (
            DraftArticle.objects.using(alias)
            .select_for_update()
            .select_related("current_revision")
            .get(pk=article_id)
        )
        existing = (
            ArticleRevision.objects.using(alias)
            .filter(pk=revision_id, article=article)
            .first()
        )
        if existing is not None:
            require_audit_replay(
                context=audit_context,
                action="article.revision.created",
                entity=existing,
                identity_key=audit_context.request_key,
                request_hash=request_hash,
            )
            return existing, False

        previous = article.current_revision
        current_revision_no = previous.revision_no if previous else 0
        if current_revision_no != base_revision_no:
            raise ValueError("base_revision_no is stale")
        before_material = _article_material(article, previous)
        revision = ArticleRevision.objects.using(alias).create(
            id=revision_id,
            article=article,
            revision_no=current_revision_no + 1,
            title=title,
            summary=summary,
            body_markdown=body_markdown,
            provenance_kind="admin_edit",
            input_manifest_hash=(
                previous.input_manifest_hash
                if previous
                else canonical_hash(
                    {},
                    schema_version=CANONICAL_HASH_SCHEMA_V1,
                )
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
        article.save(
            update_fields=[
                "current_revision",
                "state",
                "updated_at",
            ],
            using=alias,
        )
        record_audit_event(
            context=audit_context,
            action="article.revision.created",
            entity=revision,
            identity_key=audit_context.request_key,
            material_schema_version="article-audit-v1",
            before_material=before_material,
            after_material=_article_material(article, revision),
            metadata={
                "request_hash": request_hash,
                "result": "created",
                "state": article.state,
                "revision_id": str(revision.id),
                "revision_no": revision.revision_no,
                "result_hash": content_hash,
            },
        )
        return revision, True
