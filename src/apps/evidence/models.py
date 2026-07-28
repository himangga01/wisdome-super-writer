from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from wisdome_writer.domain.models import TimestampedUUIDModel


sha256_validator = RegexValidator(r"^[a-f0-9]{64}$", "Expected a lowercase SHA-256 digest")


class ExtractionEngine(models.TextChoices):
    NATIVE_PDF = "native_pdf", "Native PDF"
    PADDLEOCR = "paddleocr_ppstructurev3", "PaddleOCR PP-StructureV3"
    HTML = "html_parser", "HTML parser"
    STRUCTURED = "structured_parser", "Structured parser"
    SPREADSHEET = "spreadsheet_parser", "Spreadsheet parser"
    HWPX = "hwpx_parser", "HWPX parser"
    LEGACY_HWP = "legacy_hwp_converter", "Legacy HWP converter"
    BROWSER_CAPTURE = "browser_capture", "Browser capture"
    MEDIA = "media_parser", "Media parser"
    MANUAL = "manual_entry", "Manual entry"


class ExtractionState(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    LOW_CONFIDENCE = "low_confidence", "Low confidence"
    FAILED = "failed", "Failed"


class ProfileApprovalState(models.TextChoices):
    DRAFT = "draft", "Draft"
    APPROVED = "approved", "Approved"
    RETIRED = "retired", "Retired"


class GenericValidationMode(models.TextChoices):
    DETERMINISTIC = "deterministic", "Deterministic"
    CALIBRATED = "calibrated", "Calibrated"
    MANUAL = "manual", "Manual"


class DocumentInputKind(models.TextChoices):
    PDF = "pdf", "PDF"
    STANDALONE_IMAGE = "standalone_image", "Standalone image"


class EvidenceDerivationType(models.TextChoices):
    RAW = "raw", "Raw"
    DOCUMENT = "document_derived", "Document derived"
    OTHER = "other_derived", "Other derived"
    VISUALIZATION = "visualization_derived", "Visualization derived"


class EvidenceKind(models.TextChoices):
    TEXT = "text", "Text"
    TABLE = "table", "Table"
    PDF = "pdf", "PDF"
    IMAGE = "image", "Image"
    CHART = "chart", "Chart"
    SPREADSHEET = "spreadsheet", "Spreadsheet"
    SCREENSHOT = "screenshot", "Screenshot"
    ATTACHMENT = "attachment", "Attachment"


class LocatorType(models.TextChoices):
    DOCUMENT_BLOCK = "document_block", "Document block"
    HTML_DOM = "html_dom", "HTML DOM"
    STRUCTURED_PATH = "structured_path", "Structured path"
    SPREADSHEET_CELL = "spreadsheet_cell", "Spreadsheet cell"
    HWPX_PATH = "hwpx_path", "HWPX path"
    HWP_CONVERSION = "hwp_conversion", "HWP conversion"
    MEDIA_TIME = "media_time", "Media time"
    IMAGE_REGION = "image_region", "Image region"
    MANUAL = "manual", "Manual"
    VISUALIZATION = "visualization", "Visualization"


class RightsStatus(models.TextChoices):
    ALLOWED = "allowed", "Allowed"
    ATTRIBUTION_REQUIRED = "attribution_required", "Attribution required"
    INTERNAL_ONLY = "internal_analysis_only", "Internal analysis only"
    UNKNOWN = "unknown", "Unknown"
    PROHIBITED = "prohibited", "Prohibited"


class ReviewState(models.TextChoices):
    PENDING = "pending", "Pending"
    PASSED = "passed", "Passed"
    REJECTED = "rejected", "Rejected"
    MANUAL_REQUIRED = "manual_required", "Manual required"


class UUIDModel(TimestampedUUIDModel):
    class Meta:
        abstract = True


class ExtractionProfileSnapshot(UUIDModel):
    profile_key = models.CharField(max_length=160)
    profile_version = models.CharField(max_length=80)
    engine = models.CharField(max_length=64, choices=ExtractionEngine.choices)
    extractor_version = models.CharField(max_length=80)
    package_version = models.CharField(max_length=120, null=True, blank=True)
    runtime_version = models.CharField(max_length=120, null=True, blank=True)
    pipeline_name = models.CharField(max_length=120, null=True, blank=True)
    implementation_manifest_hash = models.CharField(max_length=64, validators=[sha256_validator])
    approval_state = models.CharField(
        max_length=16, choices=ProfileApprovalState.choices, default=ProfileApprovalState.DRAFT
    )
    decision_version = models.PositiveIntegerField(default=0)
    latest_decision = models.ForeignKey(
        "ExtractionProfileDecision", null=True, blank=True, on_delete=models.PROTECT,
        related_name="current_for_profiles",
    )
    config = models.JSONField(default=dict)
    config_hash = models.CharField(max_length=64, validators=[sha256_validator])
    validation_mode = models.CharField(
        max_length=20, choices=GenericValidationMode.choices, null=True, blank=True
    )
    calibration_profile_key = models.CharField(max_length=160, null=True, blank=True)
    calibration_profile_version = models.CharField(max_length=80, null=True, blank=True)
    calibration_manifest_object_key = models.CharField(max_length=1024, null=True, blank=True)
    calibration_manifest_object_version = models.CharField(max_length=256, null=True, blank=True)
    calibration_profile_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    model_manifest = models.JSONField(null=True, blank=True)
    model_manifest_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    profile_material_hash = models.CharField(max_length=64, validators=[sha256_validator])
    verification_report_object_key = models.CharField(max_length=1024, null=True, blank=True)
    verification_report_object_version = models.CharField(max_length=256, null=True, blank=True)
    verification_report_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("profile_key", "profile_version")
        constraints = [
            models.UniqueConstraint(fields=("profile_key", "profile_version"), name="uq_profile_key_version"),
            models.UniqueConstraint(fields=("engine", "profile_material_hash"), name="uq_profile_engine_material"),
        ]

    def clean(self) -> None:
        super().clean()
        paddle = self.engine == ExtractionEngine.PADDLEOCR
        if paddle:
            if self.package_version != "3.7.0":
                raise ValidationError({"package_version": "PaddleOCR package version must be 3.7.0"})
            if not (self.runtime_version or "").startswith("3."):
                raise ValidationError({"runtime_version": "PaddlePaddle runtime must be pinned to 3.x"})
            if self.pipeline_name != "PPStructureV3":
                raise ValidationError({"pipeline_name": "PaddleOCR pipeline must be PPStructureV3"})
            if not self.model_manifest or not self.model_manifest_hash:
                raise ValidationError({"model_manifest": "A local model manifest is required"})
        elif self.validation_mode == GenericValidationMode.CALIBRATED:
            required = (
                self.calibration_profile_key,
                self.calibration_profile_version,
                self.calibration_profile_hash,
            )
            if not all(required):
                raise ValidationError("Calibrated profiles require key, version and manifest hash")

    def __str__(self) -> str:
        return f"{self.profile_key}@{self.profile_version} ({self.approval_state})"


class ExtractionProfileDecision(UUIDModel):
    class Decision(models.TextChoices):
        APPROVED = "approved", "Approved"
        RETIRED = "retired", "Retired"

    profile_snapshot = models.ForeignKey(
        ExtractionProfileSnapshot, on_delete=models.PROTECT, related_name="decisions"
    )
    version = models.PositiveIntegerField()
    decision = models.CharField(max_length=16, choices=Decision.choices)
    expected_material_hash = models.CharField(max_length=64, validators=[sha256_validator])
    supersedes_decision = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64, validators=[sha256_validator])
    decision_hash = models.CharField(max_length=64, validators=[sha256_validator])
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decided_at = models.DateTimeField()
    reason = models.CharField(max_length=500)

    class Meta:
        ordering = ("profile_snapshot_id", "version")
        constraints = [
            models.UniqueConstraint(fields=("profile_snapshot", "version"), name="uq_profile_decision_version"),
            models.UniqueConstraint(fields=("profile_snapshot", "request_key"), name="uq_profile_decision_request"),
        ]


