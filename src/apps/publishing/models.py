from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q


PUBLICATION_INTENT_REQUEST_VERSION = "publication-intent-request-v1"
PUBLICATION_DISPATCH_REQUEST_VERSION = "publication-dispatch-request-v1"
LEGACY_UNVERIFIABLE_INTENT_REQUEST_VERSION = "legacy-unverifiable-v1"
PUBLICATION_EXECUTION_IDENTITY_VERSION = "publication-execution-v1"
PUBLICATION_RECONCILE_IDENTITY_VERSION = "publication-reconcile-v1"
LEGACY_UNVERIFIABLE_EXECUTION_IDENTITY_VERSION = "legacy-unverifiable-v1"
PUBLISHED_ASSET_COHORT_VERSION = "published-assets-v1"


sha256_validator = RegexValidator(
    r"^[0-9a-f]{64}$",
    "Expected a lowercase SHA-256 digest",
)


class ChannelCode(models.TextChoices):
    WORDPRESS = "wordpress", "WordPress"
    BLOGGER = "blogger", "Google Blogger"


class ChannelRole(models.TextChoices):
    PRIMARY = "primary_canonical", "대표 원문"
    SECONDARY = "secondary_distribution", "보조 배포"


class TargetEnvironment(models.TextChoices):
    TEST = "test", "테스트"
    PRODUCTION = "production", "운영"


class ValidationState(models.TextChoices):
    NOT_RUN = "not_run", "미실행"
    PASSED = "passed", "통과"
    FAILED = "failed", "실패"
    STALE = "stale", "만료"


class PublicationAction(models.TextChoices):
    CREATE = "create", "생성"
    UPDATE = "update", "수정"
    UNPUBLISH = "unpublish", "철회"
    MARK_WITHDRAWN = "mark_withdrawn", "철회 표시"


class ApprovalMode(models.TextChoices):
    MANUAL = "manual", "수동 승인"
    VALIDATED_AUTO = "validated_auto", "검증 자동"


class PublicationTarget(models.Model):
    class ConnectionState(models.TextChoices):
        PENDING = "pending", "연결 대기"
        VERIFIED = "verified", "검증됨"
        EXPIRED = "expired", "인증 만료"
        REVOKED = "revoked", "연결 해제"
        BLOCKED = "blocked", "차단"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel = models.CharField(max_length=20, choices=ChannelCode.choices)
    role = models.CharField(max_length=32, choices=ChannelRole.choices)
    environment = models.CharField(max_length=16, choices=TargetEnvironment.choices)
    display_name = models.CharField(max_length=200)
    remote_blog_id = models.CharField(max_length=255, null=True, blank=True)
    base_url = models.URLField(max_length=1000)
    username_ref = models.CharField(max_length=500, null=True, blank=True)
    credential_ref = models.CharField(max_length=500, null=True, blank=True)
    credential_version = models.CharField(max_length=120, blank=True)
    capabilities = models.JSONField(default=dict)
    connection_state = models.CharField(
        max_length=16,
        choices=ConnectionState.choices,
        default=ConnectionState.PENDING,
    )
    preflight_state = models.CharField(
        max_length=16, choices=ValidationState.choices, default=ValidationState.NOT_RUN
    )
    canary_state = models.CharField(
        max_length=16, choices=ValidationState.choices, default=ValidationState.NOT_RUN
    )
    pilot_state = models.CharField(
        max_length=16, choices=ValidationState.choices, default=ValidationState.NOT_RUN
    )
    canary_target = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="production_targets"
    )
    canary_policy_version = models.CharField(max_length=100, null=True, blank=True)
    last_preflight_at = models.DateTimeField(null=True, blank=True)
    last_canary_at = models.DateTimeField(null=True, blank=True)
    last_pilot_at = models.DateTimeField(null=True, blank=True)
    current_snapshot_id = models.UUIDField(null=True, blank=True)
    current_snapshot_version = models.PositiveIntegerField(default=0)
    current_config_hash = models.CharField(max_length=64, blank=True)
    publisher_contract_version = models.CharField(max_length=100, default="publisher-v1")
    publisher_adapter_manifest_hash = models.CharField(max_length=64, blank=True)
    auto_publish_enabled = models.BooleanField(default=False)
    latest_auto_publish_activation_id = models.UUIDField(null=True, blank=True)
    auto_publish_activation_version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["channel", "environment", "display_name"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(channel=ChannelCode.WORDPRESS, role=ChannelRole.PRIMARY)
                    | Q(channel=ChannelCode.BLOGGER, role=ChannelRole.SECONDARY)
                ),
                name="ck_target_channel_role",
            ),
            models.UniqueConstraint(
                fields=["environment"],
                condition=Q(role=ChannelRole.PRIMARY),
                name="uq_primary_target_per_environment",
            ),
        ]

    def clean(self):
        if not self.base_url.lower().startswith("https://"):
            raise ValidationError({"base_url": "발행 대상은 HTTPS URL이어야 합니다."})
        if self.channel == ChannelCode.BLOGGER and not self.remote_blog_id:
            raise ValidationError({"remote_blog_id": "Blogger blog ID가 필요합니다."})
        if self.environment == TargetEnvironment.PRODUCTION and self.canary_target_id:
            if self.canary_target.environment != TargetEnvironment.TEST:
                raise ValidationError({"canary_target": "격리된 테스트 target만 참조할 수 있습니다."})
            if self.canary_target.channel != self.channel:
                raise ValidationError({"canary_target": "동일 채널 target이어야 합니다."})


class PublicationTargetIntentFence(models.Model):
    """Per-target serialization row for intent creation and target mutation."""

    target = models.OneToOneField(
        PublicationTarget,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="intent_fence",
    )


class PublicationTargetSnapshotQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation() -> None:
        raise TypeError("PublicationTargetSnapshot is append-only")

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


class PublicationTargetSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.ForeignKey(PublicationTarget, on_delete=models.PROTECT, related_name="snapshots")
    version = models.PositiveIntegerField()
    channel = models.CharField(max_length=20, choices=ChannelCode.choices)
    role = models.CharField(max_length=32, choices=ChannelRole.choices)
    environment = models.CharField(max_length=16, choices=TargetEnvironment.choices)
    remote_blog_id = models.CharField(max_length=255, null=True, blank=True)
    base_url = models.URLField(max_length=1000)
    username_ref_identity_hash = models.CharField(max_length=64, null=True, blank=True)
    credential_ref_identity_hash = models.CharField(max_length=64)
    credential_version = models.CharField(max_length=120, blank=True)
    capabilities = models.JSONField(default=dict)
    connection_state = models.CharField(max_length=16, choices=PublicationTarget.ConnectionState.choices)
    preflight_state = models.CharField(max_length=16, choices=ValidationState.choices)
    canary_state = models.CharField(max_length=16, choices=ValidationState.choices)
    pilot_state = models.CharField(max_length=16, choices=ValidationState.choices)
    canary_target_id = models.UUIDField(null=True, blank=True)
    canary_policy_version = models.CharField(max_length=100, null=True, blank=True)
    publisher_contract_version = models.CharField(max_length=100)
    publisher_adapter_manifest_hash = models.CharField(max_length=64)
    config_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(PublicationTargetSnapshotQuerySet)()

    class Meta:
        ordering = ["target_id", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["target", "version"], name="uq_target_snapshot_version"),
            models.UniqueConstraint(fields=["target", "config_hash"], name="uq_target_snapshot_material"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("PublicationTargetSnapshot is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("PublicationTargetSnapshot is append-only")


class TargetCanaryRun(models.Model):
    class State(models.TextChoices):
        QUEUED = "queued", "대기"
        RUNNING = "running", "실행"
        PASSED = "passed", "통과"
        FAILED = "failed", "실패"
        CLEANUP_REQUIRED = "cleanup_required", "정리 필요"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.ForeignKey(PublicationTarget, on_delete=models.PROTECT, related_name="canary_runs")
    target_snapshot = models.ForeignKey(PublicationTargetSnapshot, on_delete=models.PROTECT)
    policy_version = models.CharField(max_length=100)
    request_key = models.CharField(max_length=200)
    state = models.CharField(max_length=24, choices=State.choices, default=State.QUEUED)
    stage_results = models.JSONField(default=list)
    report_hash = models.CharField(max_length=64, blank=True)
    remote_cleanup_refs = models.JSONField(default=list)
    reason = models.CharField(max_length=500)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["target", "request_key"], name="uq_target_canary_request")
        ]


