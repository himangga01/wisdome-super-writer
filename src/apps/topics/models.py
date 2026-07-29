import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q


sha256_validator = RegexValidator(
    r"^[a-f0-9]{64}$",
    "Expected a lowercase SHA-256 digest",
)


def default_rate_limit_policy() -> dict[str, int]:
    return {
        "maxConcurrency": 1,
        "requestsPerMinute": 10,
        "burst": 1,
    }


class TopicCode(models.TextChoices):
    HOUSING = "housing_subscription", "대한민국 부동산 청약 정보"
    SEMICONDUCTOR = "semiconductor_news", "한국 및 글로벌 반도체 뉴스"


class TopicPolicy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, choices=TopicCode.choices)
    version = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=160)
    freshness_minutes = models.PositiveIntegerField(default=1440)
    policy = models.JSONField(default=dict)
    policy_hash = models.CharField(max_length=64, validators=[sha256_validator])
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["code", "version"],
                name="uq_topic_policy_version",
            )
        ]
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

    class RightsStatus(models.TextChoices):
        ALLOWED = "allowed", "허용"
        ATTRIBUTION_REQUIRED = "attribution_required", "출처 표시 필요"
        INTERNAL_ONLY = "internal_analysis_only", "내부 분석 전용"
        UNKNOWN = "unknown", "불명"
        PROHIBITED = "prohibited", "금지"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic_code = models.CharField(
        max_length=40,
        choices=TopicCode.choices,
        db_index=True,
    )
    # Repository manifests retain a stable human-readable internal key. It is
    # intentionally not exposed by the public admin API contract.
    key = models.SlugField(max_length=100)
    display_name = models.CharField(max_length=200)
    publisher = models.CharField(max_length=200)
    owner_name = models.CharField(max_length=200)
    editorial_control_name = models.CharField(max_length=200)
    base_url = models.URLField(max_length=500)
    authority_tier = models.CharField(
        max_length=32,
        choices=AuthorityTier.choices,
    )
    access_method = models.CharField(
        max_length=32,
        choices=AccessMethod.choices,
    )
    independence_group = models.CharField(max_length=200)
    adapter_key = models.CharField(max_length=160)
    external_config = models.JSONField(default=dict)
    secret_ref = models.CharField(max_length=300, null=True, blank=True)
    allowed_mime_types = models.JSONField(default=list)
    default_rights_status = models.CharField(
        max_length=32,
        choices=RightsStatus.choices,
        default=RightsStatus.UNKNOWN,
    )
    terms_url = models.URLField(max_length=500, null=True, blank=True)
    robots_url = models.URLField(max_length=500, null=True, blank=True)
    license_url = models.URLField(max_length=500, null=True, blank=True)
    poll_interval_seconds = models.PositiveBigIntegerField(default=3600)
    rate_limit_policy = models.JSONField(default=default_rate_limit_policy)
    enabled = models.BooleanField(default=True)
    # Kept for compatibility with the existing collector projection.
    current_snapshot_version = models.PositiveIntegerField(default=1)
    latest_approved_snapshot_version = models.PositiveIntegerField(
        null=True,
        blank=True,
    )
    latest_draft_snapshot = models.ForeignKey(
        "SourceDefinitionSnapshot",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    latest_draft_snapshot_version = models.PositiveIntegerField(
        null=True,
        blank=True,
    )
    latest_draft_config_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        validators=[sha256_validator],
    )
    last_health = models.JSONField(null=True, blank=True)
    creation_request_key = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        unique=True,
    )
    creation_request_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        validators=[sha256_validator],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["topic_code", "key"],
                name="uq_source_topic_key",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        latest_draft_snapshot__isnull=True,
                        latest_draft_snapshot_version__isnull=True,
                        latest_draft_config_hash__isnull=True,
                    )
                    | Q(
                        latest_draft_snapshot__isnull=False,
                        latest_draft_snapshot_version__isnull=False,
                        latest_draft_config_hash__isnull=False,
                    )
                ),
                name="ck_source_latest_draft_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        creation_request_key__isnull=True,
                        creation_request_hash__isnull=True,
                    )
                    | Q(
                        creation_request_key__isnull=False,
                        creation_request_hash__isnull=False,
                    )
                ),
                name="ck_source_creation_request_complete",
            ),
        ]
        ordering = ["topic_code", "display_name"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            alias = kwargs.get("using") or self._state.db or "default"
            original_topic = (
                type(self).objects.using(alias)
                .only("topic_code")
                .get(pk=self.pk)
                .topic_code
            )
            if self.topic_code != original_topic:
                raise ValidationError({"topic_code": "Source topic is immutable."})
        return super().save(*args, **kwargs)


class SourceDefinitionSnapshot(models.Model):
    class State(models.TextChoices):
        DRAFT = "draft", "초안"
        APPROVED = "approved", "승인"
        RETIRED = "retired", "폐기"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(
        SourceDefinition,
        on_delete=models.PROTECT,
        related_name="snapshots",
    )
    topic_code = models.CharField(
        max_length=40,
        choices=TopicCode.choices,
        db_index=True,
    )
    version = models.PositiveIntegerField()
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.DRAFT,
    )
    # Full immutable execution material. Adapters must not read mutable source
    # projection fields when executing a snapshot.
    config = models.JSONField(default=dict)
    config_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    independence_group = models.CharField(max_length=200)
    owner_name = models.CharField(max_length=200)
    editorial_control_name = models.CharField(max_length=200)
    request_key = models.CharField(max_length=200, null=True, blank=True)
    request_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        validators=[sha256_validator],
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "version"],
                name="uq_source_snapshot_version",
            ),
            models.UniqueConstraint(
                fields=["source", "request_key"],
                condition=Q(request_key__isnull=False),
                name="uq_source_snapshot_request",
            ),
            models.CheckConstraint(
                condition=(
                    Q(request_key__isnull=True, request_hash__isnull=True)
                    | Q(request_key__isnull=False, request_hash__isnull=False)
                ),
                name="ck_source_snapshot_request_complete",
            ),
        ]
        ordering = ["source_id", "version"]

    def clean(self):
        super().clean()
        if self.source_id and self.topic_code != self.source.topic_code:
            raise ValidationError(
                {"topic_code": "Snapshot and source topics must match."}
            )
        if bool(self.request_key) != bool(self.request_hash):
            raise ValidationError(
                "request_key and request_hash must be set together."
            )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            alias = kwargs.get("using") or self._state.db or "default"
            original = type(self).objects.using(alias).get(pk=self.pk)
            immutable_fields = (
                "source_id",
                "topic_code",
                "version",
                "config",
                "config_hash",
                "independence_group",
                "owner_name",
                "editorial_control_name",
                "request_key",
                "request_hash",
            )
            if any(
                getattr(self, field) != getattr(original, field)
                for field in immutable_fields
            ):
                raise ValidationError(
                    "Source definition snapshot material is immutable."
                )
            transition = (original.state, self.state)
            if transition not in {
                (self.State.DRAFT, self.State.DRAFT),
                (self.State.DRAFT, self.State.APPROVED),
                (self.State.DRAFT, self.State.RETIRED),
                (self.State.APPROVED, self.State.APPROVED),
                (self.State.APPROVED, self.State.RETIRED),
                (self.State.RETIRED, self.State.RETIRED),
            }:
                raise ValidationError(
                    "Source definition snapshot state cannot move backward."
                )
            lifecycle_fields = (
                "approved_by_id",
                "approved_at",
                "retired_at",
            )
            if original.state == self.State.RETIRED and any(
                getattr(self, field) != getattr(original, field)
                for field in ("state", *lifecycle_fields)
            ):
                raise ValidationError(
                    "A retired source definition snapshot is terminal."
                )
            if original.state == self.state and any(
                getattr(self, field) != getattr(original, field)
                for field in lifecycle_fields
            ):
                raise ValidationError(
                    "Snapshot lifecycle metadata is immutable without a state transition."
                )
            if (
                transition == (self.State.DRAFT, self.State.APPROVED)
                and (
                    self.approved_by_id is None
                    or self.approved_at is None
                    or self.retired_at is not None
                )
            ):
                raise ValidationError(
                    "Approving a source snapshot requires approval provenance."
                )
            if (
                transition == (self.State.DRAFT, self.State.RETIRED)
                and (
                    self.approved_by_id != original.approved_by_id
                    or self.approved_at != original.approved_at
                )
            ):
                raise ValidationError(
                    "Retiring a draft cannot add approval provenance."
                )
            if (
                self.state == self.State.RETIRED
                and self.retired_at is None
            ):
                raise ValidationError(
                    "Retiring a source snapshot requires retired_at."
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("SourceDefinitionSnapshot is immutable and cannot be deleted")


class SourceRegistrySnapshot(models.Model):
    class State(models.TextChoices):
        DRAFT = "draft", "초안"
        APPROVED = "approved", "승인"
        RETIRED = "retired", "폐기"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic_code = models.CharField(
        max_length=40,
        choices=TopicCode.choices,
        db_index=True,
    )
    version = models.PositiveIntegerField()
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.DRAFT,
    )
    manifest_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    row_version = models.PositiveBigIntegerField(default=1)
    base_approved_registry = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="derived_drafts",
    )
    base_approved_version = models.PositiveIntegerField(null=True, blank=True)
    base_approved_manifest_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        validators=[sha256_validator],
    )
    draft_request_key = models.CharField(
        max_length=200,
        null=True,
        blank=True,
    )
    draft_request_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        validators=[sha256_validator],
    )
    latest_decision = models.ForeignKey(
        "SourceRegistryDecision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["topic_code", "version"],
                name="uq_registry_topic_version",
            ),
            models.UniqueConstraint(
                fields=["topic_code", "draft_request_key"],
                condition=Q(draft_request_key__isnull=False),
                name="uq_registry_draft_request",
            ),
            models.UniqueConstraint(
                fields=["topic_code"],
                condition=Q(state="approved"),
                name="uq_registry_approved_topic",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        base_approved_registry__isnull=True,
                        base_approved_version__isnull=True,
                        base_approved_manifest_hash__isnull=True,
                    )
                    | Q(
                        base_approved_registry__isnull=False,
                        base_approved_version__isnull=False,
                        base_approved_manifest_hash__isnull=False,
                    )
                ),
                name="ck_registry_base_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        draft_request_key__isnull=True,
                        draft_request_hash__isnull=True,
                    )
                    | Q(
                        draft_request_key__isnull=False,
                        draft_request_hash__isnull=False,
                    )
                ),
                name="ck_registry_draft_request_complete",
            ),
        ]
        ordering = ["topic_code", "-version"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            alias = kwargs.get("using") or self._state.db or "default"
            original = type(self).objects.using(alias).get(pk=self.pk)
            immutable_fields = (
                "topic_code",
                "version",
                "base_approved_registry_id",
                "base_approved_version",
                "base_approved_manifest_hash",
                "draft_request_key",
                "draft_request_hash",
                "created_at",
            )
            if any(
                getattr(self, field) != getattr(original, field)
                for field in immutable_fields
            ):
                raise ValidationError(
                    "Source registry snapshot identity and base material are immutable."
                )
            transition = (original.state, self.state)
            if transition not in {
                (self.State.DRAFT, self.State.DRAFT),
                (self.State.DRAFT, self.State.APPROVED),
                (self.State.APPROVED, self.State.APPROVED),
                (self.State.APPROVED, self.State.RETIRED),
                (self.State.RETIRED, self.State.RETIRED),
            }:
                raise ValidationError(
                    "Source registry snapshot state cannot move backward."
                )

            lifecycle_fields = (
                "latest_decision_id",
                "approved_by_id",
                "approved_at",
                "retired_at",
            )
            if original.state == self.State.RETIRED and any(
                getattr(self, field) != getattr(original, field)
                for field in (
                    "state",
                    "manifest_hash",
                    "row_version",
                    *lifecycle_fields,
                )
            ):
                raise ValidationError(
                    "A retired source registry snapshot is terminal."
                )
            if transition == (self.State.DRAFT, self.State.DRAFT):
                manifest_changed = (
                    self.manifest_hash != original.manifest_hash
                )
                row_changed = self.row_version != original.row_version
                if manifest_changed != row_changed or (
                    row_changed
                    and self.row_version != original.row_version + 1
                ):
                    raise ValidationError(
                        "Draft manifest and row version must advance together."
                    )
                if any(
                    getattr(self, field) != getattr(original, field)
                    for field in lifecycle_fields
                ):
                    raise ValidationError(
                        "Draft registry lifecycle metadata is immutable."
                    )
            elif transition == (
                self.State.DRAFT,
                self.State.APPROVED,
            ):
                if (
                    self.manifest_hash != original.manifest_hash
                    or self.row_version != original.row_version + 1
                    or self.latest_decision_id is None
                    or self.approved_by_id is None
                    or self.approved_at is None
                    or self.retired_at is not None
                ):
                    raise ValidationError(
                        "Registry approval requires an exact manifest and approval provenance."
                    )
            elif transition == (
                self.State.APPROVED,
                self.State.RETIRED,
            ):
                if (
                    self.manifest_hash != original.manifest_hash
                    or self.row_version != original.row_version + 1
                    or self.approved_by_id != original.approved_by_id
                    or self.approved_at != original.approved_at
                    or self.retired_at is None
                ):
                    raise ValidationError(
                        "Registry retirement must preserve approved material."
                    )
            elif original.state == self.state and any(
                getattr(self, field) != getattr(original, field)
                for field in (
                    "manifest_hash",
                    "row_version",
                    *lifecycle_fields,
                )
            ):
                raise ValidationError(
                    "Approved registry material is immutable."
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("SourceRegistrySnapshot is immutable and cannot be deleted")


class TopicRegistryHead(models.Model):
    topic_code = models.CharField(
        primary_key=True,
        max_length=40,
        choices=TopicCode.choices,
    )
    current_approved_registry = models.ForeignKey(
        SourceRegistrySnapshot,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    current_approved_version = models.PositiveIntegerField(
        null=True,
        blank=True,
    )
    current_approved_manifest_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        validators=[sha256_validator],
    )
    row_version = models.PositiveBigIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        current_approved_registry__isnull=True,
                        current_approved_version__isnull=True,
                        current_approved_manifest_hash__isnull=True,
                    )
                    | Q(
                        current_approved_registry__isnull=False,
                        current_approved_version__isnull=False,
                        current_approved_manifest_hash__isnull=False,
                    )
                ),
                name="ck_topic_registry_head_complete",
            )
        ]

    def clean(self):
        super().clean()
        values = (
            self.current_approved_registry_id,
            self.current_approved_version,
            self.current_approved_manifest_hash,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValidationError(
                "Current registry ID, version and manifest hash must be set together."
            )
        if (
            self.current_approved_registry_id
            and self.current_approved_registry.topic_code != self.topic_code
        ):
            raise ValidationError(
                "Topic head and current registry topics must match."
            )


class SourceRegistryMembership(models.Model):
    registry = models.ForeignKey(
        SourceRegistrySnapshot,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    source_definition = models.ForeignKey(
        SourceDefinition,
        on_delete=models.PROTECT,
        related_name="registry_memberships",
    )
    source_snapshot = models.ForeignKey(
        SourceDefinitionSnapshot,
        on_delete=models.PROTECT,
    )
    enabled = models.BooleanField(default=True)
    display_order = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["registry", "source_definition"],
                name="uq_registry_source_definition",
            ),
        ]
        ordering = ["display_order", "source_definition_id"]

    def clean(self):
        super().clean()
        if self.source_definition_id and self.source_snapshot_id:
            if self.source_snapshot.source_id != self.source_definition_id:
                raise ValidationError(
                    "Membership source and snapshot source must match."
                )
        if self.registry_id and self.source_definition_id:
            if self.registry.topic_code != self.source_definition.topic_code:
                raise ValidationError(
                    "Registry and source topics must match."
                )

    def save(self, *args, **kwargs):
        alias = kwargs.get("using") or self._state.db or "default"
        registry_state = (
            SourceRegistrySnapshot.objects.using(alias)
            .only("state")
            .get(pk=self.registry_id)
            .state
        )
        if registry_state != SourceRegistrySnapshot.State.DRAFT:
            raise ValidationError(
                "Only draft registry memberships can be changed."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        alias = kwargs.get("using") or self._state.db or "default"
        registry_state = (
            SourceRegistrySnapshot.objects.using(alias)
            .only("state")
            .get(pk=self.registry_id)
            .state
        )
        if registry_state != SourceRegistrySnapshot.State.DRAFT:
            raise ValidationError(
                "Only draft registry memberships can be deleted."
            )
        return super().delete(*args, **kwargs)


class SourceRegistryMutation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    registry = models.ForeignKey(
        SourceRegistrySnapshot,
        on_delete=models.PROTECT,
        related_name="mutations",
    )
    source_definition = models.ForeignKey(
        SourceDefinition,
        on_delete=models.PROTECT,
        related_name="+",
    )
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    before_manifest_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    after_manifest_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    resulting_row_version = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["registry", "request_key"],
                name="uq_registry_mutation_request",
            )
        ]
        ordering = ["registry_id", "created_at", "id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("SourceRegistryMutation is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("SourceRegistryMutation is append-only")


class SourceRegistryDecision(models.Model):
    class Decision(models.TextChoices):
        APPROVED = "approved", "승인"
        RETIRED = "retired", "폐기"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    registry = models.ForeignKey(
        SourceRegistrySnapshot,
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    version = models.PositiveIntegerField()
    decision = models.CharField(max_length=16, choices=Decision.choices)
    expected_row_version = models.PositiveBigIntegerField()
    expected_manifest_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    expected_current_head_registry = models.ForeignKey(
        SourceRegistrySnapshot,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    expected_current_head_version = models.PositiveIntegerField(
        null=True,
        blank=True,
    )
    expected_current_head_manifest_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        validators=[sha256_validator],
    )
    supersedes_decision = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by",
    )
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    decision_hash = models.CharField(
        max_length=64,
        validators=[sha256_validator],
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
    )
    decided_at = models.DateTimeField()
    reason = models.CharField(max_length=500)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["registry", "version"],
                name="uq_registry_decision_version",
            ),
            models.UniqueConstraint(
                fields=["registry", "request_key"],
                name="uq_registry_decision_request",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        expected_current_head_registry__isnull=True,
                        expected_current_head_version__isnull=True,
                        expected_current_head_manifest_hash__isnull=True,
                    )
                    | Q(
                        expected_current_head_registry__isnull=False,
                        expected_current_head_version__isnull=False,
                        expected_current_head_manifest_hash__isnull=False,
                    )
                ),
                name="ck_registry_decision_head_complete",
            ),
        ]
        ordering = ["registry_id", "version"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("SourceRegistryDecision is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("SourceRegistryDecision is append-only")
