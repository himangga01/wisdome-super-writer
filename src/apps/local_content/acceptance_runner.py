"""Closed subprocess evidence and independent verification for Task 12."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

TASK12_BASE_COMMIT = "711c03869674fbb12cb0554f31552da60927e664"
TASK12_LEGACY_RUFF_RULING_ID = "TASK12-RUFF-LEGACY-001"
TASK12_LEGACY_RUFF_RULING_TEXT = (
    "At base 711c03869674fbb12cb0554f31552da60927e664, whole-repository Ruff "
    "findings are ledgered legacy debt. Only the exact whole_repository_ruff command "
    "may exit 1 with a parsed positive finding count and remain non-gating; it must "
    "stay passed=false. Every other command is a binding gate."
)
TASK12_LEGACY_RUFF_RULING_HASH = (
    "93bde2ce215c41956a9dca4a5e652c66674010c747eb5dce1a777ef834d9acb9"
)
TASK12_ATTEMPT_SELECTION_RULE = "latest_complete_exact_plan_pass"

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
    "tests\\unit\\test_local_content_acceptance.py",
    "tests\\unit\\test_task12_deterministic_runner.py",
    "tests\\unit\\test_sqlite_test_cleanup.py",
    "tests\\integration\\test_local_housing_workflow.py",
)
_ATTEMPT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_PARSE_TAIL_BYTES = 256 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    gate: bool
    allowed_exit_codes: tuple[int, ...]
    parser: str


@dataclass(frozen=True)
class CommandResult:
    name: str
    argv: tuple[str, ...]
    cwd: str
    gate: bool
    started_at: str
    finished_at: str
    duration_ms: int
    exit_code: int
    output_bytes: int
    output_sha256: str
    log_path: str
    log_bytes: int
    log_sha256: str
    log_truncated: bool
    parsed_result: dict[str, object]


@dataclass(frozen=True)
class DeterministicVerification:
    passed: bool
    attempt_id: str | None
    failures: tuple[str, ...]
    evidence: dict[str, object]


def build_command_plan(
    project_root: Path,
    *,
    base: str,
    changed_python: tuple[str, ...],
) -> tuple[CommandSpec, ...]:
    """Return the canonical ordered command contract for Task 12."""

    if base != TASK12_BASE_COMMIT:
        raise ValueError("Task 12 base commit does not match the pinned contract")
    if (
        not changed_python
        or tuple(sorted(set(changed_python))) != changed_python
        or not all(isinstance(path, str) and path.endswith(".py") for path in changed_python)
    ):
        raise ValueError("changed Python targets must be nonempty, unique, and sorted")
    python = str(Path(project_root).resolve() / ".venv" / "Scripts" / "python.exe")
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
            True,
            (0,),
            "setup",
        ),
        CommandSpec(
            "whole_repository_ruff",
            (python, "-m", "ruff", "check", "."),
            False,
            (0, 1),
            "ruff",
        ),
        CommandSpec(
            "django_check",
            (python, "src\\manage.py", "check"),
            True,
            (0,),
            "django_check",
        ),
        CommandSpec(
            "migration_check",
            (
                python,
                "src\\manage.py",
                "makemigrations",
                "--check",
                "--dry-run",
            ),
            True,
            (0,),
            "migration_check",
        ),
        CommandSpec(
            "focused_pytest",
            (python, "-m", "pytest", *_FOCUSED_TESTS, "-q"),
            True,
            (0,),
            "pytest",
        ),
        CommandSpec(
            "full_pytest",
            (python, "-m", "pytest", "-q"),
            True,
            (0,),
            "pytest",
        ),
        CommandSpec(
            "changed_file_ruff",
            (python, "-m", "ruff", "check", *changed_python),
            True,
            (0,),
            "ruff",
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
            True,
            (0,),
            "ruff",
        ),
        CommandSpec(
            "diff_check",
            ("git", "diff", "--check", base),
            True,
            (0,),
            "diff",
        ),
    )


def command_plan_document(
    plan: tuple[CommandSpec, ...],
    *,
    base: str,
    changed_python: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "base": base,
        "changed_python": list(changed_python),
        "commands": [
            {
                "index": index,
                "name": spec.name,
                "argv": list(spec.argv),
                "gate": spec.gate,
                "allowed_exit_codes": list(spec.allowed_exit_codes),
                "parser": spec.parser,
            }
            for index, spec in enumerate(plan, start=1)
        ],
    }


def command_plan_sha256(document: dict[str, object]) -> str:
    return _canonical_sha256(document)


def discover_changed_python(project_root: Path, *, base: str) -> tuple[str, ...]:
    """Include branch/worktree Python plus every local-content Python module."""

    if base != TASK12_BASE_COMMIT:
        raise ValueError("Task 12 base commit does not match the pinned contract")
    root = Path(project_root).resolve()
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
    """Execute argv without a shell and retain closed, bounded evidence."""

    if not argv or max_log_bytes < 1:
        raise ValueError("command argv and positive log bound are required")
    started = datetime.now(UTC)
    digest = hashlib.sha256()
    output_bytes = 0
    retained = 0
    truncated = False
    parse_tail = bytearray()
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
                parse_tail.extend(chunk)
                if len(parse_tail) > _MAX_PARSE_TAIL_BYTES:
                    del parse_tail[: len(parse_tail) - _MAX_PARSE_TAIL_BYTES]
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
            parse_tail.extend(material)
            destination.write(material[:max_log_bytes])
            truncated = len(material) > max_log_bytes
        if truncated:
            destination.write(b"\n[bounded log truncated; digest covers full output]\n")
    finished = datetime.now(UTC)
    log_payload = log.read_bytes()
    duration_ms = max(0, round((finished - started).total_seconds() * 1000))
    return CommandResult(
        name=name,
        argv=argv,
        cwd=str(Path(cwd).resolve()),
        gate=gate,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_ms=duration_ms,
        exit_code=exit_code,
        output_bytes=output_bytes,
        output_sha256=digest.hexdigest(),
        log_path=str(log.resolve()),
        log_bytes=len(log_payload),
        log_sha256=hashlib.sha256(log_payload).hexdigest(),
        log_truncated=truncated,
        parsed_result=parse_command_output(name, bytes(parse_tail), exit_code),
    )


def parse_command_output(
    name: str,
    output: bytes,
    exit_code: int,
) -> dict[str, object]:
    """Parse one bounded command result into a closed, body-free summary."""

    text = output.decode("utf-8", errors="replace")
    if exit_code == 127 and "command launch failed:" in text:
        return {
            "kind": "launch_failure",
            "summary_found": True,
        }
    if name == "setup_local":
        return {
            "kind": "setup",
            "local_setup_completed": "Local setup completed." in text,
            "system_check_clean": "System check identified no issues" in text,
            "migrations_clean": (
                "No migrations to apply." in text or "No changes detected" in text
            ),
        }
    if name in {"whole_repository_ruff", "changed_file_ruff", "ci_material"}:
        findings = re.findall(r"Found (?P<count>\d+) errors?\.", text)
        return {
            "kind": "ruff",
            "finding_count": int(findings[-1]) if findings else 0,
            "all_checks_passed": "All checks passed!" in text,
            "summary_found": bool(findings) or "All checks passed!" in text,
        }
    if name == "django_check":
        return {
            "kind": "django_check",
            "system_check_clean": "System check identified no issues" in text,
        }
    if name == "migration_check":
        return {
            "kind": "migration_check",
            "no_changes_detected": "No changes detected" in text,
        }
    if name in {"focused_pytest", "full_pytest"}:
        passed = re.search(r"(?<!subtests )(?P<count>\d+) passed\b", text)
        failed = re.search(r"(?P<count>\d+) failed\b", text)
        skipped = re.search(r"(?P<count>\d+) skipped\b", text)
        subtests = re.search(r"(?P<count>\d+) subtests passed\b", text)
        return {
            "kind": "pytest",
            "passed": int(passed.group("count")) if passed else 0,
            "failed": int(failed.group("count")) if failed else 0,
            "skipped": int(skipped.group("count")) if skipped else 0,
            "subtests_passed": int(subtests.group("count")) if subtests else 0,
            "summary_found": passed is not None,
        }
    if name == "diff_check":
        return {"kind": "diff", "clean": output == b""}
    return {"kind": "unknown", "summary_found": False}


def build_deterministic_evidence(
    results: list[CommandResult],
    *,
    attempt_id: str,
    project_root: Path,
    base: str,
    changed_python: tuple[str, ...],
    log_root: Path,
) -> dict[str, object]:
    """Build schema-v2 evidence while leaving final trust to the verifier."""

    if _ATTEMPT_ID.fullmatch(attempt_id) is None:
        raise ValueError("attempt ID must be a safe stable identifier")
    root = Path(project_root).resolve()
    plan = build_command_plan(root, base=base, changed_python=changed_python)
    plan_document = command_plan_document(
        plan,
        base=base,
        changed_python=changed_python,
    )
    rows: list[dict[str, object]] = []
    failed_gates: list[str] = []
    ruling_applied = False
    for result in results:
        row = asdict(result)
        row["argv"] = list(result.argv)
        passed, disposition = _result_truth(result.name, result.exit_code, result.parsed_result)
        row["passed"] = passed
        row["disposition"] = disposition
        if result.gate and not passed:
            failed_gates.append(result.name)
        if disposition == "legacy_debt_not_gate":
            ruling_applied = True
        rows.append(row)
    exact_plan = len(results) == len(plan) and all(
        result.name == spec.name
        and result.argv == spec.argv
        and result.gate == spec.gate
        for result, spec in zip(results, plan, strict=False)
    )
    whole_valid = any(
        row["name"] == "whole_repository_ruff"
        and row["disposition"] in {"legacy_debt_not_gate", "non_gate_clean"}
        for row in rows
    )
    attempt_started = results[0].started_at if results else None
    attempt_finished = results[-1].finished_at if results else None
    attempt_duration = (
        _duration_ms(attempt_started, attempt_finished)
        if attempt_started is not None and attempt_finished is not None
        else None
    )
    overall = exact_plan and not failed_gates and whole_valid
    return {
        "schema_version": 2,
        "attempt_id": attempt_id,
        "project_root": str(root),
        "base": base,
        "changed_python": list(changed_python),
        "log_root": str(Path(log_root).resolve()),
        "plan": plan_document,
        "plan_sha256": command_plan_sha256(plan_document),
        "attempt_started_at": attempt_started,
        "attempt_finished_at": attempt_finished,
        "attempt_duration_ms": attempt_duration,
        "commands": rows,
        "failed_gates": failed_gates,
        "exact_command_plan_executed": exact_plan,
        "legacy_ruling": {
            "id": TASK12_LEGACY_RUFF_RULING_ID,
            "text": TASK12_LEGACY_RUFF_RULING_TEXT,
            "sha256": TASK12_LEGACY_RUFF_RULING_HASH,
            "applied": ruling_applied,
        },
        "overall_passed": overall,
    }


def verify_deterministic_evidence(
    evidence: object,
    *,
    project_root: Path,
    current_changed_python: tuple[str, ...] | None = None,
) -> DeterministicVerification:
    """Independently verify plan, logs, parsing, timestamps, exits, and ruling."""

    failures: list[str] = []
    root = Path(project_root).resolve()
    if not isinstance(evidence, dict):
        return DeterministicVerification(False, None, ("INVALID_EVIDENCE_OBJECT",), {})
    attempt_id = evidence.get("attempt_id")
    if not isinstance(attempt_id, str) or _ATTEMPT_ID.fullmatch(attempt_id) is None:
        failures.append("INVALID_ATTEMPT_ID")
        stable_attempt_id = None
    else:
        stable_attempt_id = attempt_id
    expected_keys = {
        "schema_version",
        "attempt_id",
        "project_root",
        "base",
        "changed_python",
        "log_root",
        "plan",
        "plan_sha256",
        "attempt_started_at",
        "attempt_finished_at",
        "attempt_duration_ms",
        "commands",
        "failed_gates",
        "exact_command_plan_executed",
        "legacy_ruling",
        "overall_passed",
    }
    if set(evidence) != expected_keys or evidence.get("schema_version") != 2:
        failures.append("INVALID_EVIDENCE_SCHEMA")
    if evidence.get("project_root") != str(root):
        failures.append("PROJECT_ROOT_MISMATCH")
    if evidence.get("base") != TASK12_BASE_COMMIT:
        failures.append("BASE_MISMATCH")
    raw_changed = evidence.get("changed_python")
    changed = tuple(raw_changed) if isinstance(raw_changed, list) else ()
    if current_changed_python is None:
        try:
            current_changed_python = discover_changed_python(
                root,
                base=TASK12_BASE_COMMIT,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            current_changed_python = ()
            failures.append("CHANGED_PYTHON_DISCOVERY_FAILED")
    if changed != current_changed_python:
        failures.append("CHANGED_PYTHON_MISMATCH")
    try:
        plan = build_command_plan(
            root,
            base=TASK12_BASE_COMMIT,
            changed_python=current_changed_python,
        )
        plan_document = command_plan_document(
            plan,
            base=TASK12_BASE_COMMIT,
            changed_python=current_changed_python,
        )
    except ValueError:
        plan = ()
        plan_document = {}
        failures.append("EXPECTED_PLAN_BUILD_FAILED")
    if evidence.get("plan") != plan_document:
        failures.append("PLAN_MISMATCH")
    if evidence.get("plan_sha256") != command_plan_sha256(plan_document):
        failures.append("PLAN_HASH_MISMATCH")
    ruling = evidence.get("legacy_ruling")
    if not isinstance(ruling, dict) or {
        key: ruling.get(key)
        for key in ("id", "text", "sha256")
    } != {
        "id": TASK12_LEGACY_RUFF_RULING_ID,
        "text": TASK12_LEGACY_RUFF_RULING_TEXT,
        "sha256": TASK12_LEGACY_RUFF_RULING_HASH,
    } or type(ruling.get("applied")) is not bool:
        failures.append("RUFF_RULING_MISMATCH")
    log_root = _confined_log_root(evidence.get("log_root"), root, failures)
    commands = evidence.get("commands")
    if not isinstance(commands, list) or len(commands) != len(plan):
        failures.append("COMMAND_COUNT_MISMATCH")
        commands = []
    failed_gates: list[str] = []
    previous_finished: datetime | None = None
    seen_logs: set[Path] = set()
    whole_valid = False
    waiver_applied = False
    for index, spec in enumerate(plan):
        if index >= len(commands) or not isinstance(commands[index], dict):
            failures.append(f"COMMAND_{index + 1}_MISSING")
            continue
        row = commands[index]
        if set(row) != _command_row_keys():
            failures.append(f"COMMAND_{index + 1}_SCHEMA")
        if (
            row.get("name") != spec.name
            or row.get("argv") != list(spec.argv)
            or row.get("cwd") != str(root)
            or row.get("gate") is not spec.gate
        ):
            failures.append(f"COMMAND_{index + 1}_PLAN")
        started = _aware_datetime(row.get("started_at"))
        finished = _aware_datetime(row.get("finished_at"))
        if started is None or finished is None or finished < started:
            failures.append(f"COMMAND_{index + 1}_TIMESTAMP")
        else:
            if previous_finished is not None and started < previous_finished:
                failures.append(f"COMMAND_{index + 1}_ORDER_TIME")
            previous_finished = finished
            expected_duration = round((finished - started).total_seconds() * 1000)
            if row.get("duration_ms") != expected_duration:
                failures.append(f"COMMAND_{index + 1}_DURATION")
        exit_code = row.get("exit_code")
        if type(exit_code) is not int or exit_code not in spec.allowed_exit_codes:
            failures.append(f"COMMAND_{index + 1}_EXIT")
            exit_code = 127
        payload = _verified_log_payload(
            row,
            log_root,
            seen_logs,
            failures,
            index=index + 1,
        )
        parsed = parse_command_output(spec.name, payload, exit_code)
        if row.get("parsed_result") != parsed:
            failures.append(f"COMMAND_{index + 1}_PARSED_RESULT")
        passed, disposition = _result_truth(spec.name, exit_code, parsed)
        if row.get("passed") is not passed or row.get("disposition") != disposition:
            failures.append(f"COMMAND_{index + 1}_TRUTH")
        if spec.gate and not passed:
            failed_gates.append(spec.name)
        if spec.name == "whole_repository_ruff":
            whole_valid = disposition in {"legacy_debt_not_gate", "non_gate_clean"}
            waiver_applied = disposition == "legacy_debt_not_gate"
            if exit_code == 1:
                if parsed.get("finding_count", 0) <= 0:
                    failures.append("RUFF_WAIVER_FINDINGS_MISSING")
                if row.get("passed") is not False:
                    failures.append("RUFF_WAIVER_PASSED_TRUE")
            elif exit_code != 0:
                failures.append("RUFF_WAIVER_EXIT_NOT_ALLOWED")
    if not isinstance(ruling, dict) or ruling.get("applied") is not waiver_applied:
        failures.append("RUFF_RULING_APPLICATION_MISMATCH")
    attempt_started = _aware_datetime(evidence.get("attempt_started_at"))
    attempt_finished = _aware_datetime(evidence.get("attempt_finished_at"))
    if (
        not commands
        or attempt_started is None
        or attempt_finished is None
        or evidence.get("attempt_started_at") != commands[0].get("started_at")
        or evidence.get("attempt_finished_at") != commands[-1].get("finished_at")
        or evidence.get("attempt_duration_ms")
        != round((attempt_finished - attempt_started).total_seconds() * 1000)
    ):
        failures.append("ATTEMPT_TIMELINE_MISMATCH")
    derived_overall = not failed_gates and whole_valid and not failures
    if evidence.get("failed_gates") != failed_gates:
        failures.append("FAILED_GATES_MISMATCH")
    if evidence.get("exact_command_plan_executed") is not True:
        failures.append("EXACT_PLAN_FALSE")
    if evidence.get("overall_passed") is not derived_overall:
        failures.append("OVERALL_TRUTH_MISMATCH")
    passed = derived_overall and not failures
    return DeterministicVerification(
        passed=passed,
        attempt_id=stable_attempt_id,
        failures=tuple(dict.fromkeys(failures)),
        evidence={
            "attempt_id": stable_attempt_id,
            "plan_sha256": evidence.get("plan_sha256"),
            "verified_command_count": len(plan) if passed else 0,
            "failures": list(dict.fromkeys(failures)),
        },
    )


def build_attempt_history_entry(
    attempt_id: str,
    evidence_path: Path,
) -> dict[str, object]:
    """Bind one retained attempt file without promoting its self-reported verdict."""

    if _ATTEMPT_ID.fullmatch(attempt_id) is None:
        raise ValueError("attempt ID must be a safe stable identifier")
    path = Path(evidence_path).resolve(strict=True)
    payload = path.read_bytes()
    parsed = json.loads(payload.decode("utf-8", errors="strict"))
    if not isinstance(parsed, dict):
        raise ValueError("attempt evidence must contain a JSON object")
    failed_gates = parsed.get("failed_gates")
    return {
        "attempt_id": attempt_id,
        "evidence_path": str(path),
        "evidence_bytes": len(payload),
        "evidence_sha256": hashlib.sha256(payload).hexdigest(),
        "schema_version": parsed.get("schema_version"),
        "overall_passed": parsed.get("overall_passed") is True,
        "failed_gates": failed_gates if isinstance(failed_gates, list) else [],
    }


def verify_attempt_selection(
    *,
    history: object,
    selection: object,
    selected_evidence: object,
    project_root: Path,
    current_changed_python: tuple[str, ...] | None = None,
) -> DeterministicVerification:
    """Verify retained attempt files and the exact latest-eligible selection rule."""

    failures: list[str] = []
    root = Path(project_root).resolve()
    if not isinstance(history, list) or not history:
        return DeterministicVerification(False, None, ("ATTEMPT_HISTORY_MISSING",), {})
    if not isinstance(selection, dict) or set(selection) != {
        "rule",
        "selected_attempt_id",
        "eligible_attempt_ids",
    }:
        failures.append("ATTEMPT_SELECTION_SCHEMA")
        selection = {}
    if selection.get("rule") != TASK12_ATTEMPT_SELECTION_RULE:
        failures.append("ATTEMPT_SELECTION_RULE")
    history_ids: list[str] = []
    eligible_ids: list[str] = []
    parsed_by_id: dict[str, dict[str, object]] = {}
    for index, entry in enumerate(history, start=1):
        if not isinstance(entry, dict) or set(entry) != {
            "attempt_id",
            "evidence_path",
            "evidence_bytes",
            "evidence_sha256",
            "schema_version",
            "overall_passed",
            "failed_gates",
        }:
            failures.append(f"ATTEMPT_HISTORY_{index}_SCHEMA")
            continue
        attempt_id = entry.get("attempt_id")
        if (
            not isinstance(attempt_id, str)
            or _ATTEMPT_ID.fullmatch(attempt_id) is None
            or attempt_id in history_ids
        ):
            failures.append(f"ATTEMPT_HISTORY_{index}_ID")
            continue
        history_ids.append(attempt_id)
        payload = _confined_history_payload(
            entry.get("evidence_path"),
            root,
            failures,
            index=index,
        )
        digest = hashlib.sha256(payload).hexdigest()
        if (
            entry.get("evidence_bytes") != len(payload)
            or entry.get("evidence_sha256") != digest
        ):
            failures.append(f"ATTEMPT_HISTORY_{index}_DIGEST")
        try:
            parsed = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeError, ValueError):
            failures.append(f"ATTEMPT_HISTORY_{index}_JSON")
            continue
        if not isinstance(parsed, dict):
            failures.append(f"ATTEMPT_HISTORY_{index}_OBJECT")
            continue
        parsed_by_id[attempt_id] = parsed
        failed_gates = parsed.get("failed_gates")
        if (
            entry.get("schema_version") != parsed.get("schema_version")
            or entry.get("overall_passed") is not (parsed.get("overall_passed") is True)
            or entry.get("failed_gates")
            != (failed_gates if isinstance(failed_gates, list) else [])
        ):
            failures.append(f"ATTEMPT_HISTORY_{index}_SUMMARY")
        if parsed.get("schema_version") == 2:
            if parsed.get("attempt_id") != attempt_id:
                failures.append(f"ATTEMPT_HISTORY_{index}_BOUND_ID")
                continue
            verified = verify_deterministic_evidence(
                parsed,
                project_root=root,
                current_changed_python=current_changed_python,
            )
            if verified.passed:
                eligible_ids.append(attempt_id)
    selected_id = selection.get("selected_attempt_id")
    if (
        selection.get("eligible_attempt_ids") != eligible_ids
        or not eligible_ids
        or selected_id != eligible_ids[-1]
    ):
        failures.append("ATTEMPT_SELECTION_NOT_LATEST_ELIGIBLE")
    if not isinstance(selected_id, str) or parsed_by_id.get(selected_id) != selected_evidence:
        failures.append("ATTEMPT_SELECTION_EVIDENCE_MISMATCH")
    return DeterministicVerification(
        passed=not failures,
        attempt_id=selected_id if isinstance(selected_id, str) else None,
        failures=tuple(dict.fromkeys(failures)),
        evidence={
            "history_attempt_ids": history_ids,
            "eligible_attempt_ids": eligible_ids,
            "selected_attempt_id": selected_id,
            "selection_rule": selection.get("rule"),
            "failures": list(dict.fromkeys(failures)),
        },
    )


def _result_truth(
    name: str,
    exit_code: int,
    parsed: Mapping[str, object],
) -> tuple[bool, str]:
    if name == "whole_repository_ruff":
        if (
            exit_code == 1
            and parsed.get("kind") == "ruff"
            and type(parsed.get("finding_count")) is int
            and parsed["finding_count"] > 0
            and parsed.get("summary_found") is True
        ):
            return False, "legacy_debt_not_gate"
        if exit_code == 0 and parsed.get("all_checks_passed") is True:
            return True, "non_gate_clean"
        return False, "unruled_non_gate_failure"
    passed = exit_code == 0 and _parsed_gate_success(name, parsed)
    return passed, "gate_passed" if passed else "gate_failed"


def _confined_history_payload(
    value: object,
    root: Path,
    failures: list[str],
    *,
    index: int,
) -> bytes:
    if not isinstance(value, str):
        failures.append(f"ATTEMPT_HISTORY_{index}_PATH")
        return b""
    try:
        path = Path(value)
        resolved = path.resolve(strict=True)
        resolved.relative_to((root / "output" / "housing").resolve())
        metadata = os.lstat(resolved)
        if not stat.S_ISREG(metadata.st_mode) or _is_reparse(metadata):
            raise ValueError
        if _normalized_absolute(path) != _normalized_absolute(resolved):
            raise ValueError
        return resolved.read_bytes()
    except (OSError, ValueError):
        failures.append(f"ATTEMPT_HISTORY_{index}_NOT_CONFINED")
        return b""


def _parsed_gate_success(name: str, parsed: Mapping[str, object]) -> bool:
    if name == "setup_local":
        return all(
            parsed.get(key) is True
            for key in (
                "local_setup_completed",
                "system_check_clean",
                "migrations_clean",
            )
        )
    if name == "django_check":
        return parsed.get("system_check_clean") is True
    if name == "migration_check":
        return parsed.get("no_changes_detected") is True
    if name in {"focused_pytest", "full_pytest"}:
        return (
            parsed.get("summary_found") is True
            and parsed.get("failed") == 0
            and type(parsed.get("passed")) is int
            and parsed["passed"] > 0
        )
    if name in {"changed_file_ruff", "ci_material"}:
        return parsed.get("all_checks_passed") is True
    if name == "diff_check":
        return parsed.get("clean") is True
    return False


def _confined_log_root(value: object, root: Path, failures: list[str]) -> Path | None:
    if not isinstance(value, str):
        failures.append("LOG_ROOT_INVALID")
        return None
    try:
        path = Path(value)
        resolved = path.resolve(strict=True)
        resolved.relative_to((root / "output" / "housing").resolve())
        metadata = os.lstat(resolved)
        if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
            raise ValueError
        if _normalized_absolute(path) != _normalized_absolute(resolved):
            raise ValueError
    except (OSError, ValueError):
        failures.append("LOG_ROOT_NOT_CONFINED")
        return None
    return resolved


def _verified_log_payload(
    row: dict[str, object],
    log_root: Path | None,
    seen_logs: set[Path],
    failures: list[str],
    *,
    index: int,
) -> bytes:
    if log_root is None or not isinstance(row.get("log_path"), str):
        failures.append(f"COMMAND_{index}_LOG_PATH")
        return b""
    try:
        path = Path(row["log_path"])
        resolved = path.resolve(strict=True)
        resolved.relative_to(log_root)
        metadata = os.lstat(resolved)
        if not stat.S_ISREG(metadata.st_mode) or _is_reparse(metadata):
            raise ValueError
        if _normalized_absolute(path) != _normalized_absolute(resolved):
            raise ValueError
        if resolved in seen_logs:
            raise ValueError
        seen_logs.add(resolved)
        payload = resolved.read_bytes()
    except (OSError, ValueError):
        failures.append(f"COMMAND_{index}_LOG_NOT_CONFINED")
        return b""
    digest = hashlib.sha256(payload).hexdigest()
    if (
        row.get("log_truncated") is not False
        or row.get("log_bytes") != len(payload)
        or row.get("log_sha256") != digest
        or row.get("output_bytes") != len(payload)
        or row.get("output_sha256") != digest
    ):
        failures.append(f"COMMAND_{index}_LOG_DIGEST")
    return payload


def _command_row_keys() -> set[str]:
    return {
        "name",
        "argv",
        "cwd",
        "gate",
        "started_at",
        "finished_at",
        "duration_ms",
        "exit_code",
        "output_bytes",
        "output_sha256",
        "log_path",
        "log_bytes",
        "log_sha256",
        "log_truncated",
        "parsed_result",
        "passed",
        "disposition",
    }


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _duration_ms(start: str, finish: str) -> int:
    return round(
        (
            datetime.fromisoformat(finish) - datetime.fromisoformat(start)
        ).total_seconds()
        * 1000
    )


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(payload).hexdigest()


def _normalized_absolute(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


if hashlib.sha256(TASK12_LEGACY_RUFF_RULING_TEXT.encode("utf-8")).hexdigest() != (
    TASK12_LEGACY_RUFF_RULING_HASH
):
    raise RuntimeError("Task 12 legacy Ruff ruling hash constant is inconsistent")
