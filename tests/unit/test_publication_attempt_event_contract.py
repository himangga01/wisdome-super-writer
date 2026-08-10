import os

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")

from django.test import SimpleTestCase

from wisdome_writer.infrastructure.event_routes import (
    payload_schema_for,
    route_for,
)
from wisdome_writer.infrastructure.outbox import (
    ForbiddenEventPayload,
    _validate_event_payload,
    routed_event_arguments,
)


ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"


class PublicationAttemptEventContractTests(SimpleTestCase):
    def test_publication_requested_v2_routes_the_exact_business_attempt(self):
        route = route_for("publication.requested", 2)
        schema = payload_schema_for("publication.requested", 2)

        self.assertIsNotNone(route)
        self.assertIsNotNone(schema)
        self.assertEqual(
            route.argument_keys,
            ("publication_attempt_id", "execution_attempt_no"),
        )
        self.assertEqual(
            route.terminal_argument_keys,
            ("publication_attempt_id", "execution_attempt_no"),
        )
        self.assertEqual(
            schema.required,
            frozenset({"publication_attempt_id", "execution_attempt_no"}),
        )
        payload = {
            "publication_attempt_id": ATTEMPT_ID,
            "execution_attempt_no": 3,
        }
        _validate_event_payload("publication.requested", 2, payload)
        self.assertEqual(
            routed_event_arguments(
                "publication.requested",
                2,
                route.argument_keys,
                payload,
            ),
            [ATTEMPT_ID, 3],
        )

    def test_publication_requested_v2_rejects_missing_extra_and_out_of_budget_fields(self):
        invalid_payloads = (
            {"publication_attempt_id": ATTEMPT_ID},
            {
                "publication_attempt_id": ATTEMPT_ID,
                "execution_attempt_no": 1,
                "attempt": 1,
            },
            {
                "publication_attempt_id": ATTEMPT_ID,
                "execution_attempt_no": 1,
                "execution_generation": 1,
            },
            {
                "publication_attempt_id": ATTEMPT_ID,
                "execution_attempt_no": 1,
                "lease_token": "22222222-2222-4222-8222-222222222222",
            },
            {
                "publication_attempt_id": ATTEMPT_ID,
                "execution_attempt_no": 1,
                "external_write_authorized": False,
            },
            {
                "publication_attempt_id": ATTEMPT_ID,
                "execution_attempt_no": 0,
            },
            {
                "publication_attempt_id": ATTEMPT_ID,
                "execution_attempt_no": 6,
            },
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ForbiddenEventPayload):
                    _validate_event_payload("publication.requested", 2, payload)

    def test_publication_requested_v1_remains_the_id_only_legacy_route(self):
        route = route_for("publication.requested", 1)
        schema = payload_schema_for("publication.requested", 1)

        self.assertEqual(route.argument_keys, ("publication_attempt_id",))
        self.assertEqual(
            route.terminal_argument_keys,
            ("publication_attempt_id",),
        )
        self.assertEqual(schema.required, frozenset({"publication_attempt_id"}))
        _validate_event_payload(
            "publication.requested",
            1,
            {"publication_attempt_id": ATTEMPT_ID},
        )
        with self.assertRaises(ForbiddenEventPayload):
            _validate_event_payload(
                "publication.requested",
                1,
                {
                    "publication_attempt_id": ATTEMPT_ID,
                    "execution_attempt_no": 1,
                },
            )

    def test_reconcile_v2_keeps_the_exact_bounded_generation(self):
        route = route_for("publication.reconcile_requested", 2)
        schema = payload_schema_for("publication.reconcile_requested", 2)

        self.assertEqual(
            route.argument_keys,
            ("publication_attempt_id", "reconcile_attempt_no"),
        )
        self.assertEqual(
            route.terminal_argument_keys,
            ("publication_attempt_id", "reconcile_attempt_no"),
        )
        self.assertEqual(
            schema.required,
            frozenset({"publication_attempt_id", "reconcile_attempt_no"}),
        )
        _validate_event_payload(
            "publication.reconcile_requested",
            2,
            {
                "publication_attempt_id": ATTEMPT_ID,
                "reconcile_attempt_no": 5,
            },
        )
        with self.assertRaises(ForbiddenEventPayload):
            _validate_event_payload(
                "publication.reconcile_requested",
                2,
                {
                    "publication_attempt_id": ATTEMPT_ID,
                    "reconcile_attempt_no": 6,
                },
            )

        for internal_field, value in (
            ("lease_token", "22222222-2222-4222-8222-222222222222"),
            ("lease_generation", 2),
            ("reconcile_generation_id", "33333333-3333-4333-8333-333333333333"),
        ):
            with self.subTest(internal_field=internal_field):
                with self.assertRaises(ForbiddenEventPayload):
                    _validate_event_payload(
                        "publication.reconcile_requested",
                        2,
                        {
                            "publication_attempt_id": ATTEMPT_ID,
                            "reconcile_attempt_no": 1,
                            internal_field: value,
                        },
                    )

    def test_reconcile_v1_remains_id_only_for_one_time_historical_binding(self):
        route = route_for("publication.reconcile_requested", 1)
        schema = payload_schema_for("publication.reconcile_requested", 1)

        self.assertEqual(route.argument_keys, ("publication_attempt_id",))
        self.assertEqual(
            route.terminal_argument_keys,
            ("publication_attempt_id",),
        )
        self.assertEqual(schema.required, frozenset({"publication_attempt_id"}))
        _validate_event_payload(
            "publication.reconcile_requested",
            1,
            {"publication_attempt_id": ATTEMPT_ID},
        )
        with self.assertRaises(ForbiddenEventPayload):
            _validate_event_payload(
                "publication.reconcile_requested",
                1,
                {
                    "publication_attempt_id": ATTEMPT_ID,
                    "reconcile_attempt_no": 1,
                },
            )
