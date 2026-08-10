from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.db import IntegrityError, OperationalError

from apps.publishing import services
from wisdome_writer.domain.errors import Conflict, InvalidInput


SHA_A = "a" * 64
SHA_B = "b" * 64
ARTICLE_A = "00000000-0000-0000-0000-000000000101"
ARTICLE_B = "00000000-0000-0000-0000-000000000102"
TARGET_A = "00000000-0000-0000-0000-000000000201"
TARGET_B = "00000000-0000-0000-0000-000000000202"
SNAPSHOT_A = "00000000-0000-0000-0000-000000000301"
SNAPSHOT_B = "00000000-0000-0000-0000-000000000302"
ADMIN_A = "00000000-0000-0000-0000-000000000401"
ADMIN_B = "00000000-0000-0000-0000-000000000402"


def _intent_body() -> dict:
    return {
        "revisionNo": 1,
        "expectedRevisionContentHash": SHA_A,
        "correctionCaseId": None,
        "targetSnapshots": [
            {
                "targetId": TARGET_A,
                "targetSnapshotId": SNAPSHOT_A,
                "targetConfigHash": SHA_B,
            }
        ],
        "targetCommands": [
            {
                "targetId": TARGET_A,
                "targetSnapshotId": SNAPSHOT_A,
                "targetConfigHash": SHA_B,
                "resolvedAction": "create",
                "canonicalDependencyTargetId": None,
            }
        ],
        "approvalMode": "manual",
        "autoPublishValidationRefs": [],
        "autoPublishActivationRefs": [],
        "expectedLatestIntentId": None,
        "requestKey": "intent-request-0001",
        "reason": "관리자 발행 의도 생성",
    }


def _admin_context(actor_id: str):
    return SimpleNamespace(
        actor_type="admin",
        actor_id=actor_id,
        event_key=None,
        request_key="intent-request-0001",
        reason_code="관리자 발행 의도 생성",
    )


def _dispatch_body() -> dict:
    return {
        "publicationIntentId": "00000000-0000-0000-0000-000000000701",
        "revisionNo": 1,
        "targetIds": [TARGET_A],
        "expectedTargetSnapshots": [
            {
                "targetId": TARGET_A,
                "targetSnapshotId": SNAPSHOT_A,
                "targetConfigHash": SHA_B,
            }
        ],
        "publishAt": None,
        "requestKey": "dispatch-request-0001",
        "reason": "愿由ъ옄 諛쒗뻾 ?꾩넚",
    }


def _dispatch_context(actor_id: str):
    return SimpleNamespace(
        actor_type="admin",
        actor_id=actor_id,
        event_key=None,
        request_key="dispatch-request-0001",
        reason_code="愿由ъ옄 諛쒗뻾 ?꾩넚",
        correlation_id="00000000-0000-0000-0000-000000000499",
    )


