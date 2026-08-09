from __future__ import annotations

import os
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import ANY, MagicMock, patch

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from apps.publishing import services
from apps.publishing.models import Approval, PublicationAction
from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase
from django.utils import timezone
from wisdome_writer.domain.errors import Conflict, InvalidInput
from wisdome_writer.domain.hashing import sha256_hex


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _intent(**overrides):
    revision = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000101",
        revision_no=3,
        content_hash=SHA_A,
        editorial_policy_hash=SHA_B,
    )
    values = {
        "id": "00000000-0000-0000-0000-000000000102",
        "intent_hash": "0" * 64,
        "article_revision_id": revision.id,
        "article_revision": revision,
        "revision_no": revision.revision_no,
        "revision_content_hash": revision.content_hash,
        "quality_gate_manifest_hash": SHA_C,
        "quality_report_hash": SHA_D,
        "input_evidence_manifest_hash": "e" * 64,
        "target_commands": [],
        "correction_case_id": None,
        "article_id": "00000000-0000-0000-0000-000000000109",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _target(**overrides):
    values = {
        "id": "00000000-0000-0000-0000-000000000103",
        "current_snapshot_id": "00000000-0000-0000-0000-000000000104",
        "current_config_hash": SHA_A,
        "channel": "wordpress",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _command(target=None, **overrides):
    target = target or _target()
    values = {
        "targetId": str(target.id),
        "targetSnapshotId": str(target.current_snapshot_id),
        "targetConfigHash": target.current_config_hash,
        "resolvedAction": PublicationAction.CREATE,
        "canonicalDependencyTargetId": None,
        "targetCommandHash": SHA_B,
    }
    values.update(overrides)
    return values


def _render(intent=None, target=None, **overrides):
    intent = intent or _intent()
    target = target or _target()
    title = "승인된 제목"
    body = "<p>승인된 본문</p>"
    values = {
        "id": "00000000-0000-0000-0000-000000000105",
        "publication_intent_id": intent.id,
        "article_revision_id": intent.article_revision_id,
        "target_id": target.id,
        "target_snapshot_id": target.current_snapshot_id,
        "target_config_hash": target.current_config_hash,
        "render_stage": "preview",
        "title": title,
        "body_html": body,
        "content_hash": sha256_hex({"title": title, "body": body}),
        "template_hash": sha256_hex(
            {
                "channel": target.channel,
                "title": title,
                "body": body,
                "revision": str(intent.article_revision_id),
            }
        ),
        "source_links": ["https://example.com/source"],
        "source_manifest_hash": sha256_hex(
            {
                "inputEvidenceManifestHash": intent.input_evidence_manifest_hash,
                "sourceLinks": ["https://example.com/source"],
            }
        ),
        "included_claim_ids": ["00000000-0000-0000-0000-000000000106"],
        "media_manifest": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _content_subject(command=None, render=None, **overrides):
    command = command or _command()
    render = render or _render()
    values = {
        "kind": "content_preview",
        "action": command["resolvedAction"],
        "renderId": str(render.id),
        "targetId": command["targetId"],
        "targetSnapshotId": command["targetSnapshotId"],
        "targetConfigHash": command["targetConfigHash"],
        "templateHash": render.template_hash,
        "sourceManifestHash": render.source_manifest_hash,
    }
    values.update(overrides)
    return values


class ApprovalMaterialTests(TestCase):
    def test_stable_subject_hash_binds_frozen_editorial_target_and_render_material(self):
        intent = _intent()
        target = _target()
        command = _command(target)
        render = _render(intent, target)
        subject = _content_subject(command, render)

        baseline = services._approval_subject_hash(
            intent=intent,
            target=target,
            command=command,
            subject=subject,
            render=render,
            publication=None,
        )
        self.assertEqual(
            baseline,
            sha256_hex(
                {
                    "schemaVersion": "approval-subject-v3",
                    "intentId": str(intent.id),
                    "intentHash": intent.intent_hash,
                    "articleRevisionId": str(intent.article_revision_id),
                    "revisionNo": intent.revision_no,
                    "revisionContentHash": intent.revision_content_hash,
                    "editorialPolicyHash": intent.article_revision.editorial_policy_hash,
                    "qualityGateManifestHash": intent.quality_gate_manifest_hash,
                    "qualityReportHash": intent.quality_report_hash,
                    "inputEvidenceManifestHash": intent.input_evidence_manifest_hash,
                    "targetId": str(target.id),
                    "targetAction": command["resolvedAction"],
                    "targetSnapshotId": str(command["targetSnapshotId"]),
                    "targetConfigHash": command["targetConfigHash"],
                    "targetCommandHash": command["targetCommandHash"],
                    "actionSubject": subject,
                    "renderMaterial": {
                        "renderId": str(render.id),
                        "contentHash": render.content_hash,
                        "templateHash": render.template_hash,
                        "sourceManifestHash": render.source_manifest_hash,
                        "mediaManifestHash": sha256_hex(render.media_manifest),
                    },
                }
            ),
        )

        changed_revision = _intent(
            article_revision=SimpleNamespace(
                id=intent.article_revision_id,
                revision_no=3,
                content_hash=SHA_A,
                editorial_policy_hash="e" * 64,
            )
        )
        changed_policy = services._approval_subject_hash(
            intent=changed_revision,
            target=target,
            command=command,
            subject=subject,
            render=render,
            publication=None,
        )
        changed_command = services._approval_subject_hash(
            intent=intent,
            target=target,
            command={**command, "targetCommandHash": "f" * 64},
            subject=subject,
            render=render,
            publication=None,
        )

        self.assertNotEqual(baseline, changed_policy)
        self.assertNotEqual(baseline, changed_command)

    def test_tampered_preview_content_is_rejected_even_when_stored_hash_is_unchanged(self):
        intent = _intent()
        target = _target()
        command = _command(target)
        render = _render(intent, target)
        render.body_html = "<p>승인 뒤 변조된 본문</p>"

        with self.assertRaisesRegex(Conflict, "render material"):
            services._approval_subject_hash(
                intent=intent,
                target=target,
                command=command,
                subject=_content_subject(command, render),
                render=render,
                publication=None,
            )

    def test_decision_hash_binds_decision_generation_predecessor_request_and_actor(self):
        base = {
            "subject_hash": SHA_A,
            "decision": Approval.Decision.APPROVED,
            "head_version": 1,
            "supersedes_approval_id": None,
            "request_hash": SHA_B,
            "actor_type": "admin",
            "actor_id": "00000000-0000-0000-0000-000000000107",
            "event_key": None,
            "decision_reason": "검토 완료",
        }

        approved = services._approval_decision_hash(**base)
        self.assertEqual(
            approved,
            sha256_hex(
                {
                    "schemaVersion": "approval-decision-v1",
                    "subjectHash": base["subject_hash"],
                    "decision": base["decision"],
                    "headVersion": base["head_version"],
                    "supersedesApprovalId": None,
                    "requestHash": base["request_hash"],
                    "actorType": base["actor_type"],
                    "actorId": base["actor_id"],
                    "eventKey": None,
                    "reason": base["decision_reason"],
                }
            ),
        )
        revoked = services._approval_decision_hash(
            **{
                **base,
                "decision": Approval.Decision.REVOKED,
                "head_version": 2,
                "supersedes_approval_id": "00000000-0000-0000-0000-000000000108",
                "decision_reason": "승인 철회",
            }
        )

        self.assertNotEqual(approved, revoked)

    def test_transition_matrix_requires_a_new_intent_after_revoke(self):
        allowed = (
            (None, Approval.Decision.APPROVED),
            (None, Approval.Decision.REJECTED),
            (Approval.Decision.REJECTED, Approval.Decision.APPROVED),
            (Approval.Decision.APPROVED, Approval.Decision.REVOKED),
        )
        for previous, requested in allowed:
            with self.subTest(previous=previous, requested=requested):
                services._validate_approval_transition(previous, requested)

        forbidden = (
            (None, Approval.Decision.REVOKED),
            (Approval.Decision.APPROVED, Approval.Decision.REJECTED),
            (Approval.Decision.APPROVED, Approval.Decision.APPROVED),
            (Approval.Decision.REJECTED, Approval.Decision.REJECTED),
            (Approval.Decision.REVOKED, Approval.Decision.APPROVED),
            (Approval.Decision.REVOKED, Approval.Decision.REJECTED),
            (Approval.Decision.REVOKED, Approval.Decision.REVOKED),
        )
        for previous, requested in forbidden:
            with self.subTest(previous=previous, requested=requested):
                with self.assertRaises(Conflict):
                    services._validate_approval_transition(previous, requested)

    def test_content_subject_must_exactly_match_target_snapshot_and_render(self):
        intent = _intent()
        target = _target()
        command = _command(target)
        render = _render(intent, target)
        valid = _content_subject(command, render)

        services._validate_approval_action_subject(
            intent=intent,
            target=target,
            command=command,
            subject=valid,
            render=render,
            publication=None,
            decision_reason="검토 완료",
        )

        invalid_values = {
            "targetId": "00000000-0000-0000-0000-000000000199",
            "targetSnapshotId": "00000000-0000-0000-0000-000000000198",
            "targetConfigHash": SHA_B,
            "renderId": "00000000-0000-0000-0000-000000000197",
            "templateHash": SHA_A,
            "sourceManifestHash": SHA_A,
        }
        for field, invalid in invalid_values.items():
            with self.subTest(field=field), self.assertRaises((Conflict, InvalidInput)):
                services._validate_approval_action_subject(
                    intent=intent,
                    target=target,
                    command=command,
                    subject={**valid, field: invalid},
                    render=render,
                    publication=None,
                    decision_reason="검토 완료",
                )

        with self.assertRaises(InvalidInput):
            services._validate_approval_action_subject(
                intent=intent,
                target=target,
                command=command,
                subject={**valid, "unexpected": True},
                render=render,
                publication=None,
                decision_reason="검토 완료",
            )

    def test_legacy_v1_nonempty_bogus_hash_is_not_an_authorization(self):
        approval = SimpleNamespace(
            approval_material_version="approval-subject-v1",
            approval_subject_hash=SHA_A,
            action_subject={},
            decision=Approval.Decision.APPROVED,
            head_version=1,
            supersedes_approval_id=None,
        )
        self.assertFalse(
            services._approval_matches_frozen_subject(
                approval,
                intent=_intent(),
                target_id=_target().id,
                command=_command(),
            )
        )

    def test_cas_requires_both_latest_id_and_head_version(self):
        latest = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000108",
            head_version=4,
        )

        services._validate_approval_cas(
            latest,
            expected_latest_approval_id=str(latest.id),
            expected_head_version=4,
        )
        for expected_id, expected_version in (
            (str(latest.id), 3),
            ("00000000-0000-0000-0000-000000000199", 4),
            (None, 4),
        ):
            with self.subTest(
                expected_id=expected_id,
                expected_version=expected_version,
            ), self.assertRaises(Conflict):
                services._validate_approval_cas(
                    latest,
                    expected_latest_approval_id=expected_id,
                    expected_head_version=expected_version,
                )

        services._validate_approval_cas(
            None,
            expected_latest_approval_id=None,
            expected_head_version=0,
        )

    def test_unpublish_subject_binds_remote_state_targets_reason_and_correction(self):
        target = _target()
        command = _command(target, resolvedAction=PublicationAction.UNPUBLISH)
        case = SimpleNamespace(
            article_id="00000000-0000-0000-0000-000000000109",
            state="verified",
            subject_hash=SHA_D,
        )
        query = MagicMock()
        query.using.return_value = query
        query.filter.return_value.first.return_value = case
        correction_model = SimpleNamespace(objects=query)
        intent = _intent(
            correction_case_id="00000000-0000-0000-0000-000000000110",
            target_commands=[command],
        )
        publication = SimpleNamespace(
            remote_post_id="remote-1",
            remote_state="published",
            state=PublicationAction.CREATE,
        )
        subject = {
            "kind": "unpublish_command",
            "action": PublicationAction.UNPUBLISH,
            "targetId": str(target.id),
            "targetSnapshotId": command["targetSnapshotId"],
            "targetConfigHash": command["targetConfigHash"],
            "remotePostId": "remote-1",
            "observedRemoteState": "published",
            "reason": "원문 철회",
            "affectedTargetIds": [str(target.id)],
            "correctionEvidenceManifestHash": SHA_D,
        }

        with patch.object(services.apps, "get_model", return_value=correction_model):
            services._validate_approval_action_subject(
                intent=intent,
                target=target,
                command=command,
                subject=subject,
                render=None,
                publication=publication,
                decision_reason="원문 철회",
            )
            for field, invalid in (
                ("observedRemoteState", "draft"),
                ("affectedTargetIds", []),
                ("reason", ""),
                ("correctionEvidenceManifestHash", SHA_A),
            ):
                with self.subTest(field=field), self.assertRaises(
                    (Conflict, InvalidInput)
                ):
                    services._validate_approval_action_subject(
                        intent=intent,
                        target=target,
                        command=command,
                        subject={**subject, field: invalid},
                        render=None,
                        publication=publication,
                        decision_reason="원문 철회",
                    )

class ReauthenticationScopeTests(TestCase):
    def test_approval_revoke_has_a_dedicated_reauthentication_scope(self):
        from apps.accounts.services import ALLOWED_ACTION_SCOPES

        self.assertIn("approval_revoke", ALLOWED_ACTION_SCOPES)

    def test_revoke_attempt_state_policy_is_exact(self):
        from apps.publishing.models import PublicationAttempt

        self.assertEqual(
            services._REVOKE_BLOCKING_ATTEMPT_STATES,
            {
                PublicationAttempt.State.RUNNING,
                PublicationAttempt.State.UNKNOWN_OUTCOME,
                PublicationAttempt.State.RECONCILING,
            },
        )
        self.assertEqual(
            services._REVOKE_STALEABLE_ATTEMPT_STATES,
            {
                PublicationAttempt.State.QUEUED,
                PublicationAttempt.State.RETRYABLE_FAILED,
            },
        )


class ApprovalModeActorContractTests(TestCase):
    def test_mode_decision_and_actor_matrix_is_fail_closed(self):
        from apps.publishing.models import ApprovalMode

        admin = SimpleNamespace(actor_type="admin")
        worker = SimpleNamespace(actor_type="worker")

        for decision in Approval.Decision.values:
            with self.subTest(mode="manual", decision=decision):
                services._validate_approval_mode_actor(
                    mode=ApprovalMode.MANUAL,
                    decision=decision,
                    audit_context=admin,
                )
                with self.assertRaises(Conflict):
                    services._validate_approval_mode_actor(
                        mode=ApprovalMode.MANUAL,
                        decision=decision,
                        audit_context=worker,
                    )

        services._validate_approval_mode_actor(
            mode=ApprovalMode.VALIDATED_AUTO,
            decision=Approval.Decision.APPROVED,
            audit_context=worker,
        )
        with self.assertRaises(Conflict):
            services._validate_approval_mode_actor(
                mode=ApprovalMode.VALIDATED_AUTO,
                decision=Approval.Decision.APPROVED,
                audit_context=admin,
            )
        for decision in (Approval.Decision.REJECTED, Approval.Decision.REVOKED):
            with self.subTest(mode="validated_auto", decision=decision):
                services._validate_approval_mode_actor(
                    mode=ApprovalMode.VALIDATED_AUTO,
                    decision=decision,
                    audit_context=admin,
                )
                with self.assertRaises(Conflict):
                    services._validate_approval_mode_actor(
                        mode=ApprovalMode.VALIDATED_AUTO,
                        decision=decision,
                        audit_context=worker,
                    )


class ApprovalRequestIdentityTests(TestCase):
    def test_request_hash_binds_article_target_and_canonical_payload(self):
        data = ApprovalReplayOrderingTests()._request()
        first = services._approval_request_hash(
            article_id="00000000-0000-0000-0000-000000000109",
            target_id=data["actionSubject"]["targetId"],
            payload=data,
        )
        self.assertNotEqual(
            first,
            services._approval_request_hash(
                article_id="00000000-0000-0000-0000-000000000110",
                target_id=data["actionSubject"]["targetId"],
                payload=data,
            ),
        )
        self.assertNotEqual(
            first,
            services._approval_request_hash(
                article_id="00000000-0000-0000-0000-000000000109",
                target_id="00000000-0000-0000-0000-000000000111",
                payload=data,
            ),
        )


class ApprovalReplayOrderingTests(TestCase):
    def _request(self):
        return {
            "revisionNo": 3,
            "publicationIntentId": "00000000-0000-0000-0000-000000000102",
            "expectedLatestApprovalId": None,
            "expectedHeadVersion": 0,
            "requestKey": "approval-request-0001",
            "reauthProofId": None,
            "actionSubject": {
                "kind": "content_preview",
                "action": "create",
                "renderId": "00000000-0000-0000-0000-000000000105",
                "targetId": "00000000-0000-0000-0000-000000000103",
                "targetSnapshotId": "00000000-0000-0000-0000-000000000104",
                "targetConfigHash": SHA_A,
                "templateHash": SHA_C,
                "sourceManifestHash": SHA_D,
            },
            "decision": "approved",
            "decisionReason": "검토 완료",
        }

    def test_exact_replay_returns_stored_decision_before_live_eligibility_or_cas(self):
        data = self._request()
        user = SimpleNamespace(pk="00000000-0000-0000-0000-000000000107")
        article_id = "00000000-0000-0000-0000-000000000109"
        existing = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000108",
            request_hash=services._approval_request_hash(
                article_id=article_id,
                target_id=data["actionSubject"]["targetId"],
                payload=data,
            ),
            admin_id=user.pk,
            decision_actor_type="admin",
            decision_actor_id=user.pk,
            decision_event_key=None,
            mode="manual",
            decision="approved",
        )
        query = MagicMock()
        query.select_related.return_value.first.return_value = existing
        audit_context = SimpleNamespace(
            actor_type="admin",
            actor_id=user.pk,
            request_key=data["requestKey"],
            reason_code=data["decisionReason"],
            event_key=None,
        )

        with (
            patch.object(
                services.Approval.objects,
                "filter",
                return_value=query,
            ) as approval_filter,
            patch.object(
                services.PublicationIntent.objects,
                "select_related",
                side_effect=AssertionError("live intent must not be read on replay"),
            ),
            patch.object(services, "require_audit_replay"),
        ):
            observed, created = services._decide_approval_atomic.__wrapped__(
                article_id,
                data["actionSubject"]["targetId"],
                data,
                user=user,
                audit_context=audit_context,
                request=None,
            )

        self.assertIs(observed, existing)
        self.assertFalse(created)
        approval_filter.assert_called_once_with(
            publication_intent_id=data["publicationIntentId"],
            publication_intent__article_id=article_id,
            target_id=data["actionSubject"]["targetId"],
            request_key=data["requestKey"],
        )

    def test_exact_legacy_body_hash_replays_without_mutating_the_stored_row(self):
        data = self._request()
        user = SimpleNamespace(pk="00000000-0000-0000-0000-000000000107")
        article_id = "00000000-0000-0000-0000-000000000109"
        legacy_hash = services._request_hash(data)
        existing = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000108",
            request_hash=legacy_hash,
            admin_id=user.pk,
            decision_actor_type="admin",
            decision_actor_id=user.pk,
            decision_event_key=None,
            mode="manual",
            decision="approved",
        )
        query = MagicMock()
        query.select_related.return_value.first.return_value = existing
        audit_context = SimpleNamespace(
            actor_type="admin",
            actor_id=user.pk,
            request_key=data["requestKey"],
            reason_code=data["decisionReason"],
            event_key=None,
        )

        with (
            patch.object(services.Approval.objects, "filter", return_value=query),
            patch.object(services, "require_audit_replay") as replay,
        ):
            observed, created = services._decide_approval_atomic.__wrapped__(
                article_id,
                data["actionSubject"]["targetId"],
                data,
                user=user,
                audit_context=audit_context,
                request=None,
            )

        self.assertIs(observed, existing)
        self.assertFalse(created)
        self.assertEqual(existing.request_hash, legacy_hash)
        self.assertEqual(replay.call_args.kwargs["request_hash"], legacy_hash)

    def test_historical_reason_payload_replays_but_cannot_create_a_new_decision(self):
        data = self._request()
        data["reason"] = data.pop("decisionReason")
        data.pop("expectedHeadVersion")
        user = SimpleNamespace(pk="00000000-0000-0000-0000-000000000107")
        article_id = "00000000-0000-0000-0000-000000000109"
        legacy_hash = services._request_hash(data)
        existing = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000108",
            request_hash=legacy_hash,
            admin_id=user.pk,
            decision_actor_type="admin",
            decision_actor_id=user.pk,
            decision_event_key=None,
            mode="manual",
            decision="approved",
        )
        query = MagicMock()
        query.select_related.return_value.first.return_value = existing
        audit_context = SimpleNamespace(
            actor_type="admin",
            actor_id=user.pk,
            request_key=data["requestKey"],
            reason_code=data["reason"],
            event_key=None,
        )

        with (
            patch.object(services.Approval.objects, "filter", return_value=query),
            patch.object(services, "require_audit_replay") as replay,
        ):
            observed, created = services._decide_approval_atomic.__wrapped__(
                article_id,
                data["actionSubject"]["targetId"],
                data,
                user=user,
                audit_context=audit_context,
                request=None,
            )

        self.assertIs(observed, existing)
        self.assertFalse(created)
        self.assertEqual(replay.call_args.kwargs["request_hash"], legacy_hash)

    def test_legacy_reason_field_is_not_accepted_by_the_direct_service(self):
        data = self._request()
        data["reason"] = data.pop("decisionReason")
        user = SimpleNamespace(pk="00000000-0000-0000-0000-000000000107")
        audit_context = SimpleNamespace(
            actor_type="admin",
            actor_id=user.pk,
            request_key=data["requestKey"],
            reason_code="",
            event_key=None,
        )

        query = MagicMock()
        query.select_related.return_value.first.return_value = None
        with (
            patch.object(services.Approval.objects, "filter", return_value=query),
            self.assertRaisesRegex(InvalidInput, "decisionReason"),
        ):
            services._decide_approval_atomic.__wrapped__(
                "00000000-0000-0000-0000-000000000109",
                data["actionSubject"]["targetId"],
                data,
                user=user,
                audit_context=audit_context,
                request=None,
            )

    def test_reused_request_key_with_different_payload_conflicts_before_live_checks(self):
        data = self._request()
        user = SimpleNamespace(pk="00000000-0000-0000-0000-000000000107")
        existing = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000108",
            request_hash=SHA_A,
            admin_id=user.pk,
            decision_actor_type="admin",
            decision_actor_id=user.pk,
            decision_event_key=None,
            mode="manual",
            decision="approved",
        )
        query = MagicMock()
        query.select_related.return_value.first.return_value = existing
        audit_context = SimpleNamespace(
            actor_type="admin",
            actor_id=user.pk,
            request_key=data["requestKey"],
            reason_code=data["decisionReason"],
            event_key=None,
        )

        with (
            patch.object(services.Approval.objects, "filter", return_value=query),
            patch.object(
                services.PublicationIntent.objects,
                "select_related",
                side_effect=AssertionError("live intent must not be read on conflict"),
            ),
        ):
            with self.assertRaises(Conflict):
                services._decide_approval_atomic.__wrapped__(
                    "00000000-0000-0000-0000-000000000109",
                    data["actionSubject"]["targetId"],
                    data,
                    user=user,
                    audit_context=audit_context,
                    request=None,
                )