class AutoPublishValidation(models.Model):
    class State(models.TextChoices):
        DRAFT = "draft", "초안"
        PASSED = "passed", "통과"
        REVOKED = "revoked", "철회"
        STALE = "stale", "만료"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.ForeignKey(PublicationTarget, on_delete=models.PROTECT, related_name="validations")
    topic_code = models.CharField(max_length=40)
    target_snapshot = models.ForeignKey(PublicationTargetSnapshot, on_delete=models.PROTECT)
    target_config_hash = models.CharField(max_length=64)
    source_registry_snapshot_id = models.UUIDField()
    registry_manifest_hash = models.CharField(max_length=64)
    source_adapter_manifest_hash = models.CharField(max_length=64)
    extraction_profile_manifest_hash = models.CharField(max_length=64)
    generation_pipeline_manifest_hash = models.CharField(max_length=64)
    topic_policy_version = models.PositiveIntegerField()
    editorial_policy_hash = models.CharField(max_length=64)
    quality_gate_manifest_hash = models.CharField(max_length=64)
    render_contract_version = models.CharField(max_length=100)
    channel_contract_version = models.CharField(max_length=100)
    publisher_adapter_manifest_hash = models.CharField(max_length=64)
    test_report_object_key = models.CharField(max_length=1000)
    test_report_object_version = models.CharField(max_length=255)
    test_report_hash = models.CharField(max_length=64)
    material_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=State.choices, default=State.DRAFT)
    latest_decision_id = models.UUIDField(null=True, blank=True)
    decision_version = models.PositiveIntegerField(default=0)
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    invalidated_at = models.DateTimeField(null=True, blank=True)
    invalidation_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["target", "request_key"], name="uq_auto_validation_request")
        ]
        ordering = ["-created_at"]


class AutoPublishValidationDecision(models.Model):
    class Decision(models.TextChoices):
        PASSED = "passed", "통과"
        REVOKED = "revoked", "철회"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    validation = models.ForeignKey(AutoPublishValidation, on_delete=models.PROTECT, related_name="decisions")
    version = models.PositiveIntegerField()
    decision = models.CharField(max_length=16, choices=Decision.choices)
    supersedes_decision_id = models.UUIDField(null=True, blank=True)
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    decision_hash = models.CharField(max_length=64)
    reauth_proof_id = models.UUIDField()
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decided_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=500)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["validation", "version"], name="uq_auto_validation_decision_version"),
            models.UniqueConstraint(fields=["validation", "request_key"], name="uq_auto_validation_decision_request"),
        ]


class AutoPublishActivation(models.Model):
    class Decision(models.TextChoices):
        ENABLED = "enabled", "활성"
        REVOKED = "revoked", "해제"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.ForeignKey(PublicationTarget, on_delete=models.PROTECT, related_name="activations")
    target_snapshot = models.ForeignKey(PublicationTargetSnapshot, on_delete=models.PROTECT)
    target_operational_config_hash = models.CharField(max_length=64)
    validation_refs = models.JSONField(default=list)
    validation_manifest_hash = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    decision = models.CharField(max_length=16, choices=Decision.choices)
    supersedes_activation_id = models.UUIDField(null=True, blank=True)
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    activation_hash = models.CharField(max_length=64)
    reauth_proof_id = models.UUIDField()
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decided_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=500)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["target", "version"], name="uq_auto_activation_version"),
            models.UniqueConstraint(fields=["target", "request_key"], name="uq_auto_activation_request"),
        ]


class PublicationIntentQuerySet(models.QuerySet):
    @staticmethod
    def _require_state_only(fields) -> None:
        if set(fields) - {"state"}:
            raise TypeError("PublicationIntent frozen identity is immutable")

    def update(self, **kwargs):
        self._require_state_only(kwargs)
        return super().update(**kwargs)

    async def aupdate(self, **kwargs):
        self._require_state_only(kwargs)
        return await super().aupdate(**kwargs)

    def delete(self):
        raise TypeError("PublicationIntent is append-only")

    async def adelete(self):
        raise TypeError("PublicationIntent is append-only")

    def _raw_delete(self, using):
        raise TypeError("PublicationIntent is append-only")

    def bulk_update(self, objs, fields, batch_size=None):
        self._require_state_only(fields)
        return super().bulk_update(objs, fields, batch_size=batch_size)

    async def abulk_update(self, objs, fields, batch_size=None):
        self._require_state_only(fields)
        return await super().abulk_update(objs, fields, batch_size=batch_size)


class PublicationIntent(models.Model):
    class State(models.TextChoices):
        DRAFT = "draft", "초안"
        AWAITING_APPROVAL = "awaiting_approval", "승인 대기"
        APPROVED = "approved", "승인"
        STALE = "stale", "만료"
        DISPATCHED = "dispatched", "전송"
        CANCELLED = "cancelled", "취소"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article_id = models.UUIDField(db_index=True)
    article_revision = models.ForeignKey(
        "editorial.ArticleRevision", on_delete=models.PROTECT, related_name="publication_intents"
    )
    revision_no = models.PositiveIntegerField()
    revision_content_hash = models.CharField(max_length=64)
    correction_case_id = models.UUIDField(null=True, blank=True)
    origin_collection_run_id = models.UUIDField(null=True, blank=True)
    target_snapshot_refs = models.JSONField(default=list)
    target_commands = models.JSONField(default=list)
    target_snapshot_manifest_hash = models.CharField(max_length=64)
    approval_mode = models.CharField(max_length=20, choices=ApprovalMode.choices)
    auto_publish_validation_refs = models.JSONField(default=list)
    auto_validation_manifest_hash = models.CharField(max_length=64, null=True, blank=True)
    auto_publish_activation_refs = models.JSONField(default=list)
    auto_activation_manifest_hash = models.CharField(max_length=64, null=True, blank=True)
    generation_attempt_id = models.UUIDField(null=True, blank=True)
    input_evidence_manifest_hash = models.CharField(max_length=64)
    generation_pipeline_manifest_hash = models.CharField(max_length=64, null=True, blank=True)
    quality_gate_manifest_hash = models.CharField(max_length=64)
    quality_report_hash = models.CharField(max_length=64)
    supersedes_intent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        db_column="supersedes_intent_id",
        on_delete=models.PROTECT,
        related_name="superseded_by",
    )
    intent_hash = models.CharField(max_length=64)
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    request_hash_version = models.CharField(max_length=40)
    state = models.CharField(max_length=24, choices=State.choices, default=State.AWAITING_APPROVAL)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(PublicationIntentQuerySet)()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["article_id", "request_key"],
                name="uq_intent_article_request",
            )
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None or set(update_fields) - {"state"}:
                raise TypeError("PublicationIntent frozen identity is immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("PublicationIntent is append-only")


class PublicationIntentHeadQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation() -> None:
        raise TypeError("PublicationIntentHead is database-managed")

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


class PublicationIntentHead(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article_id = models.UUIDField(unique=True)
    latest_intent = models.ForeignKey(
        PublicationIntent,
        on_delete=models.PROTECT,
        related_name="headed_by",
    )
    version = models.PositiveIntegerField()
    updated_at = models.DateTimeField(auto_now=True)
    objects = models.Manager.from_queryset(PublicationIntentHeadQuerySet)()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="ck_publication_intent_head_version_positive",
            )
        ]

    def save(self, *args, **kwargs):
        raise TypeError("PublicationIntentHead is database-managed")

    def delete(self, *args, **kwargs):
        raise TypeError("PublicationIntentHead is database-managed")


class PublicationDispatchQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation() -> None:
        raise TypeError("PublicationDispatch is append-only")

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


class PublicationDispatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication_intent = models.OneToOneField(
        PublicationIntent,
        on_delete=models.PROTECT,
        related_name="dispatch",
    )
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    request_hash_version = models.CharField(max_length=40)
    correlation_id = models.UUIDField(db_index=True)
    accepted_at = models.DateTimeField(auto_now_add=True)
    attempt_count = models.PositiveIntegerField()
    attempt_manifest_hash = models.CharField(max_length=64)
    objects = models.Manager.from_queryset(PublicationDispatchQuerySet)()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(attempt_count__gte=1),
                name="ck_publication_dispatch_attempt_count_positive",
            )
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("PublicationDispatch is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("PublicationDispatch is append-only")


class ArticleChannelRenderQuerySet(models.QuerySet):
    def _reject_if_frozen(self) -> None:
        if self.filter(
            Q(render_stage="final") | Q(approval__isnull=False)
        ).exists():
            raise TypeError("Frozen ArticleChannelRender is append-only")

    def update(self, **kwargs):
        self._reject_if_frozen()
        return super().update(**kwargs)

    async def aupdate(self, **kwargs):
        if await self.filter(
            Q(render_stage="final") | Q(approval__isnull=False)
        ).aexists():
            raise TypeError("Frozen ArticleChannelRender is append-only")
        return await super().aupdate(**kwargs)

    def delete(self):
        self._reject_if_frozen()
        return super().delete()

    async def adelete(self):
        if await self.filter(
            Q(render_stage="final") | Q(approval__isnull=False)
        ).aexists():
            raise TypeError("Frozen ArticleChannelRender is append-only")
        return await super().adelete()

    def _raw_delete(self, using):
        self._reject_if_frozen()
        return super()._raw_delete(using)

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        object_ids = [obj.pk for obj in objects if obj.pk is not None]
        self.model.objects.filter(pk__in=object_ids)._reject_if_frozen()
        return super().bulk_update(objects, fields, batch_size=batch_size)

    async def abulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        object_ids = [obj.pk for obj in objects if obj.pk is not None]
        if await self.model.objects.filter(
            Q(render_stage="final") | Q(approval__isnull=False),
            pk__in=object_ids,
        ).aexists():
            raise TypeError("Frozen ArticleChannelRender is append-only")
        return await super().abulk_update(objects, fields, batch_size=batch_size)


class ArticleChannelRender(models.Model):
    class Stage(models.TextChoices):
        PREVIEW = "preview", "미리보기"
        FINAL = "final", "최종"

    class CanonicalState(models.TextChoices):
        NOT_APPLICABLE = "not_applicable", "해당 없음"
        PENDING = "pending", "대기"
        RESOLVED = "resolved", "확정"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication_intent = models.ForeignKey(PublicationIntent, on_delete=models.PROTECT, related_name="renders")
    article_revision = models.ForeignKey(
        "editorial.ArticleRevision", on_delete=models.PROTECT, related_name="channel_renders"
    )
    target = models.ForeignKey(PublicationTarget, on_delete=models.PROTECT)
    target_snapshot = models.ForeignKey(PublicationTargetSnapshot, on_delete=models.PROTECT)
    target_config_hash = models.CharField(max_length=64)
    channel_role = models.CharField(max_length=32, choices=ChannelRole.choices)
    render_stage = models.CharField(max_length=16, choices=Stage.choices)
    title = models.CharField(max_length=1000)
    body_html = models.TextField()
    labels = models.JSONField(default=list)
    source_links = models.JSONField(default=list)
    included_claim_ids = models.JSONField(default=list)
    canonical_source_url = models.URLField(max_length=1000, null=True, blank=True)
    canonical_link_state = models.CharField(max_length=20, choices=CanonicalState.choices)
    template_hash = models.CharField(max_length=64)
    content_hash = models.CharField(max_length=64)
    source_manifest_hash = models.CharField(max_length=64)
    media_manifest = models.JSONField(default=list)
    correction_history = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(ArticleChannelRenderQuerySet)()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["publication_intent", "target_snapshot", "render_stage"],
                name="uq_intent_target_render_stage",
            )
        ]

    def save(self, *args, **kwargs):
        if (
            not self._state.adding
            and type(self).objects.filter(
                pk=self.pk,
            ).filter(
                Q(render_stage="final") | Q(approval__isnull=False)
            ).exists()
        ):
            raise TypeError("Frozen ArticleChannelRender is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if type(self).objects.filter(
            pk=self.pk,
        ).filter(
            Q(render_stage="final") | Q(approval__isnull=False)
        ).exists():
            raise TypeError("Frozen ArticleChannelRender is append-only")
        return super().delete(*args, **kwargs)


class ApprovalQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation() -> None:
        raise TypeError("Approval is append-only")

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
        raise TypeError(
            "Approval bulk insertion is forbidden; use decide_approval()"
        )


class Approval(models.Model):
    class Decision(models.TextChoices):
        APPROVED = "approved", "승인"
        REJECTED = "rejected", "거절"
        REVOKED = "revoked", "철회"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article_revision = models.ForeignKey(
        "editorial.ArticleRevision", on_delete=models.PROTECT, related_name="publication_approvals"
    )
    revision_no = models.PositiveIntegerField()
    publication_intent = models.ForeignKey(PublicationIntent, on_delete=models.PROTECT, related_name="approvals")
    target = models.ForeignKey(PublicationTarget, on_delete=models.PROTECT)
    target_action = models.CharField(max_length=24, choices=PublicationAction.choices)
    article_channel_render = models.ForeignKey(
        ArticleChannelRender, null=True, blank=True, on_delete=models.PROTECT
    )
    action_subject = models.JSONField()
    target_snapshot = models.ForeignKey(PublicationTargetSnapshot, on_delete=models.PROTECT)
    target_config_hash = models.CharField(max_length=64)
    mode = models.CharField(max_length=20, choices=ApprovalMode.choices)
    decision = models.CharField(max_length=16, choices=Decision.choices)
    approval_subject_hash = models.CharField(max_length=64)
    approval_material_version = models.CharField(
        max_length=40,
        default="approval-subject-v1",
    )
    decision_hash = models.CharField(max_length=64, unique=True)
    decision_reason = models.CharField(max_length=500)
    decision_actor_type = models.CharField(
        max_length=16,
        choices=(("admin", "Admin"), ("worker", "Worker")),
    )
    decision_actor_id = models.UUIDField(null=True, blank=True)
    decision_event_key = models.CharField(max_length=255, null=True, blank=True)
    head_version = models.PositiveIntegerField(default=1)
    supersedes_approval = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        db_column="supersedes_approval_id",
        on_delete=models.PROTECT,
        related_name="superseded_by",
    )
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    reauth_proof_id = models.UUIDField(null=True, blank=True)
    policy_snapshot_hash = models.CharField(max_length=64)
    quality_report_hash = models.CharField(max_length=64)
    render_template_hash = models.CharField(max_length=64, null=True, blank=True)
    source_manifest_hash = models.CharField(max_length=64)
    admin = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decided_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(ApprovalQuerySet)()

    class Meta:
        ordering = ["-decided_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["publication_intent", "target", "request_key"],
                name="uq_approval_intent_target_request",
            ),
            models.CheckConstraint(
                condition=Q(head_version__gte=1),
                name="ck_approval_head_version_positive",
            ),
            models.CheckConstraint(
                condition=Q(
                    approval_material_version="approval-subject-v3",
                    decision__in=("approved", "rejected", "revoked"),
                ),
                name="ck_approval_v3_decision_material",
            ),
            models.CheckConstraint(
                condition=~Q(id=models.F("supersedes_approval_id")),
                name="ck_approval_not_self_superseding",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        decision_actor_type="admin",
                        decision_actor_id=models.F("admin_id"),
                        decision_event_key__isnull=True,
                    )
                    | Q(
                        decision_actor_type="worker",
                        decision_actor_id__isnull=True,
                        decision_event_key__isnull=False,
                    )
                ),
                name="ck_approval_decision_actor_provenance",
            ),
            models.CheckConstraint(
                condition=(
                    Q(mode=ApprovalMode.MANUAL, decision_actor_type="admin")
                    | Q(
                        mode=ApprovalMode.VALIDATED_AUTO,
                        decision="approved",
                        decision_actor_type="worker",
                    )
                    | Q(
                        mode=ApprovalMode.VALIDATED_AUTO,
                        decision__in=("rejected", "revoked"),
                        decision_actor_type="admin",
                    )
                ),
                name="ck_approval_mode_actor_decision",
            ),
            models.UniqueConstraint(
                fields=("publication_intent", "target", "head_version"),
                name="uq_approval_intent_target_head_version",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("Approval is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Approval is append-only")


class PublicationQuerySet(models.QuerySet):
    FROZEN_IDENTITY_FIELDS = frozenset(
        {
            "article_id",
            "target",
            "target_id",
            "origin_target_snapshot_id",
            "remote_lookup_key",
            "created_at",
        }
    )
    FROZEN_IDENTITY_ATTNAMES = frozenset(
        {
            "article_id",
            "target_id",
            "origin_target_snapshot_id",
            "remote_lookup_key",
            "created_at",
        }
    )

    @classmethod
    def _reject_frozen_fields(cls, fields) -> None:
        if cls.FROZEN_IDENTITY_FIELDS.intersection(fields):
            raise TypeError("Publication frozen identity is immutable")

    def update(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return super().update(**kwargs)

    async def aupdate(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return await super().aupdate(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        self._reject_frozen_fields(fields)
        return super().bulk_update(objs, fields, batch_size=batch_size)

    async def abulk_update(self, objs, fields, batch_size=None):
        self._reject_frozen_fields(fields)
        return await super().abulk_update(objs, fields, batch_size=batch_size)


class Publication(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "대기"
        SCHEDULED = "scheduled", "예약"
        IN_PROGRESS = "in_progress", "발행 중"
        PUBLISHED = "published", "발행"
        UPDATING = "updating", "수정 중"
        WITHDRAWING = "withdrawing", "철회 중"
        WITHDRAWN = "withdrawn", "철회"
        MARKING_WITHDRAWN = "marking_withdrawn", "철회 표시 중"
        MARKED_WITHDRAWN = "marked_withdrawn", "철회 표시"
        RECONCILING = "reconciling", "조정 중"
        RETRYABLE_FAILED = "retryable_failed", "재시도 가능 실패"
        PERMANENT_FAILED = "permanent_failed", "영구 실패"
        MANUAL_REQUIRED = "manual_required", "수동 확인 필요"

    class RemoteState(models.TextChoices):
        DRAFT = "draft", "초안"
        SCHEDULED = "scheduled", "예약"
        PUBLISHED = "published", "공개"
        WITHDRAWN = "withdrawn", "철회"
        DELETED = "deleted", "삭제"
        UNKNOWN = "unknown", "알 수 없음"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article_id = models.UUIDField(db_index=True)
    target = models.ForeignKey(PublicationTarget, on_delete=models.PROTECT, related_name="publications")
    origin_target_snapshot_id = models.UUIDField()
    remote_post_id = models.CharField(max_length=255, null=True, blank=True)
    remote_lookup_key = models.CharField(max_length=255)
    remote_url = models.URLField(max_length=1000, null=True, blank=True)
    canonical_source_url = models.URLField(max_length=1000, null=True, blank=True)
    published_revision_no = models.PositiveIntegerField(null=True, blank=True)
    state = models.CharField(max_length=32, choices=State.choices, default=State.PENDING)
    remote_state = models.CharField(max_length=16, choices=RemoteState.choices, default=RemoteState.UNKNOWN)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    canonical_ready_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = models.Manager.from_queryset(PublicationQuerySet)()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["article_id", "target"], name="uq_article_publication_target"),
            models.UniqueConstraint(fields=["target", "remote_lookup_key"], name="uq_target_remote_lookup"),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            if (
                not isinstance(self.remote_lookup_key, str)
                or not self.remote_lookup_key.strip()
                or not PublicationTargetSnapshot.objects.filter(
                    id=self.origin_target_snapshot_id,
                    target_id=self.target_id,
                ).exists()
            ):
                raise TypeError("Publication frozen identity is invalid")
        else:
            update_fields = kwargs.get("update_fields")
            if update_fields is None:
                frozen = type(self).objects.filter(pk=self.pk).values(
                    *PublicationQuerySet.FROZEN_IDENTITY_ATTNAMES
                ).first()
                if frozen is None:
                    raise TypeError("Publication frozen identity is immutable")
                for field_name, stored in frozen.items():
                    if getattr(self, field_name) != stored:
                        raise TypeError("Publication frozen identity is immutable")
            else:
                PublicationQuerySet._reject_frozen_fields(update_fields)
        return super().save(*args, **kwargs)


class PublicationRecoveryState(models.TextChoices):
    NOT_REQUIRED = "not_required", "Not required"
    IN_PROGRESS = "in_progress", "In progress"
    AUTOMATIC_RETRY = "automatic_retry", "Automatic retry"
    RECONCILING = "reconciling", "Reconciling"
    MANUAL_REQUIRED = "manual_required", "Manual required"
    STOPPED = "stopped", "Stopped"


class PublicationAttemptQuerySet(models.QuerySet):
    FROZEN_IDENTITY_FIELDS = frozenset(
        {
            "publication",
            "publication_id",
            "article_revision",
            "article_revision_id",
            "publication_intent",
            "publication_intent_id",
            "target_snapshot",
            "target_snapshot_id",
            "target_config_hash",
            "resolved_action",
            "target_command_hash",
            "publisher_contract_version",
            "publisher_adapter_manifest_hash",
            "approval",
            "approval_id",
            "approval_subject_hash",
            "auto_publish_activation_id",
            "auto_publish_activation_hash",
            "idempotency_key",
            "remote_lookup_key",
            "request_fingerprint",
            "execution_identity_version",
            "correlation_id",
            "created_at",
        }
    )
    FROZEN_IDENTITY_ATTNAMES = frozenset(
        {
            "publication_id",
            "article_revision_id",
            "publication_intent_id",
            "target_snapshot_id",
            "target_config_hash",
            "resolved_action",
            "target_command_hash",
            "publisher_contract_version",
            "publisher_adapter_manifest_hash",
            "approval_id",
            "approval_subject_hash",
            "auto_publish_activation_id",
            "auto_publish_activation_hash",
            "idempotency_key",
            "remote_lookup_key",
            "request_fingerprint",
            "execution_identity_version",
            "correlation_id",
            "created_at",
        }
    )

    @classmethod
    def _reject_frozen_fields(cls, fields) -> None:
        if cls.FROZEN_IDENTITY_FIELDS.intersection(fields):
            raise TypeError("PublicationAttempt frozen identity is immutable")

    def update(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return super().update(**kwargs)

    async def aupdate(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return await super().aupdate(**kwargs)

    def delete(self):
        raise TypeError("PublicationAttempt is append-only")

    async def adelete(self):
        raise TypeError("PublicationAttempt is append-only")

    def _raw_delete(self, using):
        raise TypeError("PublicationAttempt is append-only")

    def bulk_update(self, objs, fields, batch_size=None):
        self._reject_frozen_fields(fields)
        return super().bulk_update(objs, fields, batch_size=batch_size)

    async def abulk_update(self, objs, fields, batch_size=None):
        self._reject_frozen_fields(fields)
        return await super().abulk_update(objs, fields, batch_size=batch_size)


class PublicationAttempt(models.Model):
    class State(models.TextChoices):
        QUEUED = "queued", "대기"
        RUNNING = "running", "실행"
        SUCCEEDED = "succeeded", "성공"
        RETRYABLE_FAILED = "retryable_failed", "재시도 가능 실패"
        PERMANENT_FAILED = "permanent_failed", "영구 실패"
        UNKNOWN_OUTCOME = "unknown_outcome", "결과 불명"
        RECONCILING = "reconciling", "조정 중"
        MANUAL_REQUIRED = "manual_required", "수동 확인"
        STALE = "stale", "만료"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(Publication, on_delete=models.PROTECT, related_name="attempts")
    article_revision = models.ForeignKey(
        "editorial.ArticleRevision", on_delete=models.PROTECT, related_name="publication_attempts"
    )
    publication_intent = models.ForeignKey(PublicationIntent, on_delete=models.PROTECT, related_name="attempts")
    target_snapshot = models.ForeignKey(PublicationTargetSnapshot, on_delete=models.PROTECT)
    target_config_hash = models.CharField(max_length=64)
    resolved_action = models.CharField(max_length=24, choices=PublicationAction.choices)
    target_command_hash = models.CharField(max_length=64)
    publisher_contract_version = models.CharField(max_length=100)
    publisher_adapter_manifest_hash = models.CharField(max_length=64)
    approval = models.ForeignKey(Approval, on_delete=models.PROTECT)
    approval_subject_hash = models.CharField(max_length=64)
    auto_publish_activation_id = models.UUIDField(null=True, blank=True)
    auto_publish_activation_hash = models.CharField(max_length=64, null=True, blank=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    remote_lookup_key = models.CharField(max_length=255)
    request_fingerprint = models.CharField(max_length=64)
    state = models.CharField(max_length=24, choices=State.choices, default=State.QUEUED)
    attempt_no = models.PositiveIntegerField(default=1)
    reconcile_attempt_no = models.PositiveIntegerField(default=0)
    correlation_id = models.UUIDField(db_index=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    execution_identity_version = models.CharField(
        max_length=40,
        default=PUBLICATION_EXECUTION_IDENTITY_VERSION,
    )
    execution_generation = models.PositiveBigIntegerField(default=0)
    active_source_event = models.ForeignKey(
        "infrastructure.OutboxMessage",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="active_publication_attempts",
    )
    active_consumer_name = models.CharField(max_length=160, blank=True)
    active_consumer_lease_generation = models.PositiveBigIntegerField(default=0)
    active_consumer_lease_token = models.UUIDField(null=True, blank=True)
    active_lease_token_hash = models.CharField(max_length=64, blank=True)
    active_lease_expires_at = models.DateTimeField(null=True, blank=True)
    active_write_marker = models.CharField(max_length=64, blank=True)
    active_write_started_at = models.DateTimeField(null=True, blank=True)
    terminal_event_key = models.CharField(max_length=255, blank=True)
    terminal_generation = models.PositiveBigIntegerField(default=0)
    terminal_state = models.CharField(max_length=24, blank=True)
    terminal_impact = models.JSONField(default=dict, blank=True)
    recovery_state = models.CharField(
        max_length=32,
        choices=PublicationRecoveryState.choices,
        default=PublicationRecoveryState.IN_PROGRESS,
    )
    next_recovery_at = models.DateTimeField(null=True, blank=True)
    remote_request_id = models.CharField(max_length=255, blank=True)
    http_status = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_detail_redacted = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(PublicationAttemptQuerySet)()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(reconcile_attempt_no__lte=5),
                name="ck_publication_reconcile_attempt_no_lte_5",
            ),
            models.CheckConstraint(
                condition=Q(attempt_no__gte=1, attempt_no__lte=5),
                name="ck_publication_attempt_no_1_5",
            ),
            models.UniqueConstraint(
                fields=["publication_intent", "publication"],
                name="uq_attempt_intent_publication",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None:
                frozen = type(self).objects.filter(pk=self.pk).values(
                    *PublicationAttemptQuerySet.FROZEN_IDENTITY_ATTNAMES
                ).first()
                if frozen is None:
                    raise TypeError("PublicationAttempt is append-only")
                for field_name, stored in frozen.items():
                    if getattr(self, field_name) != stored:
                        raise TypeError(
                            "PublicationAttempt frozen identity is immutable"
                        )
            else:
                PublicationAttemptQuerySet._reject_frozen_fields(update_fields)
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("PublicationAttempt is append-only")

class PublicationApprovalHead(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication_intent = models.ForeignKey(
        PublicationIntent,
        on_delete=models.CASCADE,
        related_name="approval_heads",
    )
    target = models.ForeignKey(
        PublicationTarget,
        on_delete=models.PROTECT,
        related_name="approval_heads",
    )
    latest_approval = models.ForeignKey(
        Approval,
        on_delete=models.PROTECT,
        related_name="headed_by",
    )
    version = models.PositiveIntegerField()
    subject_hash = models.CharField(max_length=64)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("publication_intent", "target"),
                name="uq_publication_approval_head",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="ck_publication_approval_head_version_positive",
            ),
        ]


class PublicationExecutionObservationQuerySet(models.QuerySet):
    FROZEN_FIELDS = frozenset(
        {
            "publication_attempt",
            "publication_attempt_id",
            "execution_attempt_no",
            "identity_version",
            "execution_generation",
            "correlation_id",
            "source_event",
            "source_event_id",
            "worker_task_id",
            "consumer_name",
            "consumer_lease_generation",
            "consumer_lease_token",
            "lease_token_hash",
            "write_marker",
            "started_at",
            "created_at",
        }
    )
    FROZEN_ATTNAMES = frozenset(
        {
            "publication_attempt_id",
            "execution_attempt_no",
            "identity_version",
            "execution_generation",
            "correlation_id",
            "source_event_id",
            "worker_task_id",
            "consumer_name",
            "consumer_lease_generation",
            "consumer_lease_token",
            "lease_token_hash",
            "write_marker",
            "started_at",
            "created_at",
        }
    )

    @classmethod
    def _reject_frozen_fields(cls, fields) -> None:
        if cls.FROZEN_FIELDS.intersection(fields):
            raise TypeError("PublicationExecutionObservation identity is immutable")

    def update(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return super().update(**kwargs)

    async def aupdate(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return await super().aupdate(**kwargs)

    def delete(self):
        raise TypeError("PublicationExecutionObservation is append-only")

    async def adelete(self):
        raise TypeError("PublicationExecutionObservation is append-only")

    def _raw_delete(self, using):
        raise TypeError("PublicationExecutionObservation is append-only")


class PublicationExecutionObservation(models.Model):
    class State(models.TextChoices):
        STARTED = "started", "Started"
        COMPLETED = "completed", "Completed"
        DELIVERY_UNKNOWN = "delivery_unknown", "Delivery unknown"

    class ProjectionDisposition(models.TextChoices):
        PENDING = "pending", "Pending"
        APPLIED = "applied", "Applied"
        STALE_FENCED = "stale_fenced", "Stale fenced"
        NO_RESULT = "no_result", "No result"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication_attempt = models.ForeignKey(
        PublicationAttempt,
        on_delete=models.PROTECT,
        related_name="execution_observations",
    )
    execution_attempt_no = models.PositiveIntegerField()
    identity_version = models.CharField(
        max_length=40,
        default=PUBLICATION_EXECUTION_IDENTITY_VERSION,
    )
    execution_generation = models.PositiveBigIntegerField(default=0)
    correlation_id = models.UUIDField(db_index=True)
    source_event = models.ForeignKey(
        "infrastructure.OutboxMessage",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="publication_execution_observations",
    )
    worker_task_id = models.CharField(max_length=255, blank=True)
    consumer_name = models.CharField(max_length=160, blank=True)
    consumer_lease_generation = models.PositiveBigIntegerField(default=0)
    consumer_lease_token = models.UUIDField(null=True, blank=True)
    lease_token_hash = models.CharField(max_length=64, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    write_marker = models.CharField(max_length=64, blank=True)
    external_write_started_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(
        max_length=24,
        choices=State.choices,
        default=State.STARTED,
    )
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    result_state = models.CharField(max_length=24, blank=True)
    result_identity = models.CharField(max_length=64, blank=True)
    projection_disposition = models.CharField(
        max_length=24,
        choices=ProjectionDisposition.choices,
        default=ProjectionDisposition.PENDING,
    )
    error_code = models.CharField(max_length=100, blank=True)
    retry_at = models.DateTimeField(null=True, blank=True)
    terminal_impact = models.JSONField(default=dict, blank=True)
    recovery_state = models.CharField(
        max_length=32,
        choices=PublicationRecoveryState.choices,
        default=PublicationRecoveryState.IN_PROGRESS,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(
        PublicationExecutionObservationQuerySet
    )()

    class Meta:
        ordering = ["publication_attempt_id", "execution_attempt_no"]
        indexes = [
            models.Index(
                fields=("publication_attempt", "execution_attempt_no"),
                name="idx_pub_exec_attempt_no",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("publication_attempt", "execution_generation"),
                name="uq_publication_execution_generation",
            ),
            models.UniqueConstraint(
                fields=("publication_attempt",),
                condition=Q(state="started"),
                name="uq_active_publication_execution",
            ),
            models.CheckConstraint(
                condition=Q(
                    execution_attempt_no__gte=1,
                    execution_attempt_no__lte=5,
                ),
                name="ck_publication_execution_attempt_no_1_5",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None:
                frozen = type(self).objects.filter(pk=self.pk).values(
                    *PublicationExecutionObservationQuerySet.FROZEN_ATTNAMES
                ).first()
                if frozen is None:
                    raise TypeError(
                        "PublicationExecutionObservation is append-only"
                    )
                for field_name, stored in frozen.items():
                    if getattr(self, field_name) != stored:
                        raise TypeError(
                            "PublicationExecutionObservation identity is immutable"
                        )
            else:
                PublicationExecutionObservationQuerySet._reject_frozen_fields(
                    update_fields
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("PublicationExecutionObservation is append-only")


class PublicationReconcileGenerationQuerySet(models.QuerySet):
    FROZEN_FIELDS = frozenset(
        {
            "publication_attempt",
            "publication_attempt_id",
            "generation",
            "source_event",
            "source_event_id",
            "delivery_identity_version",
            "correlation_id",
            "created_at",
        }
    )
    FROZEN_ATTNAMES = frozenset(
        {
            "publication_attempt_id",
            "generation",
            "source_event_id",
            "delivery_identity_version",
            "correlation_id",
            "created_at",
        }
    )

    @classmethod
    def _reject_frozen_fields(cls, fields) -> None:
        if cls.FROZEN_FIELDS.intersection(fields):
            raise TypeError("PublicationReconcileGeneration identity is immutable")

    def update(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return super().update(**kwargs)

    async def aupdate(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return await super().aupdate(**kwargs)

    def delete(self):
        raise TypeError("PublicationReconcileGeneration is append-only")

    async def adelete(self):
        raise TypeError("PublicationReconcileGeneration is append-only")

    def _raw_delete(self, using):
        raise TypeError("PublicationReconcileGeneration is append-only")


class PublicationReconcileGeneration(models.Model):
    class State(models.TextChoices):
        QUEUED = "queued", "대기"
        RUNNING = "running", "실행"
        COMPLETED = "completed", "완료"
        DELIVERY_FAILED = "delivery_failed", "전달 실패"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication_attempt = models.ForeignKey(
        PublicationAttempt,
        on_delete=models.PROTECT,
        related_name="reconcile_generations",
    )
    generation = models.PositiveSmallIntegerField()
    source_event = models.OneToOneField(
        "infrastructure.OutboxMessage",
        on_delete=models.PROTECT,
        related_name="publication_reconcile_generation",
    )
    delivery_identity_version = models.CharField(
        max_length=40,
        default=PUBLICATION_RECONCILE_IDENTITY_VERSION,
    )
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.QUEUED,
    )
    consumer_name = models.CharField(max_length=160, blank=True)
    consumer_lease_generation = models.PositiveBigIntegerField(default=0)
    consumer_lease_token = models.UUIDField(null=True, blank=True)
    lease_token_hash = models.CharField(max_length=64, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    result_identity = models.CharField(max_length=64, blank=True)
    result_state = models.CharField(max_length=24, blank=True)
    not_before = models.DateTimeField()
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    correlation_id = models.UUIDField(db_index=True)
    worker_task_id = models.CharField(max_length=255, blank=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    terminal_impact = models.JSONField(default=dict, blank=True)
    recovery_state = models.CharField(
        max_length=32,
        choices=PublicationRecoveryState.choices,
        default=PublicationRecoveryState.IN_PROGRESS,
    )
    next_recovery_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(
        PublicationReconcileGenerationQuerySet
    )()

    class Meta:
        ordering = ["publication_attempt_id", "generation"]
        constraints = [
            models.UniqueConstraint(
                fields=("publication_attempt", "generation"),
                name="uq_publication_reconcile_generation",
            ),
            models.UniqueConstraint(
                fields=("publication_attempt",),
                condition=Q(state__in=("queued", "running")),
                name="uq_active_publication_reconcile",
            ),
            models.CheckConstraint(
                condition=Q(generation__gte=1, generation__lte=5),
                name="ck_publication_reconcile_generation_1_5",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None:
                frozen = type(self).objects.filter(pk=self.pk).values(
                    *PublicationReconcileGenerationQuerySet.FROZEN_ATTNAMES
                ).first()
                if frozen is None:
                    raise TypeError(
                        "PublicationReconcileGeneration is append-only"
                    )
                for field_name, stored in frozen.items():
                    if getattr(self, field_name) != stored:
                        raise TypeError(
                            "PublicationReconcileGeneration identity is immutable"
                        )
            else:
                PublicationReconcileGenerationQuerySet._reject_frozen_fields(
                    update_fields
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("PublicationReconcileGeneration is append-only")


class PublicationReconcileDeliveryObservationQuerySet(models.QuerySet):
    FROZEN_FIELDS = frozenset(
        {
            "reconcile_generation",
            "reconcile_generation_id",
            "consumer_name",
            "consumer_lease_generation",
            "consumer_lease_token",
            "lease_token_hash",
            "lease_expires_at",
            "started_at",
            "created_at",
        }
    )
    FROZEN_ATTNAMES = frozenset(
        {
            "reconcile_generation_id",
            "consumer_name",
            "consumer_lease_generation",
            "consumer_lease_token",
            "lease_token_hash",
            "lease_expires_at",
            "started_at",
            "created_at",
        }
    )

    @classmethod
    def _reject_frozen_fields(cls, fields) -> None:
        if cls.FROZEN_FIELDS.intersection(fields):
            raise TypeError(
                "PublicationReconcileDeliveryObservation identity is immutable"
            )

    def update(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return super().update(**kwargs)

    async def aupdate(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return await super().aupdate(**kwargs)

    def delete(self):
        raise TypeError(
            "PublicationReconcileDeliveryObservation is append-only"
        )

    async def adelete(self):
        raise TypeError(
            "PublicationReconcileDeliveryObservation is append-only"
        )

    def _raw_delete(self, using):
        raise TypeError(
            "PublicationReconcileDeliveryObservation is append-only"
        )


class PublicationReconcileDeliveryObservation(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        SUPERSEDED = "superseded", "Superseded"
        SETTLED = "settled", "Settled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reconcile_generation = models.ForeignKey(
        PublicationReconcileGeneration,
        on_delete=models.PROTECT,
        related_name="delivery_observations",
    )
    consumer_name = models.CharField(max_length=160)
    consumer_lease_generation = models.PositiveBigIntegerField()
    consumer_lease_token = models.UUIDField()
    lease_token_hash = models.CharField(max_length=64)
    lease_expires_at = models.DateTimeField()
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.ACTIVE,
    )
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(
        PublicationReconcileDeliveryObservationQuerySet
    )()

    class Meta:
        ordering = ["reconcile_generation_id", "consumer_lease_generation"]
        constraints = [
            models.UniqueConstraint(
                fields=("reconcile_generation", "consumer_lease_generation"),
                name="uq_publication_reconcile_delivery_generation",
            ),
            models.UniqueConstraint(
                fields=("reconcile_generation",),
                condition=Q(state="active"),
                name="uq_active_publication_reconcile_delivery",
            ),
            models.CheckConstraint(
                condition=(
                    Q(state="active", finished_at__isnull=True)
                    | Q(
                        state__in=("superseded", "settled"),
                        finished_at__isnull=False,
                    )
                ),
                name="ck_publication_reconcile_delivery_state",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None:
                frozen = type(self).objects.filter(pk=self.pk).values(
                    *PublicationReconcileDeliveryObservationQuerySet.FROZEN_ATTNAMES
                ).first()
                if frozen is None:
                    raise TypeError(
                        "PublicationReconcileDeliveryObservation is append-only"
                    )
                for field_name, stored in frozen.items():
                    if getattr(self, field_name) != stored:
                        raise TypeError(
                            "PublicationReconcileDeliveryObservation identity is immutable"
                        )
            else:
                PublicationReconcileDeliveryObservationQuerySet._reject_frozen_fields(
                    update_fields
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError(
            "PublicationReconcileDeliveryObservation is append-only"
        )


class PublicationLateExecutionResultQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError("PublicationLateExecutionResult is append-only")

    async def aupdate(self, **kwargs):
        raise TypeError("PublicationLateExecutionResult is append-only")

    def delete(self):
        raise TypeError("PublicationLateExecutionResult is append-only")

    async def adelete(self):
        raise TypeError("PublicationLateExecutionResult is append-only")

    def _raw_delete(self, using):
        raise TypeError("PublicationLateExecutionResult is append-only")

    def bulk_update(self, objs, fields, batch_size=None):
        raise TypeError("PublicationLateExecutionResult is append-only")

    async def abulk_update(self, objs, fields, batch_size=None):
        raise TypeError("PublicationLateExecutionResult is append-only")


class PublicationLateExecutionResult(models.Model):
    class ProjectionDisposition(models.TextChoices):
        STALE_FENCED = "stale_fenced", "Stale fenced"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    execution_observation = models.ForeignKey(
        PublicationExecutionObservation,
        on_delete=models.PROTECT,
        related_name="late_results",
        null=True,
        blank=True,
    )
    reconcile_generation = models.ForeignKey(
        PublicationReconcileGeneration,
        on_delete=models.PROTECT,
        related_name="late_results",
        null=True,
        blank=True,
    )
    reconcile_delivery_observation = models.ForeignKey(
        PublicationReconcileDeliveryObservation,
        on_delete=models.PROTECT,
        related_name="late_results",
        null=True,
        blank=True,
    )
    source_event = models.ForeignKey(
        "infrastructure.OutboxMessage",
        on_delete=models.PROTECT,
        related_name="late_publication_results",
        null=True,
        blank=True,
    )
    consumer_name = models.CharField(max_length=160, blank=True)
    consumer_lease_generation = models.PositiveBigIntegerField(default=0)
    consumer_lease_token = models.UUIDField(null=True, blank=True)
    lease_token_hash = models.CharField(max_length=64, blank=True)
    result_identity = models.CharField(max_length=64)
    result_state = models.CharField(max_length=24)
    remote_post_id = models.CharField(max_length=255, blank=True)
    remote_url = models.URLField(max_length=1000, null=True, blank=True)
    remote_url_hash = models.CharField(max_length=64, blank=True, default="")
    remote_state = models.CharField(max_length=24, blank=True)
    remote_revision = models.CharField(max_length=255, blank=True)
    remote_request_id = models.CharField(max_length=255, blank=True)
    http_status = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    projection_disposition = models.CharField(
        max_length=24,
        choices=ProjectionDisposition.choices,
        default=ProjectionDisposition.STALE_FENCED,
    )
    observed_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(PublicationLateExecutionResultQuerySet)()

    class Meta:
        ordering = ["execution_observation_id", "observed_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        execution_observation__isnull=False,
                        reconcile_generation__isnull=True,
                        reconcile_delivery_observation__isnull=True,
                    )
                    | Q(
                        execution_observation__isnull=True,
                        reconcile_generation__isnull=False,
                        reconcile_delivery_observation__isnull=False,
                    )
                ),
                name="ck_publication_late_result_exact_parent",
            ),
            models.UniqueConstraint(
                fields=("execution_observation", "result_identity"),
                name="uq_publication_late_result_identity",
            ),
            models.UniqueConstraint(
                fields=("reconcile_delivery_observation", "result_identity"),
                condition=Q(reconcile_delivery_observation__isnull=False),
                name="uq_publication_late_reconcile_result_identity",
            ),
            models.CheckConstraint(
                condition=(
                    Q(remote_url__isnull=False, remote_url_hash="")
                    | Q(remote_url__isnull=True, remote_url_hash="")
                    | Q(remote_url__isnull=True)
                    & ~Q(remote_url_hash="")
                ),
                name="ck_publication_late_remote_url_fact",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("PublicationLateExecutionResult is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("PublicationLateExecutionResult is append-only")


class _AppendOnlyPublishedAssetQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation() -> None:
        raise TypeError("Published asset snapshot is append-only")

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


class _AppendOnlyPublishedAssetModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(_AppendOnlyPublishedAssetQuerySet)()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("Published asset snapshot is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Published asset snapshot is append-only")


class PublishedAssetCohort(_AppendOnlyPublishedAssetModel):
    class MaterialState(models.TextChoices):
        CURRENT = "current", "현재 정본"
        LEGACY_UNVERIFIABLE = "legacy_unverifiable", "레거시 검증 불가"

    revision = models.OneToOneField(
        "editorial.ArticleRevision",
        on_delete=models.PROTECT,
        related_name="published_asset_cohort",
    )
    schema_version = models.CharField(
        max_length=40,
        default=PUBLISHED_ASSET_COHORT_VERSION,
    )
    material_state = models.CharField(
        max_length=24,
        choices=MaterialState.choices,
        default=MaterialState.CURRENT,
    )
    item_count = models.PositiveIntegerField()
    manifest = models.JSONField(default=list)
    manifest_hash = models.CharField(max_length=64, validators=[sha256_validator])


class PublishedEvidenceSnapshot(_AppendOnlyPublishedAssetModel):
    cohort = models.ForeignKey(
        PublishedAssetCohort,
        on_delete=models.PROTECT,
        related_name="evidence_snapshots",
    )
    visual_placement = models.OneToOneField(
        "editorial.VisualPlacement",
        on_delete=models.PROTECT,
        related_name="published_evidence_snapshot",
    )
    evidence = models.ForeignKey(
        "evidence.EvidenceAsset",
        on_delete=models.PROTECT,
        related_name="published_asset_snapshots",
    )
    source_item_id = models.UUIDField()
    source_version_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    source_url_snapshot = models.URLField(max_length=1000)
    source_title_snapshot = models.CharField(max_length=1000)
    source_publisher_snapshot = models.CharField(max_length=300)
    source_published_at_snapshot = models.DateTimeField(null=True, blank=True)
    source_modified_at_snapshot = models.DateTimeField(null=True, blank=True)
    source_collected_at_snapshot = models.DateTimeField()
    evidence_content_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    asset_checksum = models.CharField(max_length=64, validators=[sha256_validator])
    object_key = models.CharField(max_length=1024)
    object_version = models.CharField(max_length=500)
    mime_type = models.CharField(max_length=255)
    byte_size = models.PositiveBigIntegerField()
    locator_snapshot = models.JSONField(default=dict)
    rights_status_snapshot = models.CharField(max_length=32)
    rights_basis_url_snapshot = models.URLField(max_length=1000)
    attribution_snapshot = models.TextField(blank=True)
    alt_text_snapshot = models.TextField()
    caption_snapshot = models.TextField()
    presentation_hash = models.CharField(max_length=64, validators=[sha256_validator])


class PublishedVisualizationSnapshot(_AppendOnlyPublishedAssetModel):
    cohort = models.ForeignKey(
        PublishedAssetCohort,
        on_delete=models.PROTECT,
        related_name="visualization_snapshots",
    )
    visual_placement = models.OneToOneField(
        "editorial.VisualPlacement",
        on_delete=models.PROTECT,
        related_name="published_visualization_snapshot",
    )
    visualization = models.ForeignKey(
        "editorial.VisualizationRender",
        on_delete=models.PROTECT,
        related_name="published_snapshots",
    )
    output_checksum = models.CharField(max_length=64, validators=[sha256_validator])
    object_key = models.CharField(max_length=1000)
    object_version = models.CharField(max_length=500)
    input_manifest_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    transform_hash = models.CharField(max_length=64, validators=[sha256_validator])
    renderer_manifest_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    mime_type = models.CharField(max_length=255)
    byte_size = models.PositiveBigIntegerField()
    rights_status_snapshot = models.CharField(max_length=32)
    rights_basis_url_snapshot = models.URLField(max_length=1000)
    attribution_snapshot = models.TextField(blank=True)
    alt_text_snapshot = models.TextField()
    caption_snapshot = models.TextField()
    presentation_hash = models.CharField(max_length=64, validators=[sha256_validator])


class PublishedVisualizationInput(_AppendOnlyPublishedAssetModel):
    visualization_snapshot = models.ForeignKey(
        PublishedVisualizationSnapshot,
        on_delete=models.PROTECT,
        related_name="input_links",
    )
    evidence_snapshot = models.ForeignKey(
        PublishedEvidenceSnapshot,
        on_delete=models.PROTECT,
        related_name="visualization_input_links",
    )
    display_order = models.PositiveIntegerField()
    input_material_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("visualization_snapshot", "display_order"),
                name="uq_published_visualization_input_order",
            ),
            models.UniqueConstraint(
                fields=("visualization_snapshot", "evidence_snapshot"),
                name="uq_published_visualization_input_evidence",
            ),
        ]


class _FrozenMediaIdentityQuerySet(models.QuerySet):
    FROZEN_IDENTITY_FIELDS = frozenset()

    @classmethod
    def _reject_frozen_fields(cls, fields) -> None:
        if cls.FROZEN_IDENTITY_FIELDS.intersection(fields):
            raise TypeError("Publication media identity is immutable")

    def update(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return super().update(**kwargs)

    async def aupdate(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return await super().aupdate(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        self._reject_frozen_fields(fields)
        return super().bulk_update(objs, fields, batch_size=batch_size)

    async def abulk_update(self, objs, fields, batch_size=None):
        self._reject_frozen_fields(fields)
        return await super().abulk_update(objs, fields, batch_size=batch_size)

    def delete(self):
        raise TypeError("Publication media lineage is append-only")

    async def adelete(self):
        raise TypeError("Publication media lineage is append-only")

    def _raw_delete(self, using):
        raise TypeError("Publication media lineage is append-only")


class _FrozenMediaIdentityModel(models.Model):
    FROZEN_IDENTITY_ATTNAMES = frozenset()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                type(self).objects.all()._reject_frozen_fields(update_fields)
            else:
                stored = type(self).objects.filter(pk=self.pk).values(
                    *self.FROZEN_IDENTITY_ATTNAMES
                ).first()
                if stored is None or any(
                    getattr(self, field) != value
                    for field, value in stored.items()
                ):
                    raise TypeError("Publication media identity is immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Publication media lineage is append-only")


class RemoteMediaQuerySet(_FrozenMediaIdentityQuerySet):
    FROZEN_IDENTITY_FIELDS = frozenset(
        {
            "target",
            "target_id",
            "asset_checksum",
            "presentation_hash",
            "remote_lookup_key",
            "request_fingerprint",
        }
    )


class RemoteMedia(_FrozenMediaIdentityModel):
    FROZEN_IDENTITY_ATTNAMES = frozenset(
        {
            "target_id",
            "asset_checksum",
            "presentation_hash",
            "remote_lookup_key",
            "request_fingerprint",
        }
    )
    class State(models.TextChoices):
        PENDING = "pending", "대기"
        UPLOADING = "uploading", "업로드 중"
        AVAILABLE = "available", "사용 가능"
        RECONCILING = "reconciling", "조정 중"
        ORPHANED = "orphaned", "미참조"
        DELETED = "deleted", "삭제"
        FAILED = "failed", "실패"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.ForeignKey(PublicationTarget, on_delete=models.PROTECT, related_name="remote_media")
    asset_checksum = models.CharField(max_length=64, validators=[sha256_validator])
    presentation_hash = models.CharField(max_length=64, validators=[sha256_validator])
    remote_lookup_key = models.CharField(max_length=255)
    remote_media_id = models.CharField(max_length=255, null=True, blank=True)
    remote_source_url = models.URLField(max_length=1000, null=True, blank=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    request_fingerprint = models.CharField(max_length=64)
    lease_generation = models.PositiveIntegerField(default=1)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    last_reconciled_at = models.DateTimeField(null=True, blank=True)
    orphaned_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    delete_reason = models.CharField(max_length=500, blank=True)
    last_reconcile_hash = models.CharField(max_length=64, blank=True)
    objects = models.Manager.from_queryset(RemoteMediaQuerySet)()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["target", "asset_checksum", "presentation_hash"],
                name="uq_remote_media_presentation",
            ),
            models.UniqueConstraint(
                fields=["target", "remote_lookup_key"], name="uq_remote_media_lookup"
            ),
        ]


class PublicDeliveryAssetQuerySet(_FrozenMediaIdentityQuerySet):
    FROZEN_IDENTITY_FIELDS = frozenset(
        {
            "asset_checksum",
            "presentation_hash",
            "mime_type",
            "byte_size",
            "delivery_object_key",
            "rights_status_snapshot",
            "alt_text_snapshot",
            "caption_snapshot",
            "attribution_snapshot",
            "created_at",
        }
    )


class PublicDeliveryAsset(_FrozenMediaIdentityModel):
    FROZEN_IDENTITY_ATTNAMES = PublicDeliveryAssetQuerySet.FROZEN_IDENTITY_FIELDS
    class State(models.TextChoices):
        PENDING = "pending", "Preparing"
        AVAILABLE = "available", "사용 가능"
        WITHDRAWAL_PENDING = "withdrawal_pending", "철회 대기"
        PENDING_DELETE = "pending_delete", "삭제 대기"
        DELETED = "deleted", "삭제"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset_checksum = models.CharField(max_length=64, validators=[sha256_validator])
    presentation_hash = models.CharField(max_length=64, validators=[sha256_validator])
    mime_type = models.CharField(max_length=255)
    byte_size = models.PositiveBigIntegerField()
    delivery_object_key = models.CharField(max_length=1000)
    delivery_object_version = models.CharField(max_length=255, blank=True)
    public_url = models.URLField(max_length=1000, unique=True, null=True, blank=True)
    rights_status_snapshot = models.CharField(max_length=32)
    alt_text_snapshot = models.CharField(max_length=1000)
    caption_snapshot = models.TextField(blank=True)
    attribution_snapshot = models.TextField(blank=True)
    state = models.CharField(max_length=24, choices=State.choices, default=State.PENDING)
    active_reference_count = models.PositiveIntegerField(default=0)
    lease_generation = models.PositiveIntegerField(default=1)
    last_remote_body_hash = models.CharField(max_length=64, blank=True)
    last_reconciled_at = models.DateTimeField(null=True, blank=True)
    zero_reference_at = models.DateTimeField(null=True, blank=True)
    delete_after = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    delete_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(PublicDeliveryAssetQuerySet)()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["asset_checksum", "presentation_hash"], name="uq_delivery_asset_presentation"
            )
        ]


class PublicationMediaQuerySet(_FrozenMediaIdentityQuerySet):
    FROZEN_IDENTITY_FIELDS = frozenset(
        {
            "publication",
            "publication_id",
            "remote_media",
            "remote_media_id",
            "public_delivery_asset",
            "public_delivery_asset_id",
            "article_revision",
            "article_revision_id",
            "asset_cohort",
            "asset_cohort_id",
            "published_evidence_snapshot",
            "published_evidence_snapshot_id",
            "published_visualization_snapshot",
            "published_visualization_snapshot_id",
            "usage",
            "block_id",
            "display_order",
            "alt_text_snapshot",
            "caption_snapshot",
            "attribution_snapshot",
            "created_at",
        }
    )


class PublicationMedia(_FrozenMediaIdentityModel):
    FROZEN_IDENTITY_ATTNAMES = frozenset(
        field
        for field in PublicationMediaQuerySet.FROZEN_IDENTITY_FIELDS
        if field not in {
            "publication",
            "remote_media",
            "public_delivery_asset",
            "article_revision",
            "asset_cohort",
            "published_evidence_snapshot",
            "published_visualization_snapshot",
        }
    )
    class Usage(models.TextChoices):
        INLINE = "inline", "본문"
        FEATURED = "featured", "대표"

    class BindingState(models.TextChoices):
        PREPARED = "prepared", "준비"
        ACTIVE = "active", "활성"
        REMOVAL_PENDING = "removal_pending", "제거 대기"
        REMOVED = "removed", "제거"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(Publication, on_delete=models.PROTECT, related_name="media_bindings")
    remote_media = models.ForeignKey(RemoteMedia, null=True, blank=True, on_delete=models.PROTECT)
    public_delivery_asset = models.ForeignKey(
        PublicDeliveryAsset, null=True, blank=True, on_delete=models.PROTECT
    )
    article_revision = models.ForeignKey(
        "editorial.ArticleRevision", on_delete=models.PROTECT, related_name="publication_media_bindings"
    )
    asset_cohort = models.ForeignKey(
        PublishedAssetCohort,
        on_delete=models.PROTECT,
        related_name="publication_media_bindings",
    )
    published_evidence_snapshot = models.ForeignKey(
        PublishedEvidenceSnapshot,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="publication_media_bindings",
    )
    published_visualization_snapshot = models.ForeignKey(
        PublishedVisualizationSnapshot,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="publication_media_bindings",
    )
    usage = models.CharField(max_length=16, choices=Usage.choices)
    block_id = models.CharField(max_length=255)
    display_order = models.PositiveIntegerField(default=0)
    alt_text_snapshot = models.CharField(max_length=1000)
    caption_snapshot = models.TextField(blank=True)
    attribution_snapshot = models.TextField(blank=True)
    binding_state = models.CharField(
        max_length=24, choices=BindingState.choices, default=BindingState.PREPARED
    )
    lease_generation = models.PositiveIntegerField(default=1)
    remote_body_hash = models.CharField(max_length=64, blank=True)
    remote_verified_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(PublicationMediaQuerySet)()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(remote_media__isnull=False, public_delivery_asset__isnull=True)
                    | Q(remote_media__isnull=True, public_delivery_asset__isnull=False)
                ),
                name="ck_publication_media_delivery_xor",
            ),
            models.CheckConstraint(
                condition=(
                    Q(published_evidence_snapshot__isnull=False, published_visualization_snapshot__isnull=True)
                    | Q(published_evidence_snapshot__isnull=True, published_visualization_snapshot__isnull=False)
                ),
                name="ck_publication_media_provenance_xor",
            ),
            models.UniqueConstraint(
                fields=["publication", "published_evidence_snapshot"],
                condition=Q(published_evidence_snapshot__isnull=False),
                name="uq_publication_evidence_snapshot",
            ),
            models.UniqueConstraint(
                fields=["publication", "published_visualization_snapshot"],
                condition=Q(published_visualization_snapshot__isnull=False),
                name="uq_publication_visual_snapshot",
            ),
            models.UniqueConstraint(
                fields=["publication", "article_revision", "remote_media", "block_id"],
                condition=Q(remote_media__isnull=False),
                name="uq_publication_remote_media_block",
            ),
            models.UniqueConstraint(
                fields=["publication", "article_revision", "public_delivery_asset", "block_id"],
                condition=Q(public_delivery_asset__isnull=False),
                name="uq_publication_delivery_asset_block",
            ),
        ]


class MediaDeliveryOperationQuerySet(models.QuerySet):
    FROZEN_IDENTITY_FIELDS = frozenset(
        {
            "mapping_kind",
            "remote_media",
            "remote_media_id",
            "public_delivery_asset",
            "public_delivery_asset_id",
            "publication_attempt",
            "publication_attempt_id",
            "publication_intent",
            "publication_intent_id",
            "action",
            "generation",
            "source_event",
            "source_event_id",
            "target_snapshot_id",
            "target_config_hash",
            "material_hash",
            "created_at",
        }
    )

    @classmethod
    def _reject_frozen_fields(cls, fields) -> None:
        if cls.FROZEN_IDENTITY_FIELDS.intersection(fields):
            raise TypeError("Media delivery operation identity is immutable")

    def update(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return super().update(**kwargs)

    async def aupdate(self, **kwargs):
        self._reject_frozen_fields(kwargs)
        return await super().aupdate(**kwargs)

    def delete(self):
        raise TypeError("Media delivery operation is append-only")

    async def adelete(self):
        raise TypeError("Media delivery operation is append-only")

    def _raw_delete(self, using):
        raise TypeError("Media delivery operation is append-only")


class MediaDeliveryOperation(models.Model):
    class MappingKind(models.TextChoices):
        REMOTE_MEDIA = "remote_media", "WordPress media"
        PUBLIC_DELIVERY_ASSET = "public_delivery_asset", "Public delivery asset"

    class Action(models.TextChoices):
        UPLOAD = "upload", "Upload"
        PREPARE = "prepare", "Prepare"
        RECONCILE = "reconcile", "Reconcile"
        DELETE = "delete", "Delete"

    class State(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        UNKNOWN_OUTCOME = "unknown_outcome", "Unknown outcome"
        MANUAL_REQUIRED = "manual_required", "Manual review required"
        DELIVERY_FAILED = "delivery_failed", "Delivery failed"
        SUPERSEDED = "superseded", "Superseded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    mapping_kind = models.CharField(max_length=32, choices=MappingKind.choices)
    remote_media = models.ForeignKey(
        RemoteMedia,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="delivery_operations",
    )
    public_delivery_asset = models.ForeignKey(
        PublicDeliveryAsset,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="delivery_operations",
    )
    publication_attempt = models.ForeignKey(
        PublicationAttempt,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="media_delivery_operations",
    )
    publication_intent = models.ForeignKey(
        PublicationIntent,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="media_delivery_operations",
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    generation = models.PositiveIntegerField()
    source_event = models.OneToOneField(
        "infrastructure.OutboxMessage",
        on_delete=models.PROTECT,
        related_name="media_delivery_operation",
    )
    target_snapshot_id = models.UUIDField(null=True, blank=True)
    target_config_hash = models.CharField(max_length=64, blank=True)
    material_hash = models.CharField(max_length=64, validators=[sha256_validator])
    state = models.CharField(max_length=24, choices=State.choices, default=State.QUEUED)
    consumer_name = models.CharField(max_length=160, blank=True)
    consumer_lease_generation = models.PositiveBigIntegerField(default=0)
    consumer_lease_token = models.UUIDField(null=True, blank=True)
    lease_token_hash = models.CharField(max_length=64, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    write_marker = models.CharField(max_length=64, blank=True)
    external_write_started_at = models.DateTimeField(null=True, blank=True)
    result_hash = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(MediaDeliveryOperationQuerySet)()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        mapping_kind="remote_media",
                        remote_media__isnull=False,
                        public_delivery_asset__isnull=True,
                    )
                    | Q(
                        mapping_kind="public_delivery_asset",
                        remote_media__isnull=True,
                        public_delivery_asset__isnull=False,
                    )
                ),
                name="ck_media_operation_mapping_xor",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        publication_attempt__isnull=True,
                        publication_intent__isnull=True,
                    )
                    | Q(
                        publication_attempt__isnull=False,
                        publication_intent__isnull=False,
                    )
                ),
                name="ck_media_operation_publication_pair",
            ),
            models.CheckConstraint(
                condition=Q(generation__gte=1, generation__lte=5),
                name="ck_media_operation_generation_1_5",
            ),
            models.UniqueConstraint(
                fields=("remote_media", "generation"),
                condition=Q(remote_media__isnull=False),
                name="uq_remote_media_operation_generation",
            ),
            models.UniqueConstraint(
                fields=("public_delivery_asset", "generation"),
                condition=Q(public_delivery_asset__isnull=False),
                name="uq_delivery_asset_operation_generation",
            ),
            models.UniqueConstraint(
                fields=("remote_media",),
                condition=Q(
                    remote_media__isnull=False,
                    state__in=("queued", "running"),
                ),
                name="uq_active_remote_media_operation",
            ),
            models.UniqueConstraint(
                fields=("public_delivery_asset",),
                condition=Q(
                    public_delivery_asset__isnull=False,
                    state__in=("queued", "running"),
                ),
                name="uq_active_delivery_asset_operation",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                MediaDeliveryOperationQuerySet._reject_frozen_fields(update_fields)
            else:
                frozen_names = {
                    field
                    for field in MediaDeliveryOperationQuerySet.FROZEN_IDENTITY_FIELDS
                    if not field.endswith(("_media", "_asset", "_attempt", "_intent", "_event"))
                    and field
                    not in {
                        "remote_media",
                        "public_delivery_asset",
                        "publication_attempt",
                        "publication_intent",
                        "source_event",
                    }
                }
                stored = type(self).objects.filter(pk=self.pk).values(
                    *frozen_names
                ).first()
                if stored is None or any(
                    getattr(self, field) != value for field, value in stored.items()
                ):
                    raise TypeError("Media delivery operation identity is immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Media delivery operation is append-only")


class TargetDisconnectDecision(models.Model):
    class State(models.TextChoices):
        ACCEPTED = "accepted", "접수"
        REVOKING = "revoking", "폐기 중"
        RECONCILING = "reconciling", "조정 중"
        COMPLETED = "completed", "완료"
        FAILED = "failed", "실패"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.ForeignKey(PublicationTarget, on_delete=models.PROTECT)
    expected_target_snapshot_id = models.UUIDField()
    expected_target_config_hash = models.CharField(max_length=64)
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    reauth_proof_id = models.UUIDField()
    reason = models.CharField(max_length=500)
    state = models.CharField(max_length=20, choices=State.choices, default=State.ACCEPTED)
    remote_result_hash = models.CharField(max_length=64, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["target", "request_key"], name="uq_target_disconnect_request")
        ]
