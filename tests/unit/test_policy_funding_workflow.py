import json
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from apps.policy_funding.contracts import FundingNotice


def notice():
    return FundingNotice(
        source_id="semas_agency",
        external_id="404",
        title="2026년 4분기 일반경영안정자금(대리대출) 접수 안내",
        issuer="소진공",
        official_url="https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=404&bbsTypeCd=01",
        published_on=date(2026, 9, 11),
        application_period="2026. 10. 6. 10:00 ~ 예산소진시까지",
        regions=("national",),
        kinds=("agency_loan",),
        evidence_text="확인서는 대출 승인이 아닙니다.",
        fetched_at=datetime(2026, 10, 5, tzinfo=UTC),
    )


def test_workflow_publishes_validated_immutable_runs_and_changes(tmp_path):
    from apps.policy_funding.storage import read_run
    from apps.policy_funding.workflow import publish_collection

    day = date(2026, 10, 5)
    first = publish_collection(tmp_path, (notice(),), (), run_date=day, mode="live")
    second = publish_collection(tmp_path, (notice(),), (), run_date=day, mode="live")
    third = publish_collection(
        tmp_path,
        (replace(notice(), application_period="접수 마감"),),
        (),
        run_date=day,
        mode="live",
    )
    assert len({first.name, second.name, third.name}) == 3
    original = read_run(tmp_path, first.name)
    assert original["notices"][0].application_period.endswith("예산소진시까지")
    assert read_run(tmp_path, second.name)["changes"][0]["status"] == "unchanged"
    changed = read_run(tmp_path, third.name)["changes"][0]
    assert changed["status"] == "changed"
    assert "application_period" in changed["fields"]
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2


def test_missing_notice_in_bounded_collection_is_not_closed(tmp_path):
    from apps.policy_funding.storage import read_run
    from apps.policy_funding.workflow import publish_collection

    publish_collection(tmp_path, (notice(),), (), run_date=date(2026, 10, 5), mode="live")
    path = publish_collection(tmp_path, (), (), run_date=date(2026, 10, 5), mode="live")
    result = read_run(tmp_path, path.name)
    assert not any(row["status"] in {"deleted", "closed"} for row in result["changes"])


def test_fixture_mode_stays_separate_and_tampered_metadata_is_rejected(tmp_path):
    from apps.policy_funding.storage import read_run
    from apps.policy_funding.workflow import publish_collection

    path = publish_collection(tmp_path, (notice(),), (), run_date=date(2026, 10, 5), mode="fixture")
    result = read_run(tmp_path, path.name)
    assert result["mode"] == "fixture"
    assert result["notices"][0].provenance == "fixture"
    assert result["case_verification"]["claims"][0]["status"] == "fixture_only"
    (path / "notices.json").write_text('{"loan_approved":true}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_run(tmp_path, path.name)


@pytest.mark.parametrize("name", ["../escape", "2026-10-05/../../escape", "C:/Windows", "invalid"])
def test_reader_rejects_path_escape(tmp_path, name):
    from apps.policy_funding.storage import read_run

    with pytest.raises(ValueError):
        read_run(tmp_path, name)


def test_writer_has_a_validated_run_metadata_reader(tmp_path):
    from apps.local_content.bundles import ArticleBundleWriter

    writer = ArticleBundleWriter(tmp_path)
    root = writer.write_run(
        date(2026, 10, 5),
        "# Policy\n",
        (),
        run_files={"notices.json": "{}", "raw-observations.json": "{}"},
    )
    data = writer.read_validated_run_file(
        date(2026, 10, 5), "notices.json", run_directory=root.name
    )
    assert data == b"{}"
    with pytest.raises(ValueError):
        writer.read_validated_run_file(date(2026, 10, 5), "../outside", run_directory=root.name)
