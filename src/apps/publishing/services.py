from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone as dt_timezone
from typing import Any, Iterable

from django.apps import apps
from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils.module_loading import import_string
from django.utils import timezone

from adapters.publishers.blogger import BloggerOAuthClient, BloggerPublisher
from adapters.publishers.wordpress import WordPressPublisher
from apps.accounts.services import consume_reauthentication_proof
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


def _request_hash(payload: dict[str, Any]) -> str:
    return sha256_hex(payload)


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


def start_blogger_oauth(target_id: str, *, user, redirect_uri: str) -> dict[str, Any]:
    target = PublicationTarget.objects.get(id=target_id, channel=ChannelCode.BLOGGER)
    state = signing.dumps(
        {"targetId": str(target.id), "adminId": str(user.id), "nonce": uuid.uuid4().hex},
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


@transaction.atomic
def complete_blogger_oauth(
    *, code: str, state: str, user, redirect_uri: str
) -> PublicationTarget:
    try:
        state_data = signing.loads(
            state,
            salt="publishing.blogger.oauth",
            max_age=600,
        )
    except signing.BadSignature as exc:
        raise Forbidden("Blogger OAuth state가 유효하지 않거나 만료되었습니다.") from exc
    if str(state_data.get("adminId")) != str(user.id):
        raise Forbidden("OAuth를 시작한 관리자 session과 다릅니다.")
    target = PublicationTarget.objects.select_for_update().get(
        id=state_data["targetId"], channel=ChannelCode.BLOGGER
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
    credential_ref = token_store(
        target_id=str(target.id),
        token_payload=token_payload,
    )
    if not credential_ref:
        raise InvalidInput("OAuth token store가 credential reference를 반환하지 않았습니다.")
    target.credential_ref = str(credential_ref)
    target.connection_state = PublicationTarget.ConnectionState.PENDING
    target.preflight_state = ValidationState.NOT_RUN
    target.auto_publish_enabled = False
    target.save()
    _snapshot_locked(target)
    _audit("publication_target.oauth_connected", target, None, target.current_config_hash)
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
def create_target(data: dict[str, Any]) -> PublicationTarget:
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
    _audit("publication_target.created", target, None, target.current_config_hash)
    return target


@transaction.atomic
def update_target(target_id: str, data: dict[str, Any]) -> PublicationTarget:
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    immutable = {"channel", "channelRole", "role", "environment", "baseUrl", "remoteBlogId"}
    if immutable.intersection(data):
        raise InvalidInput("채널, 역할, 환경, base URL과 remote blog ID는 변경할 수 없습니다.")
    before_hash = target.current_config_hash
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
    if changed_connection:
        target.connection_state = PublicationTarget.ConnectionState.PENDING
        target.preflight_state = ValidationState.STALE
        target.pilot_state = ValidationState.STALE
        target.auto_publish_enabled = False
        target.latest_auto_publish_activation_id = None
        AutoPublishValidation.objects.filter(target=target, status=AutoPublishValidation.State.PASSED).update(
            status=AutoPublishValidation.State.STALE,
            invalidated_at=timezone.now(),
            invalidation_reason="target_configuration_changed",
        )
        PublicationIntent.objects.filter(
            target_snapshot_refs__contains=[{"targetId": str(target.id)}],
            state__in=[
                PublicationIntent.State.DRAFT,
                PublicationIntent.State.AWAITING_APPROVAL,
                PublicationIntent.State.APPROVED,
            ],
        ).update(state=PublicationIntent.State.STALE)
    target.full_clean()
    target.save()
    _snapshot_locked(target)
    _audit("publication_target.updated", target, before_hash, target.current_config_hash)
    return target


@dataclass(frozen=True)
class TargetPreflightFence:
    target_id: uuid.UUID
    target_snapshot_id: uuid.UUID
    target_config_hash: str
    target_snapshot_version: int


@transaction.atomic
def begin_target_preflight(
    target_id: str,
    *,
    expected_snapshot_id: str,
    expected_config_hash: str,
) -> tuple[PublicationTarget, TargetPreflightFence] | None:
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    if (
        not target.current_snapshot_id
        or str(target.current_snapshot_id) != str(expected_snapshot_id)
        or target.current_config_hash != expected_config_hash
    ):
        _audit(
            "publication_target.preflight_stale_before_call",
            target,
            expected_config_hash,
            target.current_config_hash,
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
) -> tuple[PublicationTarget, bool]:
    target = PublicationTarget.objects.select_for_update().get(id=fence.target_id)
    if (
        target.current_snapshot_id != fence.target_snapshot_id
        or target.current_config_hash != fence.target_config_hash
        or target.current_snapshot_version != fence.target_snapshot_version
    ):
        _audit(
            "publication_target.preflight_stale_after_call",
            target,
            fence.target_config_hash,
            target.current_config_hash,
        )
        return target, False
    target.capabilities = result.capabilities.as_dict()
    target.preflight_state = ValidationState.PASSED if result.passed else ValidationState.FAILED
    if result.passed:
        target.connection_state = PublicationTarget.ConnectionState.VERIFIED
    else:
        target.connection_state = PublicationTarget.ConnectionState.BLOCKED
        target.auto_publish_enabled = False
        AutoPublishValidation.objects.filter(
            target=target, status=AutoPublishValidation.State.PASSED
        ).update(
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
    _audit("publication_target.preflight", target, None, result_hash)
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
    resolver: Any | None = None,
) -> tuple[PublicationTarget, bool]:
    prepared = begin_target_preflight(
        target_id,
        expected_snapshot_id=expected_snapshot_id,
        expected_config_hash=expected_config_hash,
    )
    if prepared is None:
        return PublicationTarget.objects.get(id=target_id), False
    target, fence = prepared
    adapter = publisher_for_target(target, resolver=resolver)
    try:
        result = adapter.preflight_connection()
    finally:
        adapter.close()
    return persist_target_preflight_result(fence, result)


@transaction.atomic
def create_canary_run(
    target_id: str, *, policy_version: str, reason: str, request_key: str, user
) -> TargetCanaryRun:
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    if target.environment != TargetEnvironment.TEST:
        raise Forbidden("쓰기가 발생하는 canary는 격리된 test target에서만 실행할 수 있습니다.")
    if target.preflight_state != ValidationState.PASSED:
        raise Conflict("읽기 전용 preflight를 먼저 통과해야 합니다.")
    existing = TargetCanaryRun.objects.filter(target=target, request_key=request_key).first()
    if existing:
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
def create_auto_publish_validation(target_id: str, data: dict[str, Any]) -> AutoPublishValidation:
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_key = data["requestKey"]
    material = _validation_material(data, str(target.id))
    request_hash = _request_hash({"requestKey": request_key, **material})
    existing = AutoPublishValidation.objects.filter(target=target, request_key=request_key).first()
    if existing:
        if existing.request_hash != request_hash:
            raise Conflict("같은 request key가 다른 validation payload에 사용되었습니다.")
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
    return AutoPublishValidation.objects.create(
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


@transaction.atomic
def decide_auto_publish_validation(
    target_id: str, validation_id: str, data: dict[str, Any], *, request
) -> tuple[AutoPublishValidationDecision, bool]:
    user = request.user
    validation = AutoPublishValidation.objects.select_for_update().select_related("target").get(
        id=validation_id, target_id=target_id
    )
    request_hash = _request_hash(data)
    existing = validation.decisions.filter(request_key=data["requestKey"]).first()
    if existing:
        if existing.request_hash != request_hash or existing.decided_by_id != user.pk:
            raise Conflict("같은 request key가 다른 decision payload에 사용되었습니다.")
        return existing, False
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
        validation.target.auto_publish_enabled = False
        validation.target.save(update_fields=["auto_publish_enabled", "updated_at"])
        PublicationIntent.objects.filter(
            auto_publish_validation_refs__contains=[{"validationId": str(validation.id)}],
            state__in=[PublicationIntent.State.APPROVED, PublicationIntent.State.AWAITING_APPROVAL],
        ).update(state=PublicationIntent.State.STALE)
    _audit("auto_publish_validation.decided", validation, None, decision_hash)
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
    target_id: str, data: dict[str, Any], *, request
) -> tuple[AutoPublishActivation, bool]:
    user = request.user
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_hash = _request_hash(data)
    existing = target.activations.filter(request_key=data["requestKey"]).first()
    if existing:
        if existing.request_hash != request_hash or existing.decided_by_id != user.pk:
            raise Conflict("같은 request key가 다른 activation payload에 사용되었습니다.")
        return existing, False
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
        PublicationIntent.objects.filter(
            auto_publish_activation_refs__contains=[{"activationId": str(activation.supersedes_activation_id)}],
            state__in=[PublicationIntent.State.APPROVED, PublicationIntent.State.AWAITING_APPROVAL],
        ).update(state=PublicationIntent.State.STALE)
    _audit("auto_publish_activation.decided", target, None, activation_hash)
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
def create_publication_intent(article_id: str, data: dict[str, Any], *, user) -> PublicationIntent:
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
    request_key = data["requestKey"]
    existing = PublicationIntent.objects.filter(article_revision_id=revision.id, request_key=request_key).first()
    target_refs = _target_ref_map(data["targetSnapshots"])
    commands = _command_map(data["targetCommands"])
    if set(target_refs) != set(commands):
        raise InvalidInput("target snapshot과 command target 집합이 같아야 합니다.")
    latest = PublicationIntent.objects.filter(article_id=article.id).order_by("-created_at").first()
    if _id(latest.id if latest else None) != _id(data.get("expectedLatestIntentId")):
        raise Conflict("발행 의도가 갱신되었습니다. 다시 불러오세요.")
    if existing:
        candidate_hash = _intent_hash(material_data, revision, target_refs, commands)
        if existing.intent_hash != candidate_hash:
            raise Conflict("같은 request key가 다른 발행 의도에 사용되었습니다.")
        return existing
    target_rows = {
        str(row.id): row
        for row in PublicationTarget.objects.select_for_update().filter(id__in=target_refs.keys())
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
    _audit("publication_intent.created", intent, None, intent.intent_hash)
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
    article_id: str, target_id: str, data: dict[str, Any], *, user, request=None
) -> tuple[Approval, bool]:
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
        return existing, False
    latest_intent = PublicationIntent.objects.filter(article_id=article_id).order_by("-created_at").first()
    if not latest_intent or latest_intent.id != intent.id or intent.state == PublicationIntent.State.STALE:
        raise Conflict("current 발행 의도만 승인할 수 있습니다.")
    command = next(
        (row for row in intent.target_commands if str(row["targetId"]) == str(target_id)), None
    )
    if not command:
        raise InvalidInput("발행 의도에 해당 target command가 없습니다.")
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
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
    _audit("publication_approval.decided", approval, None, approval.approval_subject_hash)
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
def dispatch_publication(article_id: str, data: dict[str, Any]) -> list[PublicationAttempt]:
    intent = PublicationIntent.objects.select_for_update().get(
        id=data["publicationIntentId"], article_id=article_id
    )
    if intent.revision_no != int(data["revisionNo"]):
        raise Conflict("발행 요청 revision과 intent가 다릅니다.")
    latest = PublicationIntent.objects.filter(article_id=article_id).order_by("-created_at").first()
    if not latest or latest.id != intent.id or intent.state != PublicationIntent.State.APPROVED:
        raise Conflict("current approved 발행 의도만 전송할 수 있습니다.")
    expected = _target_ref_map(data["expectedTargetSnapshots"])
    requested_ids = [str(value) for value in data["targetIds"]]
    if set(expected) != set(requested_ids):
        raise InvalidInput("target IDs와 expected target snapshot 집합이 같아야 합니다.")
    command_map = {str(row["targetId"]): row for row in intent.target_commands}
    target_rows = {
        str(row.id): row
        for row in PublicationTarget.objects.select_for_update().filter(id__in=requested_ids)
    }
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
        idempotency_key = sha256_hex(
            {
                "intentId": str(intent.id),
                "targetId": target_id,
                "action": command["resolvedAction"],
                "requestKey": data["requestKey"],
            }
        )
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
    _audit("publication.dispatched", intent, None, sha256_hex([str(row.id) for row in attempts]))
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
    try:
        OperationalControl = apps.get_model("scheduling", "OperationalControl")
        return OperationalControl.objects.filter(key="global_kill_switch", enabled=True).exists()
    except LookupError:
        return False


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


def _enqueue_reconcile_locked(
    attempt: PublicationAttempt,
    *,
    available_at=None,
) -> None:
    from wisdome_writer.infrastructure.models import OutboxMessage

    reconcile_attempt_no = attempt.reconcile_attempt_no + 1
    dedupe_key = (
        f"publication.reconcile_requested:{attempt.id}:{reconcile_attempt_no}"
    )
    if OutboxMessage.objects.filter(message_key=dedupe_key).exists():
        return
    _enqueue_event(
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


@transaction.atomic
def begin_attempt(
    attempt_id: str,
) -> tuple[PublicationAttempt, PublishCommand | None]:
    attempt = PublicationAttempt.objects.select_for_update().select_related(
        "publication__target", "publication_intent", "approval__article_channel_render"
    ).get(id=attempt_id)
    if attempt.state == PublicationAttempt.State.SUCCEEDED:
        return attempt, None
    if attempt.state == PublicationAttempt.State.RUNNING:
        attempt.state = PublicationAttempt.State.UNKNOWN_OUTCOME
        attempt.finished_at = timezone.now()
        attempt.error_code = "delivery_redelivered_after_begin"
        attempt.save(update_fields=["state", "finished_at", "error_code"])
        publication = attempt.publication
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
        return attempt, None
    if attempt.state in {
        PublicationAttempt.State.RECONCILING,
        PublicationAttempt.State.UNKNOWN_OUTCOME,
    }:
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
        attempt.state = PublicationAttempt.State.STALE
        attempt.finished_at = timezone.now()
        attempt.error_code = "attempt_gate_stale"
        attempt.save(update_fields=["state", "finished_at", "error_code"])
        raise
    render = _final_render(attempt)
    attempt.state = PublicationAttempt.State.RUNNING
    attempt.started_at = timezone.now()
    attempt.error_code = ""
    attempt.save(update_fields=["state", "started_at", "error_code"])
    publication = attempt.publication
    publication.state = {
        PublicationAction.CREATE: Publication.State.IN_PROGRESS,
        PublicationAction.UPDATE: Publication.State.UPDATING,
        PublicationAction.MARK_WITHDRAWN: Publication.State.MARKING_WITHDRAWN,
        PublicationAction.UNPUBLISH: Publication.State.WITHDRAWING,
    }[attempt.resolved_action]
    publication.save(update_fields=["state", "updated_at"])
    return attempt, _command_for_attempt(attempt, render)


@transaction.atomic
def persist_publish_result(attempt_id: str, result) -> PublicationAttempt:
    attempt = PublicationAttempt.objects.select_for_update().select_related(
        "publication__target", "publication_intent"
    ).get(id=attempt_id)
    publication = attempt.publication
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
        target = publication.target
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
    attempt.save()
    publication.save()
    _audit("publication_attempt.finished", attempt, None, sha256_hex({"state": attempt.state, "remote": result.remote_post_id}))
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
) -> tuple[PublicationAttempt, PublishCommand | None]:
    attempt = PublicationAttempt.objects.select_for_update().select_related(
        "publication__target", "publication_intent", "approval__article_channel_render"
    ).get(id=attempt_id)
    if attempt.state == PublicationAttempt.State.SUCCEEDED:
        return attempt, None
    if attempt.state not in {
        PublicationAttempt.State.UNKNOWN_OUTCOME,
        PublicationAttempt.State.RECONCILING,
        PublicationAttempt.State.RETRYABLE_FAILED,
    }:
        raise Conflict("unknown-outcome attempt만 조정할 수 있습니다.")
    if expected_reconcile_attempt_no is None:
        expected_reconcile_attempt_no = attempt.reconcile_attempt_no + 1
    if expected_reconcile_attempt_no < attempt.reconcile_attempt_no:
        return attempt, None
    if expected_reconcile_attempt_no > attempt.reconcile_attempt_no + 1:
        raise Conflict("reconcile attempt sequence has a gap")
    if expected_reconcile_attempt_no == attempt.reconcile_attempt_no + 1:
        if expected_reconcile_attempt_no > 5:
            raise Conflict("reconcile attempt budget is exhausted")
        attempt.reconcile_attempt_no = expected_reconcile_attempt_no
    attempt.state = PublicationAttempt.State.RECONCILING
    attempt.save(update_fields=["state", "reconcile_attempt_no"])
    attempt.publication.state = Publication.State.RECONCILING
    attempt.publication.save(update_fields=["state", "updated_at"])
    return attempt, _command_for_attempt(attempt, _final_render(attempt))


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


@transaction.atomic
def disconnect_target(
    target_id: str, data: dict[str, Any], *, request
) -> TargetDisconnectDecision:
    user = request.user
    target = PublicationTarget.objects.select_for_update().get(id=target_id)
    request_hash = _request_hash(data)
    existing = TargetDisconnectDecision.objects.filter(target=target, request_key=data["requestKey"]).first()
    if existing:
        if existing.request_hash != request_hash or existing.decided_by_id != user.pk:
            raise Conflict("같은 request key가 다른 연결 해제 payload에 사용되었습니다.")
        return existing
    if (
        str(target.current_snapshot_id) != str(data["expectedTargetSnapshotId"])
        or target.current_config_hash != data["expectedTargetConfigHash"]
    ):
        raise Conflict("target snapshot이 바뀌었습니다.")
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
    _audit("publication_target.disconnected", target, data["expectedTargetConfigHash"], target.current_config_hash)
    _enqueue_event(
        "publishing.target_disconnect.requested",
        {
            "decision_id": str(decision.id),
            "target_id": str(target.id),
        },
        dedupe_key=f"target-disconnect:{decision.id}",
    )
    return decision

def _audit(action: str, entity: Any, before_hash: str | None, after_hash: str | None) -> None:
    try:
        AuditEvent = apps.get_model("audit", "AuditEvent")
    except LookupError:
        return
    try:
        from wisdome_writer.observability import current_correlation_id

        correlation_id = current_correlation_id()
        try:
            correlation_id = uuid.UUID(str(correlation_id))
        except (ValueError, TypeError, AttributeError):
            correlation_id = uuid.uuid4()
        AuditEvent.objects.record(
            correlation_id=correlation_id,
            actor_type="system",
            action=action,
            entity_type=entity._meta.label_lower,
            entity_id=entity.pk,
            before_hash=before_hash or None,
            after_hash=after_hash or None,
            metadata={},
        )
    except (IntegrityError, TypeError, ValueError):
        return


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
) -> None:
    from wisdome_writer.infrastructure.outbox import enqueue_event

    resolved_id = (
        aggregate_id
        or payload.get("target_id")
        or payload.get("canary_run_id")
        or payload.get("decision_id")
    )
    enqueue_event(
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
