import hashlib
import importlib

import django.db.models.deletion
import rfc8785
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


SUBJECT_VERSION = "approval-subject-v3"
DECISION_VERSION = "approval-decision-v1"

POSTGRES_APPROVAL_INSERT_FUNCTION = "publishing_validate_approval_insert"
POSTGRES_APPROVAL_INSERT_TRIGGER = "publishing_approval_insert_guard"
POSTGRES_APPROVAL_ADVANCE_FUNCTION = "publishing_advance_approval_head"
POSTGRES_APPROVAL_ADVANCE_TRIGGER = "publishing_approval_head_advance"
POSTGRES_APPROVAL_APPEND_FUNCTION = "publishing_reject_approval_mutation"
POSTGRES_APPROVAL_APPEND_TRIGGER = "publishing_approval_append_only"
POSTGRES_HEAD_FUNCTION = "publishing_validate_approval_head"
POSTGRES_HEAD_TRIGGER = "publishing_approval_head_guard"
POSTGRES_RENDER_FUNCTION = "publishing_reject_approved_render_mutation"
POSTGRES_RENDER_TRIGGER = "publishing_approved_render_append_only"

SQLITE_APPROVAL_INSERT_TRIGGER = "publishing_approval_insert_guard"
SQLITE_APPROVAL_ADVANCE_TRIGGER = "publishing_approval_head_advance"
SQLITE_APPROVAL_UPDATE_TRIGGER = "publishing_approval_no_update"
SQLITE_APPROVAL_DELETE_TRIGGER = "publishing_approval_no_delete"
SQLITE_HEAD_INSERT_TRIGGER = "publishing_approval_head_insert_guard"
SQLITE_HEAD_UPDATE_TRIGGER = "publishing_approval_head_update_guard"
SQLITE_HEAD_DELETE_TRIGGER = "publishing_approval_head_no_delete"
SQLITE_RENDER_UPDATE_TRIGGER = "publishing_approved_render_no_update"
SQLITE_RENDER_DELETE_TRIGGER = "publishing_approved_render_no_delete"


def _hash(value):
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def _id(value):
    return str(value) if value is not None else None


def _mapping_by_target(rows, *, label):
    if not isinstance(rows, list):
        raise RuntimeError(f"Approval admission found invalid {label}")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("targetId"):
            raise RuntimeError(f"Approval admission found invalid {label}")
        target_id = str(row["targetId"])
        if target_id in result:
            raise RuntimeError(f"Approval admission found duplicate target in {label}")
        result[target_id] = row
    return result


def _normalized_validation_refs(rows):
    return sorted(
        [
            {
                "targetId": str(row["targetId"]),
                "targetSnapshotId": str(row["targetSnapshotId"]),
                "validationId": str(row["validationId"]),
                "decisionId": str(row["decisionId"]),
                "decisionVersion": int(row["decisionVersion"]),
                "decisionHash": row["decisionHash"],
                "validationMaterialHash": row["validationMaterialHash"],
            }
            for row in rows
        ],
        key=lambda row: row["targetId"],
    )


def _intent_hash(intent, revision, target_refs, commands):
    return _hash(
        {
            "articleRevisionId": str(revision.id),
            "revisionNo": revision.revision_no,
            "revisionContentHash": revision.content_hash,
            "targetSnapshots": sorted(
                target_refs.values(), key=lambda row: str(row["targetId"])
            ),
            "targetCommands": sorted(
                commands.values(), key=lambda row: str(row["targetId"])
            ),
            "approvalMode": intent.approval_mode,
            "validationRefs": _normalized_validation_refs(
                intent.auto_publish_validation_refs
            ),
            "activationRefs": sorted(
                intent.auto_publish_activation_refs,
                key=lambda row: str(row["targetId"]),
            ),
            "inputEvidenceManifestHash": intent.input_evidence_manifest_hash,
            "generationPipelineManifestHash": (
                intent.generation_pipeline_manifest_hash
            ),
            "qualityGateManifestHash": intent.quality_gate_manifest_hash,
            "qualityReportHash": intent.quality_report_hash,
            "editorialPolicyHash": revision.editorial_policy_hash,
            "verificationManifestHash": revision.verification_manifest_hash,
            "exclusionManifestHash": revision.exclusion_manifest_hash,
            "claimManifestHash": revision.claim_manifest_hash,
            "revalidationGeneration": revision.revalidation_generation,
            "correctionCaseId": _id(intent.correction_case_id),
        }
    )


def _legacy_v1_hash(approval, intent, command):
    return _hash(
        {
            "intentId": str(intent.id),
            "articleRevisionId": str(intent.article_revision_id),
            "targetId": str(approval.target_id),
            "targetAction": approval.target_action,
            "targetSnapshotId": str(command["targetSnapshotId"]),
            "targetConfigHash": command["targetConfigHash"],
            "subject": approval.action_subject,
            "qualityReportHash": intent.quality_report_hash,
        }
    )


def _legacy_v2_hash(approval, intent, command):
    return _hash(
        {
            "schemaVersion": "approval-subject-v2",
            "intentId": str(intent.id),
            "articleRevisionId": str(intent.article_revision_id),
            "targetId": str(approval.target_id),
            "targetAction": approval.target_action,
            "targetSnapshotId": str(command["targetSnapshotId"]),
            "targetConfigHash": command["targetConfigHash"],
            "subject": approval.action_subject,
            "qualityReportHash": intent.quality_report_hash,
            "decision": approval.decision,
            "headVersion": approval.head_version,
            "supersedesApprovalId": _id(approval.supersedes_approval_id),
        }
    )


