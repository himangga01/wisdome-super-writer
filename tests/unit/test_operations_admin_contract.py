from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve
from jsonschema.exceptions import ValidationError

from apps.audit import api as audit_api
from apps.collection import api as collection_api
from apps.scheduling import api as scheduling_api
from wisdome_writer.api.openapi import _compile_schema, load_openapi_contract


SCHEDULE_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
TARGET_ID = "33333333-3333-4333-8333-333333333333"
PROOF_ID = "44444444-4444-4444-8444-444444444444"
USER_ID = "55555555-5555-4555-8555-555555555555"
HASH_A = "a" * 64
NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def _validator(name: str):
    contract = load_openapi_contract()
    _, validator = _compile_schema(
        contract["components"]["schemas"][name],
        document=contract,
        subject=name,
    )
    return validator


def _request(method: str, path: str, body: dict | None = None):
    factory = RequestFactory()
    request = getattr(factory, method.lower())(
        path,
        data=(json.dumps(body) if body is not None else None),
        content_type=("application/json" if body is not None else None),
    )
    request._dont_enforce_csrf_checks = True
    request.correlation_id = uuid.UUID("66666666-6666-4666-8666-666666666666")
    request.user = SimpleNamespace(
        pk=uuid.UUID(USER_ID),
        is_authenticated=True,
        is_active=True,
        is_staff=True,
    )
    return request


def _schedule_row(*, version: int = 4):
    return SimpleNamespace(
        id=uuid.UUID(SCHEDULE_ID),
        version=version,
        name="주택 청약 점검",
        topic_code="housing_subscription",
        cron_expression="0 */2 * * *",
        timezone="Asia/Seoul",
        window_minutes=180,
        target_ids=[TARGET_ID],
        approval_mode="manual",
        auto_publish_validation_refs=[],
        auto_publish_activation_refs=[],
        overlap_policy="skip",
        enabled=True,
        next_run_at=NOW,
        last_dispatched_at=None,
        updated_at=NOW,
    )


class OperationsOpenApiContractTests(SimpleTestCase):
    maxDiff = None

    def test_every_operations_endpoint_has_an_exact_operation_id_and_route(self):
        contract = load_openapi_contract()
        expected = {
            ("/schedules", "get"): "listSchedules",
            ("/schedules", "post"): "createSchedule",
            ("/schedules/{scheduleId}", "get"): "getSchedule",
            ("/schedules/{scheduleId}", "patch"): "updateSchedule",
            ("/schedules/{scheduleId}", "delete"): "disableSchedule",
            ("/operations/kill-switch", "get"): "getKillSwitch",
            ("/operations/kill-switch", "put"): "setKillSwitch",
            ("/runs", "get"): "listRuns",
            ("/runs", "post"): "createRun",
            ("/runs/{runId}", "get"): "getRun",
            ("/runs/{runId}/stop", "post"): "stopRun",
            ("/runs/{runId}/retry", "post"): "retryRun",
            ("/corrections", "get"): "listCorrections",
            ("/corrections/{correctionId}/decisions", "post"): "decideCorrection",
            ("/retention/previews", "post"): "previewRetentionBatch",
            ("/retention/batches/{retentionBatchId}", "get"): "getRetentionBatch",
            ("/retention/batches/{retentionBatchId}/items", "get"): "listRetentionBatchItems",
            ("/retention/batches/{retentionBatchId}/execute", "post"): "executeRetentionBatch",
            ("/audit-events", "get"): "listAuditEvents",
        }
        for (path, method), operation_id in expected.items():
            with self.subTest(path=path, method=method):
                self.assertEqual(
                    contract["paths"][path][method]["operationId"],
                    operation_id,
                )

        routes = {
            f"/api/v1/schedules/{SCHEDULE_ID}": "schedule-detail",
            "/api/v1/operations/kill-switch": "kill-switch",
            f"/api/v1/runs/{RUN_ID}/stop": "run-stop",
            f"/api/v1/runs/{RUN_ID}/retry": "run-retry",
            "/api/v1/corrections": "corrections",
            "/api/v1/retention/previews": "retention-preview",
            f"/api/v1/retention/batches/{RUN_ID}": "retention-batch-detail",
            f"/api/v1/retention/batches/{RUN_ID}/items": "retention-batch-items",
            f"/api/v1/retention/batches/{RUN_ID}/execute": "retention-execute",
        }
        for path, name in routes.items():
            with self.subTest(path=path):
                self.assertEqual(resolve(path).url_name, name)

    def test_operations_requests_are_closed_and_require_cas_or_reauthentication(self):
        schedule_patch = {
            "expectedVersion": 4,
            "enabled": False,
            "requestKey": "schedule-update-0001",
            "reason": "운영 일정 일시 중지",
        }
        _validator("SchedulePatch").validate(schedule_patch)
        with self.assertRaises(ValidationError):
            _validator("SchedulePatch").validate(
                {**schedule_patch, "unexpected": True}
            )

        stop = {
            "expectedState": "drafting",
            "requestKey": "run-stop-0001",
            "reauthProofId": PROOF_ID,
            "reason": "수집 결과 확인을 위한 안전 중지",
        }
        _validator("RunStopRequest").validate(stop)

        retry = {
            "scope": {"publicationAttemptId": TARGET_ID},
            "requestKey": "run-retry-0001",
            "reauthProofId": PROOF_ID,
            "reason": "실패한 발행 시도만 선택 재시도",
        }
        _validator("RunRetryRequest").validate(retry)

        retention = {
            "expectedVersion": 1,
            "previewHash": HASH_A,
            "requestKey": "retention-execute-0001",
            "reauthProofId": PROOF_ID,
            "reason": "보존 의존성과 hold를 검토했습니다",
        }
        _validator("RetentionExecuteRequest").validate(retention)

    def test_audit_contract_supports_actor_filters(self):
        params = {
            item.get("name")
            for item in load_openapi_contract()["paths"]["/audit-events"]["get"][
                "parameters"
            ]
            if item.get("name")
        }
        self.assertTrue({"actorType", "actorId", "correlationId", "action"} <= params)


