from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


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

    class Meta:
        ordering = ["target_id", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["target", "version"], name="uq_target_snapshot_version"),
            models.UniqueConstraint(fields=["target", "config_hash"], name="uq_target_snapshot_material"),
        ]


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
    supersedes_intent_id = models.UUIDField(null=True, blank=True)
    intent_hash = models.CharField(max_length=64, unique=True)
    request_key = models.CharField(max_length=200)
    state = models.CharField(max_length=24, choices=State.choices, default=State.AWAITING_APPROVAL)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["article_revision", "request_key"], name="uq_intent_revision_request")
        ]


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

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["publication_intent", "target_snapshot", "render_stage"],
                name="uq_intent_target_render_stage",
            )
        ]


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
    supersedes_approval_id = models.UUIDField(null=True, blank=True)
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64, null=True, blank=True)
    reauth_proof_id = models.UUIDField(null=True, blank=True)
    policy_snapshot_hash = models.CharField(max_length=64)
    quality_report_hash = models.CharField(max_length=64)
    render_template_hash = models.CharField(max_length=64, null=True, blank=True)
    source_manifest_hash = models.CharField(max_length=64)
    admin = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-decided_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["publication_intent", "target", "request_key"],
                name="uq_approval_intent_target_request",
            )
        ]


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

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["article_id", "target"], name="uq_article_publication_target"),
            models.UniqueConstraint(fields=["target", "remote_lookup_key"], name="uq_target_remote_lookup"),
        ]


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
    remote_request_id = models.CharField(max_length=255, blank=True)
    http_status = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_detail_redacted = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class RemoteMedia(models.Model):
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
    evidence_asset_id = models.UUIDField(null=True, blank=True)
    asset_checksum = models.CharField(max_length=64)
    presentation_hash = models.CharField(max_length=64)
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


class PublicDeliveryAsset(models.Model):
    class State(models.TextChoices):
        AVAILABLE = "available", "사용 가능"
        WITHDRAWAL_PENDING = "withdrawal_pending", "철회 대기"
        PENDING_DELETE = "pending_delete", "삭제 대기"
        DELETED = "deleted", "삭제"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_evidence_asset_id = models.UUIDField(null=True, blank=True)
    asset_checksum = models.CharField(max_length=64)
    presentation_hash = models.CharField(max_length=64)
    mime_type = models.CharField(max_length=255)
    byte_size = models.PositiveBigIntegerField()
    delivery_object_key = models.CharField(max_length=1000)
    delivery_object_version = models.CharField(max_length=255)
    public_url = models.URLField(max_length=1000, unique=True)
    rights_status_snapshot = models.CharField(max_length=32)
    alt_text_snapshot = models.CharField(max_length=1000)
    caption_snapshot = models.TextField(blank=True)
    attribution_snapshot = models.TextField(blank=True)
    state = models.CharField(max_length=24, choices=State.choices, default=State.AVAILABLE)
    active_reference_count = models.PositiveIntegerField(default=0)
    lease_generation = models.PositiveIntegerField(default=1)
    last_remote_body_hash = models.CharField(max_length=64, blank=True)
    last_reconciled_at = models.DateTimeField(null=True, blank=True)
    zero_reference_at = models.DateTimeField(null=True, blank=True)
    delete_after = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    delete_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["asset_checksum", "presentation_hash"], name="uq_delivery_asset_presentation"
            )
        ]


class PublicationMedia(models.Model):
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
    evidence_asset_id = models.UUIDField(null=True, blank=True)
    published_evidence_snapshot_id = models.UUIDField(null=True, blank=True)
    published_visualization_snapshot_id = models.UUIDField(null=True, blank=True)
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
                    Q(published_evidence_snapshot_id__isnull=False, published_visualization_snapshot_id__isnull=True)
                    | Q(published_evidence_snapshot_id__isnull=True, published_visualization_snapshot_id__isnull=False)
                ),
                name="ck_publication_media_provenance_xor",
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
