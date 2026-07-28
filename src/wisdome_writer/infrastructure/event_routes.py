from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PayloadSchema:
    fields: dict[str, tuple[type, ...]]
    required: frozenset[str]


def _schema(
    required: dict[str, tuple[type, ...]],
    optional: dict[str, tuple[type, ...]] | None = None,
) -> PayloadSchema:
    return PayloadSchema(
        fields={**required, **(optional or {})},
        required=frozenset(required),
    )


@dataclass(frozen=True)
class EventRoute:
    consumer_name: str
    queue: str
    task_name: str
    argument_keys: tuple[str, ...]


EVENT_ROUTES: dict[str, EventRoute] = {
    "run.requested": EventRoute(
        "collection-run", "collect.housing", "apps.collection.tasks.execute_collection_run", ("run_id",)
    ),
    "run.evidence_requested": EventRoute(
        "run-evidence", "extract.generic", "apps.evidence.tasks.process_run_evidence", ("run_id",)
    ),
    "evidence.document_extract_requested": EventRoute(
        "document-extraction",
        "extract.ocr.paddle",
        "apps.evidence.tasks.process_paddleocr_document",
        ("document_extraction_id",),
    ),
    "evidence.finalize_requested": EventRoute(
        "evidence-finalize", "extract.document", "apps.evidence.tasks.finalize_run_evidence", ("run_id",)
    ),
    "evidence.document_ready": EventRoute(
        "document-ready-finalize",
        "extract.document",
        "apps.evidence.tasks.finalize_run_evidence",
        ("run_id",),
    ),
    "evidence.other_ready": EventRoute(
        "other-ready-finalize",
        "extract.document",
        "apps.evidence.tasks.finalize_run_evidence",
        ("run_id",),
    ),
    "evidence.profile_decided": EventRoute(
        "profile-decision-ack",
        "maintenance",
        "wisdome_writer.infrastructure.tasks.acknowledge_domain_event",
        (),
    ),
    "evidence.review_decided": EventRoute(
        "evidence-review-ack",
        "maintenance",
        "wisdome_writer.infrastructure.tasks.acknowledge_domain_event",
        (),
    ),
    "article.draft_requested": EventRoute(
        "article-draft", "editorial", "apps.editorial.tasks.generate_run_draft", ("run_id",)
    ),
    "publication.scheduled_run_requested": EventRoute(
        "scheduled-publication",
        "publish.wordpress",
        "apps.publishing.tasks.dispatch_scheduled_run_publication",
        ("run_id",),
    ),
    "publication.preflight_requested": EventRoute(
        "target-preflight",
        "publish.wordpress",
        "apps.publishing.tasks.run_target_preflight",
        ("target_id",),
    ),
    "publication.requested": EventRoute(
        "publication-execute",
        "publish.wordpress",
        "apps.publishing.tasks.execute_publication_attempt",
        ("publication_attempt_id",),
    ),
    "publication.reconcile_requested": EventRoute(
        "publication-reconcile",
        "reconcile",
        "apps.publishing.tasks.reconcile_publication_attempt",
        ("publication_attempt_id",),
    ),
    "publishing.target_canary.requested": EventRoute(
        "target-canary",
        "publish.wordpress",
        "apps.publishing.tasks.run_target_canary",
        ("canary_run_id",),
    ),
    "publishing.target_disconnect.requested": EventRoute(
        "target-disconnect",
        "publish.wordpress",
        "apps.publishing.tasks.revoke_target_credentials",
        ("decision_id",),
    ),
    "retention.expire_requested": EventRoute(
        "retention-expire",
        "maintenance",
        "apps.audit.retention.execute_retention_batch_task",
        ("retention_batch_id",),
    ),
    "delivery.delete_requested": EventRoute(
        "delivery-delete",
        "publish.media.wordpress",
        "apps.publishing.tasks.delete_public_delivery_asset",
        ("public_delivery_asset_id", "expected_lease_generation"),
    ),
    "media.reconcile_requested": EventRoute(
        "media-reconcile",
        "reconcile",
        "apps.publishing.tasks.reconcile_remote_media",
        ("remote_media_id",),
    ),
}

