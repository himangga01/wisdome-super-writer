from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from apps.local_content.acceptance import derive_task12_acceptance


def _report() -> dict[str, object]:
    ruling_text = "explicit ruling text"
    return {
        "live_success": True,
        "complete": True,
        "blocked": False,
        "error_codes": [],
        "task12_acceptance": {
            "artifact_audit": {"overall_passed": True},
            "browser": {"passed": True},
            "deterministic_evidence": {
                "overall_passed": True,
                "exact_command_plan_executed": True,
                "failed_gates": [],
                "legacy_ruling": {
                    "id": "TASK12-RUFF-LEGACY-001",
                    "text": ruling_text,
                    "sha256": hashlib.sha256(ruling_text.encode()).hexdigest(),
                    "applied": True,
                },
                "commands": [
                    *(
                        {
                            "name": name,
                            "exit_code": 0,
                            "gate": True,
                            "passed": True,
                            "disposition": "gate_passed",
                        }
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
                    ),
                    {
                        "name": "whole_repository_ruff",
                        "exit_code": 1,
                        "gate": False,
                        "passed": False,
                        "disposition": "legacy_debt_not_gate",
                    },
                ],
            },
        },
    }


def test_final_acceptance_derives_success_and_preserves_nonzero_ruff_truth() -> None:
    report = _report()

    final = derive_task12_acceptance(report)

    assert final["overall_passed"] is True
    ruff = report["task12_acceptance"]["deterministic_evidence"]["commands"][-1]
    assert ruff["exit_code"] == 1
    assert ruff["passed"] is False


@pytest.mark.parametrize(
    "failure",
    ["workflow", "artifact", "browser", "deterministic", "ruling_hash", "ruff_truth"],
)
def test_final_acceptance_fails_when_any_evidence_gate_or_ruling_is_invalid(
    failure: str,
) -> None:
    report = deepcopy(_report())
    section = report["task12_acceptance"]
    deterministic = section["deterministic_evidence"]
    if failure == "workflow":
        report["complete"] = False
    elif failure == "artifact":
        section["artifact_audit"]["overall_passed"] = False
    elif failure == "browser":
        section["browser"]["passed"] = False
    elif failure == "deterministic":
        deterministic["overall_passed"] = False
    elif failure == "ruling_hash":
        deterministic["legacy_ruling"]["sha256"] = "0" * 64
    else:
        deterministic["commands"][-1]["passed"] = True

    final = derive_task12_acceptance(report)

    assert final["overall_passed"] is False
