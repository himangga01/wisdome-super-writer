from __future__ import annotations

import html
import inspect
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone as dt_timezone
from typing import Any, Iterable

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.db.models import Max
from django.utils.module_loading import import_string
from django.utils import timezone

from adapters.publishers.blogger import BloggerOAuthClient, BloggerPublisher
from adapters.publishers.wordpress import WordPressPublisher
from apps.accounts.services import consume_reauthentication_proof
from apps.audit.models import AuditEvent
from apps.audit.redaction import validate_stored_metadata
from apps.audit.services import (
    AuditContext,
    audit_event_id,
    record_audit_event,
    require_audit_replay,
    require_worker_event,
)
from wisdome_writer.domain.errors import Conflict, Forbidden, InvalidInput, NotFound
from wisdome_writer.domain.hashing import sha256_hex

from .contracts import PublishCommand, PublisherError, RenderedArticle, RenderedMedia
from .models import (
    Approval,
    ApprovalMode,
    ArticleChannelRender,
    AutoPublishActivation,
    AutoPublishValidation,
    AutoPublishValidationDecision,
    ChannelCode,
    ChannelRole,
    Publication,
    PublicationAction,
    PublicationAttempt,
    PublicationReconcileGeneration,
    PublicationIntent,
    PublicationMedia,
    PublicationTarget,
    PublicationTargetSnapshot,
    PublicDeliveryAsset,
    RemoteMedia,
    TargetCanaryRun,
    TargetDisconnectDecision,
    TargetEnvironment,
    ValidationState,
)


PUBLISHER_CONTRACT_VERSION = "publisher-v1"
ADAPTER_MANIFESTS = {
    ChannelCode.WORDPRESS: sha256_hex(
        {
            "channel": "wordpress",
            "implementation": "adapters.publishers.wordpress.client.WordPressPublisher",
            "contract": PUBLISHER_CONTRACT_VERSION,
            "api": "wp-json/wp/v2",
        }
    ),
    ChannelCode.BLOGGER: sha256_hex(
        {
            "channel": "blogger",
            "implementation": "adapters.publishers.blogger.client.BloggerPublisher",
            "contract": PUBLISHER_CONTRACT_VERSION,
            "api": "blogger-v3",
        }
    ),
}
PUBLISHING_AUDIT_MATERIAL_VERSION = "publishing-state-v1"
_TARGET_CREATE_NAMESPACE = uuid.UUID("e28b5933-26ae-4e6d-83b5-1cbe64f26ba0")


def _require_audit_actor(audit_context: AuditContext, expected: str) -> None:
    if audit_context.actor_type != expected:
        raise Forbidden(f"{expected} audit provenance is required")


def _worker_audit_replay(
    audit_context: AuditContext,
    *,
    entity,
    candidates: Iterable[
        tuple[str, str, dict[str, Any] | None]
    ],
) -> AuditEvent | None:
    if audit_context.actor_type != "worker" or audit_context.event_key is None:
        return None
    for action, identity_key, metadata_expected in candidates:
        expected_id = audit_event_id(
            action=action,
            entity=entity,
            identity_key=identity_key,
        )
        if not AuditEvent.objects.using(
            audit_context.database_alias
        ).filter(id=expected_id).exists():
            continue
        return require_audit_replay(
            context=audit_context,
            action=action,
            entity=entity,
            identity_key=identity_key,
            metadata_expected=metadata_expected,
        )
    return None


def _require_worker_event(
    audit_context: AuditContext,
    *,
    topic: str,
    aggregate_id,
    payload_identity: dict[str, str],
):
    try:
        return require_worker_event(
            context=audit_context,
            topic=topic,
            aggregate_id=aggregate_id,
            payload_identity=payload_identity,
        )
    except ValueError as exc:
        raise Conflict(str(exc)) from exc


def _audit_state(entity, **extra: Any) -> dict[str, Any]:
    material: dict[str, Any] = {
        "entityType": entity._meta.label_lower,
        "entityId": str(entity.pk),
    }
    for field_name in (
        "state",
        "status",
        "decision",
        "version",
        "attempt_no",
        "reconcile_attempt_no",
        "current_snapshot_version",
        "current_config_hash",
        "connection_state",
        "preflight_state",
        "canary_state",
        "pilot_state",
        "auto_publish_enabled",
        "decision_version",
        "intent_hash",
        "approval_subject_hash",
        "activation_hash",
        "material_hash",
        "result_identity",
        "result_state",
        "remote_state",
        "last_error_code",
        "error_code",
    ):
        if hasattr(entity, field_name):
            value = getattr(entity, field_name)
            if isinstance(value, uuid.UUID):
                value = str(value)
            material[field_name] = value
    if isinstance(entity, PublicationTarget):
        material.update(
            {
                "display_name_hash": sha256_hex(entity.display_name),
                "username_ref_identity_hash": (
                    sha256_hex(entity.username_ref)
                    if entity.username_ref
                    else None
                ),
                "credential_ref_identity_hash": (
                    sha256_hex(entity.credential_ref)
                    if entity.credential_ref
                    else None
                ),
            }
        )
    material.update(extra)
    return material


def _state_transition_manifests(
    rows: Iterable[dict[str, Any]],
    *,
    state_field: str,
    next_state: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    before_rows = sorted(
        (
            {
                "id": str(row["id"]),
                "state": row[state_field],
            }
            for row in rows
        ),
        key=lambda row: row["id"],
    )
    after_rows = [
        {"id": row["id"], "state": next_state}
        for row in before_rows
    ]
    return (
        {
            "count": len(before_rows),
            "manifestHash": sha256_hex(before_rows),
        },
        {
            "count": len(after_rows),
            "manifestHash": sha256_hex(after_rows),
        },
    )


def _publication_media_state_manifest(publication_id) -> dict[str, Any]:
    rows = [
        {
            "id": str(row["id"]),
            "bindingState": row["binding_state"],
            "remoteMediaId": _id(row["remote_media_id"]),
            "publicDeliveryAssetId": _id(
                row["public_delivery_asset_id"]
            ),
        }
        for row in PublicationMedia.objects.filter(
            publication_id=publication_id
        )
        .order_by("id")
        .values(
            "id",
            "binding_state",
            "remote_media_id",
            "public_delivery_asset_id",
        )
    ]
    return {
        "count": len(rows),
        "manifestHash": sha256_hex(rows),
    }


def _record_publishing_audit(
    *,
    audit_context: AuditContext,
    action: str,
    entity,
    identity_key: str,
    before_material: dict[str, Any] | None,
    after_material: dict[str, Any] | None,
    metadata: dict[str, Any] | None = None,
):
    return record_audit_event(
        context=audit_context,
        action=action,
        entity=entity,
        identity_key=identity_key,
        material_schema_version=PUBLISHING_AUDIT_MATERIAL_VERSION,
        before_material=before_material,
        after_material=after_material,
        metadata=metadata,
    )


def _request_hash(payload: dict[str, Any]) -> str:
    return sha256_hex(payload)


def _publication_dispatch_audit_identity(
    *,
    intent_id,
    request_key: str,
) -> str:
    return sha256_hex(
        {
            "schemaVersion": "publication-dispatch-audit-identity-v1",
            "intentId": str(intent_id),
            "requestKey": request_key,
        }
    )


def _admin_request_hash(
    *,
    audit_context: AuditContext,
    action: str,
    payload: dict[str, Any],
) -> str:
    if audit_context.actor_type != AuditEvent.ActorType.ADMIN:
        raise Forbidden("admin audit provenance is required")
    if (
        "requestKey" in payload
        and payload["requestKey"] != audit_context.request_key
    ):
        raise Forbidden("requestKey differs from audit provenance")
    if (
        "reason" in payload
        and payload["reason"] != audit_context.reason_code
    ):
        raise Forbidden("reason differs from audit provenance")
    return _request_hash(
        {
            "schemaVersion": "publishing-admin-request-v1",
            "action": action,
            "actorId": str(audit_context.actor_id),
            "requestKey": audit_context.request_key,
            "reason": audit_context.reason_code,
            "payload": payload,
        }
    )


def _has_request_audit(
    *,
    audit_context: AuditContext,
    action: str,
    entity,
) -> bool:
    return any(
        event.metadata_redacted.get("request_key")
        == audit_context.request_key
        for event in AuditEvent.objects.using(
            audit_context.database_alias
        ).filter(
            action=action,
            entity_type=entity._meta.label_lower,
            entity_id=entity.pk,
        )
    )


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


def _secret_resolver():
    from wisdome_writer.infrastructure.secrets import SecretResolver

    return SecretResolver()


def _resolve_secret(resolver: Any, reference: str | None) -> Any:
    if not reference:
        raise InvalidInput("발행 대상의 비밀 저장소 참조가 없습니다.")
    if hasattr(resolver, "resolve"):
        return resolver.resolve(reference)
    if hasattr(resolver, "get"):
        return resolver.get(reference)
    raise InvalidInput("구성된 비밀 저장소 resolver가 값을 읽을 수 없습니다.")


def publisher_for_target(target: PublicationTarget, *, resolver: Any | None = None):
    resolver = resolver or _secret_resolver()
    credential = _resolve_secret(resolver, target.credential_ref)
    if target.channel == ChannelCode.WORDPRESS:
        username = _resolve_secret(resolver, target.username_ref)
        return WordPressPublisher(
            base_url=target.base_url,
            username=str(username),
            application_password=str(credential),
            write_guard=_assert_external_writes_allowed,
        )
    if target.channel == ChannelCode.BLOGGER:
        access_token = credential.get("access_token") if isinstance(credential, dict) else credential
        if not access_token:
            raise InvalidInput("Blogger OAuth access token을 확인할 수 없습니다.")
        return BloggerPublisher(
            blog_id=str(target.remote_blog_id),
            access_token=str(access_token),
            write_guard=_assert_external_writes_allowed,
        )
    raise InvalidInput("지원하지 않는 발행 채널입니다.")


def _blogger_oauth_client(*, redirect_uri: str, resolver: Any | None = None) -> BloggerOAuthClient:
    resolver = resolver or _secret_resolver()
    client_id_ref = getattr(settings, "BLOGGER_OAUTH_CLIENT_ID_REF", None)
    client_secret_ref = getattr(settings, "BLOGGER_OAUTH_CLIENT_SECRET_REF", None)
    if not client_id_ref or not client_secret_ref:
        raise InvalidInput("Blogger OAuth client secret refs가 구성되지 않았습니다.")
    return BloggerOAuthClient(
        client_id=str(_resolve_secret(resolver, client_id_ref)),
        client_secret=str(_resolve_secret(resolver, client_secret_ref)),
        redirect_uri=redirect_uri,
    )


def start_blogger_oauth(
    target_id: str,
    *,
    user,
    redirect_uri: str,
    audit_context: AuditContext,
) -> dict[str, Any]:
    _require_audit_actor(audit_context, "admin")
    if audit_context.actor_id != user.pk:
        raise Forbidden("OAuth audit actor differs from the administrator")
    target = PublicationTarget.objects.using(
        audit_context.database_alias
    ).get(id=target_id, channel=ChannelCode.BLOGGER)
    nonce = sha256_hex(
        {
            "schemaVersion": "blogger-oauth-operation-v1",
            "targetId": str(target.id),
            "adminId": str(user.id),
            "requestKey": audit_context.request_key,
        }
    )[:32]
    request_hash = _request_hash(
        {
            "targetId": str(target.id),
            "targetSnapshotId": _id(target.current_snapshot_id),
            "targetSnapshotVersion": target.current_snapshot_version,
            "targetConfigHash": target.current_config_hash,
            "redirectUriHash": sha256_hex(redirect_uri),
            "adminId": str(user.id),
            "requestKey": audit_context.request_key,
            "reason": audit_context.reason_code,
            "nonce": nonce,
        }
    )
    state = signing.dumps(
        {
            "targetId": str(target.id),
            "targetSnapshotId": _id(target.current_snapshot_id),
            "targetSnapshotVersion": target.current_snapshot_version,
            "targetConfigHash": target.current_config_hash,
            "adminId": str(user.id),
            "nonce": nonce,
            "correlationId": str(audit_context.correlation_id),
            "requestKey": audit_context.request_key,
            "reason": audit_context.reason_code,
            "requestHash": request_hash,
            "redirectUriHash": sha256_hex(redirect_uri),
        },
        salt="publishing.blogger.oauth",
        compress=True,
    )
    client = _blogger_oauth_client(redirect_uri=redirect_uri)
    try:
        authorization_url = client.authorization_url(state=state)
    finally:
        client.close()
    return {
        "authorizationUrl": authorization_url,
        "expiresAt": (timezone.now() + timedelta(minutes=10)).isoformat(),
    }


def complete_blogger_oauth(
    *,
    code: str,
    state: str,
    request,
    redirect_uri: str,
) -> PublicationTarget:
    try:
        state_data = signing.loads(
            state,
            salt="publishing.blogger.oauth",
            max_age=600,
        )
    except signing.BadSignature as exc:
        raise Forbidden("Blogger OAuth state가 유효하지 않거나 만료되었습니다.") from exc
    user = request.user
    if str(state_data.get("adminId")) != str(user.id):
        raise Forbidden("OAuth를 시작한 관리자 session과 다릅니다.")
    audit_context = AuditContext.for_admin_continuation(
        request=request,
        correlation_id=state_data.get("correlationId"),
        reason_code=state_data.get("reason"),
        request_key=state_data.get("requestKey"),
    )
    expected_request_hash = _request_hash(
        {
            "targetId": str(state_data.get("targetId")),
            "targetSnapshotId": state_data.get("targetSnapshotId"),
            "targetSnapshotVersion": state_data.get("targetSnapshotVersion"),
            "targetConfigHash": state_data.get("targetConfigHash"),
            "redirectUriHash": sha256_hex(redirect_uri),
            "adminId": str(user.id),
            "requestKey": audit_context.request_key,
            "reason": audit_context.reason_code,
            "nonce": state_data.get("nonce"),
        }
    )
    if (
        state_data.get("requestHash") != expected_request_hash
        or state_data.get("redirectUriHash") != sha256_hex(redirect_uri)
    ):
        raise Forbidden("OAuth state provenance is invalid")
    with transaction.atomic(using=audit_context.database_alias):
        target = PublicationTarget.objects.using(
            audit_context.database_alias
        ).select_for_update().get(
            id=state_data["targetId"], channel=ChannelCode.BLOGGER
        )
        existing = next(
            (
                event
                for event in AuditEvent.objects.using(
                    audit_context.database_alias
                ).filter(
                    action="publication_target.oauth_connected",
                    entity_type=target._meta.label_lower,
                    entity_id=target.id,
                )
                if event.metadata_redacted.get("request_key")
                == audit_context.request_key
            ),
            None,
        )
        if existing is not None:
            require_audit_replay(
                context=audit_context,
                action="publication_target.oauth_connected",
                entity=target,
                identity_key=f"oauth:{state_data['nonce']}",
                request_hash=expected_request_hash,
            )
            return target
        if (
            _id(target.current_snapshot_id)
            != state_data.get("targetSnapshotId")
            or target.current_snapshot_version
            != state_data.get("targetSnapshotVersion")
            or target.current_config_hash
            != state_data.get("targetConfigHash")
        ):
            raise Conflict("OAuth target changed after authorization started")
        fence = (
            target.current_snapshot_id,
            target.current_snapshot_version,
            target.current_config_hash,
        )
    client = _blogger_oauth_client(redirect_uri=redirect_uri)
    try:
        token_payload = client.exchange_code(code)
    finally:
        client.close()
    store_path = getattr(settings, "BLOGGER_OAUTH_TOKEN_STORE", None)
    if not store_path:
        raise InvalidInput("Blogger OAuth token secret-store writer가 구성되지 않았습니다.")
    token_store = import_string(store_path)
    token_store_parameters = inspect.signature(token_store).parameters
    token_store_kwargs = {
        "target_id": str(target.id),
        "token_payload": token_payload,
    }
    if (
        "operation_key" in token_store_parameters
        or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in token_store_parameters.values()
        )
    ):
        token_store_kwargs["operation_key"] = state_data["nonce"]
    credential_ref = token_store(**token_store_kwargs)
    if not credential_ref:
        raise InvalidInput("OAuth token store가 credential reference를 반환하지 않았습니다.")
    with transaction.atomic(using=audit_context.database_alias):
        target = PublicationTarget.objects.using(
            audit_context.database_alias
        ).select_for_update().get(
            id=state_data["targetId"], channel=ChannelCode.BLOGGER
        )
        if fence != (
            target.current_snapshot_id,
            target.current_snapshot_version,
            target.current_config_hash,
        ):
            raise Conflict("OAuth 교환 중 target snapshot이 변경되었습니다.")
        before_material = _audit_state(target)
        target.credential_ref = str(credential_ref)
        target.connection_state = PublicationTarget.ConnectionState.PENDING
        target.preflight_state = ValidationState.NOT_RUN
        target.auto_publish_enabled = False
        target.save()
        _snapshot_locked(target)
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.oauth_connected",
            entity=target,
            identity_key=f"oauth:{state_data['nonce']}",
            before_material=before_material,
            after_material=_audit_state(target),
            metadata={
                "request_hash": expected_request_hash,
                "target_id": str(target.id),
                "state": target.connection_state,
            },
        )
        return target


def _target_material(target: PublicationTarget) -> dict[str, Any]:
    return {
        "targetId": str(target.id),
        "channel": target.channel,
        "role": target.role,
        "environment": target.environment,
        "remoteBlogId": target.remote_blog_id,
        "baseUrl": target.base_url.rstrip("/"),
        "usernameRefIdentityHash": sha256_hex(target.username_ref or "") if target.username_ref else None,
        "credentialRefIdentityHash": sha256_hex(target.credential_ref or ""),
        "capabilities": target.capabilities,
        "connectionState": target.connection_state,
        "preflightState": target.preflight_state,
        "canaryState": target.canary_state,
        "pilotState": target.pilot_state,
        "canaryTargetId": _id(target.canary_target_id),
        "canaryPolicyVersion": target.canary_policy_version,
        "publisherContractVersion": PUBLISHER_CONTRACT_VERSION,
        "publisherAdapterManifestHash": ADAPTER_MANIFESTS[target.channel],
    }


def _snapshot_locked(target: PublicationTarget) -> PublicationTargetSnapshot:
    material = _target_material(target)
    config_hash = sha256_hex(material)
    existing = PublicationTargetSnapshot.objects.filter(target=target, config_hash=config_hash).first()
    if existing:
        target.current_snapshot_id = existing.id
        target.current_snapshot_version = existing.version
        target.current_config_hash = existing.config_hash
        target.publisher_contract_version = existing.publisher_contract_version
        target.publisher_adapter_manifest_hash = existing.publisher_adapter_manifest_hash
        target.save(
            update_fields=[
                "current_snapshot_id",
                "current_snapshot_version",
                "current_config_hash",
                "publisher_contract_version",
                "publisher_adapter_manifest_hash",
                "updated_at",
            ]
        )
        return existing
    version = (
        PublicationTargetSnapshot.objects.filter(target=target).aggregate(value=Max("version"))["value"] or 0
    ) + 1
    snapshot = PublicationTargetSnapshot.objects.create(
        target=target,
        version=version,
        channel=target.channel,
        role=target.role,
        environment=target.environment,
        remote_blog_id=target.remote_blog_id,
        base_url=target.base_url.rstrip("/"),
        username_ref_identity_hash=material["usernameRefIdentityHash"],
        credential_ref_identity_hash=material["credentialRefIdentityHash"],
        capabilities=target.capabilities,
        connection_state=target.connection_state,
        preflight_state=target.preflight_state,
        canary_state=target.canary_state,
        pilot_state=target.pilot_state,
        canary_target_id=target.canary_target_id,
        canary_policy_version=target.canary_policy_version,
        publisher_contract_version=PUBLISHER_CONTRACT_VERSION,
        publisher_adapter_manifest_hash=ADAPTER_MANIFESTS[target.channel],
        config_hash=config_hash,
    )
    target.current_snapshot_id = snapshot.id
    target.current_snapshot_version = snapshot.version
    target.current_config_hash = snapshot.config_hash
    target.publisher_contract_version = snapshot.publisher_contract_version
    target.publisher_adapter_manifest_hash = snapshot.publisher_adapter_manifest_hash
    target.save(
        update_fields=[
            "current_snapshot_id",
            "current_snapshot_version",
            "current_config_hash",
            "publisher_contract_version",
            "publisher_adapter_manifest_hash",
            "updated_at",
        ]
    )
    return snapshot


