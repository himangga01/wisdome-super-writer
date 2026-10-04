import argparse
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("network", ["none", "invalid"])
def test_rejected_exact_hwp_cli_preserves_preexisting_caller_files(tmp_path, network):
    source = ROOT / "deploy/containers/hwp-worker/wisdome-hwp-sandbox"
    loader = importlib.machinery.SourceFileLoader("review_hwp_cli", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    input_path = tmp_path / "input.hwp"
    output = tmp_path / "output.pdf"
    report = tmp_path / "report.json"
    input_path.write_bytes(b"invalid input")
    output.write_bytes(b"caller-owned-pdf")
    report.write_bytes(b"caller-owned-report")
    result = module.run_exact_cli(
        argparse.Namespace(
            input=str(input_path),
            output=str(output),
            report=str(report),
            network=network,
        )
    )
    assert result == 20
    assert output.read_bytes() == b"caller-owned-pdf"
    assert report.read_bytes() == b"caller-owned-report"
