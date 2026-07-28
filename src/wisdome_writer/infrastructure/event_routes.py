from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ObjectDoesNotExist


EventKey = tuple[str, int]


@dataclass(frozen=True)
class PayloadSchema:
    fields: dict[str, tuple[type, ...]]
    required: frozenset[str]
    formats: dict[str, str]
    choices: dict[str, frozenset[Any]]


def _schema(
    required: dict[str, tuple[type, ...]],
    optional: dict[str, tuple[type, ...]] | None = None,
    *,
    formats: dict[str, str] | None = None,
    choices: dict[str, set[Any] | frozenset[Any]] | None = None,
) -> PayloadSchema:
    return PayloadSchema(
        fields={**required, **(optional or {})},
        required=frozenset(required),
        formats=formats or {},
        choices={key: frozenset(values) for key, values in (choices or {}).items()},
    )


@dataclass(frozen=True)
class EventRoute:
    consumer_name: str
    queue: str
    task_name: str
    argument_keys: tuple[str, ...]
    terminal_task_name: str | None = None
    terminal_argument_keys: tuple[str, ...] = ()
    max_attempts: int = 5


class EventRoutingError(ValueError):
    """The persisted domain identity cannot be mapped to a safe worker pool."""


STR = (str,)
STR_OR_NONE = (str, type(None))
UUID_FORMAT = "uuid"
SHA256_FORMAT = "sha256"
VERSION_FORMAT = "version"
KEY_FORMAT = "domain_key"
POSITIVE_INTEGER_FORMAT = "positive_integer"
EXTRACTION_ENGINE_VALUES = frozenset(
    {
        "native_pdf",
        "paddleocr_ppstructurev3",
        "html_parser",
        "structured_parser",
        "spreadsheet_parser",
        "hwpx_parser",
        "legacy_hwp_converter",
        "browser_capture",
        "media_parser",
        "manual_entry",
    }
)

EVENT_ROUTES: dict[EventKey, EventRoute] = {
    ("run.requested", 1): EventRoute(
        "collection-run",
        "collect.housing",
        "apps.collection.tasks.execute_collection_run",
        ("run_id",),
    ),
    ("run.evidence_requested", 1): EventRoute(
        "run-evidence",
        "extract.generic",
        "apps.evidence.tasks.process_run_evidence",
        ("run_id",),
        max_attempts=3,
    ),
    ("evidence.document_route_requested", 1): EventRoute(
        "document-extraction",
        "extract.ocr.paddle",
        "apps.evidence.tasks.process_paddleocr_document",
        ("document_extraction_id",),
        "apps.evidence.tasks.finalize_document_extraction_failure",
        ("document_extraction_id",),
    ),
    ("evidence.other_extract_requested", 1): EventRoute(
        "generic-extraction",
        "extract.generic",
        "apps.evidence.tasks.process_generic_extraction",
        ("generic_extraction_attempt_id",),
        "apps.evidence.tasks.finalize_generic_extraction_failure",
        ("generic_extraction_attempt_id",),
    ),
    ("evidence.finalize_requested", 1): EventRoute(
        "evidence-finalize",
        "extract.document",
        "apps.evidence.tasks.finalize_run_evidence",
        ("run_id",),
    ),
    ("evidence.document_ready", 1): EventRoute(
        "document-ready-finalize",
        "extract.document",
        "apps.evidence.tasks.finalize_run_evidence",
        ("run_id",),
    ),
    ("evidence.other_ready", 1): EventRoute(
        "other-ready-finalize",
        "extract.document",
        "apps.evidence.tasks.finalize_run_evidence",
        ("run_id",),
    ),
    ("evidence.profile_decided", 1): EventRoute(
        "profile-decision-ack",
        "maintenance",
        "wisdome_writer.infrastructure.tasks.acknowledge_domain_event",
        (),
    ),
    ("evidence.review_decided", 1): EventRoute(
        "evidence-review-ack",
        "maintenance",
        "wisdome_writer.infrastructure.tasks.acknowledge_domain_event",
        (),
    ),
    ("run.draft_requested", 1): EventRoute(
        "article-draft",
        "editorial",
        "apps.editorial.tasks.generate_run_draft",
        ("run_id",),
    ),
    ("publication.scheduled_run_requested", 1): EventRoute(
        "scheduled-publication",
        "publish.wordpress",
        "apps.publishing.tasks.dispatch_scheduled_run_publication",
        ("run_id",),
    ),
    ("publication.preflight_requested", 1): EventRoute(
        "target-preflight",
        "publish.wordpress",
        "apps.publishing.tasks.run_target_preflight",
        ("target_id", "target_snapshot_id", "target_config_hash"),
        max_attempts=4,
    ),
    ("publishing.target_preflight.completed", 1): EventRoute(
        "target-preflight-completed-ack",
        "maintenance",
        "wisdome_writer.infrastructure.tasks.acknowledge_domain_event",
        (),
    ),
    ("publication.requested", 1): EventRoute(
        "publication-execute",
        "publish.wordpress",
        "apps.publishing.tasks.execute_publication_attempt",
        ("publication_attempt_id",),
    ),
    ("publication.reconcile_requested", 1): EventRoute(
        "publication-reconcile",
        "reconcile",
        "apps.publishing.tasks.reconcile_publication_attempt",
        ("publication_attempt_id",),
    ),
    ("publication.reconcile_requested", 2): EventRoute(
        "publication-reconcile",
        "reconcile",
        "apps.publishing.tasks.reconcile_publication_attempt",
        ("publication_attempt_id", "reconcile_attempt_no"),
    ),
    ("publishing.target_canary.requested", 1): EventRoute(
        "target-canary",
        "publish.wordpress",
        "apps.publishing.tasks.run_target_canary",
        ("canary_run_id",),
    ),
    ("publishing.target_disconnect.requested", 1): EventRoute(
        "target-disconnect",
        "publish.wordpress",
        "apps.publishing.tasks.revoke_target_credentials",
        ("decision_id",),
    ),
    ("retention.expire_requested", 1): EventRoute(
        "retention-expire",
        "maintenance",
        "apps.audit.retention.execute_retention_batch_task",
        ("retention_batch_id",),
    ),
    ("delivery.delete_requested", 1): EventRoute(
        "delivery-delete",
        "publish.media.wordpress",
        "apps.publishing.tasks.delete_public_delivery_asset",
        ("public_delivery_asset_id", "expected_lease_generation"),
    ),
    ("media.reconcile_requested", 1): EventRoute(
        "media-reconcile",
        "reconcile",
        "apps.publishing.tasks.reconcile_remote_media",
        ("remote_media_id", "publication_attempt_id", "publication_intent_id"),
    ),
}


