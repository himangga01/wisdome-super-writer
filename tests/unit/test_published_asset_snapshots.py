from unittest import TestCase

from django.db.models import PROTECT

from apps.publishing import models as publishing_models


class PublishedAssetSnapshotModelContractTests(TestCase):
    """Break caught: a preview can no longer freeze a durable asset cohort."""

    def test_publication_asset_models_expose_frozen_lineage(self):
        required_models = {
            "PublishedAssetCohort",
            "PublishedEvidenceSnapshot",
            "PublishedVisualizationSnapshot",
            "PublishedVisualizationInput",
        }
        self.assertLessEqual(required_models, set(dir(publishing_models)))

        cohort = publishing_models.PublishedAssetCohort
        self.assertLessEqual(
            {
                "revision",
                "schema_version",
                "material_state",
                "item_count",
                "manifest",
                "manifest_hash",
            },
            {field.name for field in cohort._meta.get_fields()},
        )
        self.assertTrue(cohort._meta.get_field("revision").unique)
        self.assertIs(
            cohort._meta.get_field("revision").remote_field.on_delete,
            PROTECT,
        )

        evidence = publishing_models.PublishedEvidenceSnapshot
        self.assertLessEqual(
            {
                "cohort",
                "visual_placement",
                "evidence",
                "source_item_id",
                "source_version_hash",
                "evidence_content_hash",
                "asset_checksum",
                "object_key",
                "object_version",
                "mime_type",
                "byte_size",
                "locator_snapshot",
                "rights_status_snapshot",
                "rights_basis_url_snapshot",
                "attribution_snapshot",
                "alt_text_snapshot",
                "caption_snapshot",
                "presentation_hash",
            },
            {field.name for field in evidence._meta.get_fields()},
        )
        self.assertIs(
            evidence._meta.get_field("cohort").remote_field.on_delete,
            PROTECT,
        )
        self.assertIs(
            evidence._meta.get_field("visual_placement").remote_field.on_delete,
            PROTECT,
        )
        self.assertIs(
            evidence._meta.get_field("evidence").remote_field.on_delete,
            PROTECT,
        )

        visualization = publishing_models.PublishedVisualizationSnapshot
        self.assertLessEqual(
            {
                "cohort",
                "visual_placement",
                "visualization",
                "output_checksum",
                "object_key",
                "object_version",
                "input_manifest_hash",
                "transform_hash",
                "renderer_manifest_hash",
                "mime_type",
                "byte_size",
                "rights_status_snapshot",
                "rights_basis_url_snapshot",
                "attribution_snapshot",
                "alt_text_snapshot",
                "caption_snapshot",
                "presentation_hash",
            },
            {field.name for field in visualization._meta.get_fields()},
        )
        self.assertTrue(
            visualization._meta.get_field("visual_placement").unique
        )

        visualization_input = publishing_models.PublishedVisualizationInput
        self.assertLessEqual(
            {
                "visualization_snapshot",
                "evidence_snapshot",
                "display_order",
                "input_material_hash",
            },
            {field.name for field in visualization_input._meta.get_fields()},
        )
        self.assertIn(
            "uq_published_visualization_input_order",
            {
                constraint.name
                for constraint in visualization_input._meta.constraints
            },
        )
