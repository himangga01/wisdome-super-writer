from __future__ import annotations

from django.db import migrations, models


SQLITE_UPDATE_TRIGGER = "collection_sourceitem_retention_update"
SQLITE_DELETE_TRIGGER = "collection_sourceitem_no_delete"
POSTGRES_FUNCTION = "collection_guard_sourceitem_retention"
POSTGRES_TRIGGER = "collection_sourceitem_retention_guard"


def install_guards(apps, schema_editor):
    connection = schema_editor.connection
    table = schema_editor.quote_name("collection_sourceitem")
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(SQLITE_UPDATE_TRIGGER)}
                BEFORE UPDATE ON {table}
                FOR EACH ROW
                WHEN NOT (
                    OLD.retention_tombstoned_at IS NULL
                    AND NEW.retention_tombstoned_at IS NOT NULL
                    AND NEW.body_text = ''
                    AND NEW.metadata = '{{}}'
                    AND NEW.attachments = '[]'
                    AND NEW.id IS OLD.id
                    AND NEW.source_id IS OLD.source_id
                    AND NEW.external_id IS OLD.external_id
                    AND NEW.canonical_url IS OLD.canonical_url
                    AND NEW.title IS OLD.title
                    AND NEW.publisher IS OLD.publisher
                    AND NEW.published_at IS OLD.published_at
                    AND NEW.modified_at IS OLD.modified_at
                    AND NEW.first_collected_at IS OLD.first_collected_at
                    AND NEW.content_hash IS OLD.content_hash
                    AND NEW.source_version_hash IS OLD.source_version_hash
                    AND NEW.source_version_schema IS OLD.source_version_schema
                    AND NEW.status IS OLD.status
                    AND NEW.supersedes_id IS OLD.supersedes_id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'SourceItem is append-only except one retention tombstone');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {schema_editor.quote_name(SQLITE_DELETE_TRIGGER)}
                BEFORE DELETE ON {table}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, 'SourceItem is append-only');
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
                AS $source_retention_guard$
                BEGIN
                    IF TG_OP = 'DELETE' THEN
                        RAISE EXCEPTION 'SourceItem is append-only' USING ERRCODE = '55000';
                    END IF;
                    IF NOT (
                        OLD.retention_tombstoned_at IS NULL
                        AND NEW.retention_tombstoned_at IS NOT NULL
                        AND NEW.body_text = ''
                        AND NEW.metadata = '{{}}'::jsonb
                        AND NEW.attachments = '[]'::jsonb
                        AND NEW.id IS NOT DISTINCT FROM OLD.id
                        AND NEW.source_id IS NOT DISTINCT FROM OLD.source_id
                        AND NEW.external_id IS NOT DISTINCT FROM OLD.external_id
                        AND NEW.canonical_url IS NOT DISTINCT FROM OLD.canonical_url
                        AND NEW.title IS NOT DISTINCT FROM OLD.title
                        AND NEW.publisher IS NOT DISTINCT FROM OLD.publisher
                        AND NEW.published_at IS NOT DISTINCT FROM OLD.published_at
                        AND NEW.modified_at IS NOT DISTINCT FROM OLD.modified_at
                        AND NEW.first_collected_at IS NOT DISTINCT FROM OLD.first_collected_at
                        AND NEW.content_hash IS NOT DISTINCT FROM OLD.content_hash
                        AND NEW.source_version_hash IS NOT DISTINCT FROM OLD.source_version_hash
                        AND NEW.source_version_schema IS NOT DISTINCT FROM OLD.source_version_schema
                        AND NEW.status IS NOT DISTINCT FROM OLD.status
                        AND NEW.supersedes_id IS NOT DISTINCT FROM OLD.supersedes_id
                    ) THEN
                        RAISE EXCEPTION 'SourceItem is append-only except one retention tombstone'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN NEW;
                END;
                $source_retention_guard$
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


def remove_guards(apps, schema_editor):
    connection = schema_editor.connection
    table = schema_editor.quote_name("collection_sourceitem")
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute(f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(SQLITE_UPDATE_TRIGGER)}")
            cursor.execute(f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(SQLITE_DELETE_TRIGGER)}")
        elif connection.vendor == "postgresql":
            cursor.execute(f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(POSTGRES_TRIGGER)} ON {table}")
            cursor.execute(f"DROP FUNCTION IF EXISTS {schema_editor.quote_name(POSTGRES_FUNCTION)}()")


def reject_populated_reverse(apps, schema_editor):
    SourceItem = apps.get_model("collection", "SourceItem")
    if SourceItem.objects.using(schema_editor.connection.alias).filter(
        retention_tombstoned_at__isnull=False
    ).exists():
        raise migrations.IrreversibleError(
            "Source retention tombstones are one-way and cannot be restored."
        )


class Migration(migrations.Migration):
    dependencies = [("collection", "0010_run_control_decision")]

    operations = [
        migrations.AddField(
            model_name="sourceitem",
            name="retention_tombstoned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(install_guards, remove_guards),
        migrations.RunPython(migrations.RunPython.noop, reject_populated_reverse),
    ]