class PublicationIntentRequestIdentityTests(SimpleTestCase):
    def test_intent_request_is_closed_and_requires_canonical_scalars(self):
        invalid = []
        unknown = _intent_body()
        unknown["unexpected"] = True
        invalid.append(unknown)
        unknown_row = _intent_body()
        unknown_row["targetCommands"][0]["unexpected"] = True
        invalid.append(unknown_row)
        missing_reason = _intent_body()
        missing_reason.pop("reason")
        invalid.append(missing_reason)
        untrimmed = _intent_body()
        untrimmed["reason"] = " leading reason"
        invalid.append(untrimmed)
        bad_key = _intent_body()
        bad_key["requestKey"] = "unsafe key"
        invalid.append(bad_key)

        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(InvalidInput):
                    services._canonical_intent_request_body(body)

        mixed_case = _intent_body()
        mixed_case["correctionCaseId"] = "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF"
        mixed_case["expectedLatestIntentId"] = "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDE0"
        normalized = services._canonical_intent_request_body(mixed_case)
        self.assertEqual(
            normalized["correctionCaseId"],
            "abcdefab-cdef-4abc-8def-abcdefabcdef",
        )
        self.assertEqual(
            normalized["expectedLatestIntentId"],
            "abcdefab-cdef-4abc-8def-abcdefabcde0",
        )

    def test_request_hash_binds_route_body_and_actor(self):
        request_hash = getattr(
            services,
            "_publication_intent_request_hash",
            lambda **kwargs: "",
        )
        body = _intent_body()
        baseline = request_hash(
            article_id=ARTICLE_A,
            data=deepcopy(body),
            audit_context=_admin_context(ADMIN_A),
        )
        changed_path = request_hash(
            article_id=ARTICLE_B,
            data=deepcopy(body),
            audit_context=_admin_context(ADMIN_A),
        )
        changed_actor = request_hash(
            article_id=ARTICLE_A,
            data=deepcopy(body),
            audit_context=_admin_context(ADMIN_B),
        )
        changed_body_data = deepcopy(body)
        changed_body_data["reason"] = "다른 생성 사유"
        changed_body = request_hash(
            article_id=ARTICLE_A,
            data=changed_body_data,
            audit_context=_admin_context(ADMIN_A),
        )

        self.assertRegex(baseline, r"^[a-f0-9]{64}$")
        self.assertEqual(len({baseline, changed_path, changed_actor, changed_body}), 4)

    def test_request_hash_canonicalizes_target_array_order(self):
        target_b = "00000000-0000-0000-0000-000000000202"
        snapshot_b = "00000000-0000-0000-0000-000000000302"
        body = _intent_body()
        body["targetSnapshots"].append(
            {
                "targetId": target_b,
                "targetSnapshotId": snapshot_b,
                "targetConfigHash": SHA_A,
            }
        )
        body["targetCommands"].append(
            {
                "targetId": target_b,
                "targetSnapshotId": snapshot_b,
                "targetConfigHash": SHA_A,
                "resolvedAction": "create",
                "canonicalDependencyTargetId": None,
            }
        )
        reordered = deepcopy(body)
        reordered["targetSnapshots"].reverse()
        reordered["targetCommands"].reverse()

        first = services._publication_intent_request_hash(
            article_id=ARTICLE_A,
            data=body,
            audit_context=_admin_context(ADMIN_A),
        )
        second = services._publication_intent_request_hash(
            article_id=ARTICLE_A,
            data=reordered,
            audit_context=_admin_context(ADMIN_A),
        )

        self.assertEqual(first, second)

    def test_intent_target_arrays_are_nonempty_and_bounded_to_twenty(self):
        empty = _intent_body()
        empty["targetSnapshots"] = []
        empty["targetCommands"] = []
        with self.assertRaises(InvalidInput):
            services._canonical_intent_request_body(empty)

        oversized = _intent_body()
        oversized["targetSnapshots"] = [
            {
                "targetId": f"00000000-0000-0000-0000-{index:012d}",
                "targetSnapshotId": f"10000000-0000-0000-0000-{index:012d}",
                "targetConfigHash": SHA_A,
            }
            for index in range(1, 22)
        ]
        oversized["targetCommands"] = [
            {
                "targetId": row["targetId"],
                "targetSnapshotId": row["targetSnapshotId"],
                "targetConfigHash": SHA_A,
                "resolvedAction": "create",
                "canonicalDependencyTargetId": None,
            }
            for row in oversized["targetSnapshots"]
        ]
        with self.assertRaises(InvalidInput):
            services._canonical_intent_request_body(oversized)

    def test_semantic_duplicate_target_ids_are_rejected_in_every_ref_array(self):
        duplicate_cases = []
        snapshots = _intent_body()
        snapshots["targetSnapshots"].append(
            {
                "targetId": TARGET_A,
                "targetSnapshotId": "00000000-0000-0000-0000-000000000399",
                "targetConfigHash": SHA_A,
            }
        )
        duplicate_cases.append(snapshots)
        commands = _intent_body()
        commands["targetCommands"].append(
            {
                "targetId": TARGET_A,
                "targetSnapshotId": SNAPSHOT_A,
                "targetConfigHash": SHA_B,
                "resolvedAction": "update",
                "canonicalDependencyTargetId": None,
            }
        )
        duplicate_cases.append(commands)

        for field_name in ("autoPublishValidationRefs", "autoPublishActivationRefs"):
            body = _intent_body()
            body["approvalMode"] = "validated_auto"
            body["autoPublishValidationRefs"] = [
                {
                    "targetId": TARGET_A,
                    "targetSnapshotId": SNAPSHOT_A,
                    "validationId": "00000000-0000-0000-0000-000000000501",
                    "materialHash": SHA_A,
                }
            ]
            body["autoPublishActivationRefs"] = [
                {
                    "targetId": TARGET_A,
                    "targetSnapshotId": SNAPSHOT_A,
                    "activationId": "00000000-0000-0000-0000-000000000601",
                    "version": 1,
                    "activationHash": SHA_B,
                }
            ]
            duplicate = deepcopy(body[field_name][0])
            if field_name == "autoPublishValidationRefs":
                duplicate["validationId"] = "00000000-0000-0000-0000-000000000502"
            else:
                duplicate["activationId"] = "00000000-0000-0000-0000-000000000602"
            body[field_name].append(duplicate)
            duplicate_cases.append(body)

        for body in duplicate_cases:
            with self.subTest(body=body):
                with self.assertRaises(InvalidInput):
                    services._canonical_intent_request_body(body)

    def test_validated_auto_refs_match_the_exact_target_snapshot_set(self):
        body = _intent_body()
        body["approvalMode"] = "validated_auto"
        body["autoPublishValidationRefs"] = [
            {
                "targetId": TARGET_A,
                "targetSnapshotId": SNAPSHOT_A,
                "validationId": "00000000-0000-0000-0000-000000000501",
                "materialHash": SHA_A,
            }
        ]
        body["autoPublishActivationRefs"] = [
            {
                "targetId": TARGET_A,
                "targetSnapshotId": "00000000-0000-0000-0000-000000000399",
                "activationId": "00000000-0000-0000-0000-000000000601",
                "version": 1,
                "activationHash": SHA_B,
            }
        ]

        with self.assertRaises(InvalidInput):
            services._canonical_intent_request_body(body)

    def test_intent_refs_require_complete_command_and_snapshot_identity(self):
        body = _intent_body()
        body["targetSnapshots"][0]["targetSnapshotId"] = None
        body["targetCommands"][0]["targetSnapshotId"] = None

        with self.assertRaises(InvalidInput):
            services._canonical_intent_request_body(body)

    def test_unknown_targets_fail_before_target_fence_locking(self):
        target_query = MagicMock()
        target_query.values_list.return_value = []
        with (
            patch.object(services.PublicationTarget.objects, "filter", return_value=target_query),
            patch.object(
                services,
                "_lock_target_intent_fences",
                side_effect=AssertionError("target fence must not be touched"),
            ),
            patch.object(services, "_lock_article_external_write_fence"),
        ):
            try:
                services._create_publication_intent_atomic.__wrapped__(
                    ARTICLE_A,
                    _intent_body(),
                    user=SimpleNamespace(pk=ADMIN_A),
                    audit_context=_admin_context(ADMIN_A),
                )
            except AssertionError as exc:
                self.fail(str(exc))
            except InvalidInput:
                pass
            else:
                self.fail("unknown target IDs must be rejected")


