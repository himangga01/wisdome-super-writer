from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from apps.local_content import acceptance as acceptance_module
from apps.local_content.acceptance import (
    build_evidence_reference,
    derive_task12_acceptance,
    humanizer_identity_evidence,
    validate_exact_kst_window,
    workflow_report_core,
)
from apps.local_content.acceptance_runner import (
    TASK12_BASE_COMMIT,
    CommandResult,
    build_attempt_history_entry,
    build_command_plan,
    build_deterministic_evidence,
    parse_command_output,
)
from apps.local_content.dates import SEOUL

CHANGED = ("src/apps/local_content/acceptance.py",)
RUN_NAME = "2026-08-28--run-aaaaaaaaaaaa"
PREVIEW_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'; object-src 'none'; img-src 'self'; style-src 'self'"
    ),
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "cache-control": "no-store",
}


@pytest.fixture(autouse=True)
def _controlled_artifact_reaudit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acceptance_module, "audit_live_run", _fake_artifact_audit)


def _command_output(name: str) -> bytes:
    return {
        "setup_local": (
            b"No migrations to apply.\nSystem check identified no issues.\n"
            b"Local setup completed.\n"
        ),
        "whole_repository_ruff": b"Found 2 errors.\n",
        "django_check": b"System check identified no issues.\n",
        "django_migrate": b"Running migrations:\n  No migrations to apply.\n",
        "migration_check": b"No changes detected\n",
        "focused_pytest": b"10 passed in 1.00s\n",
        "full_pytest": b"20 passed, 2 subtests passed in 2.00s\n",
        "changed_file_ruff": b"All checks passed!\n",
        "ci_material": b"All checks passed!\n",
        "diff_check": b"",
    }[name]