class ApprovalProjectionTests(TestCase):
    def test_readonly_projection_uses_the_current_head_and_never_dispatches_revoke(self):
        approval_id = "00000000-0000-0000-0000-000000000108"
        actor_id = "00000000-0000-0000-0000-000000000107"
        decision_hash = services._approval_decision_hash(
            subject_hash=SHA_A,
            decision=Approval.Decision.REVOKED,
            head_version=2,
            supersedes_approval_id="00000000-0000-0000-0000-000000000106",
            request_hash=SHA_B,
            actor_type="admin",
            actor_id=actor_id,
            event_key=None,
            decision_reason="승인 철회",
        )
        current = SimpleNamespace(
            id=approval_id,
            publication_intent_id="00000000-0000-0000-0000-000000000102",
            target_id="00000000-0000-0000-0000-000000000103",
            approval_subject_hash=SHA_A,
            decision_hash=decision_hash,
            decision=Approval.Decision.REVOKED,
            head_version=2,
            supersedes_approval_id="00000000-0000-0000-0000-000000000106",
            request_hash=SHA_B,
            decision_actor_type="admin",
            decision_actor_id=actor_id,
            decision_event_key=None,
            admin_id=actor_id,
            decision_reason="승인 철회",
        )
        approval = SimpleNamespace(
            id=approval_id,
            publication_intent_id=current.publication_intent_id,
            target_id=current.target_id,
        )
        head = SimpleNamespace(
            latest_approval=current,
            version=2,
            subject_hash=SHA_A,
            updated_at="2026-08-09T00:00:00Z",
        )
        approval_query = MagicMock()
        approval_query.select_related.return_value.get.return_value = approval
        head_query = MagicMock()
        head_query.select_related.return_value.filter.return_value.first.return_value = head

        with (
            patch.object(services.Approval.objects, "using", return_value=approval_query),
            patch.object(
                services.PublicationApprovalHead.objects,
                "using",
                return_value=head_query,
            ),
        ):
            projection = services.evaluate_approval_decision_readonly(
                approval_id=approval_id
            )

        self.assertEqual(projection.current_head_approval_id, approval_id)
        self.assertEqual(projection.current_head_version, 2)
        self.assertTrue(projection.is_current)
        self.assertFalse(projection.dispatch_eligible)

    def test_readonly_projection_rejects_corrupt_head_instead_of_soft_false(self):
        approval_id = "00000000-0000-0000-0000-000000000108"
        current = SimpleNamespace(
            id=approval_id,
            publication_intent_id="00000000-0000-0000-0000-000000000102",
            target_id="00000000-0000-0000-0000-000000000103",
            approval_subject_hash=SHA_A,
            decision_hash=SHA_D,
            decision=Approval.Decision.APPROVED,
            head_version=1,
            supersedes_approval_id=None,
            request_hash=SHA_B,
            decision_actor_type="admin",
            decision_actor_id="00000000-0000-0000-0000-000000000107",
            decision_event_key=None,
            admin_id="00000000-0000-0000-0000-000000000107",
            decision_reason="exact decision",
        )
        approval = SimpleNamespace(
            id=approval_id,
            publication_intent_id=current.publication_intent_id,
            target_id=current.target_id,
        )
        head = SimpleNamespace(
            latest_approval=current,
            version=2,
            subject_hash=SHA_C,
            updated_at="2026-08-09T00:00:00Z",
        )
        approval_query = MagicMock()
        approval_query.select_related.return_value.get.return_value = approval
        head_query = MagicMock()
        head_query.select_related.return_value.filter.return_value.first.return_value = head

        with (
            patch.object(services.Approval.objects, "using", return_value=approval_query),
            patch.object(
                services.PublicationApprovalHead.objects,
                "using",
                return_value=head_query,
            ),
            self.assertRaisesRegex(Conflict, "immutable decision"),
        ):
            services.evaluate_approval_decision_readonly(approval_id=approval_id)

    def test_validated_auto_projection_is_false_after_activation_is_disabled(self):
        approval_id = uuid.uuid4()
        actor_owner_id = uuid.uuid4()
        target_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        revision = SimpleNamespace(
            id=uuid.uuid4(),
            revision_no=1,
            content_hash=SHA_A,
            editorial_policy_hash=SHA_B,
        )
        command = {
            "targetId": str(target_id),
            "targetSnapshotId": str(snapshot_id),
            "targetConfigHash": SHA_C,
            "resolvedAction": PublicationAction.CREATE,
            "canonicalDependencyTargetId": None,
            "targetCommandHash": SHA_D,
        }
        target_ref = {
            "targetId": str(target_id),
            "targetSnapshotId": str(snapshot_id),
            "targetConfigHash": SHA_C,
        }
        intent = SimpleNamespace(
            id=uuid.uuid4(),
            article_id=uuid.uuid4(),
            article_revision=revision,
            article_revision_id=revision.id,
            revision_no=revision.revision_no,
            revision_content_hash=revision.content_hash,
            generation_attempt_id=uuid.uuid4(),
            input_evidence_manifest_hash=SHA_A,
            generation_pipeline_manifest_hash=SHA_B,
            quality_gate_manifest_hash=SHA_C,
            quality_report_hash=SHA_D,
            intent_hash="e" * 64,
            target_snapshot_refs=[target_ref],
            target_commands=[command],
            approval_mode="validated_auto",
            auto_publish_activation_refs=[
                {
                    "targetId": str(target_id),
                    "activationId": str(uuid.uuid4()),
                    "version": 1,
                    "activationHash": "f" * 64,
                    "targetSnapshotId": str(snapshot_id),
                }
            ],
            state="approved",
        )
        target = SimpleNamespace(
            id=target_id,
            current_snapshot_id=snapshot_id,
            current_config_hash=SHA_C,
            auto_publish_enabled=False,
            latest_auto_publish_activation_id=None,
            connection_state="verified",
            environment="test",
        )
        decision_hash = services._approval_decision_hash(
            subject_hash=SHA_A,
            decision=Approval.Decision.APPROVED,
            head_version=1,
            supersedes_approval_id=None,
            request_hash=SHA_B,
            actor_type="worker",
            actor_id=None,
            event_key=uuid.uuid4(),
            decision_reason="validated auto approval",
        )
        approval = SimpleNamespace(
            id=approval_id,
            publication_intent=intent,
            publication_intent_id=intent.id,
            article_revision_id=revision.id,
            target=target,
            target_id=target.id,
            target_snapshot_id=snapshot_id,
            target_config_hash=SHA_C,
            target_action=PublicationAction.CREATE,
            policy_snapshot_hash=revision.editorial_policy_hash,
            quality_report_hash=intent.quality_report_hash,
            approval_subject_hash=SHA_A,
            decision_hash=decision_hash,
            decision=Approval.Decision.APPROVED,
            head_version=1,
            supersedes_approval_id=None,
            request_hash=SHA_B,
            decision_actor_type="worker",
            decision_actor_id=None,
            decision_event_key=str(uuid.uuid4()),
            admin_id=actor_owner_id,
            decision_reason="validated auto approval",
        )
        approval.decision_hash = services._approval_decision_hash(
            subject_hash=approval.approval_subject_hash,
            decision=approval.decision,
            head_version=approval.head_version,
            supersedes_approval_id=None,
            request_hash=approval.request_hash,
            actor_type=approval.decision_actor_type,
            actor_id=None,
            event_key=approval.decision_event_key,
            decision_reason=approval.decision_reason,
        )
        head = SimpleNamespace(
            latest_approval=approval,
            version=1,
            subject_hash=SHA_A,
            updated_at=timezone.now(),
        )
        approval_query = MagicMock()
        approval_query.select_related.return_value.get.return_value = approval
        head_query = MagicMock()
        head_query.select_related.return_value.filter.return_value.first.return_value = head
        latest_intent_query = MagicMock()
        latest_intent_query.filter.return_value.order_by.return_value.values_list.return_value.first.return_value = intent.id

        material = {
            "revisionContentHash": revision.content_hash,
            "generationAttemptId": str(intent.generation_attempt_id),
            "inputEvidenceManifestHash": intent.input_evidence_manifest_hash,
            "generationPipelineManifestHash": intent.generation_pipeline_manifest_hash,
            "qualityGateManifestHash": intent.quality_gate_manifest_hash,
            "qualityReportHash": intent.quality_report_hash,
        }
        with (
            patch.object(services.Approval.objects, "using", return_value=approval_query),
            patch.object(
                services.PublicationApprovalHead.objects,
                "using",
                return_value=head_query,
            ),
            patch.object(
                services.PublicationIntent.objects,
                "using",
                return_value=latest_intent_query,
            ),
            patch.object(services, "_intent_material_data", return_value=material),
            patch.object(services, "_intent_hash", return_value=intent.intent_hash),
            patch.object(services, "_approval_matches_frozen_subject", return_value=True),
            patch.object(services, "_require_revision_for_commands"),
            patch.object(services, "_kill_switch_enabled", return_value=False),
        ):
            projection = services.evaluate_approval_decision_readonly(
                approval_id=approval_id
            )

        self.assertFalse(projection.dispatch_eligible)