EVENT_PAYLOAD_SCHEMAS: dict[EventKey, PayloadSchema] = {
    ("run.requested", 1): _schema(
        {"run_id": STR},
        formats={"run_id": UUID_FORMAT},
    ),
    ("run.evidence_requested", 1): _schema(
        {"run_id": STR},
        formats={"run_id": UUID_FORMAT},
    ),
    ("evidence.document_route_requested", 1): _schema(
        {
            "run_id": STR,
            "run_source_item_id": STR,
            "source_item_id": STR,
            "input_kind": STR,
            "document_extraction_id": STR,
            "input_checksum": STR,
        },
        {"input_asset_id": STR_OR_NONE},
        formats={
            "run_id": UUID_FORMAT,
            "run_source_item_id": UUID_FORMAT,
            "source_item_id": UUID_FORMAT,
            "input_asset_id": UUID_FORMAT,
            "document_extraction_id": UUID_FORMAT,
            "input_checksum": SHA256_FORMAT,
        },
        choices={"input_kind": {"pdf", "standalone_image"}},
    ),
    ("evidence.other_extract_requested", 1): _schema(
        {
            "run_id": STR,
            "run_source_item_id": STR,
            "source_item_id": STR,
            "generic_extraction_attempt_id": STR,
            "profile_snapshot_id": STR,
            "profile_material_hash": STR,
            "engine": STR,
            "extractor_version": STR,
            "config_hash": STR,
            "validation_mode": STR,
            "fingerprint_schema_version": STR,
            "extraction_fingerprint": STR,
        },
        {
            "input_asset_id": STR_OR_NONE,
            "calibration_profile_key": STR_OR_NONE,
            "calibration_profile_version": STR_OR_NONE,
            "calibration_profile_hash": STR_OR_NONE,
        },
        formats={
            "run_id": UUID_FORMAT,
            "run_source_item_id": UUID_FORMAT,
            "source_item_id": UUID_FORMAT,
            "input_asset_id": UUID_FORMAT,
            "generic_extraction_attempt_id": UUID_FORMAT,
            "profile_snapshot_id": UUID_FORMAT,
            "profile_material_hash": SHA256_FORMAT,
            "extractor_version": VERSION_FORMAT,
            "config_hash": SHA256_FORMAT,
            "calibration_profile_version": VERSION_FORMAT,
            "calibration_profile_hash": SHA256_FORMAT,
            "fingerprint_schema_version": VERSION_FORMAT,
            "extraction_fingerprint": SHA256_FORMAT,
        },
        choices={
            "engine": EXTRACTION_ENGINE_VALUES,
            "validation_mode": {"deterministic", "calibrated", "manual"},
        },
    ),
    ("evidence.finalize_requested", 1): _schema(
        {"run_id": STR},
        formats={"run_id": UUID_FORMAT},
    ),
    ("evidence.document_ready", 1): _schema(
        {
            "run_id": STR,
            "run_source_item_id": STR,
            "source_item_id": STR,
            "document_extraction_id": STR,
            "input_page_count": (int,),
            "coverage_manifest_hash": STR,
            "selected_evidence_manifest_hash": STR,
            "document_complete": (bool,),
        },
        formats={
            "run_id": UUID_FORMAT,
            "run_source_item_id": UUID_FORMAT,
            "source_item_id": UUID_FORMAT,
            "document_extraction_id": UUID_FORMAT,
            "coverage_manifest_hash": SHA256_FORMAT,
            "selected_evidence_manifest_hash": SHA256_FORMAT,
        },
        choices={"document_complete": {True}},
    ),
    ("evidence.other_ready", 1): _schema(
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
        formats={
            "run_id": UUID_FORMAT,
            "run_source_item_id": UUID_FORMAT,
            "source_item_id": UUID_FORMAT,
            "generic_extraction_attempt_id": UUID_FORMAT,
            "evidence_asset_id": UUID_FORMAT,
            "result_checksum": SHA256_FORMAT,
            "low_confidence_reasons_hash": SHA256_FORMAT,
            "calibration_profile_version": VERSION_FORMAT,
            "calibration_profile_hash": SHA256_FORMAT,
            "extraction_fingerprint": SHA256_FORMAT,
        },
        choices={
            "engine": EXTRACTION_ENGINE_VALUES,
            "validation_mode": {"deterministic", "calibrated", "manual"},
            "locator_type": {
                "document_block",
                "html_dom",
                "structured_path",
                "spreadsheet_cell",
                "hwpx_path",
                "hwp_conversion",
                "media_time",
                "image_region",
                "manual",
                "visualization",
            },
        },
    ),
    ("evidence.profile_decided", 1): _schema(
        {
            "profile_snapshot_id": STR,
            "decision_id": STR,
            "decision": STR,
            "profile_material_hash": STR,
        },
        formats={
            "profile_snapshot_id": UUID_FORMAT,
            "decision_id": UUID_FORMAT,
            "profile_material_hash": SHA256_FORMAT,
        },
        choices={"decision": {"approved", "retired"}},
    ),
    ("evidence.review_decided", 1): _schema(
        {
            "evidence_audit_snapshot_id": STR,
            "review_decision_id": STR,
            "review_subject_schema_version": STR,
            "review_subject_hash": STR,
            "request_key": STR,
            "decision": STR,
        },
        {"evidence_asset_id": STR_OR_NONE},
        formats={
            "evidence_audit_snapshot_id": UUID_FORMAT,
            "evidence_asset_id": UUID_FORMAT,
            "review_decision_id": UUID_FORMAT,
            "review_subject_schema_version": VERSION_FORMAT,
            "review_subject_hash": SHA256_FORMAT,
            "request_key": KEY_FORMAT,
        },
        choices={"decision": {"approved", "rejected"}},
    ),
    ("run.draft_requested", 1): _schema(
        {"run_id": STR},
        formats={"run_id": UUID_FORMAT},
    ),
    ("publication.scheduled_run_requested", 1): _schema(
        {"run_id": STR},
        formats={"run_id": UUID_FORMAT},
    ),
    ("publication.preflight_requested", 1): _schema(
        {
            "target_id": STR,
            "target_snapshot_id": STR,
            "target_config_hash": STR,
        },
        formats={
            "target_id": UUID_FORMAT,
            "target_snapshot_id": UUID_FORMAT,
            "target_config_hash": SHA256_FORMAT,
        },
    ),
    ("publishing.target_preflight.completed", 1): _schema(
        {
            "target_id": STR,
            "target_snapshot_id": STR,
            "target_config_hash": STR,
            "result_hash": STR,
            "passed": (bool,),
        },
        formats={
            "target_id": UUID_FORMAT,
            "target_snapshot_id": UUID_FORMAT,
            "target_config_hash": SHA256_FORMAT,
            "result_hash": SHA256_FORMAT,
        },
    ),
    ("publication.requested", 1): _schema(
        {"publication_attempt_id": STR},
        formats={"publication_attempt_id": UUID_FORMAT},
    ),
    ("publication.reconcile_requested", 1): _schema(
        {"publication_attempt_id": STR},
        formats={"publication_attempt_id": UUID_FORMAT},
    ),
    ("publication.reconcile_requested", 2): _schema(
        {
            "publication_attempt_id": STR,
            "reconcile_attempt_no": (int,),
        },
        formats={
            "publication_attempt_id": UUID_FORMAT,
            "reconcile_attempt_no": POSITIVE_INTEGER_FORMAT,
        },
    ),
    ("publishing.target_canary.requested", 1): _schema(
        {"canary_run_id": STR, "target_id": STR},
        formats={"canary_run_id": UUID_FORMAT, "target_id": UUID_FORMAT},
    ),
    ("publishing.target_disconnect.requested", 1): _schema(
        {"decision_id": STR, "target_id": STR},
        formats={"decision_id": UUID_FORMAT, "target_id": UUID_FORMAT},
    ),
    ("retention.expire_requested", 1): _schema(
        {"retention_batch_id": STR},
        formats={"retention_batch_id": UUID_FORMAT},
    ),
    ("delivery.delete_requested", 1): _schema(
        {"public_delivery_asset_id": STR, "expected_lease_generation": (int,)},
        formats={"public_delivery_asset_id": UUID_FORMAT},
    ),
    ("media.reconcile_requested", 1): _schema(
        {
            "remote_media_id": STR,
            "publication_attempt_id": STR,
            "publication_intent_id": STR,
        },
        formats={
            "remote_media_id": UUID_FORMAT,
            "publication_attempt_id": UUID_FORMAT,
            "publication_intent_id": UUID_FORMAT,
        },
    ),
}