def _report(tmp_path: Path) -> tuple[dict[str, object], Path]:
    root = tmp_path / "project"
    root.mkdir()
    log_root = root / "output" / "housing" / "2026-08-28" / "acceptance" / "attempt2"
    log_root.mkdir(parents=True)
    plan = build_command_plan(root, base=TASK12_BASE_COMMIT, changed_python=CHANGED)
    results = []
    for index, spec in enumerate(plan):
        payload = _command_output(spec.name)
        log = log_root / f"{index + 1:02d}-{spec.name}.log"
        log.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        exit_code = 1 if spec.name == "whole_repository_ruff" else 0
        results.append(
            CommandResult(
                name=spec.name,
                argv=spec.argv,
                cwd=str(root.resolve()),
                gate=spec.gate,
                started_at=f"2026-08-28T00:00:{index:02d}+00:00",
                finished_at=f"2026-08-28T00:00:{index:02d}.100000+00:00",
                duration_ms=100,
                exit_code=exit_code,
                output_bytes=len(payload),
                output_sha256=digest,
                log_path=str(log.resolve()),
                log_bytes=len(payload),
                log_sha256=digest,
                log_truncated=False,
                parsed_result=parse_command_output(spec.name, payload, exit_code),
            )
        )
    evidence = build_deterministic_evidence(
        results,
        attempt_id="attempt2",
        project_root=root,
        base=TASK12_BASE_COMMIT,
        changed_python=CHANGED,
        log_root=log_root,
    )
    evidence_path = log_root.parent / "attempt2-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    history = [build_attempt_history_entry("attempt2", evidence_path)]
    run_root = root / "output" / "housing" / RUN_NAME
    run_root.mkdir()
    report_path = run_root / "acceptance-report.json"
    report: dict[str, object] = {
        "schema_version": 1,
        "workflow_id": "workflow-closed-1",
        "started_at": "2026-08-28T12:00:00+09:00",
        "mode": "live",
        "live_success": True,
        "complete": True,
        "blocked": False,
        "window": {
            "start": "2026-08-22T00:00:00+09:00",
            "end": "2026-08-28T12:00:00+09:00",
        },
        "counts": {
            "sources": 2,
            "notices": 2,
            "conflicts": 0,
            "excluded": 1,
            "selected_articles": 1,
            "written_articles": 1,
        },
        "run_path": RUN_NAME,
        "report_path": f"{RUN_NAME}/acceptance-report.json",
        "error_codes": [],
        "sources": [
            {
                "source_key": "applyhome",
                "status": "complete",
                "notice_count": 1,
                "warning_count": 0,
                "error_codes": [],
            },
            {
                "source_key": "lh",
                "status": "complete",
                "notice_count": 1,
                "warning_count": 0,
                "error_codes": [],
            },
        ],
        "phases": [
            {
                "name": name,
                "status": "completed",
                "count": 1,
                "paths": [RUN_NAME] if name == "write" else [],
                "error_codes": [],
            }
            for name in (
                "collect",
                "merge",
                "select",
                "render",
                "images",
                "humanize",
                "verify",
                "write",
            )
        ],
        "articles": [
            {
                "source_key": "applyhome",
                "external_id": "applyhome:apt:1:1",
                "slug": "article-1",
                "status": "written",
                "humanization_status": "verified",
                "article_path": f"{RUN_NAME}/article-1",
                "draft_path": None,
                "error_codes": [],
            }
        ],
        "task12_acceptance": {
            "deterministic_evidence": evidence,
            "deterministic_attempt_history": history,
            "deterministic_selection": {
                "rule": "latest_complete_exact_plan_pass",
                "selected_attempt_id": "attempt2",
                "eligible_attempt_ids": ["attempt2"],
            },
        },
    }
    core = workflow_report_core(report)
    workflow_log = log_root / "live-workflow.log"
    workflow_summary = {
        "workflow_id": "workflow-closed-1",
        "mode": "live",
        "complete": True,
        "blocked": False,
        "live_success": True,
        "article_count": 1,
        "written_count": 1,
        "run_path": RUN_NAME,
        "report_path": f"{RUN_NAME}/acceptance-report.json",
        "error_codes": [],
        "official_api_reconciliation": "inactive_no_key",
    }
    workflow_payload = (json.dumps(workflow_summary, sort_keys=True) + "\n").encode()
    workflow_log.write_bytes(workflow_payload)
    workflow_evidence = {
        "schema_version": 1,
        "command": {
            "argv": [
                str(root / ".venv" / "Scripts" / "python.exe"),
                "src\\manage.py",
                "collect_recent_housing",
                "--days",
                "7",
                "--humanize",
                "--write-articles",
            ],
            "cwd": str(root.resolve()),
            "started_at": "2026-08-28T03:00:00+00:00",
            "finished_at": "2026-08-28T03:00:00.100000+00:00",
            "duration_ms": 100,
            "exit_code": 0,
            "log_path": str(workflow_log.resolve()),
            "log_bytes": len(workflow_payload),
            "log_sha256": hashlib.sha256(workflow_payload).hexdigest(),
            "log_truncated": False,
            "output_bytes": len(workflow_payload),
            "output_sha256": hashlib.sha256(workflow_payload).hexdigest(),
        },
        "report_path": str(report_path.resolve()),
        "report_core_sha256": _canonical_sha256(core),
        "run_name": RUN_NAME,
        "official_api_reconciliation": "inactive_no_key",
    }
    workflow_evidence_path = log_root / "workflow-evidence.json"
    _write_json(workflow_evidence_path, workflow_evidence)
    artifact_audit = _fake_artifact_audit(
        run_root,
        window_start=datetime(2026, 8, 22, tzinfo=SEOUL),
        expectations=type(
            "Expectations",
            (),
            {
                "raw_observations": 3,
                "excluded_notices": 1,
                "indexed_notices": 2,
                "detailed_articles": 1,
                "source_keys": ("applyhome", "lh"),
            },
        )(),
        executed_at=datetime(2026, 8, 28, 13, tzinfo=SEOUL),
    )
    artifact_evidence = {
        "schema_version": 2,
        "run_root": str(run_root.resolve()),
        "window_start": "2026-08-22T00:00:00+09:00",
        "executed_at": "2026-08-28T13:00:00+09:00",
        "expectations": {
            "raw_observations": 3,
            "excluded_notices": 1,
            "indexed_notices": 2,
            "detailed_articles": 1,
            "source_keys": ["applyhome", "lh"],
        },
        "audit": artifact_audit,
    }
    artifact_path = log_root / "artifact-evidence.json"
    _write_json(artifact_path, artifact_evidence)
    browser_path = log_root / "brave-evidence.json"
    _write_json(browser_path, _browser_evidence(log_root))
    report["task12_acceptance"].update(
        {
            "workflow_evidence": build_evidence_reference(workflow_evidence_path),
            "artifact_evidence": build_evidence_reference(artifact_path),
            "browser_evidence": build_evidence_reference(browser_path),
        }
    )
    _write_json(report_path, report)
    return report, root


def _fake_artifact_audit(run_root, *, expectations, **_kwargs):
    return {
        "schema_version": 1,
        "run_name": Path(run_root).name,
        "requirements": [{"name": "fresh_production_audit", "passed": True, "evidence": {}}],
        "counts": {
            "raw_observations": expectations.raw_observations,
            "excluded_notices": expectations.excluded_notices,
            "indexed_notices": expectations.indexed_notices,
            "detailed_articles": expectations.detailed_articles,
            "images": expectations.detailed_articles * 3,
        },
        "humanizer_identity_evidence": {"passed": True},
        "humanizer_job_hashes": ["1" * 64],
        "final_bundle_hashes": ["2" * 64],
        "final_article_hashes": ["3" * 64],
        "humanizer_verification_hashes": ["4" * 64],
        "candidate_output_hashes": ["5" * 64],
        "overall_passed": True,
    }


