"""Subprocess-backed deterministic evidence for Task 12 acceptance."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

_FOCUSED_TESTS = (
    "tests\\unit\\test_queue_configuration.py",
    "tests\\unit\\test_local_runtime.py",
    "tests\\unit\\test_local_content_dates.py",
    "tests\\unit\\test_applyhome_public_html.py",
    "tests\\unit\\test_lh_public_html.py",
    "tests\\unit\\test_local_content_selection.py",
    "tests\\unit\\test_local_content_rendering.py",
    "tests\\unit\\test_local_content_images.py",
    "tests\\unit\\test_local_content_humanizer.py",
    "tests\\unit\\test_local_content_bundles.py",
    "tests\\unit\\test_local_content_preview.py",
    "tests\\unit\\test_task12_deterministic_runner.py",
    "tests\\unit\\test_sqlite_test_cleanup.py",
    "tests\\integration\\test_local_housing_workflow.py",
)


@dataclass(frozen=True)
class LegacyRuling:
    ruling_id: str
    text: str


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    gate: bool = True


@dataclass(frozen=True)
class CommandResult:
    name: str
    argv: tuple[str, ...]
    cwd: str
    gate: bool
    started_at: str
    finished_at: str
    exit_code: int
    output_bytes: int
    output_sha256: str
    log_path: str
    log_truncated: bool


def build_command_plan(
    project_root: Path,
    *,
    base: str,
    changed_python: tuple[str, ...],
) -> tuple[CommandSpec, ...]:
    """Return the exact argv that the authoritative runner must execute."""

    python = str(Path(project_root) / ".venv" / "Scripts" / "python.exe")
    return (
        CommandSpec(
            "setup_local",
            (
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                ".\\scripts\\setup-local.ps1",
            ),
        ),
        CommandSpec(
            "whole_repository_ruff",
            (python, "-m", "ruff", "check", "."),
            gate=False,
        ),
        CommandSpec("django_check", (python, "src\\manage.py", "check")),
        CommandSpec(
            "migration_check",
            (
                python,
                "src\\manage.py",
                "makemigrations",
                "--check",
                "--dry-run",
            ),
        ),
        CommandSpec(
            "focused_pytest",
            (python, "-m", "pytest", *_FOCUSED_TESTS, "-q"),
        ),
        CommandSpec("full_pytest", (python, "-m", "pytest", "-q")),
        CommandSpec(
            "changed_file_ruff",
            (python, "-m", "ruff", "check", *changed_python),
        ),
        CommandSpec(
            "ci_material",
            (
                python,
                "scripts\\ci_changed_python.py",
                "--event",
                "explicit",
                "--base",
                base,
            ),
        ),
        CommandSpec("diff_check", ("git", "diff", "--check", base)),
    )


def discover_changed_python(project_root: Path, *, base: str) -> tuple[str, ...]:
    """Include branch/worktree Python plus every local-content Python module."""

    root = Path(project_root)
    candidates: set[str] = {
        path.relative_to(root).as_posix()
        for path in (root / "src" / "apps" / "local_content").rglob("*.py")
        if path.is_file()
    }
    for argv in (
        ("git", "diff", "--name-only", "--diff-filter=ACMR", base),
        ("git", "diff", "--name-only", "--diff-filter=ACMR"),
        ("git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"),
        ("git", "ls-files", "--others", "--exclude-standard"),
    ):
        completed = subprocess.run(
            argv,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        for line in completed.stdout.splitlines():
            relative = line.strip().replace("\\", "/")
            if relative.endswith(".py") and (root / relative).is_file():
                candidates.add(relative)
    if not candidates:
        raise ValueError("changed/local-content Ruff target set is empty")
    return tuple(sorted(candidates))


def execute_command(
    *,
    name: str,
    argv: tuple[str, ...],
    cwd: Path,
    log_path: Path,
    gate: bool,
    environment: Mapping[str, str] | None = None,
    max_log_bytes: int = 2 * 1024 * 1024,
) -> CommandResult:
    """Execute argv without a shell and retain a bounded, hashed combined log."""

    if not argv or max_log_bytes < 1:
        raise ValueError("command argv and positive log bound are required")
    started = datetime.now(UTC)
    digest = hashlib.sha256()
    output_bytes = 0
    retained = 0
    truncated = False
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process_environment = os.environ.copy()
    if environment is not None:
        process_environment.update(environment)
    exit_code = 127
    with log.open("wb") as destination:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=process_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                creationflags=creationflags,
            )
            assert process.stdout is not None
            while chunk := process.stdout.read(64 * 1024):
                digest.update(chunk)
                output_bytes += len(chunk)
                if retained < max_log_bytes:
                    portion = chunk[: max_log_bytes - retained]
                    destination.write(portion)
                    retained += len(portion)
                if retained == max_log_bytes and output_bytes > retained:
                    truncated = True
            exit_code = process.wait()
        except OSError as exc:
            material = f"command launch failed: {exc.__class__.__name__}\n".encode("ascii")
            digest.update(material)
            output_bytes += len(material)
            destination.write(material[:max_log_bytes])
            truncated = len(material) > max_log_bytes
        if truncated:
            destination.write(b"\n[bounded log truncated; digest covers full output]\n")
    finished = datetime.now(UTC)
    return CommandResult(
        name=name,
        argv=argv,
        cwd=str(Path(cwd).resolve()),
        gate=gate,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        exit_code=exit_code,
        output_bytes=output_bytes,
        output_sha256=digest.hexdigest(),
        log_path=str(log.resolve()),
        log_truncated=truncated,
    )


def build_deterministic_evidence(
    results: list[CommandResult],
    *,
    ruling: LegacyRuling,
) -> dict[str, object]:
    """Derive every verdict from command results and one explicit debt ruling."""

    if not ruling.ruling_id or not ruling.text:
        raise ValueError("legacy ruling ID and text are required")
    command_rows: list[dict[str, object]] = []
    failed_gates: list[str] = []
    unknown_non_gate_failures: list[str] = []
    ruling_applied = False
    for result in results:
        row = asdict(result)
        row["argv"] = list(result.argv)
        passed = result.exit_code == 0
        row["passed"] = passed
        if result.gate:
            row["disposition"] = "gate_passed" if passed else "gate_failed"
            if not passed:
                failed_gates.append(result.name)
        elif result.name == "whole_repository_ruff" and not passed:
            row["disposition"] = "legacy_debt_not_gate"
            ruling_applied = True
        elif passed:
            row["disposition"] = "non_gate_clean"
        else:
            row["disposition"] = "unruled_non_gate_failure"
            unknown_non_gate_failures.append(result.name)
        command_rows.append(row)
    required_names = {spec.name for spec in _required_plan_shape()}
    observed_names = [result.name for result in results]
    exact_plan = len(observed_names) == len(set(observed_names)) and set(
        observed_names
    ) == required_names
    whole = next(
        (result for result in results if result.name == "whole_repository_ruff"),
        None,
    )
    ruling_needed = whole is not None and whole.exit_code != 0
    ruling_valid = not ruling_needed or ruling_applied
    return {
        "schema_version": 1,
        "commands": command_rows,
        "failed_gates": failed_gates,
        "exact_command_plan_executed": exact_plan,
        "legacy_ruling": {
            "id": ruling.ruling_id,
            "text": ruling.text,
            "sha256": hashlib.sha256(ruling.text.encode("utf-8")).hexdigest(),
            "applied": ruling_applied,
        },
        "overall_passed": exact_plan
        and not failed_gates
        and not unknown_non_gate_failures
        and ruling_valid,
    }


def _required_plan_shape() -> tuple[CommandSpec, ...]:
    return build_command_plan(Path("C:/shape-only"), base="base", changed_python=("x.py",))
