from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from apps.local_content.acceptance_runner import (
    TASK12_BASE_COMMIT,
    TASK12_LEGACY_RUFF_RULING_HASH,
    TASK12_LEGACY_RUFF_RULING_ID,
    TASK12_LEGACY_RUFF_RULING_TEXT,
    CommandResult,
    build_attempt_history_entry,
    build_command_plan,
    build_deterministic_evidence,
    command_plan_document,
    command_plan_sha256,
    execute_command,
    parse_command_output,
    verify_attempt_selection,
    verify_deterministic_evidence,
)

CHANGED = ("src/apps/local_content/acceptance.py", "tests/unit/test_probe.py")


def _output_for(name: str, *, whole_exit: int = 1) -> bytes:
    values = {
        "setup_local": (
            b"No migrations to apply.\nSystem check identified no issues (0 silenced).\n"
            b"Local setup completed. No secret values were printed.\n"
        ),
        "whole_repository_ruff": (
            b"Found 12 errors.\n" if whole_exit == 1 else b"All checks passed!\n"
        ),
        "django_check": b"System check identified no issues (0 silenced).\n",
        "migration_check": b"No changes detected\n",
        "focused_pytest": b"499 passed, 2 skipped, 6 warnings in 1.00s\n",
        "full_pytest": b"1224 passed, 5 skipped, 198 subtests passed in 2.00s\n",
        "changed_file_ruff": b"All checks passed!\n",
        "ci_material": b"All checks passed!\n",
        "diff_check": b"",
    }
    return values[name]


def _closed_attempt(
    tmp_path: Path,
    *,
    attempt_id: str = "round2-attempt1",
    whole_exit: int = 1,
) -> tuple[dict[str, object], Path]:
    project_root = tmp_path / "project"
    project_root.mkdir()
    log_root = (
        project_root
        / "output"
        / "housing"
        / "2026-08-28"
        / "acceptance"
        / attempt_id
    )
    log_root.mkdir(parents=True)
    plan = build_command_plan(
        project_root,
        base=TASK12_BASE_COMMIT,
        changed_python=CHANGED,
    )
    results: list[CommandResult] = []
    for index, spec in enumerate(plan):
        exit_code = whole_exit if spec.name == "whole_repository_ruff" else 0
        output = _output_for(spec.name, whole_exit=whole_exit)
        log_path = log_root / f"{index + 1:02d}-{spec.name}.log"
        log_path.write_bytes(output)
        digest = hashlib.sha256(output).hexdigest()
        results.append(
            CommandResult(
                name=spec.name,
                argv=spec.argv,
                cwd=str(project_root.resolve()),
                gate=spec.gate,
                started_at=f"2026-08-28T00:00:{index:02d}+00:00",
                finished_at=f"2026-08-28T00:00:{index:02d}.250000+00:00",
                duration_ms=250,
                exit_code=exit_code,
                output_bytes=len(output),
                output_sha256=digest,
                log_path=str(log_path.resolve()),
                log_bytes=len(output),
                log_sha256=digest,
                log_truncated=False,
                parsed_result=parse_command_output(spec.name, output, exit_code),
            )
        )
    evidence = build_deterministic_evidence(
        results,
        attempt_id=attempt_id,
        project_root=project_root,
        base=TASK12_BASE_COMMIT,
        changed_python=CHANGED,
        log_root=log_root,
    )
    return evidence, project_root


