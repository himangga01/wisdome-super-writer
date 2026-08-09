import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase
from jsonschema.exceptions import ValidationError

from apps.publishing import api, services
from wisdome_writer.api.openapi import _compile_schema, load_openapi_contract
from wisdome_writer.domain.errors import InvalidInput, RequestValidationError


ARTICLE_ID = "11111111-1111-4111-8111-111111111111"
INTENT_ID = "22222222-2222-4222-8222-222222222222"
TARGET_ID = "33333333-3333-4333-8333-333333333333"
SNAPSHOT_ID = "44444444-4444-4444-8444-444444444444"
ATTEMPT_ID = "55555555-5555-4555-8555-555555555555"
PUBLICATION_ID = "66666666-6666-4666-8666-666666666666"
CORRELATION_ID = "77777777-7777-4777-8777-777777777777"
HASH_A = "a" * 64


def _validator(schema_name: str):
    contract = load_openapi_contract()
    _, validator = _compile_schema(
        contract["components"]["schemas"][schema_name],
        document=contract,
        subject=schema_name,
    )
    return validator


def _target_ref(*, target_id: str = TARGET_ID, snapshot_id: str = SNAPSHOT_ID):
    return {
        "targetId": target_id,
        "targetSnapshotId": snapshot_id,
        "targetConfigHash": HASH_A,
    }


def _target_command(*, target_id: str = TARGET_ID, snapshot_id: str = SNAPSHOT_ID):
    return {
        **_target_ref(target_id=target_id, snapshot_id=snapshot_id),
        "resolvedAction": "create",
        "canonicalDependencyTargetId": None,
    }


def _intent_request_payload() -> dict:
    return {
        "revisionNo": 3,
        "expectedRevisionContentHash": HASH_A,
        "correctionCaseId": None,
        "targetSnapshots": [_target_ref()],
        "targetCommands": [_target_command()],
        "approvalMode": "manual",
        "autoPublishValidationRefs": [],
        "autoPublishActivationRefs": [],
        "expectedLatestIntentId": None,
        "requestKey": "intent-request-0001",
        "reason": "Reviewed frozen publication intent material.",
    }


def _publish_request_payload() -> dict:
    return {
        "revisionNo": 3,
        "publicationIntentId": INTENT_ID,
        "targetIds": [TARGET_ID],
        "expectedTargetSnapshots": [_target_ref()],
        "publishAt": None,
        "requestKey": "dispatch-request-0001",
        "reason": "Dispatch approved frozen publication material.",
    }


def _admin_request(path: str, payload: dict):
    request = RequestFactory().post(
        path,
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_X_CSRFTOKEN="test-token",
    )
    request._dont_enforce_csrf_checks = True
    request.correlation_id = uuid.UUID(CORRELATION_ID)
    request.user = SimpleNamespace(
        pk=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        is_authenticated=True,
        is_active=True,
        is_staff=True,
    )
    return request


def _admin_get(path: str):
    request = RequestFactory().get(path, HTTP_X_CSRFTOKEN="test-token")
    request.correlation_id = uuid.UUID(CORRELATION_ID)
    request.user = SimpleNamespace(
        pk=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        is_authenticated=True,
        is_active=True,
        is_staff=True,
    )
    return request


def _dispatch_result():
    accepted_at = datetime(2026, 8, 9, 6, 0, tzinfo=timezone.utc)
    dispatch = SimpleNamespace(
        publication_intent_id=uuid.UUID(INTENT_ID),
        request_key="dispatch-request-0001",
        correlation_id=uuid.UUID(CORRELATION_ID),
        accepted_at=accepted_at,
    )
    attempt = SimpleNamespace(
        id=uuid.UUID(ATTEMPT_ID),
        publication_id=uuid.UUID(PUBLICATION_ID),
        publication=SimpleNamespace(target_id=uuid.UUID(TARGET_ID)),
        resolved_action="create",
        # A retry may advance this mutable execution counter after acceptance.
        attempt_no=5,
    )
    return SimpleNamespace(dispatch=dispatch, attempts=(attempt,)), accepted_at