class DocumentExtraction(UUIDModel):
    run_source_item = models.ForeignKey(
        "collection.RunSourceItem", on_delete=models.PROTECT, related_name="document_extractions"
    )
    source_item = models.ForeignKey(
        "collection.SourceItem", on_delete=models.PROTECT, related_name="document_extractions"
    )
    input_asset = models.ForeignKey(
        "EvidenceAsset", null=True, blank=True, on_delete=models.PROTECT,
        related_name="input_to_document_extractions",
    )
    input_object_key = models.CharField(max_length=1024)
    input_object_version = models.CharField(max_length=256)
    input_kind = models.CharField(max_length=24, choices=DocumentInputKind.choices)
    input_fingerprint = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        validators=[sha256_validator],
    )
    input_mime_type = models.CharField(max_length=160)
    input_frame_count = models.PositiveIntegerField(null=True, blank=True)
    input_checksum = models.CharField(max_length=64, validators=[sha256_validator])
    input_page_count = models.PositiveIntegerField()
    expected_page_indices = models.JSONField(default=list)
    covered_page_indices = models.JSONField(default=list)
    routing_manifest = models.JSONField(default=dict)
    coverage_manifest_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    selected_evidence_manifest_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    document_complete = models.BooleanField(default=False)
    state = models.CharField(max_length=24, choices=ExtractionState.choices, default=ExtractionState.QUEUED)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=120, null=True, blank=True)
    error_detail_redacted = models.CharField(max_length=1000, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=("run_source_item", "state")),
            models.Index(fields=("source_item", "created_at")),
        ]

    def clean(self) -> None:
        super().clean()
        expected = list(range(self.input_page_count))
        if self.expected_page_indices != expected:
            raise ValidationError({"expected_page_indices": "Must be the complete 0-based page range"})
        if self.input_kind == DocumentInputKind.STANDALONE_IMAGE:
            if self.input_page_count != 1 or self.input_frame_count != 1:
                raise ValidationError("Standalone images must be a single-frame virtual one-page document")
        elif self.input_frame_count is not None:
            raise ValidationError({"input_frame_count": "PDF frame count must be null"})
        if self.document_complete and self.covered_page_indices != expected:
            raise ValidationError({"document_complete": "Complete documents must cover every page"})