class OperationsApiBehaviorTests(SimpleTestCase):
    def test_schedule_patch_passes_expected_version_to_the_locked_service(self):
        request = _request(
            "PATCH",
            f"/api/v1/schedules/{SCHEDULE_ID}",
            {
                "expectedVersion": 4,
                "enabled": False,
                "requestKey": "schedule-update-0001",
                "reason": "운영 일정 일시 중지",
            },
        )
        with patch.object(
            scheduling_api,
            "update_schedule",
            return_value=(_schedule_row(version=5), True),
        ) as update:
            response = scheduling_api.schedule_detail(
                request,
                schedule_id=SCHEDULE_ID,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.openapi_operation_id, "updateSchedule")
        self.assertEqual(update.call_args.kwargs["expected_version"], 4)
        self.assertEqual(json.loads(response.content)["version"], 5)

    def test_stop_endpoint_delegates_to_run_control_service(self):
        request = _request(
            "POST",
            f"/api/v1/runs/{RUN_ID}/stop",
            {
                "expectedState": "drafting",
                "requestKey": "run-stop-0001",
                "reauthProofId": PROOF_ID,
                "reason": "수집 결과 확인을 위한 안전 중지",
            },
        )
        decision = SimpleNamespace(
            id=uuid.UUID(SCHEDULE_ID),
            run_id=uuid.UUID(RUN_ID),
            action="stop",
            scope={},
            request_key="run-stop-0001",
            reauth_proof_id=None,
            decided_by_id=uuid.UUID(USER_ID),
            decided_at=NOW,
        )
        run = SimpleNamespace(id=uuid.UUID(RUN_ID), state="stopping")
        audit_context = object()
        with (
            patch.object(
                collection_api.AuditContext,
                "for_admin",
                return_value=audit_context,
            ) as build_audit_context,
            patch.object(
                collection_api,
                "request_run_stop",
                return_value=(decision, True),
            ) as stop,
            patch.object(collection_api, "_locked_run_for_api", return_value=run),
            patch.object(collection_api, "_run_payload", return_value={"id": RUN_ID, "state": "stopping"}),
        ):
            response = collection_api.stop_run(request, run_id=RUN_ID)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(request.openapi_operation_id, "stopRun")
        stop.assert_called_once_with(
            run_id=RUN_ID,
            expected_state="drafting",
            request_key="run-stop-0001",
            reason="수집 결과 확인을 위한 안전 중지",
            user=request.user,
            request=request,
            reauth_proof_id=PROOF_ID,
            audit_context=audit_context,
        )
        build_audit_context.assert_called_once_with(
            request=request,
            reason_code="수집 결과 확인을 위한 안전 중지",
            request_key="run-stop-0001",
        )
        self.assertEqual(json.loads(response.content)["decision"]["action"], "stop")

    def test_failed_retention_batch_uses_resume_service_and_new_authorization(self):
        request = _request(
            "POST",
            f"/api/v1/retention/batches/{RUN_ID}/execute",
            {
                "expectedVersion": 3,
                "previewHash": HASH_A,
                "requestKey": "retention-resume-0001",
                "reauthProofId": PROOF_ID,
                "reason": "실패 항목의 원인을 해소하고 다시 실행",
            },
        )
        batch = SimpleNamespace(
            id=uuid.UUID(RUN_ID),
            state="failed",
            row_version=3,
            preview_manifest_hash=HASH_A,
        )
        resumed = SimpleNamespace(
            id=batch.id,
            state="approved",
            row_version=4,
            preview_manifest_hash=HASH_A,
        )
        resumed.refresh_from_db = lambda: None
        audit_context = object()
        with (
            patch.object(audit_api.transaction, "atomic", return_value=nullcontext()),
            patch.object(
                audit_api.AuditContext,
                "for_admin",
                return_value=audit_context,
            ),
            patch.object(audit_api, "get_object_or_404", return_value=batch),
            patch.object(audit_api, "consume_reauthentication_proof") as consume,
            patch.object(
                audit_api,
                "resume_failed_retention_batch",
                return_value=resumed,
            ) as resume,
            patch.object(audit_api, "enqueue_event") as enqueue,
            patch.object(
                audit_api,
                "_retention_payload",
                return_value={"state": "approved"},
            ),
        ):
            response = audit_api.execute_retention(
                request,
                retention_batch_id=uuid.UUID(RUN_ID),
            )

        self.assertEqual(response.status_code, 202)
        consume.assert_called_once()
        resume.assert_called_once_with(
            uuid.UUID(RUN_ID),
            expected_version=3,
            expected_preview_hash=HASH_A,
            authorized_by=request.user,
            authorization_request_key="retention-resume-0001",
            authorization_reason="실패 항목의 원인을 해소하고 다시 실행",
            reauth_proof_id=PROOF_ID,
            audit_context=audit_context,
        )
        enqueue.assert_called_once()

    def test_retention_items_never_expose_the_raw_object_key(self):
        row = SimpleNamespace(
            id=uuid.UUID(SCHEDULE_ID),
            entity_type="evidence_asset",
            entity_id=uuid.UUID(TARGET_ID),
            policy_code="raw_evidence",
            object_key="private/customer-123/secret-document.pdf",
            object_version="version-7",
            object_checksum=HASH_A,
            byte_size=1234,
            precondition_hash=HASH_A,
            dependency_manifest=[
                {"kind": "legal_hold", "blocking": True, "count": 1}
            ],
            lease_generation=1,
            state="held",
            reason_code="blocking_dependency",
            hold_reason="legal_hold",
            result_hash="",
            error_code="",
            error_detail_redacted="",
            remediation="",
            processed_at=None,
            tombstone_at=None,
        )
        payload = audit_api._retention_item_payload(row)
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("customer-123", rendered)
        self.assertNotIn("secret-document.pdf", rendered)
        self.assertRegex(payload["objectKeyRedacted"], r"^sha256:[a-f0-9]{16}$")
        _validator("RetentionBatchItem").validate(payload)

    def test_run_recovery_projection_lists_only_exact_retry_units(self):
        source = SimpleNamespace(id=uuid.UUID(SCHEDULE_ID), state="failed")
        publication = SimpleNamespace(
            id=uuid.UUID(TARGET_ID),
            publication=SimpleNamespace(target_id=uuid.UUID(PROOF_ID)),
            state="retryable_failed",
        )
        run = SimpleNamespace(
            stop_requested_at=None,
            collection_attempts=SimpleNamespace(all=lambda: [source]),
        )
        payload = collection_api._run_recovery_payload(
            run,
            document_rows=[],
            publication_rows=[publication],
        )
        self.assertEqual(
            payload["allowedRetryScopes"],
            [
                {"sourceAttemptId": SCHEDULE_ID},
                {"publicationAttemptId": TARGET_ID},
                {"targetId": PROOF_ID},
            ],
        )


