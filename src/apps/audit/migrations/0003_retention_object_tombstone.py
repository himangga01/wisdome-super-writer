from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


SQLITE_UPDATE_TRIGGER = "audit_retention_item_identity_no_update"
SQLITE_DELETE_TRIGGER = "audit_retention_item_no_delete"
POSTGRES_FUNCTION = "audit_guard_retention_item"
POSTGRES_TRIGGER = "audit_retention_item_guard"


def quarantine_legacy_rows(apps, schema_editor):
    alias = schema_editor.connection.alias
    Item = apps.get_model("audit", "RetentionBatchItem")
    Batch = apps.get_model("audit", "RetentionBatch")
    Item.objects.using(alias).all().update(
        state="held",
        reason_code="legacy_unverifiable_candidate",
        hold_reason="Legacy retention material cannot prove an exact object precondition.",
    )
    Batch.objects.using(alias).all().update(
        state="failed",
        error_code="legacy_unverifiable_candidate",
        error_detail_redacted="Create a new retention preview.",
    )


def install_retention_item_guards(apps, schema_editor):
    connection = schema_editor.connection
    table = schema_editor.quote_name("audit_retentionbatchitem")
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(SQLITE_UPDATE_TRIGGER)}
                BEFORE UPDATE ON {table}
                FOR EACH ROW
                WHEN NOT (
                    NEW.batch_id IS OLD.batch_id
                    AND NEW.entity_type IS OLD.entity_type
                    AND NEW.entity_id IS OLD.entity_id
                    AND NEW.policy_code IS OLD.policy_code
                    AND NEW.object_key IS OLD.object_key
                    AND NEW.object_version IS OLD.object_version
                    AND NEW.object_checksum IS OLD.object_checksum
                    AND NEW.byte_size IS OLD.byte_size
                    AND NEW.candidate_hash IS OLD.candidate_hash
                    AND NEW.precondition_hash IS OLD.precondition_hash
                    AND NEW.dependency_manifest IS OLD.dependency_manifest
                )
                BEGIN
                    SELECT RAISE(ABORT, 'Retention candidate identity is immutable');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(SQLITE_DELETE_TRIGGER)}
                BEFORE DELETE ON {table}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, 'Retention candidate ledger is append-only');
                END
                """
            )
        elif connection.vendor == "postgresql":
            function = schema_editor.quote_name(POSTGRES_FUNCTION)
            trigger = schema_editor.quote_name(POSTGRES_TRIGGER)
            cursor.execute(
                f"""
                CREATE OR REPLACE FUNCTION {function}()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $retention_item_guard$
                BEGIN
                    IF TG_OP = 'DELETE' THEN
                        RAISE EXCEPTION 'Retention candidate ledger is append-only'
                            USING ERRCODE = '55000';
                    END IF;
                    IF NEW.batch_id IS DISTINCT FROM OLD.batch_id
                       OR NEW.entity_type IS DISTINCT FROM OLD.entity_type
                       OR NEW.entity_id IS DISTINCT FROM OLD.entity_id
                       OR NEW.policy_code IS DISTINCT FROM OLD.policy_code
                       OR NEW.object_key IS DISTINCT FROM OLD.object_key
                       OR NEW.object_version IS DISTINCT FROM OLD.object_version
                       OR NEW.object_checksum IS DISTINCT FROM OLD.object_checksum
                       OR NEW.byte_size IS DISTINCT FROM OLD.byte_size
                       OR NEW.candidate_hash IS DISTINCT FROM OLD.candidate_hash
                       OR NEW.precondition_hash IS DISTINCT FROM OLD.precondition_hash
                       OR NEW.dependency_manifest IS DISTINCT FROM OLD.dependency_manifest THEN
                        RAISE EXCEPTION 'Retention candidate identity is immutable'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN NEW;
                END;
                $retention_item_guard$
                """
            )
            cursor.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
            cursor.execute(
                f"""
                CREATE TRIGGER {trigger}
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION {function}()
                """
            )


def remove_retention_item_guards(apps, schema_editor):
    connection = schema_editor.connection
    table = schema_editor.quote_name("audit_retentionbatchitem")
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute(
                f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(SQLITE_UPDATE_TRIGGER)}"
            )
            cursor.execute(
                f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(SQLITE_DELETE_TRIGGER)}"
            )
        elif connection.vendor == "postgresql":
            cursor.execute(
                f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(POSTGRES_TRIGGER)} ON {table}"
            )
            cursor.execute(
                f"DROP FUNCTION IF EXISTS {schema_editor.quote_name(POSTGRES_FUNCTION)}()"
            )


def reject_populated_reverse(apps, schema_editor):
    Batch = apps.get_model("audit", "RetentionBatch")
    if Batch.objects.using(schema_editor.connection.alias).exists():
        raise migrations.IrreversibleError(
            "Retention object tombstones are durable; reverse requires an empty retention ledger."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0002_auditevent_append_only"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="retentionbatch",
            name="authorization_reason",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AddField(
            model_name="retentionbatch",
            name="authorization_request_key",
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
        migrations.AddField(
            model_name="retentionbatch",
            name="authorized_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="authorized_retention_batches",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="retentionbatch",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, default=timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(model_name="retentionbatch", name="error_code", field=models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField(model_name="retentionbatch", name="expected_byte_count", field=models.PositiveBigIntegerField(default=0)),
        migrations.AddField(model_name="retentionbatch", name="expected_item_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="retentionbatch", name="failed_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="retentionbatch", name="failure_cursor", field=models.CharField(blank=True, max_length=200, null=True)),
        migrations.AddField(model_name="retentionbatch", name="preview_reason", field=models.CharField(default="legacy retention preview", max_length=500)),
        migrations.AddField(model_name="retentionbatch", name="processed_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="retentionbatch", name="reauth_proof_id", field=models.UUIDField(blank=True, null=True)),
        migrations.AddField(model_name="retentionbatch", name="remediation", field=models.CharField(blank=True, max_length=500, null=True)),
        migrations.AddField(model_name="retentionbatch", name="request_hash", field=models.CharField(default="legacy-unverifiable-v1", max_length=64)),
        migrations.AddField(model_name="retentionbatch", name="scope", field=models.CharField(default="raw_evidence", max_length=40)),
        migrations.AddField(model_name="retentionbatch", name="skipped_hold_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="retentionbatchitem", name="byte_size", field=models.PositiveBigIntegerField(default=0)),
        migrations.AddField(model_name="retentionbatchitem", name="candidate_hash", field=models.CharField(default="legacy-unverifiable-v1", max_length=64)),
        migrations.AddField(model_name="retentionbatchitem", name="cleanup_operation_id", field=models.UUIDField(blank=True, null=True)),
        migrations.AddField(model_name="retentionbatchitem", name="dependency_manifest", field=models.JSONField(default=list)),
        migrations.AddField(model_name="retentionbatchitem", name="error_code", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="retentionbatchitem", name="error_detail_redacted", field=models.CharField(blank=True, max_length=500)),
        migrations.AddField(model_name="retentionbatchitem", name="hold_reason", field=models.CharField(blank=True, max_length=500)),
        migrations.AddField(model_name="retentionbatchitem", name="lease_generation", field=models.PositiveIntegerField(default=1)),
        migrations.AddField(model_name="retentionbatchitem", name="object_checksum", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="retentionbatchitem", name="object_version", field=models.CharField(blank=True, max_length=500)),
        migrations.AddField(model_name="retentionbatchitem", name="policy_code", field=models.CharField(default="legacy-unverifiable-v1", max_length=64)),
        migrations.AddField(model_name="retentionbatchitem", name="precondition_hash", field=models.CharField(default="legacy-unverifiable-v1", max_length=64)),
        migrations.AddField(model_name="retentionbatchitem", name="remediation", field=models.CharField(blank=True, max_length=500)),
        migrations.AddField(model_name="retentionbatchitem", name="result_hash", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="retentionbatchitem", name="tombstone_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AlterField(
            model_name="retentionbatchitem",
            name="state",
            field=models.CharField(
                choices=[
                    ("candidate", "Candidate"),
                    ("held", "Held"),
                    ("deletion_pending", "Deletion pending"),
                    ("purged", "Purged"),
                    ("skipped", "Skipped"),
                    ("failed", "Failed"),
                ],
                default="candidate",
                max_length=20,
            ),
        ),
        migrations.RunPython(quarantine_legacy_rows, migrations.RunPython.noop),
        migrations.RunPython(install_retention_item_guards, remove_retention_item_guards),
        migrations.RunPython(migrations.RunPython.noop, reject_populated_reverse),
    ]
