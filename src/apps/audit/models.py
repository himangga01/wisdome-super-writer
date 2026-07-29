import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.db import models

from wisdome_writer.domain.models import UUIDModel

from .redaction import (
    POLICY_VERSION,
    redaction_policy_hash,
    sanitize_metadata,
    sanitize_reason,
)


_AUDIT_INSERT_ALLOWED: ContextVar[bool] = ContextVar(
    "audit_event_insert_allowed", default=False
)


@contextmanager
def _allow_audit_event_insert():
    token = _AUDIT_INSERT_ALLOWED.set(True)
    try:
        yield
    finally:
        _AUDIT_INSERT_ALLOWED.reset(token)


class AuditEventQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation() -> None:
        raise TypeError("AuditEvent is append-only")

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
        raise TypeError("AuditEvent bulk insertion is forbidden; use record_audit_event()")

    async def abulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        raise TypeError("AuditEvent bulk insertion is forbidden; use record_audit_event()")


class AuditEventManager(models.Manager.from_queryset(AuditEventQuerySet)):
    def record(self, **kwargs):
        raise TypeError("Use apps.audit.services.record_audit_event()")


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
    reason_code = models.CharField(max_length=500, null=True, blank=True)
    metadata_schema_version = models.CharField(max_length=32)
    redaction_policy_version = models.CharField(max_length=64)
    redaction_policy_hash = models.CharField(max_length=64)
    metadata_redacted = models.JSONField(default=dict)

    objects = AuditEventManager()

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        ordering = ("-occurred_at", "-id")
        indexes = [
            models.Index(fields=("-occurred_at", "-id")),
            models.Index(fields=("correlation_id", "-occurred_at")),
            models.Index(fields=("entity_type", "entity_id", "-occurred_at")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(actor_type="admin", actor__isnull=False)
                    | models.Q(
                        actor_type__in=("system", "worker"), actor__isnull=True
                    )
                ),
                name="audit_actor_shape_required",
            )
        ]

    def _validate_immutable_insert(self) -> None:
        try:
            self.correlation_id = uuid.UUID(str(self.correlation_id))
            self.entity_id = uuid.UUID(str(self.entity_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("AuditEvent correlation and entity IDs must be UUIDs") from exc

        if self.actor_type not in self.ActorType.values:
            raise ValueError("AuditEvent actor_type is invalid")
        if self.actor_type == self.ActorType.ADMIN:
            if self.actor_id is None:
                raise ValueError("Admin AuditEvent requires an actor")
        elif self.actor_id is not None:
            raise ValueError("System and worker AuditEvent rows cannot reference an admin")

        self.reason_code = sanitize_reason(self.reason_code)
        for field_name in ("before_hash", "after_hash"):
            value = getattr(self, field_name)
            if value is not None and (
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"AuditEvent {field_name} must be a lowercase SHA-256 digest")

        if self.redaction_policy_version != POLICY_VERSION:
            raise ValueError("New AuditEvent rows must use the current redaction policy")
        expected_policy_hash = redaction_policy_hash(
            self.action,
            metadata_schema_version=self.metadata_schema_version,
        )
        if self.redaction_policy_hash != expected_policy_hash:
            raise ValueError("AuditEvent redaction policy hash does not match its policy")
        self.metadata_redacted = sanitize_metadata(
            self.action,
            self.metadata_redacted,
            metadata_schema_version=self.metadata_schema_version,
        )

    def save_base(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("AuditEvent is append-only")
        if not _AUDIT_INSERT_ALLOWED.get():
            raise TypeError("Use apps.audit.services.record_audit_event()")
        self._validate_immutable_insert()
        return super().save_base(*args, **kwargs)

    def save(self, *args, **kwargs):
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
