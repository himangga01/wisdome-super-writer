from types import SimpleNamespace
from unittest.mock import Mock

from apps.evidence import tasks


def test_document_materializer_inherits_private_input_rights_and_manual_gate(monkeypatch):
    private = SimpleNamespace(
        rights_status="internal_analysis_only",
        rights_basis_url="https://example.com/private",
        attribution_text=None,
        manual_review_required=True,
    )
    document = SimpleNamespace(
        source_item=SimpleNamespace(title="Official notice"), input_asset=private
    )
    run = SimpleNamespace(
        state="succeeded",
        engine="native",
        package_version="v1",
        config_hash="a" * 64,
        result_checksum="b" * 64,
    )
    block = SimpleNamespace(
        block_id="block-1",
        block_type="text",
        polygon=None,
        bbox=[0, 0, 1, 1],
        reading_order=0,
        text="Official fact",
        structured_data={},
        confidence=1.0,
    )
    output = SimpleNamespace(
        low_confidence_reasons=[],
        confidence_summary={},
        pages=[SimpleNamespace(page_index=0, blocks=[block])],
    )
    record_rights = {
        "rights_status": "allowed",
        "rights_basis_url": "https://example.com/public",
        "attribution_text": None,
        "manual_review_required": False,
    }
    monkeypatch.setattr(tasks, "_rights", lambda *args, **kwargs: record_rights)
    monkeypatch.setattr(
        tasks.EvidenceAsset.objects, "filter", lambda **kwargs: SimpleNamespace(first=lambda: None)
    )
    captured = []

    def create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(**kwargs, full_clean=Mock(), save=Mock())

    monkeypatch.setattr(tasks.EvidenceAsset.objects, "create", create)
    monkeypatch.setattr(tasks, "calculate_review_subject_hash", lambda value: "c" * 64)
    result = tasks._make_document_evidence(document, run, output, SimpleNamespace())
    assert len(result) == 1
    assert captured[0]["rights_status"] == "internal_analysis_only"
    assert captured[0]["manual_review_required"] is True
    assert captured[0]["review_state"] == "manual_required"
