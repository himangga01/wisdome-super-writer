from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from adapters.extractors.base import (
    ExtractorError,
    GenericEvidenceRecord,
    GenericExtractionOutput,
    sha256_file,
    sniff_mime,
)


class LegacyHwpConverter:
    """Executes only an approved no-network sandbox wrapper; never invokes a shell."""

    engine = "legacy_hwp_converter"

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)
        self.command_template: Sequence[str] = self.config.get("sandbox_command", [])
        self.converter_manifest_hash = str(self.config.get("converter_manifest_hash", ""))
        self.timeout_seconds = int(self.config.get("timeout_seconds", 180))
        self.max_output_bytes = int(self.config.get("max_output_bytes", 300 * 1024 * 1024))
        if not self.command_template or not self.converter_manifest_hash:
            raise ExtractorError(
                "legacy_hwp_converter_unavailable",
                "Approved sandbox command and converter manifest are required",
            )

    def extract(self, path: Path) -> GenericExtractionOutput:
        input_checksum = sha256_file(path)
        with tempfile.TemporaryDirectory(prefix="wisdome-hwp-") as temp_dir:
            temp = Path(temp_dir)
            output_pdf = temp / "converted.pdf"
            report_path = temp / "conversion-report.json"
            command = [
                str(part)
                .replace("{input}", str(path.resolve()))
                .replace("{output}", str(output_pdf))
                .replace("{report}", str(report_path))
                for part in self.command_template
            ]
            if any("{" in part or "}" in part for part in command):
                raise ExtractorError("legacy_hwp_profile_invalid", "Sandbox command has unknown placeholders")
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(temp),
                "TMPDIR": str(temp),
                "NO_PROXY": "*",
                "http_proxy": "",
                "https_proxy": "",
                "ALL_PROXY": "",
            }
            try:
                completed = subprocess.run(
                    command,
                    cwd=temp,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_seconds,
                    check=False,
                    shell=False,
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
                )
            except subprocess.TimeoutExpired as exc:
                raise ExtractorError("legacy_hwp_timeout", "Legacy HWP conversion timed out") from exc
            except OSError as exc:
                raise ExtractorError("legacy_hwp_converter_unavailable", "Sandbox wrapper could not start") from exc
            if completed.returncode != 0:
                raise ExtractorError("legacy_hwp_conversion_failed", "Sandboxed HWP conversion failed")
            if not output_pdf.is_file() or not report_path.is_file():
                raise ExtractorError("legacy_hwp_conversion_failed", "Converter output or report is missing")
            if output_pdf.stat().st_size > self.max_output_bytes or sniff_mime(output_pdf) != "application/pdf":
                raise ExtractorError("legacy_hwp_output_invalid", "Converter output is not an allowed PDF")
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ExtractorError("legacy_hwp_report_invalid", "Converter report is invalid") from exc
            output_checksum = sha256_file(output_pdf)
            report_hash = sha256_file(report_path)
            if report.get("input_checksum") != input_checksum or report.get("output_pdf_checksum") != output_checksum:
                raise ExtractorError("legacy_hwp_report_invalid", "Converter report checksums do not match")
            durable_output = Path(str(path) + f".{output_checksum[:12]}.converted.pdf")
            output_pdf.replace(durable_output)
            record = GenericEvidenceRecord(
                kind="attachment",
                locator_type="hwp_conversion",
                locator={
                    "locator_type": "hwp_conversion",
                    "input_checksum": input_checksum,
                    "output_pdf_checksum": output_checksum,
                    "converter_manifest_hash": self.converter_manifest_hash,
                    "sandbox_report_hash": report_hash,
                },
                object_path=str(durable_output),
                mime_type="application/pdf",
                structured_data={
                    "converted_page_count": report.get("page_count"),
                    "sandbox_policy": report.get("sandbox_policy"),
                    "follow_up_engine_allowlist": ["native_pdf", "paddleocr_ppstructurev3"],
                },
            )
        return GenericExtractionOutput(
            engine=self.engine,
            extractor_version=str(self.config.get("extractor_version", "legacy-hwp-v1")),
            validation_mode="deterministic",
            records=[record],
            metadata={"partial_text_used": False, "network_allowed": False},
        )

