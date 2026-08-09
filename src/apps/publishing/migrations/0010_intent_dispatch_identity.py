from __future__ import annotations

import hashlib
import json
import uuid

import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


INTENT_REQUEST_VERSION = "publication-intent-request-v1"
DISPATCH_REQUEST_VERSION = "publication-dispatch-request-v1"
LEGACY_UNVERIFIABLE_VERSION = "legacy-unverifiable-v1"


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def legacy_unverifiable_request_hash(*, publication_intent_id, intent_hash) -> str:
    return _hash(
        {
            "schemaVersion": LEGACY_UNVERIFIABLE_VERSION,
            "publicationIntentId": str(publication_intent_id),
            "intentHash": intent_hash,
        }
    )


def _command_for(intent, target_id):
    rows = [
        row
        for row in (intent.target_commands or [])
        if isinstance(row, dict) and str(row.get("targetId")) == str(target_id)
    ]
    if len(rows) != 1:
        raise RuntimeError(
            "T020 migration found an ambiguous intent target command: "
            f"intent={intent.id}; target={target_id}; count={len(rows)}"
        )
    return rows[0]


def _snapshot_ref_for(intent, target_id):
    rows = [
        row
        for row in (intent.target_snapshot_refs or [])
        if isinstance(row, dict) and str(row.get("targetId")) == str(target_id)
    ]
    if len(rows) != 1:
        raise RuntimeError(
            "T020 migration found an ambiguous intent target snapshot ref: "
            f"intent={intent.id}; target={target_id}; count={len(rows)}"
        )
    return rows[0]


def _activation_ref_for(intent, target_id):
    rows = [
        row
        for row in (intent.auto_publish_activation_refs or [])
        if isinstance(row, dict) and str(row.get("targetId")) == str(target_id)
    ]
    if len(rows) != 1:
        raise RuntimeError(
            "T020 migration found an ambiguous intent activation ref: "
            f"intent={intent.id}; target={target_id}; count={len(rows)}"
        )
    return rows[0]


def _validate_attempt(attempt) -> None:
    intent = attempt.publication_intent
    publication = attempt.publication
    snapshot = attempt.target_snapshot
    approval = attempt.approval
    target_id = publication.target_id
    command = _command_for(intent, target_id)
    ref = _snapshot_ref_for(intent, target_id)
    activation_ref = (
        _activation_ref_for(intent, target_id)
        if intent.approval_mode == "validated_auto"
        else None
    )
    if (
        attempt.article_revision_id != intent.article_revision_id
        or publication.article_id != intent.article_id
        or attempt.remote_lookup_key != publication.remote_lookup_key
        or snapshot.target_id != target_id
        or str(attempt.target_snapshot_id) != str(ref.get("targetSnapshotId"))
        or attempt.target_config_hash != ref.get("targetConfigHash")
        or attempt.publisher_contract_version
        != snapshot.publisher_contract_version
        or attempt.publisher_adapter_manifest_hash
        != snapshot.publisher_adapter_manifest_hash
        or str(command.get("targetSnapshotId")) != str(attempt.target_snapshot_id)
        or command.get("targetConfigHash") != attempt.target_config_hash
        or command.get("resolvedAction") != attempt.resolved_action
        or command.get("targetCommandHash") != attempt.target_command_hash
        or approval.publication_intent_id != intent.id
        or approval.article_revision_id != attempt.article_revision_id
        or approval.target_id != target_id
        or approval.target_snapshot_id != attempt.target_snapshot_id
        or approval.target_config_hash != attempt.target_config_hash
        or approval.target_action != attempt.resolved_action
        or approval.decision != "approved"
        or approval.approval_subject_hash != attempt.approval_subject_hash
        or (
            intent.approval_mode == "manual"
            and (
                attempt.auto_publish_activation_id is not None
                or attempt.auto_publish_activation_hash is not None
            )
        )
        or (
            intent.approval_mode == "validated_auto"
            and (
                str(attempt.auto_publish_activation_id)
                != str(activation_ref.get("activationId"))
                or attempt.auto_publish_activation_hash
                != activation_ref.get("activationHash")
            )
        )
        or not approval.headed_by.filter(
            publication_intent_id=intent.id,
            target_id=target_id,
            latest_approval_id=approval.id,
            subject_hash=attempt.approval_subject_hash,
        ).exists()
    ):
        raise RuntimeError(
            "T020 migration found a PublicationAttempt with corrupt frozen lineage: "
            f"attempt={attempt.id}"
        )


def validate_legacy_t020_rows(apps, schema_editor):
    Intent = apps.get_model("publishing", "PublicationIntent")
    Attempt = apps.get_model("publishing", "PublicationAttempt")
    Publication = apps.get_model("publishing", "Publication")
    Snapshot = apps.get_model("publishing", "PublicationTargetSnapshot")
    alias = schema_editor.connection.alias

    snapshot_targets = {
        row.id: row.target_id
        for row in Snapshot.objects.using(alias).order_by("id")
    }
    for publication in Publication.objects.using(alias).order_by("id"):
        if (
            not isinstance(publication.remote_lookup_key, str)
            or not publication.remote_lookup_key.strip()
            or snapshot_targets.get(publication.origin_target_snapshot_id)
            != publication.target_id
        ):
            raise RuntimeError(
                "T020 migration found a Publication frozen identity "
                f"that cannot be proven: publication={publication.id}"
            )

    by_article = {}
    request_identities = set()
    for intent in Intent.objects.using(alias).order_by("article_id", "created_at", "id"):
        identity = (intent.article_id, intent.request_key)
        if identity in request_identities:
            raise RuntimeError(
                "T020 migration found duplicate article request identity: "
                f"article={intent.article_id}; request_key={intent.request_key}"
            )
        request_identities.add(identity)
        by_article.setdefault(intent.article_id, []).append(intent)

    for article_id, rows in by_article.items():
        by_id = {row.id: row for row in rows}
        children = {row.id: [] for row in rows}
        roots = []
        for row in rows:
            parent_id = row.supersedes_intent_id
            if parent_id is None:
                roots.append(row)
                continue
            parent = by_id.get(parent_id)
            if parent is None:
                raise RuntimeError(
                    "T020 migration found a missing/cross-article intent predecessor: "
                    f"intent={row.id}; predecessor={parent_id}"
                )
            children[parent.id].append(row)
        if len(roots) != 1 or any(len(value) > 1 for value in children.values()):
            raise RuntimeError(
                "T020 migration found an ambiguous publication intent chain: "
                f"article={article_id}; roots={len(roots)}"
            )
        seen = set()
        cursor = roots[0]
        while True:
            if cursor.id in seen:
                raise RuntimeError("T020 migration found a publication intent cycle")
            seen.add(cursor.id)
            next_rows = children[cursor.id]
            if not next_rows:
                break
            cursor = next_rows[0]
        if len(seen) != len(rows):
            raise RuntimeError(
                "T020 migration found a disconnected publication intent chain: "
                f"article={article_id}"
            )

    attempt_groups = {}
    attempt_targets_by_intent = {}
    for attempt in (
        Attempt.objects.using(alias)
        .select_related(
            "publication_intent",
            "publication",
            "target_snapshot",
            "approval",
        )
        .order_by("publication_intent_id", "publication_id", "id")
    ):
        key = (attempt.publication_intent_id, attempt.publication_id)
        if key in attempt_groups:
            raise RuntimeError(
                "T020 migration found duplicate target attempts: "
                f"intent={key[0]}; publication={key[1]}"
            )
        attempt_groups[key] = attempt.id
        attempt_targets_by_intent.setdefault(
            attempt.publication_intent_id, []
        ).append(str(attempt.publication.target_id))
        _validate_attempt(attempt)

    for intent in Intent.objects.using(alias).order_by("article_id", "created_at", "id"):
        attempt_targets = attempt_targets_by_intent.get(intent.id)
        if attempt_targets is None:
            continue
        command_targets = [
            str(row.get("targetId"))
            for row in (intent.target_commands or [])
            if isinstance(row, dict) and row.get("targetId") is not None
        ]
        snapshot_targets = [
            str(row.get("targetId"))
            for row in (intent.target_snapshot_refs or [])
            if isinstance(row, dict) and row.get("targetId") is not None
        ]
        if (
            not command_targets
            or len(command_targets) != len(set(command_targets))
            or len(snapshot_targets) != len(set(snapshot_targets))
            or len(attempt_targets) != len(set(attempt_targets))
            or set(command_targets) != set(snapshot_targets)
            or set(command_targets) != set(attempt_targets)
        ):
            raise RuntimeError(
                "T020 migration found an incomplete target cohort: "
                f"intent={intent.id}"
            )


