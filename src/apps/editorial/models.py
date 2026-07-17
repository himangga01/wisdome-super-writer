import uuid

from django.conf import settings
from django.db import models


class DraftArticle(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article_identity_key = models.CharField(max_length=200, unique=True)
    topic_code = models.CharField(max_length=40, db_index=True)
    article_type = models.CharField(max_length=40)
    source_run = models.ForeignKey("collection.CollectionRun", on_delete=models.PROTECT, related_name="articles")
    state = models.CharField(max_length=32, default="draft")
    current_revision = models.ForeignKey(
        "ArticleRevision", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class GenerationAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article = models.ForeignKey(DraftArticle, on_delete=models.CASCADE, related_name="generation_attempts")
    generator_name = models.CharField(max_length=100, default="source_grounded_template")
    generator_version = models.CharField(max_length=40, default="v1")
    input_manifest_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=20, default="running")
    error_detail_redacted = models.CharField(max_length=500, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class ArticleRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article = models.ForeignKey(DraftArticle, on_delete=models.CASCADE, related_name="revisions")
    revision_no = models.PositiveIntegerField()
    generation_attempt = models.ForeignKey(GenerationAttempt, null=True, blank=True, on_delete=models.PROTECT)
    title = models.CharField(max_length=220)
    summary = models.TextField()
    body_markdown = models.TextField()
    provenance_kind = models.CharField(max_length=20, default="generated")
    input_manifest_hash = models.CharField(max_length=64)
    claim_manifest_hash = models.CharField(max_length=64)
    quality_manifest_hash = models.CharField(max_length=64)
    quality_state = models.CharField(max_length=20, default="pending")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["article", "revision_no"], name="uq_article_revision_no")]


class Claim(models.Model):
    class ClaimType(models.TextChoices):
        FACT = "fact", "사실"
        INTERPRETATION = "interpretation", "해석"
        OUTLOOK = "outlook", "전망"
        COMPANY_CLAIM = "company_claim", "기업 주장"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.ForeignKey(ArticleRevision, on_delete=models.CASCADE, related_name="claims")
    claim_type = models.CharField(max_length=24, choices=ClaimType.choices)
    text = models.TextField()
    position = models.PositiveIntegerField()
    citation_marker = models.CharField(max_length=30)
    high_impact = models.BooleanField(default=False)

    class Meta:
        ordering = ["position"]


class ClaimEvidence(models.Model):
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="evidence_links")
    evidence = models.ForeignKey("evidence.EvidenceAsset", on_delete=models.PROTECT, related_name="claim_links")
    support_kind = models.CharField(max_length=20, default="supports")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["claim", "evidence"], name="uq_claim_evidence")]


class QualityCheck(models.Model):
    revision = models.ForeignKey(ArticleRevision, on_delete=models.CASCADE, related_name="quality_checks")
    code = models.CharField(max_length=100)
    state = models.CharField(max_length=20)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["revision", "code"], name="uq_revision_quality_code")]


class VisualizationRender(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.ForeignKey(ArticleRevision, on_delete=models.CASCADE, related_name="visualizations")
    kind = models.CharField(max_length=40)
    title = models.CharField(max_length=200)
    transform_spec = models.JSONField(default=dict)
    input_manifest_hash = models.CharField(max_length=64)
    object_key = models.CharField(max_length=1000, null=True, blank=True)
    checksum = models.CharField(max_length=64, null=True, blank=True)
    alt_text = models.CharField(max_length=500)
    state = models.CharField(max_length=20, default="queued")


class EventCluster(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic_code = models.CharField(max_length=40)
    canonical_key = models.CharField(max_length=200)
    title = models.CharField(max_length=500)
    verification_state = models.CharField(max_length=40, default="candidate")
    source_item_ids = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["topic_code", "canonical_key"], name="uq_event_cluster_key")]


class CorrectionCase(models.Model):
    class State(models.TextChoices):
        DETECTED = "detected", "감지"
        VERIFYING = "verifying", "검증"
        VERIFIED = "verified", "확인"
        APPLYING = "applying", "반영"
        COMPLETED = "completed", "완료"
        REJECTED = "rejected", "거절"
        FAILED = "failed", "실패"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article = models.ForeignKey(DraftArticle, on_delete=models.PROTECT, related_name="correction_cases")
    source_item = models.ForeignKey("collection.SourceItem", on_delete=models.PROTECT)
    prior_source_item = models.ForeignKey(
        "collection.SourceItem", null=True, blank=True, on_delete=models.PROTECT, related_name="superseding_corrections"
    )
    kind = models.CharField(max_length=32, default="correction")
    state = models.CharField(max_length=20, choices=State.choices, default=State.DETECTED)
    subject_hash = models.CharField(max_length=64)
    diff_summary = models.JSONField(default=dict)
    detected_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["article", "subject_hash"], name="uq_article_correction_subject")
        ]
