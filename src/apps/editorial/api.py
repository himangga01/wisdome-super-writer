from __future__ import annotations

from typing import Any, Iterable

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.audit.services import AuditContext
from apps.evidence.api import (
    _camelize as camelize_evidence_locator,
    _decision_summary as evidence_decision_summary,
    _document_provenance as document_evidence_provenance,
    _generic_provenance as generic_evidence_provenance,
)
from apps.evidence.models import EvidenceAsset
from wisdome_writer.api.openapi import openapi_operation
from wisdome_writer.domain.errors import InvalidInput, StaleVersion, StateConflict

from .models import CorrectionCase, DraftArticle
from .services import (
    create_manual_revision,
    evaluate_revision_publishability_readonly,
)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _rows(relation) -> list:
    if relation is None:
        return []
    return list(relation.all() if hasattr(relation, "all") else relation)


def _bounded_text(value: object, limit: int = 2000) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _policy_payload(revision) -> dict[str, Any]:
    snapshot = revision.editorial_policy_snapshot
    document = snapshot.document if isinstance(snapshot.document, dict) else {}
    legacy_quarantine = (
        document.get("schemaVersion") == "editorial-policy-legacy-quarantine-v1"
    )
    return {
        "snapshotId": str(snapshot.id),
        "policyKey": snapshot.policy_key,
        "policyVersion": revision.editorial_policy_version,
        "topic": snapshot.topic_code,
        "releaseDocumentHash": (
            None
            if legacy_quarantine
            else getattr(snapshot, "release_document_hash", None)
        ),
        "configHash": (
            None if legacy_quarantine else getattr(snapshot, "config_hash", None)
        ),
        "implementationManifestHash": (
            None
            if legacy_quarantine
            else getattr(snapshot, "implementation_manifest_hash", None)
        ),
        "materialHash": revision.editorial_policy_hash,
    }


def _revision_payload(revision) -> dict[str, Any]:
    attempt = getattr(revision, "generation_attempt", None)
    pipeline_hash = (
        getattr(attempt, "generation_pipeline_manifest_hash", None)
        if attempt is not None
        else None
    )
    return {
        "revisionNo": revision.revision_no,
        "title": revision.title,
        "summary": revision.summary,
        "bodyBlocks": list(revision.body_blocks),
        "canonicalMarkdown": revision.body_markdown,
        "contentHash": revision.content_hash,
        "provenanceKind": (
            "automated"
            if revision.provenance_kind == "generated"
            else revision.provenance_kind
        ),
        "generationAttemptId": (
            str(revision.generation_attempt_id)
            if revision.generation_attempt_id
            else None
        ),
        "editorialPolicy": _policy_payload(revision),
        "verificationManifestHash": revision.verification_manifest_hash,
        "inputEvidenceManifestHash": revision.evidence_manifest_hash,
        "excludedMaterialManifestHash": revision.exclusion_manifest_hash,
        "generationPipelineManifestHash": pipeline_hash,
        "claimGraphState": revision.claim_graph_state,
        "qualityGateManifestHash": revision.quality_gate_manifest_hash,
        "qualityReportHash": revision.quality_report_hash,
        "correctionNote": None,
        "createdAt": revision.created_at.isoformat(),
    }


def _cluster_payloads(article) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    memberships = _rows(getattr(article, "event_clusters", None))
    by_verification_id = {}
    payloads = []
    for row in memberships:
        verification = row.verification
        by_verification_id[str(verification.id)] = (row, verification)
        payloads.append(
            {
                "eventClusterId": str(row.event_cluster_id),
                "role": row.role,
                "displayOrder": row.display_order,
                "inclusionReason": row.inclusion_reason,
                "clusterSnapshotHash": row.cluster_snapshot_hash,
            }
        )
    return payloads, by_verification_id


def _verification_payloads(revision) -> list[dict[str, Any]]:
    result = []
    for frozen in revision.verification_manifest:
        result.append(
            {
                "verificationId": str(frozen.get("verificationId")),
                "role": frozen.get("role"),
                "decision": frozen.get("decision"),
                "articleType": frozen.get("articleType"),
                "category": frozen.get("category"),
                "policyVersion": frozen.get("policyVersion"),
                "policyHash": frozen.get("policyHash"),
                "evidenceManifestHash": frozen.get("evidenceManifestHash"),
                "ruleManifestHash": frozen.get("ruleManifestHash"),
                "resultManifestHash": frozen.get("resultManifestHash"),
                "localEventDate": frozen.get("localEventDate"),
            }
        )
    return result