def _attempt_manifest(attempts):
    return [
        {
            "attemptId": str(row.id),
            "publicationId": str(row.publication_id),
            "targetId": str(row.publication.target_id),
            "attemptNo": 1,
            "resolvedAction": row.resolved_action,
        }
        for row in sorted(attempts, key=lambda item: str(item.id))
    ]


def _legacy_attempt_manifest(attempts):
    return [
        {
            **row,
            "state": "queued",
        }
        for row in _attempt_manifest(attempts)
    ]


def backfill_t020_identity(apps, schema_editor):
    Intent = apps.get_model("publishing", "PublicationIntent")
    IntentHead = apps.get_model("publishing", "PublicationIntentHead")
    Dispatch = apps.get_model("publishing", "PublicationDispatch")
    Attempt = apps.get_model("publishing", "PublicationAttempt")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    alias = schema_editor.connection.alias

    intents = list(
        Intent.objects.using(alias).order_by("article_id", "created_at", "id")
    )
    by_article = {}
    for intent in intents:
        Intent.objects.using(alias).filter(pk=intent.id).update(
            request_hash=legacy_unverifiable_request_hash(
                publication_intent_id=intent.id,
                intent_hash=intent.intent_hash,
            ),
            request_hash_version=LEGACY_UNVERIFIABLE_VERSION,
        )
        by_article.setdefault(intent.article_id, []).append(intent)

    for article_id, rows in by_article.items():
        parent_ids = {
            row.supersedes_intent_id
            for row in rows
            if row.supersedes_intent_id is not None
        }
        leaves = [row for row in rows if row.id not in parent_ids]
        if len(leaves) != 1:
            raise RuntimeError(
                "T020 migration cannot choose an ambiguous intent head: "
                f"article={article_id}; leaves={len(leaves)}"
            )
        IntentHead.objects.using(alias).create(
            id=leaves[0].id,
            article_id=article_id,
            latest_intent_id=leaves[0].id,
            version=len(rows),
        )

    attempts_by_intent = {}
    for attempt in (
        Attempt.objects.using(alias)
        .select_related("publication")
        .order_by("publication_intent_id", "id")
    ):
        attempts_by_intent.setdefault(attempt.publication_intent_id, []).append(
            attempt
        )

    for intent_id, attempts in attempts_by_intent.items():
        intent = next(row for row in intents if row.id == intent_id)
        events = list(
            AuditEvent.objects.using(alias)
            .filter(
                action="publication.dispatched",
                entity_type="publishing.publicationintent",
                entity_id=intent_id,
            )
            .order_by("occurred_at", "id")
        )
        if len(events) != 1:
            raise RuntimeError(
                "T020 migration cannot prove a historical dispatch request: "
                f"intent={intent_id}; audit_events={len(events)}"
            )
        event = events[0]
        metadata = event.metadata_redacted or {}
        request_key = metadata.get("request_key")
        request_hash = metadata.get("request_hash")
        manifest = _attempt_manifest(attempts)
        manifest_hash = _hash(manifest)
        legacy_manifest_hash = _hash(_legacy_attempt_manifest(attempts))
        if (
            not isinstance(request_key, str)
            or not request_key
            or not isinstance(request_hash, str)
            or len(request_hash) != 64
            or metadata.get("count") != len(attempts)
            or metadata.get("result_hash") != legacy_manifest_hash
            or any(row.correlation_id != event.correlation_id for row in attempts)
            or intent.state != "dispatched"
        ):
            raise RuntimeError(
                "T020 migration found unverifiable historical dispatch material: "
                f"intent={intent_id}"
            )
        dispatch = Dispatch.objects.using(alias).create(
            publication_intent_id=intent_id,
            request_key=request_key,
            request_hash=request_hash,
            request_hash_version=LEGACY_UNVERIFIABLE_VERSION,
            correlation_id=event.correlation_id,
            attempt_count=len(attempts),
            attempt_manifest_hash=manifest_hash,
        )
        Dispatch.objects.using(alias).filter(pk=dispatch.id).update(
            accepted_at=event.occurred_at
        )

    dispatched_without_attempts = [
        str(row.id)
        for row in intents
        if row.state == "dispatched" and row.id not in attempts_by_intent
    ]
    if dispatched_without_attempts:
        raise RuntimeError(
            "T020 migration found dispatched intents without attempts: "
            + ",".join(dispatched_without_attempts)
        )


SQLITE_TRIGGER_NAMES = (
    "publishing_intent_insert_guard_t020",
    "publishing_intent_advance_head_t020",
    "publishing_intent_update_guard_t020",
    "publishing_intent_delete_guard_t020",
    "publishing_intent_head_insert_guard_t020",
    "publishing_intent_head_update_guard_t020",
    "publishing_intent_head_delete_guard_t020",
    "publishing_dispatch_insert_guard_t020",
    "publishing_dispatch_update_guard_t020",
    "publishing_dispatch_delete_guard_t020",
    "publishing_attempt_insert_guard_t020",
    "publishing_attempt_update_guard_t020",
    "publishing_attempt_delete_guard_t020",
    "publishing_target_snapshot_update_guard_t020",
    "publishing_target_snapshot_delete_guard_t020",
    "publishing_publication_insert_guard_t020",
    "publishing_publication_update_guard_t020",
)