class PublicationIntentReplayTests(SimpleTestCase):
    def test_exact_stale_replay_returns_before_live_gate_or_atomic_create(self):
        data = _intent_body()
        context = _admin_context(ADMIN_A)
        existing = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000701",
            article_id=ARTICLE_A,
            request_key=data["requestKey"],
            request_hash=services._publication_intent_request_hash(
                article_id=ARTICLE_A,
                data=data,
                audit_context=context,
            ),
            request_hash_version="publication-intent-request-v1",
            state="stale",
        )
        query = MagicMock()
        query.select_related.return_value.first.return_value = existing

        with (
            patch.object(services.PublicationIntent.objects, "filter", return_value=query),
            patch.object(services, "require_audit_replay"),
            patch.object(
                services,
                "_require_intent_revision_publishable",
                side_effect=AssertionError("live gate must not run for replay"),
            ),
            patch.object(
                services,
                "_create_publication_intent_atomic",
                side_effect=AssertionError("atomic create must not run for replay"),
            ),
        ):
            try:
                observed, created = services.create_publication_intent(
                    ARTICLE_A,
                    data,
                    user=SimpleNamespace(pk=ADMIN_A),
                    audit_context=context,
                )
            except AssertionError as exc:
                self.fail(str(exc))

        self.assertIs(observed, existing)
        self.assertFalse(created)

    def test_unique_race_rolls_back_then_replays_without_leaking_integrity_error(self):
        data = _intent_body()
        context = _admin_context(ADMIN_A)
        existing = SimpleNamespace(id="00000000-0000-0000-0000-000000000703")
        with (
            patch.object(
                services,
                "_find_publication_intent_replay",
                side_effect=[None, existing],
            ),
            patch.object(services, "_require_publication_targets_exist"),
            patch.object(
                services,
                "_create_publication_intent_atomic",
                side_effect=IntegrityError("unique request race"),
            ),
        ):
            try:
                observed, created = services.create_publication_intent(
                    ARTICLE_A,
                    data,
                    user=SimpleNamespace(pk=ADMIN_A),
                    audit_context=context,
                )
            except IntegrityError as exc:
                self.fail(f"database race leaked: {exc}")

        self.assertIs(observed, existing)
        self.assertFalse(created)

    def test_sqlite_busy_is_retried_outside_atomic_then_returns_created(self):
        data = _intent_body()
        context = _admin_context(ADMIN_A)
        created_row = SimpleNamespace(id="00000000-0000-0000-0000-000000000704")
        with (
            patch.object(services.connection, "vendor", "sqlite"),
            patch.object(services, "_find_publication_intent_replay", return_value=None),
            patch.object(services, "_require_publication_targets_exist"),
            patch.object(
                services,
                "_create_publication_intent_atomic",
                side_effect=[
                    OperationalError("database is locked"),
                    (created_row, True),
                ],
            ) as create,
        ):
            observed, was_created = services.create_publication_intent(
                ARTICLE_A,
                data,
                user=SimpleNamespace(pk=ADMIN_A),
                audit_context=context,
            )

        self.assertIs(observed, created_row)
        self.assertTrue(was_created)
        self.assertEqual(create.call_count, 2)


