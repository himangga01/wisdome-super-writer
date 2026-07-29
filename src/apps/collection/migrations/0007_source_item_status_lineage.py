import django.db.models.deletion
from django.db import migrations, models


def create_lineage_guards(apps, schema_editor):
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION collection_guard_source_item()
        RETURNS trigger AS $$
        DECLARE
            prior_source_id uuid;
            prior_external_id varchar;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                RAISE EXCEPTION 'SourceItem is append-only'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.supersedes_id IS NOT NULL THEN
                SELECT source_id, external_id
                  INTO prior_source_id, prior_external_id
                  FROM collection_sourceitem
                 WHERE id = NEW.supersedes_id;
                IF prior_source_id IS NULL
                   OR prior_source_id <> NEW.source_id
                   OR prior_external_id <> NEW.external_id THEN
                    RAISE EXCEPTION
                        'SourceItem supersedes crosses source lineage'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER collection_source_item_append_only
        BEFORE INSERT OR UPDATE OR DELETE
        ON collection_sourceitem
        FOR EACH ROW EXECUTE FUNCTION collection_guard_source_item();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION collection_guard_run_source_item()
        RETURNS trigger AS $$
        DECLARE
            attempt_run_id uuid;
            attempt_snapshot_id uuid;
            item_source_id uuid;
            item_external_id varchar;
            snapshot_source_id uuid;
            run_registry_id uuid;
            previous_run_id uuid;
            previous_source_id uuid;
            previous_external_id varchar;
            previous_discovered_at timestamp with time zone;
            previous_attempt_state varchar;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                RAISE EXCEPTION 'RunSourceItem is append-only'
                    USING ERRCODE = '23514';
            END IF;
            SELECT run_id, source_snapshot_id
              INTO attempt_run_id, attempt_snapshot_id
              FROM collection_sourcecollectionattempt
             WHERE id = NEW.collection_attempt_id;
            SELECT source_id, external_id
              INTO item_source_id, item_external_id
              FROM collection_sourceitem
             WHERE id = NEW.source_item_id;
            SELECT source_id
              INTO snapshot_source_id
              FROM topics_sourcedefinitionsnapshot
             WHERE id = NEW.source_snapshot_id;
            SELECT source_registry_id
              INTO run_registry_id
              FROM collection_collectionrun
             WHERE id = NEW.run_id;
            IF attempt_run_id IS NULL
               OR attempt_run_id <> NEW.run_id
               OR attempt_snapshot_id <> NEW.source_snapshot_id
               OR snapshot_source_id <> item_source_id
               OR NOT EXISTS (
                    SELECT 1
                      FROM topics_sourceregistrymembership membership
                     WHERE membership.registry_id = run_registry_id
                       AND membership.source_definition_id = item_source_id
                       AND membership.source_snapshot_id = NEW.source_snapshot_id
                       AND membership.enabled
               ) THEN
                RAISE EXCEPTION
                    'RunSourceItem provenance is inconsistent'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.previous_run_source_item_id IS NOT NULL THEN
                SELECT prior.run_id,
                       prior_item.source_id,
                       prior_item.external_id,
                       prior.discovered_at,
                       prior_attempt.state
                  INTO previous_run_id,
                       previous_source_id,
                       previous_external_id,
                       previous_discovered_at,
                       previous_attempt_state
                  FROM collection_runsourceitem prior
                  JOIN collection_sourceitem prior_item
                    ON prior_item.id = prior.source_item_id
                  JOIN collection_sourcecollectionattempt prior_attempt
                    ON prior_attempt.id = prior.collection_attempt_id
                 WHERE prior.id = NEW.previous_run_source_item_id;
                IF previous_run_id IS NULL
                   OR previous_run_id = NEW.run_id
                   OR previous_attempt_state <> 'succeeded'
                   OR previous_source_id <> item_source_id
                   OR previous_external_id <> item_external_id
                   OR NEW.discovered_at IS NULL
                   OR previous_discovered_at > NEW.discovered_at THEN
                    RAISE EXCEPTION
                        'RunSourceItem previous observation crosses lineage'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER collection_run_source_item_lineage
        BEFORE INSERT OR UPDATE OR DELETE
        ON collection_runsourceitem
        FOR EACH ROW EXECUTE FUNCTION collection_guard_run_source_item();
        """
    )


def drop_lineage_guards(apps, schema_editor):
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DROP TRIGGER IF EXISTS collection_run_source_item_lineage
            ON collection_runsourceitem;
        DROP FUNCTION IF EXISTS collection_guard_run_source_item();
        DROP TRIGGER IF EXISTS collection_source_item_append_only
            ON collection_sourceitem;
        DROP FUNCTION IF EXISTS collection_guard_source_item();
        """
    )


class Migration(migrations.Migration):

    dependencies = [
        ("collection", "0006_collection_observability"),
    ]

    operations = [
        migrations.RenameField(
            model_name="sourceitem",
            old_name="discovery_status",
            new_name="status",
        ),
        migrations.AddField(
            model_name="sourceitem",
            name="modified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sourceitem",
            name="source_version_schema",
            field=models.CharField(
                default="legacy-source-item-version-v0",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="runsourceitem",
            name="previous_run_source_item",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="subsequent_observations",
                to="collection.runsourceitem",
            ),
        ),
        migrations.AddField(
            model_name="sourcecollectionattempt",
            name="adapter_config_hash",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourcecollectionattempt",
            name="adapter_implementation_manifest_hash",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourcecollectionattempt",
            name="request_fingerprint",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourcecollectionattempt",
            name="request_window_end",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sourcecollectionattempt",
            name="request_window_start",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="sourceitem",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "활성"),
                    ("corrected", "정정"),
                    ("retracted", "철회"),
                    ("unavailable", "접근 불가"),
                ],
                default="active",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="runsourceitem",
            name="discovery_kind",
            field=models.CharField(
                choices=[
                    ("new_version", "새 버전"),
                    ("unchanged", "변경 없음"),
                    ("corrected", "정정"),
                    ("retracted", "철회"),
                    ("unavailable", "접근 불가"),
                    ("restored", "복구"),
                ],
                default="new_version",
                max_length=24,
            ),
        ),
        migrations.AddConstraint(
            model_name="sourcecollectionattempt",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("request_fingerprint__isnull", False)
                ),
                fields=("request_fingerprint",),
                name="uq_collection_attempt_request_fingerprint",
            ),
        ),
        migrations.AddConstraint(
            model_name="sourceitem",
            constraint=models.CheckConstraint(
                condition=~models.Q(
                    ("id", models.F("supersedes_id"))
                ),
                name="ck_source_item_not_self_superseding",
            ),
        ),
        migrations.AddConstraint(
            model_name="runsourceitem",
            constraint=models.CheckConstraint(
                condition=~models.Q(
                    ("id", models.F("previous_run_source_item_id"))
                ),
                name="ck_run_source_item_not_self_previous",
            ),
        ),
        migrations.RunPython(
            create_lineage_guards,
            drop_lineage_guards,
        ),
    ]