STR = (str,)
STR_OR_NONE = (str, type(None))

EVENT_PAYLOAD_SCHEMAS: dict[str, PayloadSchema] = {
    "run.requested": _schema({"run_id": STR, "topic_code": STR}),
    "run.evidence_requested": _schema({"run_id": STR}),
    "evidence.document_extract_requested": _schema(
        {"run_id": STR, "document_extraction_id": STR}
    ),
    "evidence.finalize_requested": _schema({"run_id": STR}),
    "article.draft_requested": _schema({"run_id": STR}),
    "publication.scheduled_run_requested": _schema({"run_id": STR}),
    "publication.preflight_requested": _schema(
        {"target_id": STR}, {"channel": STR}
    ),
    "publication.requested": _schema(
        {"publication_attempt_id": STR}, {"channel": STR}
    ),
    "publication.reconcile_requested": _schema(
        {"publication_attempt_id": STR}, {"channel": STR}
    ),
    "publishing.target_canary.requested": _schema(
        {"canary_run_id": STR, "target_id": STR}, {"channel": STR}
    ),
    "publishing.target_disconnect.requested": _schema(
        {"decision_id": STR, "target_id": STR}, {"channel": STR}
    ),
    "retention.expire_requested": _schema({"retention_batch_id": STR}),
    "delivery.delete_requested": _schema(
        {"public_delivery_asset_id": STR, "expected_lease_generation": (int,)}
    ),
    "media.reconcile_requested": _schema({"remote_media_id": STR}),
    "evidence.document_ready": _schema(
        {
            "run_id": STR,
            "run_source_item_id": STR,
            "source_item_id": STR,
            "document_extraction_id": STR,
            "input_page_count": (int,),
            "coverage_manifest_hash": STR,
            "selected_evidence_manifest_hash": STR,
            "document_complete": (bool,),
        }
    ),
    "evidence.other_ready": _schema(
        {
            "run_id": STR,
            "run_source_item_id": STR,
            "source_item_id": STR,
            "generic_extraction_attempt_id": STR,
            "evidence_asset_id": STR,
            "engine": STR,
            "locator_type": STR,
            "validation_mode": STR,
            "result_checksum": STR,
            "extraction_fingerprint": STR,
        },
        {
            "low_confidence_reasons_hash": STR_OR_NONE,
            "calibration_profile_key": STR_OR_NONE,
            "calibration_profile_version": STR_OR_NONE,
            "calibration_profile_hash": STR_OR_NONE,
        },
    ),
    "evidence.profile_decided": _schema(
        {
            "profile_snapshot_id": STR,
            "decision_id": STR,
            "decision": STR,
            "profile_material_hash": STR,
        }
    ),
    "evidence.review_decided": _schema(
        {
            "evidence_asset_id": STR,
            "evidence_review_decision_id": STR,
            "review_subject_schema_version": STR,
            "review_subject_hash": STR,
            "decision": STR,
            "publishable": (bool,),
        }
    ),
}


def route_for(event_type: str) -> EventRoute | None:
    return EVENT_ROUTES.get(event_type)


def payload_schema_for(event_type: str) -> PayloadSchema | None:
    return EVENT_PAYLOAD_SCHEMAS.get(event_type)


def queue_for(route: EventRoute, envelope: dict) -> str:
    event_type = envelope.get("event_type")
    payload = envelope.get("payload") or {}
    if event_type == "run.requested":
        topic_code = str(payload.get("topic_code", "")).lower()
        return "collect.semiconductor" if "semiconductor" in topic_code else "collect.housing"
    if event_type in {
        "publication.preflight_requested",
        "publication.requested",
        "publishing.target_canary.requested",
        "publishing.target_disconnect.requested",
    }:
        return (
            "publish.blogger"
            if str(payload.get("channel", "")).lower() == "blogger"
            else route.queue
        )
    return route.queue
