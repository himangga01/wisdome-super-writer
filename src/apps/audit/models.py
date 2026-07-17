import uuid

from django.conf import settings
from django.db import models

from wisdome_writer.domain.models import UUIDModel

from .redaction import POLICY_VERSION, redaction_policy_hash, sanitize_metadata


class AuditEventManager(models.Manager):
    def record(
        self,
        *,
        correlation_id: uuid.UUID | str,
        actor_type: str,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
        before_hash: str | None = None,
        after_hash: str | None = None,
        reason_code: str | None = None,
        metadata: dict | None = None,
        metadata_schema_version: str = "1",
    ):
        redacted = sanitize_metadata(action, metadata)
        return self.create(
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_hash=before_hash,
            after_hash=after_hash,
            reason_code=reason_code,
            metadata_schema_version=metadata_schema_version,
            redaction_policy_version=POLICY_VERSION,
            redaction_policy_hash=redaction_policy_hash(action),
            metadata_redacted=redacted,
        )


class AuditEvent(UUIDModel):
    class ActorType(models.TextChoices):
        ADMIN = "admin", "Admin"
        SYSTEM = "system", "System"
        WORKER = "worker", "Worker"

    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    correlation_id = models.UUIDField(db_index=True)
    actor_type = models.CharField(max_length=16, choices=ActorType.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    action = models.CharField(max_length=120, db_index=True)
    entity_type = models.CharField(max_length=120, db_index=True)
    entity_id = models.UUIDField(db_index=True)
    before_hash = models.CharField(max_length=64, null=True, blank=True)
    after_hash = models.CharField(max_length=64, null=True, blank=True)
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    metadata_schema_version = models.CharField(max_length=32)
    redaction_policy_version = models.CharField(max_length=64)
    redaction_policy_hash = models.CharField(max_length=64)
    metadata_redacted = models.JSONField(default=dict)

    objects = AuditEventManager()

    class Meta:
        ordering = ("-occurred_at", "-id")
        indexes = [
            models.Index(fields=("-occurred_at", "-id")),
            models.Index(fields=("correlation_id", "-occurred_at")),
            models.Index(fields=("entity_type", "entity_id", "-occurred_at")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(actor_type="admin", actor__isnull=False)
                | ~models.Q(actor_type="admin"),
                name="audit_admin_actor_required",
            )
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("AuditEvent is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("AuditEvent is append-only")


class RetentionHold(UUIDModel):
    scope_type = models.CharField(max_length=100)
    scope_id = models.UUIDField(null=True, blank=True)
    reason = models.CharField(max_length=500)
    active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=("scope_type", "scope_id", "active"))]


class RetentionBatch(UUIDModel):
    class State(models.TextChoices):
        PREVIEW = "preview", "Preview"
        APPROVED = "approved", "Approved"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    policy_version = models.PositiveIntegerField()
    policy_hash = models.CharField(max_length=64)
    cutoff_at = models.DateTimeField()
    state = models.CharField(max_length=20, choices=State.choices, default=State.PREVIEW)
    row_version = models.PositiveIntegerField(default=1)
    request_key = models.CharField(max_length=200, unique=True)
    preview_manifest_hash = models.CharField(max_length=64)
    counters = models.JSONField(default=dict)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    approved_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_detail_redacted = models.CharField(max_length=500, null=True, blank=True)


class RetentionBatchItem(UUIDModel):
    class State(models.TextChoices):
        CANDIDATE = "candidate", "Candidate"
        HELD = "held", "Held"
        PURGED = "purged", "Purged"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    batch = models.ForeignKey(RetentionBatch, on_delete=models.CASCADE, related_name="items")
    entity_type = models.CharField(max_length=100)
    entity_id = models.UUIDField()
    object_key = models.CharField(max_length=1024, null=True, blank=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.CANDIDATE)
    reason_code = models.CharField(max_length=100, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("batch", "entity_type", "entity_id"), name="uq_retention_batch_item")
        ]
