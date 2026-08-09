from __future__ import annotations

import math
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from wisdome_writer.domain.models import TimestampedUUIDModel


sha256_validator = RegexValidator(r"^[a-f0-9]{64}$", "Expected a lowercase SHA-256 digest")


GENERIC_ENGINE_LOCATOR_MATRIX = {
    "html_parser": frozenset({"html_dom"}),
    "structured_parser": frozenset({"structured_path"}),
    "spreadsheet_parser": frozenset({"spreadsheet_cell"}),
    "hwpx_parser": frozenset({"hwpx_path"}),
    "legacy_hwp_converter": frozenset({"hwp_conversion"}),
    # Historical rows remain readable, but these engines are not in the active MVP catalog.
    "browser_capture": frozenset({"image_region"}),
    "media_parser": frozenset({"image_region", "media_time"}),
    "manual_entry": frozenset({"manual"}),
}


def _finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_bbox(value: object) -> bool:
    return bool(
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(_finite_number(item) for item in value)
        and float(value[2]) > float(value[0])
        and float(value[3]) > float(value[1])
    )


def _valid_polygon(value: object) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return False
    points: list[tuple[float, float]] = []
    for point in value:
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 2
            or not all(_finite_number(coordinate) for coordinate in point)
        ):
            return False
        points.append((float(point[0]), float(point[1])))
    return len(set(points)) >= 3


def validate_evidence_locator(locator_type: str | None, locator: object) -> None:
    if not locator_type or not isinstance(locator, dict) or locator.get("locator_type") != locator_type:
        raise ValidationError({"locator": "Locator type and locator payload must match"})
    allowed_fields = {
        "document_block": {
            "locator_type", "page_index", "block_id", "block_type", "reading_order",
            "bbox", "polygon",
        },
        "html_dom": {"locator_type", "css_selector", "xpath"},
        "structured_path": {"locator_type", "path_type", "path"},
        "spreadsheet_cell": {"locator_type", "sheet_name", "cell_range"},
        "hwpx_path": {
            "locator_type", "section_path", "paragraph_id", "table_id",
            "row_index", "column_index", "embedded_object_id",
        },
        "hwp_conversion": {
            "locator_type", "attempt_id", "generation", "nonce", "input_checksum",
            "input_byte_size", "output_pdf_checksum", "output_pdf_byte_size",
            "converter_manifest_hash", "sandbox_report_hash", "page_count",
        },
        "image_region": {"locator_type", "bbox", "polygon", "page_index", "frame_index"},
        "media_time": {"locator_type", "start_seconds", "end_seconds", "track_id"},
        "manual": {"locator_type", "entry_id", "actor_id", "source_url"},
    }
    allowed = allowed_fields.get(locator_type)
    if allowed is None or set(locator) - allowed:
        raise ValidationError({"locator": "Locator contains unsupported fields"})
    nonempty = lambda key: isinstance(locator.get(key), str) and bool(locator[key].strip())
    region = _valid_bbox(locator.get("bbox")) or _valid_polygon(locator.get("polygon"))
    if locator_type == "document_block":
        if not (
            type(locator.get("page_index")) is int
            and locator["page_index"] >= 0
            and nonempty("block_id")
            and nonempty("block_type")
            and type(locator.get("reading_order")) is int
            and locator["reading_order"] >= 0
            and region
        ):
            raise ValidationError({"locator": "Document block locator is incomplete"})
    elif locator_type == "html_dom":
        if nonempty("css_selector") == nonempty("xpath"):
            raise ValidationError({"locator": "HTML locator requires exactly one selector or XPath"})
    elif locator_type == "structured_path":
        if locator.get("path_type") not in {"json_pointer", "xpath", "jsonpath"} or not nonempty("path"):
            raise ValidationError({"locator": "Structured locator requires path type and path"})
    elif locator_type == "spreadsheet_cell":
        if not nonempty("sheet_name") or not nonempty("cell_range"):
            raise ValidationError({"locator": "Spreadsheet locator requires sheet and cell range"})
    elif locator_type == "hwpx_path":
        paragraph = nonempty("paragraph_id")
        table = nonempty("table_id")
        embedded = nonempty("embedded_object_id")
        row = locator.get("row_index")
        column = locator.get("column_index")
        paragraph_variant = paragraph and not table and not embedded and row is None and column is None
        table_variant = (
            table and not paragraph and not embedded
            and type(row) is int and row >= 0
            and type(column) is int and column >= 0
        )
        embedded_variant = embedded and not paragraph and not table and row is None and column is None
        if not nonempty("section_path") or sum(
            bool(item) for item in (paragraph_variant, table_variant, embedded_variant)
        ) != 1:
            raise ValidationError({"locator": "HWPX locator is incomplete"})
    elif locator_type == "hwp_conversion":
        required = {
            "attempt_id", "generation", "nonce", "input_checksum", "input_byte_size",
            "output_pdf_checksum", "output_pdf_byte_size", "converter_manifest_hash",
            "sandbox_report_hash", "page_count",
        }
        try:
            attempt_id_valid = (
                isinstance(locator.get("attempt_id"), str)
                and str(uuid.UUID(locator["attempt_id"])) == locator["attempt_id"]
            )
        except (ValueError, AttributeError):
            attempt_id_valid = False
        sha_fields = (
            "nonce",
            "input_checksum",
            "output_pdf_checksum",
            "converter_manifest_hash",
            "sandbox_report_hash",
        )
        if not (
            set(locator) == required | {"locator_type"}
            and attempt_id_valid
            and locator.get("generation") == 1
            and all(
                isinstance(locator.get(key), str)
                and len(locator[key]) == 64
                and all(character in "0123456789abcdef" for character in locator[key])
                for key in sha_fields
            )
            and type(locator.get("input_byte_size")) is int
            and locator["input_byte_size"] > 0
            and type(locator.get("output_pdf_byte_size")) is int
            and locator["output_pdf_byte_size"] > 0
            and type(locator.get("page_count")) is int
            and locator["page_count"] > 0
        ):
            raise ValidationError({"locator": "Legacy HWP conversion locator is incomplete"})
    elif locator_type == "image_region":
        page_index = locator.get("page_index")
        frame_index = locator.get("frame_index")
        if (
            not region
            or (page_index is not None and (type(page_index) is not int or page_index < 0))
            or (frame_index is not None and (type(frame_index) is not int or frame_index < 0))
        ):
            raise ValidationError({"locator": "Image locator requires a bbox or polygon"})
    elif locator_type == "media_time":
        start = locator.get("start_seconds")
        end = locator.get("end_seconds")
        if not (_finite_number(start) and _finite_number(end) and 0 <= float(start) < float(end)):
            raise ValidationError({"locator": "Media locator requires a valid time range"})
    elif locator_type == "manual":
        if not (nonempty("entry_id") and nonempty("actor_id") and nonempty("source_url")):
            raise ValidationError({"locator": "Manual locator requires entry, actor, and source URL"})


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


