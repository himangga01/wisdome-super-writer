import json
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.utils import timezone
from jsonpointer import resolve_pointer

from adapters.sources.base import CollectedSourceRecord, SourceAttachment
from apps.collection import services
from apps.collection.models import CollectionRun, RunSourceItem, RunStep, SourceCollectionAttempt
from apps.editorial.services import _frozen_evidence_snapshot
from apps.evidence import tasks as evidence_tasks
from apps.evidence.models import validate_evidence_locator
from apps.topics.models import (
    SourceDefinition,
    SourceRegistryMembership,
    SourceRegistrySnapshot,
    TopicPolicy,
)
from apps.topics.services import (
    _apply_source_projection,
    _create_draft_snapshot,
    normalize_registry_import,
    registry_manifest_hash_for_memberships,
)
from wisdome_writer.infrastructure.models import OutboxMessage

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def collection_attempt(db):
    config = json.loads(
        (ROOT / "config/source-registry/housing_subscription.json").read_text("utf-8")
    )
    material = normalize_registry_import(config)["sources"][0]["material"]
    source = SourceDefinition(key="review-official-source")
    _apply_source_projection(source, material)
    source.save()
    snapshot = _create_draft_snapshot(
        source=source,
        material=material,
        version=1,
        request_key=None,
        request_hash=None,
        using="default",
    )
    registry = SourceRegistrySnapshot.objects.create(
        topic_code=material["topic"],
        version=1,
        manifest_hash="a" * 64,
    )
    SourceRegistryMembership.objects.create(
        registry=registry,
        source_definition=source,
        source_snapshot=snapshot,
        enabled=True,
    )
    registry.manifest_hash = registry_manifest_hash_for_memberships(
        registry.memberships.select_related("source_snapshot").all(),
    )
    registry.row_version += 1
    registry.save()
    now = timezone.now()
    policy_material = {"allowedAuthorityTiers": ["primary_official"]}
    policy = TopicPolicy.objects.create(
        code=material["topic"],
        version=1,
        title="Review topic",
        freshness_minutes=1440,
        policy=policy_material,
        policy_hash=services._hash(policy_material),
    )
    run = CollectionRun.objects.create(
        display_id="REVIEW-" + uuid.uuid4().hex[:8],
        topic_code=material["topic"],
        window_start=now - timedelta(hours=1),
        window_end=now,
        source_registry=registry,
        registry_manifest_hash=registry.manifest_hash,
        topic_policy=policy,
        policy_version=policy.version,
        policy_hash=policy.policy_hash,
        freshness_minutes=1440,
        allowed_authority_tiers=["primary_official"],
        freshness_cutoff=now - timedelta(days=1),
        request_fingerprint=uuid.uuid4().hex * 2,
        state="collecting",
    )
    return (
        SourceCollectionAttempt.objects.create(
            run=run,
            source_snapshot=snapshot,
            **services._source_attempt_material(run, snapshot),
        ),
        source,
        now,
    )


@pytest.mark.parametrize("with_attachment", [False, True])
def test_real_source_collection_persists_optional_attachments_and_plain_change_event(
    collection_attempt,
    with_attachment,
):
    attempt, source, now = collection_attempt
    attachments = (
        (
            SourceAttachment(
                url="https://www.applyhome.co.kr/review.pdf",
                title="Official notice.pdf",
                mime_type="application/pdf",
            ),
        )
        if with_attachment
        else ()
    )
    record = CollectedSourceRecord(
        external_id="review-notice",
        canonical_url="https://www.applyhome.co.kr/review",
        title="Official notice",
        publisher=source.publisher,
        published_at=now,
        collected_at=now,
        body_text="Applications open today.",
        attachments=attachments,
    )

    class Adapter:
        request_count = 1

        def collect(self, **kwargs):
            return [record]

        def close(self):
            pass

    with patch.object(services, "build_source_adapter", return_value=Adapter()):
        result = services.collect_source_attempt(attempt.id, delivery_attempt_no=1)
    assert result.state == "succeeded"
    link = RunSourceItem.objects.get(run=attempt.run)
    assert len(link.source_item.attachments) == int(with_attachment)
    changed = OutboxMessage.objects.get(topic="source.item_changed", aggregate_id=link.id)
    assert changed.payload["change_kind"] == "new_version"
    assert type(changed.payload["change_kind"]) is str


@pytest.mark.parametrize("successor", ["failed", "running"])
def test_stale_collector_cannot_settle_over_a_newer_delivery(collection_attempt, successor):
    attempt, source, now = collection_attempt
    record = CollectedSourceRecord(
        external_id="stale-notice",
        canonical_url="https://www.applyhome.co.kr/stale",
        title="Old response",
        publisher=source.publisher,
        published_at=now,
        collected_at=now,
        body_text="Older response must not be committed.",
    )

    class Adapter:
        request_count = 1

        def collect(self, **kwargs):
            if successor == "failed":
                services.finalize_source_attempt_delivery_failure(
                    attempt.id,
                    "newer_delivery_failed",
                    delivery_attempt_no=2,
                )
            else:
                SourceCollectionAttempt.objects.filter(pk=attempt.id).update(
                    state="running",
                    retry_count=1,
                )
            return [record]

        def close(self):
            pass

    with patch.object(services, "build_source_adapter", return_value=Adapter()):
        result = services.collect_source_attempt(attempt.id, delivery_attempt_no=1)
    assert result.state == successor
    assert not RunSourceItem.objects.filter(run=attempt.run).exists()
    assert not OutboxMessage.objects.filter(
        topic="source.item_changed", job_id=attempt.run_id
    ).exists()


def test_real_raw_record_locator_resolves_the_immutable_bound_body(collection_attempt):
    attempt, source, now = collection_attempt
    record = CollectedSourceRecord(
        external_id="raw-notice",
        canonical_url="https://www.applyhome.co.kr/raw",
        title="Official notice",
        publisher=source.publisher,
        published_at=now,
        collected_at=now,
        body_text="Applications open today.",
    )
    adapter = type(
        "Adapter",
        (),
        {
            "request_count": 1,
            "collect": lambda self, **kwargs: [record],
            "close": lambda self: None,
        },
    )()
    with patch.object(services, "build_source_adapter", return_value=adapter):
        services.collect_source_attempt(attempt.id, delivery_attempt_no=1)
    link = RunSourceItem.objects.select_related("source_item", "source_snapshot").get(
        run=attempt.run
    )
    run = link.run
    run.state = "extracting"
    run.save()
    token = uuid.uuid4()
    RunStep.objects.create(
        run=run,
        name="extract",
        state="running",
        source_event_id=uuid.uuid4(),
        lease_generation=1,
        delivery_count=1,
        lease_owner="review-fanout",
        lease_token=token,
    )
    evidence = evidence_tasks._create_raw_evidence(
        link,
        fanout_fence={
            "expected_generation": 1,
            "expected_lease_owner": "review-fanout",
            "expected_lease_token": token,
        },
    )
    validate_evidence_locator(evidence.locator_type, evidence.locator)
    assert resolve_pointer(evidence.structured_data, evidence.locator["path"]) == record.body_text
    snapshot = _frozen_evidence_snapshot(
        run=run,
        evidence_rows=[evidence],
        expected_evidence={
            str(evidence.id): {
                "authorityTier": "primary_official",
                "independenceGroup": "official",
                "originIdentityHash": "official-origin",
            }
        },
    )
    assert record.body_text in snapshot[0]["sourceText"]