def _render_material(render):
    if render is None:
        return None
    return {
        "renderId": str(render.id),
        "contentHash": render.content_hash,
        "templateHash": render.template_hash,
        "sourceManifestHash": render.source_manifest_hash,
        "mediaManifestHash": _hash(render.media_manifest),
    }


def _stable_subject_hash(approval, intent, revision, command, render):
    return _hash(
        {
            "schemaVersion": SUBJECT_VERSION,
            "intentId": str(intent.id),
            "intentHash": intent.intent_hash,
            "articleRevisionId": str(revision.id),
            "revisionNo": revision.revision_no,
            "revisionContentHash": revision.content_hash,
            "editorialPolicyHash": revision.editorial_policy_hash,
            "qualityGateManifestHash": intent.quality_gate_manifest_hash,
            "qualityReportHash": intent.quality_report_hash,
            "inputEvidenceManifestHash": intent.input_evidence_manifest_hash,
            "targetId": str(approval.target_id),
            "targetAction": approval.target_action,
            "targetSnapshotId": str(command["targetSnapshotId"]),
            "targetConfigHash": command["targetConfigHash"],
            "targetCommandHash": command["targetCommandHash"],
            "actionSubject": approval.action_subject,
            "renderMaterial": _render_material(render),
        }
    )


def _decision_hash(
    *, subject_hash, approval, request_hash, actor_type, actor_id, event_key, reason
):
    return _hash(
        {
            "schemaVersion": DECISION_VERSION,
            "subjectHash": subject_hash,
            "decision": approval.decision,
            "headVersion": approval.head_version,
            "supersedesApprovalId": _id(approval.supersedes_approval_id),
            "requestHash": request_hash,
            "actorType": actor_type,
            "actorId": _id(actor_id),
            "eventKey": event_key,
            "reason": reason,
        }
    )


