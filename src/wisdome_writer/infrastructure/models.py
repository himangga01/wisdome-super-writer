from django.db import models
from django.db.models import Q
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
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DISPATCHING = "dispatching", "Dispatching"
        PUBLISHED = "published", "Published"
        DEAD_LETTER = "dead_letter", "Dead letter"

    message_key = models.CharField(max_length=200, unique=True)
    topic = models.CharField(max_length=160, db_index=True)
    event_version = models.PositiveSmallIntegerField(default=1)
    occurred_at = models.DateTimeField(default=timezone.now)
    aggregate_type = models.CharField(max_length=120)
    aggregate_id = models.UUIDField(db_index=True)
    payload = models.JSONField(default=dict)
    correlation_id = models.UUIDField(db_index=True)
    causation_id = models.UUIDField(null=True, blank=True, db_index=True)
    job_id = models.UUIDField(db_index=True)
    operation = models.CharField(max_length=80, default="process")
    policy_versions = models.JSONField(default=dict)
    immutable_material_hash = models.CharField(max_length=64)
    not_before = models.DateTimeField(default=timezone.now)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    published_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_until = models.DateTimeField(null=True, blank=True, db_index=True)
    lease_owner = models.CharField(max_length=160, blank=True, default="")
    lease_token = models.UUIDField(null=True, blank=True)
    lease_generation = models.PositiveBigIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    last_error_code = models.CharField(max_length=120, null=True, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    dead_lettered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("available_at", "created_at", "id")
        indexes = [
            models.Index(fields=("status", "available_at", "claimed_until")),
            models.Index(fields=("aggregate_type", "aggregate_id")),
        ]


class OutboxConsumerReceiptQuerySet(models.QuerySet):
    def delete(self):
        raise TypeError("OutboxConsumerReceipt is append-only")

    async def adelete(self):
        raise TypeError("OutboxConsumerReceipt is append-only")

    def _raw_delete(self, using):
        raise TypeError("OutboxConsumerReceipt is append-only")


class OutboxConsumerReceipt(UUIDModel):
    class State(models.TextChoices):
        PROCESSING = "processing", "Processing"
        RETRY = "retry", "Retry"
        SUCCEEDED = "succeeded", "Succeeded"
        DEAD_LETTER = "dead_letter", "Dead letter"

    event = models.ForeignKey(
        OutboxMessage, on_delete=models.PROTECT, related_name="consumer_receipts"
    )
    consumer_name = models.CharField(max_length=160)
    state = models.CharField(
        max_length=20, choices=State.choices, default=State.PROCESSING, db_index=True
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=120, blank=True, default="")
    next_retry_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    dead_lettered_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_until = models.DateTimeField(null=True, blank=True, db_index=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_generation = models.PositiveBigIntegerField(default=0)
    terminal_reserved_at = models.DateTimeField(null=True, blank=True)
    terminal_lease_generation = models.PositiveBigIntegerField(default=0)
    terminal_lease_token = models.UUIDField(null=True, blank=True)
    terminal_lease_token_hash = models.CharField(max_length=64, blank=True, default="")
    terminal_error_code = models.CharField(max_length=120, blank=True, default="")
    objects = models.Manager.from_queryset(OutboxConsumerReceiptQuerySet)()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("event", "consumer_name"),
                name="unique_outbox_event_consumer",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        terminal_reserved_at__isnull=True,
                        terminal_lease_generation=0,
                        terminal_lease_token__isnull=True,
                        terminal_lease_token_hash="",
                        terminal_error_code="",
                    )
                    | Q(
                        terminal_reserved_at__isnull=False,
                        terminal_lease_generation__gte=1,
                        terminal_lease_token__isnull=False,
                    )
                    & ~Q(terminal_lease_token_hash="")
                    & ~Q(terminal_error_code="")
                ),
                name="ck_outbox_receipt_terminal_reservation_complete",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(state="succeeded")
                    | Q(terminal_reserved_at__isnull=True)
                ),
                name="ck_outbox_succeeded_receipt_not_terminal_reserved",
            ),
        ]

    def delete(self, *args, **kwargs):
        raise TypeError("OutboxConsumerReceipt is append-only")