class ExtractionRun(UUIDModel):
    document_extraction = models.ForeignKey(
        DocumentExtraction, on_delete=models.PROTECT, related_name="extraction_runs"
    )
    retry_of_run = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="retries"
    )
    extraction_profile_snapshot = models.ForeignKey(
        ExtractionProfileSnapshot, on_delete=models.PROTECT, related_name="document_runs"
    )
    engine = models.CharField(max_length=64, choices=ExtractionEngine.choices)
    page_set_hash = models.CharField(max_length=64, validators=[sha256_validator])
    fingerprint_schema_version = models.CharField(max_length=16, default="v1")
    extraction_fingerprint = models.CharField(max_length=64, validators=[sha256_validator])
    requested_page_indices = models.JSONField(default=list)
    processed_page_indices = models.JSONField(default=list)
    profile_key = models.CharField(max_length=160)
    profile_version = models.CharField(max_length=80)
    config_hash = models.CharField(max_length=64, validators=[sha256_validator])
    profile_material_hash = models.CharField(max_length=64, validators=[sha256_validator])
    package_version = models.CharField(max_length=120)
    runtime_version = models.CharField(max_length=120)
    pipeline_name = models.CharField(max_length=120, null=True, blank=True)
    model_manifest = models.JSONField(null=True, blank=True)
    model_manifest_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    language_profile = models.CharField(max_length=80, null=True, blank=True)
    device_type = models.CharField(max_length=24, null=True, blank=True)
    state = models.CharField(max_length=24, choices=ExtractionState.choices, default=ExtractionState.QUEUED)
    low_confidence_reasons = models.JSONField(null=True, blank=True)
    low_confidence_reasons_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    low_confidence_reasons_object_key = models.CharField(max_length=1024, null=True, blank=True)
    low_confidence_reasons_object_version = models.CharField(max_length=256, null=True, blank=True)
    result_object_key = models.CharField(max_length=1024, null=True, blank=True)
    result_checksum = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    peak_memory_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=120, null=True, blank=True)
    error_detail_redacted = models.CharField(max_length=1000, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("document_extraction", "extraction_fingerprint"),
                name="uq_document_extraction_fingerprint",
            )
        ]
        indexes = [models.Index(fields=("document_extraction", "state"))]

    def clean(self) -> None:
        super().clean()
        requested = sorted(set(self.requested_page_indices))
        processed = sorted(set(self.processed_page_indices))
        if requested != self.requested_page_indices or processed != self.processed_page_indices:
            raise ValidationError("Page lists must be sorted and duplicate-free")
        if self.state in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE):
            if requested != processed or not self.result_checksum:
                raise ValidationError("Completed child runs must process exactly the requested page set")


