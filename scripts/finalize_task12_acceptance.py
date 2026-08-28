"""Derive the final Task 12 verdict from persisted machine evidence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from apps.local_content.acceptance import derive_task12_acceptance  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    arguments = parser.parse_args(argv)
    report = json.loads(arguments.report.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("acceptance report must be a JSON object")
    section = derive_task12_acceptance(report, project_root=PROJECT_ROOT)
    section["finalized_at"] = datetime.now(UTC).isoformat()
    temporary = arguments.report.with_name(f".{arguments.report.name}.finalize.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(arguments.report)
    print(
        json.dumps(
            {
                "report": arguments.report.resolve().as_posix(),
                "overall_passed": section["overall_passed"],
                "requirements": [
                    {"name": row["name"], "passed": row["passed"]}
                    for row in section["requirements"]
                ],
            },
            sort_keys=True,
        )
    )
    return 0 if section["overall_passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
