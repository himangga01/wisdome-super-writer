"""Execute and record every deterministic Task 12 command gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from apps.local_content.acceptance_runner import (  # noqa: E402
    LegacyRuling,
    build_command_plan,
    build_deterministic_evidence,
    discover_changed_python,
    execute_command,
)

RULING_ID = "TASK12-RUFF-LEGACY-001"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--base", required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--acceptance-report", type=Path)
    arguments = parser.parse_args(argv)
    project_root = arguments.project_root.resolve()
    changed_python = discover_changed_python(project_root, base=arguments.base)
    plan = build_command_plan(
        project_root,
        base=arguments.base,
        changed_python=changed_python,
    )
    ruling = LegacyRuling(
        RULING_ID,
        (
            f"At base {arguments.base}, whole-repository Ruff findings are ledgered "
            "legacy debt and a nonzero whole_repository_ruff result is classified only "
            "as legacy_debt_not_gate. changed_file_ruff, every local_content Python "
            "file, ci_material, tests, Django, migrations, setup, and diff remain "
            "binding gates. This ruling never changes a nonzero command to passed=true."
        ),
    )
    results = []
    environment = {
        "WISDOME_ENVIRONMENT": "development",
        "WISDOME_RUNTIME_MODE": "local",
        "PYTHONUTF8": "1",
    }
    for index, command in enumerate(plan, start=1):
        result = execute_command(
            name=command.name,
            argv=command.argv,
            cwd=project_root,
            log_path=arguments.log_dir / f"{index:02d}-{command.name}.log",
            gate=command.gate,
            environment=environment,
        )
        results.append(result)
        evidence = build_deterministic_evidence(results, ruling=ruling)
        _write_json(arguments.evidence, evidence)
        print(
            json.dumps(
                {
                    "command": command.name,
                    "exit_code": result.exit_code,
                    "gate": command.gate,
                    "log": result.log_path,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    evidence = build_deterministic_evidence(results, ruling=ruling)
    if arguments.acceptance_report is not None:
        _merge_report(arguments.acceptance_report, evidence)
    print(
        json.dumps(
            {
                "evidence": arguments.evidence.resolve().as_posix(),
                "overall_passed": evidence["overall_passed"],
                "failed_gates": evidence["failed_gates"],
            },
            sort_keys=True,
        )
    )
    return 0 if evidence["overall_passed"] is True else 1


def _write_json(path: Path, value: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(target)


def _merge_report(path: Path, evidence: dict[str, object]) -> None:
    target = Path(path)
    report = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("acceptance report must be a JSON object")
    section = report.setdefault("task12_acceptance", {})
    if not isinstance(section, dict):
        raise ValueError("task12 acceptance section must be a JSON object")
    section["deterministic_evidence"] = evidence
    _write_json(target, report)


if __name__ == "__main__":
    raise SystemExit(main())
