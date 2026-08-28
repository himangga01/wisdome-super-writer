from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from apps.local_content.acceptance_runner import (
    CommandResult,
    LegacyRuling,
    build_command_plan,
    build_deterministic_evidence,
    execute_command,
)


def _result(name: str, exit_code: int, *, gate: bool = True) -> CommandResult:
    return CommandResult(
        name=name,
        argv=("tool", name),
        cwd="C:/workspace",
        gate=gate,
        started_at="2026-08-28T00:00:00+00:00",
        finished_at="2026-08-28T00:00:01+00:00",
        exit_code=exit_code,
        output_bytes=10,
        output_sha256="a" * 64,
        log_path=f"logs/{name}.log",
        log_truncated=False,
    )


def test_deterministic_evidence_derives_gates_and_legacy_ruling() -> None:
    ruling = LegacyRuling(
        ruling_id="TASK12-RUFF-LEGACY-001",
        text="Whole-repository Ruff debt is pre-existing and non-gating only here.",
    )
    results = [
        _result(name, 0)
        for name in (
            "setup_local",
            "django_check",
            "migration_check",
            "focused_pytest",
            "full_pytest",
            "changed_file_ruff",
            "ci_material",
            "diff_check",
        )
    ]
    results.append(_result("whole_repository_ruff", 1, gate=False))

    evidence = build_deterministic_evidence(results, ruling=ruling)

    assert evidence["overall_passed"] is True
    whole = next(
        row for row in evidence["commands"] if row["name"] == "whole_repository_ruff"
    )
    assert whole["passed"] is False
    assert whole["disposition"] == "legacy_debt_not_gate"
    assert evidence["legacy_ruling"] == {
        "id": ruling.ruling_id,
        "text": ruling.text,
        "sha256": hashlib.sha256(ruling.text.encode()).hexdigest(),
        "applied": True,
    }


def test_deterministic_evidence_fails_when_any_real_gate_fails() -> None:
    ruling = LegacyRuling("TASK12-RUFF-LEGACY-001", "explicit debt ruling")
    results = [
        _result("setup_local", 0),
        _result("full_pytest", 1),
        _result("whole_repository_ruff", 1, gate=False),
    ]

    evidence = build_deterministic_evidence(results, ruling=ruling)

    assert evidence["overall_passed"] is False
    assert evidence["failed_gates"] == ["full_pytest"]


def test_execute_command_records_actual_argv_exit_and_bounded_log(tmp_path: Path) -> None:
    result = execute_command(
        name="probe",
        argv=(sys.executable, "-c", "print('x' * 2048)"),
        cwd=tmp_path,
        log_path=tmp_path / "probe.log",
        gate=True,
        max_log_bytes=128,
    )

    assert result.exit_code == 0
    assert result.argv == (sys.executable, "-c", "print('x' * 2048)")
    assert result.output_bytes > 2048
    assert result.output_sha256 != "0" * 64
    assert result.log_truncated is True
    assert (tmp_path / "probe.log").stat().st_size < 512


def test_command_plan_contains_exact_authoritative_gate_argv(tmp_path: Path) -> None:
    changed = ("src/apps/local_content/acceptance.py", "tests/unit/test_probe.py")

    plan = build_command_plan(tmp_path, base="711c038", changed_python=changed)

    by_name = {command.name: command for command in plan}
    python = str(tmp_path / ".venv" / "Scripts" / "python.exe")
    assert by_name["setup_local"].argv == (
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        ".\\scripts\\setup-local.ps1",
    )
    assert by_name["django_check"].argv == (python, "src\\manage.py", "check")
    assert by_name["migration_check"].argv[-3:] == (
        "makemigrations",
        "--check",
        "--dry-run",
    )
    assert by_name["full_pytest"].argv == (python, "-m", "pytest", "-q")
    assert by_name["changed_file_ruff"].argv[:4] == (
        python,
        "-m",
        "ruff",
        "check",
    )
    assert by_name["changed_file_ruff"].argv[4:] == changed
    assert by_name["ci_material"].argv[-4:] == (
        "--event",
        "explicit",
        "--base",
        "711c038",
    )
    assert by_name["diff_check"].argv == ("git", "diff", "--check", "711c038")