@transaction.atomic
def create_target(
    data: dict[str, Any], *, audit_context: AuditContext
) -> PublicationTarget:
    _require_audit_actor(audit_context, "admin")
    request_hash = _admin_request_hash(
        audit_context=audit_context,
        action="publication_target.created",
        payload=data,
    )
    target_id = uuid.uuid5(
        _TARGET_CREATE_NAMESPACE,
        str(audit_context.request_key),
    )
    get_user_model().objects.select_for_update().get(
        pk=audit_context.actor_id
    )
    existing = PublicationTarget.objects.select_for_update().filter(
        pk=target_id
    ).first()
    if existing is not None:
        require_audit_replay(
            context=audit_context,
            action="publication_target.created",
            entity=existing,
            identity_key=audit_context.request_key,
            request_hash=request_hash,
        )
        return existing
    channel = data.get("channel")
    role = data.get("channelRole") or data.get("role")
    if (channel, role) not in {
        (ChannelCode.WORDPRESS, ChannelRole.PRIMARY),
        (ChannelCode.BLOGGER, ChannelRole.SECONDARY),
    }:
        raise InvalidInput("WordPress는 대표 원문, Blogger는 보조 배포 역할이어야 합니다.")
    base_url = str(data.get("baseUrl", "")).rstrip("/")
    if not base_url.startswith("https://"):
        raise InvalidInput("발행 대상은 HTTPS URL이어야 합니다.")
    default_capabilities = (
        WordPressPublisher.capabilities.as_dict()
        if channel == ChannelCode.WORDPRESS
        else BloggerPublisher.capabilities.as_dict()
    )
    target = PublicationTarget(
        id=target_id,
        channel=channel,
        role=role,
        environment=data.get("environment"),
        display_name=data.get("displayName", "").strip(),
        base_url=base_url,
        remote_blog_id=data.get("remoteBlogId"),
        canary_target_id=data.get("canaryTargetId"),
        username_ref=data.get("usernameRef"),
        credential_ref=data.get("credentialRef"),
        capabilities=default_capabilities,
        publisher_contract_version=PUBLISHER_CONTRACT_VERSION,
        publisher_adapter_manifest_hash=ADAPTER_MANIFESTS[channel],
    )
    target.full_clean()
    target.save()
    _snapshot_locked(target)
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.created",
        entity=target,
        identity_key=audit_context.request_key or f"target-created:{target.id}",
        before_material=None,
        after_material=_audit_state(target),
        metadata={
            "request_hash": request_hash,
            "target_id": str(target.id),
            "target_type": target.channel,
            "state": target.connection_state,
        },
    )
    return target


@transaction.atomic
def update_target(
    target_id: str,
    data: dict[str, Any],
    *,
    audit_context: AuditContext,
) -> PublicationTarget:
    _require_audit_actor(audit_context, "admin")
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_hash = _admin_request_hash(
        audit_context=audit_context,
        action="publication_target.updated",
        payload={"targetId": str(target.id), "changes": data},
    )
    if _has_request_audit(
        audit_context=audit_context,
        action="publication_target.updated",
        entity=target,
    ):
        require_audit_replay(
            context=audit_context,
            action="publication_target.updated",
            entity=target,
            identity_key=audit_context.request_key,
            request_hash=request_hash,
        )
        return target
    immutable = {"channel", "channelRole", "role", "environment", "baseUrl", "remoteBlogId"}
    if immutable.intersection(data):
        raise InvalidInput("채널, 역할, 환경, base URL과 remote blog ID는 변경할 수 없습니다.")
    before_material = _audit_state(target)
    field_map = {
        "displayName": "display_name",
        "canaryTargetId": "canary_target_id",
        "usernameRef": "username_ref",
        "credentialRef": "credential_ref",
    }
    changed_connection = False
    for external_name, field_name in field_map.items():
        if external_name in data:
            setattr(target, field_name, data[external_name])
            if external_name != "displayName":
                changed_connection = True
    empty_before, empty_after = _state_transition_manifests(
        [],
        state_field="state",
        next_state=PublicationIntent.State.STALE,
    )
    transition_before = {
        "validations": empty_before,
        "intents": empty_before,
    }
    transition_after = {
        "validations": empty_after,
        "intents": empty_after,
    }
    if changed_connection:
        target.connection_state = PublicationTarget.ConnectionState.PENDING
        target.preflight_state = ValidationState.STALE
        target.pilot_state = ValidationState.STALE
        target.auto_publish_enabled = False
        target.latest_auto_publish_activation_id = None
        validation_query = (
            AutoPublishValidation.objects.select_for_update()
            .filter(
                target=target,
                status=AutoPublishValidation.State.PASSED,
            )
            .order_by("id")
        )
        validation_rows = list(validation_query.values("id", "status"))
        validation_before, validation_after = _state_transition_manifests(
            validation_rows,
            state_field="status",
            next_state=AutoPublishValidation.State.STALE,
        )
        validation_query.update(
            status=AutoPublishValidation.State.STALE,
            invalidated_at=timezone.now(),
            invalidation_reason="target_configuration_changed",
        )
        intent_query = (
            PublicationIntent.objects.select_for_update()
            .filter(
                target_snapshot_refs__contains=[
                    {"targetId": str(target.id)}
                ],
                state__in=[
                    PublicationIntent.State.DRAFT,
                    PublicationIntent.State.AWAITING_APPROVAL,
                    PublicationIntent.State.APPROVED,
                ],
            )
            .order_by("id")
        )
        intent_rows = list(intent_query.values("id", "state"))
        intent_before, intent_after = _state_transition_manifests(
            intent_rows,
            state_field="state",
            next_state=PublicationIntent.State.STALE,
        )
        intent_query.update(state=PublicationIntent.State.STALE)
        transition_before = {
            "validations": validation_before,
            "intents": intent_before,
        }
        transition_after = {
            "validations": validation_after,
            "intents": intent_after,
        }
    target.full_clean()
    target.save()
    _snapshot_locked(target)
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.updated",
        entity=target,
        identity_key=(
            audit_context.request_key
            or f"target-updated:{audit_context.correlation_id}"
        ),
        before_material={
            **before_material,
            "staleTransitions": transition_before,
        },
        after_material=_audit_state(
            target,
            staleTransitions=transition_after,
        ),
        metadata={
            "request_hash": request_hash,
            "target_id": str(target.id),
            "target_type": target.channel,
            "state": target.connection_state,
        },
    )
    return target


@dataclass(frozen=True)
class TargetPreflightFence:
    target_id: uuid.UUID
    target_snapshot_id: uuid.UUID
    target_config_hash: str
    target_snapshot_version: int


@transaction.atomic
def request_target_preflight(
    target_id: str,
    *,
    audit_context: AuditContext,
):
    _require_audit_actor(audit_context, "admin")
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_hash = _admin_request_hash(
        audit_context=audit_context,
        action="publication_target.preflight_requested",
        payload={
            "targetId": str(target.id),
            "targetSnapshotId": _id(target.current_snapshot_id),
            "targetConfigHash": target.current_config_hash,
        },
    )
    dedupe_key = (
        f"publication.preflight_requested:{target.id}:"
        f"{target.current_snapshot_version}"
    )
    if _has_request_audit(
        audit_context=audit_context,
        action="publication_target.preflight_requested",
        entity=target,
    ):
        require_audit_replay(
            context=audit_context,
            action="publication_target.preflight_requested",
            entity=target,
            identity_key=audit_context.request_key,
            request_hash=request_hash,
        )
        from wisdome_writer.infrastructure.models import OutboxMessage

        event = OutboxMessage.objects.filter(message_key=dedupe_key).first()
        if event is None:
            raise Conflict(
                "preflight replay has an audit event but no matching outbox event"
            )
        return event
    event = _enqueue_event(
        "publication.preflight_requested",
        {
            "target_id": str(target.id),
            "target_snapshot_id": str(target.current_snapshot_id),
            "target_config_hash": target.current_config_hash,
        },
        dedupe_key=dedupe_key,
        aggregate_type="publication_target",
        aggregate_id=target.id,
        job_id=target.id,
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.preflight_requested",
        entity=target,
        identity_key=audit_context.request_key or f"preflight-request:{event.id}",
        before_material=_audit_state(target),
        after_material=_audit_state(target),
        metadata={
            "request_hash": request_hash,
            "target_id": str(target.id),
            "state": "queued",
        },
    )
    return event


@transaction.atomic
def begin_target_preflight(
    target_id: str,
    *,
    expected_snapshot_id: str,
    expected_config_hash: str,
    audit_context: AuditContext,
) -> tuple[PublicationTarget, TargetPreflightFence] | None:
    _require_audit_actor(audit_context, "worker")
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    _require_worker_event(
        audit_context,
        topic="publication.preflight_requested",
        aggregate_id=target.id,
        payload_identity={
            "target_id": str(target.id),
            "target_snapshot_id": expected_snapshot_id,
            "target_config_hash": expected_config_hash,
        },
    )
    if _worker_audit_replay(
        audit_context,
        entity=target,
        candidates=(
            (
                "publication_target.preflight",
                f"{audit_context.event_key}:preflight-result",
                None,
            ),
            (
                "publication_target.preflight_stale_before_call",
                f"{audit_context.event_key}:preflight-stale-before",
                None,
            ),
            (
                "publication_target.preflight_stale_after_call",
                f"{audit_context.event_key}:preflight-stale-after",
                None,
            ),
        ),
    ):
        return None
    if (
        not target.current_snapshot_id
        or str(target.current_snapshot_id) != str(expected_snapshot_id)
        or target.current_config_hash != expected_config_hash
    ):
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.preflight_stale_before_call",
            entity=target,
            identity_key=f"{audit_context.event_key}:preflight-stale-before",
            before_material={
                "targetId": str(target.id),
                "eventKey": audit_context.event_key,
                "expectedSnapshotId": expected_snapshot_id,
                "expectedConfigHash": expected_config_hash,
                "classification": "stale_before_call",
            },
            after_material={
                "targetId": str(target.id),
                "eventKey": audit_context.event_key,
                "expectedSnapshotId": expected_snapshot_id,
                "expectedConfigHash": expected_config_hash,
                "classification": "stale_before_call",
            },
            metadata={
                "target_id": str(target.id),
                "result": "stale",
            },
        )
        return None
    return target, TargetPreflightFence(
        target_id=target.id,
        target_snapshot_id=target.current_snapshot_id,
        target_config_hash=target.current_config_hash,
        target_snapshot_version=target.current_snapshot_version,
    )


@transaction.atomic
def persist_target_preflight_result(
    fence: TargetPreflightFence,
    result,
    *,
    audit_context: AuditContext,
) -> tuple[PublicationTarget, bool]:
    _require_audit_actor(audit_context, "worker")
    target = PublicationTarget.objects.select_for_update().get(id=fence.target_id)
    _require_worker_event(
        audit_context,
        topic="publication.preflight_requested",
        aggregate_id=target.id,
        payload_identity={
            "target_id": str(target.id),
            "target_snapshot_id": str(fence.target_snapshot_id),
            "target_config_hash": fence.target_config_hash,
        },
    )
    if (
        target.current_snapshot_id != fence.target_snapshot_id
        or target.current_config_hash != fence.target_config_hash
        or target.current_snapshot_version != fence.target_snapshot_version
    ):
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.preflight_stale_after_call",
            entity=target,
            identity_key=f"{audit_context.event_key}:preflight-stale-after",
            before_material={
                "targetId": str(target.id),
                "eventKey": audit_context.event_key,
                "expectedSnapshotId": str(fence.target_snapshot_id),
                "expectedConfigHash": fence.target_config_hash,
                "classification": "stale_after_call",
            },
            after_material={
                "targetId": str(target.id),
                "eventKey": audit_context.event_key,
                "expectedSnapshotId": str(fence.target_snapshot_id),
                "expectedConfigHash": fence.target_config_hash,
                "classification": "stale_after_call",
            },
            metadata={
                "target_id": str(target.id),
                "result": "stale",
            },
        )
        return target, False
    before_material = _audit_state(target)
    validation_before, validation_after = _state_transition_manifests(
        [],
        state_field="status",
        next_state=AutoPublishValidation.State.STALE,
    )
    target.capabilities = result.capabilities.as_dict()
    target.preflight_state = ValidationState.PASSED if result.passed else ValidationState.FAILED
    if result.passed:
        target.connection_state = PublicationTarget.ConnectionState.VERIFIED
    else:
        target.connection_state = PublicationTarget.ConnectionState.BLOCKED
        target.auto_publish_enabled = False
        validation_query = (
            AutoPublishValidation.objects.select_for_update()
            .filter(
                target=target,
                status=AutoPublishValidation.State.PASSED,
            )
            .order_by("id")
        )
        validation_rows = list(validation_query.values("id", "status"))
        validation_before, validation_after = _state_transition_manifests(
            validation_rows,
            state_field="status",
            next_state=AutoPublishValidation.State.STALE,
        )
        validation_query.update(
            status=AutoPublishValidation.State.STALE,
            invalidated_at=timezone.now(),
            invalidation_reason="target_preflight_failed",
        )
    if not result.passed and result.error_code in {
        "wordpress_auth_or_capability_denied",
        "blogger_token_expired",
        "blogger_scope_or_owner_denied",
    }:
        target.connection_state = PublicationTarget.ConnectionState.EXPIRED
        target.auto_publish_enabled = False
    target.last_preflight_at = timezone.now()
    target.save()
    _snapshot_locked(target)
    result_hash = sha256_hex(
        {
            "passed": result.passed,
            "remoteIdentity": result.remote_identity,
            "remoteUrl": result.remote_url,
            "capabilities": result.capabilities.as_dict(),
            "checks": result.checks,
            "errorCode": result.error_code,
        }
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.preflight",
        entity=target,
        identity_key=f"{audit_context.event_key}:preflight-result",
        before_material={
            **before_material,
            "staleValidations": validation_before,
        },
        after_material={
            **_audit_state(target),
            "resultHash": result_hash,
            "staleValidations": validation_after,
        },
        metadata={
            "target_id": str(target.id),
            "result": "passed" if result.passed else "failed",
            "result_hash": result_hash,
            "error_code": result.error_code,
        },
    )
    _enqueue_event(
        "publishing.target_preflight.completed",
        {
            "target_id": str(target.id),
            "target_snapshot_id": str(fence.target_snapshot_id),
            "target_config_hash": fence.target_config_hash,
            "result_hash": result_hash,
            "passed": result.passed,
        },
        dedupe_key=(
            f"publishing.target_preflight.completed:{target.id}:"
            f"{fence.target_snapshot_id}:{result_hash}"
        ),
        aggregate_type="publication_target",
        aggregate_id=target.id,
        job_id=target.id,
    )
    return target, True


def run_target_preflight(
    target_id: str,
    *,
    expected_snapshot_id: str,
    expected_config_hash: str,
    audit_context: AuditContext,
    resolver: Any | None = None,
) -> tuple[PublicationTarget, bool]:
    _require_audit_actor(audit_context, "worker")
    prepared = begin_target_preflight(
        target_id,
        expected_snapshot_id=expected_snapshot_id,
        expected_config_hash=expected_config_hash,
        audit_context=audit_context,
    )
    if prepared is None:
        return PublicationTarget.objects.get(id=target_id), False
    target, fence = prepared
    adapter = publisher_for_target(target, resolver=resolver)
    try:
        result = adapter.preflight_connection()
    finally:
        adapter.close()
    return persist_target_preflight_result(
        fence,
        result,
        audit_context=audit_context,
    )


@transaction.atomic
def create_canary_run(
    target_id: str,
    *,
    policy_version: str,
    reason: str,
    request_key: str,
    user,
    audit_context: AuditContext,
) -> TargetCanaryRun:
    _require_audit_actor(audit_context, "admin")
    if audit_context.actor_id != user.pk:
        raise Forbidden("canary audit actor differs from the administrator")
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    if target.environment != TargetEnvironment.TEST:
        raise Forbidden("쓰기가 발생하는 canary는 격리된 test target에서만 실행할 수 있습니다.")
    if target.preflight_state != ValidationState.PASSED:
        raise Conflict("읽기 전용 preflight를 먼저 통과해야 합니다.")
    request_hash = _admin_request_hash(
        audit_context=audit_context,
        action="publication_target.canary_requested",
        payload={
            "targetId": str(target.id),
            "policyVersion": policy_version,
            "reason": reason,
            "requestKey": request_key,
        },
    )
    existing = TargetCanaryRun.objects.filter(target=target, request_key=request_key).first()
    if existing:
        if (
            existing.policy_version != policy_version
            or existing.reason != reason
            or existing.requested_by_id != user.pk
        ):
            raise Conflict(
                "the canary request key was already used with different material"
            )
        require_audit_replay(
            context=audit_context,
            action="publication_target.canary_requested",
            entity=existing,
            identity_key=f"canary-request:{existing.id}",
            request_hash=request_hash,
        )
        return existing
    snapshot = PublicationTargetSnapshot.objects.get(id=target.current_snapshot_id)
    run = TargetCanaryRun.objects.create(
        target=target,
        target_snapshot=snapshot,
        policy_version=policy_version,
        request_key=request_key,
        reason=reason,
        requested_by=user,
    )
    _enqueue_event(
        "publishing.target_canary.requested",
        {
            "canary_run_id": str(run.id),
            "target_id": str(target.id),
        },
        dedupe_key=f"target-canary:{run.id}",
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.canary_requested",
        entity=run,
        identity_key=f"canary-request:{run.id}",
        before_material=None,
        after_material=_audit_state(
            run,
            targetId=str(target.id),
            policyVersion=policy_version,
        ),
        metadata={
            "request_hash": request_hash,
            "target_id": str(target.id),
            "state": run.state,
            "policy_version": policy_version,
        },
    )
    return run


@dataclass(frozen=True)
class TargetCanaryFence:
    run_id: uuid.UUID
    target_id: uuid.UUID
    target_snapshot_id: uuid.UUID
    target_config_hash: str


@transaction.atomic
def begin_canary_run(
    run_id: str,
    *,
    audit_context: AuditContext,
) -> tuple[TargetCanaryRun, TargetCanaryFence | None]:
    _require_audit_actor(audit_context, "worker")
    run = (
        TargetCanaryRun.objects.select_for_update()
        .select_related("target", "target_snapshot")
        .get(id=run_id)
    )
    _require_worker_event(
        audit_context,
        topic="publishing.target_canary.requested",
        aggregate_id=run.target_id,
        payload_identity={
            "canary_run_id": str(run.id),
            "target_id": str(run.target_id),
        },
    )
    if _worker_audit_replay(
        audit_context,
        entity=run,
        candidates=(
            (
                "publication_target.canary_completed",
                f"{audit_context.event_key}:canary-result",
                (
                    {"result_hash": run.report_hash}
                    if run.report_hash
                    else None
                ),
            ),
        ),
    ):
        return run, None
    if run.state in {
        TargetCanaryRun.State.PASSED,
        TargetCanaryRun.State.FAILED,
        TargetCanaryRun.State.CLEANUP_REQUIRED,
    }:
        raise Conflict(
            "terminal canary result has no matching audit event for this worker event"
        )
    if run.target.current_snapshot_id != run.target_snapshot_id:
        before_material = _audit_state(run)
        run.state = TargetCanaryRun.State.FAILED
        run.report_hash = sha256_hex(
            {
                "runId": str(run.id),
                "targetSnapshotId": str(run.target_snapshot_id),
                "result": "target_snapshot_stale",
            }
        )
        run.stage_results = [{"code": "target_snapshot_stale", "passed": False}]
        run.finished_at = timezone.now()
        run.save(
            update_fields=(
                "state",
                "report_hash",
                "stage_results",
                "finished_at",
            )
        )
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.canary_completed",
            entity=run,
            identity_key=f"{audit_context.event_key}:canary-result",
            before_material=before_material,
            after_material=_audit_state(run, resultHash=run.report_hash),
            metadata={
                "target_id": str(run.target_id),
                "result": "stale",
                "result_hash": run.report_hash,
                "state": run.state,
                "policy_version": run.policy_version,
            },
        )
        return run, None
    if run.state == TargetCanaryRun.State.QUEUED:
        before_material = _audit_state(run)
        run.state = TargetCanaryRun.State.RUNNING
        run.save(update_fields=("state",))
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.canary_started",
            entity=run,
            identity_key=f"{audit_context.event_key}:canary-start",
            before_material=before_material,
            after_material=_audit_state(run),
            metadata={
                "target_id": str(run.target_id),
                "result": "started",
                "state": run.state,
                "policy_version": run.policy_version,
            },
        )
    elif run.state == TargetCanaryRun.State.RUNNING:
        if not _worker_audit_replay(
            audit_context,
            entity=run,
            candidates=(
                (
                    "publication_target.canary_started",
                    f"{audit_context.event_key}:canary-start",
                    None,
                ),
            ),
        ):
            raise Conflict(
                "running canary has no matching started audit event"
            )
        before_material = _audit_state(run)
        run.state = TargetCanaryRun.State.CLEANUP_REQUIRED
        run.stage_results = [
            {
                "code": "worker_redelivery_after_external_call_boundary",
                "passed": False,
            }
        ]
        run.report_hash = sha256_hex(run.stage_results)
        run.finished_at = timezone.now()
        run.save(
            update_fields=(
                "state",
                "stage_results",
                "report_hash",
                "finished_at",
            )
        )
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.canary_completed",
            entity=run,
            identity_key=f"{audit_context.event_key}:canary-result",
            before_material=before_material,
            after_material=_audit_state(
                run,
                resultHash=run.report_hash,
            ),
            metadata={
                "target_id": str(run.target_id),
                "result": "unknown",
                "result_hash": run.report_hash,
                "state": run.state,
                "policy_version": run.policy_version,
            },
        )
        return run, None
    return run, TargetCanaryFence(
        run_id=run.id,
        target_id=run.target_id,
        target_snapshot_id=run.target_snapshot_id,
        target_config_hash=run.target_snapshot.config_hash,
    )