def _claim_payloads(revision) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    frozen_evidence = {
        str(row.get("evidenceId")): row
        for row in revision.evidence_manifest
        if isinstance(row, dict) and row.get("evidenceId")
    }
    bindings = list(getattr(revision, "claim_bindings", []) or [])
    claims = []
    live_evidence = {}
    for claim in _rows(revision.claims):
        binding = next(
            (
                row
                for row in bindings
                if row.get("blockId") == claim.block_id
                and row.get("statement") == claim.text
                and row.get("citationMarker") == claim.citation_marker
            ),
            {},
        )
        links = []
        for link in _rows(claim.evidence_links):
            evidence_id = str(link.evidence_id)
            frozen = frozen_evidence.get(evidence_id, {})
            evidence = getattr(link, "evidence", None)
            if evidence is not None:
                live_evidence[evidence_id] = evidence
            links.append(
                {
                    "evidenceId": evidence_id,
                    "relation": link.relation,
                    "sourceSpan": _bounded_text(link.source_span),
                    "sourceSpanHash": link.source_span_hash,
                    "verificationStrength": link.verification_strength,
                    "independenceGroup": frozen.get(
                        "independenceGroup", "unknown"
                    ),
                    "originIdentityHash": frozen.get(
                        "originIdentityHash", ""
                    ),
                    "checkedAt": _iso(link.checked_at),
                }
            )
        claims.append(
            {
                "id": str(claim.id),
                "blockId": claim.block_id,
                "statement": claim.text,
                "type": claim.claim_type,
                "riskLevel": claim.risk_level,
                "verificationState": claim.verification_state,
                "actor": binding.get("actor"),
                "attribution": binding.get("attribution"),
                "horizon": binding.get("horizon"),
                "uncertaintyNote": binding.get("uncertaintyNote"),
                "derivedFromClaimIds": list(
                    binding.get("derivedFromClaimIds", [])
                ),
                "evidenceIds": [row["evidenceId"] for row in links],
                "evidenceLinks": links,
            }
        )
    return claims, live_evidence


def _evidence_snapshot_payloads(revision) -> list[dict[str, Any]]:
    result = []
    for frozen in revision.evidence_manifest:
        evidence_id = str(frozen.get("evidenceId"))
        if frozen.get("legacyQuarantine") is True:
            result.append(
                {
                    "evidenceId": evidence_id,
                    "legacyQuarantine": True,
                }
            )
            continue
        result.append(
            {
                "evidenceId": evidence_id,
                "sourceItemId": frozen.get("sourceItemId"),
                "runSourceItemId": frozen.get("runSourceItemId"),
                "sourceVersionHash": frozen.get("sourceVersionHash", ""),
                "contentHash": frozen.get("contentHash", ""),
                "reviewSubjectHash": frozen.get("reviewSubjectHash", ""),
                "locatorType": frozen.get("locatorType", ""),
                "locator": dict(frozen.get("locator") or {}),
                "authorityTier": frozen.get("authorityTier", "unknown"),
                "independenceGroup": frozen.get(
                    "independenceGroup", "unknown"
                ),
                "originIdentityHash": frozen.get("originIdentityHash", ""),
                "sourceTitle": frozen.get("sourceTitle"),
                "sourceUrl": frozen.get("sourceUrl"),
                "publisher": frozen.get("publisher", ""),
                "publishedAt": frozen.get("publishedAt"),
                "modifiedAt": frozen.get("modifiedAt"),
                "retrievedAt": frozen.get("retrievedAt"),
                "freshnessCutoff": frozen.get("freshnessCutoff"),
                "rightsStatus": frozen.get("rightsStatus"),
                "rightsBasisUrl": frozen.get("rightsBasisUrl"),
                "attribution": frozen.get("attributionText"),
                "altText": frozen.get("altText"),
                "evaluatedPublishEligible": frozen.get("publishable") is True,
                "eligibilityEvaluatedAt": revision.created_at.isoformat(),
            }
        )
    return result


