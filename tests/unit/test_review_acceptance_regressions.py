import importlib.util
import json
import runpy
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from django.utils import timezone

from apps.local_content import acceptance, acceptance_runner
from apps.local_content.api_reconciliation import ApplyHomeApiObserver, OfficialApiError
from apps.local_content.contracts import CollectionWindow
from apps.publishing.api import target_json
from apps.publishing.models import PublicationTarget
from wisdome_writer.api.openapi import _compile_schema, load_openapi_contract
from wisdome_writer.runtime_mode import validate_runtime_mode

ROOT = Path(__file__).resolve().parents[2]


def test_compose_django_services_explicitly_select_distributed_runtime():
    document = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    environments = [
        row["environment"]
        for row in document["services"].values()
        if row.get("environment", {}).get("DJANGO_SETTINGS_MODULE") == "wisdome_writer.settings"
    ]
    assert len(environments) > 5
    for values in environments:
        assert values.get("WISDOME_RUNTIME_MODE") == "distributed"
        validate_runtime_mode("development", values["WISDOME_RUNTIME_MODE"])


@pytest.mark.parametrize("boundary", ["ci", "acceptance"])
def test_ruff_filename_cannot_override_the_lint_configuration(tmp_path, monkeypatch, boundary):
    (tmp_path / "relax.py").write_text('lint.ignore = ["ALL"]\n')
    (tmp_path / "--config=relax.py").write_text("")
    (tmp_path / "bad.py").write_text("import os\n")
    files = ("--config=relax.py", "bad.py")
    monkeypatch.chdir(tmp_path)
    if boundary == "ci":
        spec = importlib.util.spec_from_file_location(
            "review_ci_helper", ROOT / "scripts/ci_changed_python.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "selected_python_files", lambda **kwargs: files)
        monkeypatch.setattr(sys, "argv", ["ci_changed_python.py"])
        result = module.main()
    else:
        plan = acceptance_runner.build_command_plan(
            project_root=tmp_path,
            base=acceptance_runner.TASK12_BASE_COMMIT,
            changed_python=files,
        )
        command = next(item for item in plan if item.name == "changed_file_ruff")
        result = subprocess.run(
            [sys.executable, *command.argv[1:]],
            cwd=tmp_path,
            capture_output=True,
            check=False,
        ).returncode
    assert result == 1


def test_acceptance_lint_discovery_preserves_git_quoted_unicode_paths(tmp_path, monkeypatch):
    def git(*arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        ).stdout

    git("init", "--initial-branch=main")
    git("config", "user.name", "Review Fixture")
    git("config", "user.email", "review-fixture@example.com")
    git("config", "core.quotepath", "true")
    (tmp_path / "base.txt").write_text("baseline\n")
    git("add", "base.txt")
    git("commit", "-m", "test fixture baseline")
    baseline = git("rev-parse", "HEAD").decode().strip()
    monkeypatch.setattr(acceptance_runner, "TASK12_BASE_COMMIT", baseline)
    local = tmp_path / "src/apps/local_content"
    local.mkdir(parents=True)
    (local / "fixture.py").write_text("VALUE = 1\n")
    (tmp_path / "한글 review.py").write_text("VALUE = 2\n", encoding="utf-8")
    selected = acceptance_runner.discover_changed_python(tmp_path, base=baseline)
    assert selected == ("src/apps/local_content/fixture.py", "한글 review.py")


@pytest.mark.parametrize("artifact_run", ["current-run", "different-run"])
def test_final_acceptance_binds_the_artifact_audit_to_the_current_workflow(
    tmp_path,
    monkeypatch,
    artifact_run,
):
    monkeypatch.setattr(
        acceptance,
        "verify_workflow_evidence",
        lambda *args, **kwargs: (True, {"failures": [], "run_name": "current-run"}),
    )
    monkeypatch.setattr(
        acceptance,
        "verify_artifact_evidence",
        lambda *args, **kwargs: (True, {"failures": [], "run_name": artifact_run}),
    )
    monkeypatch.setattr(
        acceptance,
        "verify_brave_evidence",
        lambda *args, **kwargs: (True, {"failures": []}),
    )
    monkeypatch.setattr(
        acceptance,
        "verify_attempt_selection",
        lambda *args, **kwargs: SimpleNamespace(passed=True, evidence={"failures": []}),
    )
    report = {"run_path": "current-run", "articles": [], "task12_acceptance": {}}
    result = acceptance.derive_task12_acceptance(report, project_root=tmp_path)
    assert result["overall_passed"] is (artifact_run == "current-run")


def test_filtered_odcloud_observations_use_the_matching_record_count():
    requested_pages = []

    class Fetcher:
        def get_json(self, url, *, params):
            requested_pages.append(params["page"])
            return {
                "totalCount": 200,
                "matchCount": 1,
                "data": [
                    {
                        "HOUSE_MANAGE_NO": "42",
                        "PBLANC_NO": "1",
                        "HOUSE_NM": "Official notice",
                        "RCRIT_PBLANC_DE": "2026-10-03",
                    }
                ]
                if params["page"] == 1
                else [],
            }

    instant = timezone.datetime.fromisoformat("2026-10-03T12:00:00+09:00")
    result = ApplyHomeApiObserver(Fetcher())._endpoint(
        CollectionWindow(start=instant.replace(hour=0), end=instant),
        category="apt",
        endpoint="https://api.odcloud.kr/api/review",
    )
    assert len(result) == 1
    assert requested_pages == [1]


@pytest.mark.parametrize("total", [0, -1, "invalid", True])
def test_filtered_odcloud_count_must_still_be_a_valid_total(total):
    class Fetcher:
        def get_json(self, url, *, params):
            return {
                "totalCount": total,
                "matchCount": 1,
                "data": [
                    {
                        "HOUSE_MANAGE_NO": "42",
                        "PBLANC_NO": "1",
                        "HOUSE_NM": "Official notice",
                        "RCRIT_PBLANC_DE": "2026-10-03",
                    }
                ],
            }

    instant = timezone.datetime.fromisoformat("2026-10-03T12:00:00+09:00")
    with pytest.raises(OfficialApiError, match="SCHEMA"):
        ApplyHomeApiObserver(Fetcher())._endpoint(
            CollectionWindow(start=instant.replace(hour=0), end=instant),
            category="apt",
            endpoint="https://api.odcloud.kr/api/review",
        )


def test_snapshot_bound_target_serializes_capabilities_and_absent_activation_to_api_contract():
    row = PublicationTarget(
        channel="wordpress",
        role="primary_canonical",
        environment="test",
        display_name="Review",
        base_url="https://wordpress.example.com",
        current_snapshot_id="11111111-1111-4111-8111-111111111111",
        current_snapshot_version=1,
        current_config_hash="a" * 64,
        publisher_contract_version="publisher-v1",
        publisher_adapter_manifest_hash="b" * 64,
        capabilities={
            "create": True,
            "update": True,
            "unpublish": True,
            "mark_withdrawn": True,
            "draft": True,
            "schedule": True,
            "media_upload": True,
        },
    )
    payload = target_json(row)
    document = load_openapi_contract()
    _, validator = _compile_schema(
        document["components"]["schemas"]["PublicationTarget"],
        document=document,
        subject="PublicationTarget",
    )
    validator.validate(payload)
    assert payload["capabilities"]["markWithdrawn"] is True
    assert payload["capabilities"]["mediaUpload"] is True
    assert payload["autoPublishActivationVersion"] is None


def test_acceptance_history_selects_all_verified_passes_and_the_latest_one(tmp_path):
    helpers = runpy.run_path(str(ROOT / "tests/unit/test_task12_deterministic_runner.py"))
    cli = runpy.run_path(str(ROOT / "scripts/run_task12_deterministic.py"))
    old, project = helpers["_closed_attempt"](tmp_path, attempt_id="old-pass")
    old_root = Path(old["log_root"])
    new_root = old_root.with_name("new-pass")
    new_root.mkdir()
    new = deepcopy(old)
    new["attempt_id"] = "new-pass"
    new["log_root"] = str(new_root)
    for command in new["commands"]:
        original = Path(command["log_path"])
        path = new_root / original.name
        path.write_bytes(original.read_bytes())
        command["log_path"] = str(path)
    old_path, new_path = old_root / "evidence.json", new_root / "evidence.json"
    old_path.write_text(json.dumps(old), encoding="utf-8")
    new_path.write_text(json.dumps(new), encoding="utf-8")
    report_path = project / "output/housing/report.json"
    report_path.write_text("{}", encoding="utf-8")
    cli["_merge_report"](
        report_path, new, evidence_path=new_path, history_specs=[f"old-pass={old_path}"]
    )
    section = json.loads(report_path.read_text("utf-8"))["task12_acceptance"]
    assert section["deterministic_selection"]["eligible_attempt_ids"] == ["old-pass", "new-pass"]
    result = acceptance_runner.verify_attempt_selection(
        history=section["deterministic_attempt_history"],
        selection=section["deterministic_selection"],
        selected_evidence=section["deterministic_evidence"],
        project_root=project,
        current_changed_python=helpers["CHANGED"],
    )
    assert result.passed is True