class RevokedAttemptConvergenceTests(TestCase):
    def test_revoke_scope_includes_every_intent_for_same_article_and_target(self):
        from apps.publishing.models import PublicationAttempt

        rows = [
            SimpleNamespace(id=uuid.uuid4(), state=PublicationAttempt.State.RUNNING),
            SimpleNamespace(id=uuid.uuid4(), state=PublicationAttempt.State.QUEUED),
        ]
        scope_query = MagicMock()
        scope_query.order_by.return_value.values_list.return_value.distinct.return_value = []
        query = MagicMock()
        related_query = query.select_related.return_value
        related_query.filter.return_value.order_by.return_value = rows

        with patch.object(
            services.PublicationAttempt.objects,
            "select_for_update",
            return_value=query,
        ), patch.object(
            services.PublicationAttempt.objects,
            "filter",
            return_value=scope_query,
        ):
            blocking, staleable = services._lock_revoke_attempts_for_article_target(
                article_id="00000000-0000-0000-0000-000000000109",
                target_id="00000000-0000-0000-0000-000000000103",
            )

        related_query.filter.assert_called_once_with(
            publication_intent__article_id="00000000-0000-0000-0000-000000000109",
            publication__target_id="00000000-0000-0000-0000-000000000103",
            state__in=(
                services._REVOKE_BLOCKING_ATTEMPT_STATES
                | services._REVOKE_STALEABLE_ATTEMPT_STATES
            ),
        )
        self.assertEqual(blocking, rows[:1])
        self.assertEqual(staleable, rows[1:])

    def test_revoked_stale_delivery_records_skip_and_projects_run_idempotently(self):
        from apps.publishing.models import PublicationAttempt

        attempt = SimpleNamespace(
            id=uuid.uuid4(),
            pk=None,
            _meta=SimpleNamespace(label_lower="publishing.publicationattempt"),
            state=PublicationAttempt.State.STALE,
            error_code="approval_revoked",
            attempt_no=1,
        )
        attempt.pk = attempt.id
        audit_context = SimpleNamespace(event_key="event-1")

        with (
            patch.object(services, "_worker_audit_replay", return_value=None),
            patch.object(services, "_record_publishing_audit") as record,
            patch.object(services, "_release_article_external_write_fence_locked") as project,
        ):
            services._converge_revoked_attempt_redelivery_locked(
                attempt,
                audit_context=audit_context,
            )

        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["action"], "publication_attempt.skipped")
        project.assert_called_once_with(attempt)

    def test_delivery_dlq_converges_an_exact_revoked_stale_attempt(self):
        from apps.publishing.models import PublicationAttempt

        intent = SimpleNamespace(
            article_id=uuid.uuid4(),
            target_commands=[],
        )
        attempt = SimpleNamespace(
            id=uuid.uuid4(),
            state=PublicationAttempt.State.STALE,
            error_code="approval_revoked",
            attempt_no=1,
            publication_intent=intent,
        )
        preliminary_query = MagicMock()
        preliminary_query.filter.return_value.first.return_value = attempt
        audit_context = SimpleNamespace(actor_type="worker", event_key=str(uuid.uuid4()))

        with (
            patch.object(services, "_require_worker_event"),
            patch.object(
                services.PublicationAttempt.objects,
                "select_related",
                return_value=preliminary_query,
            ),
            patch.object(services, "_lock_article_external_write_fence"),
            patch.object(services, "_lock_target_intent_fences"),
            patch.object(
                services,
                "_lock_publication_attempt_domain",
                return_value=attempt,
            ),
            patch.object(services, "_worker_audit_replay", return_value=None),
            patch.object(
                services,
                "_converge_revoked_attempt_redelivery_locked",
            ) as converge,
        ):
            observed = services.finalize_publication_delivery_failure.__wrapped__(
                str(attempt.id),
                error_code="delivery_exhausted",
                audit_context=audit_context,
            )

        self.assertIs(observed, attempt)
        converge.assert_called_once_with(attempt, audit_context=audit_context)


