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
SOURCE_CHANGE_VALUES = frozenset(
    {
        "new_version",
        "corrected",
        "retracted",
        "unavailable",
        "restored",
    }
)
SOURCE_COLLECTION_MAX_ATTEMPTS = 5

EVENT_ROUTES: dict[EventKey, EventRoute] = {
    ("source.check_requested", 1): EventRoute(
        "source-check",
        "source.check",
        "apps.topics.tasks.check_source_snapshot",
        (
            "source_id",
            "source_snapshot_id",
            "source_config_hash",
            "check_id",
        ),
        (
            "apps.topics.tasks."
            "finalize_source_check_delivery_failure"
        ),
        (
            "source_id",
            "source_snapshot_id",
            "source_config_hash",
            "check_id",
        ),
        max_attempts=5,
    ),
    ("run.requested", 1): EventRoute(
        "collection-run",
        "collect.housing",
        "apps.collection.tasks.execute_collection_run",
        ("run_id",),
        (
            "apps.collection.tasks."
            "finalize_collection_run_delivery_failure"
        ),
        ("run_id",),
        max_attempts=4,
    ),
    ("source.collect_requested", 1): EventRoute(
        "source-collection",
        "collect.housing",
        "apps.collection.tasks.execute_source_collection_attempt",
        ("source_collection_attempt_id",),
        (
            "apps.collection.tasks."
            "finalize_source_collection_attempt_delivery_failure"
        ),
        ("source_collection_attempt_id",),
        max_attempts=SOURCE_COLLECTION_MAX_ATTEMPTS,
    ),
    ("run.collection_finalize_requested", 1): EventRoute(
        "collection-finalize",
        "collect.housing",
        "apps.collection.tasks.finalize_collection_run",
        ("run_id",),
        (
            "apps.collection.tasks."
            "finalize_collection_run_delivery_failure"
        ),
        ("run_id",),
        max_attempts=4,
    ),
    ("source.item_changed", 1): EventRoute(
        "source-item-change",
        "source.change",
        "apps.collection.tasks.route_source_item_change",
        (
            "source_collection_attempt_id",
            "run_id",
            "run_source_item_id",
            "source_item_id",
            "change_kind",
        ),
        max_attempts=4,
    ),
    ("run.evidence_requested", 1): EventRoute(
        "run-evidence",
        "extract.fanout",
        "apps.evidence.tasks.process_run_evidence",
        ("run_id",),
        "apps.evidence.tasks.finalize_run_evidence_fanout_failure",
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
        "apps.evidence.tasks.finalize_run_evidence_wake_failure",
        ("run_id",),
    ),
    ("evidence.document_ready", 1): EventRoute(
        "document-ready-finalize",
        "extract.document",
        "apps.evidence.tasks.consume_document_ready",
        (
            "run_id",
            "run_source_item_id",
            "source_item_id",
            "document_extraction_id",
            "input_page_count",
            "coverage_manifest_hash",
            "selected_evidence_manifest_hash",
            "document_complete",
            "input_asset_id",
            "input_checksum",
            "input_fingerprint",
            "routing_manifest_hash",
        ),
        "apps.evidence.tasks.finalize_document_ready_wake_failure",
        ("document_extraction_id",),
    ),
    ("evidence.other_ready", 1): EventRoute(
        "other-ready-finalize",
        "extract.document",
        "apps.evidence.tasks.consume_other_ready",
        (
            "run_id",
            "run_source_item_id",
            "source_item_id",
            "evidence_asset_id",
            "generic_extraction_attempt_id",
            "engine",
            "locator_type",
            "validation_mode",
            "result_checksum",
            "low_confidence_reasons_hash",
            "calibration_profile_key",
            "calibration_profile_version",
            "calibration_profile_hash",
            "extraction_fingerprint",
            "input_asset_id",
            "parent_asset_id",
            "profile_snapshot_id",
            "profile_material_hash",
            "extractor_version",
            "config_hash",
            "evidence_content_hash",
            "review_subject_hash",
            "locator_hash",
            "evidence_asset_ids",
            "evidence_count",
            "evidence_manifest_hash",
        ),
        "apps.evidence.tasks.finalize_other_ready_wake_failure",
        ("generic_extraction_attempt_id",),
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
    ("run.evidence_ready", 1): EventRoute(
        "evidence-ready-clustering",
        "editorial",
        "apps.editorial.tasks.request_run_clustering",
        ("run_id",),
        "apps.editorial.tasks.finalize_editorial_run_delivery_failure",
        ("run_id",),
    ),
    ("editorial.cluster_requested", 1): EventRoute(
        "event-clustering",
        "editorial",
        "apps.editorial.tasks.cluster_and_verify_run",
        ("run_id",),
        "apps.editorial.tasks.finalize_editorial_run_delivery_failure",
        ("run_id",),
    ),
    ("editorial.generate_requested", 1): EventRoute(
        "verified-article-draft",
        "editorial",
        "apps.editorial.tasks.generate_verification_draft",
        (
            "verification_id",
            "run_id",
            "verification_ids",
            "generation_manifest_hash",
        ),
        "apps.editorial.tasks.finalize_editorial_generation_delivery_failure",
        ("run_id", "verification_id"),
    ),
    ("editorial.revalidate_requested", 1): EventRoute(
        "manual-article-revalidation",
        "editorial",
        "apps.editorial.tasks.revalidate_manual_revision",
        (
            "article_id",
            "article_revision_id",
            "editorial_policy_snapshot_id",
            "editorial_policy_material_hash",
            "verification_manifest_hash",
            "input_evidence_manifest_hash",
            "excluded_material_manifest_hash",
        ),
        "apps.editorial.tasks.finalize_manual_revalidation_delivery_failure",
        ("article_id", "article_revision_id"),
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
        "apps.publishing.tasks.finalize_publication_delivery_failure",
        ("publication_attempt_id",),
    ),
    ("publication.reconcile_requested", 1): EventRoute(
        "publication-reconcile",
        "reconcile",
        "apps.publishing.tasks.reconcile_publication_attempt",
        ("publication_attempt_id",),
        "apps.publishing.tasks.finalize_publication_reconcile_failure",
        ("publication_attempt_id",),
    ),
    ("publication.reconcile_requested", 2): EventRoute(
        "publication-reconcile",
        "reconcile",
        "apps.publishing.tasks.reconcile_publication_attempt",
        ("publication_attempt_id", "reconcile_attempt_no"),
        "apps.publishing.tasks.finalize_publication_reconcile_failure",
        ("publication_attempt_id",),
    ),
    ("publishing.target_canary.requested", 1): EventRoute(
        "target-canary",
        "publish.wordpress",
        "apps.publishing.tasks.run_target_canary",
        ("canary_run_id",),
        max_attempts=2,
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
    ("source.check_requested", 1): _schema(
        {
            "source_id": STR,
            "source_snapshot_id": STR,
            "source_config_hash": STR,
            "check_id": STR,
        },
        formats={
            "source_id": UUID_FORMAT,
            "source_snapshot_id": UUID_FORMAT,
            "source_config_hash": SHA256_FORMAT,
            "check_id": UUID_FORMAT,
        },
    ),
    ("run.requested", 1): _schema(
        {"run_id": STR},
        formats={"run_id": UUID_FORMAT},
    ),
    ("source.collect_requested", 1): _schema(
        {"source_collection_attempt_id": STR},
        formats={
            "source_collection_attempt_id": UUID_FORMAT,
        },
    ),
    ("run.collection_finalize_requested", 1): _schema(
        {"run_id": STR},
        formats={"run_id": UUID_FORMAT},
    ),
    ("source.item_changed", 1): _schema(
        {
            "source_collection_attempt_id": STR,
            "run_id": STR,
            "run_source_item_id": STR,
            "source_item_id": STR,
            "change_kind": STR,
        },
        formats={
            "source_collection_attempt_id": UUID_FORMAT,
            "run_id": UUID_FORMAT,
            "run_source_item_id": UUID_FORMAT,
            "source_item_id": UUID_FORMAT,
        },
        choices={"change_kind": SOURCE_CHANGE_VALUES},
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
        {
            "input_asset_id": STR_OR_NONE,
            "input_checksum": STR,
            "input_fingerprint": STR_OR_NONE,
            "routing_manifest_hash": STR,
        },
        formats={
            "run_id": UUID_FORMAT,
            "run_source_item_id": UUID_FORMAT,
            "source_item_id": UUID_FORMAT,
            "document_extraction_id": UUID_FORMAT,
            "coverage_manifest_hash": SHA256_FORMAT,
            "selected_evidence_manifest_hash": SHA256_FORMAT,
            "input_asset_id": UUID_FORMAT,
            "input_checksum": SHA256_FORMAT,
            "input_fingerprint": SHA256_FORMAT,
            "routing_manifest_hash": SHA256_FORMAT,
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
            "input_asset_id": STR_OR_NONE,
            "parent_asset_id": STR_OR_NONE,
            "profile_snapshot_id": STR_OR_NONE,
            "profile_material_hash": STR_OR_NONE,
            "extractor_version": STR_OR_NONE,
            "config_hash": STR_OR_NONE,
            "evidence_content_hash": STR_OR_NONE,
            "review_subject_hash": STR_OR_NONE,
            "locator_hash": STR_OR_NONE,
            "evidence_asset_ids": (list, type(None)),
            "evidence_count": (int, type(None)),
            "evidence_manifest_hash": STR_OR_NONE,
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
            "input_asset_id": UUID_FORMAT,
            "parent_asset_id": UUID_FORMAT,
            "profile_snapshot_id": UUID_FORMAT,
            "profile_material_hash": SHA256_FORMAT,
            "config_hash": SHA256_FORMAT,
            "evidence_content_hash": SHA256_FORMAT,
            "review_subject_hash": SHA256_FORMAT,
            "locator_hash": SHA256_FORMAT,
            "evidence_count": POSITIVE_INTEGER_FORMAT,
            "evidence_manifest_hash": SHA256_FORMAT,
        },
        choices={
            "engine": {
                "html_parser",
                "structured_parser",
                "spreadsheet_parser",
                "hwpx_parser",
                "legacy_hwp_converter",
                "browser_capture",
                "media_parser",
                "manual_entry",
            },
            "validation_mode": {"deterministic", "calibrated", "manual"},
            "locator_type": {
                "document_block",
                "visualization",
                "html_dom",
                "structured_path",
                "spreadsheet_cell",
                "hwpx_path",
                "hwp_conversion",
                "image_region",
                "media_time",
                "manual",
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
    ("run.evidence_ready", 1): _schema(
        {"run_id": STR},
        formats={"run_id": UUID_FORMAT},
    ),
    ("editorial.cluster_requested", 1): _schema(
        {"run_id": STR},
        formats={"run_id": UUID_FORMAT},
    ),
    ("editorial.generate_requested", 1): _schema(
        {
            "verification_id": STR,
            "run_id": STR,
            "verification_ids": (list,),
            "generation_manifest_hash": STR,
        },
        formats={
            "verification_id": UUID_FORMAT,
            "run_id": UUID_FORMAT,
            "generation_manifest_hash": SHA256_FORMAT,
        },
    ),
    ("editorial.revalidate_requested", 1): _schema(
        {
            "article_id": STR,
            "article_revision_id": STR,
            "editorial_policy_snapshot_id": STR,
            "editorial_policy_material_hash": STR,
            "verification_manifest_hash": STR,
            "input_evidence_manifest_hash": STR,
            "excluded_material_manifest_hash": STR,
        },
        formats={
            "article_id": UUID_FORMAT,
            "article_revision_id": UUID_FORMAT,
            "editorial_policy_snapshot_id": UUID_FORMAT,
            "editorial_policy_material_hash": SHA256_FORMAT,
            "verification_manifest_hash": SHA256_FORMAT,
            "input_evidence_manifest_hash": SHA256_FORMAT,
            "excluded_material_manifest_hash": SHA256_FORMAT,
        },
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


def _collection_queue(topic_code: str) -> str:
    if topic_code == "semiconductor_news":
        return "collect.semiconductor"
    if topic_code == "housing_subscription":
        return "collect.housing"
    raise EventRoutingError("unsupported_collection_topic")


def queue_for(route: EventRoute, envelope: dict[str, Any]) -> str:
    event_type = envelope["event_type"]
    payload = envelope["payload"]
    try:
        if event_type in {
            "run.requested",
            "run.collection_finalize_requested",
        }:
            from apps.collection.models import CollectionRun

            topic_code = CollectionRun.objects.values_list(
                "topic_code",
                flat=True,
            ).get(id=payload["run_id"])
            return _collection_queue(topic_code)
        if event_type == "source.collect_requested":
            from apps.collection.models import SourceCollectionAttempt

            topic_code = SourceCollectionAttempt.objects.values_list(
                "run__topic_code",
                flat=True,
            ).get(id=payload["source_collection_attempt_id"])
            return _collection_queue(topic_code)
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
