from __future__ import annotations

import json
import importlib.metadata
import time
from pathlib import Path
from typing import Any, Mapping

from adapters.extractors.base import ExtractorError, GenericEvidenceRecord, GenericExtractionOutput


class StructuredDataExtractor:
    engine = "structured_parser"
    extractor_version = "1.0.0"
    HARD_MAX_FILE_BYTES = 12 * 1024 * 1024

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.max_file_bytes = min(
            int(self.config.get("max_file_bytes", self.HARD_MAX_FILE_BYTES)),
            self.HARD_MAX_FILE_BYTES,
        )
        self.max_records = min(int(self.config.get("max_records", 50_000)), 50_000)
        self.max_json_nodes = min(int(self.config.get("max_json_nodes", 200_000)), 200_000)
        self.max_json_depth = min(int(self.config.get("max_json_depth", 64)), 64)
        self.max_xml_nodes = min(int(self.config.get("max_xml_nodes", 200_000)), 200_000)
        self.max_xml_depth = min(int(self.config.get("max_xml_depth", 64)), 64)
        self.max_text_chars = min(
            int(self.config.get("max_text_chars", 16 * 1024 * 1024)), 16 * 1024 * 1024
        )
        self.max_parse_seconds = min(float(self.config.get("max_parse_seconds", 10)), 10.0)

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
        started_at = time.monotonic()
        try:
            source = path.read_text(encoding="utf-8-sig")
            self._preflight_json(source, started_at=started_at)
            payload = json.loads(source)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ExtractorError("structured_invalid", "JSON parser rejected the input") from exc
        records: list[GenericEvidenceRecord] = []
        nodes = 0
        text_chars = 0
        stack: list[tuple[Any, str, int]] = [(payload, "", 1)]
        while stack:
            value, pointer, depth = stack.pop()
            nodes += 1
            if (
                nodes > self.max_json_nodes
                or depth > self.max_json_depth
                or time.monotonic() - started_at > self.max_parse_seconds
            ):
                raise ExtractorError(
                    "structured_limit_exceeded",
                    "JSON depth, node count, or parse time exceeds the limit",
                )
            if isinstance(value, dict):
                children = []
                for key in sorted(value):
                    escaped = str(key).replace("~", "~0").replace("/", "~1")
                    children.append((value[key], f"{pointer}/{escaped}", depth + 1))
                stack.extend(reversed(children))
            elif isinstance(value, list):
                for index in range(len(value) - 1, -1, -1):
                    stack.append((value[index], f"{pointer}/{index}", depth + 1))
            else:
                if len(records) >= self.max_records:
                    raise ExtractorError("structured_limit_exceeded", "Structured input has too many values")
                text = None if value is None else str(value)
                text_chars += len(text or "")
                if text_chars > self.max_text_chars:
                    raise ExtractorError("structured_limit_exceeded", "JSON text exceeds the configured limit")
                records.append(GenericEvidenceRecord(
                    kind="text",
                    locator_type="structured_path",
                    locator={"locator_type": "structured_path", "path_type": "json_pointer", "path": pointer or "/"},
                    text=text,
                    structured_data={"value": value},
                ))
        return GenericExtractionOutput(
            engine=self.engine, extractor_version=self.extractor_version, validation_mode="deterministic",
            records=records, metadata={"record_count": len(records)},
        )

    def _preflight_json(self, source: str, *, started_at: float) -> None:
        """Bound lexical work before the recursive C decoder allocates objects."""
        depth = 0
        nodes = 0
        text_chars = 0
        in_string = False
        escaped = False
        primitive = False
        for index, character in enumerate(source):
            if index % 1024 == 0 and time.monotonic() - started_at > self.max_parse_seconds:
                raise ExtractorError(
                    "structured_limit_exceeded", "JSON preflight exceeded the deadline"
                )
            if in_string:
                text_chars += 1
                if text_chars > self.max_text_chars:
                    raise ExtractorError(
                        "structured_limit_exceeded", "JSON text exceeds the configured limit"
                    )
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                primitive = False
                in_string = True
                nodes += 1
            elif character in "[{":
                primitive = False
                depth += 1
                nodes += 1
                if depth > self.max_json_depth:
                    raise ExtractorError(
                        "structured_limit_exceeded", "JSON depth exceeds the configured limit"
                    )
            elif character in "]}":
                primitive = False
                depth -= 1
                if depth < 0:
                    raise ExtractorError("structured_invalid", "JSON delimiters are invalid")
            elif character in ",:":
                primitive = False
            elif not character.isspace() and not primitive:
                primitive = True
                nodes += 1
            if nodes > self.max_json_nodes:
                raise ExtractorError(
                    "structured_limit_exceeded", "JSON node count exceeds the configured limit"
                )

    def _extract_xml(self, path: Path) -> GenericExtractionOutput:
        started_at = time.monotonic()
        try:
            from defusedxml import ElementTree
        except ImportError as exc:
            raise ExtractorError(
                "structured_dependency_missing", "defusedxml is required for XML parsing"
            ) from exc
        try:
            if importlib.metadata.version("defusedxml") != "0.7.1":
                raise ExtractorError(
                    "structured_dependency_mismatch", "defusedxml must be pinned to 0.7.1"
                )
        except importlib.metadata.PackageNotFoundError as exc:
            raise ExtractorError(
                "structured_dependency_missing", "defusedxml package metadata is missing"
            ) from exc
        try:
            events = ElementTree.iterparse(
                path,
                events=("start", "end"),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            )
        except ExtractorError:
            raise
        except Exception as exc:
            raise ExtractorError("structured_invalid", "XML parser rejected the input") from exc
        records: list[GenericEvidenceRecord] = []
        nodes = 0
        text_chars = 0
        path_stack: list[str] = []
        sibling_counts: list[dict[str, int]] = []
        try:
            for event, element in events:
                if time.monotonic() - started_at > self.max_parse_seconds:
                    raise ExtractorError(
                        "structured_limit_exceeded", "XML parsing exceeded the deadline"
                    )
                if event == "start":
                    nodes += 1
                    depth = len(path_stack) + 1
                    if (
                        nodes > self.max_xml_nodes
                        or depth > self.max_xml_depth
                    ):
                        raise ExtractorError(
                            "structured_limit_exceeded",
                            "XML depth, node count, or parse time exceeds the limit",
                        )
                    tag = element.tag.rsplit("}", 1)[-1]
                    counts = sibling_counts[-1] if sibling_counts else {}
                    index = counts.get(tag, 0) + 1
                    counts[tag] = index
                    if not sibling_counts:
                        sibling_counts.append(counts)
                    path_stack.append(f"{tag}[{index}]")
                    sibling_counts.append({})
                    continue
                text = (element.text or "").strip()
                if text:
                    if len(records) >= self.max_records:
                        raise ExtractorError("structured_limit_exceeded", "Structured input has too many values")
                    text_chars += len(text)
                    if text_chars > self.max_text_chars:
                        raise ExtractorError("structured_limit_exceeded", "XML text exceeds the configured limit")
                    records.append(GenericEvidenceRecord(
                        kind="text", locator_type="structured_path",
                        locator={"locator_type": "structured_path", "path_type": "xpath", "path": "/" + "/".join(path_stack)},
                        text=text, structured_data={"attributes": dict(element.attrib)},
                    ))
                element.clear()
                path_stack.pop()
                sibling_counts.pop()
        except ExtractorError:
            raise
        except Exception as exc:
            raise ExtractorError("structured_invalid", "XML parser rejected the input") from exc
        return GenericExtractionOutput(
            engine=self.engine, extractor_version=self.extractor_version, validation_mode="deterministic",
            records=records, metadata={"record_count": len(records), "external_entities_resolved": 0},
        )