def _browser_evidence(root: Path) -> dict[str, object]:
    screenshot_references = {}
    for name in ("requested_index", "run_index", "detail_desktop", "detail_mobile"):
        path = root / f"{name}.png"
        path.write_bytes(f"png-{name}".encode())
        screenshot_references[name] = build_evidence_reference(path)
    detail = {
        "url": f"http://127.0.0.1:8000/local-articles/{RUN_NAME}/article-1/",
        "status": 200,
        "heading_has_korean": True,
        "horizontal_overflow": False,
        "images": [
            {
                "src": f"http://127.0.0.1:8000/local-articles/{RUN_NAME}/article-1/assets/{index}",
                "alt_present": True,
                "complete": True,
                "width": 1200,
                "height": 630,
            }
            for index in range(3)
        ],
        "official_link_count": 1,
        "security_headers": PREVIEW_HEADERS,
        "headers_exact": True,
        "passed": True,
    }
    asset_headers = {**PREVIEW_HEADERS, "cache-control": "public, max-age=31536000, immutable"}
    assertions = {
        "brave_executable_exact": True,
        "requested_index": True,
        "run_index": True,
        "all_detail_pages": True,
        "representative_mobile": True,
        "asset_headers_exact": True,
        "no_console_or_page_errors": True,
        "no_failed_requests": True,
        "no_bad_local_responses": True,
        "traversal_rejected": True,
        "status_ready_headers_exact": True,
        "owned_brave_processes_closed": True,
        "screenshots_digest_bound": True,
    }
    return {
        "schema_version": 3,
        "checked_at": "2026-08-28T04:00:00+00:00",
        "brave_executable": (
            r"C:\Users\c\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"
        ),
        "brave_owned_pids": [101],
        "brave_surviving_owned_pids": [],
        "django_owned_pids": [202],
        "requested_index": {
            "url": "http://127.0.0.1:8000/local-articles/2026-08-28/",
            "status": 200,
            "heading_has_korean": True,
            "html_lang": "ko",
            "horizontal_overflow": False,
            "security_headers": PREVIEW_HEADERS,
            "headers_exact": True,
            "passed": True,
        },
        "run_index": {
            "url": f"http://127.0.0.1:8000/local-articles/{RUN_NAME}/",
            "status": 200,
            "heading_has_korean": True,
            "html_lang": "ko",
            "horizontal_overflow": False,
            "security_headers": PREVIEW_HEADERS,
            "headers_exact": True,
            "passed": True,
        },
        "detail_pages": [detail],
        "mobile": {**detail, "body_font_size": "16px"},
        "status_endpoint": {
            "status": 200,
            "headers_exact": True,
            "humanizer_ready": True,
            "security_headers": PREVIEW_HEADERS,
        },
        "asset_headers": asset_headers,
        "traversal_statuses": [404, 404],
        "diagnostics": {
            "console_error_count": 0,
            "page_error_count": 0,
            "failed_request_count": 0,
            "bad_local_response_count": 0,
            "local_response_count": 10,
        },
        "screenshots": screenshot_references,
        "assertions": assertions,
        "passed": True,
    }


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def test_final_acceptance_independently_verifies_selected_attempt(tmp_path: Path) -> None:
    report, root = _report(tmp_path)

    final = derive_task12_acceptance(
        report,
        project_root=root,
        current_changed_python=CHANGED,
    )

    assert final["overall_passed"] is True
    evidence = final["requirements"][-1]["evidence"]
    assert evidence["selected_attempt_id"] == "attempt2"
    assert evidence["history_attempt_ids"] == ["attempt2"]


def test_fabricated_summary_booleans_without_closed_evidence_fail(tmp_path: Path) -> None:
    report, root = _report(tmp_path)
    section = report["task12_acceptance"]
    section.pop("workflow_evidence")
    section.pop("artifact_evidence")
    section.pop("browser_evidence")
    section["artifact_audit"] = {"overall_passed": True}
    section["browser"] = {"passed": True}

    final = derive_task12_acceptance(
        report,
        project_root=root,
        current_changed_python=CHANGED,
    )

    assert final["overall_passed"] is False
    assert [row["passed"] for row in final["requirements"][:3]] == [False, False, False]