class PublicationIntentContractTests(SimpleTestCase):
    maxDiff = None

    def test_intent_and_dispatch_requests_reject_unsafe_or_unbounded_target_material(self):
        contract = load_openapi_contract()
        intent_validator = _validator("PublicationIntentRequest")
        publish_validator = _validator("PublishRequest")
        intent_payload = _intent_request_payload()
        publish_payload = _publish_request_payload()
        too_many_ids = [
            str(uuid.UUID(int=index + 1))
            for index in range(21)
        ]
        too_many_refs = [
            _target_ref(target_id=target_id, snapshot_id=str(uuid.UUID(int=index + 101)))
            for index, target_id in enumerate(too_many_ids)
        ]

        rejected_intents = (
            {**intent_payload, "requestKey": "unsafe key"},
            {**intent_payload, "reason": " leading whitespace"},
            {**intent_payload, "reason": "trailing whitespace "},
            {**intent_payload, "targetSnapshots": too_many_refs},
            {
                **intent_payload,
                "targetCommands": [
                    _target_command(
                        target_id=target_id,
                        snapshot_id=str(uuid.UUID(int=index + 101)),
                    )
                    for index, target_id in enumerate(too_many_ids)
                ],
            },
        )
        rejected_dispatches = (
            {**publish_payload, "requestKey": "unsafe key"},
            {**publish_payload, "reason": " leading whitespace"},
            {**publish_payload, "reason": "trailing whitespace "},
            {**publish_payload, "targetIds": [TARGET_ID, TARGET_ID]},
            {**publish_payload, "targetIds": too_many_ids},
            {**publish_payload, "expectedTargetSnapshots": []},
            {**publish_payload, "expectedTargetSnapshots": too_many_refs},
        )

        for payload in rejected_intents:
            with self.subTest(intent=payload):
                with self.assertRaises(ValidationError):
                    intent_validator.validate(payload)
        for payload in rejected_dispatches:
            with self.subTest(dispatch=payload):
                with self.assertRaises(ValidationError):
                    publish_validator.validate(payload)

        intent_projection = contract["components"]["schemas"]["PublicationIntent"]
        for field in (
            "targetSnapshots",
            "targetCommands",
            "autoPublishValidationRefs",
            "autoPublishActivationRefs",
        ):
            with self.subTest(intent_projection_field=field):
                self.assertEqual(intent_projection["properties"][field]["maxItems"], 20)

        without_publish_at = {
            key: value
            for key, value in publish_payload.items()
            if key != "publishAt"
        }
        publish_validator.validate(without_publish_at)
        publish_validator.validate(publish_payload)

    def test_intent_and_dispatch_request_schemas_are_closed_and_require_exact_fields(self):
        cases = (
            ("PublicationIntentRequest", _intent_request_payload()),
            ("PublishRequest", _publish_request_payload()),
        )

        for schema_name, payload in cases:
            validator = _validator(schema_name)
            schema = load_openapi_contract()["components"]["schemas"][schema_name]
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("reason", schema["required"])

            for required_field in schema["required"]:
                with self.subTest(schema=schema_name, missing=required_field):
                    missing = dict(payload)
                    missing.pop(required_field)
                    with self.assertRaises(ValidationError):
                        validator.validate(missing)

            with self.subTest(schema=schema_name, unknown=True):
                with self.assertRaises(ValidationError):
                    validator.validate({**payload, "unknownField": "rejected"})

    def test_publish_at_requires_aware_rfc3339_and_canonicalizes_same_instant_to_utc_z(self):
        validator = _validator("PublishRequest")
        utc_payload = {
            **_publish_request_payload(),
            "publishAt": "2026-08-09T06:00:00Z",
        }
        offset_payload = {
            **_publish_request_payload(),
            "publishAt": "2026-08-09T15:00:00+09:00",
        }
        lowercase_payload = {
            **_publish_request_payload(),
            "publishAt": "2026-08-09t06:00:00z",
        }
        naive_payload = {
            **_publish_request_payload(),
            "publishAt": "2026-08-09T06:00:00",
        }
        non_rfc3339_payload = {
            **_publish_request_payload(),
            "publishAt": "2026-08-09 06:00:00+00:00",
        }

        validator.validate(utc_payload)
        validator.validate(offset_payload)
        validator.validate(lowercase_payload)
        with self.assertRaises(ValidationError):
            validator.validate(naive_payload)
        with self.assertRaises(ValidationError):
            validator.validate(non_rfc3339_payload)

        self.assertEqual(
            services._canonical_dispatch_request_body(utc_payload)["publishAt"],
            "2026-08-09T06:00:00Z",
        )
        self.assertEqual(
            services._canonical_dispatch_request_body(offset_payload)["publishAt"],
            "2026-08-09T06:00:00Z",
        )
        self.assertEqual(
            services._canonical_dispatch_request_body(lowercase_payload)["publishAt"],
            "2026-08-09T06:00:00Z",
        )
        with self.assertRaises(InvalidInput):
            services._canonical_dispatch_request_body(naive_payload)
        with self.assertRaises(InvalidInput):
            services._canonical_dispatch_request_body(non_rfc3339_payload)

    def test_valid_uuid_case_is_accepted_but_hash_material_is_canonical_lowercase(self):
        intent_payload = _intent_request_payload()
        intent_payload.update(
            {
                "correctionCaseId": "ABCDEFAB-CDEF-4ABC-8ABC-ABCDEFABCDEF",
                "expectedLatestIntentId": "FEDCBAFE-DCBA-4FED-8FED-FEDCBAFEDCBA",
            }
        )
        intent_payload["targetSnapshots"] = [
            _target_ref(
                target_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                snapshot_id="BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
            )
        ]
        intent_payload["targetCommands"] = [
            _target_command(
                target_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                snapshot_id="BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
            )
        ]
        dispatch_payload = {
            **_publish_request_payload(),
            "publicationIntentId": "CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC",
            "targetIds": ["AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"],
            "expectedTargetSnapshots": [
                _target_ref(
                    target_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                    snapshot_id="BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
                )
            ],
        }

        _validator("PublicationIntentRequest").validate(intent_payload)
        _validator("PublishRequest").validate(dispatch_payload)
        canonical_intent = services._canonical_intent_request_body(intent_payload)
        canonical_dispatch = services._canonical_dispatch_request_body(dispatch_payload)

        self.assertEqual(
            canonical_intent["correctionCaseId"],
            "abcdefab-cdef-4abc-8abc-abcdefabcdef",
        )
        self.assertEqual(
            canonical_intent["expectedLatestIntentId"],
            "fedcbafe-dcba-4fed-8fed-fedcbafedcba",
        )
        self.assertEqual(
            canonical_dispatch["publicationIntentId"],
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        )
        self.assertEqual(
            canonical_dispatch["targetIds"],
            ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"],
        )

    def test_path_article_uuid_case_is_the_same_canonical_replay_identity(self):
        uppercase_article_id = "ABCDEFAB-CDEF-4ABC-8ABC-ABCDEFABCDEF"
        lowercase_article_id = uppercase_article_id.lower()
        audit_context = SimpleNamespace(
            actor_type="admin",
            actor_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
            event_key=None,
        )

        self.assertEqual(
            services._publication_intent_request_hash(
                article_id=uppercase_article_id,
                data=_intent_request_payload(),
                audit_context=audit_context,
            ),
            services._publication_intent_request_hash(
                article_id=lowercase_article_id,
                data=_intent_request_payload(),
                audit_context=audit_context,
            ),
        )
        self.assertEqual(
            services._publication_dispatch_request_hash(
                article_id=uppercase_article_id,
                data=_publish_request_payload(),
                audit_context=audit_context,
            ),
            services._publication_dispatch_request_hash(
                article_id=lowercase_article_id,
                data=_publish_request_payload(),
                audit_context=audit_context,
            ),
        )

    def test_revision_number_is_bounded_to_json_safe_integer_for_all_callers(self):
        maximum = 9_007_199_254_740_991
        overflow = maximum + 1
        intent_at_maximum = {**_intent_request_payload(), "revisionNo": maximum}
        dispatch_at_maximum = {**_publish_request_payload(), "revisionNo": maximum}
        intent_overflow = {**_intent_request_payload(), "revisionNo": overflow}
        dispatch_overflow = {**_publish_request_payload(), "revisionNo": overflow}

        _validator("PublicationIntentRequest").validate(intent_at_maximum)
        _validator("PublishRequest").validate(dispatch_at_maximum)
        self.assertEqual(
            services._canonical_intent_request_body(intent_at_maximum)["revisionNo"],
            maximum,
        )
        self.assertEqual(
            services._canonical_dispatch_request_body(dispatch_at_maximum)["revisionNo"],
            maximum,
        )

        with self.assertRaises(ValidationError):
            _validator("PublicationIntentRequest").validate(intent_overflow)
        with self.assertRaises(ValidationError):
            _validator("PublishRequest").validate(dispatch_overflow)
        with self.assertRaises(InvalidInput):
            services._canonical_intent_request_body(intent_overflow)
        with self.assertRaises(InvalidInput):
            services._canonical_dispatch_request_body(dispatch_overflow)

    def test_sha256_material_rejects_non_string_values(self):
        with self.assertRaises(InvalidInput):
            services._canonical_sha256(int("1" * 64), field_name="materialHash")

    def test_public_head_resolver_canonicalizes_article_uuid_before_query(self):
        manager = services.PublicationIntentHead.objects
        heads = patch.object(manager, "using")
        with heads as using:
            query = using.return_value
            query.select_related.return_value.filter.return_value.first.return_value = None
            with patch.object(
                services.PublicationIntent.objects,
                "using",
            ) as intents_using:
                intents_using.return_value.filter.return_value.exists.return_value = False
                observed = services.resolve_current_publication_intent(
                    "ABCDEFAB-CDEF-4ABC-8ABC-ABCDEFABCDEF"
                )

        self.assertIsNone(observed)
        query.select_related.return_value.filter.assert_called_once_with(
            article_id="abcdefab-cdef-4abc-8abc-abcdefabcdef"
        )

    def test_dispatch_result_is_bounded_immutable_and_target_sorted(self):
        result, accepted_at = _dispatch_result()
        second_target_id = "88888888-8888-4888-8888-888888888888"
        second = SimpleNamespace(
            id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            publication_id=uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            publication=SimpleNamespace(target_id=uuid.UUID(second_target_id)),
            resolved_action="update",
            attempt_no=4,
        )
        result = SimpleNamespace(
            dispatch=result.dispatch,
            attempts=(second, result.attempts[0]),
        )

        payload = api.dispatch_result_json(result, replayed=True)

        self.assertEqual(
            payload,
            {
                "publicationIntentId": INTENT_ID,
                "requestKey": "dispatch-request-0001",
                "correlationId": CORRELATION_ID,
                "acceptedAt": accepted_at.isoformat(),
                "replayed": True,
                "attempts": [
                    {
                        "attemptId": ATTEMPT_ID,
                        "publicationId": PUBLICATION_ID,
                        "targetId": TARGET_ID,
                        "resolvedAction": "create",
                        "attemptNo": 1,
                    },
                    {
                        "attemptId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "publicationId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        "targetId": second_target_id,
                        "resolvedAction": "update",
                        "attemptNo": 1,
                    },
                ],
            },
        )
        _validator("PublicationDispatchResult").validate(payload)
        self.assertNotIn("state", payload)
        self.assertNotIn("history", payload)

    def test_create_intent_endpoint_binds_exact_body_and_distinguishes_replay(self):
        payload = _intent_request_payload()
        row = SimpleNamespace(id=uuid.UUID(INTENT_ID))
        audit_context = object()
        serialized = {"id": INTENT_ID}

        with (
            patch.object(api.AuditContext, "for_admin", return_value=audit_context),
            patch.object(
                api,
                "create_publication_intent",
                side_effect=((row, True), (row, False)),
            ) as create,
            patch.object(api, "intent_json", return_value=serialized),
        ):
            created = api.publication_intents(
                _admin_request(
                    f"/api/v1/articles/{ARTICLE_ID}/publication-intents",
                    payload,
                ),
                article_id=uuid.UUID(ARTICLE_ID),
            )
            replayed = api.publication_intents(
                _admin_request(
                    f"/api/v1/articles/{ARTICLE_ID}/publication-intents",
                    payload,
                ),
                article_id=uuid.UUID(ARTICLE_ID),
            )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(replayed.status_code, 200)
        self.assertEqual(create.call_count, 2)
        for call in create.call_args_list:
            self.assertEqual(call.args[:2], (uuid.UUID(ARTICLE_ID), payload))
            self.assertEqual(call.kwargs["audit_context"], audit_context)

        invalid = {**payload, "unexpected": "must be rejected"}
        with (
            patch.object(api.AuditContext, "for_admin", return_value=audit_context),
            patch.object(api, "create_publication_intent") as create_invalid,
            self.assertRaises(RequestValidationError),
        ):
            api.publication_intents(
                _admin_request(
                    f"/api/v1/articles/{ARTICLE_ID}/publication-intents",
                    invalid,
                ),
                article_id=uuid.UUID(ARTICLE_ID),
            )
        create_invalid.assert_not_called()

    def test_current_intent_get_uses_authoritative_head_resolver(self):
        current = SimpleNamespace(id=uuid.UUID(INTENT_ID))
        stale = SimpleNamespace(
            id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        )
        timestamp_ordered = SimpleNamespace(
            order_by=lambda *_args, **_kwargs: SimpleNamespace(first=lambda: stale)
        )

        with (
            patch.object(
                api,
                "resolve_current_publication_intent",
                return_value=current,
                create=True,
            ),
            patch.object(
                api.PublicationIntent.objects,
                "filter",
                return_value=timestamp_ordered,
            ),
            patch.object(
                api,
                "intent_json",
                side_effect=lambda row: {"id": str(row.id)},
            ),
        ):
            response = api.publication_intents(
                _admin_get(
                    f"/api/v1/articles/{ARTICLE_ID}/publication-intents",
                ),
                article_id=uuid.UUID(ARTICLE_ID),
            )

        self.assertEqual(json.loads(response.content), {"item": {"id": INTENT_ID}})

    def test_dispatch_endpoint_returns_new_and_replay_status_with_exact_result(self):
        payload = _publish_request_payload()
        result, _accepted_at = _dispatch_result()
        audit_context = object()

        with (
            patch.object(api.AuditContext, "for_admin", return_value=audit_context),
            patch.object(
                api,
                "dispatch_publication",
                side_effect=((result, True), (result, False)),
            ) as dispatch,
        ):
            created = api.publish(
                _admin_request(f"/api/v1/articles/{ARTICLE_ID}/publish", payload),
                article_id=uuid.UUID(ARTICLE_ID),
            )
            replayed = api.publish(
                _admin_request(f"/api/v1/articles/{ARTICLE_ID}/publish", payload),
                article_id=uuid.UUID(ARTICLE_ID),
            )

        self.assertEqual(created.status_code, 202)
        self.assertEqual(replayed.status_code, 200)
        self.assertFalse(json.loads(created.content)["replayed"])
        self.assertTrue(json.loads(replayed.content)["replayed"])
        _validator("PublicationDispatchResult").validate(json.loads(created.content))
        _validator("PublicationDispatchResult").validate(json.loads(replayed.content))
        self.assertEqual(dispatch.call_count, 2)
        for call in dispatch.call_args_list:
            self.assertEqual(call.args[:2], (uuid.UUID(ARTICLE_ID), payload))
            self.assertEqual(call.kwargs["audit_context"], audit_context)

    def test_publication_audit_context_maps_sanitizer_failures_to_invalid_input(self):
        request = _admin_request(
            f"/api/v1/articles/{ARTICLE_ID}/publication-intents",
            _intent_request_payload(),
        )
        rejected = (
            {**_intent_request_payload(), "requestKey": "unsafe key"},
            {
                **_intent_request_payload(),
                "reason": "api_key=must-not-enter-audit",
            },
        )

        for payload in rejected:
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidInput):
                    api._publication_audit_context(request, payload)

    def test_intent_and_dispatch_status_contracts_are_explicit(self):
        paths = load_openapi_contract()["paths"]
        intent_responses = paths[
            "/articles/{articleId}/publication-intents"
        ]["post"]["responses"]
        dispatch_responses = paths[
            "/articles/{articleId}/publish"
        ]["post"]["responses"]

        self.assertEqual(
            {"200", "201", "409", "422"},
            {status for status in intent_responses if status != "default"},
        )
        self.assertEqual(
            {"200", "202", "409", "422"},
            {status for status in dispatch_responses if status != "default"},
        )
