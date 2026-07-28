import uuid

from django.conf import settings
from django.db import models


class RunState(models.TextChoices):
    QUEUED = "queued", "대기"
    COLLECTING = "collecting", "수집"
    EXTRACTING = "extracting", "추출"
    VALIDATING = "validating", "검증"
    DRAFTING = "drafting", "초안"
    AWAITING_APPROVAL = "awaiting_approval", "승인 대기"
    PUBLISHING = "publishing", "발행"
    COMPLETED = "completed", "완료"
    STOPPING = "stopping", "중지 중"
    STOPPED = "stopped", "중지"
    FAILED = "failed", "실패"


class CollectionRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    display_id = models.CharField(max_length=32, unique=True)
    topic_code = models.CharField(max_length=40, db_index=True)
    trigger = models.CharField(max_length=20, default="manual")
    approval_mode = models.CharField(max_length=20, default="manual")
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    source_registry = models.ForeignKey("topics.SourceRegistrySnapshot", on_delete=models.PROTECT)
    registry_manifest_hash = models.CharField(max_length=64)
    requested_target_ids = models.JSONField(default=list)
    request_fingerprint = models.CharField(max_length=64, unique=True)
    state = models.CharField(max_length=32, choices=RunState.choices, default=RunState.QUEUED, db_index=True)
    counters = models.JSONField(default=dict)
    error_summary = models.JSONField(null=True, blank=True)
    stop_requested_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class SourceCollectionAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(CollectionRun, on_delete=models.CASCADE, related_name="collection_attempts")
    source_snapshot = models.ForeignKey("topics.SourceDefinitionSnapshot", on_delete=models.PROTECT)
    adapter_name = models.CharField(max_length=120)
    adapter_version = models.CharField(max_length=40, default="v1")
    state = models.CharField(max_length=20, default="queued")
    response_count = models.PositiveIntegerField(default=0)
    response_checksum = models.CharField(max_length=64, null=True, blank=True)
    error_code = models.CharField(max_length=100, null=True, blank=True)
    error_detail_redacted = models.CharField(max_length=500, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "source_snapshot"], name="uq_collection_attempt_source")
        ]


class SourceItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey("topics.SourceDefinition", on_delete=models.PROTECT)
    external_id = models.CharField(max_length=500)
    canonical_url = models.URLField(max_length=1000)
    title = models.CharField(max_length=1000)
    publisher = models.CharField(max_length=300)
    published_at = models.DateTimeField(null=True, blank=True)
    first_collected_at = models.DateTimeField()
    content_hash = models.CharField(max_length=64)
    source_version_hash = models.CharField(max_length=64)
    body_text = models.TextField(blank=True)
    metadata = models.JSONField(default=dict)
    attachments = models.JSONField(default=list)
    discovery_status = models.CharField(max_length=20, default="active")
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id", "source_version_hash"], name="uq_source_item_version"
            )
        ]
        indexes = [models.Index(fields=["source", "external_id"])]


class RunSourceItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(CollectionRun, on_delete=models.CASCADE, related_name="run_source_items")
    collection_attempt = models.ForeignKey(SourceCollectionAttempt, on_delete=models.PROTECT)
    source_item = models.ForeignKey(SourceItem, on_delete=models.PROTECT, related_name="run_links")
    source_snapshot = models.ForeignKey("topics.SourceDefinitionSnapshot", on_delete=models.PROTECT)
    discovery_kind = models.CharField(max_length=24, default="new_version")
    discovered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "source_item"], name="uq_run_source_item")]


class RunStep(models.Model):
    run = models.ForeignKey(CollectionRun, on_delete=models.CASCADE, related_name="steps")
    name = models.CharField(max_length=64)
    attempt_no = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=20, default="queued")
    input_count = models.PositiveIntegerField(default=0)
    output_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=100, null=True, blank=True)
    error_detail_redacted = models.CharField(max_length=500, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    fanout_completed_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "name", "attempt_no"], name="uq_run_step_attempt")]