class PublicationIntentCreateTests(SimpleTestCase):
    def test_new_intent_persists_request_identity_and_verifies_database_head(self):
        data = _intent_body()
        context = _admin_context(ADMIN_A)
        article = SimpleNamespace(id=ARTICLE_A)
        revision = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000801",
            revision_no=1,
            content_hash=SHA_A,
        )
        material = {
            "revisionContentHash": SHA_A,
            "generationAttemptId": None,
            "inputEvidenceManifestHash": SHA_A,
            "generationPipelineManifestHash": None,
            "qualityGateManifestHash": SHA_A,
            "qualityReportHash": SHA_B,
            "editorialPolicyHash": SHA_A,
            "verificationManifestHash": SHA_A,
            "exclusionManifestHash": SHA_A,
            "claimManifestHash": SHA_A,
            "revalidationGeneration": 1,
        }
        target = SimpleNamespace(
            id=TARGET_A,
            current_snapshot_id=SNAPSHOT_A,
            current_config_hash=SHA_B,
            channel="wordpress",
            capabilities={"create": True},
            display_name="primary",
        )
        target_query = MagicMock()
        target_query.filter.return_value.order_by.return_value = [target]
        latest_query = MagicMock()
        latest_query.filter.return_value.order_by.return_value.first.return_value = None
        intent = MagicMock(
            id="00000000-0000-0000-0000-000000000901",
            pk="00000000-0000-0000-0000-000000000901",
            article_revision_id=revision.id,
            state="awaiting_approval",
            intent_hash=SHA_A,
        )
        intent.renders.order_by.return_value.values.return_value = []
        head_after = SimpleNamespace(
            latest_intent_id=intent.id,
            version=1,
        )

        with (
            patch.object(services, "_require_publication_targets_exist"),
            patch.object(services, "_lock_article_external_write_fence"),
            patch.object(services, "_lock_target_intent_fences"),
            patch.object(services, "_find_publication_intent_replay", return_value=None),
            patch.object(services, "_current_revision", return_value=(article, revision)),
            patch.object(services, "_revision_publication_material", return_value=material),
            patch.object(services.PublicationIntent.objects, "select_for_update", return_value=latest_query),
            patch.object(services.PublicationTarget.objects, "select_for_update", return_value=target_query),
            patch.object(services.PublicationIntent.objects, "create", return_value=intent) as create_intent,
            patch.object(services, "_create_preview_render"),
            patch.object(services, "_record_publishing_audit"),
            patch.object(services, "_audit_state", return_value={}),
            patch.object(
                services,
                "_locked_publication_intent_head",
                side_effect=[None, head_after],
                create=True,
            ) as locked_head,
        ):
            observed, created = services._create_publication_intent_atomic.__wrapped__(
                ARTICLE_A,
                data,
                user=SimpleNamespace(pk=ADMIN_A),
                audit_context=context,
            )

        self.assertIs(observed, intent)
        self.assertTrue(created)
        create_kwargs = create_intent.call_args.kwargs
        self.assertEqual(create_kwargs["request_hash_version"], "publication-intent-request-v1")
        self.assertEqual(
            create_kwargs["request_hash"],
            services._publication_intent_request_hash(
                article_id=ARTICLE_A,
                data=data,
                audit_context=context,
            ),
        )
        self.assertIsNone(create_kwargs["supersedes_intent"])
        self.assertEqual(locked_head.call_count, 2)


