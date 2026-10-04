import inspect
import json
from types import SimpleNamespace
from unittest.mock import Mock

from apps.publishing import api
from wisdome_writer.api.openapi import _compile_schema, load_openapi_contract


def _row(**changes):
    values = dict(
        id="11111111-1111-4111-8111-111111111111",
        material_hash="a" * 64,
        test_report_object_key="db://publishing/review",
        test_report_object_version="v1",
        test_report_hash="b" * 64,
        status="draft",
        created_at=api.datetime.fromisoformat("2026-10-03T00:00:00+00:00"),
        material_document={
            "validationEvidence": {
                "kind": "test_canary",
                "stageResults": [
                    {"code": "read_verified", "passed": True},
                    {"code": "cleanup_verified", "passed": True},
                ],
            }
        },
    )
    return SimpleNamespace(**{**values, **changes})


def test_canary_report_result_is_separate_from_pending_validation_decision(monkeypatch):
    monkeypatch.setattr(api.AutoPublishValidation.objects, "get", lambda **kwargs: _row())
    response = inspect.unwrap(api.auto_publish_validation_report)(None, "target", "validation")
    payload = json.loads(response.content)
    assert payload["overallResult"] == "passed"
    assert payload["validationState"] == "draft"
    assert payload["metrics"] == []
    document = load_openapi_contract()
    _, validator = _compile_schema(
        document["components"]["schemas"]["AutoPublishValidationReport"],
        document=document,
        subject="AutoPublishValidationReport",
    )
    validator.validate(payload)


def test_report_with_an_invalid_immutable_digest_cannot_be_reported_passed(monkeypatch):
    row = _row(status="passed", test_report_object_key="evidence/reports/review.json")
    monkeypatch.setattr(api.AutoPublishValidation.objects, "get", lambda **kwargs: row)
    storage = Mock()
    storage.get_bytes.return_value = b'{"overallResult":"passed"}'
    storage.get_bounded_bytes.return_value = storage.get_bytes.return_value
    monkeypatch.setattr(api, "S3ObjectStorage", lambda: storage)
    response = inspect.unwrap(api.auto_publish_validation_report)(None, "target", "validation")
    assert json.loads(response.content)["overallResult"] == "failed"