class GenericExtractionAttempt(UUIDModel):
    run_source_item = models.ForeignKey(
        "collection.RunSourceItem", on_delete=models.PROTECT, related_name="generic_extraction_attempts"
    )
    source_item = models.ForeignKey(
        "collection.SourceItem", on_delete=models.PROTECT, related_name="generic_extraction_attempts"
    )
    input_asset = models.ForeignKey(
        "EvidenceAsset", null=True, blank=True, on_delete=models.PROTECT,
        related_name="input_to_generic_attempts",
    )
    evidence_asset = models.OneToOneField(
        "EvidenceAsset", null=True, blank=True, on_delete=models.PROTECT,
        related_name="producing_generic_attempt",
    )
    extraction_profile_snapshot = models.ForeignKey(
        ExtractionProfileSnapshot, on_delete=models.PROTECT, related_name="generic_attempts"
    )
    profile_material_hash = models.CharField(max_length=64, validators=[sha256_validator])
    engine = models.CharField(max_length=64, choices=ExtractionEngine.choices)
    extractor_version = models.CharField(max_length=80)
    config_hash = models.CharField(max_length=64, validators=[sha256_validator])
    validation_mode = models.CharField(max_length=20, choices=GenericValidationMode.choices)
    calibration_profile_key = models.CharField(max_length=160, null=True, blank=True)
    calibration_profile_version = models.CharField(max_length=80, null=True, blank=True)
    calibration_profile_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    fingerprint_schema_version = models.CharField(max_length=16, default="v1")
    extraction_fingerprint = models.CharField(max_length=64, validators=[sha256_validator])
    state = models.CharField(max_length=24, choices=ExtractionState.choices, default=ExtractionState.QUEUED)
    result_checksum = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    low_confidence_reasons_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    low_confidence_reasons_object_key = models.CharField(max_length=1024, null=True, blank=True)
    low_confidence_reasons_object_version = models.CharField(max_length=256, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=120, null=True, blank=True)
    error_detail_redacted = models.CharField(max_length=1000, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("run_source_item", "extraction_fingerprint"),
                name="uq_generic_attempt_fingerprint",
            )
        ]

    def clean(self) -> None:
        super().clean()
        calibrated = self.validation_mode == GenericValidationMode.CALIBRATED
        calibration = (
            self.calibration_profile_key,
            self.calibration_profile_version,
            self.calibration_profile_hash,
        )
        if calibrated != all(calibration):
            raise ValidationError("Calibration material is required only for calibrated extraction")
        if self.state == ExtractionState.LOW_CONFIDENCE and not calibrated:
            raise ValidationError("Only calibrated generic extraction can be low-confidence")
        if self.state in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE) and not self.result_checksum:
            raise ValidationError({"result_checksum": "Completed attempts require a result checksum"})