class AttemptOriginRunGateTests(TestCase):
    def test_failed_origin_run_blocks_a_queued_attempt_before_external_io(self):
        target = SimpleNamespace(
            id=uuid.uuid4(),
            channel="wordpress",
            current_snapshot_id=uuid.uuid4(),
            current_config_hash=SHA_A,
            publisher_adapter_manifest_hash=services.ADAPTER_MANIFESTS["wordpress"],
            connection_state="verified",
            environment="test",
        )
        command = {
            "targetId": str(target.id),
            "targetSnapshotId": str(target.current_snapshot_id),
            "targetConfigHash": target.current_config_hash,
            "resolvedAction": PublicationAction.CREATE,
            "canonicalDependencyTargetId": None,
            "targetCommandHash": SHA_B,
        }
        intent = SimpleNamespace(
            id=uuid.uuid4(),
            article_id=uuid.uuid4(),
            origin_collection_run_id=uuid.uuid4(),
            state="dispatched",
            target_commands=[command],
            approval_mode="manual",
        )
        approval = SimpleNamespace(
            id=uuid.uuid4(),
            decision=Approval.Decision.APPROVED,
            approval_subject_hash=SHA_C,
            target_action=PublicationAction.CREATE,
        )
        attempt = SimpleNamespace(
            publication_intent=intent,
            publication=SimpleNamespace(target=target),
            target_snapshot_id=target.current_snapshot_id,
            target_config_hash=target.current_config_hash,
            publisher_adapter_manifest_hash=target.publisher_adapter_manifest_hash,
            approval=approval,
            approval_subject_hash=approval.approval_subject_hash,
            resolved_action=PublicationAction.CREATE,
        )
        latest_query = MagicMock()
        latest_query.order_by.return_value.first.return_value = intent
        run_model = services.apps.get_model("collection", "CollectionRun")
        run_query = MagicMock()
        run_query.filter.return_value.first.return_value = SimpleNamespace(
            state="failed",
            stop_requested_at=None,
        )

        with (
            patch.object(services, "_kill_switch_enabled", return_value=False),
            patch.object(
                services.PublicationIntent.objects,
                "filter",
                return_value=latest_query,
            ),
            patch.object(services, "_latest_approval_locked", return_value=approval),
            patch.object(services, "_approval_matches_frozen_subject", return_value=True),
            patch.object(run_model.objects, "filter", return_value=run_query.filter.return_value),
            self.assertRaisesRegex(Conflict, "run"),
        ):
            services.validate_attempt_gate(attempt)


