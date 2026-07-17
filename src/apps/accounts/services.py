from collections.abc import Iterable
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.module_loading import import_string

from wisdome_writer.domain.errors import Conflict, Forbidden, InvalidInput, NotFound

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


def _validate_mfa(admin, mfa_code: str | None) -> None:
    if not settings.REAUTH_MFA_REQUIRED:
        return
    validator_path = getattr(settings, "REAUTH_MFA_VALIDATOR", "")
    if not validator_path:
        raise ImproperlyConfigured("REAUTH_MFA_VALIDATOR is required when MFA is enabled")
    if not import_string(validator_path)(admin, mfa_code):
        raise Forbidden("MFA verification failed")


@transaction.atomic
def issue_reauthentication_proof(
    *, request, current_password: str, action_scopes: Iterable[str], mfa_code: str | None = None
) -> ReauthenticationProof:
    admin = get_user_model().objects.select_for_update().get(pk=request.user.pk)
    if not admin.is_active or not admin.is_staff or not admin.check_password(current_password):
        raise Forbidden("Current credentials could not be verified")
    _validate_mfa(admin, mfa_code)

    scopes = sorted(set(action_scopes))
    if not scopes or any(scope not in ALLOWED_ACTION_SCOPES for scope in scopes):
        raise InvalidInput("At least one supported action scope is required")

    now = timezone.now()
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
    admin.save(update_fields=("last_reauthenticated_at", "updated_at"))
    return ReauthenticationProof.objects.create(
        admin=admin,
        session_binding_hash=session_binding_hash,
        action_scopes=scopes,
        issued_at=now,
        expires_at=now + timedelta(seconds=settings.REAUTH_PROOF_TTL_SECONDS),
    )


@transaction.atomic
def consume_reauthentication_proof(
    *, request, proof_id: UUID | str, action_scope: str, entity_type: str, entity_id: UUID
) -> ReauthenticationProof:
    try:
        proof = ReauthenticationProof.objects.select_for_update().get(pk=proof_id)
    except ReauthenticationProof.DoesNotExist as exc:
        raise NotFound("Reauthentication proof was not found") from exc

    expected_binding = _session_binding_hash(
        admin_id=request.user.pk,
        session_key=_ensure_session_key(request),
    )
    now = timezone.now()
    if proof.expires_at <= now and proof.state == ReauthenticationProof.State.ACTIVE:
        proof.state = ReauthenticationProof.State.EXPIRED
        proof.save(update_fields=("state",))
    if proof.state != ReauthenticationProof.State.ACTIVE:
        raise Conflict("Reauthentication proof is no longer active")
    if proof.admin_id != request.user.pk or proof.session_binding_hash != expected_binding:
        raise Forbidden("Reauthentication proof is bound to a different session")
    if action_scope not in proof.action_scopes:
        raise Forbidden("Reauthentication proof does not include this action scope")

    proof.state = ReauthenticationProof.State.CONSUMED
    proof.consumed_at = now
    proof.consumed_entity_type = entity_type
    proof.consumed_entity_id = entity_id
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

