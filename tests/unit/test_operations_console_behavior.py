import json
import shutil
import subprocess
from pathlib import Path

import pytest

from wisdome_writer.api.openapi import _compile_schema, load_openapi_contract

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def console_requests():
    if NODE is None:
        pytest.skip("Node is required to execute the operations console JavaScript")
    result = subprocess.run(
        [
            NODE,
            str(ROOT / "tests/js/operations_console_probe.cjs"),
            str(ROOT / "src/static/admin_console/operations.js"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=True,
    )
    return json.loads(result.stdout)


def test_console_schedule_edit_emits_a_valid_immutable_topic_patch(console_requests):
    assert "topic" not in console_requests["patch"]
    assert console_requests["patch"]["expectedVersion"] == 4
    assert console_requests["topicDisabledDuringEdit"] is True
    document = load_openapi_contract()
    _, validator = _compile_schema(
        document["components"]["schemas"]["SchedulePatch"],
        document=document,
        subject="SchedulePatch",
    )
    validator.validate(console_requests["patch"])


def test_console_schedule_create_retains_the_selected_topic(console_requests):
    assert console_requests["create"]["topic"] == "housing_subscription"
    assert "expectedVersion" not in console_requests["create"]
    assert console_requests["topicDisabledDuringCreate"] is False


def test_operations_reauthentication_forwards_optional_mfa(console_requests):
    assert console_requests["reauthScopes"] == ["kill_switch_disable"]
    assert console_requests["mfaProvided"] is True
