from __future__ import annotations

from types import SimpleNamespace
import importlib
from unittest import TestCase as UnitTestCase
from unittest.mock import patch
from unittest.mock import MagicMock

from django.test import SimpleTestCase
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from apps.publishing import services
from apps.publishing import tasks as publishing_tasks
from apps.publishing.models import AutoPublishValidation, TargetCanaryRun
from apps.evidence import profiles as evidence_profiles
from apps.topics import services as topic_services
from wisdome_writer.infrastructure.event_routes import (
    EVENT_PAYLOAD_SCHEMAS,
    EVENT_ROUTES,
)
from wisdome_writer.api.openapi import _compile_schema, load_openapi_contract
from wisdome_writer.domain.errors import InvalidInput


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
TARGET_ID = "00000000-0000-0000-0000-000000000101"
SNAPSHOT_ID = "00000000-0000-0000-0000-000000000102"
REGISTRY_ID = "00000000-0000-0000-0000-000000000103"
POLICY_ID = "00000000-0000-0000-0000-000000000104"
PROFILE_ID = "00000000-0000-0000-0000-000000000105"
CANARY_ID = "00000000-0000-0000-0000-000000000106"


class AutoPublishServerMaterialTests(SimpleTestCase):
    def _target(self):
        return SimpleNamespace(
            id=TARGET_ID,
            current_snapshot_id=SNAPSHOT_ID,
            current_snapshot_version=7,
            current_config_hash=SHA_A,
            channel="wordpress",
            role="primary_canonical",
            environment="test",
            credential_version="secret-v4",
            publisher_contract_version="publisher-v1",
            publisher_adapter_manifest_hash=services.ADAPTER_MANIFESTS["wordpress"],
            connection_state="verified",
            preflight_state="passed",
            canary_state="passed",
            pilot_state="not_run",
            canary_target_id=None,
        )

    def test_validation_request_rejects_client_supplied_material(self):
        body = {
            "topic": "housing_subscription",
            "requestKey": "validation-request-1",
            "reason": "관리자 검증 요청",
            "registryManifestHash": SHA_A,
        }
        with self.assertRaises(InvalidInput):
            services._normalize_auto_publish_validation_request(body)

    def test_server_builder_uses_only_approved_runtime_material(self):
        target = self._target()
        registry = {
            "snapshotId": REGISTRY_ID,
            "version": 3,
            "manifestHash": SHA_A,
            "sourceAdapterManifestHash": SHA_B,
        }
        profiles = {
            "refs": [
                {
                    "profileId": PROFILE_ID,
                    "profileKey": "native-pdf",
                    "profileVersion": "1.1.0",
                    "materialHash": SHA_B,
                    "decisionId": POLICY_ID,
                    "decisionVersion": 2,
                    "verificationReportHash": SHA_C,
                }
            ],
            "manifestHash": SHA_C,
        }
        topic_policy = {
            "id": POLICY_ID,
            "version": 9,
            "policyHash": SHA_A,
        }
        editorial = SimpleNamespace(
            id=POLICY_ID,
            policy_key="housing-editorial",
            policy_version="4",
            material_hash=SHA_B,
            release_document_hash=SHA_A,
            config_hash=SHA_C,
            implementation_manifest_hash=SHA_B,
            document={"checks": [{"code": "gate", "version": "1"}]},
        )
        canary = {
            "kind": "test_canary",
            "runId": CANARY_ID,
            "targetSnapshotId": SNAPSHOT_ID,
            "policyVersion": services.AUTO_PUBLISH_CANARY_POLICY_VERSION,
            "reportHash": SHA_C,
            "cleanupRefs": [{"kind": "post", "remoteId": "p1", "state": "deleted"}],
        }

        with (
            patch.object(services, "approved_registry_material", return_value=registry),
            patch.object(services, "approved_profile_refs", return_value=profiles),
            patch.object(services, "approved_topic_policy_material", return_value=topic_policy),
            patch.object(services, "resolve_editorial_policy_snapshot", return_value=editorial),
            patch.object(services, "_auto_publish_validation_evidence", return_value=canary),
        ):
            material = services.build_auto_publish_validation_material(
                target,
                "housing_subscription",
            )

        self.assertEqual(material["schemaVersion"], "auto-publish-validation-material-v2")
        self.assertEqual(material["target"]["credentialVersion"], "secret-v4")
        self.assertEqual(material["registry"], registry)
        self.assertEqual(material["profiles"], profiles["refs"])
        self.assertEqual(material["profileManifestHash"], SHA_C)
        self.assertEqual(material["topicPolicy"], topic_policy)
        self.assertEqual(material["editorialPolicy"]["snapshotId"], POLICY_ID)
        self.assertEqual(material["validationEvidence"], canary)
        self.assertEqual(
            material["publisher"]["adapterManifestHash"],
            services.ADAPTER_MANIFESTS["wordpress"],
        )
        implementation = material["publisher"]["implementationManifest"]
        self.assertEqual(
            implementation["schemaVersion"],
            "publisher-implementation-manifest-v1",
        )
        self.assertEqual(implementation["channel"], "wordpress")
        self.assertEqual(
            material["publisher"]["implementationManifestHash"],
            services._auto_publish_material_hash(implementation),
        )
        self.assertIn(
            "src/adapters/publishers/wordpress/client.py",
            {row["path"] for row in implementation["implementationFiles"]},
        )

    def test_live_gate_rebuilds_server_material(self):
        validation = SimpleNamespace(
            target=self._target(),
            topic_code="housing_subscription",
            material_document={"schemaVersion": "auto-publish-validation-material-v2", "value": 1},
            material_hash=services._auto_publish_material_hash(
                {"schemaVersion": "auto-publish-validation-material-v2", "value": 1}
            ),
        )
        with patch.object(
            services,
            "build_auto_publish_validation_material",
            return_value={"schemaVersion": "auto-publish-validation-material-v2", "value": 2},
        ):
            self.assertFalse(services._auto_publish_validation_is_current(validation))

    def test_canary_evidence_requires_cleanup_proof(self):
        run = SimpleNamespace(
            id=CANARY_ID,
            target_snapshot_id=SNAPSHOT_ID,
            result_target_snapshot_id=None,
            policy_version=services.AUTO_PUBLISH_CANARY_POLICY_VERSION,
            report_hash=SHA_A,
            state="passed",
            stage_results=[
                {"code": code, "passed": True}
                for code in services.REQUIRED_CANARY_STAGE_CODES
            ],
            remote_cleanup_refs=[
                {"kind": "post", "remoteId": "p1", "state": "cleanup_pending"}
            ],
        )
        with self.assertRaises(InvalidInput):
            services._canary_run_material(run)

    def test_registry_material_hashes_exact_approved_adapter_refs(self):
        snapshot = SimpleNamespace(
            id=SNAPSHOT_ID,
            version=4,
            frozen_config_hash=SHA_A,
            frozen_config={
                "adapterKey": "housing_applyhome",
                "adapterVersion": "v3",
                "adapterImplementationManifestHash": SHA_B,
            },
        )
        membership = SimpleNamespace(
            enabled=True,
            source_definition_id=TARGET_ID,
            source_snapshot=snapshot,
        )
        registry = SimpleNamespace(
            id=REGISTRY_ID,
            version=2,
            manifest_hash=SHA_C,
            memberships=SimpleNamespace(all=lambda: [membership]),
        )
        with (
            patch.object(topic_services, "current_registry", return_value=registry),
            patch.object(topic_services, "_is_verifiable_source_snapshot", return_value=True),
        ):
            material = topic_services.approved_registry_material("housing_subscription")
        self.assertEqual(material["snapshotId"], REGISTRY_ID)
        self.assertEqual(material["sourceAdapters"][0]["adapterVersion"], "v3")
        self.assertEqual(
            material["sourceAdapterManifestHash"],
            topic_services._hash(material["sourceAdapters"]),
        )

    def test_profile_refs_require_exact_approved_release_and_decision(self):
        decision = SimpleNamespace(
            id=POLICY_ID,
            decision="approved",
            expected_material_hash=SHA_A,
            version=1,
            verification_report_hash=SHA_B,
        )
        profile = SimpleNamespace(
            id=PROFILE_ID,
            profile_key="native-pdf-v1",
            profile_version="1.1.0",
            engine="native_pdf",
            extractor_version="1.1.0",
            package_version="1.28.0",
            runtime_version=None,
            pipeline_name=None,
            implementation_manifest_hash=SHA_C,
            config={"python_runtime_version": "3.12.10"},
            config_hash=SHA_B,
            validation_mode=None,
            calibration_profile_key=None,
            calibration_profile_version=None,
            calibration_manifest_object_key=None,
            calibration_manifest_object_version=None,
            calibration_profile_hash=None,
            model_manifest=None,
            model_manifest_hash=None,
            profile_material_hash=SHA_A,
            verification_report_hash=SHA_B,
            latest_decision=decision,
            decision_version=1,
        )
        release = {
            field: getattr(profile, field)
            for field in (
                "profile_key", "profile_version", "engine", "extractor_version",
                "package_version", "runtime_version", "pipeline_name",
                "implementation_manifest_hash", "config", "config_hash",
                "validation_mode", "calibration_profile_key",
                "calibration_profile_version", "calibration_manifest_object_key",
                "calibration_manifest_object_version", "calibration_profile_hash",
                "model_manifest", "model_manifest_hash", "profile_material_hash",
            )
        }
        queryset = MagicMock()
        queryset.select_related.return_value = queryset
        queryset.filter.return_value = queryset
        queryset.order_by.return_value = [profile]
        with (
            patch.object(evidence_profiles.ExtractionProfileSnapshot.objects, "using", return_value=queryset),
            patch.object(evidence_profiles, "release_profile_snapshot_values", return_value=release),
        ):
            material = evidence_profiles.approved_profile_refs()
        self.assertEqual(material["refs"][0]["profileId"], PROFILE_ID)
        self.assertEqual(
            material["manifestHash"],
            evidence_profiles.canonical_hash(material["refs"]),
        )

    def test_material_change_stales_validation_and_disables_target(self):
        validation = SimpleNamespace(
            id=REGISTRY_ID,
            target_id=TARGET_ID,
            status="passed",
            invalidated_at=None,
            invalidation_reason="",
            save=MagicMock(),
        )
        target = SimpleNamespace(
            id=TARGET_ID,
            auto_publish_enabled=True,
            save=MagicMock(),
        )
        validations = MagicMock()
        validations.filter.return_value = validations
        validations.values_list.return_value = [TARGET_ID]
        validations.select_for_update.return_value = validations
        validations.order_by.return_value = [validation]
        targets = MagicMock()
        targets.select_for_update.return_value = targets
        targets.filter.return_value = targets
        targets.order_by.return_value = [target]
        with (
            patch.object(services.AutoPublishValidation.objects, "filter", return_value=validations),
            patch.object(services.PublicationTarget.objects, "select_for_update", return_value=targets),
            patch.object(services, "_lock_target_intent_fences"),
            patch.object(services, "_lock_open_target_intents", return_value=[]),
        ):
            count = services.invalidate_auto_publish_server_material.__wrapped__(
                topic_code="housing_subscription",
                reason="source_registry_approved",
            )
        self.assertEqual(count, 1)
        self.assertEqual(validation.status, "stale")
        self.assertEqual(validation.invalidation_reason, "source_registry_approved")
        self.assertFalse(target.auto_publish_enabled)

    def test_canary_worker_persists_exact_stages_and_deleted_remote_refs(self):
        run = SimpleNamespace(
            id=SimpleNamespace(hex="1" * 32),
            target_id=TARGET_ID,
            target=SimpleNamespace(
                role="secondary_distribution",
                channel="blogger",
                base_url="https://example.blogspot.com/",
            ),
            target_snapshot_id=SNAPSHOT_ID,
            target_snapshot=SimpleNamespace(
                config_hash=SHA_A,
                publisher_contract_version="publisher-v1",
                publisher_adapter_manifest_hash=services.ADAPTER_MANIFESTS["blogger"],
            ),
        )
        fence = SimpleNamespace()
        adapter = MagicMock()

        def execute(command):
            if command.action == "create":
                return SimpleNamespace(status="succeeded", remote_post_id="post-1")
            return SimpleNamespace(status="succeeded", remote_post_id="post-1")

        adapter.execute.side_effect = execute
        adapter.fetch_remote_state.return_value = SimpleNamespace(
            status="succeeded",
            remote_state="published",
            remote_url="https://example.blogspot.com/p/post-1.html",
        )
        adapter.delete_post.return_value = SimpleNamespace(status="succeeded")
        persisted = SimpleNamespace(state="passed", report_hash=SHA_B)
        audit_context = SimpleNamespace(correlation_id="00000000-0000-0000-0000-000000000199")
        with (
            patch.object(publishing_tasks, "_worker_audit_context", return_value=audit_context),
            patch.object(publishing_tasks, "begin_canary_run", return_value=(run, fence)),
            patch.object(publishing_tasks, "publisher_for_target", return_value=adapter),
            patch.object(publishing_tasks, "_kill_switch_enabled", return_value=False),
            patch.object(
                publishing_tasks,
                "persist_canary_run_result",
                return_value=persisted,
            ) as persist,
        ):
            result = publishing_tasks.run_target_canary.run(CANARY_ID)
        self.assertEqual(result["state"], "passed")
        kwargs = persist.call_args.kwargs
        self.assertEqual(
            {row["code"] for row in kwargs["stages"]},
            services.REQUIRED_CANARY_STAGE_CODES,
        )
        self.assertEqual(
            kwargs["remote_cleanup_refs"],
            [{"kind": "post", "remoteId": "post-1", "state": "deleted"}],
        )