SQLITE_GUARD_SQL = (
    """
    CREATE TRIGGER publishing_intent_insert_guard_t020
    BEFORE INSERT ON publishing_publicationintent
    BEGIN
      SELECT CASE WHEN NEW.request_hash_version <> 'publication-intent-request-v1'
        THEN RAISE(ABORT, 'new publication intent request version is invalid') END;
      SELECT CASE WHEN length(NEW.request_hash) <> 64
        OR NEW.request_hash GLOB '*[^0-9a-f]*'
        THEN RAISE(ABORT, 'publication intent request hash is invalid') END;
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM editorial_articlerevision revision
        WHERE revision.id = NEW.article_revision_id
          AND revision.article_id = NEW.article_id
          AND revision.revision_no = NEW.revision_no
          AND revision.content_hash = NEW.revision_content_hash
      ) THEN RAISE(ABORT, 'publication intent revision lineage is invalid') END;
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM publishing_publicationintenthead
        WHERE article_id = NEW.article_id
      ) AND NEW.supersedes_intent_id IS NOT NULL
        THEN RAISE(ABORT, 'root publication intent has a predecessor') END;
      SELECT CASE WHEN EXISTS (
        SELECT 1 FROM publishing_publicationintenthead
        WHERE article_id = NEW.article_id
          AND latest_intent_id IS NOT NEW.supersedes_intent_id
      ) THEN RAISE(ABORT, 'publication intent predecessor is not current head') END;
    END
    """,
    """
    CREATE TRIGGER publishing_intent_advance_head_t020
    AFTER INSERT ON publishing_publicationintent
    BEGIN
      UPDATE publishing_publicationintenthead
      SET latest_intent_id = NEW.id,
          version = version + 1,
          updated_at = CURRENT_TIMESTAMP
      WHERE article_id = NEW.article_id;
      INSERT INTO publishing_publicationintenthead
        (id, article_id, latest_intent_id, version, updated_at)
      SELECT NEW.id, NEW.article_id, NEW.id, 1, CURRENT_TIMESTAMP
      WHERE changes() = 0;
    END
    """,
    """
    CREATE TRIGGER publishing_intent_update_guard_t020
    BEFORE UPDATE ON publishing_publicationintent
    WHEN NEW.id IS NOT OLD.id
      OR NEW.article_id IS NOT OLD.article_id
      OR NEW.article_revision_id IS NOT OLD.article_revision_id
      OR NEW.revision_no IS NOT OLD.revision_no
      OR NEW.revision_content_hash IS NOT OLD.revision_content_hash
      OR NEW.correction_case_id IS NOT OLD.correction_case_id
      OR NEW.origin_collection_run_id IS NOT OLD.origin_collection_run_id
      OR NEW.target_snapshot_refs IS NOT OLD.target_snapshot_refs
      OR NEW.target_commands IS NOT OLD.target_commands
      OR NEW.target_snapshot_manifest_hash IS NOT OLD.target_snapshot_manifest_hash
      OR NEW.approval_mode IS NOT OLD.approval_mode
      OR NEW.auto_publish_validation_refs IS NOT OLD.auto_publish_validation_refs
      OR NEW.auto_validation_manifest_hash IS NOT OLD.auto_validation_manifest_hash
      OR NEW.auto_publish_activation_refs IS NOT OLD.auto_publish_activation_refs
      OR NEW.auto_activation_manifest_hash IS NOT OLD.auto_activation_manifest_hash
      OR NEW.generation_attempt_id IS NOT OLD.generation_attempt_id
      OR NEW.input_evidence_manifest_hash IS NOT OLD.input_evidence_manifest_hash
      OR NEW.generation_pipeline_manifest_hash IS NOT OLD.generation_pipeline_manifest_hash
      OR NEW.quality_gate_manifest_hash IS NOT OLD.quality_gate_manifest_hash
      OR NEW.quality_report_hash IS NOT OLD.quality_report_hash
      OR NEW.supersedes_intent_id IS NOT OLD.supersedes_intent_id
      OR NEW.intent_hash IS NOT OLD.intent_hash
      OR NEW.request_key IS NOT OLD.request_key
      OR NEW.request_hash IS NOT OLD.request_hash
      OR NEW.request_hash_version IS NOT OLD.request_hash_version
      OR NEW.created_by_id IS NOT OLD.created_by_id
      OR NEW.created_at IS NOT OLD.created_at
    BEGIN SELECT RAISE(ABORT, 'publication intent frozen identity is immutable'); END
    """,
    """CREATE TRIGGER publishing_intent_delete_guard_t020 BEFORE DELETE ON publishing_publicationintent BEGIN SELECT RAISE(ABORT, 'publication intent is append-only'); END""",
    """
    CREATE TRIGGER publishing_intent_head_insert_guard_t020
    BEFORE INSERT ON publishing_publicationintenthead
    WHEN NEW.version <> 1 OR NOT EXISTS (
      SELECT 1 FROM publishing_publicationintent intent
      WHERE intent.id = NEW.latest_intent_id
        AND intent.article_id = NEW.article_id
        AND intent.supersedes_intent_id IS NULL
    )
    BEGIN SELECT RAISE(ABORT, 'publication intent head root is invalid'); END
    """,
    """
    CREATE TRIGGER publishing_intent_head_update_guard_t020
    BEFORE UPDATE ON publishing_publicationintenthead
    WHEN NEW.id IS NOT OLD.id
      OR NEW.article_id IS NOT OLD.article_id
      OR NEW.version <> OLD.version + 1
      OR NOT EXISTS (
        SELECT 1 FROM publishing_publicationintent intent
        WHERE intent.id = NEW.latest_intent_id
          AND intent.article_id = OLD.article_id
          AND intent.supersedes_intent_id = OLD.latest_intent_id
      )
    BEGIN SELECT RAISE(ABORT, 'publication intent head advance is invalid'); END
    """,
    """CREATE TRIGGER publishing_intent_head_delete_guard_t020 BEFORE DELETE ON publishing_publicationintenthead BEGIN SELECT RAISE(ABORT, 'publication intent head cannot be deleted'); END""",
    """
    CREATE TRIGGER publishing_dispatch_insert_guard_t020
    BEFORE INSERT ON publishing_publicationdispatch
    BEGIN
      SELECT CASE WHEN NEW.request_hash_version <> 'publication-dispatch-request-v1'
        OR length(NEW.request_key) < 1
        OR length(NEW.request_hash) <> 64
        OR NEW.request_hash GLOB '*[^0-9a-f]*'
        OR length(NEW.attempt_manifest_hash) <> 64
        OR NEW.attempt_manifest_hash GLOB '*[^0-9a-f]*'
        OR NEW.attempt_count < 1
        THEN RAISE(ABORT, 'publication dispatch request identity is invalid') END;
      SELECT CASE WHEN NEW.attempt_count <> (
        SELECT count(*) FROM publishing_publicationattempt
        WHERE publication_intent_id = NEW.publication_intent_id
      ) THEN RAISE(ABORT, 'publication dispatch attempt count is invalid') END;
      SELECT CASE WHEN EXISTS (
        SELECT 1 FROM publishing_publicationattempt
        WHERE publication_intent_id = NEW.publication_intent_id
          AND correlation_id IS NOT NEW.correlation_id
      ) THEN RAISE(ABORT, 'publication dispatch correlation is invalid') END;
      SELECT CASE WHEN NEW.attempt_count <> (
        SELECT json_array_length(target_commands)
        FROM publishing_publicationintent
        WHERE id = NEW.publication_intent_id
      ) OR NEW.attempt_count <> (
        SELECT json_array_length(target_snapshot_refs)
        FROM publishing_publicationintent
        WHERE id = NEW.publication_intent_id
      ) THEN RAISE(ABORT, 'publication dispatch target cohort is incomplete') END;
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM publishing_publicationintent
        WHERE id = NEW.publication_intent_id
          AND state = 'dispatched'
      ) THEN RAISE(ABORT, 'publication dispatch intent state is invalid') END;
      SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM publishing_publicationattempt attempt
        JOIN publishing_publication publication
          ON publication.id = attempt.publication_id
        WHERE attempt.publication_intent_id = NEW.publication_intent_id
          AND NOT EXISTS (
            SELECT 1
            FROM publishing_approval approval
            JOIN publishing_publicationapprovalhead head
              ON head.publication_intent_id = attempt.publication_intent_id
             AND head.target_id = publication.target_id
            WHERE approval.id = attempt.approval_id
              AND approval.publication_intent_id = attempt.publication_intent_id
              AND approval.target_id = publication.target_id
              AND approval.decision = 'approved'
              AND approval.approval_subject_hash = attempt.approval_subject_hash
              AND head.latest_approval_id = approval.id
              AND head.subject_hash = attempt.approval_subject_hash
          )
      ) THEN RAISE(ABORT, 'publication dispatch approval cohort is stale') END;
      SELECT CASE WHEN EXISTS (
        SELECT 1 FROM publishing_publicationattempt
        WHERE publication_intent_id = NEW.publication_intent_id
          AND resolved_action IN ('create', 'update')
      ) AND NOT EXISTS (
        SELECT 1
        FROM publishing_publicationintent intent
        JOIN publishing_publicationintenthead head
          ON head.article_id = intent.article_id
         AND head.latest_intent_id = intent.id
        WHERE intent.id = NEW.publication_intent_id
      ) THEN RAISE(ABORT, 'publication dispatch intent is not current') END;
    END
    """,
    """CREATE TRIGGER publishing_dispatch_update_guard_t020 BEFORE UPDATE ON publishing_publicationdispatch BEGIN SELECT RAISE(ABORT, 'publication dispatch is append-only'); END""",
    """CREATE TRIGGER publishing_dispatch_delete_guard_t020 BEFORE DELETE ON publishing_publicationdispatch BEGIN SELECT RAISE(ABORT, 'publication dispatch is append-only'); END""",
    """
    CREATE TRIGGER publishing_attempt_insert_guard_t020
    BEFORE INSERT ON publishing_publicationattempt
    BEGIN
      SELECT CASE WHEN NEW.attempt_no <> 1
        THEN RAISE(ABORT, 'initial publication attempt number must be one') END;
      SELECT CASE WHEN EXISTS (
        SELECT 1 FROM publishing_publicationdispatch
        WHERE publication_intent_id = NEW.publication_intent_id
      ) THEN RAISE(ABORT, 'publication dispatch attempt cohort is frozen') END;
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM publishing_publicationintent intent
        JOIN publishing_publication publication ON publication.id = NEW.publication_id
        JOIN publishing_publicationtargetsnapshot snapshot ON snapshot.id = NEW.target_snapshot_id
        JOIN publishing_approval approval ON approval.id = NEW.approval_id
        JOIN publishing_publicationapprovalhead head
          ON head.publication_intent_id = intent.id
         AND head.target_id = publication.target_id
        WHERE intent.id = NEW.publication_intent_id
          AND intent.article_revision_id = NEW.article_revision_id
          AND intent.article_id = publication.article_id
          AND publication.remote_lookup_key = NEW.remote_lookup_key
          AND snapshot.target_id = publication.target_id
          AND snapshot.config_hash = NEW.target_config_hash
          AND snapshot.publisher_contract_version = NEW.publisher_contract_version
          AND snapshot.publisher_adapter_manifest_hash = NEW.publisher_adapter_manifest_hash
          AND approval.publication_intent_id = intent.id
          AND approval.article_revision_id = NEW.article_revision_id
          AND approval.target_id = publication.target_id
          AND approval.target_snapshot_id = NEW.target_snapshot_id
          AND approval.target_config_hash = NEW.target_config_hash
          AND approval.target_action = NEW.resolved_action
          AND approval.decision = 'approved'
          AND approval.approval_subject_hash = NEW.approval_subject_hash
          AND head.latest_approval_id = approval.id
          AND head.subject_hash = NEW.approval_subject_hash
          AND (
            (NEW.resolved_action IN ('create', 'update')
             AND intent.state = 'approved'
             AND EXISTS (
               SELECT 1 FROM publishing_publicationintenthead intent_head
               WHERE intent_head.article_id = intent.article_id
                 AND intent_head.latest_intent_id = intent.id
             ))
            OR
            (NEW.resolved_action IN ('unpublish', 'mark_withdrawn')
             AND intent.state IN ('approved', 'stale'))
          )
          AND (
            (intent.approval_mode = 'manual'
             AND NEW.auto_publish_activation_id IS NULL
             AND NEW.auto_publish_activation_hash IS NULL)
            OR
            (intent.approval_mode = 'validated_auto'
             AND (SELECT count(*) FROM json_each(intent.auto_publish_activation_refs) activation
                  WHERE replace(json_extract(activation.value, '$.targetId'), '-', '') = publication.target_id
                    AND replace(json_extract(activation.value, '$.activationId'), '-', '') = NEW.auto_publish_activation_id
                    AND json_extract(activation.value, '$.activationHash') = NEW.auto_publish_activation_hash) = 1)
          )
          AND (SELECT count(*) FROM json_each(intent.target_snapshot_refs) ref
               WHERE replace(json_extract(ref.value, '$.targetId'), '-', '') = publication.target_id
                 AND replace(json_extract(ref.value, '$.targetSnapshotId'), '-', '') = NEW.target_snapshot_id
                 AND json_extract(ref.value, '$.targetConfigHash') = NEW.target_config_hash) = 1
          AND (SELECT count(*) FROM json_each(intent.target_commands) command
               WHERE replace(json_extract(command.value, '$.targetId'), '-', '') = publication.target_id
                 AND replace(json_extract(command.value, '$.targetSnapshotId'), '-', '') = NEW.target_snapshot_id
                 AND json_extract(command.value, '$.targetConfigHash') = NEW.target_config_hash
                 AND json_extract(command.value, '$.resolvedAction') = NEW.resolved_action
                 AND json_extract(command.value, '$.targetCommandHash') = NEW.target_command_hash) = 1
      ) THEN RAISE(ABORT, 'publication attempt frozen lineage is invalid') END;
    END
    """,
    """
    CREATE TRIGGER publishing_attempt_update_guard_t020
    BEFORE UPDATE ON publishing_publicationattempt
    WHEN NEW.id IS NOT OLD.id
      OR NEW.publication_id IS NOT OLD.publication_id
      OR NEW.article_revision_id IS NOT OLD.article_revision_id
      OR NEW.publication_intent_id IS NOT OLD.publication_intent_id
      OR NEW.target_snapshot_id IS NOT OLD.target_snapshot_id
      OR NEW.target_config_hash IS NOT OLD.target_config_hash
      OR NEW.resolved_action IS NOT OLD.resolved_action
      OR NEW.target_command_hash IS NOT OLD.target_command_hash
      OR NEW.publisher_contract_version IS NOT OLD.publisher_contract_version
      OR NEW.publisher_adapter_manifest_hash IS NOT OLD.publisher_adapter_manifest_hash
      OR NEW.approval_id IS NOT OLD.approval_id
      OR NEW.approval_subject_hash IS NOT OLD.approval_subject_hash
      OR NEW.auto_publish_activation_id IS NOT OLD.auto_publish_activation_id
      OR NEW.auto_publish_activation_hash IS NOT OLD.auto_publish_activation_hash
      OR NEW.idempotency_key IS NOT OLD.idempotency_key
      OR NEW.remote_lookup_key IS NOT OLD.remote_lookup_key
      OR NEW.request_fingerprint IS NOT OLD.request_fingerprint
      OR NEW.correlation_id IS NOT OLD.correlation_id
      OR NEW.created_at IS NOT OLD.created_at
    BEGIN SELECT RAISE(ABORT, 'publication attempt frozen identity is immutable'); END
    """,
    """CREATE TRIGGER publishing_attempt_delete_guard_t020 BEFORE DELETE ON publishing_publicationattempt BEGIN SELECT RAISE(ABORT, 'publication attempt is append-only'); END""",
    """CREATE TRIGGER publishing_target_snapshot_update_guard_t020 BEFORE UPDATE ON publishing_publicationtargetsnapshot BEGIN SELECT RAISE(ABORT, 'publication target snapshot is append-only'); END""",
    """CREATE TRIGGER publishing_target_snapshot_delete_guard_t020 BEFORE DELETE ON publishing_publicationtargetsnapshot BEGIN SELECT RAISE(ABORT, 'publication target snapshot is append-only'); END""",
    """
    CREATE TRIGGER publishing_publication_insert_guard_t020
    BEFORE INSERT ON publishing_publication
    BEGIN
      SELECT CASE WHEN NEW.remote_lookup_key IS NULL
        OR length(trim(NEW.remote_lookup_key)) < 1
        THEN RAISE(ABORT, 'publication remote lookup identity is invalid') END;
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM publishing_publicationtargetsnapshot snapshot
        WHERE snapshot.id = NEW.origin_target_snapshot_id
          AND snapshot.target_id = NEW.target_id
      ) THEN RAISE(ABORT, 'publication origin snapshot identity is invalid') END;
    END
    """,
    """
    CREATE TRIGGER publishing_publication_update_guard_t020
    BEFORE UPDATE ON publishing_publication
    WHEN NEW.id IS NOT OLD.id
      OR NEW.article_id IS NOT OLD.article_id
      OR NEW.target_id IS NOT OLD.target_id
      OR NEW.origin_target_snapshot_id IS NOT OLD.origin_target_snapshot_id
      OR NEW.remote_lookup_key IS NOT OLD.remote_lookup_key
      OR NEW.created_at IS NOT OLD.created_at
    BEGIN SELECT RAISE(ABORT, 'publication frozen identity is immutable'); END
    """,
)


