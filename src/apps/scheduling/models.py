import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


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


class ScheduleDispatch(models.Model):
    class State(models.TextChoices):
        SKIPPED = "skipped", "건너뜀"
        QUEUED = "queued", "대기"
        DISPATCHED = "dispatched", "실행"
        COALESCED = "coalesced", "병합"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, related_name="dispatches")
    schedule_version = models.PositiveIntegerField()
    scheduled_for = models.DateTimeField()
    tick_key = models.CharField(max_length=200, unique=True)
    state = models.CharField(max_length=20, choices=State.choices)
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    coalesced_into = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    collection_run = models.ForeignKey("collection.CollectionRun", null=True, blank=True, on_delete=models.PROTECT)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["schedule", "scheduled_for"], name="uq_schedule_tick"),
            models.UniqueConstraint(
                fields=["schedule"], condition=Q(state="queued"), name="uq_schedule_single_queued"
            ),
        ]


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
