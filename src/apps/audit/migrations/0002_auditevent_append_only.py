from django.db import migrations, models
from django.db.models.functions import Length


POSTGRES_FUNCTION = "audit_reject_auditevent_mutation"
POSTGRES_TRIGGER = "audit_auditevent_append_only"
SQLITE_UPDATE_TRIGGER = "audit_auditevent_no_update"
SQLITE_DELETE_TRIGGER = "audit_auditevent_no_delete"
PREFLIGHT_SAMPLE_LIMIT = 10
LEGACY_REASON_MAX_LENGTH = 100


def preflight_actor_shape(apps, schema_editor):
    AuditEvent = apps.get_model("audit", "AuditEvent")
    alias = schema_editor.connection.alias
    valid_shape = (
        models.Q(actor_type="admin", actor_id__isnull=False)
        | models.Q(
            actor_type__in=("system", "worker"),
            actor_id__isnull=True,
        )
    )
    invalid_rows = (
        AuditEvent.objects.using(alias)
        .exclude(valid_shape)
        .order_by("id")
    )
    invalid_count = invalid_rows.count()
    if not invalid_count:
        return
    sample_ids = [
        str(row_id)
        for row_id in invalid_rows.values_list("id", flat=True)[
            :PREFLIGHT_SAMPLE_LIMIT
        ]
    ]
    raise RuntimeError(
        "AuditEvent actor-shape constraint preflight failed: "
        f"invalid_count={invalid_count}; sample_ids={sample_ids}. "
        "Automatic repair is forbidden."
    )


def preflight_reason_downgrade(apps, schema_editor):
    AuditEvent = apps.get_model("audit", "AuditEvent")
    alias = schema_editor.connection.alias
    oversized_rows = (
        AuditEvent.objects.using(alias)
        .annotate(_reason_length=Length("reason_code"))
        .filter(_reason_length__gt=LEGACY_REASON_MAX_LENGTH)
        .order_by("id")
    )
    oversized_count = oversized_rows.count()
    if not oversized_count:
        return
    sample_ids = [
        str(row_id)
        for row_id in oversized_rows.values_list("id", flat=True)[
            :PREFLIGHT_SAMPLE_LIMIT
        ]
    ]
    raise RuntimeError(
        "AuditEvent reason_code downgrade preflight failed before append-only "
        "guards were removed: "
        f"oversized_count={oversized_count}; sample_ids={sample_ids}; "
        f"legacy_max_length={LEGACY_REASON_MAX_LENGTH}. "
        "Automatic truncation is forbidden."
    )


def install_append_only_guards(apps, schema_editor):
    connection = schema_editor.connection
    vendor = connection.vendor
    table = schema_editor.quote_name("audit_auditevent")
    with connection.cursor() as cursor:
        if vendor == "postgresql":
            function = schema_editor.quote_name(POSTGRES_FUNCTION)
            trigger = schema_editor.quote_name(POSTGRES_TRIGGER)
            cursor.execute(
                f"""
                CREATE OR REPLACE FUNCTION {function}()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $audit_append_only$
                BEGIN
                    RAISE EXCEPTION 'AuditEvent is append-only; % is forbidden', TG_OP
                        USING ERRCODE = '55000';
                END;
                $audit_append_only$;
                """
            )
            cursor.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
            cursor.execute(
                f"""
                CREATE TRIGGER {trigger}
                BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
                FOR EACH STATEMENT
                EXECUTE FUNCTION {function}()
                """
            )
            cursor.execute(
                f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {table} FROM PUBLIC"
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
                    SELECT RAISE(ABORT, 'AuditEvent is append-only; UPDATE is forbidden');
                END
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {delete_trigger}
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'AuditEvent is append-only; DELETE is forbidden');
                END
                """
            )
            return
    raise RuntimeError(
        f"AuditEvent append-only enforcement does not support database vendor {vendor!r}"
    )


def remove_append_only_guards(apps, schema_editor):
    connection = schema_editor.connection
    vendor = connection.vendor
    table = schema_editor.quote_name("audit_auditevent")
    with connection.cursor() as cursor:
        if vendor == "postgresql":
            trigger = schema_editor.quote_name(POSTGRES_TRIGGER)
            function = schema_editor.quote_name(POSTGRES_FUNCTION)
            cursor.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
            cursor.execute(f"DROP FUNCTION IF EXISTS {function}()")
            return
        if vendor == "sqlite":
            cursor.execute(
                f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(SQLITE_UPDATE_TRIGGER)}"
            )
            cursor.execute(
                f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(SQLITE_DELETE_TRIGGER)}"
            )
            return
    raise RuntimeError(
        f"AuditEvent append-only enforcement does not support database vendor {vendor!r}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="auditevent",
            options={
                "base_manager_name": "objects",
                "default_manager_name": "objects",
                "ordering": ("-occurred_at", "-id"),
            },
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="reason_code",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.RunPython(
            preflight_actor_shape,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveConstraint(
            model_name="auditevent",
            name="audit_admin_actor_required",
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(actor_type="admin", actor__isnull=False)
                    | models.Q(
                        actor_type__in=("system", "worker"),
                        actor__isnull=True,
                    )
                ),
                name="audit_actor_shape_required",
            ),
        ),
        migrations.RunPython(
            install_append_only_guards,
            reverse_code=remove_append_only_guards,
        ),
        migrations.RunPython(
            migrations.RunPython.noop,
            reverse_code=preflight_reason_downgrade,
        ),
    ]