POSTGRES_GUARD_SQL = (
    """
    CREATE OR REPLACE FUNCTION publishing_intent_insert_guard_t020_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE current_head uuid;
    BEGIN
      IF NEW.request_hash_version <> 'publication-intent-request-v1'
         OR NEW.request_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'publication intent request identity is invalid';
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM editorial_articlerevision revision
        WHERE revision.id = NEW.article_revision_id
          AND revision.article_id = NEW.article_id
          AND revision.revision_no = NEW.revision_no
          AND revision.content_hash = NEW.revision_content_hash
      ) THEN RAISE EXCEPTION 'publication intent revision lineage is invalid'; END IF;
      SELECT latest_intent_id INTO current_head
      FROM publishing_publicationintenthead
      WHERE article_id = NEW.article_id FOR UPDATE;
      IF FOUND THEN
        IF NEW.supersedes_intent_id IS DISTINCT FROM current_head THEN
          RAISE EXCEPTION 'publication intent predecessor is not current head';
        END IF;
      ELSIF NEW.supersedes_intent_id IS NOT NULL THEN
        RAISE EXCEPTION 'root publication intent has a predecessor';
      END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER publishing_intent_insert_guard_t020 BEFORE INSERT ON publishing_publicationintent FOR EACH ROW EXECUTE FUNCTION publishing_intent_insert_guard_t020_fn()""",
    """
    CREATE OR REPLACE FUNCTION publishing_intent_advance_head_t020_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      UPDATE publishing_publicationintenthead
      SET latest_intent_id = NEW.id,
          version = version + 1,
          updated_at = CURRENT_TIMESTAMP
      WHERE article_id = NEW.article_id;
      IF NOT FOUND THEN
        INSERT INTO publishing_publicationintenthead
          (id, article_id, latest_intent_id, version, updated_at)
        VALUES (NEW.id, NEW.article_id, NEW.id, 1, CURRENT_TIMESTAMP);
      END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER publishing_intent_advance_head_t020 AFTER INSERT ON publishing_publicationintent FOR EACH ROW EXECUTE FUNCTION publishing_intent_advance_head_t020_fn()""",
    """
    CREATE OR REPLACE FUNCTION publishing_intent_update_guard_t020_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF ROW(NEW.id, NEW.article_id, NEW.article_revision_id, NEW.revision_no,
             NEW.revision_content_hash, NEW.correction_case_id,
             NEW.origin_collection_run_id, NEW.target_snapshot_refs,
             NEW.target_commands, NEW.target_snapshot_manifest_hash,
             NEW.approval_mode, NEW.auto_publish_validation_refs,
             NEW.auto_validation_manifest_hash, NEW.auto_publish_activation_refs,
             NEW.auto_activation_manifest_hash, NEW.generation_attempt_id,
             NEW.input_evidence_manifest_hash,
             NEW.generation_pipeline_manifest_hash,
             NEW.quality_gate_manifest_hash, NEW.quality_report_hash,
             NEW.supersedes_intent_id, NEW.intent_hash, NEW.request_key,
             NEW.request_hash, NEW.request_hash_version, NEW.created_by_id,
             NEW.created_at)
         IS DISTINCT FROM
         ROW(OLD.id, OLD.article_id, OLD.article_revision_id, OLD.revision_no,
             OLD.revision_content_hash, OLD.correction_case_id,
             OLD.origin_collection_run_id, OLD.target_snapshot_refs,
             OLD.target_commands, OLD.target_snapshot_manifest_hash,
             OLD.approval_mode, OLD.auto_publish_validation_refs,
             OLD.auto_validation_manifest_hash, OLD.auto_publish_activation_refs,
             OLD.auto_activation_manifest_hash, OLD.generation_attempt_id,
             OLD.input_evidence_manifest_hash,
             OLD.generation_pipeline_manifest_hash,
             OLD.quality_gate_manifest_hash, OLD.quality_report_hash,
             OLD.supersedes_intent_id, OLD.intent_hash, OLD.request_key,
             OLD.request_hash, OLD.request_hash_version, OLD.created_by_id,
             OLD.created_at) THEN
        RAISE EXCEPTION 'publication intent frozen identity is immutable';
      END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER publishing_intent_update_guard_t020 BEFORE UPDATE ON publishing_publicationintent FOR EACH ROW EXECUTE FUNCTION publishing_intent_update_guard_t020_fn()""",
    """
    CREATE OR REPLACE FUNCTION publishing_intent_delete_guard_t020_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      RAISE EXCEPTION 'publication intent is append-only';
    END $$
    """,
    """CREATE TRIGGER publishing_intent_delete_guard_t020 BEFORE DELETE ON publishing_publicationintent FOR EACH ROW EXECUTE FUNCTION publishing_intent_delete_guard_t020_fn()""",
    """
    CREATE OR REPLACE FUNCTION publishing_intent_head_guard_t020_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'publication intent head cannot be deleted'; END IF;
      IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 1 OR NOT EXISTS (
          SELECT 1 FROM publishing_publicationintent intent
          WHERE intent.id = NEW.latest_intent_id
            AND intent.article_id = NEW.article_id
            AND intent.supersedes_intent_id IS NULL
        ) THEN RAISE EXCEPTION 'publication intent head root is invalid'; END IF;
      ELSE
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.article_id IS DISTINCT FROM OLD.article_id
           OR NEW.version <> OLD.version + 1
           OR NOT EXISTS (
             SELECT 1 FROM publishing_publicationintent intent
             WHERE intent.id = NEW.latest_intent_id
               AND intent.article_id = OLD.article_id
               AND intent.supersedes_intent_id = OLD.latest_intent_id
           ) THEN RAISE EXCEPTION 'publication intent head advance is invalid'; END IF;
      END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER publishing_intent_head_insert_guard_t020 BEFORE INSERT ON publishing_publicationintenthead FOR EACH ROW EXECUTE FUNCTION publishing_intent_head_guard_t020_fn()""",
    """CREATE TRIGGER publishing_intent_head_update_guard_t020 BEFORE UPDATE ON publishing_publicationintenthead FOR EACH ROW EXECUTE FUNCTION publishing_intent_head_guard_t020_fn()""",
    """CREATE TRIGGER publishing_intent_head_delete_guard_t020 BEFORE DELETE ON publishing_publicationintenthead FOR EACH ROW EXECUTE FUNCTION publishing_intent_head_guard_t020_fn()""",
    """
    CREATE OR REPLACE FUNCTION publishing_dispatch_guard_t020_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'publication dispatch is append-only'; END IF;
      PERFORM intent_head.id
      FROM publishing_publicationintent intent
      JOIN publishing_publicationintenthead intent_head
        ON intent_head.article_id = intent.article_id
      WHERE intent.id = NEW.publication_intent_id
        AND EXISTS (
          SELECT 1 FROM publishing_publicationattempt attempt
          WHERE attempt.publication_intent_id = intent.id
            AND attempt.resolved_action IN ('create', 'update')
        )
      FOR UPDATE OF intent_head;
      PERFORM intent.id
      FROM publishing_publicationintent intent
      WHERE intent.id = NEW.publication_intent_id
      FOR UPDATE OF intent;
      PERFORM head.id
      FROM publishing_publicationapprovalhead head
      JOIN publishing_publicationattempt attempt
        ON attempt.publication_intent_id = head.publication_intent_id
      JOIN publishing_publication publication
        ON publication.id = attempt.publication_id
       AND publication.target_id = head.target_id
      WHERE attempt.publication_intent_id = NEW.publication_intent_id
      ORDER BY head.target_id
      FOR UPDATE OF head;
      IF NEW.request_hash_version <> 'publication-dispatch-request-v1'
         OR length(NEW.request_key) < 1
         OR NEW.request_hash !~ '^[0-9a-f]{64}$'
         OR NEW.attempt_manifest_hash !~ '^[0-9a-f]{64}$'
         OR NEW.attempt_count < 1
         OR NEW.attempt_count <> (SELECT count(*) FROM publishing_publicationattempt WHERE publication_intent_id = NEW.publication_intent_id)
         OR NEW.attempt_count <> (SELECT jsonb_array_length(target_commands) FROM publishing_publicationintent WHERE id = NEW.publication_intent_id)
         OR NEW.attempt_count <> (SELECT jsonb_array_length(target_snapshot_refs) FROM publishing_publicationintent WHERE id = NEW.publication_intent_id)
         OR NOT EXISTS (SELECT 1 FROM publishing_publicationintent WHERE id = NEW.publication_intent_id AND state = 'dispatched')
         OR EXISTS (SELECT 1 FROM publishing_publicationattempt WHERE publication_intent_id = NEW.publication_intent_id AND correlation_id IS DISTINCT FROM NEW.correlation_id)
         OR EXISTS (
           SELECT 1
           FROM publishing_publicationattempt attempt
           JOIN publishing_publication publication
             ON publication.id = attempt.publication_id
           WHERE attempt.publication_intent_id = NEW.publication_intent_id
             AND NOT EXISTS (
               SELECT 1
               FROM publishing_approval approval
               JOIN publishing_publicationapprovalhead head
                 ON head.publication_intent_id = attempt.publication_intent_id
                AND head.target_id = publication.target_id
               WHERE approval.id = attempt.approval_id
                 AND approval.publication_intent_id = attempt.publication_intent_id
                 AND approval.target_id = publication.target_id
                 AND approval.decision = 'approved'
                 AND approval.approval_subject_hash = attempt.approval_subject_hash
                 AND head.latest_approval_id = approval.id
                 AND head.subject_hash = attempt.approval_subject_hash
             )
         )
         OR (
           EXISTS (
             SELECT 1 FROM publishing_publicationattempt attempt
             WHERE attempt.publication_intent_id = NEW.publication_intent_id
               AND attempt.resolved_action IN ('create', 'update')
           )
           AND NOT EXISTS (
             SELECT 1
             FROM publishing_publicationintent intent
             JOIN publishing_publicationintenthead head
               ON head.article_id = intent.article_id
              AND head.latest_intent_id = intent.id
             WHERE intent.id = NEW.publication_intent_id
           )
         )
      THEN RAISE EXCEPTION 'publication dispatch request identity is invalid'; END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER publishing_dispatch_insert_guard_t020 BEFORE INSERT ON publishing_publicationdispatch FOR EACH ROW EXECUTE FUNCTION publishing_dispatch_guard_t020_fn()""",
    """CREATE TRIGGER publishing_dispatch_update_guard_t020 BEFORE UPDATE ON publishing_publicationdispatch FOR EACH ROW EXECUTE FUNCTION publishing_dispatch_guard_t020_fn()""",
    """CREATE TRIGGER publishing_dispatch_delete_guard_t020 BEFORE DELETE ON publishing_publicationdispatch FOR EACH ROW EXECUTE FUNCTION publishing_dispatch_guard_t020_fn()""",
    """
    CREATE OR REPLACE FUNCTION publishing_attempt_guard_t020_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'publication attempt is append-only'; END IF;
      IF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.id, NEW.publication_id, NEW.article_revision_id,
               NEW.publication_intent_id, NEW.target_snapshot_id,
               NEW.target_config_hash, NEW.resolved_action,
               NEW.target_command_hash, NEW.publisher_contract_version,
               NEW.publisher_adapter_manifest_hash, NEW.approval_id,
               NEW.approval_subject_hash, NEW.auto_publish_activation_id,
               NEW.auto_publish_activation_hash, NEW.idempotency_key,
               NEW.remote_lookup_key, NEW.request_fingerprint,
               NEW.correlation_id, NEW.created_at)
           IS DISTINCT FROM
           ROW(OLD.id, OLD.publication_id, OLD.article_revision_id,
               OLD.publication_intent_id, OLD.target_snapshot_id,
               OLD.target_config_hash, OLD.resolved_action,
               OLD.target_command_hash, OLD.publisher_contract_version,
               OLD.publisher_adapter_manifest_hash, OLD.approval_id,
               OLD.approval_subject_hash, OLD.auto_publish_activation_id,
               OLD.auto_publish_activation_hash, OLD.idempotency_key,
               OLD.remote_lookup_key, OLD.request_fingerprint,
               OLD.correlation_id, OLD.created_at) THEN
          RAISE EXCEPTION 'publication attempt frozen identity is immutable';
        END IF;
        RETURN NEW;
      END IF;
      IF EXISTS (
        SELECT 1 FROM publishing_publicationdispatch
        WHERE publication_intent_id = NEW.publication_intent_id
      ) THEN RAISE EXCEPTION 'publication dispatch attempt cohort is frozen'; END IF;
      PERFORM intent_head.id
      FROM publishing_publicationintent intent
      JOIN publishing_publicationintenthead intent_head
        ON intent_head.article_id = intent.article_id
      WHERE intent.id = NEW.publication_intent_id
        AND NEW.resolved_action IN ('create', 'update')
      FOR UPDATE OF intent_head;
      PERFORM intent.id
      FROM publishing_publicationintent intent
      WHERE intent.id = NEW.publication_intent_id
      FOR UPDATE OF intent;
      PERFORM head.id
      FROM publishing_publication publication
      JOIN publishing_publicationapprovalhead head
        ON head.publication_intent_id = NEW.publication_intent_id
       AND head.target_id = publication.target_id
      WHERE publication.id = NEW.publication_id
      FOR UPDATE OF head;
      IF NEW.attempt_no <> 1 OR NOT EXISTS (
        SELECT 1
        FROM publishing_publicationintent intent
        JOIN publishing_publication publication ON publication.id = NEW.publication_id
        JOIN publishing_publicationtargetsnapshot snapshot ON snapshot.id = NEW.target_snapshot_id
        JOIN publishing_approval approval ON approval.id = NEW.approval_id
        JOIN publishing_publicationapprovalhead head
          ON head.publication_intent_id = intent.id
         AND head.target_id = publication.target_id
        WHERE intent.id = NEW.publication_intent_id
          AND intent.article_revision_id = NEW.article_revision_id
          AND intent.article_id = publication.article_id
          AND publication.remote_lookup_key = NEW.remote_lookup_key
          AND snapshot.target_id = publication.target_id
          AND snapshot.config_hash = NEW.target_config_hash
          AND snapshot.publisher_contract_version = NEW.publisher_contract_version
          AND snapshot.publisher_adapter_manifest_hash = NEW.publisher_adapter_manifest_hash
          AND approval.publication_intent_id = intent.id
          AND approval.article_revision_id = NEW.article_revision_id
          AND approval.target_id = publication.target_id
          AND approval.target_snapshot_id = NEW.target_snapshot_id
          AND approval.target_config_hash = NEW.target_config_hash
          AND approval.target_action = NEW.resolved_action
          AND approval.decision = 'approved'
          AND approval.approval_subject_hash = NEW.approval_subject_hash
          AND head.latest_approval_id = approval.id
          AND head.subject_hash = NEW.approval_subject_hash
          AND (
            (NEW.resolved_action IN ('create', 'update')
             AND intent.state = 'approved'
             AND EXISTS (
               SELECT 1 FROM publishing_publicationintenthead intent_head
               WHERE intent_head.article_id = intent.article_id
                 AND intent_head.latest_intent_id = intent.id
             ))
            OR
            (NEW.resolved_action IN ('unpublish', 'mark_withdrawn')
             AND intent.state IN ('approved', 'stale'))
          )
          AND (
            (intent.approval_mode = 'manual'
             AND NEW.auto_publish_activation_id IS NULL
             AND NEW.auto_publish_activation_hash IS NULL)
            OR
            (intent.approval_mode = 'validated_auto'
             AND (SELECT count(*) FROM jsonb_array_elements(intent.auto_publish_activation_refs) activation
                  WHERE activation->>'targetId' = publication.target_id::text
                    AND activation->>'activationId' = NEW.auto_publish_activation_id::text
                    AND activation->>'activationHash' = NEW.auto_publish_activation_hash) = 1)
          )
          AND (SELECT count(*) FROM jsonb_array_elements(intent.target_snapshot_refs) ref
               WHERE ref->>'targetId' = publication.target_id::text
                 AND ref->>'targetSnapshotId' = NEW.target_snapshot_id::text
                 AND ref->>'targetConfigHash' = NEW.target_config_hash) = 1
          AND (SELECT count(*) FROM jsonb_array_elements(intent.target_commands) command
               WHERE command->>'targetId' = publication.target_id::text
                 AND command->>'targetSnapshotId' = NEW.target_snapshot_id::text
                 AND command->>'targetConfigHash' = NEW.target_config_hash
                 AND command->>'resolvedAction' = NEW.resolved_action
                 AND command->>'targetCommandHash' = NEW.target_command_hash) = 1
      ) THEN RAISE EXCEPTION 'publication attempt frozen lineage is invalid'; END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER publishing_attempt_insert_guard_t020 BEFORE INSERT ON publishing_publicationattempt FOR EACH ROW EXECUTE FUNCTION publishing_attempt_guard_t020_fn()""",
    """CREATE TRIGGER publishing_attempt_update_guard_t020 BEFORE UPDATE ON publishing_publicationattempt FOR EACH ROW EXECUTE FUNCTION publishing_attempt_guard_t020_fn()""",
    """CREATE TRIGGER publishing_attempt_delete_guard_t020 BEFORE DELETE ON publishing_publicationattempt FOR EACH ROW EXECUTE FUNCTION publishing_attempt_guard_t020_fn()""",
    """
    CREATE OR REPLACE FUNCTION publishing_target_snapshot_guard_t020_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      RAISE EXCEPTION 'publication target snapshot is append-only';
    END $$
    """,
    """CREATE TRIGGER publishing_target_snapshot_update_guard_t020 BEFORE UPDATE ON publishing_publicationtargetsnapshot FOR EACH ROW EXECUTE FUNCTION publishing_target_snapshot_guard_t020_fn()""",
    """CREATE TRIGGER publishing_target_snapshot_delete_guard_t020 BEFORE DELETE ON publishing_publicationtargetsnapshot FOR EACH ROW EXECUTE FUNCTION publishing_target_snapshot_guard_t020_fn()""",
    """
    CREATE OR REPLACE FUNCTION publishing_publication_insert_guard_t020_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF btrim(COALESCE(NEW.remote_lookup_key, '')) = '' THEN
        RAISE EXCEPTION 'publication remote lookup identity is invalid';
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM publishing_publicationtargetsnapshot snapshot
        WHERE snapshot.id = NEW.origin_target_snapshot_id
          AND snapshot.target_id = NEW.target_id
      ) THEN
        RAISE EXCEPTION 'publication origin snapshot identity is invalid';
      END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER publishing_publication_insert_guard_t020 BEFORE INSERT ON publishing_publication FOR EACH ROW EXECUTE FUNCTION publishing_publication_insert_guard_t020_fn()""",
    """
    CREATE OR REPLACE FUNCTION publishing_publication_update_guard_t020_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF ROW(NEW.id, NEW.article_id, NEW.target_id,
             NEW.origin_target_snapshot_id, NEW.remote_lookup_key,
             NEW.created_at)
         IS DISTINCT FROM
         ROW(OLD.id, OLD.article_id, OLD.target_id,
             OLD.origin_target_snapshot_id, OLD.remote_lookup_key,
             OLD.created_at) THEN
        RAISE EXCEPTION 'publication frozen identity is immutable';
      END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER publishing_publication_update_guard_t020 BEFORE UPDATE ON publishing_publication FOR EACH ROW EXECUTE FUNCTION publishing_publication_update_guard_t020_fn()""",
)


