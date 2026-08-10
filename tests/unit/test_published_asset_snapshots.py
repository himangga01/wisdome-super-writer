import json
import uuid
from datetime import timedelta
from unittest import TestCase

from django.db.models import PROTECT
from django.test import TestCase as DjangoTestCase
from django.utils import timezone

from apps.publishing import models as publishing_models
from apps.publishing import services
from wisdome_writer.domain.hashing import sha256_hex


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


class PublishedAssetCohortServiceTests(DjangoTestCase):
    """Break caught: exact preview replay creates a different or mutable cohort."""

    @classmethod
    def setUpTestData(cls):
        from tests.unit.test_publication_approval_service import (
            ApprovalDecisionDatabaseTests,
        )

        ApprovalDecisionDatabaseTests.setUpTestData()
        cls.fixture = ApprovalDecisionDatabaseTests.fixture
        cls.revision = cls.fixture.intent.article_revision

    def _evidence_revision(self, *, with_visualization=False):
        from apps.collection.models import (
            RunSourceItem,
            SourceCollectionAttempt,
            SourceItem,
        )
        from apps.editorial.models import (
            ArticleRevision,
            VisualizationRender,
            VisualPlacement,
        )
        from apps.evidence.models import EvidenceAsset
        from apps.topics.models import (
            SourceDefinition,
            SourceDefinitionSnapshot,
            SourceRegistryMembership,
        )

        now = timezone.now()
        unique = uuid.uuid4().hex
        source = SourceDefinition.objects.create(
            topic_code="housing_subscription",
            key=f"t022-{unique}",
            display_name="T022 source",
            publisher="공식기관",
            owner_name="공식기관",
            editorial_control_name="공식기관",
            base_url="https://source.example/",
            authority_tier="primary_official",
            access_method="public_file",
            independence_group=f"official-{unique}",
            adapter_key="tests.t022",
            external_config={},
            allowed_mime_types=["image/png"],
            default_rights_status="allowed",
            license_url="https://source.example/license",
        )
        source_snapshot = SourceDefinitionSnapshot.objects.create(
            source=source,
            topic_code=source.topic_code,
            version=1,
            state="approved",
            config={},
            config_hash="1" * 64,
            frozen_config={},
            frozen_config_hash="1" * 64,
            independence_group=source.independence_group,
            owner_name=source.owner_name,
            editorial_control_name=source.editorial_control_name,
            approved_by=self.fixture.user,
            approved_at=now,
        )
        attempt = SourceCollectionAttempt.objects.create(
            run=self.revision.origin_run,
            source_snapshot=source_snapshot,
            adapter_name="tests.t022",
            adapter_version="v1",
            state="succeeded",
        )
        SourceRegistryMembership.objects.create(
            registry=self.revision.origin_run.source_registry,
            source_definition=source,
            source_snapshot=source_snapshot,
            enabled=True,
            display_order=0,
        )
        source_item = SourceItem.objects.create(
            source=source,
            external_id=f"image-{unique}",
            canonical_url=f"https://source.example/{unique}.png",
            title="공식 배치도",
            publisher=source.publisher,
            published_at=now - timedelta(days=1),
            modified_at=now - timedelta(hours=1),
            first_collected_at=now,
            content_hash="2" * 64,
            source_version_hash="3" * 64,
            source_version_schema="source-item-v1",
            metadata={"contentType": "image/png"},
            attachments=[{"url": f"https://source.example/{unique}.png"}],
            status="active",
        )
        run_source_item = RunSourceItem.objects.create(
            run=self.revision.origin_run,
            collection_attempt=attempt,
            source_item=source_item,
            source_snapshot=source_snapshot,
        )
        locator = {"x": 0, "y": 0, "width": 1200, "height": 800}
        evidence = EvidenceAsset.objects.create(
            source_item=source_item,
            origin_run_source_item=run_source_item,
            derivation_type="raw",
            raw_input_fingerprint="4" * 64,
            kind="image",
            locator_type="image_region",
            locator=locator,
            object_key=f"evidence/{unique}.png",
            object_version="version-1",
            mime_type="image/png",
            byte_size=1024,
            checksum="5" * 64,
            evidence_content_hash="6" * 64,
            review_subject_hash="7" * 64,
            rights_status="allowed",
            rights_basis_url="https://source.example/license",
            attribution_text="공식기관 제공",
            alt_text="공식 배치도",
            review_state="passed",
            publishable=True,
        )
        placement_id = uuid.uuid4()
        visual = {
            "blockId": "facts-1",
            "evidenceId": str(evidence.id),
            "visualizationId": None,
            "rightsStatus": evidence.rights_status,
            "rightsBasisUrl": evidence.rights_basis_url,
            "attributionText": evidence.attribution_text,
            "altText": evidence.alt_text,
            "caption": "공식 배치도다. [S1]",
            "captionClaimMarker": "S1",
            "locator": locator,
            "renderProvenance": None,
        }
        placement_material = {
            "schemaVersion": "editorial-visual-placement-v1",
            **visual,
            "displayOrder": 0,
        }
        presentation_hash = sha256_hex(placement_material)
        visual_manifest = [
            {
                "placementId": str(placement_id),
                **placement_material,
                "presentationHash": presentation_hash,
            }
        ]
        visualization_id = uuid.uuid4() if with_visualization else None
        visualization_placement_id = uuid.uuid4() if with_visualization else None
        visualization_transform = None
        visualization_input_manifest_hash = None
        visualization_transform_hash = None
        if with_visualization:
            visualization_input_manifest_hash = sha256_hex([str(evidence.id)])
            visualization_transform = {
                "schemaVersion": "visualization-transform-v1",
                "inputEvidenceIds": [str(evidence.id)],
                "outputMimeType": "image/png",
                "outputByteSize": 2048,
                "rendererManifestHash": "e" * 64,
                "chartType": "bar",
            }
            visualization_transform_hash = sha256_hex(visualization_transform)
            visualization_visual = {
                "blockId": "visual-1",
                "evidenceId": None,
                "visualizationId": str(visualization_id),
                "rightsStatus": "allowed",
                "rightsBasisUrl": "https://source.example/license",
                "attributionText": "공식기관 자료를 기반으로 제작",
                "altText": "공식 수치를 시각화한 막대그래프",
                "caption": "공식 수치 비교다. [S1]",
                "captionClaimMarker": "S1",
                "locator": {
                    "inputEvidenceIds": [str(evidence.id)],
                    "chartType": "bar",
                },
                "renderProvenance": {
                    "objectKey": f"visualizations/{unique}.png",
                    "objectVersion": "visual-version-1",
                    "checksum": "f" * 64,
                    "inputManifestHash": visualization_input_manifest_hash,
                    "transformHash": visualization_transform_hash,
                },
            }
            visualization_material = {
                "schemaVersion": "editorial-visual-placement-v1",
                **visualization_visual,
                "displayOrder": 1,
            }
            visualization_presentation_hash = sha256_hex(
                visualization_material
            )
            visual_manifest.append(
                {
                    "placementId": str(visualization_placement_id),
                    **visualization_material,
                    "presentationHash": visualization_presentation_hash,
                }
            )
        evidence_manifest = [
            {
                "evidenceId": str(evidence.id),
                "sourceItemId": str(source_item.id),
                "runSourceItemId": str(run_source_item.id),
                "selectionState": "selected",
                "contentHash": evidence.evidence_content_hash,
                "checksum": evidence.checksum,
                "reviewSubjectHash": evidence.review_subject_hash,
                "publishable": True,
                "derivationType": evidence.derivation_type,
                "kind": evidence.kind,
                "locatorType": evidence.locator_type,
                "locator": locator,
                "rightsStatus": evidence.rights_status,
                "rightsBasisUrl": evidence.rights_basis_url,
                "attributionText": evidence.attribution_text,
                "altText": evidence.alt_text,
                "sourceTitle": source_item.title,
                "sourceUrl": source_item.canonical_url,
                "publisher": source_item.publisher,
                "sourceStatus": source_item.status,
                "sourceVersionHash": source_item.source_version_hash,
                "sourceContentHash": source_item.content_hash,
                "publishedAt": source_item.published_at.isoformat(),
                "modifiedAt": source_item.modified_at.isoformat(),
                "retrievedAt": source_item.first_collected_at.isoformat(),
                "sourceText": "공개 manifest에 복사되면 안 되는 원문",
            }
        ]
        revision = ArticleRevision.objects.create(
            article=self.revision.article,
            origin_run=self.revision.origin_run,
            revision_no=2,
            base_revision=self.revision,
            editorial_policy_snapshot=self.revision.editorial_policy_snapshot,
            editorial_policy_version=self.revision.editorial_policy_version,
            editorial_policy_hash=self.revision.editorial_policy_hash,
            verification_manifest=self.revision.verification_manifest,
            verification_manifest_hash=self.revision.verification_manifest_hash,
            evidence_manifest=evidence_manifest,
            evidence_manifest_hash=sha256_hex(evidence_manifest),
            exclusion_manifest=[],
            exclusion_manifest_hash=sha256_hex([]),
            visual_manifest=visual_manifest,
            visual_manifest_hash=sha256_hex(visual_manifest),
            title="시각자료가 있는 기사",
            summary="요약",
            body_markdown="본문",
            body_blocks=[{"id": "facts-1", "type": "fact", "content": "본문"}],
            claim_bindings=[],
            content_hash="8" * 64,
            input_manifest_hash="9" * 64,
            claim_manifest_hash="a" * 64,
            quality_manifest_hash="b" * 64,
            claim_graph_state="queued",
            quality_gate_manifest_hash="c" * 64,
            quality_report_hash="d" * 64,
            quality_state="pending",
        )
        placement = VisualPlacement.objects.create(
            id=placement_id,
            revision=revision,
            block_id="facts-1",
            source_evidence=evidence,
            display_order=0,
            locator_snapshot=locator,
            rights_status_snapshot=evidence.rights_status,
            rights_basis_url_snapshot=evidence.rights_basis_url,
            attribution_snapshot=evidence.attribution_text,
            alt_text_snapshot=evidence.alt_text,
            caption="공식 배치도다. [S1]",
            caption_claim_marker="S1",
            presentation_hash=presentation_hash,
        )
        if not with_visualization:
            return revision, placement, evidence, source_item
        visualization = VisualizationRender.objects.create(
            id=visualization_id,
            revision=revision,
            kind="bar_chart",
            title="공식 수치 비교",
            transform_spec=visualization_transform,
            input_manifest_hash=visualization_input_manifest_hash,
            object_key=f"visualizations/{unique}.png",
            object_version="visual-version-1",
            checksum="f" * 64,
            alt_text="공식 수치를 시각화한 막대그래프",
            state="succeeded",
        )
        visualization_placement = VisualPlacement.objects.create(
            id=visualization_placement_id,
            revision=revision,
            block_id="visual-1",
            visualization=visualization,
            display_order=1,
            locator_snapshot={
                "inputEvidenceIds": [str(evidence.id)],
                "chartType": "bar",
            },
            rights_status_snapshot="allowed",
            rights_basis_url_snapshot="https://source.example/license",
            attribution_snapshot="공식기관 자료를 기반으로 제작",
            alt_text_snapshot="공식 수치를 시각화한 막대그래프",
            caption="공식 수치 비교다. [S1]",
            caption_claim_marker="S1",
            render_object_key_snapshot=visualization.object_key,
            render_object_version_snapshot=visualization.object_version,
            render_checksum_snapshot=visualization.checksum,
            render_input_manifest_hash_snapshot=visualization.input_manifest_hash,
            render_transform_hash_snapshot=visualization_transform_hash,
            presentation_hash=visualization_presentation_hash,
        )
        return (
            revision,
            placement,
            evidence,
            source_item,
            visualization_placement,
            visualization,
        )

    def _intent_for_revision(self, revision):
        root = self.fixture.intent
        return publishing_models.PublicationIntent.objects.create(
            article_id=root.article_id,
            article_revision=revision,
            revision_no=revision.revision_no,
            revision_content_hash=revision.content_hash,
            target_snapshot_refs=root.target_snapshot_refs,
            target_commands=root.target_commands,
            target_snapshot_manifest_hash=root.target_snapshot_manifest_hash,
            approval_mode=root.approval_mode,
            input_evidence_manifest_hash=revision.evidence_manifest_hash,
            generation_pipeline_manifest_hash=root.generation_pipeline_manifest_hash,
            quality_gate_manifest_hash=revision.quality_gate_manifest_hash,
            quality_report_hash=revision.quality_report_hash,
            supersedes_intent=root,
            intent_hash="0" * 64,
            request_key=f"t022-intent-{revision.id}",
            request_hash="1" * 64,
            request_hash_version="publication-intent-request-v1",
            created_by=self.fixture.user,
        )

    def test_empty_revision_cohort_is_canonical_and_idempotent(self):
        self.assertTrue(hasattr(services, "freeze_revision_asset_cohort"))
        first = services.freeze_revision_asset_cohort(revision=self.revision)
        second = services.freeze_revision_asset_cohort(revision=self.revision)

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.schema_version, "published-assets-v1")
        self.assertEqual(first.material_state, "current")
        self.assertEqual(first.item_count, 0)
        self.assertEqual(first.manifest, [])
        self.assertEqual(
            first.manifest_hash,
            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba87"
            "3c2f11161202b945",
        )

    def test_evidence_placement_freezes_minimal_snapshot_and_channel_manifest(self):
        revision, placement, evidence, source_item = self._evidence_revision()

        cohort = services.freeze_revision_asset_cohort(revision=revision)
        snapshot = cohort.evidence_snapshots.get()
        channel_manifest = services.build_channel_media_manifest(
            cohort=cohort,
            channel="wordpress",
        )

        self.assertEqual(cohort.item_count, 1)
        self.assertEqual(snapshot.visual_placement_id, placement.id)
        self.assertEqual(snapshot.evidence_id, evidence.id)
        self.assertEqual(snapshot.source_item_id, source_item.id)
        self.assertEqual(snapshot.source_version_hash, source_item.source_version_hash)
        self.assertEqual(snapshot.object_version, "version-1")
        self.assertEqual(snapshot.presentation_hash, placement.presentation_hash)
        self.assertEqual(channel_manifest[0]["cohortId"], str(cohort.id))
        self.assertEqual(channel_manifest[0]["snapshotId"], str(snapshot.id))
        self.assertEqual(channel_manifest[0]["usage"], "inline")
        self.assertEqual(channel_manifest[0]["mimeType"], "image/png")
        self.assertNotIn("sourceText", json.dumps(channel_manifest))
        self.assertNotIn("sourceUrl", json.dumps(channel_manifest))

    def test_visualization_snapshot_freezes_stable_evidence_input_order(self):
        (
            revision,
            _evidence_placement,
            _evidence,
            _source_item,
            visualization_placement,
            visualization,
        ) = self._evidence_revision(with_visualization=True)

        cohort = services.freeze_revision_asset_cohort(revision=revision)
        visualization_snapshot = cohort.visualization_snapshots.get()
        input_link = visualization_snapshot.input_links.get()
        channel_manifest = services.build_channel_media_manifest(
            cohort=cohort,
            channel="blogger",
        )

        self.assertEqual(cohort.item_count, 2)
        self.assertEqual(
            visualization_snapshot.visual_placement_id,
            visualization_placement.id,
        )
        self.assertEqual(visualization_snapshot.visualization_id, visualization.id)
        self.assertEqual(visualization_snapshot.mime_type, "image/png")
        self.assertEqual(visualization_snapshot.byte_size, 2048)
        self.assertEqual(input_link.display_order, 0)
        self.assertEqual(
            input_link.evidence_snapshot.evidence_id,
            _evidence.id,
        )
        self.assertEqual(channel_manifest[1]["snapshotKind"], "visualization")

    def test_preview_render_binds_the_frozen_channel_media_manifest(self):
        revision, _placement, _evidence, _source_item = self._evidence_revision()
        intent = self._intent_for_revision(revision)

        render = services._create_preview_render(
            intent,
            revision,
            self.fixture.target,
        )

        cohort = revision.published_asset_cohort
        self.assertEqual(len(render.media_manifest), 1)
        self.assertEqual(render.media_manifest[0]["cohortId"], str(cohort.id))
        self.assertEqual(
            render.media_manifest[0]["cohortManifestHash"],
            cohort.manifest_hash,
        )
        self.assertEqual(render.media_manifest[0]["channel"], "wordpress")