class ExtractionObjectWriteState(models.TextChoices):
    RESERVED = "reserved", "Reserved"
    UPLOADED = "uploaded", "Uploaded"
    BOUND = "bound", "Bound"
    ORPHANED = "orphaned", "Orphaned"


class ExtractionObjectWritePurpose(models.TextChoices):
    RAW = "raw", "Raw input"
    RESULT = "result", "Extraction result"
    REASON = "reason", "Low-confidence reason"
    CONVERTED = "converted", "Converted document"


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
        allowed_modes = {
            ExtractionEngine.NATIVE_PDF: {None, ""},
            ExtractionEngine.PADDLEOCR: {None, ""},
            ExtractionEngine.HTML: {
                GenericValidationMode.DETERMINISTIC,
                GenericValidationMode.CALIBRATED,
            },
            ExtractionEngine.STRUCTURED: {
                GenericValidationMode.DETERMINISTIC,
                GenericValidationMode.CALIBRATED,
            },
            ExtractionEngine.SPREADSHEET: {
                GenericValidationMode.DETERMINISTIC,
                GenericValidationMode.CALIBRATED,
            },
            ExtractionEngine.HWPX: {GenericValidationMode.DETERMINISTIC},
            ExtractionEngine.LEGACY_HWP: {GenericValidationMode.DETERMINISTIC},
            ExtractionEngine.BROWSER_CAPTURE: {GenericValidationMode.DETERMINISTIC},
            ExtractionEngine.MEDIA: {
                GenericValidationMode.DETERMINISTIC,
                GenericValidationMode.CALIBRATED,
            },
            ExtractionEngine.MANUAL: {GenericValidationMode.MANUAL},
        }
        if self.validation_mode not in allowed_modes.get(self.engine, set()):
            raise ValidationError(
                {"validation_mode": "Extraction engine and validation mode do not match"}
            )
        in_process_v11_engines = {
            ExtractionEngine.NATIVE_PDF,
            ExtractionEngine.PADDLEOCR,
            ExtractionEngine.HTML,
            ExtractionEngine.STRUCTURED,
            ExtractionEngine.SPREADSHEET,
            ExtractionEngine.HWPX,
        }
        if self.profile_version == "1.1.0" and self.engine in in_process_v11_engines:
            expected_packages = {
                ExtractionEngine.NATIVE_PDF: "1.28.0",
                ExtractionEngine.PADDLEOCR: "3.7.0",
                ExtractionEngine.HTML: "0.4.11",
                ExtractionEngine.STRUCTURED: "0.7.1",
                ExtractionEngine.SPREADSHEET: "3.1.5",
                ExtractionEngine.HWPX: "0.7.1",
            }
            expected_package = expected_packages.get(self.engine)
            if expected_package is not None and self.package_version != expected_package:
                raise ValidationError(
                    {"package_version": "Extraction package differs from the v1.1 release pin"}
                )
            if (self.config or {}).get("python_runtime_version") != "3.12.10":
                raise ValidationError(
                    {"config": "Extraction v1.1 requires the deployed Python 3.12.10 pin"}
                )
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
            expected_dependencies = {
                "paddleocr": "3.7.0",
                "paddlepaddle": "3.2.2",
                "PyMuPDF": "1.28.0",
                "Pillow": "12.3.0",
            }
            if (
                self.profile_version == "1.1.0"
                and (self.config or {}).get("dependency_versions") != expected_dependencies
            ):
                raise ValidationError(
                    {"config": "PaddleOCR transitive dependency versions must be exact"}
                )
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
    verification_report_object_key = models.CharField(
        max_length=1024, null=True, blank=True
    )
    verification_report_object_version = models.CharField(
        max_length=256, null=True, blank=True
    )
    verification_report_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
        null=True,
        blank=True,
    )
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decided_at = models.DateTimeField()
    reason = models.CharField(max_length=500)

    class Meta:
        ordering = ("profile_snapshot_id", "version")
        constraints = [
            models.UniqueConstraint(fields=("profile_snapshot", "version"), name="uq_profile_decision_version"),
            models.UniqueConstraint(fields=("profile_snapshot", "request_key"), name="uq_profile_decision_request"),
        ]

    def clean(self) -> None:
        super().clean()
        envelope = (
            self.verification_report_object_key,
            self.verification_report_object_version,
            self.verification_report_hash,
        )
        if not all(envelope):
            raise ValidationError(
                "Profile decisions require a frozen verification report envelope"
            )
        profile = self.profile_snapshot
        profile_envelope = (
            profile.verification_report_object_key,
            profile.verification_report_object_version,
            profile.verification_report_hash,
        )
        if envelope != profile_envelope:
            raise ValidationError(
                "Profile decision report envelope differs from its profile projection"
            )
        if self.decision == self.Decision.RETIRED:
            previous = self.supersedes_decision
            if previous is None or previous.decision != self.Decision.APPROVED:
                raise ValidationError(
                    "A retired profile decision must supersede an approved decision"
                )
            previous_envelope = (
                previous.verification_report_object_key,
                previous.verification_report_object_version,
                previous.verification_report_hash,
            )
            if envelope != previous_envelope:
                raise ValidationError(
                    "A retired decision must copy its approved report envelope"
                )


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
    source_event_id = models.UUIDField(null=True, blank=True, db_index=True)
    lease_generation = models.PositiveBigIntegerField(default=0)
    lease_owner = models.CharField(max_length=160, blank=True, default="")
    lease_token = models.UUIDField(null=True, blank=True)
    delivery_count = models.PositiveBigIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    terminal_event_key = models.CharField(
        max_length=200, null=True, blank=True, unique=True
    )
    terminal_state = models.CharField(max_length=32, null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state=ExtractionState.RUNNING,
                        source_event_id__isnull=False,
                        lease_generation__gt=0,
                        delivery_count=models.F("lease_generation"),
                        lease_token__isnull=False,
                    )
                    & ~models.Q(lease_owner="")
                    | ~models.Q(state=ExtractionState.RUNNING)
                    & models.Q(lease_owner="", lease_token__isnull=True)
                ),
                name="ck_document_extraction_lease_complete",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state__in=(ExtractionState.QUEUED, ExtractionState.RUNNING),
                        terminal_event_key__isnull=True,
                        terminal_state__isnull=True,
                    )
                    | models.Q(
                        state__in=(ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE),
                        terminal_event_key__isnull=False,
                        terminal_state="ready",
                    )
                    | models.Q(
                        state=ExtractionState.FAILED,
                        terminal_event_key__isnull=False,
                        terminal_state="failed",
                    )
                ),
                name="ck_document_extraction_terminal_complete",
            ),
        ]
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
    expected_evidence_count = models.PositiveIntegerField(null=True, blank=True)
    expected_evidence_manifest_hash = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    peak_memory_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=120, null=True, blank=True)
    error_detail_redacted = models.CharField(max_length=1000, null=True, blank=True)
    source_event_id = models.UUIDField(null=True, blank=True, db_index=True)
    parent_lease_generation = models.PositiveBigIntegerField(default=0)
    lease_generation = models.PositiveBigIntegerField(default=0)
    lease_owner = models.CharField(max_length=160, blank=True, default="")
    lease_token = models.UUIDField(null=True, blank=True)
    delivery_count = models.PositiveBigIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    terminal_event_key = models.CharField(
        max_length=200, null=True, blank=True, unique=True
    )
    terminal_state = models.CharField(max_length=32, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("document_extraction", "extraction_fingerprint"),
                name="uq_document_extraction_fingerprint",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state=ExtractionState.RUNNING,
                        source_event_id__isnull=False,
                        lease_generation__gt=0,
                        delivery_count=models.F("lease_generation"),
                        lease_token__isnull=False,
                    )
                    & ~models.Q(lease_owner="")
                    | ~models.Q(state=ExtractionState.RUNNING)
                    & models.Q(lease_owner="", lease_token__isnull=True)
                ),
                name="ck_extraction_run_lease_complete",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state__in=(ExtractionState.QUEUED, ExtractionState.RUNNING),
                        terminal_event_key__isnull=True,
                        terminal_state__isnull=True,
                    )
                    | models.Q(
                        state__in=(ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE),
                        terminal_event_key__isnull=False,
                        terminal_state="ready",
                    )
                    | models.Q(
                        state=ExtractionState.FAILED,
                        terminal_event_key__isnull=False,
                        terminal_state="failed",
                    )
                ),
                name="ck_extraction_run_terminal_complete",
            ),
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
    expected_evidence_count = models.PositiveIntegerField(null=True, blank=True)
    expected_evidence_manifest_hash = models.CharField(
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
    source_event_id = models.UUIDField(null=True, blank=True, db_index=True)
    lease_generation = models.PositiveBigIntegerField(default=0)
    lease_owner = models.CharField(max_length=160, blank=True, default="")
    lease_token = models.UUIDField(null=True, blank=True)
    delivery_count = models.PositiveBigIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    terminal_event_key = models.CharField(
        max_length=200, null=True, blank=True, unique=True
    )
    terminal_state = models.CharField(max_length=32, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("run_source_item", "extraction_fingerprint"),
                name="uq_generic_attempt_fingerprint",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state=ExtractionState.RUNNING,
                        source_event_id__isnull=False,
                        lease_generation__gt=0,
                        delivery_count=models.F("lease_generation"),
                        lease_token__isnull=False,
                    )
                    & ~models.Q(lease_owner="")
                    | ~models.Q(state=ExtractionState.RUNNING)
                    & models.Q(lease_owner="", lease_token__isnull=True)
                ),
                name="ck_generic_extraction_lease_complete",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state__in=(ExtractionState.QUEUED, ExtractionState.RUNNING),
                        terminal_event_key__isnull=True,
                        terminal_state__isnull=True,
                    )
                    | models.Q(
                        state__in=(ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE),
                        terminal_event_key__isnull=False,
                        terminal_state="ready",
                    )
                    | models.Q(
                        state=ExtractionState.FAILED,
                        terminal_event_key__isnull=False,
                        terminal_state="failed",
                    )
                ),
                name="ck_generic_extraction_terminal_complete",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(
                        state__in=(
                            ExtractionState.SUCCEEDED,
                            ExtractionState.LOW_CONFIDENCE,
                        )
                    )
                    | models.Q(
                        expected_evidence_count__isnull=False,
                        expected_evidence_manifest_hash__isnull=False,
                    )
                ),
                name="ck_generic_terminal_manifest",
            ),
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
        if self.state in (ExtractionState.SUCCEEDED, ExtractionState.LOW_CONFIDENCE) and (
            not self.expected_evidence_count or not self.expected_evidence_manifest_hash
        ):
            raise ValidationError("Completed attempts require an exact evidence manifest")