class EvidenceAsset(UUIDModel):
    source_item = models.ForeignKey(
        "collection.SourceItem", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="evidence_assets",
    )
    origin_run_source_item = models.ForeignKey(
        "collection.RunSourceItem", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="originated_evidence_assets",
    )
    derivation_type = models.CharField(max_length=32, choices=EvidenceDerivationType.choices)
    document_extraction = models.ForeignKey(
        DocumentExtraction, null=True, blank=True, on_delete=models.SET_NULL, related_name="evidence_assets"
    )
    extraction_run = models.ForeignKey(
        ExtractionRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="evidence_assets"
    )
    generic_extraction_attempt = models.ForeignKey(
        GenericExtractionAttempt, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="derived_evidence_assets",
    )
    visualization_render_id = models.UUIDField(null=True, blank=True)
    parent_asset = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="derived_assets"
    )
    kind = models.CharField(max_length=24, choices=EvidenceKind.choices)
    locator_type = models.CharField(max_length=32, choices=LocatorType.choices, null=True, blank=True)
    locator = models.JSONField(default=dict)
    object_key = models.CharField(max_length=1024, null=True, blank=True)
    object_version = models.CharField(max_length=256, null=True, blank=True)
    mime_type = models.CharField(max_length=160, null=True, blank=True)
    byte_size = models.PositiveBigIntegerField(null=True, blank=True)
    checksum = models.CharField(max_length=64, validators=[sha256_validator], null=True, blank=True)
    extracted_text = models.TextField(null=True, blank=True)
    structured_data = models.JSONField(null=True, blank=True)
    extraction_method = models.CharField(max_length=120, null=True, blank=True)
    extractor_version = models.CharField(max_length=80, null=True, blank=True)
    extraction_config_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    validation_mode = models.CharField(
        max_length=20, choices=GenericValidationMode.choices, null=True, blank=True
    )
    extraction_result_checksum = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    calibration_profile_key = models.CharField(max_length=160, null=True, blank=True)
    calibration_profile_version = models.CharField(max_length=80, null=True, blank=True)
    calibration_profile_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    confidence = models.DecimalField(
        max_digits=8, decimal_places=7, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    confidence_detail = models.JSONField(null=True, blank=True)
    low_confidence_reasons = models.JSONField(default=list)
    rights_status = models.CharField(max_length=32, choices=RightsStatus.choices, default=RightsStatus.UNKNOWN)
    rights_basis_url = models.URLField(max_length=2048, null=True, blank=True)
    attribution_text = models.TextField(null=True, blank=True)
    alt_text = models.TextField(null=True, blank=True)
    review_state = models.CharField(max_length=24, choices=ReviewState.choices, default=ReviewState.PENDING)
    manual_review_required = models.BooleanField(default=False)
    manual_reviewed_at = models.DateTimeField(null=True, blank=True)
    manual_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_evidence_assets",
    )
    evidence_content_hash = models.CharField(max_length=64, validators=[sha256_validator])
    review_subject_schema_version = models.CharField(max_length=16, default="v1")
    review_subject_hash = models.CharField(max_length=64, validators=[sha256_validator])
    latest_review_decision = models.ForeignKey(
        "EvidenceReviewDecision", null=True, blank=True, on_delete=models.PROTECT,
        related_name="current_for_evidence",
    )
    publishable = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=("source_item", "kind")),
            models.Index(fields=("origin_run_source_item", "publishable")),
            models.Index(fields=("document_extraction", "extraction_run")),
        ]

    def clean(self) -> None:
        super().clean()
        provenance = {
            EvidenceDerivationType.RAW: (False, False, False, False),
            EvidenceDerivationType.DOCUMENT: (True, True, False, False),
            EvidenceDerivationType.OTHER: (False, False, True, False),
            EvidenceDerivationType.VISUALIZATION: (False, False, False, True),
        }
        actual = (
            self.document_extraction_id is not None,
            self.extraction_run_id is not None,
            self.generic_extraction_attempt_id is not None,
            self.visualization_render_id is not None,
        )
        if provenance.get(self.derivation_type) != actual:
            raise ValidationError("Evidence provenance does not match derivation type")
        if self.derivation_type == EvidenceDerivationType.DOCUMENT:
            required_locator = {"page_index", "block_id", "block_type", "reading_order"}
            if self.locator_type != LocatorType.DOCUMENT_BLOCK:
                raise ValidationError({"locator_type": "Document evidence requires a document block locator"})
            if not isinstance(self.locator, dict) or not required_locator.issubset(self.locator):
                raise ValidationError({"locator": "Document evidence requires page, block and reading-order fields"})
            bbox = self.locator.get("bbox")
            polygon = self.locator.get("polygon")
            if not bbox and not polygon:
                raise ValidationError({"locator": "Document evidence requires a page-region bbox or polygon"})
        if self.derivation_type != EvidenceDerivationType.VISUALIZATION and (
            not self.source_item_id or not self.origin_run_source_item_id
        ):
            raise ValidationError("Single-source evidence requires source and origin run-source lineage")
        if self.derivation_type == EvidenceDerivationType.VISUALIZATION and (
            self.source_item_id or self.origin_run_source_item_id
        ):
            raise ValidationError("Visualization evidence uses its input joins, not a single source")
        calibrated = self.validation_mode == GenericValidationMode.CALIBRATED
        if calibrated and (self.confidence is None or not self.calibration_profile_hash):
            raise ValidationError("Calibrated evidence requires confidence and calibration provenance")
        if self.validation_mode in (GenericValidationMode.DETERMINISTIC, GenericValidationMode.MANUAL):
            if self.confidence is not None or self.calibration_profile_hash:
                raise ValidationError("Deterministic/manual evidence cannot contain numeric confidence")
        if self.publishable:
            if self.rights_status not in (RightsStatus.ALLOWED, RightsStatus.ATTRIBUTION_REQUIRED):
                raise ValidationError({"publishable": "Rights status blocks publishing"})
            if not self.rights_basis_url:
                raise ValidationError({"rights_basis_url": "Publishable evidence requires a rights basis"})
            if self.rights_status == RightsStatus.ATTRIBUTION_REQUIRED and not self.attribution_text:
                raise ValidationError({"attribution_text": "Attribution text is required"})
            if self.kind in (EvidenceKind.IMAGE, EvidenceKind.CHART, EvidenceKind.SCREENSHOT) and not self.alt_text:
                raise ValidationError({"alt_text": "Visual evidence requires alt text"})
            if self.manual_review_required:
                raise ValidationError({"publishable": "Manual review is still required"})


