from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from wisdome_writer.domain.models import TimestampedUUIDModel, UUIDModel

from .managers import AdminAccountManager


class AdminAccount(AbstractBaseUser, PermissionsMixin, TimestampedUUIDModel):
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=True)
    last_reauthenticated_at = models.DateTimeField(null=True, blank=True)

    objects = AdminAccountManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        ordering = ("email",)

    def __str__(self) -> str:
        return self.email


class ReauthenticationProof(UUIDModel):
    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        CONSUMED = "consumed", "Consumed"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"

    admin = models.ForeignKey(
        "accounts.AdminAccount",
        on_delete=models.PROTECT,
        related_name="reauthentication_proofs",
    )
    session_binding_hash = models.CharField(max_length=64)
    action_scopes = models.JSONField(default=list)
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    consumed_entity_type = models.CharField(max_length=100, null=True, blank=True)
    consumed_entity_id = models.UUIDField(null=True, blank=True)
    consumed_action = models.CharField(max_length=100, null=True, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.ACTIVE)

    class Meta:
        indexes = [
            models.Index(fields=("admin", "state", "expires_at")),
            models.Index(fields=("session_binding_hash", "state")),
        ]