@pytest.mark.parametrize(
    "failure",
    [
        "workflow_report",
        "workflow_log",
        "artifact_digest",
        "artifact_fabricated_summary",
        "browser_digest",
        "browser_incomplete",
        "selection",
        "deterministic_log",
    ],
)
def test_final_acceptance_fails_when_any_independent_gate_is_invalid(
    tmp_path: Path,
    failure: str,
) -> None:
    report, root = _report(tmp_path)
    section = report["task12_acceptance"]
    if failure == "workflow_report":
        report["complete"] = False
    elif failure == "workflow_log":
        Path(section["workflow_evidence"]["path"]).with_name("live-workflow.log").write_text(
            "tampered", encoding="utf-8"
        )
    elif failure == "artifact_digest":
        Path(section["artifact_evidence"]["path"]).write_text("{}", encoding="utf-8")
    elif failure == "artifact_fabricated_summary":
        path = Path(section["artifact_evidence"]["path"])
        document = json.loads(path.read_text("utf-8"))
        document["audit"] = {"overall_passed": True}
        _write_json(path, document)
        section["artifact_evidence"] = build_evidence_reference(path)
    elif failure == "browser_digest":
        Path(section["browser_evidence"]["path"]).write_text("{}", encoding="utf-8")
    elif failure == "browser_incomplete":
        path = Path(section["browser_evidence"]["path"])
        document = json.loads(path.read_text("utf-8"))
        document["detail_pages"] = []
        document["passed"] = True
        _write_json(path, document)
        section["browser_evidence"] = build_evidence_reference(path)
    elif failure == "selection":
        section["deterministic_selection"]["selected_attempt_id"] = "attempt1"
    else:
        path = Path(section["deterministic_evidence"]["commands"][2]["log_path"])
        path.write_bytes(b"tampered")

    final = derive_task12_acceptance(
        report,
        project_root=root,
        current_changed_python=CHANGED,
    )

    assert final["overall_passed"] is False


def test_humanizer_identity_allows_duplicate_candidate_prose_transparently() -> None:
    verdict = humanizer_identity_evidence(
        job_hashes=("1" * 64, "2" * 64),
        bundle_hashes=("3" * 64, "4" * 64),
        final_article_hashes=("5" * 64, "6" * 64),
        verification_hashes=("7" * 64, "8" * 64),
        candidate_output_hashes=("9" * 64, "9" * 64),
        expected=2,
    )

    assert verdict["passed"] is True
    assert verdict["candidate_output_count"] == 2
    assert verdict["unique_candidate_output_count"] == 1
    assert verdict["candidate_output_uniqueness_gate"] is False


@pytest.mark.parametrize(
    "field",
    ["job_hashes", "bundle_hashes", "final_article_hashes", "verification_hashes"],
)
def test_humanizer_identity_rejects_duplicate_binding_hashes(field: str) -> None:
    material = {
        "job_hashes": ("1" * 64, "2" * 64),
        "bundle_hashes": ("3" * 64, "4" * 64),
        "final_article_hashes": ("5" * 64, "6" * 64),
        "verification_hashes": ("7" * 64, "8" * 64),
        "candidate_output_hashes": ("9" * 64, "9" * 64),
    }
    duplicate = material[field][0]
    material[field] = (duplicate, duplicate)

    verdict = humanizer_identity_evidence(**material, expected=2)

    assert verdict["passed"] is False


def test_exact_kst_window_accepts_literal_plus_nine_representation() -> None:
    verdict = validate_exact_kst_window(
        start_text="2026-08-22T00:00:00+09:00",
        end_text="2026-08-28T13:23:31.621371+09:00",
        expected_start=datetime(2026, 8, 22, tzinfo=SEOUL),
        run_date=date(2026, 8, 28),
        executed_at=datetime(2026, 8, 28, 14, 0, tzinfo=SEOUL),
    )

    assert verdict["passed"] is True


@pytest.mark.parametrize(
    ("start_text", "end_text"),
    [
        ("2026-08-21T15:00:00+00:00", "2026-08-28T13:23:31.621371+09:00"),
        ("2026-08-22T00:00:00+09:00", "2026-08-28T04:23:31.621371+00:00"),
        ("2026-08-22T00:00:00+09:00", "2026-08-28T14:00:00.000001+09:00"),
        ("2026-08-22T00:00:00+09:00", "2026-08-29T00:00:00+09:00"),
    ],
)
def test_exact_kst_window_rejects_equivalent_or_out_of_run_representation(
    start_text: str,
    end_text: str,
) -> None:
    verdict = validate_exact_kst_window(
        start_text=start_text,
        end_text=end_text,
        expected_start=datetime(2026, 8, 22, tzinfo=SEOUL),
        run_date=date(2026, 8, 28),
        executed_at=datetime(2026, 8, 28, 14, 0, tzinfo=SEOUL),
    )

    assert verdict["passed"] is False