class CanonicalDependencyTests(TestCase):
    def _attempt(self):
        dependency_id = uuid.uuid4()
        blogger_id = uuid.uuid4()
        intent = SimpleNamespace(
            id=uuid.uuid4(),
            target_commands=[
                {
                    "targetId": str(blogger_id),
                    "canonicalDependencyTargetId": str(dependency_id),
                    "resolvedAction": PublicationAction.CREATE,
                }
            ],
        )
        target = SimpleNamespace(id=blogger_id, channel="blogger")
        return (
            SimpleNamespace(
                resolved_action=PublicationAction.CREATE,
                publication_intent=intent,
                publication=SimpleNamespace(
                    article_id=uuid.uuid4(),
                    target=target,
                ),
            ),
            dependency_id,
        )

    def test_readiness_queries_the_exact_frozen_wordpress_dependency(self):
        attempt, dependency_id = self._attempt()
        query = MagicMock()
        query.exists.return_value = True

        with patch.object(
            services.Publication.objects,
            "filter",
            return_value=query,
        ) as publication_filter:
            self.assertTrue(services._wordpress_dependency_ready(attempt))

        publication_filter.assert_called_once_with(
            article_id=attempt.publication.article_id,
            target_id=dependency_id,
            target__channel="wordpress",
            state="published",
            canonical_ready_at__isnull=False,
        )