class PublicationDispatchRequestIdentityTests(SimpleTestCase):
    def test_dispatch_request_is_closed_and_normalizes_aware_instants_to_utc(self):
        invalid = []
        unknown = _dispatch_body()
        unknown["unexpected"] = True
        invalid.append(unknown)
        unknown_ref = _dispatch_body()
        unknown_ref["expectedTargetSnapshots"][0]["unexpected"] = True
        invalid.append(unknown_ref)
        missing_reason = _dispatch_body()
        missing_reason.pop("reason")
        invalid.append(missing_reason)
        naive = _dispatch_body()
        naive["publishAt"] = "2026-08-09T06:00:00"
        invalid.append(naive)
        space_separator = _dispatch_body()
        space_separator["publishAt"] = "2026-08-09 06:00:00+00:00"
        invalid.append(space_separator)
        untrimmed = _dispatch_body()
        untrimmed["reason"] = "trailing reason "
        invalid.append(untrimmed)

        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(InvalidInput):
                    services._canonical_dispatch_request_body(body)

        utc = _dispatch_body()
        utc["publishAt"] = "2026-08-09T06:00:00Z"
        offset = _dispatch_body()
        offset["publishAt"] = "2026-08-09T15:00:00+09:00"
        self.assertEqual(
            services._canonical_dispatch_request_body(utc),
            services._canonical_dispatch_request_body(offset),
        )
    def test_dispatch_request_hash_binds_route_body_and_actor(self):
        body = _dispatch_body()
        baseline = services._publication_dispatch_request_hash(
            article_id=ARTICLE_A,
            data=deepcopy(body),
            audit_context=_dispatch_context(ADMIN_A),
        )
        changed_path = services._publication_dispatch_request_hash(
            article_id=ARTICLE_B,
            data=deepcopy(body),
            audit_context=_dispatch_context(ADMIN_A),
        )
        changed_actor = services._publication_dispatch_request_hash(
            article_id=ARTICLE_A,
            data=deepcopy(body),
            audit_context=_dispatch_context(ADMIN_B),
        )
        changed_body_data = deepcopy(body)
        changed_body_data["reason"] = "?ㅻⅨ ?꾩넚 ?ъ쑀"
        changed_body = services._publication_dispatch_request_hash(
            article_id=ARTICLE_A,
            data=changed_body_data,
            audit_context=_dispatch_context(ADMIN_A),
        )

        self.assertRegex(baseline, r"^[a-f0-9]{64}$")
        self.assertEqual(len({baseline, changed_path, changed_actor, changed_body}), 4)

    def test_dispatch_normalization_sorts_targets_and_equates_omitted_publish_at(self):
        body = _dispatch_body()
        body["targetIds"].append(TARGET_B)
        body["expectedTargetSnapshots"].append(
            {
                "targetId": TARGET_B,
                "targetSnapshotId": SNAPSHOT_B,
                "targetConfigHash": SHA_A,
            }
        )
        reordered = deepcopy(body)
        reordered["targetIds"].reverse()
        reordered["expectedTargetSnapshots"].reverse()
        omitted = deepcopy(body)
        omitted.pop("publishAt")

        hashes = {
            services._publication_dispatch_request_hash(
                article_id=ARTICLE_A,
                data=value,
                audit_context=_dispatch_context(ADMIN_A),
            )
            for value in (body, reordered, omitted)
        }
        self.assertEqual(len(hashes), 1)

    def test_dispatch_targets_are_nonempty_unique_exact_and_bounded(self):
        invalid: list[dict] = []
        empty = _dispatch_body()
        empty["targetIds"] = []
        empty["expectedTargetSnapshots"] = []
        invalid.append(empty)
        duplicate = _dispatch_body()
        duplicate["targetIds"].append(TARGET_A)
        duplicate["expectedTargetSnapshots"].append(
            {
                "targetId": TARGET_A,
                "targetSnapshotId": SNAPSHOT_B,
                "targetConfigHash": SHA_A,
            }
        )
        invalid.append(duplicate)
        mismatch = _dispatch_body()
        mismatch["targetIds"] = [TARGET_B]
        invalid.append(mismatch)
        incomplete = _dispatch_body()
        incomplete["expectedTargetSnapshots"][0]["targetSnapshotId"] = None
        invalid.append(incomplete)
        oversized = _dispatch_body()
        oversized["targetIds"] = [
            f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 22)
        ]
        oversized["expectedTargetSnapshots"] = [
            {
                "targetId": target_id,
                "targetSnapshotId": f"10000000-0000-0000-0000-{index:012d}",
                "targetConfigHash": SHA_A,
            }
            for index, target_id in enumerate(oversized["targetIds"], start=1)
        ]
        invalid.append(oversized)

        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(InvalidInput):
                    services._canonical_dispatch_request_body(body)

    def test_dispatch_manifest_freezes_initial_attempt_number_across_retries(self):
        publication = SimpleNamespace(id="publication-1", target_id=TARGET_A)
        attempt = SimpleNamespace(
            id="attempt-1",
            publication_id=publication.id,
            publication=publication,
            attempt_no=1,
            resolved_action="create",
        )
        accepted = services._publication_attempt_manifest([attempt])
        attempt.attempt_no = 4
        retried = services._publication_attempt_manifest([attempt])

        self.assertEqual(accepted, retried)
        self.assertEqual(retried[0]["attemptNo"], 1)


