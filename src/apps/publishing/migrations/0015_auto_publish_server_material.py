from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
import django.db.models.deletion


SERVER_VERSION = "auto-publish-validation-material-v2"
LEGACY_VERSION = "legacy-client-material-v1"


def backfill_legacy_material(apps, schema_editor):
    Validation = apps.get_model("publishing", "AutoPublishValidation")
    for row in Validation.objects.using(schema_editor.connection.alias).all().iterator():
        row.material_version = LEGACY_VERSION
        row.material_document = {
            "targetId": str(row.target_id),
            "topic": row.topic_code,
            "targetSnapshotId": str(row.target_snapshot_id),
            "targetConfigHash": row.target_config_hash,
            "sourceRegistrySnapshotId": str(row.source_registry_snapshot_id),
            "registryManifestHash": row.registry_manifest_hash,
            "sourceAdapterManifestHash": row.source_adapter_manifest_hash,
            "extractionProfileManifestHash": row.extraction_profile_manifest_hash,
            "generationPipelineManifestHash": row.generation_pipeline_manifest_hash,
            "topicPolicyVersion": row.topic_policy_version,
            "editorialPolicyHash": row.editorial_policy_hash,
            "qualityGateManifestHash": row.quality_gate_manifest_hash,
            "renderContractVersion": row.render_contract_version,
            "channelContractVersion": row.channel_contract_version,
            "publisherAdapterManifestHash": row.publisher_adapter_manifest_hash,
            "testReportObjectKey": row.test_report_object_key,
            "testReportObjectVersion": row.test_report_object_version,
            "testReportHash": row.test_report_hash,
        }
        row.save(update_fields=("material_version", "material_document"))


def require_unique_material_for_reverse(apps, schema_editor):
    Validation = apps.get_model("publishing", "AutoPublishValidation")
    duplicate = (
        Validation.objects.using(schema_editor.connection.alias)
        .values("material_hash")
        .annotate(count=models.Count("id"))
        .filter(count__gt=1)
        .exists()
    )
    if duplicate:
        raise IrreversibleError(
            "Cannot restore the legacy unique material hash after multiple exact requests."
        )


class Migration(migrations.Migration):
    dependencies = [("publishing", "0014_publication_dependency")]

    operations = [
        migrations.AddField(
            model_name="targetcanaryrun",
            name="result_target_snapshot",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="completed_canary_runs",
                to="publishing.publicationtargetsnapshot",
            ),
        ),
        migrations.AddField(
            model_name="autopublishvalidation",
            name="material_document",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="autopublishvalidation",
            name="material_version",
            field=models.CharField(default=SERVER_VERSION, max_length=100),
        ),
        migrations.AlterField(
            model_name="autopublishvalidation",
            name="material_hash",
            field=models.CharField(db_index=True, max_length=64),
        ),
        migrations.RunPython(
            backfill_legacy_material,
            require_unique_material_for_reverse,
        ),
    ]
