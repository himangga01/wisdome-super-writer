from collections.abc import Iterable
from datetime import timedelta
from math import ceil
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.module_loading import import_string

from wisdome_writer.domain.errors import (
    AuthenticationFailed,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    RateLimited,
)

from .models import ReauthenticationProof

ALLOWED_ACTION_SCOPES = frozenset(
    {
        "auto_publish_change",
        "kill_switch_disable",
        "unpublish",
        "bulk_retry",
        "retention_execute",
        "registry_decision",
        "profile_decision",
        "validation_decision",
        "credential_disconnect",
        "approval_revoke",
    }
)


def _session_binding_hash(*, admin_id: UUID, session_key: str) -> str:
    return salted_hmac(
        "wisdome-writer.reauthentication-proof.v1",
        f"{admin_id}:{session_key}",
        secret=settings.SECRET_KEY,
        algorithm="sha256",
    ).hexdigest()


def _ensure_session_key(request) -> str:
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def _current_session_key(request) -> str:
    session_key = request.session.session_key
    if not session_key:
        raise Forbidden("Reauthentication proof requires the session in which it was issued")
    return session_key


def _validate_mfa(admin, mfa_code: str | None) -> bool:
    if not settings.REAUTH_MFA_REQUIRED:
        return True
    validator_path = getattr(settings, "REAUTH_MFA_VALIDATOR", "")
    if not validator_path:
        raise ImproperlyConfigured("REAUTH_MFA_VALIDATOR is required when MFA is enabled")
    return bool(import_string(validator_path)(admin, mfa_code))


def issue_reauthentication_proof(
    *, request, current_password: str, action_scopes: Iterable[str], mfa_code: str | None = None
) -> ReauthenticationProof:
    now = timezone.now()
    proof: ReauthenticationProof | None = None
    authentication_failed = False
    retry_after_seconds: int | None = None

    with transaction.atomic():
        admin = get_user_model().objects.select_for_update().get(pk=request.user.pk)
        if admin.reauth_locked_until and admin.reauth_locked_until > now:
            retry_after_seconds = ceil(
                (admin.reauth_locked_until - now).total_seconds()
            )
        else:
            password_valid = admin.check_password(current_password)
            mfa_valid = _validate_mfa(admin, mfa_code)
            credentials_valid = (
                admin.is_active
                and admin.is_staff
                and password_valid
                and mfa_valid
            )
            if not credentials_valid:
                window_started_at = admin.reauth_failure_window_started_at
                window_elapsed = (
                    window_started_at is None
                    or now - window_started_at
                    >= timedelta(seconds=settings.REAUTH_FAILURE_WINDOW_SECONDS)
                )
                if window_elapsed:
                    window_started_at = now
                    failure_count = 1
                else:
                    failure_count = admin.reauth_failure_count + 1

                admin.reauth_failure_count = failure_count
                admin.reauth_failure_window_started_at = window_started_at
                admin.reauth_locked_until = None
                if failure_count >= settings.REAUTH_FAILURE_LIMIT:
                    admin.reauth_locked_until = now + timedelta(
                        seconds=settings.REAUTH_LOCK_SECONDS
                    )
                    retry_after_seconds = settings.REAUTH_LOCK_SECONDS
                admin.save(
                    update_fields=(
                        "reauth_failure_count",
                        "reauth_failure_window_started_at",
                        "reauth_locked_until",
                        "updated_at",
                    )
                )
                authentication_failed = True
            else:
                scopes = sorted(set(action_scopes))
                if not scopes or any(
                    scope not in ALLOWED_ACTION_SCOPES for scope in scopes
                ):
                    raise InvalidInput(
                        "At least one supported action scope is required"
                    )

                session_binding_hash = _session_binding_hash(
                    admin_id=admin.pk,
                    session_key=_ensure_session_key(request),
                )
                ReauthenticationProof.objects.filter(
                    admin=admin,
                    session_binding_hash=session_binding_hash,
                    state=ReauthenticationProof.State.ACTIVE,
                ).update(state=ReauthenticationProof.State.REVOKED)

                admin.last_reauthenticated_at = now
                admin.reauth_failure_count = 0
                admin.reauth_failure_window_started_at = None
                admin.reauth_locked_until = None
                admin.save(
                    update_fields=(
                        "last_reauthenticated_at",
                        "reauth_failure_count",
                        "reauth_failure_window_started_at",
                        "reauth_locked_until",
                        "updated_at",
                    )
                )
                proof = ReauthenticationProof.objects.create(
                    admin=admin,
                    session_binding_hash=session_binding_hash,
                    action_scopes=scopes,
                    issued_at=now,
                    expires_at=now
                    + timedelta(seconds=settings.REAUTH_PROOF_TTL_SECONDS),
                )

    if retry_after_seconds is not None:
        raise RateLimited(retry_after_seconds=retry_after_seconds)
    if authentication_failed:
        raise AuthenticationFailed()
    if proof is None:
        raise AuthenticationFailed()
    return proof


@transaction.atomic
def consume_reauthentication_proof(
    *, request, proof_id: UUID | str, action_scope: str, entity_type: str, entity_id: UUID
) -> ReauthenticationProof:
    if action_scope not in ALLOWED_ACTION_SCOPES:
        raise InvalidInput("Unsupported reauthentication action scope")
    normalized_entity_type = str(entity_type).strip()
    if not normalized_entity_type or len(normalized_entity_type) > 100:
        raise InvalidInput("A valid reauthentication entity type is required")
    try:
        normalized_entity_id = UUID(str(entity_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidInput("A valid reauthentication entity ID is required") from exc
    if (
        not request.user.is_authenticated
        or not request.user.is_active
        or not request.user.is_staff
    ):
        raise Forbidden("An active administrator session is required")

    try:
        proof = ReauthenticationProof.objects.select_for_update().get(pk=proof_id)
    except (ReauthenticationProof.DoesNotExist, ValidationError, ValueError) as exc:
        raise NotFound("Reauthentication proof was not found") from exc

    expected_binding = _session_binding_hash(
        admin_id=request.user.pk,
        session_key=_current_session_key(request),
    )
    now = timezone.now()
    if proof.expires_at <= now:
        raise Conflict("Reauthentication proof has expired")
    if proof.state != ReauthenticationProof.State.ACTIVE:
        raise Conflict("Reauthentication proof is no longer active")
    if proof.admin_id != request.user.pk or not constant_time_compare(
        proof.session_binding_hash, expected_binding
    ):
        raise Forbidden("Reauthentication proof is bound to a different session")
    if action_scope not in proof.action_scopes:
        raise Forbidden("Reauthentication proof does not include this action scope")

    proof.state = ReauthenticationProof.State.CONSUMED
    proof.consumed_at = now
    proof.consumed_entity_type = normalized_entity_type
    proof.consumed_entity_id = normalized_entity_id
    proof.consumed_action = action_scope
    proof.save(
        update_fields=(
            "state",
            "consumed_at",
            "consumed_entity_type",
            "consumed_entity_id",
            "consumed_action",
        )
    )
    return proof