class PublicationDispatchReplayTests(SimpleTestCase):
    def test_exact_replay_returns_before_any_live_gate_or_atomic_dispatch(self):
        data = _dispatch_body()
        expected = SimpleNamespace(dispatch=object(), attempts=())
        with (
            patch.object(
                services,
                "_find_publication_dispatch_replay",
                return_value=expected,
                create=True,
            ),
            patch.object(
                services,
                "_require_intent_revision_publishable",
                side_effect=AssertionError("live gate must not run for replay"),
            ),
            patch.object(
                services,
                "_dispatch_publication_atomic",
                side_effect=AssertionError("atomic dispatch must not run for replay"),
            ),
        ):
            try:
                observed, created = services.dispatch_publication(
                    ARTICLE_A,
                    data,
                    audit_context=_dispatch_context(ADMIN_A),
                )
            except AssertionError as exc:
                self.fail(str(exc))

        self.assertIs(observed, expected)
        self.assertFalse(created)

    def test_inner_dispatch_replay_wins_before_publishability_gate(self):
        data = _dispatch_body()
        expected = SimpleNamespace(dispatch=object(), attempts=())
        with (
            patch.object(services, "_require_publication_targets_exist"),
            patch.object(services, "_lock_article_external_write_fence"),
            patch.object(services, "_lock_target_intent_fences"),
            patch.object(
                services,
                "_find_publication_dispatch_replay",
                return_value=expected,
            ),
            patch.object(
                services,
                "_require_intent_revision_publishable",
                side_effect=AssertionError("publishability gate must not run for inner replay"),
            ),
        ):
            try:
                observed, created = services._dispatch_publication_atomic.__wrapped__(
                    ARTICLE_A,
                    data,
                    audit_context=_dispatch_context(ADMIN_A),
                )
            except AssertionError as exc:
                self.fail(str(exc))

        self.assertIs(observed, expected)
        self.assertFalse(created)

    def test_dispatch_unique_race_replays_without_leaking_integrity_error(self):
        data = _dispatch_body()
        expected = SimpleNamespace(dispatch=object(), attempts=())
        with (
            patch.object(
                services,
                "_find_publication_dispatch_replay",
                side_effect=[None, expected],
                create=True,
            ),
            patch.object(services, "_require_publication_targets_exist"),
            patch.object(
                services,
                "_dispatch_publication_atomic",
                side_effect=IntegrityError("dispatch unique race"),
            ),
        ):
            try:
                observed, created = services.dispatch_publication(
                    ARTICLE_A,
                    data,
                    audit_context=_dispatch_context(ADMIN_A),
                )
            except IntegrityError as exc:
                self.fail(f"database race leaked: {exc}")

        self.assertIs(observed, expected)
        self.assertFalse(created)

    def test_dispatch_sqlite_busy_retries_then_returns_exact_replay(self):
        data = _dispatch_body()
        expected = SimpleNamespace(dispatch=object(), attempts=())
        with (
            patch.object(services.connection, "vendor", "sqlite"),
            patch.object(
                services,
                "_find_publication_dispatch_replay",
                side_effect=[None, None, expected],
            ),
            patch.object(services, "_require_publication_targets_exist"),
            patch.object(
                services,
                "_dispatch_publication_atomic",
                side_effect=[
                    OperationalError("database table is locked"),
                    OperationalError("database is locked"),
                ],
            ) as dispatch,
        ):
            observed, created = services.dispatch_publication(
                ARTICLE_A,
                data,
                audit_context=_dispatch_context(ADMIN_A),
            )

        self.assertIs(observed, expected)
        self.assertFalse(created)
        self.assertEqual(dispatch.call_count, 2)


class PublicationAttemptGateRoundOneTests(SimpleTestCase):
    def _attempt(self):
        intent = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000701",
            article_id=ARTICLE_A,
            state="dispatched",
            approval_mode="manual",
            target_commands=[],
        )
        target = SimpleNamespace(
            id=TARGET_A,
            current_snapshot_id=SNAPSHOT_A,
            current_config_hash=SHA_A,
            publisher_adapter_manifest_hash=SHA_B,
            channel="wordpress",
            connection_state="verified",
            environment="test",
        )
        publication = SimpleNamespace(
            article_id=ARTICLE_A,
            target=target,
            remote_lookup_key="ww-current",
        )
        return SimpleNamespace(
            publication_intent=intent,
            publication=publication,
            resolved_action="create",
            remote_lookup_key="ww-frozen",
        )

    def test_attempt_gate_uses_authoritative_head_not_created_at(self):
        attempt = self._attempt()
        other = SimpleNamespace(id="00000000-0000-0000-0000-000000000799")
        with (
            patch.object(services, "_require_attempt_origin_run_active"),
            patch.object(services, "_kill_switch_enabled", return_value=False),
            patch.object(
                services,
                "resolve_current_publication_intent",
                return_value=other,
                create=True,
            ),
        ):
            with self.assertRaises(Conflict):
                services.validate_attempt_gate(attempt)

    def test_attempt_gate_rejects_remote_lookup_identity_drift(self):
        attempt = self._attempt()
        with (
            patch.object(services, "_require_attempt_origin_run_active"),
            patch.object(services, "_kill_switch_enabled", return_value=False),
            patch.object(
                services,
                "resolve_current_publication_intent",
                return_value=attempt.publication_intent,
                create=True,
            ),
        ):
            with self.assertRaises(Conflict):
                services.validate_attempt_gate(attempt)