class AutoPublishServerMaterialModelContractTests(UnitTestCase):
    def test_validation_and_canary_models_expose_server_material_identity(self):
        self.assertEqual(
            AutoPublishValidation._meta.get_field("material_version").default,
            "auto-publish-validation-material-v2",
        )
        self.assertEqual(
            AutoPublishValidation._meta.get_field("material_hash").unique,
            False,
        )
        self.assertEqual(
            TargetCanaryRun._meta.get_field("result_target_snapshot").remote_field.on_delete.__name__,
            "PROTECT",
        )

    def test_legacy_backfill_is_explicitly_quarantined(self):
        migration = importlib.import_module(
            "apps.publishing.migrations.0015_auto_publish_server_material"
        )
        self.assertEqual(migration.LEGACY_VERSION, "legacy-client-material-v1")
        self.assertEqual(
            migration.SERVER_VERSION,
            "auto-publish-validation-material-v2",
        )

    def test_registry_and_profile_decisions_route_to_stale_projection(self):
        profile_route = EVENT_ROUTES[("evidence.profile_decided", 1)]
        registry_route = EVENT_ROUTES[("topics.registry_decided", 1)]
        self.assertEqual(
            profile_route.task_name,
            "apps.publishing.tasks.invalidate_auto_publish_for_profile",
        )
        self.assertEqual(
            registry_route.task_name,
            "apps.publishing.tasks.invalidate_auto_publish_for_registry",
        )
        self.assertEqual(
            EVENT_PAYLOAD_SCHEMAS[("topics.registry_decided", 1)].required,
            frozenset(
                {
                    "topic_code",
                    "registry_id",
                    "decision_id",
                    "decision",
                    "manifest_hash",
                }
            ),
        )

    def test_openapi_accepts_only_server_derived_validation_request(self):
        contract = load_openapi_contract()
        _, validator = _compile_schema(
            contract["components"]["schemas"]["AutoPublishValidationCreateRequest"],
            document=contract,
            subject="AutoPublishValidationCreateRequest",
        )
        body = {
            "topic": "housing_subscription",
            "requestKey": "validation-request-1",
            "reason": "관리자 검증 요청",
        }
        validator.validate(body)
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate({**body, "registryManifestHash": SHA_A})

    def test_openapi_canary_policy_version_is_server_owned(self):
        contract = load_openapi_contract()
        _, validator = _compile_schema(
            contract["components"]["schemas"]["CanaryRequest"],
            document=contract,
            subject="CanaryRequest",
        )
        body = {
            "confirmIsolatedTestTarget": True,
            "requestKey": "canary-request-1",
            "reason": "격리 canary 검증",
        }
        validator.validate(body)
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate({**body, "policyVersion": "client-v9"})
