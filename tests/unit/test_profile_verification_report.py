from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from adapters.extractors.base import canonical_bytes
from adapters.storage import ObjectInfo
from apps.evidence import api as evidence_api
from apps.evidence.management.commands import verify_extraction_profile_files as command_module
from django.core.exceptions import ValidationError

from apps.evidence.models import (
    ExtractionProfileDecision,
    ExtractionProfileSnapshot,
    ProfileApprovalState,
)
from apps.evidence.services import (
    _profile_report_envelope,
    _validate_profile_report,
    canonical_hash,
)


class _Storage:
    def __init__(self, *, version_id: str | None, etag: str | None = None) -> None:
        self.version_id = version_id
        self.etag = etag
        self.calls: list[dict[str, object]] = []

    def put_bytes(self, **kwargs):
        self.calls.append(kwargs)
        data = kwargs["data"]
        return ObjectInfo(
            key=str(kwargs["key"]),
            version_id=self.version_id,
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            content_type="application/json",
            etag=self.etag,
        )


class ProfileVerificationReportTests(unittest.TestCase):
    def _profile(self):
        saved: list[tuple[str, ...]] = []
        profile = SimpleNamespace(
            id="11111111-1111-4111-8111-111111111111",
            profile_material_hash="1" * 64,
            verification_report_object_key=None,
            verification_report_object_version=None,
            verification_report_hash=None,
            approval_state=ProfileApprovalState.DRAFT,
            latest_decision=None,
            save=lambda *, update_fields: saved.append(tuple(update_fields)),
        )
        return profile, saved

    def _report(self, profile) -> dict[str, object]:
        return {
            "schemaVersion": "v1",
            "subjectType": "extraction_profile_snapshot",
            "subjectId": str(profile.id),
            "subjectMaterialHash": profile.profile_material_hash,
            "overallResult": "passed",
            "stageResults": [{"stage": "files", "result": "passed"}],
        }

    def test_uploads_self_reference_free_core_and_persists_actual_version_id(self) -> None:
        profile, saved = self._profile()
        report = self._report(profile)
        storage = _Storage(version_id="s3-version-7")

        report_hash = command_module._persist_verification_report(
            profile=profile,
            report=report,
            storage=storage,
        )

        uploaded = json.loads(storage.calls[0]["data"])
        self.assertEqual(uploaded, report)
        self.assertNotIn("reportObjectVersion", uploaded)
        self.assertEqual(report_hash, canonical_hash(report))
        self.assertEqual(profile.verification_report_object_version, "s3-version-7")
        self.assertEqual(profile.verification_report_hash, report_hash)
        self.assertEqual(len(saved), 1)

    def test_etag_fallback_is_explicit_and_core_report_validates_against_db_envelope(self) -> None:
        profile, _saved = self._profile()
        report = self._report(profile)
        storage = _Storage(version_id=None, etag="etag-value")
        command_module._persist_verification_report(
            profile=profile,
            report=report,
            storage=storage,
        )

        self.assertEqual(profile.verification_report_object_version, "etag:etag-value")
        self.assertEqual(_validate_profile_report(profile, report), report)
        mutated = json.loads(canonical_bytes(report))
        mutated["overallResult"] = "failed"
        with self.assertRaises(Exception):
            _validate_profile_report(profile, mutated)

    def test_approved_profile_report_is_revalidated_without_overwriting_envelope(self) -> None:
        profile, saved = self._profile()
        report = self._report(profile)
        report_hash = canonical_hash(report)
        profile.approval_state = ProfileApprovalState.APPROVED
        profile.verification_report_object_key = "reports/frozen.json"
        profile.verification_report_object_version = "frozen-version"
        profile.verification_report_hash = report_hash
        storage = _Storage(version_id="new-version")
        storage.get_bytes = lambda **_kwargs: canonical_bytes(report)

        observed_hash = command_module._persist_verification_report(
            profile=profile,
            report=report | {"generatedAt": "new-mutable-time"},
            storage=storage,
        )

        self.assertEqual(observed_hash, report_hash)
        self.assertEqual(storage.calls, [])
        self.assertEqual(saved, [])
        self.assertEqual(profile.verification_report_object_version, "frozen-version")

    def test_approved_profile_rejects_noncanonical_or_duplicate_core_bytes(self) -> None:
        profile, _saved = self._profile()
        report = self._report(profile)
        profile.approval_state = ProfileApprovalState.APPROVED
        profile.verification_report_object_key = "reports/frozen.json"
        profile.verification_report_object_version = "frozen-version"
        profile.verification_report_hash = canonical_hash(report)
        storage = _Storage(version_id="new-version")
        storage.get_bytes = lambda **_kwargs: b" " + canonical_bytes(report)

        with self.assertRaises(ValidationError):
            command_module._persist_verification_report(
                profile=profile,
                report=report,
                storage=storage,
            )

    def test_approved_decision_and_api_envelope_are_frozen_to_the_decision(self) -> None:
        profile, _saved = self._profile()
        profile.approval_state = ProfileApprovalState.APPROVED
        profile.verification_report_object_key = "reports/frozen.json"
        profile.verification_report_object_version = "frozen-version"
        profile.verification_report_hash = "2" * 64
        decision = SimpleNamespace(
            decision=ExtractionProfileDecision.Decision.APPROVED,
            verification_report_object_key="reports/frozen.json",
            verification_report_object_version="frozen-version",
            verification_report_hash="2" * 64,
        )
        profile.latest_decision = decision

        self.assertEqual(
            _profile_report_envelope(profile),
            ("reports/frozen.json", "frozen-version", "2" * 64),
        )
        profile.verification_report_hash = "3" * 64
        with self.assertRaises(Exception):
            _profile_report_envelope(profile)

    def test_decision_requires_report_envelope_matching_profile_projection(self) -> None:
        profile = ExtractionProfileSnapshot(
            id=uuid.uuid4(),
            verification_report_object_key="reports/frozen.json",
            verification_report_object_version="frozen-version",
            verification_report_hash="2" * 64,
        )
        decision = ExtractionProfileDecision(
            profile_snapshot=profile,
            decision=ExtractionProfileDecision.Decision.APPROVED,
        )
        with self.assertRaises(ValidationError):
            decision.clean()

        decision.verification_report_object_key = "reports/frozen.json"
        decision.verification_report_object_version = "frozen-version"
        decision.verification_report_hash = "2" * 64
        decision.clean()

    def test_approved_report_api_returns_conflict_for_mutated_projection(self) -> None:
        profile, _saved = self._profile()
        profile.approval_state = ProfileApprovalState.APPROVED
        profile.verification_report_object_key = "reports/frozen.json"
        profile.verification_report_object_version = "frozen-version"
        profile.verification_report_hash = "3" * 64
        profile.latest_decision = SimpleNamespace(
            decision=ExtractionProfileDecision.Decision.APPROVED,
            verification_report_object_key="reports/frozen.json",
            verification_report_object_version="frozen-version",
            verification_report_hash="2" * 64,
        )
        core_view = inspect.unwrap(evidence_api.extraction_profile_report)
        with patch.object(evidence_api, "get_object_or_404", return_value=profile):
            response = core_view(None, profile.id)

        self.assertEqual(response.status_code, 409)

    def test_migration_backfills_existing_decision_chain_or_fails_closed(self) -> None:
        migration = importlib.import_module(
            "apps.evidence.migrations.0004_extractionprofiledecision_report_envelope"
        )

        class _DecisionManager:
            def __init__(self, rows):
                self.rows = rows

            def using(self, _alias):
                return self

            def select_related(self, _field):
                return self

            def iterator(self):
                return iter(self.rows)

        saved: list[tuple[str, ...]] = []
        profile = SimpleNamespace(
            verification_report_object_key="reports/frozen.json",
            verification_report_object_version="frozen-version",
            verification_report_hash="2" * 64,
        )
        row = SimpleNamespace(
            profile_snapshot=profile,
            verification_report_object_key=None,
            verification_report_object_version=None,
            verification_report_hash=None,
            save=lambda **kwargs: saved.append(tuple(kwargs["update_fields"])),
        )
        manager = _DecisionManager([row])
        apps = SimpleNamespace(
            get_model=lambda _app, _model: SimpleNamespace(objects=manager)
        )
        editor = SimpleNamespace(connection=SimpleNamespace(alias="default"))

        migration.backfill_profile_report_envelopes(apps, editor)
        self.assertEqual(row.verification_report_hash, "2" * 64)
        self.assertEqual(len(saved), 1)

        profile.verification_report_object_version = None
        with self.assertRaises(RuntimeError):
            migration.backfill_profile_report_envelopes(apps, editor)


if __name__ == "__main__":
    unittest.main()
