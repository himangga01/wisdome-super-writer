import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)


SCHEDULE_DISPATCH_MATERIAL_VERSION = "schedule-dispatch-material-v1"
SCHEDULE_EXECUTION_MATERIAL_VERSION = "schedule-execution-material-v1"
LEGACY_UNVERIFIABLE_SCHEDULE_MATERIAL_VERSION = "legacy-unverifiable-v1"

sha256_validator = RegexValidator(
    r"^[0-9a-f]{64}$",
    "Expected a lowercase SHA-256 digest",
)


class Schedule(models.Model):
    class OverlapPolicy(models.TextChoices):
        SKIP = "skip", "건너뛰기"
        QUEUE_ONE = "queue_one", "한 건 대기"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=200)
    topic_code = models.CharField(max_length=40)
    cron_expression = models.CharField(max_length=100)
    timezone = models.CharField(max_length=100, default="Asia/Seoul")
    window_minutes = models.PositiveIntegerField(default=1440)
    target_ids = models.JSONField(default=list)
    approval_mode = models.CharField(max_length=20, default="manual")
    auto_publish_validation_refs = models.JSONField(default=list)
    auto_publish_activation_refs = models.JSONField(default=list)
    overlap_policy = models.CharField(max_length=20, choices=OverlapPolicy.choices, default=OverlapPolicy.SKIP)
    enabled = models.BooleanField(default=False)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_dispatched_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ScheduleDispatchQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError("ScheduleDispatch changes require the locked scheduling service")

    def delete(self):
        raise TypeError("ScheduleDispatch is append-only")