class ExtractionObjectWriteReservation(UUIDModel):
    aggregate_kind = models.CharField(max_length=32)
    aggregate_id = models.UUIDField(db_index=True)
    source_event_id = models.UUIDField(db_index=True)
    lease_generation = models.PositiveBigIntegerField()
    lease_identity_hash = models.CharField(max_length=64, validators=[sha256_validator])
    purpose = models.CharField(max_length=24, choices=ExtractionObjectWritePurpose.choices)
    object_key = models.CharField(max_length=1024)
    state = models.CharField(
        max_length=16,
        choices=ExtractionObjectWriteState.choices,
        default=ExtractionObjectWriteState.RESERVED,
    )
    object_version = models.CharField(max_length=256, null=True, blank=True)
    object_etag = models.CharField(max_length=256, null=True, blank=True)
    checksum = models.CharField(
        max_length=64, validators=[sha256_validator], null=True, blank=True
    )
    byte_size = models.PositiveBigIntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=255, null=True, blank=True)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    bound_at = models.DateTimeField(null=True, blank=True)
    orphaned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "aggregate_kind",
                    "aggregate_id",
                    "lease_generation",
                    "purpose",
                    "object_key",
                ),
                name="uq_extraction_object_write_generation",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state=ExtractionObjectWriteState.RESERVED,
                        object_version__isnull=True,
                        object_etag__isnull=True,
                        checksum__isnull=True,
                        byte_size__isnull=True,
                        content_type__isnull=True,
                        uploaded_at__isnull=True,
                        bound_at__isnull=True,
                        orphaned_at__isnull=True,
                    )
                    | models.Q(
                        state=ExtractionObjectWriteState.UPLOADED,
                        object_version__isnull=False,
                        checksum__isnull=False,
                        byte_size__isnull=False,
                        content_type__isnull=False,
                        uploaded_at__isnull=False,
                        bound_at__isnull=True,
                        orphaned_at__isnull=True,
                    )
                    | models.Q(
                        state=ExtractionObjectWriteState.BOUND,
                        object_version__isnull=False,
                        checksum__isnull=False,
                        byte_size__isnull=False,
                        content_type__isnull=False,
                        uploaded_at__isnull=False,
                        bound_at__isnull=False,
                        orphaned_at__isnull=True,
                    )
                    | models.Q(
                        state=ExtractionObjectWriteState.ORPHANED,
                        object_version__isnull=True,
                        object_etag__isnull=True,
                        checksum__isnull=True,
                        byte_size__isnull=True,
                        content_type__isnull=True,
                        uploaded_at__isnull=True,
                        bound_at__isnull=True,
                        orphaned_at__isnull=False,
                    )
                    | models.Q(
                        state=ExtractionObjectWriteState.ORPHANED,
                        object_version__isnull=False,
                        checksum__isnull=False,
                        byte_size__isnull=False,
                        content_type__isnull=False,
                        uploaded_at__isnull=False,
                        bound_at__isnull=True,
                        orphaned_at__isnull=False,
                    )
                ),
                name="ck_extraction_object_write_state",
            ),
        ]


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
    raw_input_fingerprint = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        validators=[sha256_validator],
    )
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
    low_confidence_reasons = models.JSONField(default=list, blank=True)
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
            if self.locator_type != LocatorType.DOCUMENT_BLOCK:
                raise ValidationError({"locator_type": "Document evidence requires a document block locator"})
            validate_evidence_locator(self.locator_type, self.locator)
        if self.derivation_type == EvidenceDerivationType.OTHER:
            allowed_locators = GENERIC_ENGINE_LOCATOR_MATRIX.get(self.extraction_method or "")
            if not allowed_locators or self.locator_type not in allowed_locators:
                raise ValidationError(
                    {"locator_type": "Generic extraction engine and locator type do not match"}
                )
            if self.extraction_method == ExtractionEngine.MANUAL:
                if self.validation_mode != GenericValidationMode.MANUAL:
                    raise ValidationError("Manual engine requires manual validation mode")
            elif self.validation_mode not in {
                GenericValidationMode.DETERMINISTIC,
                GenericValidationMode.CALIBRATED,
            }:
                raise ValidationError("Generic extraction requires a frozen validation mode")
            validate_evidence_locator(self.locator_type, self.locator)
        if self.derivation_type != EvidenceDerivationType.VISUALIZATION and (
            not self.source_item_id or not self.origin_run_source_item_id
        ):
            raise ValidationError("Single-source evidence requires source and origin run-source lineage")
        if self.derivation_type == EvidenceDerivationType.VISUALIZATION and (
            self.source_item_id or self.origin_run_source_item_id
        ):
            raise ValidationError("Visualization evidence uses its input joins, not a single source")
        calibrated = self.validation_mode == GenericValidationMode.CALIBRATED
        calibration = (
            self.calibration_profile_key,
            self.calibration_profile_version,
            self.calibration_profile_hash,
        )
        if calibrated and (self.confidence is None or not all(calibration)):
            raise ValidationError("Calibrated evidence requires confidence and calibration provenance")
        if not calibrated and (self.confidence is not None or any(calibration)):
            if self.validation_mode in (
                GenericValidationMode.DETERMINISTIC,
                GenericValidationMode.MANUAL,
                None,
            ):
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