def _exclusion_payloads(revision) -> list[dict[str, Any]]:
    result = []
    for frozen in revision.exclusion_manifest:
        evidence_material = {
            str(row.get("evidenceId")): row
            for row in frozen.get("evidenceMaterial", [])
            if isinstance(row, dict) and row.get("evidenceId")
        }
        evidence_ids = frozen.get("evidenceIds") or [None]
        for evidence_id in evidence_ids:
            evidence = evidence_material.get(str(evidence_id), {})
            result.append(
                {
                    "verificationId": str(frozen.get("verificationId")),
                    "runSourceItemId": frozen.get("runSourceItemId"),
                    "sourceItemId": frozen.get("sourceItemId"),
                    "evidenceId": str(evidence_id) if evidence_id else None,
                    "sourceUrl": frozen.get("sourceUrl"),
                    "sourceTitle": frozen.get("sourceTitle"),
                    "publisher": frozen.get("publisher"),
                    "rightsStatus": evidence.get(
                        "rightsStatus", frozen.get("rightsStatus")
                    ),
                    "classification": frozen.get("classification")
                    or {
                        "excluded": "excluded",
                        "duplicate": "duplicate",
                        "conflicting": "conflicting",
                    }.get(frozen.get("selectionState"), "excluded"),
                    "sourceStatus": frozen.get("sourceStatus"),
                    "reason": frozen.get("reason"),
                }
            )
    return result