class OriginRunProjectionTests(TestCase):
    def test_terminal_projection_waits_for_every_dispatched_intent_in_the_run(self):
        from apps.publishing.models import PublicationAttempt

        run = SimpleNamespace(
            id=uuid.uuid4(),
            state="publishing",
            save=MagicMock(),
        )
        first_intent = SimpleNamespace(
            id=uuid.uuid4(),
            origin_collection_run_id=run.id,
            target_commands=[{"targetId": "a"}],
            state="dispatched",
        )
        second_intent = SimpleNamespace(
            id=uuid.uuid4(),
            origin_collection_run_id=run.id,
            target_commands=[{"targetId": "a"}, {"targetId": "b"}],
            state="dispatched",
        )
        stale = SimpleNamespace(
            id=uuid.uuid4(),
            publication_intent=first_intent,
            publication_intent_id=first_intent.id,
            state=PublicationAttempt.State.STALE,
            publication=SimpleNamespace(
                target=SimpleNamespace(channel="blogger")
            ),
        )
        queued = SimpleNamespace(
            id=uuid.uuid4(),
            publication_intent=second_intent,
            publication_intent_id=second_intent.id,
            state=PublicationAttempt.State.QUEUED,
        )
        run_query = MagicMock()
        run_query.get.return_value = run
        intent_query = MagicMock()
        intent_query.filter.return_value.order_by.return_value = [
            first_intent,
            second_intent,
        ]
        attempt_query = MagicMock()

        def attempts_for_scope(**kwargs):
            result = MagicMock()
            if "publication_intent" in kwargs:
                result.order_by.return_value = [stale]
            else:
                result.order_by.return_value = [stale, queued]
            return result

        attempt_query.filter.side_effect = attempts_for_scope

        with (
            patch.object(
                services.apps.get_model("collection", "CollectionRun").objects,
                "select_for_update",
                return_value=run_query,
            ),
            patch.object(
                services.PublicationIntent.objects,
                "select_for_update",
                return_value=intent_query,
            ),
            patch.object(
                services.PublicationAttempt.objects,
                "select_for_update",
                return_value=attempt_query,
            ),
            patch(
                "apps.collection.services.project_run_terminal_observation"
            ),
        ):
            services._project_origin_run_terminal_locked(stale)

        self.assertEqual(run.state, "publishing")