POSTGRES_TRIGGER_NAMES = SQLITE_TRIGGER_NAMES
POSTGRES_FUNCTION_NAMES = (
    "publishing_intent_insert_guard_t020_fn",
    "publishing_intent_advance_head_t020_fn",
    "publishing_intent_update_guard_t020_fn",
    "publishing_intent_delete_guard_t020_fn",
    "publishing_intent_head_guard_t020_fn",
    "publishing_dispatch_guard_t020_fn",
    "publishing_attempt_guard_t020_fn",
    "publishing_target_snapshot_guard_t020_fn",
    "publishing_publication_insert_guard_t020_fn",
    "publishing_publication_update_guard_t020_fn",
)

POSTGRES_TRIGGER_TABLES = {
    "publishing_intent_insert_guard_t020": "publishing_publicationintent",
    "publishing_intent_advance_head_t020": "publishing_publicationintent",
    "publishing_intent_update_guard_t020": "publishing_publicationintent",
    "publishing_intent_delete_guard_t020": "publishing_publicationintent",
    "publishing_intent_head_insert_guard_t020": "publishing_publicationintenthead",
    "publishing_intent_head_update_guard_t020": "publishing_publicationintenthead",
    "publishing_intent_head_delete_guard_t020": "publishing_publicationintenthead",
    "publishing_dispatch_insert_guard_t020": "publishing_publicationdispatch",
    "publishing_dispatch_update_guard_t020": "publishing_publicationdispatch",
    "publishing_dispatch_delete_guard_t020": "publishing_publicationdispatch",
    "publishing_attempt_insert_guard_t020": "publishing_publicationattempt",
    "publishing_attempt_update_guard_t020": "publishing_publicationattempt",
    "publishing_attempt_delete_guard_t020": "publishing_publicationattempt",
    "publishing_target_snapshot_update_guard_t020": "publishing_publicationtargetsnapshot",
    "publishing_target_snapshot_delete_guard_t020": "publishing_publicationtargetsnapshot",
    "publishing_publication_insert_guard_t020": "publishing_publication",
    "publishing_publication_update_guard_t020": "publishing_publication",
}


