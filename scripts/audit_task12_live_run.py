"""Audit one Task 12 live housing run and merge body-free evidence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from apps.local_content.acceptance import (  # noqa: E402
    AcceptanceExpectations,
    audit_live_run,
    merge_evidence_reference,
)
from apps.local_content.dates import SEOUL  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--expected-raw", required=True, type=int)
    parser.add_argument("--expected-excluded", required=True, type=int)
    parser.add_argument("--expected-indexed", required=True, type=int)
    parser.add_argument("--expected-articles", required=True, type=int)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--no-write", action="store_true")
    arguments = parser.parse_args(argv)
    executed_at = datetime.now(SEOUL)
    expectations = AcceptanceExpectations(
        raw_observations=arguments.expected_raw,
        excluded_notices=arguments.expected_excluded,
        indexed_notices=arguments.expected_indexed,
        detailed_articles=arguments.expected_articles,
    )
    audit = audit_live_run(
        arguments.run_root,
        window_start=datetime.fromisoformat(arguments.window_start),
        expectations=expectations,
        executed_at=executed_at,
    )
    report_path = arguments.report or arguments.run_root / "acceptance-report.json"
    if not arguments.no_write:
        if arguments.evidence is None:
            raise ValueError("--evidence is required when writing acceptance evidence")
        evidence = {
            "schema_version": 2,
            "run_root": str(arguments.run_root.resolve()),
            "window_start": arguments.window_start,
            "executed_at": executed_at.isoformat(),
            "expectations": {
                "raw_observations": expectations.raw_observations,
                "excluded_notices": expectations.excluded_notices,
                "indexed_notices": expectations.indexed_notices,
                "detailed_articles": expectations.detailed_articles,
                "source_keys": list(expectations.source_keys),
            },
            "audit": audit,
        }
        _write_json(arguments.evidence, evidence)
        merge_evidence_reference(
            report_path,
            field="artifact_evidence",
            evidence_path=arguments.evidence,
        )
    print(
        json.dumps(
            {
                "report": report_path.as_posix(),
                "overall_passed": audit["overall_passed"],
                "counts": audit["counts"],
                "requirements": [
                    {"name": row["name"], "passed": row["passed"]}
                    for row in audit["requirements"]
                ],
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0 if audit["overall_passed"] is True else 1


def _write_json(path: Path, value: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


if __name__ == "__main__":
    raise SystemExit(main())
