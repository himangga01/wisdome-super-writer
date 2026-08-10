import importlib

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


LEGACY_VERSION = "legacy-unverifiable-v1"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _migration(name):
    return importlib.import_module(f"apps.publishing.migrations.{name}")


def remove_existing_publishing_guards(apps, schema_editor):
    _migration("0012_published_asset_snapshots").remove_asset_guards(
        apps, schema_editor
    )
    t021 = _migration("0011_publication_attempt_fencing")
    t021.remove_t021_guards(apps, schema_editor)
    t021.remove_t020_guards(apps, schema_editor)
    _migration("0009_approval_decision_integrity").remove_t019_guards(
        apps, schema_editor
    )


def install_existing_publishing_guards(apps, schema_editor):
    _migration("0009_approval_decision_integrity").install_t019_guards(
        apps, schema_editor
    )
    t021 = _migration("0011_publication_attempt_fencing")
    t021.install_t020_guards(apps, schema_editor)
    t021.install_t021_guards(apps, schema_editor)
    _migration("0012_published_asset_snapshots").install_asset_guards(
        apps, schema_editor
    )


def backfill_credential_versions(apps, schema_editor):
    alias = schema_editor.connection.alias
    Target = apps.get_model("publishing", "PublicationTarget")
    Snapshot = apps.get_model("publishing", "PublicationTargetSnapshot")
    Target.objects.using(alias).filter(
        credential_ref__isnull=False,
    ).exclude(credential_ref="").update(credential_version=LEGACY_VERSION)
    Snapshot.objects.using(alias).exclude(
        credential_ref_identity_hash=EMPTY_SHA256,
    ).update(credential_version=LEGACY_VERSION)


def reject_versioned_reverse(apps, schema_editor):
    alias = schema_editor.connection.alias
    for model_name in ("PublicationTarget", "PublicationTargetSnapshot"):
        model = apps.get_model("publishing", model_name)
        if model.objects.using(alias).exclude(credential_version="").exists():
            raise IrreversibleError(
                "T023 credential versions must be remediated before reverse migration"
            )


class Migration(migrations.Migration):
    dependencies = [("publishing", "0012_published_asset_snapshots")]

    operations = [
        migrations.RunPython(
            remove_existing_publishing_guards,
            reverse_code=install_existing_publishing_guards,
        ),
        migrations.AddField(
            model_name="publicationtarget",
            name="credential_version",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="publicationtargetsnapshot",
            name="credential_version",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.RunPython(
            backfill_credential_versions,
            reverse_code=reject_versioned_reverse,
        ),
        migrations.RunPython(
            install_existing_publishing_guards,
            reverse_code=remove_existing_publishing_guards,
        ),
    ]