class FinalRenderReplayTests(TestCase):
    def test_existing_final_render_must_match_the_full_approved_material(self):
        from apps.publishing.models import ArticleChannelRender

        intent = SimpleNamespace(id=uuid.uuid4())
        target = SimpleNamespace(id=uuid.uuid4(), channel="wordpress")
        preview = SimpleNamespace(
            id=uuid.uuid4(),
            publication_intent_id=intent.id,
            article_revision_id=uuid.uuid4(),
            target_id=target.id,
            target_snapshot_id=uuid.uuid4(),
            target_config_hash=SHA_A,
            channel_role="primary_canonical",
            title="title",
            body_html="<p>body</p>",
            labels=["one"],
            source_links=["https://example.com/source"],
            included_claim_ids=[str(uuid.uuid4())],
            template_hash=SHA_B,
            source_manifest_hash=SHA_C,
            media_manifest=[{"assetId": str(uuid.uuid4())}],
            correction_history=[],
        )
        existing = SimpleNamespace(
            publication_intent_id=intent.id,
            article_revision_id=preview.article_revision_id,
            target_id=target.id,
            target_snapshot_id=preview.target_snapshot_id,
            target_config_hash=preview.target_config_hash,
            channel_role=preview.channel_role,
            render_stage=ArticleChannelRender.Stage.FINAL,
            title=preview.title,
            body_html=preview.body_html,
            labels=["tampered"],
            source_links=preview.source_links,
            included_claim_ids=preview.included_claim_ids,
            canonical_source_url=None,
            canonical_link_state=ArticleChannelRender.CanonicalState.NOT_APPLICABLE,
            template_hash=preview.template_hash,
            content_hash=sha256_hex({"title": preview.title, "body": preview.body_html}),
            source_manifest_hash=preview.source_manifest_hash,
            media_manifest=preview.media_manifest,
            correction_history=preview.correction_history,
        )
        query = MagicMock()
        query.first.return_value = existing
        attempt = SimpleNamespace(
            resolved_action=PublicationAction.CREATE,
            approval=SimpleNamespace(article_channel_render=preview),
            publication_intent=intent,
            publication=SimpleNamespace(target=target, article_id=uuid.uuid4()),
        )

        with (
            patch.object(
                services.ArticleChannelRender.objects,
                "filter",
                return_value=query,
            ),
            self.assertRaisesRegex(Conflict, "final render"),
        ):
            services._final_render(attempt)


