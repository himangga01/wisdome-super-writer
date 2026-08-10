import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models


sha256_validator = RegexValidator(
    r"^[a-f0-9]{64}$",
    "Expected a lowercase SHA-256 digest",
)


def allowed_authority_tiers_for_policy(policy) -> list[str]:
    """Return the frozen authority allow-list encoded by a topic policy."""
    material = policy.policy if hasattr(policy, "policy") else policy
    if not isinstance(material, dict):
        return []
    tiers = material.get("allowedAuthorityTiers")
    if isinstance(tiers, list) and all(
        isinstance(tier, str) and tier for tier in tiers
    ):
        return list(dict.fromkeys(tiers))
    required_tier = material.get("requiredAuthority")
    return [required_tier] if isinstance(required_tier, str) and required_tier else []


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


class RecoveryState(models.TextChoices):
    NOT_REQUIRED = "not_required", "Not required"
    IN_PROGRESS = "in_progress", "In progress"
    AUTOMATIC_RETRY = "automatic_retry", "Automatic retry"
    RECONCILING = "reconciling", "Reconciling"
    MANUAL_REQUIRED = "manual_required", "Manual required"
    STOPPED = "stopped", "Stopped"


class SourceCollectionAttemptState(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    RETRY_SCHEDULED = "retry_scheduled", "Retry scheduled"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class SourceCollectionFailureCategory(models.TextChoices):
    POLICY = "policy", "Policy"
    SCHEMA = "schema", "Schema"
    AUTHENTICATION = "authentication", "Authentication"
    TRANSIENT = "transient", "Transient"
    SECURITY = "security", "Security"
    INFRASTRUCTURE = "infrastructure", "Infrastructure"
    FRESHNESS = "freshness", "Freshness"
    AUTHORITY = "authority", "Authority"
    RIGHTS = "rights", "Rights"


class SourceItemStatus(models.TextChoices):
    ACTIVE = "active", "활성"
    CORRECTED = "corrected", "정정"
    RETRACTED = "retracted", "철회"
    UNAVAILABLE = "unavailable", "접근 불가"


class SourceDiscoveryKind(models.TextChoices):
    NEW_VERSION = "new_version", "새 버전"
    UNCHANGED = "unchanged", "변경 없음"
    CORRECTED = "corrected", "정정"
    RETRACTED = "retracted", "철회"
    UNAVAILABLE = "unavailable", "접근 불가"
    RESTORED = "restored", "복구"


class SourceItemQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation() -> None:
        raise TypeError("SourceItem is append-only")

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


class SourceItemManager(
    models.Manager.from_queryset(SourceItemQuerySet)
):
    pass


class RunSourceItemQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation() -> None:
        raise TypeError("RunSourceItem is append-only")

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


class RunSourceItemManager(
    models.Manager.from_queryset(RunSourceItemQuerySet)
):
    pass


class SourceCollectionObservationQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation() -> None:
        raise TypeError("SourceCollectionObservation is append-only")

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


class SourceCollectionObservationManager(
    models.Manager.from_queryset(SourceCollectionObservationQuerySet)
):
    pass


class CollectionRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    display_id = models.CharField(max_length=32, unique=True)
    topic_code = models.CharField(max_length=40, db_index=True)
    trigger = models.CharField(max_length=20, default="manual")
    approval_mode = models.CharField(max_length=20, default="manual")
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    source_registry = models.ForeignKey("topics.SourceRegistrySnapshot", on_delete=models.PROTECT)
    registry_manifest_hash = models.CharField(max_length=64)
    topic_policy = models.ForeignKey(
        "topics.TopicPolicy",
        on_delete=models.PROTECT,
        related_name="collection_runs",
    )
    policy_version = models.PositiveIntegerField()
    policy_hash = models.CharField(max_length=64, validators=[sha256_validator])
    freshness_minutes = models.PositiveIntegerField()
    allowed_authority_tiers = models.JSONField(default=list)
    freshness_cutoff = models.DateTimeField()
    requested_target_ids = models.JSONField(default=list)
    request_fingerprint = models.CharField(max_length=64, unique=True)
    state = models.CharField(max_length=32, choices=RunState.choices, default=RunState.QUEUED, db_index=True)
    counters = models.JSONField(default=dict)
    error_summary = models.JSONField(null=True, blank=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    terminal_impact = models.JSONField(default=dict)
    recovery_state = models.CharField(
        max_length=32,
        choices=RecoveryState.choices,
        default=RecoveryState.IN_PROGRESS,
        db_index=True,
    )
    next_recovery_at = models.DateTimeField(null=True, blank=True)
    stop_requested_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(policy_version__gt=0),
                name="ck_collection_run_policy_version_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(freshness_minutes__gt=0),
                name="ck_collection_run_freshness_minutes_positive",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if not self.topic_policy_id:
            errors["topic_policy"] = "A frozen topic policy is required."
        if not isinstance(self.allowed_authority_tiers, list) or not all(
            isinstance(tier, str) and tier
            for tier in self.allowed_authority_tiers
        ):
            errors["allowed_authority_tiers"] = (
                "Allowed authority tiers must be a list of non-empty strings."
            )
        if self.topic_policy_id:
            policy = self._state.fields_cache.get("topic_policy")
            if policy is None or policy.pk != self.topic_policy_id:
                policy = self.topic_policy
            if policy.code != self.topic_code:
                errors["topic_policy"] = "Topic policy must match the run topic."
            if self.policy_version != policy.version:
                errors["policy_version"] = "Must match the frozen topic policy."
            if self.policy_hash != policy.policy_hash:
                errors["policy_hash"] = "Must match the frozen topic policy."
            if self.freshness_minutes != policy.freshness_minutes:
                errors["freshness_minutes"] = "Must match the frozen topic policy."
        if errors:
            raise ValidationError(errors)


class RunControlDecisionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError("RunControlDecision is append-only")

    def delete(self):
        raise TypeError("RunControlDecision is append-only")

    async def aupdate(self, **kwargs):
        raise TypeError("RunControlDecision is append-only")

    async def adelete(self):
        raise TypeError("RunControlDecision is append-only")


class RunControlDecision(models.Model):
    class Action(models.TextChoices):
        STOP = "stop", "Stop"
        RETRY = "retry", "Retry"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        CollectionRun,
        on_delete=models.PROTECT,
        related_name="control_decisions",
    )
    action = models.CharField(max_length=24, choices=Action.choices)
    scope = models.JSONField(default=dict, blank=True)
    request_key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64, validators=[sha256_validator])
    reauth_proof_id = models.UUIDField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decided_at = models.DateTimeField(auto_now_add=True)

    objects = RunControlDecisionQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("run", "request_key"),
                name="uq_run_control_decision_request",
            ),
        ]
        ordering = ("decided_at", "id")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("RunControlDecision is append-only")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("RunControlDecision is append-only")


class SourceCollectionAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(CollectionRun, on_delete=models.CASCADE, related_name="collection_attempts")
    source_snapshot = models.ForeignKey("topics.SourceDefinitionSnapshot", on_delete=models.PROTECT)
    adapter_name = models.CharField(max_length=120)
    adapter_version = models.CharField(max_length=40, default="v1")
    adapter_implementation_manifest_hash = models.CharField(
        max_length=64, null=True, blank=True,
    )
    adapter_config_hash = models.CharField(max_length=64, null=True, blank=True)
    request_fingerprint = models.CharField(max_length=64, null=True, blank=True)
    request_window_start = models.DateTimeField(null=True, blank=True)
    request_window_end = models.DateTimeField(null=True, blank=True)
    state = models.CharField(
        max_length=20,
        choices=SourceCollectionAttemptState.choices,
        default=SourceCollectionAttemptState.QUEUED,
    )
    failure_category = models.CharField(
        max_length=32,
        choices=SourceCollectionFailureCategory.choices,
        blank=True,
        default="",
    )
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    retry_at = models.DateTimeField(null=True, blank=True)
    retry_after_seconds = models.PositiveIntegerField(null=True, blank=True)
    request_count = models.PositiveIntegerField(default=0)
    access_policy_hash = models.CharField(
        max_length=64, blank=True, default="", validators=[sha256_validator],
    )
    rights_policy_hash = models.CharField(
        max_length=64, blank=True, default="", validators=[sha256_validator],
    )
    authority_tier = models.CharField(max_length=32, blank=True, default="")
    freshness_cutoff = models.DateTimeField(null=True, blank=True)
    freshness_excluded_count = models.PositiveIntegerField(default=0)
    response_count = models.PositiveIntegerField(default=0)
    response_checksum = models.CharField(max_length=64, null=True, blank=True)
    error_code = models.CharField(max_length=100, null=True, blank=True)
    error_detail_redacted = models.CharField(max_length=500, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "source_snapshot"], name="uq_collection_attempt_source"),
            models.UniqueConstraint(
                fields=["request_fingerprint"],
                condition=models.Q(request_fingerprint__isnull=False),
                name="uq_collection_attempt_request_fingerprint",
            ),
            models.CheckConstraint(condition=models.Q(retry_count__gte=0), name="ck_collection_attempt_retry_count_nonnegative"),
            models.CheckConstraint(
                condition=(
                    models.Q(http_status__isnull=True)
                    | (
                        models.Q(http_status__gte=100)
                        & models.Q(http_status__lte=599)
                    )
                ),
                name="ck_collection_attempt_http_status_valid",
            ),
            models.CheckConstraint(
                condition=(models.Q(retry_after_seconds__isnull=True) | models.Q(retry_after_seconds__gte=0)),
                name="ck_collection_attempt_retry_after_nonnegative",
            ),
            models.CheckConstraint(condition=models.Q(request_count__gte=0), name="ck_collection_attempt_request_count_nonnegative"),
            models.CheckConstraint(
                condition=(models.Q(duration_ms__isnull=True) | models.Q(duration_ms__gte=0)),
                name="ck_collection_attempt_duration_nonnegative",
            ),
        ]
        indexes = [
            models.Index(
                fields=["run", "state", "retry_at"],
                name="collection__run_id_4ed4f3_idx",
            )
        ]