class ValidatedAutoProvenanceTests(SimpleTestCase):
    def test_supplied_validation_refs_must_equal_enabled_activation_material(self):
        validation_a = "00000000-0000-0000-0000-000000000501"
        validation_b = "00000000-0000-0000-0000-000000000502"
        activation_id = "00000000-0000-0000-0000-000000000601"
        activation_refs = [
            {
                "targetId": TARGET_A,
                "targetSnapshotId": SNAPSHOT_A,
                "validationId": validation_a,
                "materialHash": SHA_A,
            }
        ]
        activation = SimpleNamespace(
            id=activation_id,
            target_id=TARGET_A,
            target_snapshot_id=SNAPSHOT_A,
            target_operational_config_hash=SHA_B,
            validation_refs=activation_refs,
            validation_manifest_hash=services.sha256_hex(activation_refs),
            activation_hash=SHA_A,
            version=1,
            decision="enabled",
            supersedes_activation_id=None,
        )
        target = SimpleNamespace(
            id=TARGET_A,
            current_snapshot_id=SNAPSHOT_A,
            current_config_hash=SHA_B,
            latest_auto_publish_activation_id=activation_id,
            auto_publish_enabled=True,
            connection_state="verified",
            environment="test",
            preflight_state="passed",
        )
        supplied = [
            {
                **activation_refs[0],
                "validationId": validation_b,
            }
        ]
        intent = SimpleNamespace(
            approval_mode="validated_auto",
            auto_publish_validation_refs=supplied,
            auto_validation_manifest_hash=services.sha256_hex(supplied),
            auto_publish_activation_refs=[
                {
                    "targetId": TARGET_A,
                    "targetSnapshotId": SNAPSHOT_A,
                    "activationId": activation_id,
                    "version": 1,
                    "activationHash": SHA_A,
                }
            ],
        )
        activation_query = MagicMock()
        activation_query.filter.return_value.first.return_value = activation
        validation = SimpleNamespace(
            id=validation_a,
            target_id=TARGET_A,
            target_snapshot_id=SNAPSHOT_A,
            target_config_hash=SHA_B,
            material_hash=SHA_A,
            status="passed",
        )
        validation_query = MagicMock()
        validation_query.filter.return_value = [validation]

        activation.activation_hash = services.sha256_hex(
            {
                "targetId": TARGET_A,
                "targetSnapshotId": SNAPSHOT_A,
                "operationalConfigHash": SHA_B,
                "validationRefs": activation_refs,
                "version": 1,
                "decision": "enabled",
                "supersedes": None,
            }
        )
        intent.auto_publish_activation_refs[0]["activationHash"] = activation.activation_hash

        with (
            patch.object(
                services.AutoPublishActivation.objects,
                "using",
                return_value=activation_query,
            ),
            patch.object(
                services.AutoPublishValidation.objects,
                "using",
                return_value=validation_query,
            ),
        ):
            self.assertFalse(
                services._validated_auto_live_eligible(
                    intent=intent,
                    target=target,
                )
            )
            intent.auto_publish_validation_refs = activation_refs
            intent.auto_validation_manifest_hash = services.sha256_hex(activation_refs)
            self.assertTrue(
                services._validated_auto_live_eligible(
                    intent=intent,
                    target=target,
                )
            )

