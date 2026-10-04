import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.services import issue_reauthentication_proof
from apps.audit.services import AuditContext
from apps.publishing import services
from apps.publishing.models import AutoPublishValidation, ChannelRole, PublicationTarget
from wisdome_writer.domain.hashing import sha256_hex


@pytest.mark.django_db
def test_validation_decision_consumes_its_own_scoped_proof_and_replays():
    user = get_user_model().objects.create_user(
        email="validation-review@example.com",
        password="fixture-password",
        is_staff=True,
    )
    target = PublicationTarget.objects.create(
        channel="blogger",
        role=ChannelRole.SECONDARY,
        environment="test",
        display_name="Review target",
        remote_blog_id="review-blog",
        base_url="https://blog.example.com/",
        credential_ref="vault://review/blogger",
        credential_version="v1",
    )
    services._snapshot_locked(target)
    validation = AutoPublishValidation.objects.create(
        target=target,
        target_snapshot_id=target.current_snapshot_id,
        target_config_hash=target.current_config_hash,
        topic_code="housing_subscription",
        source_registry_snapshot_id=uuid.uuid4(),
        topic_policy_version=1,
        registry_manifest_hash="a" * 64,
        source_adapter_manifest_hash="a" * 64,
        extraction_profile_manifest_hash="a" * 64,
        generation_pipeline_manifest_hash="a" * 64,
        editorial_policy_hash="a" * 64,
        quality_gate_manifest_hash="a" * 64,
        render_contract_version="render-v1",
        channel_contract_version="blogger-v1",
        publisher_adapter_manifest_hash=target.publisher_adapter_manifest_hash,
        test_report_object_key="db://review",
        test_report_object_version="v1",
        test_report_hash="a" * 64,
        material_hash="a" * 64,
        request_key="review-validation-create",
        request_hash="b" * 64,
    )
    request = SimpleNamespace(
        user=user,
        session=SimpleNamespace(session_key="review-scope-session"),
        correlation_id=uuid.uuid4(),
    )
    proof = issue_reauthentication_proof(
        request=request,
        current_password="fixture-password",
        action_scopes=["validation_decision"],
    )
    body = {
        "decision": "revoked",
        "expectedLatestDecisionId": None,
        "requestKey": "review-validation-revoke",
        "reason": "Retire reviewed validation",
        "reauthProofId": str(proof.id),
    }
    context = AuditContext.for_admin(
        request=request,
        reason_code=body["reason"],
        request_key=body["requestKey"],
    )
    decision, created = services.decide_auto_publish_validation(
        str(target.id),
        str(validation.id),
        body,
        request=request,
        audit_context=context,
    )
    assert created is True
    assert decision.decision == "revoked"
    validation.refresh_from_db()
    proof.refresh_from_db()
    assert validation.status == "revoked"
    assert proof.consumed_at is not None
    assert proof.consumed_action == "validation_decision"
    replay, replay_created = services.decide_auto_publish_validation(
        str(target.id),
        str(validation.id),
        body,
        request=request,
        audit_context=context,
    )
    assert replay.id == decision.id
    assert replay_created is False


@pytest.mark.parametrize("history", [[], [{"correctionCaseId": "verified-case-1"}]])
def test_real_preview_hashes_are_accepted_and_bind_correction_history(monkeypatch, history):
    revision_id = uuid.uuid4()
    intent = SimpleNamespace(article_revision_id=revision_id, input_evidence_manifest_hash="a" * 64)
    claims = Mock()
    claims.exists.return_value = False
    claims.values_list.return_value = []
    revision = SimpleNamespace(
        id=revision_id,
        title="Official fact",
        body_markdown="Official fact",
        article=SimpleNamespace(topic_code="housing_subscription"),
        claims=claims,
    )
    target = SimpleNamespace(
        channel="wordpress",
        role="primary_canonical",
        current_snapshot_id=uuid.uuid4(),
        current_config_hash="b" * 64,
    )
    monkeypatch.setattr(
        services, "freeze_revision_asset_cohort", lambda **kwargs: SimpleNamespace()
    )
    monkeypatch.setattr(services, "build_channel_media_manifest", lambda **kwargs: [])
    monkeypatch.setattr(services, "_correction_render_material", lambda *args: (list(history), ""))
    monkeypatch.setattr(
        services, "_revision_source_links", lambda *args: ["https://example.com/notice"]
    )
    monkeypatch.setattr(
        services.ArticleChannelRender.objects,
        "create",
        lambda **kwargs: SimpleNamespace(id=uuid.uuid4(), **kwargs),
    )
    render = services._create_preview_render.__wrapped__(intent, revision, target)
    material = services._render_approval_material(render, intent=intent, target=target)
    assert material["renderId"] == str(render.id)
    if history == []:
        legacy_template = render.template_hash
        legacy_source = render.source_manifest_hash
        render.template_hash = sha256_hex(
            {
                "channel": target.channel,
                "title": render.title,
                "body": render.body_html,
                "revision": str(revision_id),
                "correctionHistory": [],
            }
        )
        render.source_manifest_hash = sha256_hex(
            {
                "inputEvidenceManifestHash": intent.input_evidence_manifest_hash,
                "sourceLinks": render.source_links,
                "correctionHistory": [],
            }
        )
        services._render_approval_material(render, intent=intent, target=target)
        render.template_hash = legacy_template
        with pytest.raises(services.Conflict, match="stale"):
            services._render_approval_material(render, intent=intent, target=target)
        render.source_manifest_hash = legacy_source
    render.correction_history.append({"correctionCaseId": "unapproved-tampering"})
    with pytest.raises(services.Conflict, match="stale"):
        services._render_approval_material(render, intent=intent, target=target)