class SourceCollectionObservation(models.Model):
    """Append-only delivery-attempt history for a source collection attempt."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attempt = models.ForeignKey(
        SourceCollectionAttempt, on_delete=models.CASCADE, related_name="observations",
    )
    delivery_attempt_no = models.PositiveIntegerField()
    outcome = models.CharField(max_length=20, choices=SourceCollectionAttemptState.choices)
    failure_category = models.CharField(
        max_length=32, choices=SourceCollectionFailureCategory.choices,
        blank=True, default="",
    )
    error_code = models.CharField(max_length=100, null=True, blank=True)
    error_detail_redacted = models.CharField(max_length=500, null=True, blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    retry_at = models.DateTimeField(null=True, blank=True)
    retry_after_seconds = models.PositiveIntegerField(null=True, blank=True)
    request_count = models.PositiveIntegerField(default=0)
    freshness_excluded_count = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    access_policy_hash = models.CharField(
        max_length=64, blank=True, default="", validators=[sha256_validator],
    )
    authority_tier = models.CharField(max_length=32, blank=True, default="")
    freshness_cutoff = models.DateTimeField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(fields=["attempt", "delivery_attempt_no"], name="uq_collection_observation_delivery_attempt"),
            models.CheckConstraint(condition=models.Q(delivery_attempt_no__gt=0), name="ck_collection_observation_delivery_attempt_positive"),
            models.CheckConstraint(condition=models.Q(retry_count__gte=0), name="ck_collection_observation_retry_count_nonnegative"),
            models.CheckConstraint(
                condition=(
                    models.Q(http_status__isnull=True)
                    | (
                        models.Q(http_status__gte=100)
                        & models.Q(http_status__lte=599)
                    )
                ),
                name="ck_collection_observation_http_status_valid",
            ),
            models.CheckConstraint(
                condition=(models.Q(retry_after_seconds__isnull=True) | models.Q(retry_after_seconds__gte=0)),
                name="ck_collection_observation_retry_after_nonnegative",
            ),
            models.CheckConstraint(condition=models.Q(request_count__gte=0), name="ck_collection_observation_request_count_nonnegative"),
            models.CheckConstraint(
                condition=(models.Q(duration_ms__isnull=True) | models.Q(duration_ms__gte=0)),
                name="ck_collection_observation_duration_nonnegative",
            ),
        ]
        indexes = [
            models.Index(
                fields=["attempt", "recorded_at"],
                name="collection__attempt_2c7c03_idx",
            )
        ]

    objects = SourceCollectionObservationManager()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("SourceCollectionObservation is append-only")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("SourceCollectionObservation is append-only")


class SourceItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey("topics.SourceDefinition", on_delete=models.PROTECT)
    external_id = models.CharField(max_length=500)
    canonical_url = models.URLField(max_length=1000)
    title = models.CharField(max_length=1000)
    publisher = models.CharField(max_length=300)
    published_at = models.DateTimeField(null=True, blank=True)
    modified_at = models.DateTimeField(null=True, blank=True)
    first_collected_at = models.DateTimeField()
    content_hash = models.CharField(max_length=64)
    source_version_hash = models.CharField(max_length=64)
    source_version_schema = models.CharField(
        max_length=64,
        default="legacy-source-item-version-v0",
    )
    body_text = models.TextField(blank=True)
    metadata = models.JSONField(default=dict)
    attachments = models.JSONField(default=list)
    status = models.CharField(
        max_length=20,
        choices=SourceItemStatus.choices,
        default=SourceItemStatus.ACTIVE,
    )
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    objects = SourceItemManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id", "source_version_hash"], name="uq_source_item_version"
            ),
            models.CheckConstraint(
                condition=~models.Q(id=models.F("supersedes_id")),
                name="ck_source_item_not_self_superseding",
            ),
        ]
        indexes = [models.Index(fields=["source", "external_id"])]

    def clean(self):
        super().clean()
        if self.supersedes_id is None:
            return
        prior = self._state.fields_cache.get("supersedes")
        if prior is None or prior.pk != self.supersedes_id:
            prior = type(self).objects.only(
                "source_id",
                "external_id",
            ).get(pk=self.supersedes_id)
        if (
            prior.pk == self.pk
            or prior.source_id != self.source_id
            or prior.external_id != self.external_id
        ):
            raise ValidationError(
                "SourceItem supersedes must stay in the same source lineage."
            )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("SourceItem is append-only")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("SourceItem is append-only")


class RunSourceItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(CollectionRun, on_delete=models.CASCADE, related_name="run_source_items")
    collection_attempt = models.ForeignKey(SourceCollectionAttempt, on_delete=models.PROTECT)
    source_item = models.ForeignKey(SourceItem, on_delete=models.PROTECT, related_name="run_links")
    source_snapshot = models.ForeignKey("topics.SourceDefinitionSnapshot", on_delete=models.PROTECT)
    previous_run_source_item = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="subsequent_observations",
    )
    discovery_kind = models.CharField(
        max_length=24,
        choices=SourceDiscoveryKind.choices,
        default=SourceDiscoveryKind.NEW_VERSION,
    )
    discovered_at = models.DateTimeField(auto_now_add=True)
    objects = RunSourceItemManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "source_item"], name="uq_run_source_item"),
            models.CheckConstraint(
                condition=~models.Q(
                    id=models.F("previous_run_source_item_id")
                ),
                name="ck_run_source_item_not_self_previous",
            ),
        ]

    def clean(self):
        super().clean()
        if not all(
            (
                self.run_id,
                self.collection_attempt_id,
                self.source_item_id,
                self.source_snapshot_id,
            )
        ):
            return
        attempt = self._state.fields_cache.get("collection_attempt")
        if attempt is None or attempt.pk != self.collection_attempt_id:
            attempt = SourceCollectionAttempt.objects.only(
                "run_id",
                "source_snapshot_id",
            ).get(pk=self.collection_attempt_id)
        item = self._state.fields_cache.get("source_item")
        if item is None or item.pk != self.source_item_id:
            item = SourceItem.objects.only(
                "source_id",
                "external_id",
            ).get(pk=self.source_item_id)
        snapshot = self._state.fields_cache.get("source_snapshot")
        if snapshot is None or snapshot.pk != self.source_snapshot_id:
            from apps.topics.models import SourceDefinitionSnapshot

            snapshot = SourceDefinitionSnapshot.objects.only(
                "source_id"
            ).get(pk=self.source_snapshot_id)
        run = self._state.fields_cache.get("run")
        if run is None or run.pk != self.run_id:
            run = CollectionRun.objects.only(
                "source_registry_id"
            ).get(pk=self.run_id)
        if (
            attempt.run_id != self.run_id
            or attempt.source_snapshot_id != self.source_snapshot_id
            or snapshot.source_id != item.source_id
        ):
            raise ValidationError(
                "RunSourceItem provenance does not match its run and source."
            )
        from apps.topics.models import SourceRegistryMembership

        if not SourceRegistryMembership.objects.filter(
            registry_id=run.source_registry_id,
            source_definition_id=item.source_id,
            source_snapshot_id=self.source_snapshot_id,
            enabled=True,
        ).exists():
            raise ValidationError(
                "RunSourceItem source snapshot is not enabled in the "
                "run registry."
            )
        if self.previous_run_source_item_id is not None:
            previous = self._state.fields_cache.get(
                "previous_run_source_item"
            )
            if (
                previous is None
                or previous.pk != self.previous_run_source_item_id
            ):
                previous = type(self).objects.select_related(
                    "source_item",
                    "collection_attempt",
                ).get(pk=self.previous_run_source_item_id)
            if (
                previous.pk == self.pk
                or previous.run_id == self.run_id
                or previous.collection_attempt.state != "succeeded"
                or previous.source_item.source_id != item.source_id
                or previous.source_item.external_id != item.external_id
            ):
                raise ValidationError(
                    "previous_run_source_item must be the prior observation "
                    "in the same source lineage."
                )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("RunSourceItem is append-only")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("RunSourceItem is append-only")


class RunStep(models.Model):
    run = models.ForeignKey(CollectionRun, on_delete=models.CASCADE, related_name="steps")
    name = models.CharField(max_length=64)
    attempt_no = models.PositiveIntegerField(default=1)
    correlation_id = models.UUIDField(editable=False, db_index=True)
    worker_task_id = models.CharField(max_length=255, null=True, blank=True)
    state = models.CharField(max_length=20, default="queued")
    input_count = models.PositiveIntegerField(default=0)
    output_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=100, null=True, blank=True)
    error_detail_redacted = models.CharField(max_length=500, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    fanout_completed_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    retry_at = models.DateTimeField(null=True, blank=True)
    terminal_impact = models.JSONField(default=dict)
    recovery_state = models.CharField(
        max_length=32,
        choices=RecoveryState.choices,
        default=RecoveryState.IN_PROGRESS,
        db_index=True,
    )
    source_event_id = models.UUIDField(null=True, blank=True, db_index=True)
    lease_generation = models.PositiveBigIntegerField(default=0)
    lease_owner = models.CharField(max_length=160, blank=True, default="")
    lease_token = models.UUIDField(null=True, blank=True)
    delivery_count = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "name", "attempt_no"], name="uq_run_step_attempt"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="running",
                        source_event_id__isnull=False,
                        lease_generation__gt=0,
                        delivery_count=models.F("lease_generation"),
                        lease_token__isnull=False,
                    )
                    & ~models.Q(lease_owner="")
                    | ~models.Q(state="running")
                    & models.Q(lease_owner="", lease_token__isnull=True)
                ),
                name="ck_run_step_lease_complete",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.run_id:
            run = self._state.fields_cache.get("run")
            if run is None or run.pk != self.run_id:
                database = kwargs.get("using") or self._state.db
                run = CollectionRun.objects.using(database).only(
                    "correlation_id"
                ).get(
                    pk=self.run_id
                )
            if self.correlation_id != run.correlation_id:
                self.correlation_id = run.correlation_id
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = tuple(
                        {*update_fields, "correlation_id"}
                    )
        return super().save(*args, **kwargs)
