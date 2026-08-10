from __future__ import annotations

import hashlib
import uuid

from django.test import TestCase
from django.utils import timezone

from wisdome_writer.infrastructure import outbox
from wisdome_writer.infrastructure.models import (
    OutboxConsumerReceipt,
    OutboxMessage,
)


class OutboxTerminalReservationTests(TestCase):
    def _delivery(self):
        aggregate_id = uuid.uuid4()
        event = OutboxMessage.objects.create(
            message_key=f"test.terminal:{aggregate_id}",
            topic="test.terminal",
            aggregate_type="test_aggregate",
            aggregate_id=aggregate_id,
            payload={},
            correlation_id=uuid.uuid4(),
            job_id=aggregate_id,
            immutable_material_hash="a" * 64,
        )
        token = uuid.uuid4()
        receipt = OutboxConsumerReceipt.objects.create(
            event=event,
            consumer_name="test-consumer",
            state=OutboxConsumerReceipt.State.PROCESSING,
            attempts=4,
            claimed_at=timezone.now(),
            claimed_until=timezone.now() + timezone.timedelta(minutes=1),
            lease_token=token,
            lease_generation=7,
        )
        return event, receipt, token

    def test_terminal_callback_observes_reserved_capability_before_dlq(self):
        event, receipt, token = self._delivery()
        observed = []

        def terminal(_error_code):
            row = OutboxConsumerReceipt.objects.get(pk=receipt.pk)
            observed.append(
                (
                    row.state,
                    row.terminal_reserved_at,
                    row.terminal_lease_generation,
                    row.terminal_lease_token_hash,
                    row.terminal_error_code,
                )
            )

        result = outbox.dead_letter_consumer_event(
            event.id,
            consumer_name="test-consumer",
            error_code="consumer_exhausted",
            terminal_handler=terminal,
            expected_consumer_lease_token=token,
            expected_consumer_lease_generation=7,
        )

        receipt.refresh_from_db()
        self.assertEqual(result["state"], "dead_letter")
        self.assertEqual(
            observed,
            [
                (
                    OutboxConsumerReceipt.State.PROCESSING,
                    receipt.terminal_reserved_at,
                    7,
                    hashlib.sha256(str(token).encode("ascii")).hexdigest(),
                    "consumer_exhausted",
                )
            ],
        )
        self.assertIsNotNone(receipt.terminal_reserved_at)
        self.assertEqual(receipt.state, OutboxConsumerReceipt.State.DEAD_LETTER)

    def test_terminal_callback_failure_rolls_back_the_reservation(self):
        event, receipt, token = self._delivery()
        observed = []

        def terminal(_error_code):
            row = OutboxConsumerReceipt.objects.get(pk=receipt.pk)
            observed.append(row.terminal_reserved_at)
            raise RuntimeError("terminal callback failed")

        with self.assertRaisesRegex(RuntimeError, "terminal callback failed"):
            outbox.dead_letter_consumer_event(
                event.id,
                consumer_name="test-consumer",
                error_code="consumer_exhausted",
                terminal_handler=terminal,
                expected_consumer_lease_token=token,
                expected_consumer_lease_generation=7,
            )

        receipt.refresh_from_db()
        self.assertIsNotNone(observed[0])
        self.assertIsNone(receipt.terminal_reserved_at)
        self.assertEqual(receipt.state, OutboxConsumerReceipt.State.PROCESSING)
        self.assertEqual(receipt.lease_token, token)
        self.assertEqual(receipt.lease_generation, 7)
