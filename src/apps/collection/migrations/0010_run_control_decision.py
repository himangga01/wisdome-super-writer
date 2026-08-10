import django.core.validators
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


def install_append_only_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        schema_editor.execute(
            """
            CREATE TRIGGER collection_run_control_no_update
            BEFORE UPDATE ON collection_runcontroldecision
            BEGIN
              SELECT RAISE(ABORT, 'RunControlDecision is append-only');
            END
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER collection_run_control_no_delete
            BEFORE DELETE ON collection_runcontroldecision
            BEGIN
              SELECT RAISE(ABORT, 'RunControlDecision is append-only');
            END
            """
        )
    elif vendor == "postgresql":
        schema_editor.execute(
            """
            CREATE OR REPLACE FUNCTION collection_run_control_append_only()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'RunControlDecision is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER collection_run_control_no_update
            BEFORE UPDATE ON collection_runcontroldecision
            FOR EACH ROW EXECUTE FUNCTION collection_run_control_append_only()
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER collection_run_control_no_delete
            BEFORE DELETE ON collection_runcontroldecision
            FOR EACH ROW EXECUTE FUNCTION collection_run_control_append_only()
            """
        )


def remove_append_only_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS collection_run_control_no_update")
        schema_editor.execute("DROP TRIGGER IF EXISTS collection_run_control_no_delete")
    elif vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS collection_run_control_no_update "
            "ON collection_runcontroldecision"
        )
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS collection_run_control_no_delete "
            "ON collection_runcontroldecision"
        )
        schema_editor.execute(
            "DROP FUNCTION IF EXISTS collection_run_control_append_only()"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("collection", "0009_run_step_generation_fencing"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RunControlDecision",
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
                (
                    "action",
                    models.CharField(
                        choices=[("stop", "Stop"), ("retry", "Retry")],
                        max_length=24,
                    ),
                ),
                ("scope", models.JSONField(blank=True, default=dict)),
                ("request_key", models.CharField(max_length=200)),
                (
                    "request_hash",
                    models.CharField(
                        max_length=64,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[a-f0-9]{64}$",
                                "Expected a lowercase SHA-256 digest",
                            )
                        ],
                    ),
                ),
                ("reauth_proof_id", models.UUIDField(blank=True, null=True)),
                ("decided_at", models.DateTimeField(auto_now_add=True)),
                (
                    "decided_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="control_decisions",
                        to="collection.collectionrun",
                    ),
                ),
            ],
            options={"ordering": ("decided_at", "id")},
        ),
        migrations.AddConstraint(
            model_name="runcontroldecision",
            constraint=models.UniqueConstraint(
                fields=("run", "request_key"),
                name="uq_run_control_decision_request",
            ),
        ),
        migrations.RunPython(
            install_append_only_guards,
            remove_append_only_guards,
        ),
    ]