def route_for(event_type: str, event_version: int) -> EventRoute | None:
    return EVENT_ROUTES.get((event_type, event_version))


def payload_schema_for(event_type: str, event_version: int) -> PayloadSchema | None:
    return EVENT_PAYLOAD_SCHEMAS.get((event_type, event_version))


def _channel_queue(channel: str) -> str:
    if channel == "wordpress":
        return "publish.wordpress"
    if channel == "blogger":
        return "publish.blogger"
    raise EventRoutingError("unsupported_publication_channel")


def queue_for(route: EventRoute, envelope: dict[str, Any]) -> str:
    event_type = envelope["event_type"]
    payload = envelope["payload"]
    try:
        if event_type == "run.requested":
            from apps.collection.models import CollectionRun

            topic_code = CollectionRun.objects.values_list("topic_code", flat=True).get(
                id=payload["run_id"]
            )
            if topic_code == "semiconductor_news":
                return "collect.semiconductor"
            if topic_code == "housing_subscription":
                return "collect.housing"
            raise EventRoutingError("unsupported_collection_topic")
        if event_type == "publication.preflight_requested":
            from apps.publishing.models import PublicationTarget

            channel = PublicationTarget.objects.values_list("channel", flat=True).get(
                id=payload["target_id"]
            )
            return _channel_queue(channel)
        if event_type == "publication.requested":
            from apps.publishing.models import PublicationAttempt

            channel = PublicationAttempt.objects.values_list(
                "publication__target__channel", flat=True
            ).get(id=payload["publication_attempt_id"])
            return _channel_queue(channel)
        if event_type == "publishing.target_canary.requested":
            from apps.publishing.models import TargetCanaryRun

            channel = TargetCanaryRun.objects.values_list(
                "target__channel", flat=True
            ).get(id=payload["canary_run_id"])
            return _channel_queue(channel)
        if event_type == "publishing.target_disconnect.requested":
            from apps.publishing.models import TargetDisconnectDecision

            channel = TargetDisconnectDecision.objects.values_list(
                "target__channel", flat=True
            ).get(id=payload["decision_id"])
            return _channel_queue(channel)
    except (KeyError, ValueError, TypeError) as exc:
        raise EventRoutingError("invalid_routing_identity") from exc
    except ObjectDoesNotExist as exc:
        raise EventRoutingError("routing_entity_not_found") from exc
    return route.queue