def _require_hash(value, *, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeError(f"Approval admission found invalid {label}")


def _require_render_exact(approval, intent, revision, command, Render, alias):
    subject = approval.action_subject
    if not isinstance(subject, dict):
        raise RuntimeError("Approval admission found an invalid action subject")
    required_identity = (
        str(subject.get("targetId")) == str(approval.target_id)
        and str(subject.get("targetSnapshotId"))
        == str(command["targetSnapshotId"])
        and subject.get("targetConfigHash") == command["targetConfigHash"]
        and subject.get("action") == approval.target_action
    )
    if not required_identity:
        raise RuntimeError("Approval admission found a stale action subject")
    if approval.target_action == "unpublish":
        if (
            subject.get("kind") != "unpublish_command"
            or approval.article_channel_render_id is not None
            or approval.render_template_hash is not None
            or approval.source_manifest_hash
            != subject.get("correctionEvidenceManifestHash")
        ):
            raise RuntimeError("Approval admission found invalid unpublish material")
        return None
    if subject.get("kind") != "content_preview":
        raise RuntimeError("Approval admission found invalid content material")
    try:
        render = Render.objects.using(alias).get(id=approval.article_channel_render_id)
    except Render.DoesNotExist as exc:
        raise RuntimeError("Approval admission found a missing preview render") from exc
    if (
        render.publication_intent_id != intent.id
        or render.article_revision_id != revision.id
        or render.target_id != approval.target_id
        or render.target_snapshot_id != approval.target_snapshot_id
        or render.target_config_hash != approval.target_config_hash
        or render.render_stage != "preview"
        or str(subject.get("renderId")) != str(render.id)
        or subject.get("templateHash") != render.template_hash
        or subject.get("sourceManifestHash") != render.source_manifest_hash
        or approval.render_template_hash != render.template_hash
        or approval.source_manifest_hash != render.source_manifest_hash
        or render.content_hash
        != _hash({"title": render.title, "body": render.body_html})
        or render.template_hash
        != _hash(
            {
                "channel": render.target.channel,
                "title": render.title,
                "body": render.body_html,
                "revision": str(revision.id),
            }
        )
        or render.source_manifest_hash
        != _hash(
            {
                "inputEvidenceManifestHash": intent.input_evidence_manifest_hash,
                "sourceLinks": render.source_links,
            }
        )
    ):
        raise RuntimeError("Approval admission found a mutated preview render")
    raise RuntimeError(
        "Legacy content approval media material is not immutably proven; "
        "manual reapproval is required"
    )


def _require_audit_exact(approval, AuditEvent, alias, legacy_hash):
    if approval.admin_id is None:
        raise RuntimeError("Approval admission found a missing approving admin")
    events = list(
        AuditEvent.objects.using(alias).filter(
            action="publication_approval.decided",
            entity_type="publishing.approval",
            entity_id=approval.id,
        )
    )
    if len(events) != 1:
        raise RuntimeError("Approval admission requires exactly one audit event")
    event = events[0]
    metadata = event.metadata_redacted
    if not isinstance(metadata, dict):
        raise RuntimeError("Approval admission found invalid audit metadata")
    request_hash = metadata.get("request_hash")
    _require_hash(request_hash, label="audit request hash")
    if (
        metadata.get("approval_hash") != legacy_hash
        or metadata.get("decision") != approval.decision
        or str(metadata.get("decision_id")) != str(approval.id)
        or str(metadata.get("intent_id")) != str(approval.publication_intent_id)
        or str(metadata.get("target_id")) != str(approval.target_id)
        or (approval.request_hash is not None and approval.request_hash != request_hash)
        or event.actor_type not in {"admin", "worker"}
        or not isinstance(event.reason_code, str)
        or not event.reason_code.strip()
        or len(event.reason_code) > 500
    ):
        raise RuntimeError("Approval admission found mismatched audit provenance")
    if event.actor_type == "admin":
        if (
            event.actor_id is None
            or event.actor_id != approval.admin_id
            or metadata.get("event_key") is not None
        ):
            raise RuntimeError("Approval admission found mismatched admin provenance")
        event_key = None
    else:
        event_key = metadata.get("event_key")
        if (
            event.actor_id is not None
            or not isinstance(event_key, str)
            or not event_key.strip()
            or len(event_key) > 255
        ):
            raise RuntimeError("Approval admission found invalid worker provenance")
    return event, request_hash, event_key


def _mode_actor_decision_is_valid(*, mode, decision, actor_type):
    return (
        mode == "manual" and actor_type == "admin"
    ) or (
        mode == "validated_auto"
        and (
            (decision == "approved" and actor_type == "worker")
            or (
                decision in {"rejected", "revoked"}
                and actor_type == "admin"
            )
        )
    )


def admit_and_backfill_approval_history(apps, schema_editor):
    alias = schema_editor.connection.alias
    Approval = apps.get_model("publishing", "Approval")
    Head = apps.get_model("publishing", "PublicationApprovalHead")
    Intent = apps.get_model("publishing", "PublicationIntent")
    Render = apps.get_model("publishing", "ArticleChannelRender")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    approvals = list(Approval.objects.using(alias).order_by("decided_at", "id"))
    by_id = {row.id: row for row in approvals}
    for approval in approvals:
        try:
            intent = Intent.objects.using(alias).get(id=approval.publication_intent_id)
            revision = intent.article_revision
        except (Intent.DoesNotExist, AttributeError) as exc:
            raise RuntimeError("Approval admission found missing intent material") from exc
        commands = _mapping_by_target(intent.target_commands, label="target commands")
        refs = _mapping_by_target(intent.target_snapshot_refs, label="target snapshots")
        command = commands.get(str(approval.target_id))
        ref = refs.get(str(approval.target_id))
        if command is None or ref is None:
            raise RuntimeError("Approval admission found a target outside its intent")
        if (
            approval.article_revision_id != revision.id
            or approval.revision_no != revision.revision_no
            or intent.revision_no != revision.revision_no
            or intent.revision_content_hash != revision.content_hash
            or approval.target_action != command.get("resolvedAction")
            or str(approval.target_snapshot_id)
            != str(command.get("targetSnapshotId"))
            or approval.target_config_hash != command.get("targetConfigHash")
            or str(ref.get("targetSnapshotId"))
            != str(command.get("targetSnapshotId"))
            or ref.get("targetConfigHash") != command.get("targetConfigHash")
            or approval.quality_report_hash != intent.quality_report_hash
            or intent.quality_report_hash != revision.quality_report_hash
            or intent.quality_gate_manifest_hash
            != revision.quality_gate_manifest_hash
            or approval.policy_snapshot_hash != intent.quality_gate_manifest_hash
            or intent.intent_hash != _intent_hash(intent, revision, refs, commands)
        ):
            raise RuntimeError("Approval admission found inconsistent frozen material")
        if approval.supersedes_approval_id is None:
            if (
                approval.head_version != 1
                or approval.decision not in {"approved", "rejected"}
            ):
                raise RuntimeError("Approval admission found invalid root version")
        else:
            parent = by_id.get(approval.supersedes_approval_id)
            if (
                parent is None
                or parent.publication_intent_id != approval.publication_intent_id
                or parent.target_id != approval.target_id
                or approval.head_version != parent.head_version + 1
                or parent.decided_at > approval.decided_at
            ):
                raise RuntimeError("Approval admission found invalid predecessor lineage")
            if not (
                (parent.decision == "rejected" and approval.decision == "approved")
                or (
                    parent.decision == "approved"
                    and approval.decision == "revoked"
                )
            ):
                raise RuntimeError("Approval admission found invalid decision transition")
        render = _require_render_exact(
            approval, intent, revision, command, Render, alias
        )
        if approval.approval_material_version == "approval-subject-v1":
            legacy_hash = _legacy_v1_hash(approval, intent, command)
        elif approval.approval_material_version == "approval-subject-v2":
            legacy_hash = _legacy_v2_hash(approval, intent, command)
        else:
            raise RuntimeError("Approval admission found an unsupported material version")
        if approval.approval_subject_hash != legacy_hash:
            raise RuntimeError("Approval admission found an invalid legacy subject hash")
        event, request_hash, event_key = _require_audit_exact(
            approval, AuditEvent, alias, legacy_hash
        )
        if not _mode_actor_decision_is_valid(
            mode=approval.mode,
            decision=approval.decision,
            actor_type=event.actor_type,
        ):
            raise RuntimeError(
                "Approval admission found invalid mode/actor decision provenance"
            )
        subject_hash = _stable_subject_hash(
            approval, intent, revision, command, render
        )
        decision_hash = _decision_hash(
            subject_hash=subject_hash,
            approval=approval,
            request_hash=request_hash,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            event_key=event_key,
            reason=event.reason_code,
        )
        Approval.objects.using(alias).filter(id=approval.id).update(
            approval_material_version=SUBJECT_VERSION,
            approval_subject_hash=subject_hash,
            decision_hash=decision_hash,
            decision_reason=event.reason_code,
            decision_actor_type=event.actor_type,
            decision_actor_id=event.actor_id,
            decision_event_key=event_key,
            request_hash=request_hash,
            policy_snapshot_hash=revision.editorial_policy_hash,
        )

    heads = list(Head.objects.using(alias).all())
    grouped = {}
    for approval in approvals:
        grouped.setdefault(
            (approval.publication_intent_id, approval.target_id), []
        ).append(approval)
    if len(heads) != len(grouped):
        raise RuntimeError("Approval admission found an incomplete head projection")
    for head in heads:
        rows = grouped.get((head.publication_intent_id, head.target_id), [])
        superseded = {
            row.supersedes_approval_id
            for row in rows
            if row.supersedes_approval_id is not None
        }
        leaves = [row for row in rows if row.id not in superseded]
        if (
            len(leaves) != 1
            or head.latest_approval_id != leaves[0].id
            or head.version != leaves[0].head_version
        ):
            raise RuntimeError("Approval admission found an ambiguous head projection")
        subject_hashes = set(
            Approval.objects.using(alias)
            .filter(
                publication_intent_id=head.publication_intent_id,
                target_id=head.target_id,
            )
            .values_list("approval_subject_hash", flat=True)
        )
        if len(subject_hashes) != 1:
            raise RuntimeError(
                "Approval admission found decisions for different subjects in one chain"
            )
        latest = Approval.objects.using(alias).get(id=head.latest_approval_id)
        Head.objects.using(alias).filter(id=head.id).update(
            subject_hash=latest.approval_subject_hash
        )


def remove_legacy_guards(apps, schema_editor):
    migration = importlib.import_module(
        "apps.publishing.migrations.0008_approval_head_and_target_intent_fence"
    )
    migration.remove_approval_guards(apps, schema_editor)


def _drop_t019_guards(schema_editor):
    connection = schema_editor.connection
    vendor = connection.vendor
    approval_table = schema_editor.quote_name("publishing_approval")
    head_table = schema_editor.quote_name(
        "publishing_publicationapprovalhead"
    )
    render_table = schema_editor.quote_name("publishing_articlechannelrender")
    with connection.cursor() as cursor:
        if vendor == "postgresql":
            for trigger, table in (
                (POSTGRES_APPROVAL_INSERT_TRIGGER, approval_table),
                (POSTGRES_APPROVAL_ADVANCE_TRIGGER, approval_table),
                (POSTGRES_APPROVAL_APPEND_TRIGGER, approval_table),
                (POSTGRES_HEAD_TRIGGER, head_table),
                (POSTGRES_RENDER_TRIGGER, render_table),
            ):
                cursor.execute(
                    f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(trigger)} ON {table}"
                )
            for function in (
                POSTGRES_APPROVAL_INSERT_FUNCTION,
                POSTGRES_APPROVAL_ADVANCE_FUNCTION,
                POSTGRES_APPROVAL_APPEND_FUNCTION,
                POSTGRES_HEAD_FUNCTION,
                POSTGRES_RENDER_FUNCTION,
            ):
                cursor.execute(
                    f"DROP FUNCTION IF EXISTS {schema_editor.quote_name(function)}()"
                )
            return
        if vendor == "sqlite":
            for trigger in (
                SQLITE_APPROVAL_INSERT_TRIGGER,
                SQLITE_APPROVAL_ADVANCE_TRIGGER,
                SQLITE_APPROVAL_UPDATE_TRIGGER,
                SQLITE_APPROVAL_DELETE_TRIGGER,
                SQLITE_HEAD_INSERT_TRIGGER,
                SQLITE_HEAD_UPDATE_TRIGGER,
                SQLITE_HEAD_DELETE_TRIGGER,
                SQLITE_RENDER_UPDATE_TRIGGER,
                SQLITE_RENDER_DELETE_TRIGGER,
            ):
                cursor.execute(
                    f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(trigger)}"
                )
            return
    raise RuntimeError(f"T019 guards do not support database vendor {vendor!r}")