class ApprovalDecisionDatabaseTests(DjangoTestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.collection.models import CollectionRun
        from apps.editorial.models import (
            ArticleRevision,
            DraftArticle,
            EditorialPolicySnapshot,
        )
        from apps.publishing.models import (
            ArticleChannelRender,
            PublicationIntent,
            PublicationTarget,
            PublicationTargetSnapshot,
        )
        from apps.topics.models import SourceRegistrySnapshot, TopicPolicy

        now = timezone.now()
        user = get_user_model().objects.create_user(
            email="t019-service@example.com",
            password="not-a-real-secret",
            is_staff=True,
        )
        topic_policy = TopicPolicy.objects.create(
            code="housing_subscription",
            version=919,
            title="T019 service policy",
            freshness_minutes=60,
            policy={},
            policy_hash=SHA_A,
        )
        registry = SourceRegistrySnapshot.objects.create(
            topic_code="housing_subscription",
            version=919,
            manifest_hash=SHA_A,
        )
        run = CollectionRun.objects.create(
            display_id="RUN-T019-SERVICE",
            topic_code="housing_subscription",
            window_start=now - timedelta(hours=1),
            window_end=now,
            source_registry=registry,
            registry_manifest_hash=SHA_A,
            topic_policy=topic_policy,
            policy_version=topic_policy.version,
            policy_hash=SHA_A,
            freshness_minutes=60,
            allowed_authority_tiers=["primary_official"],
            freshness_cutoff=now - timedelta(hours=1),
            request_fingerprint=SHA_B,
        )
        policy_snapshot = EditorialPolicySnapshot(
            topic_code="housing_subscription",
            policy_key="t019-service",
            policy_version="1",
            document={},
            release_document_hash=SHA_A,
            config_hash=SHA_A,
            implementation_manifest={},
            implementation_manifest_hash=SHA_A,
            material_hash=SHA_A,
        )
        EditorialPolicySnapshot.objects.bulk_create([policy_snapshot])
        article = DraftArticle.objects.create(
            article_identity_key="t019-service-article",
            topic_code="housing_subscription",
            article_type="housing_notice",
            source_run=run,
        )
        revision = ArticleRevision.objects.create(
            article=article,
            origin_run=run,
            revision_no=1,
            editorial_policy_snapshot=policy_snapshot,
            editorial_policy_version="1",
            editorial_policy_hash=SHA_B,
            verification_manifest_hash=SHA_C,
            evidence_manifest_hash=SHA_D,
            exclusion_manifest_hash="e" * 64,
            title="승인 본문",
            summary="승인 요약",
            body_markdown="승인 본문",
            content_hash=SHA_A,
            input_manifest_hash=SHA_D,
            claim_manifest_hash="f" * 64,
            quality_manifest_hash="1" * 64,
            quality_gate_manifest_hash="2" * 64,
            quality_report_hash="3" * 64,
            quality_state="passed",
        )
        target = PublicationTarget.objects.create(
            channel="wordpress",
            role="primary_canonical",
            environment="test",
            display_name="T019 service target",
            base_url="https://example.com/",
            connection_state="verified",
            current_config_hash="6" * 64,
            publisher_adapter_manifest_hash="5" * 64,
        )
        snapshot = PublicationTargetSnapshot.objects.create(
            target=target,
            version=1,
            channel=target.channel,
            role=target.role,
            environment=target.environment,
            base_url=target.base_url,
            credential_ref_identity_hash="4" * 64,
            connection_state="verified",
            preflight_state="passed",
            canary_state="passed",
            pilot_state="passed",
            publisher_contract_version="publisher-v1",
            publisher_adapter_manifest_hash="5" * 64,
            config_hash=target.current_config_hash,
        )
        target.current_snapshot_id = snapshot.id
        target.save(update_fields=("current_snapshot_id", "current_config_hash"))
        raw_command = {
            "targetId": str(target.id),
            "targetSnapshotId": str(snapshot.id),
            "targetConfigHash": snapshot.config_hash,
            "resolvedAction": "create",
            "canonicalDependencyTargetId": None,
        }
        command = services._command_map([raw_command])[str(target.id)]
        target_ref = {
            "targetId": str(target.id),
            "targetSnapshotId": str(snapshot.id),
            "targetConfigHash": snapshot.config_hash,
        }
        material = services._revision_publication_material(revision)
        intent_data = {
            **material,
            "approvalMode": "manual",
            "autoPublishValidationRefs": [],
            "autoPublishActivationRefs": [],
            "correctionCaseId": None,
        }
        intent = PublicationIntent.objects.create(
            article_id=article.id,
            article_revision=revision,
            revision_no=revision.revision_no,
            revision_content_hash=revision.content_hash,
            target_snapshot_refs=[target_ref],
            target_commands=[command],
            target_snapshot_manifest_hash=sha256_hex([target_ref]),
            approval_mode="manual",
            input_evidence_manifest_hash=revision.evidence_manifest_hash,
            generation_pipeline_manifest_hash=None,
            quality_gate_manifest_hash=revision.quality_gate_manifest_hash,
            quality_report_hash=revision.quality_report_hash,
            intent_hash=services._intent_hash(
                intent_data,
                revision,
                {str(target.id): target_ref},
                {str(target.id): command},
            ),
            request_key="intent-t019-service",
            created_by=user,
        )
        title = revision.title
        body = "<p>승인 본문</p>"
        source_links = []
        render = ArticleChannelRender.objects.create(
            publication_intent=intent,
            article_revision=revision,
            target=target,
            target_snapshot=snapshot,
            target_config_hash=snapshot.config_hash,
            channel_role=target.role,
            render_stage="preview",
            title=title,
            body_html=body,
            canonical_link_state="not_applicable",
            template_hash=sha256_hex(
                {
                    "channel": target.channel,
                    "title": title,
                    "body": body,
                    "revision": str(revision.id),
                }
            ),
            content_hash=sha256_hex({"title": title, "body": body}),
            source_links=source_links,
            source_manifest_hash=sha256_hex(
                {
                    "inputEvidenceManifestHash": intent.input_evidence_manifest_hash,
                    "sourceLinks": source_links,
                }
            ),
        )
        cls.fixture = SimpleNamespace(
            user=user,
            article=article,
            intent=intent,
            target=target,
            command=command,
            render=render,
        )

    def _decide(self, data, *, request=None):
        fixture = self.fixture
        audit_context = SimpleNamespace(
            actor_type="admin",
            actor_id=fixture.user.pk,
            event_key=None,
            request_key=data["requestKey"],
            reason_code=data["decisionReason"],
        )

        with (
            patch.object(services, "_lock_article_external_write_fence"),
            patch.object(services, "_lock_target_intent_fences"),
            patch.object(
                services,
                "_require_intent_revision_publishable",
                side_effect=lambda row, **_kwargs: row,
            ),
            patch.object(services, "_record_publishing_audit"),
        ):
            approval, created = services._decide_approval_atomic.__wrapped__(
                str(fixture.article.id),
                str(fixture.target.id),
                data,
                user=fixture.user,
                audit_context=audit_context,
                request=request,
            )
        return approval, created

    def _approval_data(self):
        fixture = self.fixture
        return {
            "revisionNo": fixture.intent.revision_no,
            "publicationIntentId": str(fixture.intent.id),
            "expectedLatestApprovalId": None,
            "expectedHeadVersion": 0,
            "requestKey": "approval-t019-service",
            "reauthProofId": None,
            "actionSubject": _content_subject(fixture.command, fixture.render),
            "decision": Approval.Decision.APPROVED,
            "decisionReason": "동결 자료 검토 완료",
        }

    def _attempt(self, approval, *, state):
        from apps.publishing.models import Publication, PublicationAttempt

        fixture = self.fixture
        publication = Publication.objects.create(
            article_id=fixture.article.id,
            target=fixture.target,
            origin_target_snapshot_id=fixture.target.current_snapshot_id,
            remote_lookup_key=f"t019-{state}",
        )
        return PublicationAttempt.objects.create(
            publication=publication,
            article_revision=fixture.intent.article_revision,
            publication_intent=fixture.intent,
            target_snapshot_id=fixture.target.current_snapshot_id,
            target_config_hash=fixture.target.current_config_hash,
            resolved_action=fixture.command["resolvedAction"],
            target_command_hash=fixture.command["targetCommandHash"],
            publisher_contract_version="publisher-v1",
            publisher_adapter_manifest_hash=fixture.target.publisher_adapter_manifest_hash,
            approval=approval,
            approval_subject_hash=approval.approval_subject_hash,
            idempotency_key=f"t019-{state}",
            remote_lookup_key=publication.remote_lookup_key,
            request_fingerprint=SHA_A,
            state=state,
            correlation_id=uuid.uuid4(),
        )

    def _revoke_data(self, approval):
        data = self._approval_data()
        data.update(
            {
                "expectedLatestApprovalId": str(approval.id),
                "expectedHeadVersion": approval.head_version,
                "requestKey": "approval-t019-revoke",
                "reauthProofId": str(uuid.uuid4()),
                "decision": Approval.Decision.REVOKED,
                "decisionReason": "승인 철회 요청",
            }
        )
        return data

    def test_initial_decision_persists_v3_subject_decision_and_head_atomically(self):
        fixture = self.fixture
        approval, created = self._decide(self._approval_data())

        self.assertTrue(created)
        self.assertEqual(approval.approval_material_version, "approval-subject-v3")
        self.assertEqual(approval.decision_actor_id, fixture.user.pk)
        self.assertIsNone(approval.decision_event_key)
        head = approval.headed_by.get()
        self.assertEqual(head.latest_approval_id, approval.id)
        self.assertEqual(head.subject_hash, approval.approval_subject_hash)
        self.assertEqual(
            approval.decision_hash,
            services._approval_decision_hash(
                subject_hash=approval.approval_subject_hash,
                decision=approval.decision,
                head_version=1,
                supersedes_approval_id=None,
                request_hash=approval.request_hash,
                actor_type="admin",
                actor_id=fixture.user.pk,
                event_key=None,
                decision_reason=approval.decision_reason,
            ),
        )
        with (
            patch.object(services, "require_revision_publishable"),
            patch.object(services, "_kill_switch_enabled", return_value=False),
        ):
            projection = services.evaluate_approval_decision_readonly(
                approval_id=approval.id
            )
        self.assertTrue(projection.is_current)
        self.assertTrue(projection.dispatch_eligible)

    def test_revoke_conflicts_while_an_external_outcome_is_unresolved(self):
        from apps.publishing.models import PublicationAttempt

        approval, _ = self._decide(self._approval_data())
        self._attempt(approval, state=PublicationAttempt.State.UNKNOWN_OUTCOME)

        with (
            patch.object(services, "consume_reauthentication_proof"),
            self.assertRaisesRegex(Conflict, "active attempt"),
        ):
            self._decide(
                self._revoke_data(approval),
                request=SimpleNamespace(),
            )

        approval.headed_by.get().refresh_from_db()
        self.assertEqual(
            approval.headed_by.get().latest_approval_id,
            approval.id,
        )

    def test_revoke_stales_queued_attempt_in_the_same_transaction(self):
        from apps.publishing.models import PublicationAttempt

        approval, _ = self._decide(self._approval_data())
        attempt = self._attempt(approval, state=PublicationAttempt.State.QUEUED)

        with patch.object(services, "consume_reauthentication_proof") as consume:
            revoked, created = self._decide(
                self._revoke_data(approval),
                request=SimpleNamespace(),
            )

        self.assertTrue(created)
        self.assertEqual(revoked.decision, Approval.Decision.REVOKED)
        consume.assert_called_once_with(
            request=ANY,
            proof_id=ANY,
            action_scope="approval_revoke",
            entity_type="publication_approval",
            entity_id=approval.id,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, PublicationAttempt.State.STALE)
        self.assertEqual(attempt.error_code, "approval_revoked")
        self.assertIsNone(attempt.next_retry_at)

    def test_revoke_clears_a_scheduled_publication_projection(self):
        from apps.publishing.models import Publication, PublicationAttempt

        approval, _ = self._decide(self._approval_data())
        attempt = self._attempt(approval, state=PublicationAttempt.State.QUEUED)
        publication = attempt.publication
        publication.state = Publication.State.SCHEDULED
        publication.scheduled_for = timezone.now() + timedelta(hours=2)
        publication.last_error_code = "previous_error"
        publication.save(
            update_fields=(
                "state",
                "scheduled_for",
                "last_error_code",
                "updated_at",
            )
        )

        with patch.object(services, "consume_reauthentication_proof"):
            self._decide(
                self._revoke_data(approval),
                request=SimpleNamespace(),
            )

        publication.refresh_from_db()
        self.assertEqual(publication.state, Publication.State.PENDING)
        self.assertIsNone(publication.scheduled_for)
        self.assertEqual(publication.last_error_code, "")