def install_t020_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    statements = SQLITE_GUARD_SQL if vendor == "sqlite" else POSTGRES_GUARD_SQL
    if vendor not in {"sqlite", "postgresql"}:
        raise RuntimeError(f"T020 database guards do not support {vendor}")
    with schema_editor.connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def remove_t020_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    with schema_editor.connection.cursor() as cursor:
        if vendor == "sqlite":
            for name in SQLITE_TRIGGER_NAMES:
                cursor.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        elif vendor == "postgresql":
            for name in POSTGRES_TRIGGER_NAMES:
                cursor.execute(
                    f'DROP TRIGGER IF EXISTS "{name}" ON '
                    f'"{POSTGRES_TRIGGER_TABLES[name]}"'
                )
            for name in POSTGRES_FUNCTION_NAMES:
                cursor.execute(f'DROP FUNCTION IF EXISTS "{name}"()')
        else:
            raise RuntimeError(f"T020 database guards do not support {vendor}")


def reject_populated_reverse(apps, schema_editor):
    models_to_check = (
        apps.get_model("publishing", "PublicationIntent"),
        apps.get_model("publishing", "PublicationAttempt"),
        apps.get_model("publishing", "PublicationIntentHead"),
        apps.get_model("publishing", "PublicationDispatch"),
        apps.get_model("publishing", "PublicationTargetSnapshot"),
        apps.get_model("publishing", "Publication"),
    )
    if any(model.objects.using(schema_editor.connection.alias).exists() for model in models_to_check):
        raise IrreversibleError(
            "T020 intent/dispatch identity cannot be reversed while publication rows exist"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0009_approval_decision_integrity"),
        ("audit", "0002_auditevent_append_only"),
    ]

    operations = [
        migrations.RunPython(validate_legacy_t020_rows, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="publicationintent",
            name="uq_intent_revision_request",
        ),
        migrations.AlterField(
            model_name="publicationintent",
            name="intent_hash",
            field=models.CharField(max_length=64),
        ),
        migrations.RenameField(
            model_name="publicationintent",
            old_name="supersedes_intent_id",
            new_name="supersedes_intent",
        ),
        migrations.AlterField(
            model_name="publicationintent",
            name="supersedes_intent",
            field=models.ForeignKey(
                blank=True,
                db_column="supersedes_intent_id",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="superseded_by",
                to="publishing.publicationintent",
            ),
        ),
        migrations.AddField(
            model_name="publicationintent",
            name="request_hash",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="publicationintent",
            name="request_hash_version",
            field=models.CharField(max_length=40, null=True),
        ),
        migrations.CreateModel(
            name="PublicationIntentHead",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("article_id", models.UUIDField(unique=True)),
                ("version", models.PositiveIntegerField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "latest_intent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="headed_by",
                        to="publishing.publicationintent",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(version__gte=1),
                        name="ck_publication_intent_head_version_positive",
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="PublicationDispatch",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("request_key", models.CharField(max_length=200)),
                ("request_hash", models.CharField(max_length=64)),
                ("request_hash_version", models.CharField(max_length=40)),
                ("correlation_id", models.UUIDField(db_index=True)),
                ("accepted_at", models.DateTimeField(auto_now_add=True)),
                ("attempt_count", models.PositiveIntegerField()),
                ("attempt_manifest_hash", models.CharField(max_length=64)),
                (
                    "publication_intent",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dispatch",
                        to="publishing.publicationintent",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(attempt_count__gte=1),
                        name="ck_publication_dispatch_attempt_count_positive",
                    )
                ]
            },
        ),
        migrations.RunPython(backfill_t020_identity, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="publicationintent",
            name="request_hash",
            field=models.CharField(max_length=64),
        ),
        migrations.AlterField(
            model_name="publicationintent",
            name="request_hash_version",
            field=models.CharField(max_length=40),
        ),
        migrations.AddConstraint(
            model_name="publicationintent",
            constraint=models.UniqueConstraint(
                fields=("article_id", "request_key"),
                name="uq_intent_article_request",
            ),
        ),
        migrations.AddConstraint(
            model_name="publicationattempt",
            constraint=models.UniqueConstraint(
                fields=("publication_intent", "publication"),
                name="uq_attempt_intent_publication",
            ),
        ),
        migrations.RunPython(install_t020_guards, remove_t020_guards),
        migrations.RunPython(
            migrations.RunPython.noop,
            reverse_code=reject_populated_reverse,
        ),
    ]