def install_t019_guards(apps, schema_editor):
    del apps
    _drop_t019_guards(schema_editor)
    connection = schema_editor.connection
    vendor = connection.vendor
    approval_table = schema_editor.quote_name("publishing_approval")
    head_table = schema_editor.quote_name(
        "publishing_publicationapprovalhead"
    )
    render_table = schema_editor.quote_name("publishing_articlechannelrender")
    with connection.cursor() as cursor:
        if vendor == "postgresql":
            cursor.execute(
                f"""
                CREATE FUNCTION {schema_editor.quote_name(POSTGRES_APPROVAL_APPEND_FUNCTION)}()
                RETURNS trigger LANGUAGE plpgsql AS $body$
                BEGIN
                    RAISE EXCEPTION 'Approval is append-only; % is forbidden', TG_OP
                        USING ERRCODE = '55000';
                END;
                $body$;
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(POSTGRES_APPROVAL_APPEND_TRIGGER)}
                BEFORE UPDATE OR DELETE OR TRUNCATE ON {approval_table}
                FOR EACH STATEMENT EXECUTE FUNCTION
                    {schema_editor.quote_name(POSTGRES_APPROVAL_APPEND_FUNCTION)}()
                """
            )
            cursor.execute(
                f"""
                CREATE FUNCTION {schema_editor.quote_name(POSTGRES_APPROVAL_INSERT_FUNCTION)}()
                RETURNS trigger LANGUAGE plpgsql AS $body$
                DECLARE
                    current_head RECORD;
                    current_decision text;
                BEGIN
                    SELECT * INTO current_head FROM {head_table}
                    WHERE publication_intent_id = NEW.publication_intent_id
                      AND target_id = NEW.target_id
                    FOR UPDATE;
                    IF current_head.id IS NOT NULL THEN
                        SELECT decision INTO current_decision
                        FROM {approval_table}
                        WHERE id = current_head.latest_approval_id;
                    END IF;
                    IF length(NEW.approval_subject_hash) <> 64
                       OR NEW.approval_subject_hash !~ '^[0-9a-f]{{64}}$'
                       OR length(NEW.decision_hash) <> 64
                       OR NEW.decision_hash !~ '^[0-9a-f]{{64}}$'
                       OR length(NEW.request_hash) <> 64
                       OR NEW.request_hash !~ '^[0-9a-f]{{64}}$'
                       OR NEW.approval_material_version <> 'approval-subject-v3'
                       OR NEW.decision NOT IN ('approved', 'rejected', 'revoked')
                       OR NEW.decision_actor_type NOT IN ('admin', 'worker')
                       OR (
                           NEW.decision_actor_type = 'admin'
                           AND (
                               NEW.decision_actor_id IS NULL
                               OR NEW.decision_actor_id IS DISTINCT FROM NEW.admin_id
                               OR NEW.decision_event_key IS NOT NULL
                           )
                       )
                       OR (
                           NEW.decision_actor_type = 'worker'
                           AND (
                               NEW.decision_actor_id IS NOT NULL
                               OR NEW.decision_event_key IS NULL
                               OR btrim(NEW.decision_event_key) = ''
                           )
                       )
                       OR NOT (
                           (
                               NEW.mode = 'manual'
                               AND NEW.decision_actor_type = 'admin'
                           )
                           OR (
                               NEW.mode = 'validated_auto'
                               AND NEW.decision = 'approved'
                               AND NEW.decision_actor_type = 'worker'
                           )
                           OR (
                               NEW.mode = 'validated_auto'
                               AND NEW.decision IN ('rejected', 'revoked')
                               AND NEW.decision_actor_type = 'admin'
                           )
                       )
                       OR btrim(NEW.decision_reason) = ''
                    THEN
                        RAISE EXCEPTION 'Approval immutable decision material is invalid'
                            USING ERRCODE = '23514';
                    END IF;
                    IF current_head.id IS NULL THEN
                        IF NEW.head_version <> 1
                           OR NEW.supersedes_approval_id IS NOT NULL
                           OR NEW.decision NOT IN ('approved', 'rejected')
                           OR EXISTS (
                               SELECT 1 FROM {approval_table}
                               WHERE publication_intent_id = NEW.publication_intent_id
                                 AND target_id = NEW.target_id
                           )
                        THEN
                            RAISE EXCEPTION 'Approval insert lineage is invalid'
                                USING ERRCODE = '23514';
                        END IF;
                    ELSIF NEW.head_version <> current_head.version + 1
                       OR NEW.supersedes_approval_id IS DISTINCT FROM current_head.latest_approval_id
                       OR NEW.approval_subject_hash <> current_head.subject_hash
                       OR NOT (
                           (current_decision = 'rejected' AND NEW.decision = 'approved')
                           OR (current_decision = 'approved' AND NEW.decision = 'revoked')
                       )
                    THEN
                        RAISE EXCEPTION 'Approval insert lineage is invalid'
                            USING ERRCODE = '23514';
                    END IF;
                    RETURN NEW;
                END;
                $body$;
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(POSTGRES_APPROVAL_INSERT_TRIGGER)}
                BEFORE INSERT ON {approval_table}
                FOR EACH ROW EXECUTE FUNCTION
                    {schema_editor.quote_name(POSTGRES_APPROVAL_INSERT_FUNCTION)}()
                """
            )
            cursor.execute(
                f"""
                CREATE FUNCTION {schema_editor.quote_name(POSTGRES_HEAD_FUNCTION)}()
                RETURNS trigger LANGUAGE plpgsql AS $body$
                BEGIN
                    IF TG_OP = 'DELETE' THEN
                        RAISE EXCEPTION 'PublicationApprovalHead cannot be deleted'
                            USING ERRCODE = '55000';
                    END IF;
                    IF TG_OP = 'UPDATE' AND (
                        NEW.id IS DISTINCT FROM OLD.id
                        OR NEW.publication_intent_id IS DISTINCT FROM OLD.publication_intent_id
                        OR NEW.target_id IS DISTINCT FROM OLD.target_id
                    ) THEN
                        RAISE EXCEPTION 'PublicationApprovalHead identity is immutable'
                            USING ERRCODE = '23514';
                    END IF;
                    IF TG_OP = 'INSERT' THEN
                        IF NEW.version <> 1 OR NOT EXISTS (
                            SELECT 1 FROM {approval_table} approval
                            WHERE approval.id = NEW.latest_approval_id
                              AND approval.publication_intent_id = NEW.publication_intent_id
                              AND approval.target_id = NEW.target_id
                              AND approval.head_version = NEW.version
                              AND approval.supersedes_approval_id IS NULL
                              AND NEW.subject_hash = approval.approval_subject_hash
                        ) THEN
                            RAISE EXCEPTION 'PublicationApprovalHead initial lineage is invalid'
                                USING ERRCODE = '23514';
                        END IF;
                    ELSIF TG_OP = 'UPDATE' THEN
                        IF NEW.version <> OLD.version + 1
                           OR NEW.subject_hash <> OLD.subject_hash
                           OR NOT EXISTS (
                            SELECT 1 FROM {approval_table} approval
                            WHERE approval.id = NEW.latest_approval_id
                              AND approval.publication_intent_id = NEW.publication_intent_id
                              AND approval.target_id = NEW.target_id
                              AND approval.head_version = NEW.version
                              AND approval.supersedes_approval_id = OLD.latest_approval_id
                              AND NEW.subject_hash = approval.approval_subject_hash
                        ) THEN
                            RAISE EXCEPTION 'PublicationApprovalHead lineage is invalid'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;
                    RETURN NEW;
                END;
                $body$;
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(POSTGRES_HEAD_TRIGGER)}
                BEFORE INSERT OR UPDATE OR DELETE ON {head_table}
                FOR EACH ROW EXECUTE FUNCTION
                    {schema_editor.quote_name(POSTGRES_HEAD_FUNCTION)}()
                """
            )
            cursor.execute(
                f"""
                CREATE FUNCTION {schema_editor.quote_name(POSTGRES_APPROVAL_ADVANCE_FUNCTION)}()
                RETURNS trigger LANGUAGE plpgsql AS $body$
                DECLARE
                    advanced_head_id uuid;
                BEGIN
                    IF NEW.head_version = 1 THEN
                        INSERT INTO {head_table} (
                            id,
                            publication_intent_id,
                            target_id,
                            latest_approval_id,
                            version,
                            subject_hash,
                            updated_at
                        ) VALUES (
                            NEW.id,
                            NEW.publication_intent_id,
                            NEW.target_id,
                            NEW.id,
                            NEW.head_version,
                            NEW.approval_subject_hash,
                            CURRENT_TIMESTAMP
                        )
                        RETURNING id INTO advanced_head_id;
                    ELSE
                        UPDATE {head_table}
                        SET latest_approval_id = NEW.id,
                            version = NEW.head_version,
                            subject_hash = NEW.approval_subject_hash,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE publication_intent_id = NEW.publication_intent_id
                          AND target_id = NEW.target_id
                          AND latest_approval_id = NEW.supersedes_approval_id
                          AND version = NEW.head_version - 1
                          AND subject_hash = NEW.approval_subject_hash
                        RETURNING id INTO advanced_head_id;
                    END IF;
                    IF advanced_head_id IS NULL THEN
                        RAISE EXCEPTION 'Approval head advance did not affect exactly one row'
                            USING ERRCODE = '23514';
                    END IF;
                    RETURN NEW;
                END;
                $body$;
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(POSTGRES_APPROVAL_ADVANCE_TRIGGER)}
                AFTER INSERT ON {approval_table}
                FOR EACH ROW EXECUTE FUNCTION
                    {schema_editor.quote_name(POSTGRES_APPROVAL_ADVANCE_FUNCTION)}()
                """
            )
            cursor.execute(
                f"""
                CREATE FUNCTION {schema_editor.quote_name(POSTGRES_RENDER_FUNCTION)}()
                RETURNS trigger LANGUAGE plpgsql AS $body$
                BEGIN
                    IF OLD.render_stage = 'final' OR EXISTS (
                        SELECT 1 FROM {approval_table}
                        WHERE article_channel_render_id = OLD.id
                    ) THEN
                        RAISE EXCEPTION 'ArticleChannelRender approved material is append-only'
                            USING ERRCODE = '55000';
                    END IF;
                    IF TG_OP = 'UPDATE' THEN
                        RETURN NEW;
                    END IF;
                    RETURN OLD;
                END;
                $body$;
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(POSTGRES_RENDER_TRIGGER)}
                BEFORE UPDATE OR DELETE ON {render_table}
                FOR EACH ROW EXECUTE FUNCTION
                    {schema_editor.quote_name(POSTGRES_RENDER_FUNCTION)}()
                """
            )
            return
        if vendor == "sqlite":
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(SQLITE_APPROVAL_UPDATE_TRIGGER)}
                BEFORE UPDATE ON {approval_table}
                BEGIN
                    SELECT RAISE(ABORT, 'Approval is append-only; UPDATE is forbidden');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(SQLITE_APPROVAL_DELETE_TRIGGER)}
                BEFORE DELETE ON {approval_table}
                BEGIN
                    SELECT RAISE(ABORT, 'Approval is append-only; DELETE is forbidden');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(SQLITE_APPROVAL_INSERT_TRIGGER)}
                BEFORE INSERT ON {approval_table}
                WHEN length(NEW.approval_subject_hash) <> 64
                  OR NEW.approval_subject_hash GLOB '*[^0-9a-f]*'
                  OR length(NEW.decision_hash) <> 64
                  OR NEW.decision_hash GLOB '*[^0-9a-f]*'
                  OR length(NEW.request_hash) <> 64
                  OR NEW.request_hash GLOB '*[^0-9a-f]*'
                  OR NEW.approval_material_version <> 'approval-subject-v3'
                  OR NEW.decision NOT IN ('approved', 'rejected', 'revoked')
                  OR NEW.decision_actor_type NOT IN ('admin', 'worker')
                  OR (
                    NEW.decision_actor_type = 'admin'
                    AND (
                      NEW.decision_actor_id IS NULL
                      OR NEW.admin_id IS NULL
                      OR NEW.decision_actor_id <> NEW.admin_id
                      OR NEW.decision_event_key IS NOT NULL
                    )
                  )
                  OR (
                    NEW.decision_actor_type = 'worker'
                    AND (
                      NEW.decision_actor_id IS NOT NULL
                      OR NEW.decision_event_key IS NULL
                      OR trim(NEW.decision_event_key) = ''
                    )
                  )
                  OR NOT (
                    (
                      NEW.mode = 'manual'
                      AND NEW.decision_actor_type = 'admin'
                    )
                    OR (
                      NEW.mode = 'validated_auto'
                      AND NEW.decision = 'approved'
                      AND NEW.decision_actor_type = 'worker'
                    )
                    OR (
                      NEW.mode = 'validated_auto'
                      AND NEW.decision IN ('rejected', 'revoked')
                      AND NEW.decision_actor_type = 'admin'
                    )
                  )
                  OR trim(NEW.decision_reason) = ''
                  OR (
                    NOT EXISTS (
                        SELECT 1 FROM {head_table}
                        WHERE publication_intent_id = NEW.publication_intent_id
                          AND target_id = NEW.target_id
                    )
                    AND (
                        NEW.head_version <> 1
                        OR NEW.supersedes_approval_id IS NOT NULL
                        OR NEW.decision NOT IN ('approved', 'rejected')
                        OR EXISTS (
                            SELECT 1 FROM {approval_table}
                            WHERE publication_intent_id = NEW.publication_intent_id
                              AND target_id = NEW.target_id
                        )
                    )
                  )
                  OR (
                    EXISTS (
                        SELECT 1 FROM {head_table}
                        WHERE publication_intent_id = NEW.publication_intent_id
                          AND target_id = NEW.target_id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM {head_table} head
                        JOIN {approval_table} current_approval
                          ON current_approval.id = head.latest_approval_id
                        WHERE head.publication_intent_id = NEW.publication_intent_id
                          AND head.target_id = NEW.target_id
                          AND NEW.head_version = head.version + 1
                          AND NEW.supersedes_approval_id = head.latest_approval_id
                          AND NEW.approval_subject_hash = head.subject_hash
                          AND (
                            (
                              current_approval.decision = 'rejected'
                              AND NEW.decision = 'approved'
                            )
                            OR (
                              current_approval.decision = 'approved'
                              AND NEW.decision = 'revoked'
                            )
                          )
                    )
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'Approval insert lineage is invalid');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(SQLITE_HEAD_INSERT_TRIGGER)}
                BEFORE INSERT ON {head_table}
                WHEN NEW.version <> 1 OR NOT EXISTS (
                    SELECT 1 FROM {approval_table} approval
                    WHERE approval.id = NEW.latest_approval_id
                      AND approval.publication_intent_id = NEW.publication_intent_id
                      AND approval.target_id = NEW.target_id
                      AND approval.head_version = NEW.version
                      AND approval.supersedes_approval_id IS NULL
                      AND NEW.subject_hash = approval.approval_subject_hash
                )
                BEGIN
                    SELECT RAISE(ABORT, 'PublicationApprovalHead initial lineage is invalid');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(SQLITE_HEAD_UPDATE_TRIGGER)}
                BEFORE UPDATE ON {head_table}
                WHEN NEW.id <> OLD.id
                  OR NEW.publication_intent_id <> OLD.publication_intent_id
                  OR NEW.target_id <> OLD.target_id
                  OR NEW.version <> OLD.version + 1
                  OR NEW.subject_hash <> OLD.subject_hash
                  OR NOT EXISTS (
                    SELECT 1 FROM {approval_table} approval
                    WHERE approval.id = NEW.latest_approval_id
                      AND approval.publication_intent_id = NEW.publication_intent_id
                      AND approval.target_id = NEW.target_id
                      AND approval.head_version = NEW.version
                      AND approval.supersedes_approval_id = OLD.latest_approval_id
                      AND NEW.subject_hash = approval.approval_subject_hash
                )
                BEGIN
                    SELECT RAISE(ABORT, 'PublicationApprovalHead lineage is invalid');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(SQLITE_HEAD_DELETE_TRIGGER)}
                BEFORE DELETE ON {head_table}
                BEGIN
                    SELECT RAISE(ABORT, 'PublicationApprovalHead cannot be deleted');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(SQLITE_APPROVAL_ADVANCE_TRIGGER)}
                AFTER INSERT ON {approval_table}
                BEGIN
                    INSERT INTO {head_table} (
                        id,
                        publication_intent_id,
                        target_id,
                        latest_approval_id,
                        version,
                        subject_hash,
                        updated_at
                    )
                    SELECT
                        NEW.id,
                        NEW.publication_intent_id,
                        NEW.target_id,
                        NEW.id,
                        NEW.head_version,
                        NEW.approval_subject_hash,
                        CURRENT_TIMESTAMP
                    WHERE NEW.head_version = 1;

                    UPDATE {head_table}
                    SET latest_approval_id = NEW.id,
                        version = NEW.head_version,
                        subject_hash = NEW.approval_subject_hash,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE NEW.head_version > 1
                      AND publication_intent_id = NEW.publication_intent_id
                      AND target_id = NEW.target_id
                      AND latest_approval_id = NEW.supersedes_approval_id
                      AND version = NEW.head_version - 1
                      AND subject_hash = NEW.approval_subject_hash;

                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM {head_table}
                        WHERE publication_intent_id = NEW.publication_intent_id
                          AND target_id = NEW.target_id
                          AND latest_approval_id = NEW.id
                          AND version = NEW.head_version
                          AND subject_hash = NEW.approval_subject_hash
                    ) THEN RAISE(
                        ABORT,
                        'Approval head advance did not affect exactly one row'
                    ) END;
                END
                """
            )
            for operation, trigger in (
                ("UPDATE", SQLITE_RENDER_UPDATE_TRIGGER),
                ("DELETE", SQLITE_RENDER_DELETE_TRIGGER),
            ):
                cursor.execute(
                    f"""
                    CREATE TRIGGER {schema_editor.quote_name(trigger)}
                    BEFORE {operation} ON {render_table}
                    WHEN OLD.render_stage = 'final' OR EXISTS (
                        SELECT 1 FROM {approval_table}
                        WHERE article_channel_render_id = OLD.id
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'ArticleChannelRender approved material is append-only');
                    END
                    """
                )
            return
    raise RuntimeError(f"T019 guards do not support database vendor {vendor!r}")


def remove_t019_guards(apps, schema_editor):
    del apps
    _drop_t019_guards(schema_editor)


def restore_legacy_guards(apps, schema_editor):
    _drop_t019_guards(schema_editor)
    migration = importlib.import_module(
        "apps.publishing.migrations.0008_approval_head_and_target_intent_fence"
    )
    migration.install_approval_guards(apps, schema_editor)


def reject_populated_reverse(apps, schema_editor):
    Approval = apps.get_model("publishing", "Approval")
    if Approval.objects.using(schema_editor.connection.alias).exists():
        raise IrreversibleError(
            "T019 approval integrity cannot be reversed after approval rows exist"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0008_approval_head_and_target_intent_fence"),
        ("audit", "0002_auditevent_append_only"),
        ("editorial", "0003_editorial_policy_runtime"),
    ]

    operations = [
        migrations.RunPython(
            remove_legacy_guards,
            reverse_code=restore_legacy_guards,
        ),
        migrations.AddField(
            model_name="approval",
            name="decision_actor_type",
            field=models.CharField(
                choices=(("admin", "Admin"), ("worker", "Worker")),
                max_length=16,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="approval",
            name="decision_actor_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="approval",
            name="decision_event_key",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="approval",
            name="decision_hash",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="approval",
            name="decision_reason",
            field=models.CharField(max_length=500, null=True),
        ),
        migrations.AddField(
            model_name="publicationapprovalhead",
            name="subject_hash",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.RunPython(
            admit_and_backfill_approval_history,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="approval",
            name="decision_actor_type",
            field=models.CharField(
                choices=(("admin", "Admin"), ("worker", "Worker")),
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="approval",
            name="decision_hash",
            field=models.CharField(max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name="approval",
            name="decision_reason",
            field=models.CharField(max_length=500),
        ),
        migrations.AlterField(
            model_name="approval",
            name="request_hash",
            field=models.CharField(max_length=64),
        ),
        migrations.AlterField(
            model_name="approval",
            name="admin",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RenameField(
            model_name="approval",
            old_name="supersedes_approval_id",
            new_name="supersedes_approval",
        ),
        migrations.AlterField(
            model_name="approval",
            name="supersedes_approval",
            field=models.ForeignKey(
                blank=True,
                db_column="supersedes_approval_id",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="superseded_by",
                to="publishing.approval",
            ),
        ),
        migrations.AlterField(
            model_name="publicationapprovalhead",
            name="subject_hash",
            field=models.CharField(max_length=64),
        ),
        migrations.AddConstraint(
            model_name="approval",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    approval_material_version="approval-subject-v3",
                    decision__in=("approved", "rejected", "revoked"),
                ),
                name="ck_approval_v3_decision_material",
            ),
        ),
        migrations.AddConstraint(
            model_name="approval",
            constraint=models.CheckConstraint(
                condition=~models.Q(
                    ("id", models.F("supersedes_approval_id"))
                ),
                name="ck_approval_not_self_superseding",
            ),
        ),
        migrations.AddConstraint(
            model_name="approval",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        decision_actor_type="admin",
                        decision_actor_id=models.F("admin_id"),
                        decision_event_key__isnull=True,
                    )
                    | models.Q(
                        decision_actor_type="worker",
                        decision_actor_id__isnull=True,
                        decision_event_key__isnull=False,
                    )
                ),
                name="ck_approval_decision_actor_provenance",
            ),
        ),
        migrations.AddConstraint(
            model_name="approval",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(mode="manual", decision_actor_type="admin")
                    | models.Q(
                        mode="validated_auto",
                        decision="approved",
                        decision_actor_type="worker",
                    )
                    | models.Q(
                        mode="validated_auto",
                        decision__in=("rejected", "revoked"),
                        decision_actor_type="admin",
                    )
                ),
                name="ck_approval_mode_actor_decision",
            ),
        ),
        migrations.AddConstraint(
            model_name="approval",
            constraint=models.UniqueConstraint(
                fields=("publication_intent", "target", "head_version"),
                name="uq_approval_intent_target_head_version",
            ),
        ),
        migrations.RunPython(
            install_t019_guards,
            reverse_code=remove_t019_guards,
        ),
        migrations.RunPython(
            migrations.RunPython.noop,
            reverse_code=reject_populated_reverse,
        ),
    ]
