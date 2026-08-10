from django.conf import settings
from django.db.migrations.exceptions import IrreversibleError
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion
import uuid


SQLITE_FORWARD = """
CREATE TRIGGER editorial_correction_decision_insert_guard
BEFORE INSERT ON editorial_correctiondecision
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM editorial_correctioncase c
    WHERE c.id = NEW.correction_case_id
      AND c.subject_hash = NEW.subject_hash
      AND (
        (
          c.decision_version = 0
          AND c.latest_decision_id IS NULL
          AND NEW.head_version = 1
          AND NEW.supersedes_id IS NULL
        )
        OR (
          c.decision_version > 0
          AND c.latest_decision_id = NEW.supersedes_id
          AND NEW.head_version = c.decision_version + 1
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'CorrectionDecision must extend the current case head');
END;

CREATE TRIGGER editorial_correction_decision_update_guard
BEFORE UPDATE ON editorial_correctiondecision
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'CorrectionDecision is append-only');
END;

CREATE TRIGGER editorial_correction_decision_delete_guard
BEFORE DELETE ON editorial_correctiondecision
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'CorrectionDecision is append-only');
END;

CREATE TRIGGER editorial_correction_case_head_guard
BEFORE UPDATE OF latest_decision_id, decision_version, corrected_revision_id
ON editorial_correctioncase
FOR EACH ROW
WHEN (
    NEW.latest_decision_id IS NOT OLD.latest_decision_id
    OR NEW.decision_version <> OLD.decision_version
    OR NEW.corrected_revision_id IS NOT OLD.corrected_revision_id
) AND NOT EXISTS (
    SELECT 1
    FROM editorial_correctiondecision d
    WHERE d.id = NEW.latest_decision_id
      AND d.correction_case_id = NEW.id
      AND d.head_version = NEW.decision_version
      AND (
        (d.decision = 'verified' AND NEW.corrected_revision_id = d.corrected_revision_id)
        OR (d.decision = 'rejected' AND NEW.corrected_revision_id IS NULL)
      )
)
BEGIN
    SELECT RAISE(ABORT, 'CorrectionCase head must match its latest decision');
END;
"""


SQLITE_REVERSE = """
DROP TRIGGER IF EXISTS editorial_correction_case_head_guard;
DROP TRIGGER IF EXISTS editorial_correction_decision_delete_guard;
DROP TRIGGER IF EXISTS editorial_correction_decision_update_guard;
DROP TRIGGER IF EXISTS editorial_correction_decision_insert_guard;
"""


POSTGRES_FORWARD = """
CREATE OR REPLACE FUNCTION editorial_correction_decision_insert_guard_fn()
RETURNS trigger AS $$
DECLARE
    current_case editorial_correctioncase%ROWTYPE;
BEGIN
    SELECT * INTO current_case
    FROM editorial_correctioncase
    WHERE id = NEW.correction_case_id
    FOR UPDATE;
    IF NOT FOUND
       OR current_case.subject_hash IS DISTINCT FROM NEW.subject_hash
       OR NOT (
         (
           current_case.decision_version = 0
           AND current_case.latest_decision_id IS NULL
           AND NEW.head_version = 1
           AND NEW.supersedes_id IS NULL
         )
         OR (
           current_case.decision_version > 0
           AND current_case.latest_decision_id = NEW.supersedes_id
           AND NEW.head_version = current_case.decision_version + 1
         )
       ) THEN
        RAISE EXCEPTION 'CorrectionDecision must extend the current case head'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION editorial_correction_decision_append_only_fn()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'CorrectionDecision is append-only' USING ERRCODE = '23514';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION editorial_correction_case_head_guard_fn()
RETURNS trigger AS $$
BEGIN
    IF (
      NEW.latest_decision_id IS DISTINCT FROM OLD.latest_decision_id
      OR NEW.decision_version IS DISTINCT FROM OLD.decision_version
      OR NEW.corrected_revision_id IS DISTINCT FROM OLD.corrected_revision_id
    ) AND NOT EXISTS (
      SELECT 1
      FROM editorial_correctiondecision d
      WHERE d.id = NEW.latest_decision_id
        AND d.correction_case_id = NEW.id
        AND d.head_version = NEW.decision_version
        AND (
          (d.decision = 'verified' AND NEW.corrected_revision_id = d.corrected_revision_id)
          OR (d.decision = 'rejected' AND NEW.corrected_revision_id IS NULL)
        )
    ) THEN
        RAISE EXCEPTION 'CorrectionCase head must match its latest decision'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER editorial_correction_decision_insert_guard
BEFORE INSERT ON editorial_correctiondecision
FOR EACH ROW EXECUTE FUNCTION editorial_correction_decision_insert_guard_fn();

CREATE TRIGGER editorial_correction_decision_update_guard
BEFORE UPDATE ON editorial_correctiondecision
FOR EACH ROW EXECUTE FUNCTION editorial_correction_decision_append_only_fn();

CREATE TRIGGER editorial_correction_decision_delete_guard
BEFORE DELETE ON editorial_correctiondecision
FOR EACH ROW EXECUTE FUNCTION editorial_correction_decision_append_only_fn();

CREATE TRIGGER editorial_correction_case_head_guard
BEFORE UPDATE OF latest_decision_id, decision_version, corrected_revision_id
ON editorial_correctioncase
FOR EACH ROW EXECUTE FUNCTION editorial_correction_case_head_guard_fn();
"""