def test_command_plan_is_exact_ordered_hashed_contract(tmp_path: Path) -> None:
    plan = build_command_plan(
        tmp_path,
        base=TASK12_BASE_COMMIT,
        changed_python=CHANGED,
    )

    assert [command.name for command in plan] == [
        "setup_local",
        "whole_repository_ruff",
        "django_check",
        "migration_check",
        "focused_pytest",
        "full_pytest",
        "changed_file_ruff",
        "ci_material",
        "diff_check",
    ]
    python = str(tmp_path / ".venv" / "Scripts" / "python.exe")
    assert plan[0].argv == (
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        ".\\scripts\\setup-local.ps1",
    )
    assert plan[1].argv == (python, "-m", "ruff", "check", ".")
    assert plan[1].allowed_exit_codes == (0, 1)
    assert all(command.allowed_exit_codes == (0,) for command in plan[2:])
    document = command_plan_document(
        plan,
        base=TASK12_BASE_COMMIT,
        changed_python=CHANGED,
    )
    assert command_plan_sha256(document) == hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def test_legacy_ruff_ruling_is_pinned_not_caller_supplied() -> None:
    assert TASK12_LEGACY_RUFF_RULING_ID == "TASK12-RUFF-LEGACY-001"
    assert TASK12_LEGACY_RUFF_RULING_HASH == hashlib.sha256(
        TASK12_LEGACY_RUFF_RULING_TEXT.encode()
    ).hexdigest()
    assert TASK12_LEGACY_RUFF_RULING_HASH == (
        "93bde2ce215c41956a9dca4a5e652c66674010c747eb5dce1a777ef834d9acb9"
    )


def test_execute_command_records_duration_log_digest_and_parsed_result(
    tmp_path: Path,
) -> None:
    result = execute_command(
        name="full_pytest",
        argv=(sys.executable, "-c", "print('3 passed, 1 skipped in 0.01s')"),
        cwd=tmp_path,
        log_path=tmp_path / "logs" / "probe.log",
        gate=True,
        max_log_bytes=4096,
    )

    assert result.exit_code == 0
    assert result.duration_ms >= 0
    assert result.started_at.endswith("+00:00")
    assert result.finished_at.endswith("+00:00")
    assert result.output_bytes == result.log_bytes
    assert result.output_sha256 == result.log_sha256
    assert result.log_sha256 == hashlib.sha256(
        (tmp_path / "logs" / "probe.log").read_bytes()
    ).hexdigest()
    assert result.parsed_result == {
        "kind": "pytest",
        "passed": 3,
        "failed": 0,
        "skipped": 1,
        "subtests_passed": 0,
        "summary_found": True,
    }


def test_verifier_accepts_exact_logs_plan_result_and_exit_one_ruff(
    tmp_path: Path,
) -> None:
    evidence, project_root = _closed_attempt(tmp_path)

    verified = verify_deterministic_evidence(
        evidence,
        project_root=project_root,
        current_changed_python=CHANGED,
    )

    assert verified.passed is True
    assert verified.attempt_id == "round2-attempt1"
    whole = evidence["commands"][1]
    assert whole["exit_code"] == 1
    assert whole["passed"] is False
    assert whole["parsed_result"]["finding_count"] == 12


def test_verifier_accepts_clean_whole_ruff_without_applying_waiver(
    tmp_path: Path,
) -> None:
    evidence, project_root = _closed_attempt(tmp_path, whole_exit=0)

    verified = verify_deterministic_evidence(
        evidence,
        project_root=project_root,
        current_changed_python=CHANGED,
    )

    assert verified.passed is True
    assert evidence["legacy_ruling"]["applied"] is False
    assert evidence["commands"][1]["passed"] is True
    assert evidence["commands"][1]["disposition"] == "non_gate_clean"


@pytest.mark.parametrize(
    "mutation",
    [
        "fake_ruling",
        "wrong_argv",
        "missing_digest",
        "launch_failure_127",
        "wrong_order",
        "naive_timestamp",
        "outside_log",
        "tampered_log",
        "fake_parsed_result",
        "wrong_plan_hash",
    ],
)
def test_verifier_rejects_closed_evidence_counterexamples(
    tmp_path: Path,
    mutation: str,
) -> None:
    evidence, project_root = _closed_attempt(tmp_path)
    candidate = deepcopy(evidence)
    if mutation == "fake_ruling":
        candidate["legacy_ruling"]["text"] = "invented waiver"
    elif mutation == "wrong_argv":
        candidate["commands"][0]["argv"] = [
            "powershell.exe",
            "-File",
            "forged.ps1",
        ]
    elif mutation == "missing_digest":
        candidate["commands"][2].pop("log_sha256")
    elif mutation == "launch_failure_127":
        candidate["commands"][1]["exit_code"] = 127
    elif mutation == "wrong_order":
        candidate["commands"][0], candidate["commands"][1] = (
            candidate["commands"][1],
            candidate["commands"][0],
        )
    elif mutation == "naive_timestamp":
        candidate["commands"][0]["started_at"] = "2026-08-28T00:00:00"
    elif mutation == "outside_log":
        outside = tmp_path / "outside.log"
        outside.write_bytes(_output_for("django_check"))
        candidate["commands"][2]["log_path"] = str(outside)
    elif mutation == "tampered_log":
        Path(candidate["commands"][2]["log_path"]).write_bytes(b"tampered")
    elif mutation == "fake_parsed_result":
        candidate["commands"][4]["parsed_result"]["passed"] = 9999
    else:
        candidate["plan_sha256"] = "0" * 64

    verified = verify_deterministic_evidence(
        candidate,
        project_root=project_root,
        current_changed_python=CHANGED,
    )

    assert verified.passed is False