class OperationsConsoleContractTests(SimpleTestCase):
    def test_console_is_korean_first_and_uses_safe_dom_rendering(self):
        template = (
            Path(settings.BASE_DIR)
            / "templates"
            / "admin_console"
            / "operations"
            / "index.html"
        ).read_text(encoding="utf-8")
        script = (
            Path(settings.BASE_DIR)
            / "static"
            / "admin_console"
            / "operations.js"
        ).read_text(encoding="utf-8")

        for text in (
            "운영 제어",
            "일정",
            "실행 복구",
            "정정 검증",
            "보존 삭제",
            "감사 이력",
        ):
            self.assertIn(text, template)
        self.assertNotIn("innerHTML", script)
        self.assertIn("textContent", script)
        self.assertIn("createElement", script)
        self.assertIn("/api/v1/retention/previews", script)
        self.assertIn("/api/v1/corrections", script)
        self.assertIn("actionScopes", script)

        legacy_runs = (
            Path(settings.BASE_DIR) / "static" / "admin_console" / "runs.js"
        ).read_text(encoding="utf-8")
        for required_field in (
            "targetIds",
            "approvalMode",
            "autoPublishValidationRefs",
            "autoPublishActivationRefs",
            "requestKey",
        ):
            self.assertIn(required_field, legacy_runs)
