from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from adapters.extractors.base import ExtractorError, GenericEvidenceRecord, GenericExtractionOutput


class StructuredDataExtractor:
    engine = "structured_parser"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.max_file_bytes = int(self.config.get("max_file_bytes", 50 * 1024 * 1024))
        self.max_records = int(self.config.get("max_records", 100_000))

    def extract(self, path: Path) -> GenericExtractionOutput:
        if path.stat().st_size > self.max_file_bytes:
            raise ExtractorError("structured_limit_exceeded", "Structured input exceeds the byte limit")
        suffix = path.suffix.lower()
        if suffix in {".json", ".jsonld"}:
            return self._extract_json(path)
        if suffix in {".xml", ".rss", ".atom"}:
            return self._extract_xml(path)
        raise ExtractorError("structured_format_unsupported", "Only JSON/JSON-LD/XML/RSS/Atom are supported")

    def _extract_json(self, path: Path) -> GenericExtractionOutput:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ExtractorError("structured_invalid", "JSON parser rejected the input") from exc
        records: list[GenericEvidenceRecord] = []

        def visit(value: Any, pointer: str) -> None:
            if len(records) >= self.max_records:
                raise ExtractorError("structured_limit_exceeded", "Structured input has too many values")
            if isinstance(value, dict):
                for key in sorted(value):
                    escaped = str(key).replace("~", "~0").replace("/", "~1")
                    visit(value[key], f"{pointer}/{escaped}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    visit(item, f"{pointer}/{index}")
            else:
                records.append(GenericEvidenceRecord(
                    kind="text",
                    locator_type="structured_path",
                    locator={"locator_type": "structured_path", "path_type": "json_pointer", "path": pointer or "/"},
                    text=None if value is None else str(value),
                    structured_data={"value": value},
                ))

        visit(payload, "")
        return GenericExtractionOutput(
            engine=self.engine, extractor_version="json-stdlib", validation_mode="deterministic",
            records=records, metadata={"record_count": len(records)},
        )

    def _extract_xml(self, path: Path) -> GenericExtractionOutput:
        try:
            from defusedxml import ElementTree
        except ImportError:
            import xml.etree.ElementTree as ElementTree
        try:
            root = ElementTree.parse(path).getroot()
        except Exception as exc:
            raise ExtractorError("structured_invalid", "XML parser rejected the input") from exc
        records = []

        def visit(element, xpath: str) -> None:
            if len(records) >= self.max_records:
                raise ExtractorError("structured_limit_exceeded", "Structured input has too many values")
            text = (element.text or "").strip()
            if text:
                records.append(GenericEvidenceRecord(
                    kind="text", locator_type="structured_path",
                    locator={"locator_type": "structured_path", "path_type": "xpath", "path": xpath},
                    text=text, structured_data={"attributes": dict(element.attrib)},
                ))
            counts: dict[str, int] = {}
            for child in list(element):
                tag = child.tag.rsplit("}", 1)[-1]
                counts[tag] = counts.get(tag, 0) + 1
                visit(child, f"{xpath}/{tag}[{counts[tag]}]")

        root_tag = root.tag.rsplit("}", 1)[-1]
        visit(root, f"/{root_tag}[1]")
        return GenericExtractionOutput(
            engine=self.engine, extractor_version="defusedxml", validation_mode="deterministic",
            records=records, metadata={"record_count": len(records), "external_entities_resolved": 0},
        )