def _evidence_payloads(
    evidence_by_id: dict[str, Any],
    claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    excerpts = {}
    for claim in claims:
        for link in claim["evidenceLinks"]:
            if link["sourceSpan"] and link["evidenceId"] not in excerpts:
                excerpts[link["evidenceId"]] = link["sourceSpan"]
    result = []
    for evidence_id, row in sorted(evidence_by_id.items()):
        source = getattr(row, "source_item", None)
        origin = getattr(row, "origin_run_source_item", None)
        extraction = None
        if row.derivation_type == "document_derived":
            extraction = document_evidence_provenance(row)
        elif row.derivation_type == "other_derived":
            extraction = generic_evidence_provenance(row)
        result.append(
            {
                "id": evidence_id,
                "sourceItemId": (
                    str(row.source_item_id) if row.source_item_id else None
                ),
                "originCollectionRunId": (
                    str(origin.run_id) if origin is not None else None
                ),
                "originRunSourceItemId": (
                    str(row.origin_run_source_item_id)
                    if row.origin_run_source_item_id
                    else None
                ),
                "originSourceSnapshotId": (
                    str(origin.source_snapshot_id) if origin is not None else None
                ),
                "sourceChecksum": getattr(source, "content_hash", None),
                "derivationType": row.derivation_type,
                "kind": row.kind,
                "contentHash": row.evidence_content_hash,
                "sourceTitle": getattr(source, "title", None),
                "sourceUrl": getattr(source, "canonical_url", None),
                "publisher": getattr(source, "publisher", ""),
                "publishedAt": _iso(getattr(source, "published_at", None)),
                "locator": camelize_evidence_locator(row.locator) if row.locator else None,
                # Only the bounded, claim-selected citation span is returned.
                # EvidenceAsset.extracted_text/structured_data are never read here.
                "excerpt": excerpts.get(evidence_id),
                "extraction": extraction,
                "rightsStatus": row.rights_status,
                "rightsBasisUrl": row.rights_basis_url,
                "attribution": row.attribution_text,
                "altText": row.alt_text,
                "confidence": (
                    float(row.confidence) if row.confidence is not None else None
                ),
                "confidenceDetail": row.confidence_detail,
                "lowConfidenceReasons": list(row.low_confidence_reasons or []),
                "reviewSubjectSchemaVersion": row.review_subject_schema_version,
                "reviewSubjectHash": row.review_subject_hash,
                "latestManualReviewDecision": evidence_decision_summary(
                    getattr(row, "latest_review_decision", None)
                ),
                "manualReviewRequired": row.manual_review_required,
                "publishable": row.publishable,
                "reviewState": row.review_state,
                "exclusionReason": None,
            }
        )
    return result


def _quality_payloads(revision) -> list[dict[str, Any]]:
    return [
        {
            "code": row.code,
            "checkVersion": row.check_version,
            "result": row.result,
            "score": row.score,
            "blocking": row.blocking,
            "details": dict(row.details),
            "executedAt": row.created_at.isoformat(),
        }
        for row in _rows(revision.quality_checks)
    ]


def _visual_payloads(revision) -> list[dict[str, Any]]:
    return [
        {
            "id": str(row.id),
            "blockId": row.block_id,
            "evidenceId": (
                str(row.source_evidence_id)
                if row.source_evidence_id
                else None
            ),
            "visualizationId": (
                str(row.visualization_id) if row.visualization_id else None
            ),
            "displayOrder": row.display_order,
            "locator": dict(row.locator_snapshot),
            "rightsStatus": row.rights_status_snapshot,
            "attribution": row.attribution_snapshot,
            "altText": row.alt_text_snapshot,
            "caption": row.caption,
            "presentationHash": row.presentation_hash,
        }
        for row in _rows(getattr(revision, "visual_placements", None))
    ]


def _runtime_payload(
    revision,
    *,
    publishability_decision,
    evaluated_at=None,
) -> dict[str, Any]:
    evaluated_at = evaluated_at or timezone.now()
    snapshot = revision.editorial_policy_snapshot
    current_snapshot_id = publishability_decision.current_policy_snapshot_id
    return {
        "evaluatedPolicySnapshotId": str(snapshot.id),
        "evaluatedPolicyMaterialHash": snapshot.material_hash,
        "currentReleasePolicySnapshotId": (
            str(current_snapshot_id) if current_snapshot_id else None
        ),
        "currentReleasePolicyMaterialHash": (
            publishability_decision.current_policy_material_hash
        ),
        "policyCurrent": publishability_decision.policy_current,
        "evidenceCurrent": publishability_decision.evidence_current,
        "publishEligible": publishability_decision.publishable,
        "blockingCodes": sorted(publishability_decision.blocking_codes),
        "evaluatedAt": evaluated_at.isoformat(),
    }


def _publication_payloads(publications: Iterable) -> list[dict[str, Any]]:
    return [
        {
            "targetId": str(row.target_id),
            "channel": row.target.channel,
            "channelRole": row.target.role,
            "state": row.state,
            "remoteState": row.remote_state,
            "remotePostId": row.remote_post_id,
            "remoteUrl": row.remote_url,
            "canonicalSourceUrl": row.canonical_source_url,
            "scheduledFor": _iso(row.scheduled_for),
            "canonicalReadyAt": _iso(row.canonical_ready_at),
            "publishedAt": _iso(row.published_at),
            "publishedRevisionNo": row.published_revision_no,
            "errorCode": row.last_error_code or None,
        }
        for row in publications
    ]


def _article_payload(
    article,
    detail: bool = False,
    *,
    evidence_rows: Iterable | None = None,
    publications: Iterable | None = None,
    publishability_decision=None,
    evaluated_at=None,
):
    revision = article.current_revision
    source_verification = getattr(article, "source_verification", None)
    payload = {
        "id": str(article.id),
        "topic": article.topic_code,
        "articleType": article.article_type,
        "articleIdentityKey": article.article_identity_key,
        "localDigestDate": _iso(
            getattr(source_verification, "local_event_date", None)
        ),
        "digestPolicyVersion": getattr(source_verification, "version", None),
        "state": "drafting" if article.state == "draft" else article.state,
        "title": revision.title if revision else "",
        "currentRevisionNo": revision.revision_no if revision else None,
        "updatedAt": article.updated_at.isoformat(),
    }
    if not detail or revision is None:
        return payload
    if publishability_decision is None:
        raise ValueError("publishability decision is required for article detail")

    cluster_memberships, _verification_by_id = _cluster_payloads(article)
    claims, linked_evidence = _claim_payloads(revision)
    evidence_by_id = dict(linked_evidence)
    for row in evidence_rows or []:
        evidence_by_id[str(row.id)] = row
    quality_checks = _quality_payloads(revision)
    payload.update(
        {
            "revision": _revision_payload(revision),
            "clusterMemberships": cluster_memberships,
            "verificationSnapshots": _verification_payloads(revision),
            "evidenceSnapshots": _evidence_snapshot_payloads(revision),
            "excludedMaterials": _exclusion_payloads(revision),
            "claims": claims,
            "evidence": _evidence_payloads(evidence_by_id, claims),
            "qualityChecks": quality_checks,
            "visualPlacements": _visual_payloads(revision),
            "runtimeEligibility": _runtime_payload(
                revision,
                publishability_decision=publishability_decision,
                evaluated_at=evaluated_at,
            ),
            "publications": _publication_payloads(publications or []),
            "revalidationState": revision.claim_graph_state,
            "qualityState": revision.quality_state,
        }
    )
    return payload


@login_required
@openapi_operation("listArticles")
def articles(request):
    queryset = DraftArticle.objects.select_related(
        "current_revision", "source_run", "source_verification"
    )[:100]
    return JsonResponse(
        {"items": [_article_payload(article) for article in queryset]}
    )


@login_required
@openapi_operation("getArticle")
def article_detail(request, article_id):
    article = get_object_or_404(
        DraftArticle.objects.select_related(
            "current_revision__editorial_policy_snapshot",
            "current_revision__generation_attempt",
            "source_verification",
        ).prefetch_related(
            "event_clusters__verification",
            "current_revision__claims__evidence_links__evidence__source_item",
            "current_revision__claims__evidence_links__evidence__origin_run_source_item",
            "current_revision__quality_checks",
            "current_revision__visual_placements",
        ),
        id=article_id,
    )
    revision = article.current_revision
    evidence_rows = []
    publishability_decision = None
    if revision is not None:
        publishability_decision = evaluate_revision_publishability_readonly(
            revision_id=revision.id,
            using=revision._state.db or "default",
        )
        evidence_ids = [
            row.get("evidenceId")
            for row in revision.evidence_manifest
            if isinstance(row, dict) and row.get("evidenceId")
        ]
        evidence_rows = list(
            EvidenceAsset.objects.filter(pk__in=evidence_ids).select_related(
                "source_item",
                "origin_run_source_item",
                "document_extraction",
                "extraction_run",
                "generic_extraction_attempt__input_asset",
                "latest_review_decision",
            )
        )
    from apps.publishing.models import Publication

    publications = Publication.objects.filter(article_id=article.id).select_related(
        "target"
    )
    return JsonResponse(
        _article_payload(
            article,
            detail=True,
            evidence_rows=evidence_rows,
            publications=publications,
            publishability_decision=publishability_decision,
        )
    )


@login_required
@openapi_operation("createArticleRevision")
def revise_article(request, article_id):
    body = request.openapi_body
    get_object_or_404(DraftArticle, id=article_id)
    audit_context = AuditContext.for_admin(
        request=request,
        reason_code=body["editReason"],
        request_key=body["requestKey"],
    )
    try:
        revision, created = create_manual_revision(
            article_id=article_id,
            base_revision_no=body["baseRevisionNo"],
            title=body["title"],
            summary=body["summary"],
            body_blocks=body["bodyBlocks"],
            claim_bindings=body["claimBindings"],
            user=request.user,
            audit_context=audit_context,
        )
    except ValueError as exc:
        reason = str(exc)
        if reason == "base_revision_no is stale":
            raise StaleVersion("base revision is stale") from exc
        if reason in {
            "article is not eligible for manual revision",
            "manual revision requires a verified base revision",
        }:
            raise StateConflict("article is not eligible for revision") from exc
        raise InvalidInput("manual revision material is invalid") from exc
    return JsonResponse(
        {
            "articleId": str(article_id),
            "revisionId": str(revision.id),
            "revisionNo": revision.revision_no,
            "revalidationState": revision.claim_graph_state,
            "qualityState": revision.quality_state,
            "revisionContentHash": revision.content_hash,
            "inputEvidenceManifestHash": revision.evidence_manifest_hash,
            "editorialPolicyHash": revision.editorial_policy_hash,
            "verificationManifestHash": revision.verification_manifest_hash,
            "excludedMaterialManifestHash": revision.exclusion_manifest_hash,
        },
        status=201 if created else 200,
    )


@login_required
def article_corrections(request, article_id):
    rows = CorrectionCase.objects.filter(article_id=article_id).order_by(
        "-detected_at"
    )
    return JsonResponse(
        {
            "items": [
                {
                    "id": str(row.id),
                    "kind": row.kind,
                    "state": row.state,
                    "sourceItemId": str(row.source_item_id),
                    "priorSourceItemId": (
                        str(row.prior_source_item_id)
                        if row.prior_source_item_id
                        else None
                    ),
                    "subjectHash": row.subject_hash,
                    "diffSummary": row.diff_summary,
                    "detectedAt": row.detected_at.isoformat(),
                    "completedAt": (
                        row.completed_at.isoformat()
                        if row.completed_at
                        else None
                    ),
                }
                for row in rows
            ]
        }
    )