@transaction.atomic
def persist_canary_run_result(
    fence: TargetCanaryFence,
    *,
    stages: list[dict[str, object]],
    passed: bool,
    audit_context: AuditContext,
) -> TargetCanaryRun:
    _require_audit_actor(audit_context, "worker")
    run = (
        TargetCanaryRun.objects.select_for_update()
        .select_related("target")
        .get(id=fence.run_id)
    )
    _require_worker_event(
        audit_context,
        topic="publishing.target_canary.requested",
        aggregate_id=fence.target_id,
        payload_identity={
            "canary_run_id": str(run.id),
            "target_id": str(fence.target_id),
        },
    )
    input_result_hash = sha256_hex(
        {
            "stages": stages,
            "passed": passed,
        }
    )
    if _worker_audit_replay(
        audit_context,
        entity=run,
        candidates=(
            (
                "publication_target.canary_completed",
                f"{audit_context.event_key}:canary-result",
                {"request_hash": input_result_hash},
            ),
        ),
    ):
        return run
    before_material = _audit_state(run)
    target = PublicationTarget.objects.select_for_update().get(id=fence.target_id)
    target_before_material = _audit_state(target)
    fenced = (
        run.target_snapshot_id == fence.target_snapshot_id
        and target.current_snapshot_id == fence.target_snapshot_id
        and target.current_config_hash == fence.target_config_hash
    )
    if not fenced:
        stages = [*stages, {"code": "target_snapshot_stale_after_call", "passed": False}]
        passed = False
    report_hash = sha256_hex(stages)
    run.stage_results = stages
    run.report_hash = report_hash
    run.state = (
        TargetCanaryRun.State.PASSED
        if passed
        else TargetCanaryRun.State.FAILED
    )
    if any(str(row.get("code", "")).endswith("cleanup_pending") for row in stages):
        run.state = TargetCanaryRun.State.CLEANUP_REQUIRED
    run.finished_at = timezone.now()
    run.save(
        update_fields=(
            "stage_results",
            "report_hash",
            "state",
            "finished_at",
        )
    )
    if fenced:
        target.canary_state = (
            ValidationState.PASSED if passed else ValidationState.FAILED
        )
        if passed:
            target.canary_policy_version = run.policy_version
            target.connection_state = PublicationTarget.ConnectionState.VERIFIED
        target.last_canary_at = timezone.now()
        target.save()
        _snapshot_locked(target)
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.canary_completed",
        entity=run,
        identity_key=f"{audit_context.event_key}:canary-result",
        before_material={
            "run": before_material,
            "target": target_before_material,
        },
        after_material={
            "run": _audit_state(run, resultHash=report_hash),
            "target": _audit_state(target),
        },
        metadata={
            "target_id": str(run.target_id),
            "request_hash": input_result_hash,
            "result": "passed" if passed else "failed",
            "result_hash": report_hash,
            "state": run.state,
            "policy_version": run.policy_version,
        },
    )
    return run


def _validation_material(data: dict[str, Any], target_id: str) -> dict[str, Any]:
    return {
        "targetId": target_id,
        "topic": data["topic"],
        "targetSnapshotId": data["targetSnapshotId"],
        "targetConfigHash": data["targetConfigHash"],
        "sourceRegistrySnapshotId": data["sourceRegistrySnapshotId"],
        "registryManifestHash": data["registryManifestHash"],
        "sourceAdapterManifestHash": data["sourceAdapterManifestHash"],
        "extractionProfileManifestHash": data["extractionProfileManifestHash"],
        "generationPipelineManifestHash": data["generationPipelineManifestHash"],
        "topicPolicyVersion": data["topicPolicyVersion"],
        "editorialPolicyHash": data["editorialPolicyHash"],
        "qualityGateManifestHash": data["qualityGateManifestHash"],
        "renderContractVersion": data["renderContractVersion"],
        "channelContractVersion": data["channelContractVersion"],
        "publisherAdapterManifestHash": data["publisherAdapterManifestHash"],
        "testReportObjectKey": data["testReportObjectKey"],
        "testReportObjectVersion": data["testReportObjectVersion"],
        "testReportHash": data["testReportHash"],
    }