class PublicationDispatchCreateTests(SimpleTestCase):
    def test_new_dispatch_persists_ledger_before_queue_and_returns_frozen_result(self):
        data = _dispatch_body()
        context = _dispatch_context(ADMIN_A)
        intent = MagicMock(
            id=data["publicationIntentId"],
            article_id=ARTICLE_A,
            article_revision_id="00000000-0000-0000-0000-000000000801",
            target_commands=[
                {
                    "targetId": TARGET_A,
                    "targetSnapshotId": SNAPSHOT_A,
                    "targetConfigHash": SHA_B,
                    "resolvedAction": "create",
                    "canonicalDependencyTargetId": None,
                    "targetCommandHash": SHA_A,
                }
            ],
            target_snapshot_refs=data["expectedTargetSnapshots"],
            auto_publish_activation_refs=[],
            revision_no=1,
            state="approved",
            intent_hash=SHA_A,
        )
        target = SimpleNamespace(
            id=TARGET_A,
            current_snapshot_id=SNAPSHOT_A,
            current_config_hash=SHA_B,
            channel="wordpress",
            publisher_contract_version="publisher-v1",
            publisher_adapter_manifest_hash=SHA_A,
        )
        approval = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000811",
            decision="approved",
            approval_subject_hash=SHA_B,
            target_action="create",
        )
        publication = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000821",
            target=target,
            target_id=TARGET_A,
            remote_lookup_key="ww-publication-821",
        )
        attempt = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000831",
            publication_id=publication.id,
            publication=publication,
            attempt_no=1,
            resolved_action="create",
            correlation_id=context.correlation_id,
            depends_on_attempt_id=None,
            dependency_subject_hash="",
        )
        dispatch = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000841",
            publication_intent_id=intent.id,
        )
        candidate_query = MagicMock()
        candidate_query.get.return_value = intent
        locked_query = MagicMock()
        locked_query.get.return_value = intent
        latest_query = MagicMock()
        latest_query.order_by.return_value.first.return_value = intent
        target_query = MagicMock()
        target_query.filter.return_value.order_by.return_value = [target]
        attempt_query = MagicMock()
        attempt_query.first.return_value = None
        trace: list[str] = []

        with (
            patch.object(services, "_require_publication_targets_exist"),
            patch.object(services, "_lock_article_external_write_fence"),
            patch.object(services, "_lock_target_intent_fences"),
            patch.object(services, "_find_publication_dispatch_replay", return_value=None),
            patch.object(services.PublicationIntent.objects, "select_related", return_value=candidate_query),
            patch.object(services.PublicationIntent.objects, "select_for_update", return_value=locked_query),
            patch.object(services.PublicationIntent.objects, "filter", return_value=latest_query),
            patch.object(
                services,
                "resolve_current_publication_intent",
                return_value=intent,
            ),
            patch.object(services.PublicationTarget.objects, "select_for_update", return_value=target_query),
            patch.object(services.PublicationAttempt.objects, "filter", return_value=attempt_query),
            patch.object(
                services.PublicationAttempt.objects,
                "create",
                side_effect=lambda **kwargs: (trace.append("attempt"), attempt)[1],
            ),
            patch.object(
                services,
                "ensure_publication_media_delivery_operations_locked",
                side_effect=lambda **kwargs: trace.append("media"),
            ),
            patch.object(
                services.PublicationDispatch.objects,
                "create",
                side_effect=lambda **kwargs: (trace.append("ledger"), dispatch)[1],
            ) as create_dispatch,
            patch.object(services, "_require_intent_revision_publishable"),
            patch.object(services, "_require_current_dispatch_approval_locked"),
            patch.object(services, "_latest_approval_locked", return_value=approval),
            patch.object(services, "_publication_for", return_value=publication),
            patch.object(services, "_mark_origin_run_publishing_locked"),
            patch.object(
                services,
                "_queue_attempt_on_commit",
                side_effect=lambda *args, **kwargs: trace.append("queue"),
            ),
            patch.object(services, "_record_publishing_audit"),
            patch.object(services, "_audit_state", return_value={}),
        ):
            observed, created = services._dispatch_publication_atomic.__wrapped__(
                ARTICLE_A,
                data,
                audit_context=context,
            )

        self.assertTrue(created)
        self.assertIs(observed.dispatch, dispatch)
        self.assertEqual(observed.attempts, (attempt,))
        self.assertEqual(trace, ["attempt", "media", "ledger", "queue"])
        ledger_kwargs = create_dispatch.call_args.kwargs
        self.assertEqual(ledger_kwargs["request_hash_version"], "publication-dispatch-request-v1")
        self.assertEqual(ledger_kwargs["attempt_count"], 1)
        self.assertEqual(
            ledger_kwargs["attempt_manifest_hash"],
            services.sha256_hex(services._publication_attempt_manifest([attempt])),
        )

    def test_inner_replay_wins_race_before_live_gate_and_cas(self):
        data = _intent_body()
        context = _admin_context(ADMIN_A)
        existing = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000702",
            article_id=ARTICLE_A,
            request_key=data["requestKey"],
            request_hash=services._publication_intent_request_hash(
                article_id=ARTICLE_A,
                data=data,
                audit_context=context,
            ),
            request_hash_version="publication-intent-request-v1",
            state="awaiting_approval",
        )
        current_query = MagicMock()
        current_query.select_related.return_value.first.return_value = existing
        with (
            patch.object(services, "_require_publication_targets_exist"),
            patch.object(services, "_lock_article_external_write_fence"),
            patch.object(services, "_lock_target_intent_fences"),
            patch.object(services.PublicationIntent.objects, "filter", return_value=current_query),
            patch.object(services, "require_audit_replay"),
            patch.object(
                services,
                "_require_intent_revision_publishable",
                side_effect=AssertionError("live gate must not run for inner replay"),
            ),
        ):
            try:
                observed, created = services._create_publication_intent_atomic.__wrapped__(
                    ARTICLE_A,
                    data,
                    user=SimpleNamespace(pk=ADMIN_A),
                    audit_context=context,
                )
            except AssertionError as exc:
                self.fail(str(exc))

        self.assertIs(observed, existing)
        self.assertFalse(created)
