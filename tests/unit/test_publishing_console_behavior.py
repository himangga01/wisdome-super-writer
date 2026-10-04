import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_publishing_evidence_uses_the_source_url_and_rejects_unsafe_links():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the publishing console JavaScript")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            node,
            str(root / "tests/js/publishing_console_probe.cjs"),
            str(root / "src/static/admin_console/publishing_article.js"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["error"] == ""
    assert observed["anchors"] == [
        {"href": "https://example.com/notice", "rel": "noopener noreferrer"}
    ]
