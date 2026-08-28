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
    merge_artifact_audit,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--expected-raw", required=True, type=int)
    parser.add_argument("--expected-excluded", required=True, type=int)
    parser.add_argument("--expected-indexed", required=True, type=int)
    parser.add_argument("--expected-articles", required=True, type=int)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--no-write", action="store_true")
    arguments = parser.parse_args(argv)
    audit = audit_live_run(
        arguments.run_root,
        window_start=datetime.fromisoformat(arguments.window_start),
        expectations=AcceptanceExpectations(
            raw_observations=arguments.expected_raw,
            excluded_notices=arguments.expected_excluded,
            indexed_notices=arguments.expected_indexed,
            detailed_articles=arguments.expected_articles,
        ),
    )
    report_path = arguments.report or arguments.run_root / "acceptance-report.json"
    if not arguments.no_write:
        merge_artifact_audit(report_path, audit)
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


if __name__ == "__main__":
    raise SystemExit(main())