class EvidenceAuditSnapshot(UUIDModel):
    original_evidence_asset_id = models.UUIDField(null=True, blank=True)
    source_identity_hash = models.CharField(max_length=64, validators=[sha256_validator])
    evidence_content_hash = models.CharField(max_length=64, validators=[sha256_validator])
    locator_hash = models.CharField(max_length=64, validators=[sha256_validator])
    provenance_type = models.CharField(max_length=24, choices=(
        ("raw", "Raw"), ("document", "Document"), ("generic", "Generic"),
    ))
    provenance_manifest_hash = models.CharField(max_length=64, validators=[sha256_validator])
    low_confidence_reasons_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    review_subject_schema_version = models.CharField(max_length=16, default="v1")
    review_subject_hash = models.CharField(max_length=64, validators=[sha256_validator])
    snapshot_hash = models.CharField(max_length=64, validators=[sha256_validator], unique=True)
    raw_purged_at = models.DateTimeField(null=True, blank=True)


class EvidenceReviewDecision(UUIDModel):
    class Decision(models.TextChoices):
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    evidence_asset = models.ForeignKey(
        EvidenceAsset, null=True, blank=True, on_delete=models.SET_NULL, related_name="review_decisions"
    )
    evidence_audit_snapshot = models.ForeignKey(
        EvidenceAuditSnapshot, on_delete=models.PROTECT, related_name="review_decisions"
    )
    decision_provenance_type = models.CharField(max_length=24, choices=(
        ("raw", "Raw"), ("document", "Document"), ("generic", "Generic"),
    ))
    review_subject_schema_version = models.CharField(max_length=16, default="v1")
    review_subject_hash = models.CharField(max_length=64, validators=[sha256_validator])
    extraction_run = models.ForeignKey(
        ExtractionRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="review_decisions"
    )
    generic_extraction_attempt = models.ForeignKey(
        GenericExtractionAttempt, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="review_decisions",
    )
    supersedes_decision = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64, validators=[sha256_validator])
    decision = models.CharField(max_length=16, choices=Decision.choices)
    reason = models.CharField(max_length=500)
    reviewer_admin = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decided_at = models.DateTimeField()

    class Meta:
        ordering = ("evidence_audit_snapshot_id", "decided_at")
        constraints = [
            models.UniqueConstraint(
                fields=("evidence_audit_snapshot", "request_key"), name="uq_evidence_review_request"
            )
        ]

    def clean(self) -> None:
        super().clean()
        document = self.extraction_run_id is not None
        generic = self.generic_extraction_attempt_id is not None
        expected = self.decision_provenance_type
        if expected == "document" and not (document and not generic):
            raise ValidationError("Document decisions require only an extraction run")
        if expected == "generic" and not (generic and not document):
            raise ValidationError("Generic decisions require only a generic attempt")
        if expected == "raw" and (document or generic):
            raise ValidationError("Raw decisions cannot reference an extraction attempt")
