import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class DraftArticle(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article_identity_key = models.CharField(max_length=200, unique=True)
    topic_code = models.CharField(max_length=40, db_index=True)
    article_type = models.CharField(max_length=40)
    source_run = models.ForeignKey("collection.CollectionRun", on_delete=models.PROTECT, related_name="articles")
    source_verification = models.ForeignKey(
        "EventClusterVerification",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="articles",
    )
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
    generation_manifest_hash = models.CharField(max_length=64, blank=True, default="")
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


class EventClusterItem(models.Model):
    cluster = models.ForeignKey(
        EventCluster,
        on_delete=models.PROTECT,
        related_name="members",
    )
    run_source_item = models.ForeignKey(
        "collection.RunSourceItem",
        on_delete=models.PROTECT,
        related_name="event_cluster_memberships",
    )
    origin_identity_hash = models.CharField(max_length=64)
    independence_group = models.CharField(max_length=120)
    role = models.CharField(max_length=24)
    selection_state = models.CharField(max_length=24)
    decision_reason = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cluster", "run_source_item"],
                name="uq_event_cluster_run_item",
            )
        ]


class EventClusterVerificationQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation() -> None:
        raise TypeError("EventClusterVerification is append-only")

    def update(self, **kwargs):
        self._reject_mutation()

    async def aupdate(self, **kwargs):
        self._reject_mutation()

    def delete(self):
        self._reject_mutation()

    async def adelete(self):
        self._reject_mutation()

    def _raw_delete(self, using):
        self._reject_mutation()

    def bulk_update(self, objs, fields, batch_size=None):
        self._reject_mutation()

    async def abulk_update(self, objs, fields, batch_size=None):
        self._reject_mutation()

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            self._reject_mutation()
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class EventClusterVerificationManager(
    models.Manager.from_queryset(EventClusterVerificationQuerySet)
):
    pass


class EventClusterVerification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cluster = models.ForeignKey(
        EventCluster,
        on_delete=models.PROTECT,
        related_name="verifications",
    )
    origin_run = models.ForeignKey(
        "collection.CollectionRun",
        on_delete=models.PROTECT,
        related_name="event_cluster_verifications",
    )
    version = models.PositiveIntegerField()
    decision = models.CharField(max_length=32)
    article_type = models.CharField(max_length=40)
    category = models.CharField(max_length=64)
    primary_source_count = models.PositiveIntegerField()
    independent_origin_count = models.PositiveIntegerField()
    decision_reason = models.CharField(max_length=500)
    policy_version = models.CharField(max_length=40)
    policy_hash = models.CharField(max_length=64)
    local_event_date = models.DateField()
    evidence_manifest = models.JSONField(default=list)
    evidence_manifest_hash = models.CharField(max_length=64)
    conflict_manifest = models.JSONField(default=list)
    excluded_source_manifest = models.JSONField(default=list)
    rule_manifest_hash = models.CharField(max_length=64)
    result_manifest_hash = models.CharField(max_length=64)
    supersedes = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by",
    )
    verified_at = models.DateTimeField(auto_now_add=True)

    objects = EventClusterVerificationManager()

    class Meta:
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["cluster", "version"],
                name="uq_event_cluster_verification_version",
            ),
            models.UniqueConstraint(
                fields=["cluster", "origin_run"],
                name="uq_event_cluster_verification_origin_run",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(decision="verified_breaking")
                    | (
                        models.Q(
                            category__in=(
                                "regulation_export_control",
                                "factory_supply_disruption",
                                "merger_or_material_earnings",
                                "critical_technology_or_mass_production",
                            )
                        )
                        & (
                            models.Q(primary_source_count__gte=1)
                            | models.Q(independent_origin_count__gte=2)
                        )
                    )
                ),
                name="ck_event_verification_breaking_gate",
            ),
        ]
        ordering = ["cluster_id", "version"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("EventClusterVerification is append-only")
        if self.decision == "verified_breaking" and (
            self.category
            not in {
                "regulation_export_control",
                "factory_supply_disruption",
                "merger_or_material_earnings",
                "critical_technology_or_mass_production",
            }
            or (
                self.primary_source_count < 1
                and self.independent_origin_count < 2
            )
        ):
            raise ValidationError(
                "verified_breaking requires an approved category and "
                "one direct primary or two independent origins"
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("EventClusterVerification is append-only")


class ArticleEventClusterQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation() -> None:
        raise TypeError("ArticleEventCluster is append-only")

    def update(self, **kwargs):
        self._reject_mutation()

    async def aupdate(self, **kwargs):
        self._reject_mutation()

    def delete(self):
        self._reject_mutation()

    async def adelete(self):
        self._reject_mutation()

    def _raw_delete(self, using):
        self._reject_mutation()

    def bulk_update(self, objs, fields, batch_size=None):
        self._reject_mutation()

    async def abulk_update(self, objs, fields, batch_size=None):
        self._reject_mutation()

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            self._reject_mutation()
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class ArticleEventClusterManager(
    models.Manager.from_queryset(ArticleEventClusterQuerySet)
):
    pass


class ArticleEventCluster(models.Model):
    article = models.ForeignKey(
        DraftArticle,
        on_delete=models.PROTECT,
        related_name="event_clusters",
    )
    event_cluster = models.ForeignKey(
        EventCluster,
        on_delete=models.PROTECT,
        related_name="article_memberships",
    )
    verification = models.ForeignKey(
        EventClusterVerification,
        on_delete=models.PROTECT,
        related_name="article_memberships",
    )
    role = models.CharField(max_length=24)
    display_order = models.PositiveIntegerField()
    inclusion_reason = models.CharField(max_length=500)
    cluster_snapshot_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ArticleEventClusterManager()

    class Meta:
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["article", "event_cluster"],
                name="uq_article_event_cluster",
            ),
            models.UniqueConstraint(
                fields=["article", "display_order"],
                name="uq_article_event_display_order",
            ),
        ]
        ordering = ["article_id", "display_order"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("ArticleEventCluster is append-only")
        alias = kwargs.get("using") or self._state.db or "default"
        verification = self._state.fields_cache.get("verification")
        if verification is None or verification.pk != self.verification_id:
            verification = EventClusterVerification.objects.using(alias).get(
                pk=self.verification_id
            )
        if (
            verification.cluster_id != self.event_cluster_id
            or verification.evidence_manifest_hash
            != self.cluster_snapshot_hash
        ):
            raise ValidationError(
                "article event membership must match its frozen verification"
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("ArticleEventCluster is append-only")


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
