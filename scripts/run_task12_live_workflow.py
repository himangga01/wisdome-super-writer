"""Run the real live workflow and bind its log and immutable report core."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from apps.local_content.acceptance import (  # noqa: E402
    merge_evidence_reference,
    workflow_report_core,
)
from apps.local_content.acceptance_runner import execute_command  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, required=True)
    arguments = parser.parse_args()
    evidence_root = arguments.evidence_root.resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    python = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")
    argv = (
        python,
        "src\\manage.py",
        "collect_recent_housing",
        "--days",
        "7",
        "--humanize",
        "--write-articles",
    )
    result = execute_command(
        name="live_workflow",
        argv=argv,
        cwd=PROJECT_ROOT,
        log_path=evidence_root / "live-workflow.log",
        gate=True,
        environment={
            "WISDOME_ENVIRONMENT": "development",
            "WISDOME_RUNTIME_MODE": "local",
            "HUMANIZER_BASE_URL": "http://127.0.0.1:3210",
            "PYTHONUTF8": "1",
        },
        max_log_bytes=8 * 1024 * 1024,
    )
    log = Path(result.log_path).read_bytes()
    summary = _last_json(log)
    if result.exit_code != 0 or result.log_truncated or summary is None:
        print(
            json.dumps(
                {
                    "exit_code": result.exit_code,
                    "log": result.log_path,
                    "summary_found": summary is not None,
                    "log_truncated": result.log_truncated,
                },
                sort_keys=True,
            )
        )
        return 1
    report_relative = summary.get("report_path")
    run_name = summary.get("run_path")
    if not isinstance(report_relative, str) or not isinstance(run_name, str):
        raise ValueError("live workflow summary omitted report identity")
    report_path = (PROJECT_ROOT / "output" / "housing" / report_relative).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("live workflow report must be an object")
    core = workflow_report_core(report)
    command = asdict(result)
    command.pop("name")
    command.pop("gate")
    command.pop("parsed_result")
    command["argv"] = list(result.argv)
    evidence = {
        "schema_version": 1,
        "command": command,
        "report_path": str(report_path),
        "report_core_sha256": _canonical_sha256(core),
        "run_name": run_name,
        "official_api_reconciliation": (
            summary.get("official_api_reconciliation")
        ),
    }
    evidence_path = evidence_root / "workflow-evidence.json"
    _write_json(evidence_path, evidence)
    merge_evidence_reference(
        report_path,
        field="workflow_evidence",
        evidence_path=evidence_path,
    )
    print(
        json.dumps(
            {
                "run_path": run_name,
                "report_path": str(report_path),
                "evidence_path": str(evidence_path),
                "official_api_reconciliation": evidence["official_api_reconciliation"],
            },
            sort_keys=True,
        )
    )
    return 0


def _last_json(payload: bytes) -> dict[str, object] | None:
    for line in reversed(payload.decode("utf-8", errors="strict").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _canonical_sha256(value: object) -> str:
    import hashlib

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
