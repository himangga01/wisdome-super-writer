import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods
from wisdome_writer.domain.hashing import sha256_hex

from .models import CorrectionCase, DraftArticle
from .services import create_manual_revision


def _article_payload(article, detail=False):
    revision = article.current_revision
    payload = {
        "id": str(article.id),
        "identityKey": article.article_identity_key,
        "topic": article.topic_code,
        "articleType": article.article_type,
        "state": article.state,
        "runId": str(article.source_run_id),
        "currentRevisionId": str(article.current_revision_id) if article.current_revision_id else None,
        "title": revision.title if revision else None,
        "summary": revision.summary if revision else None,
    }
    if detail and revision:
        payload.update(
            {
                "revisionNo": revision.revision_no,
                "revisionContentHash": sha256_hex(
                    {
                        "title": revision.title,
                        "summary": revision.summary,
                        "bodyMarkdown": revision.body_markdown,
                    }
                ),
                "bodyMarkdown": revision.body_markdown,
                "qualityState": revision.quality_state,
                "claims": [
                    {
                        "id": str(claim.id),
                        "type": claim.claim_type,
                        "text": claim.text,
                        "citationMarker": claim.citation_marker,
                        "evidenceIds": [str(link.evidence_id) for link in claim.evidence_links.all()],
                    }
                    for claim in revision.claims.prefetch_related("evidence_links")
                ],
            }
        )
    return payload


@login_required
def articles(request):
    queryset = DraftArticle.objects.select_related("current_revision", "source_run")[:100]
    return JsonResponse({"items": [_article_payload(article) for article in queryset]})


@login_required
def article_detail(request, article_id):
    article = get_object_or_404(DraftArticle.objects.select_related("current_revision"), id=article_id)
    return JsonResponse(_article_payload(article, detail=True))


@login_required
@require_http_methods(["POST"])
def revise_article(request, article_id):
    article = get_object_or_404(DraftArticle.objects.select_related("current_revision"), id=article_id)
    body = json.loads(request.body or b"{}")
    revision = create_manual_revision(
        article,
        title=body["title"],
        summary=body.get("summary", ""),
        body_markdown=body["bodyMarkdown"],
        user=request.user,
    )
    return JsonResponse({"articleId": str(article.id), "revisionId": str(revision.id)}, status=201)


@login_required
def article_corrections(request, article_id):
    rows = CorrectionCase.objects.filter(article_id=article_id).order_by("-detected_at")
    return JsonResponse(
        {
            "items": [
                {
                    "id": str(row.id),
                    "kind": row.kind,
                    "state": row.state,
                    "sourceItemId": str(row.source_item_id),
                    "priorSourceItemId": (
                        str(row.prior_source_item_id) if row.prior_source_item_id else None
                    ),
                    "subjectHash": row.subject_hash,
                    "diffSummary": row.diff_summary,
                    "detectedAt": row.detected_at.isoformat(),
                    "completedAt": row.completed_at.isoformat() if row.completed_at else None,
                }
                for row in rows
            ]
        }
    )
