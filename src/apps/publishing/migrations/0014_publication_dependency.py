import hashlib
import importlib
import json

from django.db import migrations, models
import django.db.models.deletion
from django.db.migrations.exceptions import IrreversibleError


DEPENDENCY_VERSION = "publication-dependency-v1"
SQLITE_TRIGGER_NAMES = (
    "publishing_attempt_dependency_insert_guard_t024",
    "publishing_attempt_dependency_update_guard_t024",
)
POSTGRES_TRIGGER_NAMES = SQLITE_TRIGGER_NAMES
POSTGRES_FUNCTION_NAME = "publishing_attempt_dependency_guard_t024_fn"


def _migration(name):
    return importlib.import_module(f"apps.publishing.migrations.{name}")


def remove_existing_publishing_guards(apps, schema_editor):
    _migration("0013_publisher_credentials").remove_existing_publishing_guards(
        apps, schema_editor
    )


def install_existing_publishing_guards(apps, schema_editor):
    _migration("0013_publisher_credentials").install_existing_publishing_guards(
        apps, schema_editor
    )


def _sha256(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _dependency_subject(*, dependent, dependency):
    return {
        "version": DEPENDENCY_VERSION,
        "publicationIntentId": str(dependent.publication_intent_id),
        "articleRevisionId": str(dependent.article_revision_id),
        "dependentTargetId": str(dependent.publication.target_id),
        "dependentTargetSnapshotId": str(dependent.target_snapshot_id),
        "dependentTargetConfigHash": dependent.target_config_hash,
        "dependentAction": dependent.resolved_action,
        "dependencyAttemptId": str(dependency.id),
        "dependencyPublicationId": str(dependency.publication_id),
        "dependencyTargetId": str(dependency.publication.target_id),
        "dependencyTargetSnapshotId": str(dependency.target_snapshot_id),
        "dependencyTargetConfigHash": dependency.target_config_hash,
        "dependencyAction": dependency.resolved_action,
        "dependencyApprovalSubjectHash": dependency.approval_subject_hash,
        "dependencyRequestFingerprint": dependency.request_fingerprint,
        "environment": dependency.publication.target.environment,
    }


def backfill_dependencies(apps, schema_editor):
    alias = schema_editor.connection.alias
    Attempt = apps.get_model("publishing", "PublicationAttempt")
    Dispatch = apps.get_model("publishing", "PublicationDispatch")
    attempts = list(
        Attempt.objects.using(alias)
        .select_related(
            "publication__target",
            "publication_intent",
            "target_snapshot",
        )
        .order_by("id")
    )
    by_intent_target = {}
    for row in attempts:
        by_intent_target.setdefault(
            (str(row.publication_intent_id), str(row.publication.target_id)),
            [],
        ).append(row)
    for row in attempts:
        target = row.publication.target
        if target.channel != "blogger":
            continue
        commands = [
            command
            for command in (row.publication_intent.target_commands or [])
            if isinstance(command, dict)
            and str(command.get("targetId")) == str(target.id)
        ]
        if len(commands) != 1 or not commands[0].get(
            "canonicalDependencyTargetId"
        ):
            raise RuntimeError(
                "T024 cannot prove a legacy Blogger dependency command"
            )
        dependency_target_id = str(
            commands[0]["canonicalDependencyTargetId"]
        )
        candidates = by_intent_target.get(
            (str(row.publication_intent_id), dependency_target_id),
            [],
        )
        if len(candidates) != 1:
            raise RuntimeError(
                "T024 legacy Blogger dependency is missing or ambiguous"
            )
        dependency = candidates[0]
        dependency_target = dependency.publication.target
        if (
            dependency_target.channel != "wordpress"
            or dependency_target.role != "primary_canonical"
            or dependency_target.environment != target.environment
            or dependency.article_revision_id != row.article_revision_id
            or dependency.publication.article_id != row.publication.article_id
        ):
            raise RuntimeError(
                "T024 legacy Blogger dependency lineage is invalid"
            )
        row.depends_on_attempt_id = dependency.id
        row.dependency_subject_hash = _sha256(
            _dependency_subject(dependent=row, dependency=dependency)
        )
        row.save(
            update_fields=(
                "depends_on_attempt",
                "dependency_subject_hash",
            )
        )
    for dispatch in Dispatch.objects.using(alias).order_by("id"):
        cohort = [
            row
            for row in attempts
            if row.publication_intent_id == dispatch.publication_intent_id
        ]
        if len(cohort) != dispatch.attempt_count:
            raise RuntimeError(
                "T024 dispatch cohort count differs from its immutable ledger"
            )
        manifest = [
            {
                "attemptId": str(row.id),
                "publicationId": str(row.publication_id),
                "targetId": str(row.publication.target_id),
                "attemptNo": 1,
                "resolvedAction": row.resolved_action,
                "dependsOnAttemptId": (
                    str(row.depends_on_attempt_id)
                    if row.depends_on_attempt_id
                    else None
                ),
                "dependencySubjectHash": row.dependency_subject_hash,
            }
            for row in sorted(cohort, key=lambda value: str(value.id))
        ]
        dispatch.attempt_manifest_hash = _sha256(manifest)
        dispatch.save(update_fields=("attempt_manifest_hash",))


SQLITE_STATEMENTS = (
    """
    CREATE TRIGGER publishing_attempt_dependency_insert_guard_t024
    BEFORE INSERT ON publishing_publicationattempt
    BEGIN
      SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM publishing_publication child_publication
        JOIN publishing_publicationtarget child_target
          ON child_target.id = child_publication.target_id
        WHERE child_publication.id = NEW.publication_id
          AND child_target.channel = 'blogger'
          AND (
            NEW.depends_on_attempt_id IS NULL
            OR length(NEW.dependency_subject_hash) <> 64
            OR NEW.dependency_subject_hash GLOB '*[^0-9a-f]*'
          )
      ) THEN RAISE(ABORT, 'Blogger attempt dependency is incomplete') END;
      SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM publishing_publication child_publication
        JOIN publishing_publicationtarget child_target
          ON child_target.id = child_publication.target_id
        WHERE child_publication.id = NEW.publication_id
          AND child_target.channel <> 'blogger'
          AND (
            NEW.depends_on_attempt_id IS NOT NULL
            OR NEW.dependency_subject_hash <> ''
          )
      ) THEN RAISE(ABORT, 'non-Blogger attempt cannot have a dependency') END;
      SELECT CASE WHEN NEW.depends_on_attempt_id = NEW.id
        THEN RAISE(ABORT, 'publication attempt cannot depend on itself') END;
      SELECT CASE WHEN NEW.depends_on_attempt_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM publishing_publicationattempt dependency
        JOIN publishing_publication dependency_publication
          ON dependency_publication.id = dependency.publication_id
        JOIN publishing_publicationtarget dependency_target
          ON dependency_target.id = dependency_publication.target_id
        JOIN publishing_publication child_publication
          ON child_publication.id = NEW.publication_id
        JOIN publishing_publicationtarget child_target
          ON child_target.id = child_publication.target_id
        JOIN publishing_publicationintent intent
          ON intent.id = NEW.publication_intent_id
        WHERE dependency.id = NEW.depends_on_attempt_id
          AND dependency_target.channel = 'wordpress'
          AND dependency_target.role = 'primary_canonical'
          AND child_target.channel = 'blogger'
          AND dependency_target.environment = child_target.environment
          AND dependency.publication_intent_id = NEW.publication_intent_id
          AND dependency.article_revision_id = NEW.article_revision_id
          AND dependency_publication.article_id = child_publication.article_id
          AND (SELECT count(*) FROM json_each(intent.target_commands) command
               WHERE replace(json_extract(command.value, '$.targetId'), '-', '') = child_publication.target_id
                 AND replace(json_extract(command.value, '$.canonicalDependencyTargetId'), '-', '') = dependency_publication.target_id) = 1
      ) THEN RAISE(ABORT, 'publication attempt dependency lineage is invalid') END;
    END
    """,
    """
    CREATE TRIGGER publishing_attempt_dependency_update_guard_t024
    BEFORE UPDATE ON publishing_publicationattempt
    WHEN NEW.depends_on_attempt_id IS NOT OLD.depends_on_attempt_id
      OR NEW.dependency_subject_hash IS NOT OLD.dependency_subject_hash
    BEGIN
      SELECT RAISE(ABORT, 'publication attempt dependency is immutable');
    END
    """,
)


POSTGRES_STATEMENTS = (
    """
    CREATE OR REPLACE FUNCTION publishing_attempt_dependency_guard_t024_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      child_channel text;
    BEGIN
      IF TG_OP = 'UPDATE' THEN
        IF NEW.depends_on_attempt_id IS DISTINCT FROM OLD.depends_on_attempt_id
           OR NEW.dependency_subject_hash IS DISTINCT FROM OLD.dependency_subject_hash THEN
          RAISE EXCEPTION 'publication attempt dependency is immutable';
        END IF;
        RETURN NEW;
      END IF;
      SELECT target.channel INTO child_channel
      FROM publishing_publication publication
      JOIN publishing_publicationtarget target ON target.id = publication.target_id
      WHERE publication.id = NEW.publication_id;
      IF child_channel = 'blogger' AND (
        NEW.depends_on_attempt_id IS NULL
        OR NEW.dependency_subject_hash !~ '^[0-9a-f]{64}$'
      ) THEN
        RAISE EXCEPTION 'Blogger attempt dependency is incomplete';
      END IF;
      IF child_channel <> 'blogger' AND (
        NEW.depends_on_attempt_id IS NOT NULL
        OR NEW.dependency_subject_hash <> ''
      ) THEN
        RAISE EXCEPTION 'non-Blogger attempt cannot have a dependency';
      END IF;
      IF NEW.depends_on_attempt_id = NEW.id THEN
        RAISE EXCEPTION 'publication attempt cannot depend on itself';
      END IF;
      IF NEW.depends_on_attempt_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM publishing_publicationattempt dependency
        JOIN publishing_publication dependency_publication
          ON dependency_publication.id = dependency.publication_id
        JOIN publishing_publicationtarget dependency_target
          ON dependency_target.id = dependency_publication.target_id
        JOIN publishing_publication child_publication
          ON child_publication.id = NEW.publication_id
        JOIN publishing_publicationtarget child_target
          ON child_target.id = child_publication.target_id
        JOIN publishing_publicationintent intent
          ON intent.id = NEW.publication_intent_id
        WHERE dependency.id = NEW.depends_on_attempt_id
          AND dependency_target.channel = 'wordpress'
          AND dependency_target.role = 'primary_canonical'
          AND child_target.channel = 'blogger'
          AND dependency_target.environment = child_target.environment
          AND dependency.publication_intent_id = NEW.publication_intent_id
          AND dependency.article_revision_id = NEW.article_revision_id
          AND dependency_publication.article_id = child_publication.article_id
          AND (SELECT count(*) FROM jsonb_array_elements(intent.target_commands) command
               WHERE command->>'targetId' = child_publication.target_id::text
                 AND command->>'canonicalDependencyTargetId' = dependency_publication.target_id::text) = 1
      ) THEN
        RAISE EXCEPTION 'publication attempt dependency lineage is invalid';
      END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER publishing_attempt_dependency_insert_guard_t024 BEFORE INSERT ON publishing_publicationattempt FOR EACH ROW EXECUTE FUNCTION publishing_attempt_dependency_guard_t024_fn()""",
    """CREATE TRIGGER publishing_attempt_dependency_update_guard_t024 BEFORE UPDATE ON publishing_publicationattempt FOR EACH ROW EXECUTE FUNCTION publishing_attempt_dependency_guard_t024_fn()""",
)


def remove_dependency_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    with schema_editor.connection.cursor() as cursor:
        if vendor == "sqlite":
            for name in SQLITE_TRIGGER_NAMES:
                cursor.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        elif vendor == "postgresql":
            for name in POSTGRES_TRIGGER_NAMES:
                cursor.execute(
                    f'DROP TRIGGER IF EXISTS "{name}" ON publishing_publicationattempt'
                )
            cursor.execute(
                f'DROP FUNCTION IF EXISTS "{POSTGRES_FUNCTION_NAME}"()'
            )
        else:
            raise RuntimeError(f"T024 database guards do not support {vendor}")


def install_dependency_guards(apps, schema_editor):
    statements = (
        SQLITE_STATEMENTS
        if schema_editor.connection.vendor == "sqlite"
        else POSTGRES_STATEMENTS
        if schema_editor.connection.vendor == "postgresql"
        else None
    )
    if statements is None:
        raise RuntimeError(
            f"T024 database guards do not support {schema_editor.connection.vendor}"
        )
    remove_dependency_guards(apps, schema_editor)
    with schema_editor.connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def reject_populated_reverse(apps, schema_editor):
    Attempt = apps.get_model("publishing", "PublicationAttempt")
    Dispatch = apps.get_model("publishing", "PublicationDispatch")
    alias = schema_editor.connection.alias
    if (
        Attempt.objects.using(alias).exists()
        or Dispatch.objects.using(alias).exists()
    ):
        raise IrreversibleError(
            "T024 dependency manifests must be remediated before reverse migration"
        )


class Migration(migrations.Migration):
    dependencies = [("publishing", "0013_publisher_credentials")]

    operations = [
        migrations.RunPython(
            remove_existing_publishing_guards,
            reverse_code=install_existing_publishing_guards,
        ),
        migrations.AddField(
            model_name="publicationattempt",
            name="depends_on_attempt",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="dependent_attempts",
                to="publishing.publicationattempt",
            ),
        ),
        migrations.AddField(
            model_name="publicationattempt",
            name="dependency_subject_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.RunPython(
            backfill_dependencies,
            reverse_code=reject_populated_reverse,
        ),
        migrations.RunPython(
            install_existing_publishing_guards,
            reverse_code=remove_existing_publishing_guards,
        ),
        migrations.RunPython(
            install_dependency_guards,
            reverse_code=remove_dependency_guards,
        ),
    ]
