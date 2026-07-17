import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class TopicCode(models.TextChoices):
    HOUSING = "housing_subscription", "대한민국 부동산 청약정보"
    SEMICONDUCTOR = "semiconductor_news", "한국·글로벌 반도체 뉴스"


class TopicPolicy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, choices=TopicCode.choices)
    version = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=160)
    freshness_minutes = models.PositiveIntegerField(default=1440)
    policy = models.JSONField(default=dict)
    policy_hash = models.CharField(max_length=64)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["code", "version"], name="uq_topic_policy_version")]
        ordering = ["code", "-version"]


class SourceDefinition(models.Model):
    class AuthorityTier(models.TextChoices):
        PRIMARY_OFFICIAL = "primary_official", "공식 1차 출처"
        PRIMARY_CORPORATE = "primary_corporate", "기업 1차 출처"
        TRUSTED_SECONDARY = "trusted_secondary", "신뢰 보조 출처"
        DISCOVERY_ONLY = "discovery_only", "발견 전용"

    class AccessMethod(models.TextChoices):
        PUBLIC_API = "public_api", "공개 API"
        OPEN_DATA_API = "open_data_api", "공공데이터 API"
        RSS_ATOM = "rss_atom", "RSS/Atom"
        PUBLIC_HTML = "public_html", "공개 HTML"
        PUBLIC_FILE = "public_file", "공개 파일"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic_code = models.CharField(max_length=40, choices=TopicCode.choices, db_index=True)
    key = models.SlugField(max_length=100)
    display_name = models.CharField(max_length=200)
    owner_name = models.CharField(max_length=200)
    base_url = models.URLField(max_length=500)
    authority_tier = models.CharField(max_length=32, choices=AuthorityTier.choices)
    access_method = models.CharField(max_length=32, choices=AccessMethod.choices)
    independence_group = models.CharField(max_length=120)
    enabled = models.BooleanField(default=True)
    current_snapshot_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["topic_code", "key"], name="uq_source_topic_key")]
        ordering = ["topic_code", "display_name"]


class SourceDefinitionSnapshot(models.Model):
    class State(models.TextChoices):
        DRAFT = "draft", "초안"
        APPROVED = "approved", "승인"
        RETIRED = "retired", "폐기"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(SourceDefinition, on_delete=models.PROTECT, related_name="snapshots")
    version = models.PositiveIntegerField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.DRAFT)
    config = models.JSONField(default=dict)
    config_hash = models.CharField(max_length=64)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "version"], name="uq_source_snapshot_version"),
            models.UniqueConstraint(fields=["source", "config_hash"], name="uq_source_snapshot_material"),
        ]


class SourceRegistrySnapshot(models.Model):
    class State(models.TextChoices):
        DRAFT = "draft", "초안"
        APPROVED = "approved", "승인"
        RETIRED = "retired", "폐기"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic_code = models.CharField(max_length=40, choices=TopicCode.choices)
    version = models.PositiveIntegerField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.DRAFT)
    manifest_hash = models.CharField(max_length=64)
    row_version = models.PositiveIntegerField(default=1)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["topic_code", "version"], name="uq_registry_topic_version")]
        ordering = ["topic_code", "-version"]


class SourceRegistryMembership(models.Model):
    registry = models.ForeignKey(SourceRegistrySnapshot, on_delete=models.CASCADE, related_name="memberships")
    source_snapshot = models.ForeignKey(SourceDefinitionSnapshot, on_delete=models.PROTECT)
    enabled = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["registry", "source_snapshot"], name="uq_registry_source_snapshot")
        ]
        ordering = ["display_order", "source_snapshot_id"]

    def clean(self):
        if self.registry_id and self.source_snapshot_id:
            if self.registry.topic_code != self.source_snapshot.source.topic_code:
                raise ValidationError("Registry and source topics must match.")