class ScheduleDispatch(models.Model):
    class State(models.TextChoices):
        SKIPPED = "skipped", "건너뜀"
        QUEUED = "queued", "대기"
        DISPATCHED = "dispatched", "실행"
        COALESCED = "coalesced", "병합"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schedule = models.ForeignKey(Schedule, on_delete=models.PROTECT, related_name="dispatches")
    schedule_version = models.PositiveIntegerField()
    scheduled_for = models.DateTimeField()
    tick_key = models.CharField(max_length=200, unique=True)
    material_version = models.CharField(
        max_length=40,
        default=LEGACY_UNVERIFIABLE_SCHEDULE_MATERIAL_VERSION,
    )
    schedule_material = models.JSONField(default=dict)
    schedule_material_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        validators=[sha256_validator],
    )
    source_registry = models.ForeignKey(
        "topics.SourceRegistrySnapshot",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="schedule_dispatches",
    )
    registry_manifest_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        validators=[sha256_validator],
    )
    topic_policy = models.ForeignKey(
        "topics.TopicPolicy",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="schedule_dispatches",
    )
    topic_policy_version = models.PositiveIntegerField(null=True, blank=True)
    topic_policy_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        validators=[sha256_validator],
    )
    target_snapshot_refs = models.JSONField(default=list)
    approval_mode_snapshot = models.CharField(max_length=20, default="manual")
    validation_refs = models.JSONField(default=list)
    activation_refs = models.JSONField(default=list)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="schedule_dispatches",
    )
    state = models.CharField(max_length=20, choices=State.choices)
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    coalesced_into = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    coalesced_tick_refs = models.JSONField(default=list)
    coalesced_tick_manifest_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        validators=[sha256_validator],
    )
    tick_set_version = models.PositiveIntegerField(default=1)
    execution_material = models.JSONField(null=True, blank=True)
    execution_material_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        validators=[sha256_validator],
    )
    run_request_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        validators=[sha256_validator],
    )
    collection_run = models.ForeignKey("collection.CollectionRun", null=True, blank=True, on_delete=models.PROTECT)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    dispatched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ScheduleDispatchQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["schedule", "scheduled_for"], name="uq_schedule_tick"),
            models.UniqueConstraint(
                fields=["schedule"], condition=Q(state="queued"), name="uq_schedule_single_queued"
            ),
            models.CheckConstraint(
                condition=Q(tick_set_version__gt=0),
                name="ck_schedule_dispatch_tick_set_version_positive",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(material_version=SCHEDULE_DISPATCH_MATERIAL_VERSION)
                    | (
                        Q(source_registry__isnull=False)
                        & Q(topic_policy__isnull=False)
                        & Q(topic_policy_version__isnull=False)
                        & Q(requested_by__isnull=False)
                        & ~Q(schedule_material_hash="")
                        & ~Q(registry_manifest_hash="")
                        & ~Q(topic_policy_hash="")
                        & ~Q(coalesced_tick_manifest_hash="")
                    )
                ),
                name="ck_schedule_dispatch_current_material_complete",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(
                        material_version=SCHEDULE_DISPATCH_MATERIAL_VERSION,
                        state="dispatched",
                    )
                    | (
                        Q(execution_material__isnull=False)
                        & Q(collection_run__isnull=False)
                        & Q(dispatched_at__isnull=False)
                        & ~Q(execution_material_hash="")
                        & ~Q(run_request_fingerprint="")
                    )
                ),
                name="ck_schedule_dispatch_execution_complete",
            ),
        ]

    @staticmethod
    def _material_hash(value) -> str:
        return canonical_hash(
            value,
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        )

    def _validate_current_material(self) -> None:
        if self.material_version != SCHEDULE_DISPATCH_MATERIAL_VERSION:
            return
        material = self.schedule_material
        if (
            not isinstance(material, dict)
            or material.get("schemaVersion")
            != SCHEDULE_DISPATCH_MATERIAL_VERSION
            or self.schedule_material_hash != self._material_hash(material)
            or str(material.get("scheduleDispatchId")) != str(self.id)
            or str(material.get("scheduleId")) != str(self.schedule_id)
            or int(material.get("scheduleVersion") or 0) != self.schedule_version
            or self.source_registry_id is None
            or self.topic_policy_id is None
            or self.topic_policy_version is None
            or self.requested_by_id is None
            or str(material.get("requestedById")) != str(self.requested_by_id)
            or material.get("approvalMode") != self.approval_mode_snapshot
            or material.get("targetSnapshots") != self.target_snapshot_refs
            or material.get("validationRefs") != self.validation_refs
            or material.get("activationRefs") != self.activation_refs
            or str((material.get("registry") or {}).get("snapshotId"))
            != str(self.source_registry_id)
            or (material.get("registry") or {}).get("manifestHash")
            != self.registry_manifest_hash
            or str((material.get("topicPolicy") or {}).get("id"))
            != str(self.topic_policy_id)
            or int((material.get("topicPolicy") or {}).get("version") or 0)
            != self.topic_policy_version
            or (material.get("topicPolicy") or {}).get("policyHash")
            != self.topic_policy_hash
            or not isinstance(self.coalesced_tick_refs, list)
            or not self.coalesced_tick_refs
            or self.coalesced_tick_manifest_hash
            != self._material_hash(self.coalesced_tick_refs)
        ):
            raise ValidationError("ScheduleDispatch frozen material is inconsistent")
        if self.state == self.State.DISPATCHED:
            if (
                not isinstance(self.execution_material, dict)
                or self.execution_material.get("schemaVersion")
                != SCHEDULE_EXECUTION_MATERIAL_VERSION
                or self.execution_material_hash
                != self._material_hash(self.execution_material)
                or self.collection_run_id is None
                or self.dispatched_at is None
                or not self.run_request_fingerprint
            ):
                raise ValidationError("ScheduleDispatch execution material is incomplete")
        elif any(
            (
                self.execution_material is not None,
                bool(self.execution_material_hash),
                bool(self.run_request_fingerprint),
                self.dispatched_at is not None,
                self.collection_run_id is not None,
            )
        ):
            raise ValidationError("Only a dispatched tick can bind execution material")

    def save(self, *args, **kwargs):
        self._validate_current_material()
        if not self._state.adding:
            alias = kwargs.get("using") or self._state.db or "default"
            original = type(self).objects.using(alias).get(pk=self.pk)
            frozen_fields = (
                "schedule_id",
                "schedule_version",
                "scheduled_for",
                "tick_key",
                "material_version",
                "schedule_material",
                "schedule_material_hash",
                "source_registry_id",
                "registry_manifest_hash",
                "topic_policy_id",
                "topic_policy_version",
                "topic_policy_hash",
                "target_snapshot_refs",
                "approval_mode_snapshot",
                "validation_refs",
                "activation_refs",
                "requested_by_id",
                "created_at",
            )
            if any(
                getattr(self, field) != getattr(original, field)
                for field in frozen_fields
            ):
                raise ValidationError("ScheduleDispatch tick material is immutable")
            transition = (original.state, self.state)
            if transition not in {
                (self.State.QUEUED, self.State.QUEUED),
                (self.State.QUEUED, self.State.DISPATCHED),
                (self.State.SKIPPED, self.State.SKIPPED),
                (self.State.COALESCED, self.State.COALESCED),
                (self.State.DISPATCHED, self.State.DISPATCHED),
            }:
                raise ValidationError("ScheduleDispatch state transition is invalid")
            if original.state in {
                self.State.SKIPPED,
                self.State.COALESCED,
                self.State.DISPATCHED,
            }:
                mutable = (
                    "state",
                    "reason_code",
                    "coalesced_into_id",
                    "coalesced_tick_refs",
                    "coalesced_tick_manifest_hash",
                    "tick_set_version",
                    "execution_material",
                    "execution_material_hash",
                    "run_request_fingerprint",
                    "collection_run_id",
                    "window_start",
                    "window_end",
                    "dispatched_at",
                )
                if any(
                    getattr(self, field) != getattr(original, field)
                    for field in mutable
                ):
                    raise ValidationError("A terminal ScheduleDispatch is immutable")
            if transition == (self.State.QUEUED, self.State.QUEUED):
                tick_material_changed = any(
                    (
                        self.coalesced_tick_refs != original.coalesced_tick_refs,
                        self.coalesced_tick_manifest_hash
                        != original.coalesced_tick_manifest_hash,
                        self.window_start != original.window_start,
                        self.window_end != original.window_end,
                    )
                )
                expected_tick_version = original.tick_set_version + (
                    1 if tick_material_changed else 0
                )
                if self.tick_set_version != expected_tick_version:
                    raise ValidationError(
                        "ScheduleDispatch tick-set changes require one version advance"
                    )
                if self.window_start > original.window_start or self.window_end < original.window_end:
                    raise ValidationError("A queue-one window can only expand")
            if transition == (self.State.QUEUED, self.State.DISPATCHED) and (
                self.tick_set_version != original.tick_set_version
                or self.coalesced_tick_refs != original.coalesced_tick_refs
                or self.coalesced_tick_manifest_hash
                != original.coalesced_tick_manifest_hash
                or self.window_start != original.window_start
                or self.window_end != original.window_end
            ):
                raise ValidationError("Dispatch must freeze the locked queue-one tick set")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("ScheduleDispatch is append-only")


class OperationalControl(models.Model):
    key = models.CharField(max_length=100, primary_key=True, default="global_kill_switch")
    enabled = models.BooleanField(default=True, help_text="True blocks all new external writes and dispatches.")
    version = models.PositiveIntegerField(default=1)
    reason = models.CharField(max_length=500, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)


class KillSwitchDecision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    expected_version = models.PositiveIntegerField()
    enabled = models.BooleanField()
    request_key = models.CharField(max_length=200, unique=True)
    reason = models.CharField(max_length=500)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decided_at = models.DateTimeField(auto_now_add=True)
