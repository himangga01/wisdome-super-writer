import uuid

import django.db.models.deletion
from django.db import migrations, models


POSTGRES_FUNCTION = "publishing_reject_approval_mutation"
POSTGRES_TRIGGER = "publishing_approval_append_only"
SQLITE_UPDATE_TRIGGER = "publishing_approval_no_update"
SQLITE_DELETE_TRIGGER = "publishing_approval_no_delete"
POSTGRES_HEAD_FUNCTION = "publishing_validate_approval_head"
POSTGRES_HEAD_TRIGGER = "publishing_approval_head_guard"
SQLITE_HEAD_INSERT_TRIGGER = "publishing_approval_head_insert_guard"
SQLITE_HEAD_UPDATE_TRIGGER = "publishing_approval_head_update_guard"
SQLITE_HEAD_DELETE_TRIGGER = "publishing_approval_head_no_delete"


def backfill_heads_and_fences(apps, schema_editor):
    Approval = apps.get_model("publishing", "Approval")
    Head = apps.get_model("publishing", "PublicationApprovalHead")
    Target = apps.get_model("publishing", "PublicationTarget")
    Fence = apps.get_model("publishing", "PublicationTargetIntentFence")
    alias = schema_editor.connection.alias

    Fence.objects.using(alias).bulk_create(
        [Fence(target_id=target_id) for target_id in Target.objects.using(alias).values_list("id", flat=True)],
        ignore_conflicts=True,
    )
    groups = {}
    for row in Approval.objects.using(alias).order_by(
        "publication_intent_id", "target_id", "decided_at", "id"
    ):
        groups.setdefault(
            (row.publication_intent_id, row.target_id), []
        ).append(row)
    for (intent_id, target_id), rows in groups.items():
        by_id = {row.id: row for row in rows}
        superseded = {
            row.supersedes_approval_id
            for row in rows
            if row.supersedes_approval_id is not None
        }
        leaves = [row for row in rows if row.id not in superseded]
        if len(leaves) != 1:
            raise RuntimeError(
                "Approval head backfill found an ambiguous supersession chain: "
                f"intent={intent_id}; target={target_id}; leaves={len(leaves)}"
            )
        versions = {}

        def version(row, visiting):
            if row.id in versions:
                return versions[row.id]
            if row.id in visiting:
                raise RuntimeError("Approval head backfill found a cycle")
            if row.supersedes_approval_id is None:
                value = 1
            else:
                parent = by_id.get(row.supersedes_approval_id)
                if parent is None:
                    raise RuntimeError(
                        "Approval head backfill found a missing superseded decision"
                    )
                value = version(parent, {*visiting, row.id}) + 1
            versions[row.id] = value
            return value

        for row in rows:
            Approval.objects.using(alias).filter(pk=row.id).update(
                head_version=version(row, set())
            )
        leaf = leaves[0]
        Head.objects.using(alias).create(
            publication_intent_id=intent_id,
            target_id=target_id,
            latest_approval_id=leaf.id,
            version=versions[leaf.id],
        )