@pytest.fixture
def disconnect_subject(db):
    user = get_user_model().objects.create_user(
        email="disconnect-review@example.com",
        password="fixture-password",
        is_staff=True,
    )
    target = PublicationTarget.objects.create(
        channel="wordpress",
        role="primary_canonical",
        environment="test",
        display_name="Review",
        base_url="https://wordpress.example.com",
        username_ref="vault://review/user",
        credential_ref="vault://review/v1",
        credential_version="v1",
        capabilities=services.WordPressPublisher.capabilities.as_dict(),
    )
    services._snapshot_locked(target)
    request = SimpleNamespace(
        user=user,
        session=SimpleNamespace(session_key="disconnect-review"),
        correlation_id=uuid.uuid4(),
    )
    proof = issue_reauthentication_proof(
        request=request,
        current_password="fixture-password",
        action_scopes=["credential_disconnect"],
    )
    body = {
        "expectedTargetSnapshotId": str(target.current_snapshot_id),
        "expectedTargetConfigHash": target.current_config_hash,
        "requestKey": "review-disconnect",
        "reason": "Disconnect original credentials",
        "reauthProofId": str(proof.id),
    }
    context = AuditContext.for_admin(
        request=request, reason_code=body["reason"], request_key=body["requestKey"]
    )
    decision = services.disconnect_target(
        str(target.id), body, request=request, audit_context=context
    )
    return target, decision, request


def test_pending_disconnect_prevents_replacement_credentials(disconnect_subject):
    target, decision, request = disconnect_subject
    context = AuditContext.for_admin(
        request=request,
        reason_code="Replace credentials",
        request_key="review-replacement",
    )
    with pytest.raises(services.Conflict, match="revocation"):
        services.update_target(
            str(target.id), {"credentialRef": "vault://review/v2"}, audit_context=context
        )


def test_old_disconnect_cannot_select_replacement_credential_material(
    disconnect_subject, monkeypatch
):
    target, decision, request = disconnect_subject
    PublicationTarget.objects.filter(pk=target.id).update(
        credential_ref="vault://review/v2", credential_version="v2"
    )
    target.refresh_from_db()
    services._snapshot_locked(target)
    monkeypatch.setattr(services, "_require_audit_actor", lambda *args: None)
    monkeypatch.setattr(services, "_require_worker_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(services, "_worker_audit_replay", lambda *args, **kwargs: None)
    monkeypatch.setattr(services, "_record_publishing_audit", lambda **kwargs: None)
    with pytest.raises(services.Conflict, match="credential"):
        services.begin_target_credential_revoke(
            str(decision.id), audit_context=SimpleNamespace(event_key="review-event")
        )


@pytest.mark.django_db
def test_distinct_preflight_requests_do_not_reuse_a_completed_job():
    user = get_user_model().objects.create_user(
        email="preflight-review@example.com", password="unused", is_staff=True
    )
    target = PublicationTarget.objects.create(
        channel="wordpress",
        role="primary_canonical",
        environment="test",
        display_name="Review",
        base_url="https://wordpress.example.com",
        username_ref="vault://review/user",
        credential_ref="vault://review/v1",
    )
    services._snapshot_locked(target)
    request = SimpleNamespace(user=user, correlation_id=uuid.uuid4())
    first_context = AuditContext.for_admin(
        request=request, reason_code="Check current connection", request_key="review-preflight-1"
    )
    second_context = AuditContext.for_admin(
        request=request, reason_code="Check current connection", request_key="review-preflight-2"
    )
    first = services.request_target_preflight(str(target.id), audit_context=first_context)
    second = services.request_target_preflight(str(target.id), audit_context=second_context)
    assert second.id != first.id
    assert (
        services.request_target_preflight(str(target.id), audit_context=first_context).id
        == first.id
    )


def test_media_release_keeps_the_already_accepted_future_publication_time(monkeypatch):
    scheduled = datetime(2099, 1, 2, 3, 4, tzinfo=UTC)
    publication = SimpleNamespace(state="pending", scheduled_for=scheduled, save=Mock())
    attempt = SimpleNamespace(
        id=uuid.uuid4(), attempt_no=1, correlation_id=uuid.uuid4(), publication=publication
    )
    events = []
    monkeypatch.setattr(services, "_enqueue_event", lambda *args, **kwargs: events.append(kwargs))
    services._queue_attempt_on_commit(attempt)
    assert events[0]["available_at"] == scheduled
    assert publication.state == "scheduled"