POSTGRES_REVERSE = """
DROP TRIGGER IF EXISTS editorial_correction_case_head_guard ON editorial_correctioncase;
DROP TRIGGER IF EXISTS editorial_correction_decision_delete_guard ON editorial_correctiondecision;
DROP TRIGGER IF EXISTS editorial_correction_decision_update_guard ON editorial_correctiondecision;
DROP TRIGGER IF EXISTS editorial_correction_decision_insert_guard ON editorial_correctiondecision;
DROP FUNCTION IF EXISTS editorial_correction_case_head_guard_fn();
DROP FUNCTION IF EXISTS editorial_correction_decision_append_only_fn();
DROP FUNCTION IF EXISTS editorial_correction_decision_insert_guard_fn();
"""


def install_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        schema_editor.connection.cursor().executescript(SQLITE_FORWARD)
    elif vendor == "postgresql":
        schema_editor.execute(POSTGRES_FORWARD)


def remove_guards(apps, schema_editor):
    CorrectionDecision = apps.get_model("editorial", "CorrectionDecision")
    CorrectionCase = apps.get_model("editorial", "CorrectionCase")
    if (
        CorrectionDecision.objects.using(schema_editor.connection.alias).exists()
        or CorrectionCase.objects.using(schema_editor.connection.alias)
        .filter(
            models.Q(supersedes__isnull=False)
            | models.Q(corrected_revision__isnull=False)
            | models.Q(latest_decision__isnull=False)
            | models.Q(decision_version__gt=0)
            | models.Q(verified_at__isnull=False)
            | models.Q(dispatched_at__isnull=False)
            | ~models.Q(failure_summary={})
        )
        .exists()
    ):
        raise IrreversibleError(
            "Correction decision data must be remediated before reversing 0004."
        )
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        schema_editor.connection.cursor().executescript(SQLITE_REVERSE)
    elif vendor == "postgresql":
        schema_editor.execute(POSTGRES_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("editorial", "0003_editorial_policy_runtime"),
    ]

    operations = [
        migrations.AddField(
            model_name="correctioncase",
            name="corrected_revision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="correction_cases",
                to="editorial.articlerevision",
            ),
        ),
        migrations.AddField(
            model_name="correctioncase",
            name="decision_version",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="correctioncase",
            name="dispatched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="correctioncase",
            name="failure_summary",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="correctioncase",
            name="supersedes",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="superseding_cases",
                to="editorial.correctioncase",
            ),
        ),
        migrations.AddField(
            model_name="correctioncase",
            name="verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="CorrectionDecision",
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
                    "decision",
                    models.CharField(
                        choices=[("verified", "Verified"), ("rejected", "Rejected")],
                        max_length=16,
                    ),
                ),
                (
                    "subject_hash",
                    models.CharField(
                        max_length=64,
                        validators=[
                            django.core.validators.RegexValidator(
                                message="Must be a lowercase SHA-256 hex digest.",
                                regex="^[0-9a-f]{64}$",
                            )
                        ],
                    ),
                ),
                (
                    "diff_manifest_hash",
                    models.CharField(
                        max_length=64,
                        validators=[
                            django.core.validators.RegexValidator(
                                message="Must be a lowercase SHA-256 hex digest.",
                                regex="^[0-9a-f]{64}$",
                            )
                        ],
                    ),
                ),
                ("head_version", models.PositiveIntegerField()),
                ("request_key", models.CharField(max_length=200)),
                (
                    "request_hash",
                    models.CharField(
                        max_length=64,
                        validators=[
                            django.core.validators.RegexValidator(
                                message="Must be a lowercase SHA-256 hex digest.",
                                regex="^[0-9a-f]{64}$",
                            )
                        ],
                    ),
                ),
                ("reauth_proof_id", models.UUIDField()),
                ("decision_reason", models.CharField(max_length=500)),
                ("decided_at", models.DateTimeField(auto_now_add=True)),
                (
                    "corrected_revision",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="correction_decisions",
                        to="editorial.articlerevision",
                    ),
                ),
                (
                    "correction_case",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="decisions",
                        to="editorial.correctioncase",
                    ),
                ),
                (
                    "decided_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="correction_decisions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "supersedes",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="superseding_decisions",
                        to="editorial.correctiondecision",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("correction_case", "request_key"),
                        name="uq_correction_decision_request_key",
                    ),
                    models.UniqueConstraint(
                        fields=("correction_case", "head_version"),
                        name="uq_correction_decision_head_version",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                decision="verified",
                                corrected_revision__isnull=False,
                            )
                            | models.Q(
                                decision="rejected",
                                corrected_revision__isnull=True,
                            )
                        ),
                        name="ck_correction_decision_revision_binding",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(head_version__gt=0),
                        name="ck_correction_decision_head_version_positive",
                    ),
                ]
            },
        ),
        migrations.AddField(
            model_name="correctioncase",
            name="latest_decision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="editorial.correctiondecision",
            ),
        ),
        migrations.AddConstraint(
            model_name="correctioncase",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(decision_version=0, latest_decision__isnull=True)
                    | models.Q(decision_version__gt=0, latest_decision__isnull=False)
                ),
                name="ck_correction_case_decision_head_complete",
            ),
        ),
        migrations.RunPython(install_guards, remove_guards),
    ]