def install_approval_guards(apps, schema_editor):
    connection = schema_editor.connection
    vendor = connection.vendor
    table = schema_editor.quote_name("publishing_approval")
    head_table = schema_editor.quote_name(
        "publishing_publicationapprovalhead"
    )
    with connection.cursor() as cursor:
        if vendor == "postgresql":
            function = schema_editor.quote_name(POSTGRES_FUNCTION)
            trigger = schema_editor.quote_name(POSTGRES_TRIGGER)
            cursor.execute(
                f"""
                CREATE OR REPLACE FUNCTION {function}()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $publishing_approval_append_only$
                BEGIN
                    RAISE EXCEPTION 'Approval is append-only; % is forbidden', TG_OP
                        USING ERRCODE = '55000';
                END;
                $publishing_approval_append_only$;
                """
            )
            cursor.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
            cursor.execute(
                f"""
                CREATE TRIGGER {trigger}
                BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION {function}()
                """
            )
            head_function = schema_editor.quote_name(
                POSTGRES_HEAD_FUNCTION
            )
            head_trigger = schema_editor.quote_name(POSTGRES_HEAD_TRIGGER)
            cursor.execute(
                f"""
                CREATE OR REPLACE FUNCTION {head_function}()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $publishing_approval_head_guard$
                BEGIN
                    IF TG_OP = 'DELETE' THEN
                        RAISE EXCEPTION 'PublicationApprovalHead cannot be deleted'
                            USING ERRCODE = '55000';
                    END IF;
                    IF TG_OP = 'INSERT' THEN
                        IF NEW.version <> 1 OR NOT EXISTS (
                            SELECT 1 FROM {table} approval
                            WHERE approval.id = NEW.latest_approval_id
                              AND approval.publication_intent_id = NEW.publication_intent_id
                              AND approval.target_id = NEW.target_id
                              AND approval.head_version = NEW.version
                              AND approval.supersedes_approval_id IS NULL
                        ) THEN
                            RAISE EXCEPTION 'PublicationApprovalHead initial lineage is invalid'
                                USING ERRCODE = '23514';
                        END IF;
                    ELSIF TG_OP = 'UPDATE' THEN
                        IF NEW.version <> OLD.version + 1 OR NOT EXISTS (
                            SELECT 1 FROM {table} approval
                            WHERE approval.id = NEW.latest_approval_id
                              AND approval.publication_intent_id = NEW.publication_intent_id
                              AND approval.target_id = NEW.target_id
                              AND approval.head_version = NEW.version
                              AND approval.supersedes_approval_id = OLD.latest_approval_id
                        ) THEN
                            RAISE EXCEPTION 'PublicationApprovalHead lineage is invalid'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;
                    RETURN NEW;
                END;
                $publishing_approval_head_guard$;
                """
            )
            cursor.execute(
                f"DROP TRIGGER IF EXISTS {head_trigger} ON {head_table}"
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {head_trigger}
                BEFORE INSERT OR UPDATE OR DELETE ON {head_table}
                FOR EACH ROW EXECUTE FUNCTION {head_function}()
                """
            )
            return
        if vendor == "sqlite":
            update_trigger = schema_editor.quote_name(SQLITE_UPDATE_TRIGGER)
            delete_trigger = schema_editor.quote_name(SQLITE_DELETE_TRIGGER)
            cursor.execute(f"DROP TRIGGER IF EXISTS {update_trigger}")
            cursor.execute(f"DROP TRIGGER IF EXISTS {delete_trigger}")
            cursor.execute(
                f"""
                CREATE TRIGGER {update_trigger}
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'Approval is append-only; UPDATE is forbidden');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {delete_trigger}
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'Approval is append-only; DELETE is forbidden');
                END
                """
            )
            head_insert = schema_editor.quote_name(
                SQLITE_HEAD_INSERT_TRIGGER
            )
            head_update = schema_editor.quote_name(
                SQLITE_HEAD_UPDATE_TRIGGER
            )
            head_delete = schema_editor.quote_name(
                SQLITE_HEAD_DELETE_TRIGGER
            )
            for trigger_name in (head_insert, head_update, head_delete):
                cursor.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            cursor.execute(
                f"""
                CREATE TRIGGER {head_insert}
                BEFORE INSERT ON {head_table}
                WHEN NEW.version <> 1 OR NOT EXISTS (
                    SELECT 1 FROM {table} approval
                    WHERE approval.id = NEW.latest_approval_id
                      AND approval.publication_intent_id = NEW.publication_intent_id
                      AND approval.target_id = NEW.target_id
                      AND approval.head_version = NEW.version
                      AND approval.supersedes_approval_id IS NULL
                )
                BEGIN
                    SELECT RAISE(ABORT, 'PublicationApprovalHead initial lineage is invalid');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {head_update}
                BEFORE UPDATE ON {head_table}
                WHEN NEW.version <> OLD.version + 1 OR NOT EXISTS (
                    SELECT 1 FROM {table} approval
                    WHERE approval.id = NEW.latest_approval_id
                      AND approval.publication_intent_id = NEW.publication_intent_id
                      AND approval.target_id = NEW.target_id
                      AND approval.head_version = NEW.version
                      AND approval.supersedes_approval_id = OLD.latest_approval_id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'PublicationApprovalHead lineage is invalid');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {head_delete}
                BEFORE DELETE ON {head_table}
                BEGIN
                    SELECT RAISE(ABORT, 'PublicationApprovalHead cannot be deleted');
                END
                """
            )
            return
    raise RuntimeError(
        f"Approval append-only enforcement does not support database vendor {vendor!r}"
    )


def remove_approval_guards(apps, schema_editor):
    connection = schema_editor.connection
    vendor = connection.vendor
    table = schema_editor.quote_name("publishing_approval")
    head_table = schema_editor.quote_name(
        "publishing_publicationapprovalhead"
    )
    with connection.cursor() as cursor:
        if vendor == "postgresql":
            cursor.execute(
                f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(POSTGRES_TRIGGER)} ON {table}"
            )
            cursor.execute(
                f"DROP FUNCTION IF EXISTS {schema_editor.quote_name(POSTGRES_FUNCTION)}()"
            )
            cursor.execute(
                f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(POSTGRES_HEAD_TRIGGER)} ON {head_table}"
            )
            cursor.execute(
                f"DROP FUNCTION IF EXISTS {schema_editor.quote_name(POSTGRES_HEAD_FUNCTION)}()"
            )
            return
        if vendor == "sqlite":
            cursor.execute(
                f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(SQLITE_UPDATE_TRIGGER)}"
            )
            cursor.execute(
                f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(SQLITE_DELETE_TRIGGER)}"
            )
            for trigger_name in (
                SQLITE_HEAD_INSERT_TRIGGER,
                SQLITE_HEAD_UPDATE_TRIGGER,
                SQLITE_HEAD_DELETE_TRIGGER,
            ):
                cursor.execute(
                    f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(trigger_name)}"
                )
            return
    raise RuntimeError(
        f"Approval append-only enforcement does not support database vendor {vendor!r}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0007_publication_execution_observations"),
    ]

    operations = [
        migrations.CreateModel(
            name="PublicationTargetIntentFence",
            fields=[
                (
                    "target",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="intent_fence",
                        serialize=False,
                        to="publishing.publicationtarget",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="approval",
            name="approval_material_version",
            field=models.CharField(
                default="approval-subject-v1",
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="approval",
            name="head_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.CreateModel(
            name="PublicationApprovalHead",
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
                ("version", models.PositiveIntegerField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "latest_approval",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="headed_by",
                        to="publishing.approval",
                    ),
                ),
                (
                    "publication_intent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="approval_heads",
                        to="publishing.publicationintent",
                    ),
                ),
                (
                    "target",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approval_heads",
                        to="publishing.publicationtarget",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="publicationapprovalhead",
            constraint=models.UniqueConstraint(
                fields=("publication_intent", "target"),
                name="uq_publication_approval_head",
            ),
        ),
        migrations.AddConstraint(
            model_name="approval",
            constraint=models.CheckConstraint(
                condition=models.Q(("head_version__gte", 1)),
                name="ck_approval_head_version_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="publicationapprovalhead",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)),
                name="ck_publication_approval_head_version_positive",
            ),
        ),
        migrations.RunPython(
            backfill_heads_and_fences,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RunPython(
            install_approval_guards,
            reverse_code=remove_approval_guards,
        ),
    ]
