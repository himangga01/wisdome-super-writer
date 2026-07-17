from django.db import models
from django.utils import timezone

from wisdome_writer.domain.models import TimestampedUUIDModel, UUIDModel


class SecretReference(TimestampedUUIDModel):
    """Stores only a secret location; secret bytes never enter the database."""

    code = models.SlugField(max_length=120, unique=True)
    provider = models.CharField(max_length=32, default="env")
    locator = models.CharField(max_length=300)
    version = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "locator", "version"),
                name="unique_secret_reference_location",
            )
        ]

    def __str__(self) -> str:
        return self.code


class OutboxMessage(UUIDModel):
    message_key = models.CharField(max_length=200, unique=True)
    topic = models.CharField(max_length=160, db_index=True)
    aggregate_type = models.CharField(max_length=120)
    aggregate_id = models.UUIDField(db_index=True)
    payload = models.JSONField(default=dict)
    correlation_id = models.UUIDField(db_index=True)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_until = models.DateTimeField(null=True, blank=True, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=120, null=True, blank=True)

    class Meta:
        ordering = ("available_at", "created_at", "id")
        indexes = [
            models.Index(fields=("published_at", "available_at", "claimed_until")),
            models.Index(fields=("aggregate_type", "aggregate_id")),
        ]
