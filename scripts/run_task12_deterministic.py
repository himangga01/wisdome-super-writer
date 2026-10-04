"""Execute and record every deterministic Task 12 command gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from apps.local_content.acceptance_runner import (  # noqa: E402
    TASK12_ATTEMPT_SELECTION_RULE,
    TASK12_BASE_COMMIT,
    build_attempt_history_entry,
    build_command_plan,
    build_deterministic_evidence,
    discover_changed_python,
    execute_command,
    verify_deterministic_evidence,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--base", default=TASK12_BASE_COMMIT)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--history-evidence", action="append", default=[])
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
        evidence = build_deterministic_evidence(
            results,
            attempt_id=arguments.attempt_id,
            project_root=project_root,
            base=arguments.base,
            changed_python=changed_python,
            log_root=arguments.log_dir,
        )
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
    evidence = build_deterministic_evidence(
        results,
        attempt_id=arguments.attempt_id,
        project_root=project_root,
        base=arguments.base,
        changed_python=changed_python,
        log_root=arguments.log_dir,
    )
    if arguments.acceptance_report is not None:
        _merge_report(
            arguments.acceptance_report,
            evidence,
            evidence_path=arguments.evidence,
            history_specs=arguments.history_evidence,
        )
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


def _merge_report(
    path: Path,
    evidence: dict[str, object],
    *,
    evidence_path: Path,
    history_specs: list[str],
) -> None:
    target = Path(path)
    report = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("acceptance report must be a JSON object")
    section = report.setdefault("task12_acceptance", {})
    if not isinstance(section, dict):
        raise ValueError("task12 acceptance section must be a JSON object")
    history = [_history_entry(spec) for spec in history_specs]
    history.append(
        build_attempt_history_entry(str(evidence["attempt_id"]), evidence_path)
    )
    attempt_ids = [entry["attempt_id"] for entry in history]
    if len(attempt_ids) != len(set(attempt_ids)):
        raise ValueError("deterministic attempt history IDs must be unique")
    section["deterministic_attempt_history"] = history
    eligible = []
    documents = {}
    root = Path(str(evidence["project_root"])).resolve()
    targets = tuple(evidence["changed_python"])
    for entry in history:
        document = json.loads(Path(entry["evidence_path"]).read_text(encoding="utf-8"))
        documents[entry["attempt_id"]] = document
        verified = verify_deterministic_evidence(
            document, project_root=root, current_changed_python=targets
        )
        if verified.passed and document.get("plan_sha256") == evidence.get("plan_sha256"):
            eligible.append(entry["attempt_id"])
    section["deterministic_evidence"] = documents[eligible[-1]] if eligible else evidence
    section["deterministic_selection"] = {
        "rule": TASK12_ATTEMPT_SELECTION_RULE,
        "selected_attempt_id": eligible[-1] if eligible else None,
        "eligible_attempt_ids": eligible,
    }
    _write_json(target, report)


def _history_entry(specification: str) -> dict[str, object]:
    attempt_id, separator, raw_path = specification.partition("=")
    if not separator or not attempt_id or not raw_path:
        raise ValueError("history evidence must use ATTEMPT_ID=PATH")
    return build_attempt_history_entry(attempt_id, Path(raw_path))


if __name__ == "__main__":
    raise SystemExit(main())