def test_exit_one_ruff_requires_positive_parsed_finding_count(tmp_path: Path) -> None:
    evidence, project_root = _closed_attempt(tmp_path)
    ruff_log = Path(evidence["commands"][1]["log_path"])
    payload = b"ruff stopped without a finding summary\n"
    ruff_log.write_bytes(payload)
    evidence["commands"][1].update(
        {
            "output_bytes": len(payload),
            "output_sha256": hashlib.sha256(payload).hexdigest(),
            "log_bytes": len(payload),
            "log_sha256": hashlib.sha256(payload).hexdigest(),
            "parsed_result": {
                "kind": "ruff",
                "finding_count": 0,
                "all_checks_passed": False,
                "summary_found": False,
            },
        }
    )

    verified = verify_deterministic_evidence(
        evidence,
        project_root=project_root,
        current_changed_python=CHANGED,
    )

    assert verified.passed is False


def test_attempt_history_retains_failure_and_selects_latest_exact_pass(
    tmp_path: Path,
) -> None:
    selected, project_root = _closed_attempt(tmp_path, attempt_id="attempt2")
    evidence_root = project_root / "output" / "housing" / "2026-08-28" / "acceptance"
    failed_path = evidence_root / "attempt1-evidence.json"
    failed_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "overall_passed": False,
                "failed_gates": ["full_pytest"],
            }
        ),
        encoding="utf-8",
    )
    selected_path = evidence_root / "attempt2-evidence.json"
    selected_path.write_text(json.dumps(selected), encoding="utf-8")
    history = [
        build_attempt_history_entry("attempt1", failed_path),
        build_attempt_history_entry("attempt2", selected_path),
    ]
    selection = {
        "rule": "latest_complete_exact_plan_pass",
        "selected_attempt_id": "attempt2",
        "eligible_attempt_ids": ["attempt2"],
    }

    verified = verify_attempt_selection(
        history=history,
        selection=selection,
        selected_evidence=selected,
        project_root=project_root,
        current_changed_python=CHANGED,
    )

    assert verified.passed is True
    assert verified.attempt_id == "attempt2"
    assert verified.evidence["history_attempt_ids"] == ["attempt1", "attempt2"]


@pytest.mark.parametrize("mutation", ["wrong_selection", "tampered_history_digest"])
def test_attempt_selection_rejects_history_counterexamples(
    tmp_path: Path,
    mutation: str,
) -> None:
    selected, project_root = _closed_attempt(tmp_path, attempt_id="attempt2")
    evidence_path = (
        project_root
        / "output"
        / "housing"
        / "2026-08-28"
        / "acceptance"
        / "attempt2-evidence.json"
    )
    evidence_path.write_text(json.dumps(selected), encoding="utf-8")
    history = [build_attempt_history_entry("attempt2", evidence_path)]
    selection = {
        "rule": "latest_complete_exact_plan_pass",
        "selected_attempt_id": "attempt2",
        "eligible_attempt_ids": ["attempt2"],
    }
    if mutation == "wrong_selection":
        selection["selected_attempt_id"] = "attempt1"
    else:
        history[0]["evidence_sha256"] = "0" * 64

    verified = verify_attempt_selection(
        history=history,
        selection=selection,
        selected_evidence=selected,
        project_root=project_root,
        current_changed_python=CHANGED,
    )

    assert verified.passed is False