@transaction.atomic
def create_auto_publish_validation(
    target_id: str,
    data: dict[str, Any],
    *,
    audit_context: AuditContext,
) -> AutoPublishValidation:
    _require_audit_actor(audit_context, "admin")
    if (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden(
            "validation request provenance differs from the audit context"
        )
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_key = data["requestKey"]
    material = _validation_material(data, str(target.id))
    request_hash = _request_hash({"requestKey": request_key, **material})
    existing = AutoPublishValidation.objects.filter(target=target, request_key=request_key).first()
    if existing:
        if existing.request_hash != request_hash:
            raise Conflict("같은 request key가 다른 validation payload에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="auto_publish_validation.created",
            entity=existing,
            identity_key=f"validation-created:{existing.id}",
            request_hash=request_hash,
        )
        return existing
    if str(target.current_snapshot_id) != str(data["targetSnapshotId"]):
        raise Conflict("현재 target snapshot과 validation 대상이 다릅니다.")
    if target.current_config_hash != data["targetConfigHash"]:
        raise Conflict("현재 target config hash와 validation 대상이 다릅니다.")
    if ADAPTER_MANIFESTS[target.channel] != data["publisherAdapterManifestHash"]:
        raise Conflict("현재 배포된 publisher adapter material과 다릅니다.")
    material_hash = sha256_hex(material)
    same_material = AutoPublishValidation.objects.filter(
        target=target, material_hash=material_hash
    ).first()
    if same_material:
        return same_material
    validation = AutoPublishValidation.objects.create(
        target=target,
        topic_code=data["topic"],
        target_snapshot_id=data["targetSnapshotId"],
        target_config_hash=data["targetConfigHash"],
        source_registry_snapshot_id=data["sourceRegistrySnapshotId"],
        registry_manifest_hash=data["registryManifestHash"],
        source_adapter_manifest_hash=data["sourceAdapterManifestHash"],
        extraction_profile_manifest_hash=data["extractionProfileManifestHash"],
        generation_pipeline_manifest_hash=data["generationPipelineManifestHash"],
        topic_policy_version=data["topicPolicyVersion"],
        editorial_policy_hash=data["editorialPolicyHash"],
        quality_gate_manifest_hash=data["qualityGateManifestHash"],
        render_contract_version=data["renderContractVersion"],
        channel_contract_version=data["channelContractVersion"],
        publisher_adapter_manifest_hash=data["publisherAdapterManifestHash"],
        test_report_object_key=data["testReportObjectKey"],
        test_report_object_version=data["testReportObjectVersion"],
        test_report_hash=data["testReportHash"],
        material_hash=material_hash,
        request_key=request_key,
        request_hash=request_hash,
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="auto_publish_validation.created",
        entity=validation,
        identity_key=f"validation-created:{validation.id}",
        before_material=None,
        after_material=_audit_state(validation),
        metadata={
            "target_id": str(target.id),
            "validation_id": str(validation.id),
            "material_hash": material_hash,
            "request_hash": request_hash,
            "state": validation.status,
        },
    )
    return validation


@transaction.atomic
def decide_auto_publish_validation(
    target_id: str,
    validation_id: str,
    data: dict[str, Any],
    *,
    request,
    audit_context: AuditContext,
) -> tuple[AutoPublishValidationDecision, bool]:
    _require_audit_actor(audit_context, "admin")
    user = request.user
    if audit_context.actor_id != user.pk:
        raise Forbidden("validation audit actor differs from the administrator")
    if (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden(
            "validation decision provenance differs from the audit context"
        )
    target = PublicationTarget.objects.select_for_update().get(
        id=target_id
    )
    validation = AutoPublishValidation.objects.select_for_update().get(
        id=validation_id,
        target=target,
    )
    validation.target = target
    request_hash = _request_hash(data)
    existing = validation.decisions.filter(request_key=data["requestKey"]).first()
    if existing:
        if existing.request_hash != request_hash or existing.decided_by_id != user.pk:
            raise Conflict("같은 request key가 다른 decision payload에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="auto_publish_validation.decided",
            entity=validation,
            identity_key=f"validation-decision:{existing.id}",
            request_hash=request_hash,
        )
        return existing, False
    before_material = _audit_state(validation)
    target_before_material = _audit_state(target)
    intent_before, intent_after = _state_transition_manifests(
        [],
        state_field="state",
        next_state=PublicationIntent.State.STALE,
    )
    if _id(validation.latest_decision_id) != _id(data.get("expectedLatestDecisionId")):
        raise Conflict("validation decision이 갱신되었습니다. 다시 불러오세요.")
    consume_reauthentication_proof(
        request=request,
        proof_id=data["reauthProofId"],
        action_scope="auto_publish_change",
        entity_type="auto_publish_validation",
        entity_id=validation.id,
    )
    if validation.target.current_snapshot_id != validation.target_snapshot_id:
        raise Conflict("validation target snapshot이 이미 만료되었습니다.")
    if validation.target.current_config_hash != validation.target_config_hash:
        raise Conflict("validation target config가 이미 만료되었습니다.")
    version = validation.decision_version + 1
    decision_hash = sha256_hex(
        {
            "validationId": str(validation.id),
            "version": version,
            "decision": data["decision"],
            "supersedes": _id(validation.latest_decision_id),
            "materialHash": validation.material_hash,
            "reason": data["reason"],
        }
    )
    decision = AutoPublishValidationDecision.objects.create(
        validation=validation,
        version=version,
        decision=data["decision"],
        supersedes_decision_id=validation.latest_decision_id,
        request_key=data["requestKey"],
        request_hash=request_hash,
        decision_hash=decision_hash,
        reauth_proof_id=data["reauthProofId"],
        decided_by=user,
        reason=data["reason"],
    )
    validation.latest_decision_id = decision.id
    validation.decision_version = version
    validation.status = (
        AutoPublishValidation.State.PASSED
        if decision.decision == AutoPublishValidationDecision.Decision.PASSED
        else AutoPublishValidation.State.REVOKED
    )
    validation.save(update_fields=["latest_decision_id", "decision_version", "status"])
    if decision.decision == AutoPublishValidationDecision.Decision.REVOKED:
        target.auto_publish_enabled = False
        target.save(update_fields=["auto_publish_enabled", "updated_at"])
        intent_query = (
            PublicationIntent.objects.select_for_update()
            .filter(
                auto_publish_validation_refs__contains=[
                    {"validationId": str(validation.id)}
                ],
                state__in=[
                    PublicationIntent.State.APPROVED,
                    PublicationIntent.State.AWAITING_APPROVAL,
                ],
            )
            .order_by("id")
        )
        intent_rows = list(intent_query.values("id", "state"))
        intent_before, intent_after = _state_transition_manifests(
            intent_rows,
            state_field="state",
            next_state=PublicationIntent.State.STALE,
        )
        intent_query.update(state=PublicationIntent.State.STALE)
    _record_publishing_audit(
        audit_context=audit_context,
        action="auto_publish_validation.decided",
        entity=validation,
        identity_key=f"validation-decision:{decision.id}",
        before_material={
            "validation": before_material,
            "target": target_before_material,
            "staleIntents": intent_before,
        },
        after_material={
            "validation": _audit_state(
                validation,
                decisionHash=decision_hash,
            ),
            "target": _audit_state(target),
            "staleIntents": intent_after,
        },
        metadata={
            "target_id": str(validation.target_id),
            "validation_id": str(validation.id),
            "decision_id": str(decision.id),
            "decision": decision.decision,
            "decision_hash": decision_hash,
            "state": validation.status,
            "version": decision.version,
            "reauth_proof_id": str(decision.reauth_proof_id),
        },
    )
    return decision, True


def _normalized_validation_refs(refs: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    normalized = [
        {
            "targetId": str(ref["targetId"]),
            "targetSnapshotId": str(ref["targetSnapshotId"]),
            "validationId": str(ref["validationId"]),
            "materialHash": ref["materialHash"],
        }
        for ref in refs
    ]
    return sorted(normalized, key=lambda row: (row["targetId"], row["validationId"]))


@transaction.atomic
def set_auto_publish(
    target_id: str,
    data: dict[str, Any],
    *,
    request,
    audit_context: AuditContext,
) -> tuple[AutoPublishActivation, bool]:
    _require_audit_actor(audit_context, "admin")
    user = request.user
    if audit_context.actor_id != user.pk:
        raise Forbidden("activation audit actor differs from the administrator")
    if (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden(
            "activation provenance differs from the audit context"
        )
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_hash = _request_hash(data)
    existing = target.activations.filter(request_key=data["requestKey"]).first()
    if existing:
        if existing.request_hash != request_hash or existing.decided_by_id != user.pk:
            raise Conflict("같은 request key가 다른 activation payload에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="auto_publish_activation.decided",
            entity=target,
            identity_key=f"auto-activation:{existing.id}",
            request_hash=request_hash,
        )
        return existing, False
    before_material = _audit_state(target)
    intent_before, intent_after = _state_transition_manifests(
        [],
        state_field="state",
        next_state=PublicationIntent.State.STALE,
    )
    if _id(target.latest_auto_publish_activation_id) != _id(data.get("expectedLatestActivationId")):
        raise Conflict("자동발행 상태가 갱신되었습니다. 다시 불러오세요.")
    consume_reauthentication_proof(
        request=request,
        proof_id=data["reauthProofId"],
        action_scope="auto_publish_change",
        entity_type="publication_target",
        entity_id=target.id,
    )
    enabled = bool(data["enabled"])
    refs = _normalized_validation_refs(data.get("validationRefs", []))
    if enabled:
        if target.connection_state != PublicationTarget.ConnectionState.VERIFIED:
            raise Conflict("검증된 연결만 자동발행할 수 있습니다.")
        if target.preflight_state != ValidationState.PASSED:
            raise Conflict("현재 target preflight를 통과해야 합니다.")
        if target.environment == TargetEnvironment.PRODUCTION:
            if not target.canary_target_id or target.canary_target.canary_state != ValidationState.PASSED:
                raise Conflict("동일 채널 test target의 현재 canary가 필요합니다.")
            if target.pilot_state != ValidationState.PASSED:
                raise Conflict("관리자 승인 운영 파일럿 게시가 필요합니다.")
        if not refs:
            raise InvalidInput("활성화에는 최소 한 개의 passed validation이 필요합니다.")
        validation_ids = [ref["validationId"] for ref in refs]
        validations = list(AutoPublishValidation.objects.filter(id__in=validation_ids, target=target))
        if len(validations) != len(validation_ids):
            raise InvalidInput("validation target 또는 ID가 올바르지 않습니다.")
        by_id = {str(row.id): row for row in validations}
        for ref in refs:
            validation = by_id[ref["validationId"]]
            if validation.status != AutoPublishValidation.State.PASSED:
                raise Conflict("passed 상태가 아닌 validation은 활성화할 수 없습니다.")
            if validation.material_hash != ref["materialHash"]:
                raise Conflict("validation material hash가 다릅니다.")
            if validation.target_snapshot_id != target.current_snapshot_id:
                raise Conflict("validation target snapshot이 현재 값과 다릅니다.")
            if ref["targetSnapshotId"] != str(target.current_snapshot_id):
                raise Conflict("validation ref snapshot이 현재 값과 다릅니다.")
    elif refs:
        raise InvalidInput("비활성화 요청에는 validation refs가 없어야 합니다.")
    version = target.auto_publish_activation_version + 1
    validation_manifest_hash = sha256_hex(refs)
    decision = AutoPublishActivation.Decision.ENABLED if enabled else AutoPublishActivation.Decision.REVOKED
    activation_hash = sha256_hex(
        {
            "targetId": str(target.id),
            "targetSnapshotId": str(target.current_snapshot_id),
            "operationalConfigHash": target.current_config_hash,
            "validationRefs": refs,
            "version": version,
            "decision": decision,
            "supersedes": _id(target.latest_auto_publish_activation_id),
        }
    )
    activation = AutoPublishActivation.objects.create(
        target=target,
        target_snapshot_id=target.current_snapshot_id,
        target_operational_config_hash=target.current_config_hash,
        validation_refs=refs,
        validation_manifest_hash=validation_manifest_hash,
        version=version,
        decision=decision,
        supersedes_activation_id=target.latest_auto_publish_activation_id,
        request_key=data["requestKey"],
        request_hash=request_hash,
        activation_hash=activation_hash,
        reauth_proof_id=data["reauthProofId"],
        decided_by=user,
        reason=data["reason"],
    )
    target.latest_auto_publish_activation_id = activation.id
    target.auto_publish_activation_version = version
    target.auto_publish_enabled = enabled
    target.save(
        update_fields=[
            "latest_auto_publish_activation_id",
            "auto_publish_activation_version",
            "auto_publish_enabled",
            "updated_at",
        ]
    )
    if not enabled:
        intent_query = (
            PublicationIntent.objects.select_for_update()
            .filter(
                auto_publish_activation_refs__contains=[
                    {
                        "activationId": str(
                            activation.supersedes_activation_id
                        )
                    }
                ],
                state__in=[
                    PublicationIntent.State.APPROVED,
                    PublicationIntent.State.AWAITING_APPROVAL,
                ],
            )
            .order_by("id")
        )
        intent_rows = list(intent_query.values("id", "state"))
        intent_before, intent_after = _state_transition_manifests(
            intent_rows,
            state_field="state",
            next_state=PublicationIntent.State.STALE,
        )
        intent_query.update(state=PublicationIntent.State.STALE)
    _record_publishing_audit(
        audit_context=audit_context,
        action="auto_publish_activation.decided",
        entity=target,
        identity_key=f"auto-activation:{activation.id}",
        before_material={
            **before_material,
            "staleIntents": intent_before,
        },
        after_material=_audit_state(
            target,
            activationHash=activation_hash,
            staleIntents=intent_after,
        ),
        metadata={
            "target_id": str(target.id),
            "activation_id": str(activation.id),
            "decision": activation.decision,
            "decision_hash": activation_hash,
            "enabled": target.auto_publish_enabled,
            "version": activation.version,
            "reauth_proof_id": str(activation.reauth_proof_id),
        },
    )
    return activation, True


def _target_ref_map(refs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for ref in refs:
        key = str(ref["targetId"])
        if key in result:
            raise InvalidInput("target snapshot ref가 중복되었습니다.")
        result[key] = {
            "targetId": key,
            "targetSnapshotId": str(ref["targetSnapshotId"]),
            "targetConfigHash": ref["targetConfigHash"],
        }
    return result


def _command_map(commands: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for command in commands:
        target_id = str(command["targetId"])
        if target_id in result:
            raise InvalidInput("target command가 중복되었습니다.")
        normalized = {
            "targetId": target_id,
            "targetSnapshotId": str(command["targetSnapshotId"]),
            "targetConfigHash": command["targetConfigHash"],
            "resolvedAction": command["resolvedAction"],
            "canonicalDependencyTargetId": _id(command.get("canonicalDependencyTargetId")),
        }
        normalized["targetCommandHash"] = sha256_hex(normalized)
        result[target_id] = normalized
    return result


def _current_revision(article_id: str, revision_no: int):
    DraftArticle = apps.get_model("editorial", "DraftArticle")
    ArticleRevision = apps.get_model("editorial", "ArticleRevision")
    try:
        article = DraftArticle.objects.select_for_update().get(id=article_id)
    except DraftArticle.DoesNotExist as exc:
        raise NotFound("글을 찾을 수 없습니다.") from exc
    if not article.current_revision_id:
        raise Conflict("현재 개정이 없습니다.")
    revision = ArticleRevision.objects.get(id=article.current_revision_id)
    if revision.revision_no != revision_no:
        raise Conflict("현재 개정 번호가 바뀌었습니다.")
    if revision.quality_state != "passed":
        raise Conflict("차단 품질 검사를 모두 통과해야 발행할 수 있습니다.")
    return article, revision


@transaction.atomic
def create_publication_intent(
    article_id: str,
    data: dict[str, Any],
    *,
    user,
    audit_context: AuditContext,
) -> PublicationIntent:
    if audit_context.actor_type not in {"admin", "worker"}:
        raise Forbidden("admin or worker audit provenance is required")
    if audit_context.actor_type == "admin" and audit_context.actor_id != user.pk:
        raise Forbidden("intent audit actor differs from the administrator")
    if audit_context.actor_type == "admin" and (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden("intent provenance differs from the audit context")
    request_key = data["requestKey"]
    existing = (
        PublicationIntent.objects.select_related("article_revision")
        .filter(article_id=article_id, request_key=request_key)
        .order_by("created_at")
        .first()
    )
    if existing:
        existing_refs = _target_ref_map(data["targetSnapshots"])
        existing_commands = _command_map(data["targetCommands"])
        candidate_data = {
            **data,
            "revisionContentHash": existing.revision_content_hash,
            "generationAttemptId": _id(existing.generation_attempt_id),
            "inputEvidenceManifestHash": existing.input_evidence_manifest_hash,
            "generationPipelineManifestHash": existing.generation_pipeline_manifest_hash,
            "qualityGateManifestHash": existing.quality_gate_manifest_hash,
            "qualityReportHash": existing.quality_report_hash,
        }
        candidate_hash = _intent_hash(
            candidate_data,
            existing.article_revision,
            existing_refs,
            existing_commands,
        )
        if (
            existing.revision_no != int(data["revisionNo"])
            or existing.revision_content_hash != data["expectedRevisionContentHash"]
            or existing.intent_hash != candidate_hash
        ):
            raise Conflict("같은 request key가 다른 발행 의도에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="publication_intent.created",
            entity=existing,
            identity_key=f"publication-intent:{existing.id}",
            request_hash=existing.intent_hash,
        )
        return existing
    article, revision = _current_revision(article_id, int(data["revisionNo"]))
    revision_content_hash = sha256_hex(
        {"title": revision.title, "summary": revision.summary, "bodyMarkdown": revision.body_markdown}
    )
    if revision_content_hash != data["expectedRevisionContentHash"]:
        raise Conflict("현재 개정 본문 hash가 요청 시점과 다릅니다.")
    generation_pipeline_manifest_hash = sha256_hex(
        {
            "generator": getattr(revision.generation_attempt, "generator_name", "admin_edit"),
            "version": getattr(revision.generation_attempt, "generator_version", "v1"),
        }
    )
    quality_rows = list(
        revision.quality_checks.order_by("code").values("code", "state", "detail")
    )
    material_data = {
        **data,
        "revisionContentHash": revision_content_hash,
        "generationAttemptId": _id(revision.generation_attempt_id),
        "inputEvidenceManifestHash": revision.input_manifest_hash,
        "generationPipelineManifestHash": generation_pipeline_manifest_hash,
        "qualityGateManifestHash": revision.quality_manifest_hash,
        "qualityReportHash": sha256_hex(quality_rows),
    }
    target_refs = _target_ref_map(data["targetSnapshots"])
    commands = _command_map(data["targetCommands"])
    if set(target_refs) != set(commands):
        raise InvalidInput("target snapshot과 command target 집합이 같아야 합니다.")
    latest = PublicationIntent.objects.filter(article_id=article.id).order_by("-created_at").first()
    if _id(latest.id if latest else None) != _id(data.get("expectedLatestIntentId")):
        raise Conflict("발행 의도가 갱신되었습니다. 다시 불러오세요.")
    latest_before_material = _audit_state(latest) if latest else None
    target_rows = {
        str(row.id): row
        for row in PublicationTarget.objects.select_for_update()
        .filter(id__in=target_refs.keys())
        .order_by("id")
    }
    if set(target_rows) != set(target_refs):
        raise InvalidInput("알 수 없는 발행 target이 포함되었습니다.")
    wordpress_ids = [key for key, row in target_rows.items() if row.channel == ChannelCode.WORDPRESS]
    for target_id, target in target_rows.items():
        ref = target_refs[target_id]
        command = commands[target_id]
        if str(target.current_snapshot_id) != ref["targetSnapshotId"]:
            raise Conflict(f"{target.display_name} snapshot이 바뀌었습니다.")
        if target.current_config_hash != ref["targetConfigHash"]:
            raise Conflict(f"{target.display_name} config가 바뀌었습니다.")
        if command["targetSnapshotId"] != ref["targetSnapshotId"] or command["targetConfigHash"] != ref["targetConfigHash"]:
            raise InvalidInput("target command와 snapshot ref가 다릅니다.")
        if command["resolvedAction"] not in target.capabilities or not target.capabilities[command["resolvedAction"]]:
            raise InvalidInput(f"{target.display_name}은 요청한 동작을 지원하지 않습니다.")
        if target.channel == ChannelCode.BLOGGER and command["resolvedAction"] != PublicationAction.UNPUBLISH:
            dependency = command["canonicalDependencyTargetId"]
            if dependency not in wordpress_ids:
                prior_wordpress = Publication.objects.filter(
                    article_id=article.id,
                    target__channel=ChannelCode.WORDPRESS,
                    state=Publication.State.PUBLISHED,
                    canonical_ready_at__isnull=False,
                ).exists()
                if not prior_wordpress:
                    raise InvalidInput("Blogger 발행에는 대표 WordPress target이 필요합니다.")
    mode = data["approvalMode"]
    validation_refs = _normalized_validation_refs(data.get("autoPublishValidationRefs", []))
    activation_refs = sorted(
        [
            {
                "targetId": str(ref["targetId"]),
                "targetSnapshotId": str(ref["targetSnapshotId"]),
                "activationId": str(ref["activationId"]),
                "version": int(ref["version"]),
                "activationHash": ref["activationHash"],
            }
            for ref in data.get("autoPublishActivationRefs", [])
        ],
        key=lambda row: row["targetId"],
    )
    if mode == ApprovalMode.MANUAL and (validation_refs or activation_refs):
        raise InvalidInput("manual 발행 의도에는 자동발행 참조를 포함할 수 없습니다.")
    if mode == ApprovalMode.VALIDATED_AUTO:
        if {ref["targetId"] for ref in validation_refs} != set(target_refs):
            raise InvalidInput("validation target 집합이 발행 target과 같아야 합니다.")
        if {ref["targetId"] for ref in activation_refs} != set(target_refs):
            raise InvalidInput("activation target 집합이 발행 target과 같아야 합니다.")
        for ref in activation_refs:
            target = target_rows[ref["targetId"]]
            if not target.auto_publish_enabled or str(target.latest_auto_publish_activation_id) != ref["activationId"]:
                raise Conflict("현재 enabled activation과 발행 의도가 다릅니다.")
            activation = AutoPublishActivation.objects.get(id=ref["activationId"], target=target)
            if activation.activation_hash != ref["activationHash"] or activation.version != ref["version"]:
                raise Conflict("activation material이 다릅니다.")
    normalized_refs = sorted(target_refs.values(), key=lambda row: row["targetId"])
    normalized_commands = sorted(commands.values(), key=lambda row: row["targetId"])
    intent_hash = _intent_hash(material_data, revision, target_refs, commands)
    intent = PublicationIntent.objects.create(
        article_id=article.id,
        article_revision_id=revision.id,
        revision_no=revision.revision_no,
        revision_content_hash=material_data["revisionContentHash"],
        correction_case_id=data.get("correctionCaseId"),
        origin_collection_run_id=getattr(article, "source_run_id", None),
        target_snapshot_refs=normalized_refs,
        target_commands=normalized_commands,
        target_snapshot_manifest_hash=sha256_hex(normalized_refs),
        approval_mode=mode,
        auto_publish_validation_refs=validation_refs,
        auto_validation_manifest_hash=sha256_hex(validation_refs) if validation_refs else None,
        auto_publish_activation_refs=activation_refs,
        auto_activation_manifest_hash=sha256_hex(activation_refs) if activation_refs else None,
        generation_attempt_id=material_data["generationAttemptId"],
        input_evidence_manifest_hash=material_data["inputEvidenceManifestHash"],
        generation_pipeline_manifest_hash=material_data["generationPipelineManifestHash"],
        quality_gate_manifest_hash=material_data["qualityGateManifestHash"],
        quality_report_hash=material_data["qualityReportHash"],
        supersedes_intent_id=latest.id if latest else None,
        intent_hash=intent_hash,
        request_key=request_key,
        state=PublicationIntent.State.AWAITING_APPROVAL,
        created_by=user,
    )
    if latest and latest.state not in {PublicationIntent.State.DISPATCHED, PublicationIntent.State.CANCELLED}:
        latest.state = PublicationIntent.State.STALE
        latest.save(update_fields=["state"])
    for target_id in sorted(target_rows):
        _create_preview_render(intent, revision, target_rows[target_id])
    render_manifest = list(
        intent.renders.order_by("target_id", "id").values(
            "id",
            "target_id",
            "template_hash",
            "content_hash",
            "source_manifest_hash",
        )
    )
    render_manifest_hash = sha256_hex(
        [
            {
                **row,
                "id": str(row["id"]),
                "target_id": str(row["target_id"]),
            }
            for row in render_manifest
        ]
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_intent.created",
        entity=intent,
        identity_key=f"publication-intent:{intent.id}",
        before_material={
            "intent": None,
            "supersededIntent": latest_before_material,
            "renderCount": 0,
            "renderManifestHash": sha256_hex([]),
        },
        after_material={
            "intent": _audit_state(intent),
            "supersededIntent": (
                _audit_state(latest) if latest else None
            ),
            "renderCount": len(render_manifest),
            "renderManifestHash": render_manifest_hash,
        },
        metadata={
            "intent_id": str(intent.id),
            "revision_id": str(intent.article_revision_id),
            "revision_no": intent.revision_no,
            "intent_hash": intent.intent_hash,
            "request_hash": intent.intent_hash,
            "state": intent.state,
            "count": len(target_rows),
        },
    )
    return intent


def _intent_hash(data, revision, target_refs, commands) -> str:
    return sha256_hex(
        {
            "articleRevisionId": str(revision.id),
            "revisionNo": revision.revision_no,
            "revisionContentHash": data["revisionContentHash"],
            "targetSnapshots": sorted(target_refs.values(), key=lambda row: row["targetId"]),
            "targetCommands": sorted(commands.values(), key=lambda row: row["targetId"]),
            "approvalMode": data["approvalMode"],
            "validationRefs": _normalized_validation_refs(data.get("autoPublishValidationRefs", [])),
            "activationRefs": sorted(data.get("autoPublishActivationRefs", []), key=lambda row: str(row["targetId"])),
            "inputEvidenceManifestHash": data["inputEvidenceManifestHash"],
            "generationPipelineManifestHash": data.get("generationPipelineManifestHash"),
            "qualityGateManifestHash": data["qualityGateManifestHash"],
            "qualityReportHash": data["qualityReportHash"],
            "correctionCaseId": data.get("correctionCaseId"),
        }
    )


def _markdown_to_html(markdown: str) -> str:
    blocks: list[str] = []
    in_list = False
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            continue
        if line.startswith("### "):
            blocks.append(f"<h3>{_render_markdown_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            blocks.append(f"<h2>{_render_markdown_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            blocks.append(f"<h1>{_render_markdown_inline(line[2:])}</h1>")
        elif line.startswith("- "):
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{_render_markdown_inline(line[2:])}</li>")
        else:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            blocks.append(f"<p>{_render_markdown_inline(line)}</p>")
    if in_list:
        blocks.append("</ul>")
    return "\n".join(blocks)


_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")


def _render_markdown_inline(value: str) -> str:
    """Render only HTTPS/HTTP Markdown links; all other input remains escaped text."""
    rendered: list[str] = []
    cursor = 0
    for match in _MARKDOWN_LINK.finditer(value):
        rendered.append(html.escape(value[cursor : match.start()]))
        label, url = match.groups()
        rendered.append(
            '<a href="{}" rel="noopener noreferrer">{}</a>'.format(
                html.escape(url, quote=True),
                html.escape(label),
            )
        )
        cursor = match.end()
    rendered.append(html.escape(value[cursor:]))
    return "".join(rendered)


def _revision_source_links(revision) -> list[str]:
    values = revision.claims.values_list(
        "evidence_links__evidence__source_item__canonical_url",
        flat=True,
    )
    return sorted({str(value) for value in values if value})


def _create_preview_render(intent, revision, target) -> ArticleChannelRender:
    body = _markdown_to_html(revision.body_markdown)
    source_links = _revision_source_links(revision)
    if revision.claims.exists() and not source_links:
        raise Conflict("게시 주장에 독자가 접근할 수 있는 원출처 URL이 없습니다.")
    canonical_state = ArticleChannelRender.CanonicalState.NOT_APPLICABLE
    if target.channel == ChannelCode.BLOGGER:
        canonical_state = ArticleChannelRender.CanonicalState.PENDING
        body += '\n<p class="canonical-source">원문: {{CANONICAL_WORDPRESS_URL}}</p>'
    template_material = {
        "channel": target.channel,
        "title": revision.title,
        "body": body,
        "revision": str(revision.id),
    }
    template_hash = sha256_hex(template_material)
    source_manifest_hash = sha256_hex(
        {"inputEvidenceManifestHash": intent.input_evidence_manifest_hash, "sourceLinks": source_links}
    )
    return ArticleChannelRender.objects.create(
        publication_intent=intent,
        article_revision_id=revision.id,
        target=target,
        target_snapshot_id=target.current_snapshot_id,
        target_config_hash=target.current_config_hash,
        channel_role=target.role,
        render_stage=ArticleChannelRender.Stage.PREVIEW,
        title=revision.title,
        body_html=body,
        labels=["반도체" if getattr(revision.article, "topic_code", "") == "semiconductor_news" else "청약정보"],
        source_links=source_links,
        included_claim_ids=[str(value) for value in revision.claims.values_list("id", flat=True)],
        canonical_link_state=canonical_state,
        template_hash=template_hash,
        content_hash=sha256_hex({"title": revision.title, "body": body}),
        source_manifest_hash=source_manifest_hash,
    )


@transaction.atomic
def decide_approval(
    article_id: str,
    target_id: str,
    data: dict[str, Any],
    *,
    user,
    audit_context: AuditContext,
    request=None,
) -> tuple[Approval, bool]:
    if audit_context.actor_type not in {"admin", "worker"}:
        raise Forbidden("admin or worker audit provenance is required")
    if audit_context.actor_type == "admin" and audit_context.actor_id != user.pk:
        raise Forbidden("approval audit actor differs from the administrator")
    if audit_context.actor_type == "admin" and (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden("approval provenance differs from the audit context")
    target = PublicationTarget.objects.select_for_update().get(
        id=target_id
    )
    intent = PublicationIntent.objects.select_for_update().get(
        id=data["publicationIntentId"], article_id=article_id
    )
    request_hash = _request_hash(data)
    existing = Approval.objects.filter(
        publication_intent=intent,
        target_id=target_id,
        request_key=data["requestKey"],
    ).first()
    if existing:
        if (
            existing.request_hash != request_hash
            or existing.admin_id != user.pk
        ):
            raise Conflict("같은 request key가 다른 승인 대상에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="publication_approval.decided",
            entity=existing,
            identity_key=f"publication-approval:{existing.id}",
            request_hash=request_hash,
        )
        return existing, False
    latest_intent = PublicationIntent.objects.filter(article_id=article_id).order_by("-created_at").first()
    if not latest_intent or latest_intent.id != intent.id or intent.state == PublicationIntent.State.STALE:
        raise Conflict("current 발행 의도만 승인할 수 있습니다.")
    intent_before_material = _audit_state(intent)
    command = next(
        (row for row in intent.target_commands if str(row["targetId"]) == str(target_id)), None
    )
    if not command:
        raise InvalidInput("발행 의도에 해당 target command가 없습니다.")
    if str(target.current_snapshot_id) != str(command["targetSnapshotId"]):
        raise Conflict("target snapshot이 변경되어 새 미리보기가 필요합니다.")
    latest = Approval.objects.filter(publication_intent=intent, target=target).order_by("-decided_at").first()
    if _id(latest.id if latest else None) != _id(data.get("expectedLatestApprovalId")):
        raise Conflict("승인 상태가 갱신되었습니다. 다시 불러오세요.")
    subject = data["actionSubject"]
    action = command["resolvedAction"]
    render = None
    render_template_hash = None
    if action == PublicationAction.UNPUBLISH:
        if subject.get("kind") != "unpublish_command" or subject.get("action") != action:
            raise InvalidInput("철회 승인 대상 형식이 올바르지 않습니다.")
        if not data.get("reauthProofId"):
            raise Forbidden("철회에는 최근 재인증이 필요합니다.")
        publication = Publication.objects.filter(article_id=article_id, target=target).first()
        if not publication or publication.remote_post_id != subject.get("remotePostId"):
            raise Conflict("현재 원격 게시물과 철회 승인 대상이 다릅니다.")
        if request is None:
            raise Forbidden("철회에는 관리자 session 재인증이 필요합니다.")
        consume_reauthentication_proof(
            request=request,
            proof_id=data["reauthProofId"],
            action_scope="unpublish",
            entity_type="publication",
            entity_id=publication.id,
        )
        source_manifest_hash = subject["correctionEvidenceManifestHash"]
    else:
        if subject.get("kind") != "content_preview" or subject.get("action") != action:
            raise InvalidInput("콘텐츠 승인 대상 형식이 올바르지 않습니다.")
        render = ArticleChannelRender.objects.get(
            id=subject["renderId"],
            publication_intent=intent,
            target=target,
            render_stage=ArticleChannelRender.Stage.PREVIEW,
        )
        if render.template_hash != subject["templateHash"]:
            raise Conflict("미리보기 template hash가 다릅니다.")
        if render.source_manifest_hash != subject["sourceManifestHash"]:
            raise Conflict("미리보기 source manifest가 다릅니다.")
        render_template_hash = render.template_hash
        source_manifest_hash = render.source_manifest_hash
    approval_subject_hash = sha256_hex(
        {
            "intentId": str(intent.id),
            "articleRevisionId": str(intent.article_revision_id),
            "targetId": str(target.id),
            "targetAction": action,
            "targetSnapshotId": str(target.current_snapshot_id),
            "targetConfigHash": target.current_config_hash,
            "subject": subject,
            "qualityReportHash": intent.quality_report_hash,
        }
    )
    approval = Approval.objects.create(
        article_revision_id=intent.article_revision_id,
        revision_no=intent.revision_no,
        publication_intent=intent,
        target=target,
        target_action=action,
        article_channel_render=render,
        action_subject=subject,
        target_snapshot_id=target.current_snapshot_id,
        target_config_hash=target.current_config_hash,
        mode=intent.approval_mode,
        decision=data["decision"],
        approval_subject_hash=approval_subject_hash,
        supersedes_approval_id=latest.id if latest else None,
        request_key=data["requestKey"],
        request_hash=request_hash,
        reauth_proof_id=data.get("reauthProofId"),
        policy_snapshot_hash=intent.quality_gate_manifest_hash,
        quality_report_hash=intent.quality_report_hash,
        render_template_hash=render_template_hash,
        source_manifest_hash=source_manifest_hash,
        admin=user,
    )
    target_ids = {str(row["targetId"]) for row in intent.target_commands}
    approved_ids = set()
    for command_row in intent.target_commands:
        current = Approval.objects.filter(
            publication_intent=intent, target_id=command_row["targetId"]
        ).order_by("-decided_at").first()
        if current and current.decision == Approval.Decision.APPROVED:
            approved_ids.add(str(current.target_id))
    if target_ids.issubset(approved_ids):
        intent.state = PublicationIntent.State.APPROVED
        intent.save(update_fields=["state"])
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_approval.decided",
        entity=approval,
        identity_key=f"publication-approval:{approval.id}",
        before_material={
            "approval": None,
            "intent": intent_before_material,
        },
        after_material={
            "approval": _audit_state(
                approval,
                decision=approval.decision,
                approvalSubjectHash=approval.approval_subject_hash,
            ),
            "intent": _audit_state(intent),
        },
        metadata={
            "approval_hash": approval.approval_subject_hash,
            "decision": approval.decision,
            "decision_id": str(approval.id),
            "intent_id": str(intent.id),
            "request_hash": request_hash,
            "target_id": str(target.id),
        },
    )
    return approval, True


def _publication_for(article_id: str, target: PublicationTarget) -> Publication:
    publication = Publication.objects.filter(article_id=article_id, target=target).first()
    if publication:
        return publication
    publication = Publication(
        article_id=article_id,
        target=target,
        origin_target_snapshot_id=target.current_snapshot_id,
        remote_lookup_key="pending",
    )
    publication.remote_lookup_key = f"ww-{publication.id.hex}"
    publication.save()
    return publication


@transaction.atomic
def dispatch_publication(
    article_id: str,
    data: dict[str, Any],
    *,
    audit_context: AuditContext,
) -> list[PublicationAttempt]:
    if audit_context.actor_type not in {"admin", "worker"}:
        raise Forbidden("admin or worker audit provenance is required")
    if audit_context.actor_type == "admin" and (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden("dispatch provenance differs from the audit context")
    expected = _target_ref_map(data["expectedTargetSnapshots"])
    requested_ids = [str(value) for value in data["targetIds"]]
    if len(requested_ids) != len(set(requested_ids)):
        raise InvalidInput("target IDs에는 중복이 없어야 합니다.")
    if set(expected) != set(requested_ids):
        raise InvalidInput("target IDs와 expected target snapshot 집합이 같아야 합니다.")
    target_rows = {
        str(row.id): row
        for row in PublicationTarget.objects.select_for_update()
        .filter(id__in=requested_ids)
        .order_by("id")
    }
    if set(target_rows) != set(requested_ids):
        raise InvalidInput("알 수 없는 발행 target이 포함되었습니다.")
    intent = PublicationIntent.objects.select_for_update().get(
        id=data["publicationIntentId"], article_id=article_id
    )
    if intent.revision_no != int(data["revisionNo"]):
        raise Conflict("발행 요청 revision과 intent가 다릅니다.")
    command_map = {str(row["targetId"]): row for row in intent.target_commands}
    if any(target_id not in command_map for target_id in requested_ids):
        raise InvalidInput("publication intent does not contain every requested target")
    dispatch_material = {
        "requestKey": data["requestKey"],
        "publishAt": data.get("publishAt"),
    }
    dispatch_request_hash = _request_hash(
        {
            "schemaVersion": "publication-dispatch-request-v1",
            "intentId": str(intent.id),
            "actorType": audit_context.actor_type,
            "actorId": _id(audit_context.actor_id),
            "provenance": audit_context.provenance_metadata(),
            "reason": audit_context.reason_code,
            "payload": data,
        }
    )
    idempotency_keys = {
        target_id: sha256_hex(
            {
                "intentId": str(intent.id),
                "targetId": target_id,
                "action": command_map[target_id]["resolvedAction"],
                "requestKey": data["requestKey"],
            }
        )
        for target_id in requested_ids
        if target_id in command_map
    }
    replay_rows = list(
        PublicationAttempt.objects.select_related("publication__target").filter(
            idempotency_key__in=idempotency_keys.values()
        )
    )
    if replay_rows:
        replay_by_target = {str(row.publication.target_id): row for row in replay_rows}
        if (
            set(replay_by_target) != set(requested_ids)
            or intent.state != PublicationIntent.State.DISPATCHED
        ):
            raise Conflict("발행 dispatch request가 부분 적용되었거나 다른 payload입니다.")
        for target_id, row in replay_by_target.items():
            ref = expected[target_id]
            command = command_map.get(target_id)
            if (
                command is None
                or row.publication_intent_id != intent.id
                or row.resolved_action != command["resolvedAction"]
                or str(row.target_snapshot_id) != ref["targetSnapshotId"]
                or row.target_config_hash != ref["targetConfigHash"]
                or row.request_fingerprint
                != sha256_hex(
                    {
                        "intentHash": intent.intent_hash,
                        "command": command,
                        "approvalSubjectHash": row.approval_subject_hash,
                        "dispatch": dispatch_material,
                    }
                )
            ):
                raise Conflict("같은 request key가 다른 dispatch payload에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="publication.dispatched",
            entity=intent,
            identity_key=_publication_dispatch_audit_identity(
                intent_id=intent.id,
                request_key=data["requestKey"],
            ),
            request_hash=dispatch_request_hash,
        )
        return [replay_by_target[target_id] for target_id in requested_ids]
    latest = PublicationIntent.objects.filter(article_id=article_id).order_by("-created_at").first()
    if not latest or latest.id != intent.id or intent.state != PublicationIntent.State.APPROVED:
        raise Conflict("current approved 발행 의도만 전송할 수 있습니다.")
    before_material = _audit_state(intent)
    attempts: list[PublicationAttempt] = []
    for target_id in requested_ids:
        target = target_rows.get(target_id)
        command = command_map.get(target_id)
        if not target or not command:
            raise InvalidInput("발행 의도에 없는 target입니다.")
        ref = expected[target_id]
        if (
            str(target.current_snapshot_id) != ref["targetSnapshotId"]
            or target.current_config_hash != ref["targetConfigHash"]
            or str(command["targetSnapshotId"]) != ref["targetSnapshotId"]
        ):
            raise Conflict("target snapshot이 변경되어 재승인이 필요합니다.")
        approval = Approval.objects.filter(
            publication_intent=intent,
            target=target,
            decision=Approval.Decision.APPROVED,
        ).order_by("-decided_at").first()
        if not approval or approval.approval_subject_hash == "":
            raise Conflict("target별 current action 승인이 필요합니다.")
        if approval.target_action != command["resolvedAction"]:
            raise Conflict("승인 action과 target command가 다릅니다.")
        publication = _publication_for(article_id, target)
        idempotency_key = idempotency_keys[target_id]
        existing = PublicationAttempt.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            attempts.append(existing)
            continue
        activation_ref = next(
            (row for row in intent.auto_publish_activation_refs if str(row["targetId"]) == target_id), None
        )
        attempt = PublicationAttempt.objects.create(
            publication=publication,
            article_revision_id=intent.article_revision_id,
            publication_intent=intent,
            target_snapshot_id=target.current_snapshot_id,
            target_config_hash=target.current_config_hash,
            resolved_action=command["resolvedAction"],
            target_command_hash=command["targetCommandHash"],
            publisher_contract_version=target.publisher_contract_version,
            publisher_adapter_manifest_hash=target.publisher_adapter_manifest_hash,
            approval=approval,
            approval_subject_hash=approval.approval_subject_hash,
            auto_publish_activation_id=(activation_ref or {}).get("activationId"),
            auto_publish_activation_hash=(activation_ref or {}).get("activationHash"),
            idempotency_key=idempotency_key,
            remote_lookup_key=publication.remote_lookup_key,
            request_fingerprint=sha256_hex(
                {
                    "intentHash": intent.intent_hash,
                    "command": command,
                    "approvalSubjectHash": approval.approval_subject_hash,
                    "dispatch": dispatch_material,
                }
            ),
        )
        attempts.append(attempt)
    intent.state = PublicationIntent.State.DISPATCHED
    intent.save(update_fields=["state"])
    publish_at = data.get("publishAt")
    wordpress = [row for row in attempts if row.publication.target.channel == ChannelCode.WORDPRESS]
    blogger = [row for row in attempts if row.publication.target.channel == ChannelCode.BLOGGER]
    initial = wordpress or [row for row in blogger if _wordpress_dependency_ready(row)]
    for attempt in initial:
        _queue_attempt_on_commit(attempt, publish_at=publish_at)
    attempts_manifest = [
        {
            "attemptId": str(row.id),
            "publicationId": str(row.publication_id),
            "targetId": str(row.publication.target_id),
            "state": row.state,
            "attemptNo": row.attempt_no,
            "resolvedAction": row.resolved_action,
        }
        for row in sorted(attempts, key=lambda item: str(item.id))
    ]
    attempts_hash = sha256_hex(attempts_manifest)
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication.dispatched",
        entity=intent,
        identity_key=_publication_dispatch_audit_identity(
            intent_id=intent.id,
            request_key=data["requestKey"],
        ),
        before_material={
            "intent": before_material,
            "attemptCount": 0,
            "attemptManifestHash": sha256_hex([]),
        },
        after_material={
            "intent": _audit_state(intent),
            "attemptCount": len(attempts_manifest),
            "attemptManifestHash": attempts_hash,
        },
        metadata={
            "intent_id": str(intent.id),
            "count": len(attempts),
            "request_hash": dispatch_request_hash,
            "result_hash": attempts_hash,
            "state": intent.state,
        },
    )
    return attempts


def _queue_attempt_on_commit(attempt: PublicationAttempt, *, publish_at: str | None = None) -> None:
    eta = None
    if publish_at:
        eta = timezone.datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
        if timezone.is_naive(eta):
            eta = timezone.make_aware(eta, dt_timezone.utc)
        attempt.publication.state = Publication.State.SCHEDULED
        attempt.publication.scheduled_for = eta
        attempt.publication.save(update_fields=["state", "scheduled_for", "updated_at"])
    _enqueue_event(
        "publication.requested",
        {
            "publication_attempt_id": str(attempt.id),
        },
        dedupe_key=f"publication.requested:{attempt.id}:{attempt.attempt_no}",
        aggregate_type="publication_attempt",
        aggregate_id=attempt.id,
        job_id=attempt.id,
        available_at=eta,
    )


def _wordpress_dependency_ready(attempt: PublicationAttempt) -> bool:
    return Publication.objects.filter(
        article_id=attempt.publication.article_id,
        target__channel=ChannelCode.WORDPRESS,
        state=Publication.State.PUBLISHED,
        canonical_ready_at__isnull=False,
    ).exists()


def _kill_switch_enabled() -> bool:
    from apps.scheduling.controls import is_external_write_blocked

    return is_external_write_blocked()


def _assert_external_writes_allowed() -> None:
    if _kill_switch_enabled():
        raise PublisherError("global_kill_switch_enabled", category="retryable")


def validate_attempt_gate(attempt: PublicationAttempt) -> None:
    if _kill_switch_enabled():
        raise Conflict("전역 kill switch가 활성화되어 외부 쓰기가 차단되었습니다.")
    intent = attempt.publication_intent
    latest = PublicationIntent.objects.filter(article_id=intent.article_id).order_by("-created_at").first()
    if not latest or latest.id != intent.id or intent.state not in {
        PublicationIntent.State.APPROVED,
        PublicationIntent.State.DISPATCHED,
    }:
        raise Conflict("publication attempt가 current intent에 속하지 않습니다.")
    target = attempt.publication.target
    if (
        target.current_snapshot_id != attempt.target_snapshot_id
        or target.current_config_hash != attempt.target_config_hash
        or target.publisher_adapter_manifest_hash != attempt.publisher_adapter_manifest_hash
        or ADAPTER_MANIFESTS[target.channel] != attempt.publisher_adapter_manifest_hash
    ):
        raise Conflict("target 또는 publisher adapter snapshot이 변경되었습니다.")
    approval = attempt.approval
    if (
        approval.decision != Approval.Decision.APPROVED
        or approval.approval_subject_hash != attempt.approval_subject_hash
        or approval.target_action != attempt.resolved_action
    ):
        raise Conflict("현재 action-specific 승인과 attempt가 다릅니다.")
    if intent.approval_mode == ApprovalMode.VALIDATED_AUTO:
        if not target.auto_publish_enabled:
            raise Conflict("자동발행이 비활성화되었습니다.")
        if target.latest_auto_publish_activation_id != attempt.auto_publish_activation_id:
            raise Conflict("current 자동발행 activation과 attempt가 다릅니다.")
        activation = AutoPublishActivation.objects.get(id=attempt.auto_publish_activation_id)
        if activation.activation_hash != attempt.auto_publish_activation_hash:
            raise Conflict("자동발행 activation hash가 다릅니다.")
        validation_ids = [row["validationId"] for row in activation.validation_refs]
        if AutoPublishValidation.objects.filter(
            id__in=validation_ids, status=AutoPublishValidation.State.PASSED
        ).count() != len(validation_ids):
            raise Conflict("자동발행 validation이 stale 또는 revoked 상태입니다.")
    if target.connection_state != PublicationTarget.ConnectionState.VERIFIED:
        raise Conflict("검증된 target 연결만 발행할 수 있습니다.")
    if target.environment == TargetEnvironment.PRODUCTION and intent.approval_mode == ApprovalMode.VALIDATED_AUTO:
        if (
            not target.canary_target_id
            or target.canary_target.canary_state != ValidationState.PASSED
            or target.pilot_state != ValidationState.PASSED
        ):
            raise Conflict("현재 test canary와 운영 파일럿 게이트가 유효하지 않습니다.")
    if target.channel == ChannelCode.BLOGGER and attempt.resolved_action != PublicationAction.UNPUBLISH:
        if not _wordpress_dependency_ready(attempt):
            raise Conflict("WordPress 대표 원문의 공개 확인을 기다리고 있습니다.")


def _final_render(attempt: PublicationAttempt) -> ArticleChannelRender | None:
    if attempt.resolved_action == PublicationAction.UNPUBLISH:
        return None
    approval_render = attempt.approval.article_channel_render
    if not approval_render:
        raise Conflict("콘텐츠 action에는 승인된 preview render가 필요합니다.")
    existing = ArticleChannelRender.objects.filter(
        publication_intent=attempt.publication_intent,
        target=attempt.publication.target,
        render_stage=ArticleChannelRender.Stage.FINAL,
    ).first()
    if existing:
        if existing.template_hash != approval_render.template_hash:
            raise Conflict("final render template가 승인된 preview와 다릅니다.")
        return existing
    target = attempt.publication.target
    body = approval_render.body_html
    canonical_url = None
    canonical_state = ArticleChannelRender.CanonicalState.NOT_APPLICABLE
    if target.channel == ChannelCode.BLOGGER:
        wordpress = Publication.objects.filter(
            article_id=attempt.publication.article_id,
            target__channel=ChannelCode.WORDPRESS,
            state=Publication.State.PUBLISHED,
            canonical_ready_at__isnull=False,
        ).order_by("-last_success_at").first()
        if not wordpress or not wordpress.remote_url:
            raise Conflict("검증된 WordPress 대표 URL이 없습니다.")
        canonical_url = wordpress.remote_url
        canonical_state = ArticleChannelRender.CanonicalState.RESOLVED
        body = body.replace("{{CANONICAL_WORDPRESS_URL}}", html.escape(canonical_url, quote=True))
    return ArticleChannelRender.objects.create(
        publication_intent=attempt.publication_intent,
        article_revision_id=approval_render.article_revision_id,
        target=target,
        target_snapshot=approval_render.target_snapshot,
        target_config_hash=approval_render.target_config_hash,
        channel_role=approval_render.channel_role,
        render_stage=ArticleChannelRender.Stage.FINAL,
        title=approval_render.title,
        body_html=body,
        labels=approval_render.labels,
        source_links=approval_render.source_links,
        included_claim_ids=approval_render.included_claim_ids,
        canonical_source_url=canonical_url,
        canonical_link_state=canonical_state,
        template_hash=approval_render.template_hash,
        content_hash=sha256_hex({"title": approval_render.title, "body": body}),
        source_manifest_hash=approval_render.source_manifest_hash,
        media_manifest=approval_render.media_manifest,
        correction_history=approval_render.correction_history,
    )


def _rendered_article(render: ArticleChannelRender) -> RenderedArticle:
    media = tuple(
        RenderedMedia(
            asset_id=str(row["assetId"]),
            delivery_kind=row["deliveryKind"],
            delivery_id=str(row["deliveryId"]),
            delivery_url=row["deliveryUrl"],
            mime_type=row["mimeType"],
            checksum=row["checksum"],
            alt_text=row["altText"],
            caption=row.get("caption", ""),
            attribution=row.get("attribution", ""),
            rights_status=row["rightsStatus"],
        )
        for row in render.media_manifest
    )
    return RenderedArticle(
        article_id=str(render.publication_intent.article_id),
        revision_no=render.publication_intent.revision_no,
        channel_role=render.channel_role,
        render_stage=render.render_stage,
        title=render.title,
        body_html=render.body_html,
        labels=tuple(render.labels),
        source_links=tuple(render.source_links),
        included_claim_ids=tuple(str(value) for value in render.included_claim_ids),
        canonical_source_url=render.canonical_source_url,
        canonical_link_state=render.canonical_link_state,
        template_hash=render.template_hash,
        media=media,
        correction_history=tuple(render.correction_history),
        content_hash=render.content_hash,
        source_manifest_hash=render.source_manifest_hash,
    )


def _command_for_attempt(attempt: PublicationAttempt, render: ArticleChannelRender | None) -> PublishCommand:
    return PublishCommand(
        publication_attempt_id=str(attempt.id),
        action=attempt.resolved_action,
        target_command_hash=attempt.target_command_hash,
        idempotency_key=attempt.idempotency_key,
        remote_lookup_key=attempt.remote_lookup_key,
        target_id=str(attempt.publication.target_id),
        publication_intent_id=str(attempt.publication_intent_id),
        approval_id=str(attempt.approval_id),
        approval_subject_hash=attempt.approval_subject_hash,
        target_snapshot_id=str(attempt.target_snapshot_id),
        target_config_hash=attempt.target_config_hash,
        publisher_contract_version=attempt.publisher_contract_version,
        publisher_adapter_manifest_hash=attempt.publisher_adapter_manifest_hash,
        auto_publish_activation_id=_id(attempt.auto_publish_activation_id),
        auto_publish_activation_hash=attempt.auto_publish_activation_hash,
        remote_post_id=attempt.publication.remote_post_id,
        rendered_article=_rendered_article(render) if render else None,
        publish_at=attempt.publication.scheduled_for,
        requested_at=timezone.now(),
        correlation_id=str(attempt.publication_intent.origin_collection_run_id or attempt.publication_intent_id),
    )


def _project_reconciling_locked(
    attempt: PublicationAttempt,
    *,
    update_counter: bool = False,
) -> None:
    terminal_states = {
        PublicationAttempt.State.SUCCEEDED,
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
        PublicationAttempt.State.STALE,
    }
    if attempt.state in terminal_states:
        return
    attempt.state = PublicationAttempt.State.RECONCILING
    attempt_fields = ["state"]
    if update_counter:
        attempt_fields.append("reconcile_attempt_no")
    attempt.save(update_fields=attempt_fields)
    publication = attempt.publication
    publication.state = Publication.State.RECONCILING
    publication.save(update_fields=("state", "updated_at"))


def _manualize_reconcile_attempt_locked(
    attempt: PublicationAttempt,
    *,
    error_code: str,
    now=None,
) -> str:
    now = now or timezone.now()
    preserved_terminal_states = {
        PublicationAttempt.State.SUCCEEDED,
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
        PublicationAttempt.State.STALE,
    }
    if attempt.state in preserved_terminal_states:
        return attempt.state
    attempt.state = PublicationAttempt.State.MANUAL_REQUIRED
    attempt.error_code = error_code[:100]
    attempt.finished_at = now
    attempt.save(
        update_fields=(
            "state",
            "error_code",
            "finished_at",
        )
    )
    attempt.publication.state = Publication.State.MANUAL_REQUIRED
    attempt.publication.last_error_code = attempt.error_code
    attempt.publication.save(
        update_fields=(
            "state",
            "last_error_code",
            "updated_at",
        )
    )
    return attempt.state


def _terminalize_reconcile_generation_locked(
    attempt: PublicationAttempt,
    generation: PublicationReconcileGeneration,
    *,
    error_code: str,
) -> None:
    if generation.state == PublicationReconcileGeneration.State.COMPLETED:
        return
    now = timezone.now()
    result_state = _manualize_reconcile_attempt_locked(
        attempt,
        error_code=error_code,
        now=now,
    )
    generation.state = PublicationReconcileGeneration.State.COMPLETED
    generation.result_identity = sha256_hex(
        {
            "kind": "reconcile_delivery_terminal",
            "publication_attempt_id": str(attempt.id),
            "generation": generation.generation,
            "source_event_id": str(generation.source_event_id),
            "result_state": result_state,
            "error_code": error_code[:100],
        }
    )
    generation.result_state = result_state
    generation.completed_at = now
    generation.save(
        update_fields=(
            "state",
            "result_identity",
            "result_state",
            "completed_at",
        )
    )


def _reconcile_generation_delivery_dead_lettered(
    generation: PublicationReconcileGeneration,
) -> bool:
    source_event = generation.source_event
    return (
        source_event.status == "dead_letter"
        or source_event.consumer_receipts.filter(
            consumer_name="publication-reconcile",
            state="dead_letter",
        ).exists()
    )


def _enqueue_reconcile_locked(
    attempt: PublicationAttempt,
    *,
    available_at=None,
) -> PublicationReconcileGeneration | None:
    if attempt.reconcile_attempt_no:
        current = (
            PublicationReconcileGeneration.objects.select_related(
                "source_event"
            ).filter(
                publication_attempt=attempt,
                generation=attempt.reconcile_attempt_no,
            )
            .order_by("generation")
            .first()
        )
        if (
            current is not None
            and current.state == PublicationReconcileGeneration.State.STARTED
        ):
            if _reconcile_generation_delivery_dead_lettered(current):
                _terminalize_reconcile_generation_locked(
                    attempt,
                    current,
                    error_code=(
                        current.source_event.last_error_code
                        or "reconcile_delivery_dead_letter"
                    ),
                )
            else:
                _project_reconciling_locked(attempt)
            return current
    if attempt.state in {
        PublicationAttempt.State.SUCCEEDED,
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
        PublicationAttempt.State.STALE,
    }:
        return None
    reconcile_attempt_no = attempt.reconcile_attempt_no + 1
    if reconcile_attempt_no > 5:
        return None
    dedupe_key = (
        f"publication.reconcile_requested:{attempt.id}:{reconcile_attempt_no}"
    )
    event = _enqueue_event(
        "publication.reconcile_requested",
        {
            "publication_attempt_id": str(attempt.id),
            "reconcile_attempt_no": reconcile_attempt_no,
        },
        event_version=2,
        dedupe_key=dedupe_key,
        aggregate_type="publication_attempt",
        aggregate_id=attempt.id,
        job_id=attempt.id,
        available_at=available_at,
    )
    generation, created = PublicationReconcileGeneration.objects.get_or_create(
        publication_attempt=attempt,
        generation=reconcile_attempt_no,
        defaults={
            "source_event": event,
            "state": PublicationReconcileGeneration.State.STARTED,
            "not_before": event.not_before,
            "started_at": event.occurred_at,
        },
    )
    if not created and generation.source_event_id != event.id:
        raise Conflict("reconcile generation is bound to a different event")
    attempt.reconcile_attempt_no = reconcile_attempt_no
    _project_reconciling_locked(attempt, update_counter=True)
    return generation


@transaction.atomic
def begin_attempt(
    attempt_id: str,
    *,
    audit_context: AuditContext,
) -> tuple[PublicationAttempt, PublishCommand | None]:
    _require_audit_actor(audit_context, "worker")
    attempt = PublicationAttempt.objects.select_for_update().select_related(
        "publication__target", "publication_intent", "approval__article_channel_render"
    ).get(id=attempt_id)
    publication = attempt.publication
    _require_worker_event(
        audit_context,
        topic="publication.requested",
        aggregate_id=attempt.id,
        payload_identity={"publication_attempt_id": str(attempt.id)},
    )
    if attempt.state in {
        PublicationAttempt.State.SUCCEEDED,
        PublicationAttempt.State.PERMANENT_FAILED,
        PublicationAttempt.State.MANUAL_REQUIRED,
        PublicationAttempt.State.STALE,
    }:
        require_audit_replay(
            context=audit_context,
            action="publication_attempt.finished",
            entity=attempt,
            identity_key=(
                f"{audit_context.event_key}:attempt-result:"
                f"{attempt.attempt_no}"
            ),
            metadata_expected={"attempt": attempt.attempt_no},
        )
        return attempt, None
    if attempt.state == PublicationAttempt.State.RUNNING:
        if not _worker_audit_replay(
            audit_context,
            entity=attempt,
            candidates=(
                (
                    "publication_attempt.started",
                    (
                        f"{audit_context.event_key}:attempt-start:"
                        f"{attempt.attempt_no}"
                    ),
                    {"attempt": attempt.attempt_no},
                ),
            ),
        ):
            raise Conflict(
                "running publication attempt has no matching started audit"
            )
        before_material = {
            "attempt": _audit_state(attempt),
            "publication": _audit_state(publication),
        }
        attempt.state = PublicationAttempt.State.UNKNOWN_OUTCOME
        attempt.finished_at = timezone.now()
        attempt.error_code = "delivery_redelivered_after_begin"
        attempt.save(update_fields=["state", "finished_at", "error_code"])
        publication.state = Publication.State.RECONCILING
        publication.remote_state = Publication.RemoteState.UNKNOWN
        publication.last_error_code = attempt.error_code
        publication.save(
            update_fields=(
                "state",
                "remote_state",
                "last_error_code",
                "updated_at",
            )
        )
        _enqueue_reconcile_locked(attempt)
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_attempt.reconcile_started",
            entity=attempt,
            identity_key=(
                f"{audit_context.event_key}:delivery-redelivery:"
                f"{attempt.attempt_no}"
            ),
            before_material=before_material,
            after_material={
                "attempt": _audit_state(attempt),
                "publication": _audit_state(publication),
            },
            metadata={
                "publication_attempt_id": str(attempt.id),
                "attempt": attempt.attempt_no,
                "result": "unknown_outcome",
                "error_code": attempt.error_code,
                "state": attempt.state,
            },
        )
        return attempt, None
    if attempt.state in {
        PublicationAttempt.State.RECONCILING,
        PublicationAttempt.State.UNKNOWN_OUTCOME,
    }:
        if not _worker_audit_replay(
            audit_context,
            entity=attempt,
            candidates=(
                (
                    "publication_attempt.finished",
                    (
                        f"{audit_context.event_key}:attempt-result:"
                        f"{attempt.attempt_no}"
                    ),
                    {"attempt": attempt.attempt_no},
                ),
                (
                    "publication_attempt.reconcile_started",
                    (
                        f"{audit_context.event_key}:delivery-redelivery:"
                        f"{attempt.attempt_no}"
                    ),
                    {"attempt": attempt.attempt_no},
                ),
            ),
        ):
            raise Conflict(
                "reconciling publication attempt has no matching audit event"
            )
        _enqueue_reconcile_locked(attempt)
        return attempt, None
    if attempt.state not in {
        PublicationAttempt.State.QUEUED,
        PublicationAttempt.State.RETRYABLE_FAILED,
    }:
        raise Conflict("terminal publication attempt cannot be executed again")
    try:
        validate_attempt_gate(attempt)
    except Conflict:
        before_material = _audit_state(attempt)
        attempt.state = PublicationAttempt.State.STALE
        attempt.finished_at = timezone.now()
        attempt.error_code = "attempt_gate_stale"
        attempt.save(update_fields=["state", "finished_at", "error_code"])
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_attempt.finished",
            entity=attempt,
            identity_key=(
                f"{audit_context.event_key}:attempt-result:"
                f"{attempt.attempt_no}"
            ),
            before_material=before_material,
            after_material=_audit_state(attempt),
            metadata={
                "publication_attempt_id": str(attempt.id),
                "attempt": attempt.attempt_no,
                "result": "stale",
                "error_code": attempt.error_code,
                "state": attempt.state,
            },
        )
        return attempt, None
    render = _final_render(attempt)
    before_material = {
        "attempt": _audit_state(attempt),
        "publication": _audit_state(publication),
    }
    attempt.state = PublicationAttempt.State.RUNNING
    attempt.started_at = timezone.now()
    attempt.error_code = ""
    attempt.save(update_fields=["state", "started_at", "error_code"])
    publication.state = {
        PublicationAction.CREATE: Publication.State.IN_PROGRESS,
        PublicationAction.UPDATE: Publication.State.UPDATING,
        PublicationAction.MARK_WITHDRAWN: Publication.State.MARKING_WITHDRAWN,
        PublicationAction.UNPUBLISH: Publication.State.WITHDRAWING,
    }[attempt.resolved_action]
    publication.save(update_fields=["state", "updated_at"])
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_attempt.started",
        entity=attempt,
        identity_key=(
            f"{audit_context.event_key}:attempt-start:"
            f"{attempt.attempt_no}"
        ),
        before_material=before_material,
        after_material={
            "attempt": _audit_state(attempt),
            "publication": _audit_state(publication),
        },
        metadata={
            "publication_attempt_id": str(attempt.id),
            "attempt": attempt.attempt_no,
            "action": attempt.resolved_action,
            "channel": publication.target.channel,
            "intent_id": str(attempt.publication_intent_id),
            "state": attempt.state,
            "target_id": str(publication.target_id),
        },
    )
    return attempt, _command_for_attempt(attempt, render)


def _publish_result_identity(result) -> str:
    return sha256_hex(
        {
            "status": result.status,
            "remote_post_id": result.remote_post_id,
            "remote_url": result.remote_url,
            "remote_state": result.remote_state,
            "remote_revision": result.remote_revision,
            "scheduled_for": (
                result.scheduled_for.isoformat()
                if result.scheduled_for
                else None
            ),
            "published_at": (
                result.published_at.isoformat()
                if result.published_at
                else None
            ),
            "request_id": result.request_id,
            "reconcile_required": result.reconcile_required,
            "http_status": result.http_status,
            "error_code": result.error_code,
            "error_detail_redacted": result.error_detail_redacted,
        }
    )


@transaction.atomic
def persist_publish_result(
    attempt_id: str,
    result,
    *,
    audit_context: AuditContext,
    expected_reconcile_generation: int | None = None,
    expected_reconcile_event_id: uuid.UUID | str | None = None,
    retry_after_seconds: int | None = None,
) -> PublicationAttempt:
    _require_audit_actor(audit_context, "worker")
    attempt = PublicationAttempt.objects.select_for_update().select_related(
        "publication__target", "publication_intent"
    ).get(id=attempt_id)
    publication = attempt.publication
    target = publication.target
    before_material = {
        "attempt": _audit_state(attempt),
        "publication": _audit_state(publication),
        "target": _audit_state(target),
        "media": _publication_media_state_manifest(publication.id),
    }
    if (
        expected_reconcile_generation is None
    ) != (
        expected_reconcile_event_id is None
    ):
        raise Conflict("reconcile result fence is incomplete")
    expected_event_uuid = None
    if expected_reconcile_generation is None:
        _require_worker_event(
            audit_context,
            topic="publication.requested",
            aggregate_id=attempt.id,
            payload_identity={"publication_attempt_id": str(attempt.id)},
        )
    else:
        try:
            expected_event_uuid = uuid.UUID(
                str(expected_reconcile_event_id)
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise Conflict("reconcile result event fence is invalid") from exc
        if str(expected_event_uuid) != audit_context.event_key:
            raise Conflict(
                "reconcile result event fence differs from audit provenance"
            )
        _require_worker_event(
            audit_context,
            topic="publication.reconcile_requested",
            aggregate_id=attempt.id,
            payload_identity={
                "publication_attempt_id": str(attempt.id),
                "reconcile_attempt_no": str(
                    expected_reconcile_generation
                ),
            },
        )
    result_identity = _publish_result_identity(result)
    replay_action = (
        "publication_attempt.reconciled"
        if expected_reconcile_generation is not None
        else "publication_attempt.finished"
    )
    replay_candidates = (
        (
            replay_action,
            (
                f"{audit_context.event_key}:reconcile-result:"
                f"{expected_reconcile_generation}"
            ),
            {
                "reconcile_attempt_no": expected_reconcile_generation,
            },
        ),
    ) if expected_reconcile_generation is not None else tuple(
        (
            replay_action,
            f"{audit_context.event_key}:attempt-result:{attempt_no}",
            {"attempt": attempt_no},
        )
        for attempt_no in range(1, 6)
    )
    replay = _worker_audit_replay(
        audit_context,
        entity=attempt,
        candidates=replay_candidates,
    )
    if replay is not None:
        if replay.metadata_redacted.get("result_hash") != result_identity:
            raise Conflict(
                "worker event was replayed with a different publisher result"
            )
        return attempt
    execution_attempt_no = attempt.attempt_no
    reconcile_generation = None
    if expected_reconcile_generation is not None:
        reconcile_generation = (
            PublicationReconcileGeneration.objects.select_for_update()
            .filter(
                publication_attempt=attempt,
                generation=expected_reconcile_generation,
                source_event_id=expected_event_uuid,
            )
            .first()
        )
        if (
            reconcile_generation is None
            or reconcile_generation.state
            != PublicationReconcileGeneration.State.STARTED
            or attempt.reconcile_attempt_no
            != expected_reconcile_generation
            or attempt.state != PublicationAttempt.State.RECONCILING
        ):
            raise Conflict("stale reconcile result was fenced")
    elif attempt.state != PublicationAttempt.State.RUNNING:
        raise Conflict(
            "publication result can only finalize the active worker attempt"
        )
    now = timezone.now()
    attempt.http_status = result.http_status
    attempt.remote_request_id = result.request_id or ""
    attempt.finished_at = now
    attempt.error_code = result.error_code or ""
    attempt.error_detail_redacted = result.error_detail_redacted or ""
    if result.status == "succeeded":
        attempt.state = PublicationAttempt.State.SUCCEEDED
        publication.remote_post_id = result.remote_post_id or publication.remote_post_id
        publication.remote_url = result.remote_url or publication.remote_url
        publication.remote_state = result.remote_state
        publication.published_revision_no = attempt.publication_intent.revision_no
        publication.last_success_at = now
        publication.last_error_code = ""
        if attempt.resolved_action == PublicationAction.UNPUBLISH:
            publication.state = Publication.State.WITHDRAWN
        elif attempt.resolved_action == PublicationAction.MARK_WITHDRAWN:
            publication.state = Publication.State.MARKED_WITHDRAWN
            publication.published_at = result.published_at or publication.published_at
        else:
            publication.state = Publication.State.PUBLISHED
            publication.published_at = result.published_at or now
        if publication.target.channel == ChannelCode.WORDPRESS and publication.state == Publication.State.PUBLISHED:
            publication.canonical_ready_at = now
        if publication.target.channel == ChannelCode.BLOGGER:
            wordpress = Publication.objects.filter(
                article_id=publication.article_id,
                target__channel=ChannelCode.WORDPRESS,
                state=Publication.State.PUBLISHED,
            ).first()
            publication.canonical_source_url = wordpress.remote_url if wordpress else None
        PublicationMedia.objects.filter(
            publication=publication,
            article_revision_id=attempt.article_revision_id,
            binding_state=PublicationMedia.BindingState.PREPARED,
        ).update(binding_state=PublicationMedia.BindingState.ACTIVE, remote_verified_at=now)
        if (
            target.environment == TargetEnvironment.PRODUCTION
            and attempt.publication_intent.approval_mode == ApprovalMode.MANUAL
            and publication.state in {Publication.State.PUBLISHED, Publication.State.MARKED_WITHDRAWN}
            and target.pilot_state != ValidationState.PASSED
        ):
            target.pilot_state = ValidationState.PASSED
            target.last_pilot_at = now
            target.save(update_fields=["pilot_state", "last_pilot_at", "updated_at"])
            _snapshot_locked(target)
    elif result.status == "unknown_outcome":
        attempt.state = PublicationAttempt.State.UNKNOWN_OUTCOME
        publication.state = Publication.State.RECONCILING
        publication.remote_state = Publication.RemoteState.UNKNOWN
    elif result.status == "retryable_failed":
        attempt.state = PublicationAttempt.State.RETRYABLE_FAILED
        publication.state = Publication.State.RETRYABLE_FAILED
        publication.last_error_code = result.error_code or "publisher_retryable"
    elif result.status == "manual_required":
        attempt.state = PublicationAttempt.State.MANUAL_REQUIRED
        publication.state = Publication.State.MANUAL_REQUIRED
        publication.last_error_code = result.error_code or "manual_reconcile_required"
    else:
        attempt.state = PublicationAttempt.State.PERMANENT_FAILED
        publication.state = Publication.State.PERMANENT_FAILED
        publication.last_error_code = result.error_code or "publisher_permanent"
    retry_audit_action = None
    retry_result = None
    if (
        reconcile_generation is None
        and attempt.state == PublicationAttempt.State.RETRYABLE_FAILED
    ):
        if execution_attempt_no >= 5:
            attempt.state = PublicationAttempt.State.MANUAL_REQUIRED
            attempt.error_code = attempt.error_code or "publication_attempts_exhausted"
            publication.state = Publication.State.MANUAL_REQUIRED
            publication.last_error_code = attempt.error_code
            retry_audit_action = "publication_attempt.retry_exhausted"
            retry_result = "manual_required"
        else:
            attempt.attempt_no = execution_attempt_no + 1
            retry_audit_action = "publication_attempt.retry_scheduled"
            retry_result = "retry_scheduled"
    attempt.save()
    publication.save()
    if reconcile_generation is not None:
        reconcile_result_state = attempt.state
        reconcile_generation.state = (
            PublicationReconcileGeneration.State.COMPLETED
        )
        reconcile_generation.result_identity = result_identity
        reconcile_generation.result_state = reconcile_result_state
        reconcile_generation.completed_at = now
        reconcile_generation.save(
            update_fields=(
                "state",
                "result_identity",
                "result_state",
                "completed_at",
            )
        )
        if reconcile_result_state in {
            PublicationAttempt.State.RETRYABLE_FAILED,
            PublicationAttempt.State.UNKNOWN_OUTCOME,
        }:
            if reconcile_generation.generation >= 5:
                _manualize_reconcile_attempt_locked(
                    attempt,
                    error_code=(
                        attempt.error_code
                        or "reconcile_attempts_exhausted"
                    ),
                    now=now,
                )
                retry_audit_action = "publication_attempt.retry_exhausted"
                retry_result = "manual_required"
            else:
                delay = (
                    min(max(retry_after_seconds, 5), 3600)
                    if retry_after_seconds is not None
                    else min(
                        15 * (2 ** max(reconcile_generation.generation - 1, 0)),
                        1800,
                    )
                )
                attempt.next_retry_at = now + timedelta(seconds=delay)
                attempt.save(update_fields=("next_retry_at",))
                _enqueue_reconcile_locked(
                    attempt,
                    available_at=attempt.next_retry_at,
                )
                retry_audit_action = "publication_attempt.retry_scheduled"
                retry_result = "reconcile_scheduled"
    elif attempt.state == PublicationAttempt.State.UNKNOWN_OUTCOME:
        _enqueue_reconcile_locked(attempt)
    audit_action = (
        "publication_attempt.reconciled"
        if reconcile_generation is not None
        else "publication_attempt.finished"
    )
    audit_identity_suffix = (
        f"reconcile-result:{reconcile_generation.generation}"
        if reconcile_generation is not None
        else f"attempt-result:{execution_attempt_no}"
    )
    audit_metadata = {
        "publication_attempt_id": str(attempt.id),
        "attempt": execution_attempt_no,
        "action": attempt.resolved_action,
        "channel": publication.target.channel,
        "error_code": attempt.error_code,
        "intent_id": str(attempt.publication_intent_id),
        "result": result.status,
        "result_hash": result_identity,
        "state": attempt.state,
        "target_id": str(publication.target_id),
    }
    if reconcile_generation is not None:
        audit_metadata.update(
            {
                "reconcile_attempt_no": reconcile_generation.generation,
                "source_event_id": str(reconcile_generation.source_event_id),
            }
        )
    _record_publishing_audit(
        audit_context=audit_context,
        action=audit_action,
        entity=attempt,
        identity_key=f"{audit_context.event_key}:{audit_identity_suffix}",
        before_material=before_material,
        after_material={
            "attempt": _audit_state(
                attempt,
                resultIdentity=result_identity,
                resultStatus=result.status,
            ),
            "publication": _audit_state(publication),
            "target": _audit_state(target),
            "media": _publication_media_state_manifest(publication.id),
        },
        metadata=audit_metadata,
    )
    if retry_audit_action is not None:
        _record_publishing_audit(
            audit_context=audit_context,
            action=retry_audit_action,
            entity=attempt,
            identity_key=(
                f"{audit_context.event_key}:{retry_audit_action}:"
                f"{reconcile_generation.generation if reconcile_generation else execution_attempt_no}"
            ),
            before_material=before_material,
            after_material=_audit_state(attempt),
            metadata={
                "publication_attempt_id": str(attempt.id),
                "attempt": execution_attempt_no,
                "reconcile_attempt_no": (
                    reconcile_generation.generation
                    if reconcile_generation is not None
                    else 0
                ),
                "result": retry_result,
                "result_hash": result_identity,
                "state": attempt.state,
            },
        )
    if attempt.state == PublicationAttempt.State.SUCCEEDED:
        _release_dependents_on_commit(attempt)
        if attempt.publication_intent.correction_case_id:
            correction_case_id = str(attempt.publication_intent.correction_case_id)
            transaction.on_commit(
                lambda value=correction_case_id: _complete_correction(value)
            )
    return attempt


def _complete_correction(case_id: str) -> None:
    from .corrections import complete_correction_if_terminal

    complete_correction_if_terminal(case_id)


def publisher_error_result(error: PublisherError):
    from .contracts import PublishResult

    status = {
        "unknown_outcome": "unknown_outcome",
        "retryable": "retryable_failed",
        "refreshable_auth": "retryable_failed",
        "permanent": "permanent_failed",
    }.get(error.category, "permanent_failed")
    return PublishResult(
        status=status,
        remote_state="unknown",
        reconcile_required=status == "unknown_outcome",
        http_status=error.http_status,
        error_code=error.code,
        error_detail_redacted=error.detail_redacted,
    )


def _release_dependents_on_commit(attempt: PublicationAttempt) -> None:
    if attempt.publication.target.channel != ChannelCode.WORDPRESS:
        return
    dependent = list(
        PublicationAttempt.objects.filter(
            publication_intent=attempt.publication_intent,
            publication__target__channel=ChannelCode.BLOGGER,
            state=PublicationAttempt.State.QUEUED,
        ).values_list("id", flat=True)
    )
    for attempt_id in dependent:
        _enqueue_event(
            "publication.requested",
            {
                "publication_attempt_id": str(attempt_id),
            },
            dedupe_key=f"publication.requested:{attempt_id}:1",
            aggregate_type="publication_attempt",
            aggregate_id=attempt_id,
            job_id=attempt_id,
        )


@transaction.atomic
def begin_reconcile(
    attempt_id: str,
    expected_reconcile_attempt_no: int | None = None,
    *,
    source_event_id: uuid.UUID | str,
    audit_context: AuditContext,
) -> tuple[
    PublicationAttempt,
    PublicationReconcileGeneration | None,
    PublishCommand | None,
]:
    from wisdome_writer.infrastructure.models import OutboxMessage

    _require_audit_actor(audit_context, "worker")
    try:
        source_event_uuid = uuid.UUID(str(source_event_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise Conflict("reconcile source event context is invalid") from exc
    if str(source_event_uuid) != audit_context.event_key:
        raise Conflict("reconcile source event differs from audit provenance")
    attempt = PublicationAttempt.objects.select_for_update().select_related(
        "publication__target", "publication_intent", "approval__article_channel_render"
    ).get(id=attempt_id)
    expected_payload = {"publication_attempt_id": str(attempt.id)}
    if expected_reconcile_attempt_no is not None:
        expected_payload["reconcile_attempt_no"] = str(
            expected_reconcile_attempt_no
        )
    _require_worker_event(
        audit_context,
        topic="publication.reconcile_requested",
        aggregate_id=attempt.id,
        payload_identity=expected_payload,
    )
    source_event = OutboxMessage.objects.select_for_update().filter(
        id=source_event_uuid,
        topic="publication.reconcile_requested",
        aggregate_id=attempt.id,
    ).first()
    if source_event is None:
        raise Conflict("reconcile source event does not match the attempt")
    payload = source_event.payload if isinstance(source_event.payload, dict) else {}
    if payload.get("publication_attempt_id") != str(attempt.id):
        raise Conflict("reconcile source event payload does not match the attempt")

    generation = (
        PublicationReconcileGeneration.objects.select_for_update()
        .filter(source_event=source_event)
        .first()
    )
    if generation is not None:
        if generation.publication_attempt_id != attempt.id:
            raise Conflict("reconcile source event is bound to another attempt")
        if (
            expected_reconcile_attempt_no is not None
            and generation.generation != expected_reconcile_attempt_no
        ):
            raise Conflict("reconcile source event generation does not match")
    else:
        if attempt.state in {
            PublicationAttempt.State.SUCCEEDED,
            PublicationAttempt.State.PERMANENT_FAILED,
            PublicationAttempt.State.MANUAL_REQUIRED,
            PublicationAttempt.State.STALE,
        }:
            return attempt, None, None
        if source_event.event_version == 1:
            generation_no = attempt.reconcile_attempt_no + 1
        elif source_event.event_version == 2:
            payload_generation = payload.get("reconcile_attempt_no")
            if (
                type(payload_generation) is not int
                or payload_generation != expected_reconcile_attempt_no
            ):
                raise Conflict("reconcile v2 payload generation does not match")
            generation_no = payload_generation
        else:
            raise Conflict("unsupported reconcile event version")
        if generation_no < 1 or generation_no > 5:
            raise Conflict("reconcile attempt budget is exhausted")
        if generation_no != attempt.reconcile_attempt_no + 1:
            raise Conflict("reconcile generation is not contiguous")
        generation = PublicationReconcileGeneration.objects.create(
            publication_attempt=attempt,
            generation=generation_no,
            source_event=source_event,
            state=PublicationReconcileGeneration.State.STARTED,
            not_before=source_event.not_before,
            started_at=timezone.now(),
        )
        attempt.reconcile_attempt_no = generation_no
        attempt.save(update_fields=("reconcile_attempt_no",))

    if (
        generation.state == PublicationReconcileGeneration.State.COMPLETED
        or generation.generation < attempt.reconcile_attempt_no
    ):
        completed_audit = _worker_audit_replay(
            audit_context,
            entity=attempt,
            candidates=(
                (
                    "publication_attempt.reconciled",
                    (
                        f"{audit_context.event_key}:reconcile-result:"
                        f"{generation.generation}"
                    ),
                    {
                        "reconcile_attempt_no": generation.generation,
                        "result_hash": generation.result_identity,
                    },
                ),
                (
                    "publication_attempt.reconcile_delivery_failed",
                    (
                        f"{audit_context.event_key}:"
                        "reconcile-delivery-failure:"
                        f"{generation.generation}"
                    ),
                    {
                        "reconcile_attempt_no": generation.generation,
                        "source_event_id": str(source_event.id),
                    },
                ),
            ),
        )
        if completed_audit is None:
            raise Conflict(
                "completed reconcile generation has no matching result audit"
            )
        return attempt, generation, None
    if attempt.state == PublicationAttempt.State.SUCCEEDED:
        return attempt, generation, None
    if attempt.state not in {
        PublicationAttempt.State.UNKNOWN_OUTCOME,
        PublicationAttempt.State.RECONCILING,
        PublicationAttempt.State.RETRYABLE_FAILED,
    }:
        raise Conflict("unknown-outcome attempt만 조정할 수 있습니다.")
    before_material = _audit_state(attempt)
    attempt.state = PublicationAttempt.State.RECONCILING
    attempt.save(update_fields=["state"])
    attempt.publication.state = Publication.State.RECONCILING
    attempt.publication.save(update_fields=["state", "updated_at"])
    if not _worker_audit_replay(
        audit_context,
        entity=attempt,
        candidates=(
            (
                "publication_attempt.reconcile_started",
                (
                    f"{audit_context.event_key}:reconcile-start:"
                    f"{generation.generation}"
                ),
                {"reconcile_attempt_no": generation.generation},
            ),
        ),
    ):
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_attempt.reconcile_started",
            entity=attempt,
            identity_key=(
                f"{audit_context.event_key}:reconcile-start:"
                f"{generation.generation}"
            ),
            before_material=before_material,
            after_material=_audit_state(attempt),
            metadata={
                "publication_attempt_id": str(attempt.id),
                "attempt": attempt.attempt_no,
                "reconcile_attempt_no": generation.generation,
                "source_event_id": str(source_event.id),
                "state": attempt.state,
            },
        )
    return (
        attempt,
        generation,
        _command_for_attempt(attempt, _final_render(attempt)),
    )


@transaction.atomic
def finalize_reconcile_delivery_failure(
    attempt_id: str,
    *,
    source_event_id: uuid.UUID | str,
    error_code: str,
    audit_context: AuditContext,
) -> PublicationAttempt | None:
    from wisdome_writer.infrastructure.models import OutboxMessage

    _require_audit_actor(audit_context, "worker")
    try:
        source_event_uuid = uuid.UUID(str(source_event_id))
    except (ValueError, TypeError, AttributeError):
        return None
    if str(source_event_uuid) != audit_context.event_key:
        raise Conflict("reconcile source event differs from audit provenance")
    attempt = (
        PublicationAttempt.objects.select_for_update()
        .select_related("publication")
        .filter(id=attempt_id)
        .first()
    )
    if attempt is None:
        return None
    _require_worker_event(
        audit_context,
        topic="publication.reconcile_requested",
        aggregate_id=attempt.id,
        payload_identity={"publication_attempt_id": str(attempt.id)},
    )
    if _worker_audit_replay(
        audit_context,
        entity=attempt,
        candidates=(
            (
                "publication_attempt.reconcile_delivery_failed",
                (
                    f"{audit_context.event_key}:"
                    "reconcile-delivery-failure:"
                    f"{attempt.reconcile_attempt_no}"
                ),
                {
                    "reconcile_attempt_no": attempt.reconcile_attempt_no,
                    "source_event_id": str(source_event_uuid),
                    "error_code": (
                        error_code
                        or "reconcile_delivery_exhausted"
                    ),
                },
            ),
        ),
    ):
        return attempt
    before_material = _audit_state(attempt)

    def audit_failure() -> None:
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_attempt.reconcile_delivery_failed",
            entity=attempt,
            identity_key=(
                f"{audit_context.event_key}:reconcile-delivery-failure:"
                f"{attempt.reconcile_attempt_no}"
            ),
            before_material=before_material,
            after_material=_audit_state(attempt),
            metadata={
                "publication_attempt_id": str(attempt.id),
                "attempt": attempt.attempt_no,
                "reconcile_attempt_no": attempt.reconcile_attempt_no,
                "source_event_id": str(source_event_uuid),
                "error_code": error_code or "reconcile_delivery_exhausted",
                "result": "manual_required",
                "state": attempt.state,
            },
        )
    source_event = (
        OutboxMessage.objects.select_for_update()
        .filter(
            id=source_event_uuid,
            topic="publication.reconcile_requested",
            aggregate_id=attempt.id,
        )
        .first()
    )
    if source_event is None:
        _manualize_reconcile_attempt_locked(
            attempt,
            error_code="reconcile_source_event_missing",
        )
        audit_failure()
        return attempt
    payload = source_event.payload if isinstance(source_event.payload, dict) else {}
    if payload.get("publication_attempt_id") != str(attempt.id):
        _manualize_reconcile_attempt_locked(
            attempt,
            error_code="reconcile_source_event_mismatch",
        )
        audit_failure()
        return attempt

    generation = (
        PublicationReconcileGeneration.objects.select_for_update()
        .filter(source_event=source_event)
        .first()
    )
    if generation is None:
        if source_event.event_version == 1:
            generation_no = attempt.reconcile_attempt_no + 1
        elif source_event.event_version == 2:
            generation_no = payload.get("reconcile_attempt_no")
        else:
            generation_no = None
        if (
            type(generation_no) is not int
            or generation_no < 1
            or generation_no > 5
        ):
            _manualize_reconcile_attempt_locked(
                attempt,
                error_code="reconcile_generation_invalid",
            )
            audit_failure()
            return attempt
        generation = (
            PublicationReconcileGeneration.objects.select_for_update()
            .filter(
                publication_attempt=attempt,
                generation=generation_no,
            )
            .first()
        )
        if (
            generation is not None
            and generation.source_event_id != source_event.id
        ):
            _manualize_reconcile_attempt_locked(
                attempt,
                error_code="reconcile_generation_binding_conflict",
            )
            audit_failure()
            return attempt
        if generation is None:
            generation = PublicationReconcileGeneration.objects.create(
                publication_attempt=attempt,
                generation=generation_no,
                source_event=source_event,
                state=PublicationReconcileGeneration.State.STARTED,
                not_before=source_event.not_before,
                started_at=source_event.occurred_at,
            )
        if generation_no > attempt.reconcile_attempt_no:
            attempt.reconcile_attempt_no = generation_no
            attempt.save(update_fields=("reconcile_attempt_no",))

    _terminalize_reconcile_generation_locked(
        attempt,
        generation,
        error_code=error_code or "reconcile_delivery_exhausted",
    )
    audit_failure()
    return attempt


@dataclass(frozen=True)
class RemoteMediaReconcileFence:
    remote_media_id: uuid.UUID
    publication_attempt_id: uuid.UUID
    publication_intent_id: uuid.UUID
    target_id: uuid.UUID
    lease_generation: int
    request_fingerprint: str


def _remote_media_result_hash(result) -> str:
    return sha256_hex(
        {
            "status": result.status,
            "remote_media_id": result.remote_post_id,
            "remote_url": result.remote_url,
            "error_code": getattr(result, "error_code", None),
            "http_status": getattr(result, "http_status", None),
        }
    )


@transaction.atomic
def begin_remote_media_reconcile(
    remote_media_id: str,
    *,
    publication_attempt_id: str,
    publication_intent_id: str,
    audit_context: AuditContext,
) -> tuple[RemoteMedia, RemoteMediaReconcileFence | None]:
    _require_audit_actor(audit_context, "worker")
    attempt = PublicationAttempt.objects.select_for_update().get(
        id=publication_attempt_id,
        publication_intent_id=publication_intent_id,
    )
    remote = RemoteMedia.objects.select_for_update().get(id=remote_media_id)
    binding = (
        PublicationMedia.objects.select_for_update()
        .filter(
            publication_id=attempt.publication_id,
            remote_media_id=remote.id,
        )
        .order_by("id")
        .first()
    )
    if binding is None:
        raise Conflict(
            "media reconcile identity does not match the publication attempt"
        )
    _require_worker_event(
        audit_context,
        topic="media.reconcile_requested",
        aggregate_id=remote.id,
        payload_identity={
            "remote_media_id": str(remote.id),
            "publication_attempt_id": str(attempt.id),
            "publication_intent_id": str(attempt.publication_intent_id),
        },
    )
    result_identity = (
        f"{audit_context.event_key}:remote-media-result:"
        f"{remote.lease_generation}"
    )
    if _worker_audit_replay(
        audit_context,
        entity=remote,
        candidates=(
            (
                "remote_media.reconciled",
                result_identity,
                {
                    "publication_attempt_id": str(attempt.id),
                    "intent_id": str(attempt.publication_intent_id),
                },
            ),
        ),
    ):
        return remote, None
    if remote.state not in {
        RemoteMedia.State.RECONCILING,
        RemoteMedia.State.UPLOADING,
    }:
        raise Conflict(
            "terminal remote media state has no matching reconcile audit"
        )
    start_identity = (
        f"{audit_context.event_key}:remote-media-start:"
        f"{remote.lease_generation}"
    )
    if not _worker_audit_replay(
        audit_context,
        entity=remote,
        candidates=(
            (
                "remote_media.reconcile_started",
                start_identity,
                {
                    "publication_attempt_id": str(attempt.id),
                    "intent_id": str(attempt.publication_intent_id),
                },
            ),
        ),
    ):
        before_material = _audit_state(remote)
        remote.state = RemoteMedia.State.RECONCILING
        remote.save(update_fields=("state",))
        _record_publishing_audit(
            audit_context=audit_context,
            action="remote_media.reconcile_started",
            entity=remote,
            identity_key=start_identity,
            before_material=before_material,
            after_material=_audit_state(remote),
            metadata={
                "remote_media_id": str(remote.id),
                "publication_attempt_id": str(attempt.id),
                "intent_id": str(attempt.publication_intent_id),
                "target_id": str(remote.target_id),
                "result": "started",
                "state": remote.state,
            },
        )
    return remote, RemoteMediaReconcileFence(
        remote_media_id=remote.id,
        publication_attempt_id=attempt.id,
        publication_intent_id=attempt.publication_intent_id,
        target_id=remote.target_id,
        lease_generation=remote.lease_generation,
        request_fingerprint=remote.request_fingerprint,
    )


@transaction.atomic
def persist_remote_media_reconcile_result(
    fence: RemoteMediaReconcileFence,
    result,
    *,
    audit_context: AuditContext,
) -> RemoteMedia:
    _require_audit_actor(audit_context, "worker")
    attempt = PublicationAttempt.objects.select_for_update().filter(
        id=fence.publication_attempt_id,
        publication_intent_id=fence.publication_intent_id,
    ).first()
    if attempt is None:
        raise Conflict(
            "media reconcile result no longer matches the publication attempt"
        )
    remote = RemoteMedia.objects.select_for_update().get(
        id=fence.remote_media_id,
    )
    binding = (
        PublicationMedia.objects.select_for_update()
        .filter(
            publication_id=attempt.publication_id,
            remote_media_id=remote.id,
        )
        .order_by("id")
        .first()
    )
    if binding is None:
        raise Conflict(
            "media reconcile result no longer matches the publication attempt"
        )
    _require_worker_event(
        audit_context,
        topic="media.reconcile_requested",
        aggregate_id=remote.id,
        payload_identity={
            "remote_media_id": str(remote.id),
            "publication_attempt_id": str(attempt.id),
            "publication_intent_id": str(attempt.publication_intent_id),
        },
    )
    result_hash = _remote_media_result_hash(result)
    result_identity = (
        f"{audit_context.event_key}:remote-media-result:"
        f"{fence.lease_generation}"
    )
    replay = _worker_audit_replay(
        audit_context,
        entity=remote,
        candidates=(
            (
                "remote_media.reconciled",
                result_identity,
                {
                    "publication_attempt_id": str(attempt.id),
                    "intent_id": str(attempt.publication_intent_id),
                    "result_hash": result_hash,
                },
            ),
        ),
    )
    if replay is not None:
        return remote
    if (
        remote.target_id != fence.target_id
        or remote.lease_generation != fence.lease_generation
        or remote.request_fingerprint != fence.request_fingerprint
        or remote.state != RemoteMedia.State.RECONCILING
    ):
        raise Conflict("stale remote media reconcile result was fenced")
    before_material = _audit_state(remote)
    if result.status == "succeeded" and result.remote_post_id:
        remote.remote_media_id = result.remote_post_id
        remote.remote_source_url = result.remote_url
        remote.state = RemoteMedia.State.AVAILABLE
        result_code = "available"
    else:
        remote.state = RemoteMedia.State.FAILED
        result_code = "failed"
    remote.last_reconciled_at = timezone.now()
    remote.last_reconcile_hash = result_hash
    remote.save(
        update_fields=(
            "remote_media_id",
            "remote_source_url",
            "state",
            "last_reconciled_at",
            "last_reconcile_hash",
        )
    )
    _record_publishing_audit(
        audit_context=audit_context,
        action="remote_media.reconciled",
        entity=remote,
        identity_key=result_identity,
        before_material=before_material,
        after_material=_audit_state(
            remote,
            remoteIdentityHash=(
                sha256_hex(result.remote_post_id)
                if result.remote_post_id
                else None
            ),
            remoteUrlHash=(
                sha256_hex(result.remote_url)
                if result.remote_url
                else None
            ),
            resultHash=result_hash,
        ),
        metadata={
            "remote_media_id": str(remote.id),
            "publication_attempt_id": str(attempt.id),
            "intent_id": str(attempt.publication_intent_id),
            "target_id": str(remote.target_id),
            "result": result_code,
            "result_hash": result_hash,
            "status": result.status,
            "error_code": getattr(result, "error_code", None),
            "state": remote.state,
        },
    )
    return remote


@transaction.atomic
def prepare_wordpress_media(
    *,
    publication_id: str,
    article_revision_id: str,
    evidence_asset_id: str,
    published_evidence_snapshot_id: str | None,
    published_visualization_snapshot_id: str | None,
    block_id: str,
    usage: str,
    asset_checksum: str,
    presentation_hash: str,
    alt_text: str,
    caption: str,
    attribution: str,
) -> tuple[RemoteMedia, PublicationMedia]:
    publication = Publication.objects.select_for_update().select_related("target").get(id=publication_id)
    if publication.target.channel != ChannelCode.WORDPRESS:
        raise InvalidInput("WordPress publication만 remote media를 사용할 수 있습니다.")
    remote, _ = RemoteMedia.objects.select_for_update().get_or_create(
        target=publication.target,
        asset_checksum=asset_checksum,
        presentation_hash=presentation_hash,
        defaults={
            "evidence_asset_id": evidence_asset_id,
            "remote_lookup_key": f"ww-media-{asset_checksum[:20]}-{presentation_hash[:12]}",
            "request_fingerprint": sha256_hex({"checksum": asset_checksum, "presentation": presentation_hash}),
        },
    )
    if remote.state == RemoteMedia.State.ORPHANED:
        remote.state = RemoteMedia.State.PENDING
        remote.orphaned_at = None
        remote.lease_generation += 1
        remote.save(update_fields=["state", "orphaned_at", "lease_generation"])
    binding, _ = PublicationMedia.objects.get_or_create(
        publication=publication,
        article_revision_id=article_revision_id,
        remote_media=remote,
        block_id=block_id,
        defaults={
            "evidence_asset_id": evidence_asset_id,
            "published_evidence_snapshot_id": published_evidence_snapshot_id,
            "published_visualization_snapshot_id": published_visualization_snapshot_id,
            "usage": usage,
            "alt_text_snapshot": alt_text,
            "caption_snapshot": caption,
            "attribution_snapshot": attribution,
            "lease_generation": remote.lease_generation,
        },
    )
    return remote, binding


@transaction.atomic
def prepare_public_delivery(
    *,
    publication_id: str,
    article_revision_id: str,
    evidence_asset_id: str,
    published_evidence_snapshot_id: str | None,
    published_visualization_snapshot_id: str | None,
    block_id: str,
    usage: str,
    asset_checksum: str,
    presentation_hash: str,
    mime_type: str,
    byte_size: int,
    delivery_object_key: str,
    delivery_object_version: str,
    public_url: str,
    rights_status: str,
    alt_text: str,
    caption: str,
    attribution: str,
) -> tuple[PublicDeliveryAsset, PublicationMedia]:
    publication = Publication.objects.select_for_update().select_related("target").get(id=publication_id)
    if publication.target.channel != ChannelCode.BLOGGER:
        raise InvalidInput("Blogger publication만 public delivery asset을 사용합니다.")
    if not public_url.startswith("https://"):
        raise InvalidInput("Public delivery URL은 HTTPS여야 합니다.")
    delivery, _ = PublicDeliveryAsset.objects.select_for_update().get_or_create(
        asset_checksum=asset_checksum,
        presentation_hash=presentation_hash,
        defaults={
            "source_evidence_asset_id": evidence_asset_id,
            "mime_type": mime_type,
            "byte_size": byte_size,
            "delivery_object_key": delivery_object_key,
            "delivery_object_version": delivery_object_version,
            "public_url": public_url,
            "rights_status_snapshot": rights_status,
            "alt_text_snapshot": alt_text,
            "caption_snapshot": caption,
            "attribution_snapshot": attribution,
        },
    )
    if delivery.state in {PublicDeliveryAsset.State.PENDING_DELETE, PublicDeliveryAsset.State.WITHDRAWAL_PENDING}:
        delivery.state = PublicDeliveryAsset.State.AVAILABLE
        delivery.zero_reference_at = None
        delivery.delete_after = None
        delivery.lease_generation += 1
    delivery.active_reference_count += 1
    delivery.save()
    binding, _ = PublicationMedia.objects.get_or_create(
        publication=publication,
        article_revision_id=article_revision_id,
        public_delivery_asset=delivery,
        block_id=block_id,
        defaults={
            "evidence_asset_id": evidence_asset_id,
            "published_evidence_snapshot_id": published_evidence_snapshot_id,
            "published_visualization_snapshot_id": published_visualization_snapshot_id,
            "usage": usage,
            "alt_text_snapshot": alt_text,
            "caption_snapshot": caption,
            "attribution_snapshot": attribution,
            "lease_generation": delivery.lease_generation,
        },
    )
    return delivery, binding


@transaction.atomic
def schedule_public_delivery_deletion(asset_id: str) -> PublicDeliveryAsset:
    asset = PublicDeliveryAsset.objects.select_for_update().get(id=asset_id)
    active = PublicationMedia.objects.filter(
        public_delivery_asset=asset,
        binding_state__in=[PublicationMedia.BindingState.PREPARED, PublicationMedia.BindingState.ACTIVE],
        publication__state__in=[
            Publication.State.SCHEDULED,
            Publication.State.IN_PROGRESS,
            Publication.State.PUBLISHED,
            Publication.State.MARKED_WITHDRAWN,
            Publication.State.RECONCILING,
        ],
    ).count()
    asset.active_reference_count = active
    if active == 0:
        asset.state = PublicDeliveryAsset.State.PENDING_DELETE
        asset.zero_reference_at = timezone.now()
        asset.delete_after = timezone.now() + timedelta(days=30)
    asset.save()
    return asset


def _has_valid_retry_reservation(attempt: PublicationAttempt) -> bool:
    expected_attempt = max(attempt.attempt_no - 1, 0)
    events = AuditEvent.objects.using(attempt._state.db).filter(
        action="publication_attempt.retry_scheduled",
        entity_type=attempt._meta.label_lower,
        entity_id=attempt.id,
    ).order_by("occurred_at", "id")
    for event in events:
        metadata = validate_stored_metadata(
            action=event.action,
            metadata_schema_version=event.metadata_schema_version,
            redaction_policy_version=event.redaction_policy_version,
            redaction_policy_hash_value=event.redaction_policy_hash,
            metadata=event.metadata_redacted,
        )
        if metadata.get("attempt") == expected_attempt:
            return True
    return False


@transaction.atomic
def retry_publication_attempt(
    attempt_id: str,
    *,
    audit_context: AuditContext,
) -> tuple[PublicationAttempt, str]:
    _require_audit_actor(audit_context, "admin")
    attempt = (
        PublicationAttempt.objects.select_for_update()
        .select_related("publication__target")
        .get(id=attempt_id)
    )
    request_hash = _admin_request_hash(
        audit_context=audit_context,
        action="publication_attempt.retry_requested",
        payload={"publicationAttemptId": str(attempt.id)},
    )
    if _has_request_audit(
        audit_context=audit_context,
        action="publication_attempt.retry_requested",
        entity=attempt,
    ):
        replay = require_audit_replay(
            context=audit_context,
            action="publication_attempt.retry_requested",
            entity=attempt,
            identity_key=audit_context.request_key,
            request_hash=request_hash,
        )
        return attempt, str(replay.metadata_redacted.get("result") or "replay")
    before_material = _audit_state(attempt)
    if attempt.state in {
        PublicationAttempt.State.UNKNOWN_OUTCOME,
        PublicationAttempt.State.RECONCILING,
    }:
        _enqueue_reconcile_locked(attempt)
        action = (
            "manual_required"
            if attempt.state == PublicationAttempt.State.MANUAL_REQUIRED
            else "reconcile"
        )
    elif attempt.state == PublicationAttempt.State.RETRYABLE_FAILED:
        next_attempt_was_reserved = _has_valid_retry_reservation(attempt)
        if not next_attempt_was_reserved:
            attempt.attempt_no += 1
        attempt.state = PublicationAttempt.State.QUEUED
        attempt.started_at = None
        attempt.finished_at = None
        attempt.next_retry_at = None
        attempt.error_detail_redacted = ""
        attempt.save(
            update_fields=[
                "state",
                "attempt_no",
                "started_at",
                "finished_at",
                "next_retry_at",
                "error_detail_redacted",
            ]
        )
        _enqueue_event(
            "publication.requested",
            {"publication_attempt_id": str(attempt.id)},
            dedupe_key=f"publication.requested:{attempt.id}:{attempt.attempt_no}",
            aggregate_type="publication_attempt",
            aggregate_id=attempt.id,
            job_id=attempt.id,
        )
        action = "retry"
    else:
        raise Conflict("only retryable or unknown publication attempts can be retried")
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_attempt.retry_requested",
        entity=attempt,
        identity_key=(
            audit_context.request_key
            or f"retry-request:{audit_context.correlation_id}:{attempt.id}"
        ),
        before_material=before_material,
        after_material=_audit_state(attempt),
        metadata={
            "publication_attempt_id": str(attempt.id),
            "attempt": attempt.attempt_no,
            "request_hash": request_hash,
            "result": action,
            "state": attempt.state,
            "target_id": str(attempt.publication.target_id),
        },
    )
    return attempt, action


@dataclass(frozen=True)
class TargetCredentialRevokeFence:
    decision_id: uuid.UUID
    target_id: uuid.UUID
    target_snapshot_id: uuid.UUID
    target_snapshot_version: int
    target_config_hash: str


@transaction.atomic
def begin_target_credential_revoke(
    decision_id: str,
    *,
    audit_context: AuditContext,
) -> tuple[
    TargetDisconnectDecision,
    PublicationTarget,
    TargetCredentialRevokeFence,
] | None:
    _require_audit_actor(audit_context, "worker")
    target_id = TargetDisconnectDecision.objects.values_list(
        "target_id",
        flat=True,
    ).get(id=decision_id)
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    decision = TargetDisconnectDecision.objects.select_for_update().get(
        id=decision_id,
        target_id=target.id,
    )
    _require_worker_event(
        audit_context,
        topic="publishing.target_disconnect.requested",
        aggregate_id=target.id,
        payload_identity={
            "decision_id": str(decision.id),
            "target_id": str(target.id),
        },
    )
    terminal_recorded = _worker_audit_replay(
        audit_context,
        entity=decision,
        candidates=(
            (
                "publication_target.credentials_revoked",
                (
                    f"{audit_context.event_key}:"
                    "credential-revoke:succeeded"
                ),
                None,
            ),
            (
                "publication_target.credential_revoke_failed",
                (
                    f"{audit_context.event_key}:"
                    "credential-revoke:failed"
                ),
                None,
            ),
        ),
    )
    if terminal_recorded:
        return None
    if decision.state == TargetDisconnectDecision.State.COMPLETED:
        raise Conflict(
            "completed credential revocation has no matching audit event"
        )
    started_recorded = _worker_audit_replay(
        audit_context,
        entity=decision,
        candidates=(
            (
                "publication_target.credential_revoke_started",
                f"{audit_context.event_key}:credential-revoke-start",
                None,
            ),
        ),
    )
    if started_recorded:
        before_material = _audit_state(decision)
        decision.state = TargetDisconnectDecision.State.RECONCILING
        decision.remote_result_hash = sha256_hex(
            {
                "decisionId": str(decision.id),
                "result": "worker_redelivery_after_external_call_boundary",
            }
        )
        decision.save(update_fields=("state", "remote_result_hash"))
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.credential_revoke_failed",
            entity=decision,
            identity_key=f"{audit_context.event_key}:credential-revoke:failed",
            before_material=before_material,
            after_material=_audit_state(
                decision,
                outcomeHash=decision.remote_result_hash,
                errorCode="credential_revoke_outcome_unknown",
            ),
            metadata={
                "target_id": str(target.id),
                "decision_id": str(decision.id),
                "outcome_hash": decision.remote_result_hash,
                "error_code": "credential_revoke_outcome_unknown",
                "result": "unknown",
                "state": decision.state,
            },
        )
        return None
    if decision.state != TargetDisconnectDecision.State.ACCEPTED:
        raise Conflict(
            "credential revocation state has no matching terminal audit event"
        )
    if not started_recorded:
        before_material = _audit_state(decision)
        decision.state = TargetDisconnectDecision.State.REVOKING
        decision.save(update_fields=("state",))
        _record_publishing_audit(
            audit_context=audit_context,
            action="publication_target.credential_revoke_started",
            entity=decision,
            identity_key=f"{audit_context.event_key}:credential-revoke-start",
            before_material=before_material,
            after_material=_audit_state(decision),
            metadata={
                "target_id": str(target.id),
                "decision_id": str(decision.id),
                "result": "started",
                "state": decision.state,
            },
        )
    return (
        decision,
        target,
        TargetCredentialRevokeFence(
            decision_id=decision.id,
            target_id=target.id,
            target_snapshot_id=target.current_snapshot_id,
            target_snapshot_version=target.current_snapshot_version,
            target_config_hash=target.current_config_hash,
        ),
    )


@transaction.atomic
def persist_target_credential_revoke_result(
    fence: TargetCredentialRevokeFence,
    *,
    succeeded: bool,
    outcome_hash: str,
    error_code: str = "",
    unknown_outcome: bool = False,
    audit_context: AuditContext,
) -> TargetDisconnectDecision:
    _require_audit_actor(audit_context, "worker")
    target = PublicationTarget.objects.select_for_update().get(id=fence.target_id)
    decision = TargetDisconnectDecision.objects.select_for_update().get(
        id=fence.decision_id,
        target_id=target.id,
    )
    _require_worker_event(
        audit_context,
        topic="publishing.target_disconnect.requested",
        aggregate_id=target.id,
        payload_identity={
            "decision_id": str(decision.id),
            "target_id": str(target.id),
        },
    )
    fenced = (
        target.current_snapshot_id == fence.target_snapshot_id
        and target.current_snapshot_version == fence.target_snapshot_version
        and target.current_config_hash == fence.target_config_hash
    )
    committed_success = succeeded and fenced
    action = (
        "publication_target.credentials_revoked"
        if committed_success
        else "publication_target.credential_revoke_failed"
    )
    result_code = "revoked" if committed_success else "failed"
    outcome_status = (
        "succeeded"
        if committed_success
        else (
            "unknown_outcome"
            if unknown_outcome
            else (
                "stale_fence"
                if succeeded
                else "failed"
            )
        )
    )
    effective_error_code = error_code
    if succeeded and not fenced:
        effective_error_code = "target_snapshot_stale_after_revoke"
    replay = _worker_audit_replay(
        audit_context,
        entity=decision,
        candidates=(
            (
                action,
                (
                    f"{audit_context.event_key}:credential-revoke:"
                    f"{'succeeded' if committed_success else 'failed'}"
                ),
                {
                    "outcome_hash": outcome_hash,
                    "error_code": effective_error_code,
                    "result": result_code,
                    "status": outcome_status,
                },
            ),
        ),
    )
    if replay is not None:
        return decision
    before_material = _audit_state(decision)
    target_before_material = _audit_state(target)
    if succeeded and fenced:
        target.credential_ref = None
        target.username_ref = None
        target.save(
            update_fields=("credential_ref", "username_ref", "updated_at")
        )
        _snapshot_locked(target)
        decision.state = TargetDisconnectDecision.State.COMPLETED
    else:
        decision.state = (
            TargetDisconnectDecision.State.RECONCILING
            if unknown_outcome or (succeeded and not fenced)
            else TargetDisconnectDecision.State.FAILED
        )
    decision.remote_result_hash = outcome_hash
    decision.save(update_fields=("state", "remote_result_hash"))
    _record_publishing_audit(
        audit_context=audit_context,
        action=action,
        entity=decision,
        identity_key=(
            f"{audit_context.event_key}:credential-revoke:"
            f"{'succeeded' if succeeded and fenced else 'failed'}"
        ),
        before_material={
            "decision": before_material,
            "target": target_before_material,
        },
        after_material={
            "decision": _audit_state(
                decision,
                outcomeHash=outcome_hash,
                errorCode=effective_error_code,
            ),
            "target": _audit_state(target),
        },
        metadata={
            "target_id": str(target.id),
            "decision_id": str(decision.id),
            "outcome_hash": outcome_hash,
            "error_code": effective_error_code,
            "result": result_code,
            "status": outcome_status,
            "state": decision.state,
        },
    )
    return decision


@transaction.atomic
def disconnect_target(
    target_id: str,
    data: dict[str, Any],
    *,
    request,
    audit_context: AuditContext,
) -> TargetDisconnectDecision:
    _require_audit_actor(audit_context, "admin")
    user = request.user
    if audit_context.actor_id != user.pk:
        raise Forbidden("disconnect audit actor differs from the administrator")
    if (
        data.get("requestKey") != audit_context.request_key
        or data.get("reason") != audit_context.reason_code
    ):
        raise Forbidden(
            "disconnect provenance differs from the audit context"
        )
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_hash = _request_hash(data)
    existing = TargetDisconnectDecision.objects.filter(target=target, request_key=data["requestKey"]).first()
    if existing:
        if existing.request_hash != request_hash or existing.decided_by_id != user.pk:
            raise Conflict("같은 request key가 다른 연결 해제 payload에 사용되었습니다.")
        require_audit_replay(
            context=audit_context,
            action="publication_target.disconnected",
            entity=target,
            identity_key=f"target-disconnect:{existing.id}",
            request_hash=request_hash,
        )
        return existing
    if (
        str(target.current_snapshot_id) != str(data["expectedTargetSnapshotId"])
        or target.current_config_hash != data["expectedTargetConfigHash"]
    ):
        raise Conflict("target snapshot이 바뀌었습니다.")
    before_material = _audit_state(target)
    consume_reauthentication_proof(
        request=request,
        proof_id=data["reauthProofId"],
        action_scope="credential_disconnect",
        entity_type="publication_target",
        entity_id=target.id,
    )
    decision = TargetDisconnectDecision.objects.create(
        target=target,
        expected_target_snapshot_id=data["expectedTargetSnapshotId"],
        expected_target_config_hash=data["expectedTargetConfigHash"],
        request_key=data["requestKey"],
        request_hash=request_hash,
        reauth_proof_id=data["reauthProofId"],
        reason=data["reason"],
        decided_by=user,
    )
    target.auto_publish_enabled = False
    target.connection_state = PublicationTarget.ConnectionState.REVOKED
    target.save()
    _snapshot_locked(target)
    _record_publishing_audit(
        audit_context=audit_context,
        action="publication_target.disconnected",
        entity=target,
        identity_key=f"target-disconnect:{decision.id}",
        before_material=before_material,
        after_material=_audit_state(target),
        metadata={
            "target_id": str(target.id),
            "decision_id": str(decision.id),
            "request_hash": request_hash,
            "result": "accepted",
            "state": target.connection_state,
            "reauth_proof_id": str(decision.reauth_proof_id),
        },
    )
    _enqueue_event(
        "publishing.target_disconnect.requested",
        {
            "decision_id": str(decision.id),
            "target_id": str(target.id),
        },
        dedupe_key=f"target-disconnect:{decision.id}",
    )
    return decision

def _enqueue_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_version: int = 1,
    dedupe_key: str,
    aggregate_type: str | None = None,
    aggregate_id=None,
    job_id=None,
    available_at=None,
):
    from wisdome_writer.infrastructure.outbox import enqueue_event

    resolved_id = (
        aggregate_id
        or payload.get("target_id")
        or payload.get("canary_run_id")
        or payload.get("decision_id")
    )
    return enqueue_event(
        event_type=event_type,
        event_version=event_version,
        aggregate_type=aggregate_type
        or (event_type.split(".")[1] if "." in event_type else "publishing"),
        aggregate_id=uuid.UUID(str(resolved_id)),
        job_id=job_id or resolved_id,
        payload=payload,
        dedupe_key=dedupe_key,
        available_at=available_at,
    )
