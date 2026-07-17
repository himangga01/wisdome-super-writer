from __future__ import annotations

import importlib.metadata
import posixpath
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from adapters.extractors.base import ExtractorError, GenericEvidenceRecord, GenericExtractionOutput


class HwpxExtractor:
    engine = "hwpx_parser"
    ACTIVE_SUFFIXES = {".exe", ".dll", ".js", ".vbs", ".bat", ".cmd", ".ps1", ".jar", ".class"}
    ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.max_entries = int(self.config.get("max_entries", 10_000))
        self.max_uncompressed_bytes = int(self.config.get("max_uncompressed_bytes", 500 * 1024 * 1024))
        self.max_compression_ratio = float(self.config.get("max_compression_ratio", 200.0))

    def extract(self, path: Path) -> GenericExtractionOutput:
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise ExtractorError("hwpx_corrupt", "HWPX ZIP container is invalid") from exc
        records: list[GenericEvidenceRecord] = []
        try:
            entries = archive.infolist()
            self._validate_entries(entries)
            section_names = sorted(
                info.filename for info in entries
                if info.filename.startswith("Contents/section") and info.filename.lower().endswith(".xml")
            )
            if not section_names:
                raise ExtractorError("hwpx_structure_invalid", "HWPX contains no section XML")
            parser = self._xml_parser()
            for section_name in section_names:
                try:
                    root = parser.fromstring(archive.read(section_name))
                except Exception as exc:
                    raise ExtractorError("hwpx_xml_invalid", "HWPX section XML is invalid") from exc
                paragraph_index = 0
                table_index = 0
                for element in root.iter():
                    tag = self._local_name(element.tag)
                    if tag == "p":
                        paragraph_id = element.attrib.get("id") or f"p-{paragraph_index}"
                        text = "".join(element.itertext()).strip()
                        if text:
                            records.append(GenericEvidenceRecord(
                                kind="text",
                                locator_type="hwpx_path",
                                locator={
                                    "locator_type": "hwpx_path",
                                    "section_path": section_name,
                                    "paragraph_id": paragraph_id,
                                    "table_id": None,
                                    "row_index": None,
                                    "column_index": None,
                                    "embedded_object_id": None,
                                },
                                text=text,
                            ))
                        paragraph_index += 1
                    elif tag == "tbl":
                        table_id = element.attrib.get("id") or f"table-{table_index}"
                        rows = [child for child in element.iter() if self._local_name(child.tag) == "tr"]
                        for row_index, row in enumerate(rows):
                            cells = [child for child in row if self._local_name(child.tag) in {"tc", "cell"}]
                            for column_index, cell in enumerate(cells):
                                text = "".join(cell.itertext()).strip()
                                records.append(GenericEvidenceRecord(
                                    kind="table",
                                    locator_type="hwpx_path",
                                    locator={
                                        "locator_type": "hwpx_path",
                                        "section_path": section_name,
                                        "paragraph_id": None,
                                        "table_id": table_id,
                                        "row_index": row_index,
                                        "column_index": column_index,
                                        "embedded_object_id": None,
                                    },
                                    text=text,
                                    structured_data={"table_id": table_id, "row": row_index, "column": column_index},
                                ))
                        table_index += 1
        finally:
            archive.close()
        try:
            version = importlib.metadata.version("defusedxml")
        except importlib.metadata.PackageNotFoundError:
            version = "stdlib"
        return GenericExtractionOutput(
            engine=self.engine,
            extractor_version=version,
            validation_mode="deterministic",
            records=records,
            metadata={"record_count": len(records), "external_links_followed": 0},
        )

    def _validate_entries(self, entries: list[zipfile.ZipInfo]) -> None:
        if len(entries) > self.max_entries:
            raise ExtractorError("hwpx_zip_bomb", "HWPX has too many archive entries")
        total = 0
        seen: set[str] = set()
        for entry in entries:
            raw = entry.filename
            path = PurePosixPath(raw)
            normalized = posixpath.normpath(raw)
            if raw.startswith(("/", "\\")) or "\\" in raw or normalized.startswith("../") or ".." in path.parts:
                raise ExtractorError("hwpx_path_traversal", "HWPX contains an unsafe archive path")
            if normalized in seen:
                raise ExtractorError("hwpx_structure_invalid", "HWPX contains duplicate archive paths")
            seen.add(normalized)
            suffix = path.suffix.lower()
            if suffix in self.ACTIVE_SUFFIXES:
                raise ExtractorError("hwpx_active_content", "HWPX active content is prohibited")
            if suffix in self.ARCHIVE_SUFFIXES:
                raise ExtractorError("hwpx_nested_archive", "Nested archives are prohibited")
            total += entry.file_size
            if total > self.max_uncompressed_bytes:
                raise ExtractorError("hwpx_zip_bomb", "HWPX uncompressed size exceeds the limit")
            if entry.file_size and entry.compress_size == 0:
                raise ExtractorError("hwpx_zip_bomb", "HWPX entry has an invalid compression ratio")
            if entry.compress_size and entry.file_size / entry.compress_size > self.max_compression_ratio:
                raise ExtractorError("hwpx_zip_bomb", "HWPX compression ratio exceeds the limit")

    @staticmethod
    def _xml_parser():
        try:
            from defusedxml import ElementTree

            return ElementTree
        except ImportError:
            import xml.etree.ElementTree as ElementTree

            return ElementTree

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

