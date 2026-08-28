from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from apps.local_content.acceptance import (
    derive_task12_acceptance,
    humanizer_identity_evidence,
    validate_exact_kst_window,
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
    report: dict[str, object] = {
        "live_success": True,
        "complete": True,
        "blocked": False,
        "error_codes": [],
        "task12_acceptance": {
            "artifact_audit": {"overall_passed": True},
            "browser": {"passed": True},
            "deterministic_evidence": evidence,
            "deterministic_attempt_history": history,
            "deterministic_selection": {
                "rule": "latest_complete_exact_plan_pass",
                "selected_attempt_id": "attempt2",
                "eligible_attempt_ids": ["attempt2"],
            },
        },
    }
    return report, root


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


@pytest.mark.parametrize(
    "failure",
    ["workflow", "artifact", "browser", "selection", "deterministic_log"],
)
def test_final_acceptance_fails_when_any_independent_gate_is_invalid(
    tmp_path: Path,
    failure: str,
) -> None:
    report, root = _report(tmp_path)
    section = report["task12_acceptance"]
    if failure == "workflow":
        report["complete"] = False
    elif failure == "artifact":
        section["artifact_audit"]["overall_passed"] = False
    elif failure == "browser":
        section["browser"]["passed"] = False
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
