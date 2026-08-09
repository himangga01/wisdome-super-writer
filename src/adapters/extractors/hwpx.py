from __future__ import annotations

import importlib.metadata
import posixpath
import re
import time
import unicodedata
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

from adapters.extractors.base import ExtractorError, GenericEvidenceRecord, GenericExtractionOutput


class HwpxExtractor:
    engine = "hwpx_parser"
    extractor_version = "1.0.0"
    DEFUSEDXML_VERSION = "0.7.1"
    HARD_MAX_RAW_BYTES = 12 * 1024 * 1024
    HARD_MAX_ENTRIES = 2_048
    HARD_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
    HARD_MAX_ENTRY_BYTES = 8 * 1024 * 1024
    HARD_MAX_XML_BYTES = 32 * 1024 * 1024
    HARD_MAX_COMPRESSION_RATIO = 100.0
    HARD_MAX_SECTIONS = 256
    HARD_MAX_XML_DEPTH = 64
    HARD_MAX_XML_ELEMENTS = 200_000
    HARD_MAX_TEXT_CHARS = 16 * 1024 * 1024
    HARD_MAX_RECORDS = 50_000
    HARD_MAX_PARSE_SECONDS = 10.0
    ACTIVE_SUFFIXES = {".exe", ".dll", ".js", ".vbs", ".bat", ".cmd", ".ps1", ".jar", ".class"}
    ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}
    PACKAGE_NAMESPACES = {
        "http://www.idpf.org/2007/opf/",
        "http://www.hancom.co.kr/hwpml/2011/package",
        "http://www.hancom.co.kr/hwpml/2016/package",
    }
    SECTION_NAMESPACES = {
        "http://www.hancom.co.kr/hwpml/2011/section",
        "http://www.hancom.co.kr/hwpml/2016/section",
        "http://www.owpml.org/owpml/2021/section",
        "http://www.owpml.org/owpml/2024/section",
    }
    PARAGRAPH_NAMESPACES = {
        "http://www.hancom.co.kr/hwpml/2011/paragraph",
        "http://www.hancom.co.kr/hwpml/2016/paragraph",
        "http://www.owpml.org/owpml/2021/paragraph",
        "http://www.owpml.org/owpml/2024/paragraph",
    }
    PARAGRAPH_ELEMENTS = {"p", "run", "t", "tbl", "tr", "tc", "cell"}
    SECTION_PATH = re.compile(r"^Contents/section[0-9]+\.xml$")
    LEGACY_SECTION_HREF = re.compile(r"^section[0-9]+\.xml$")

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.max_raw_bytes = min(
            int(self.config.get("max_raw_bytes", self.HARD_MAX_RAW_BYTES)),
            self.HARD_MAX_RAW_BYTES,
        )
        self.max_entries = min(
            int(self.config.get("max_entries", self.HARD_MAX_ENTRIES)),
            self.HARD_MAX_ENTRIES,
        )
        self.max_uncompressed_bytes = min(
            int(self.config.get("max_uncompressed_bytes", self.HARD_MAX_UNCOMPRESSED_BYTES)),
            self.HARD_MAX_UNCOMPRESSED_BYTES,
        )
        self.max_entry_uncompressed_bytes = min(
            int(self.config.get("max_entry_uncompressed_bytes", self.HARD_MAX_ENTRY_BYTES)),
            self.HARD_MAX_ENTRY_BYTES,
        )
        self.max_xml_bytes = min(
            int(self.config.get("max_xml_bytes", self.HARD_MAX_XML_BYTES)),
            self.HARD_MAX_XML_BYTES,
        )
        self.max_compression_ratio = min(
            float(self.config.get("max_compression_ratio", self.HARD_MAX_COMPRESSION_RATIO)),
            self.HARD_MAX_COMPRESSION_RATIO,
        )
        self.max_sections = min(
            int(self.config.get("max_sections", self.HARD_MAX_SECTIONS)),
            self.HARD_MAX_SECTIONS,
        )
        self.max_xml_depth = min(
            int(self.config.get("max_xml_depth", self.HARD_MAX_XML_DEPTH)),
            self.HARD_MAX_XML_DEPTH,
        )
        self.max_xml_elements = min(
            int(self.config.get("max_xml_elements", self.HARD_MAX_XML_ELEMENTS)),
            self.HARD_MAX_XML_ELEMENTS,
        )
        self.max_text_chars = min(
            int(self.config.get("max_text_chars", self.HARD_MAX_TEXT_CHARS)),
            self.HARD_MAX_TEXT_CHARS,
        )
        self.max_records = min(
            int(self.config.get("max_records", self.HARD_MAX_RECORDS)),
            self.HARD_MAX_RECORDS,
        )
        self.max_parse_seconds = min(
            float(self.config.get("max_parse_seconds", self.HARD_MAX_PARSE_SECONDS)),
            self.HARD_MAX_PARSE_SECONDS,
        )

    def extract(self, path: Path) -> GenericExtractionOutput:
        started_at = time.monotonic()
        if path.stat().st_size > self.max_raw_bytes:
            raise ExtractorError("hwpx_limit_exceeded", "HWPX exceeds the raw byte limit")
        self._preflight_zip_container(path)
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise ExtractorError("hwpx_corrupt", "HWPX ZIP container is invalid") from exc
        records: list[GenericEvidenceRecord] = []
        try:
            entries = archive.infolist()
            self._validate_package_preamble(archive, entries)
            self._validate_entries(entries)
            parser = self._xml_parser()
            section_names, xml_bytes = self._trusted_section_names(
                archive, entries, parser
            )
            text_chars = 0
            for section_name in section_names:
                if time.monotonic() - started_at > self.max_parse_seconds:
                    raise ExtractorError("hwpx_timeout", "HWPX parsing exceeded the deadline")
                try:
                    payload = self._read_entry_bounded(
                        archive, section_name, self.max_entry_uncompressed_bytes
                    )
                    xml_bytes += len(payload)
                    if xml_bytes > self.max_xml_bytes:
                        raise ExtractorError("hwpx_limit_exceeded", "HWPX XML exceeds the byte limit")
                except ExtractorError:
                    raise
                except Exception as exc:
                    raise ExtractorError("hwpx_xml_invalid", "HWPX section XML is invalid") from exc
                section_records, section_text_chars = self._parse_section_incremental(
                    payload,
                    section_name=section_name,
                    parser=parser,
                    started_at=started_at,
                    remaining_records=self.max_records - len(records),
                    remaining_text_chars=self.max_text_chars - text_chars,
                )
                records.extend(section_records)
                text_chars += section_text_chars
        finally:
            archive.close()
        return GenericExtractionOutput(
            engine=self.engine,
            extractor_version=self.extractor_version,
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
            normalized_identity = unicodedata.normalize("NFC", normalized).casefold()
            extra_offset = 0
            zip64_extra = False
            while extra_offset + 4 <= len(entry.extra):
                header_id = int.from_bytes(entry.extra[extra_offset:extra_offset + 2], "little")
                data_size = int.from_bytes(entry.extra[extra_offset + 2:extra_offset + 4], "little")
                extra_offset += 4
                if extra_offset + data_size > len(entry.extra):
                    raise ExtractorError("hwpx_structure_invalid", "HWPX ZIP extra field is malformed")
                zip64_extra = zip64_extra or header_id == 0x0001
                extra_offset += data_size
            if extra_offset != len(entry.extra):
                raise ExtractorError("hwpx_structure_invalid", "HWPX ZIP extra field is malformed")
            if (
                not raw
                or raw.startswith(("/", "\\"))
                or "\\" in raw
                or normalized.startswith("../")
                or ".." in path.parts
                or "\x00" in raw
                or any(ord(character) < 32 for character in raw)
                or ":" in path.parts[0]
            ):
                raise ExtractorError("hwpx_path_traversal", "HWPX contains an unsafe archive path")
            if normalized_identity in seen:
                raise ExtractorError("hwpx_structure_invalid", "HWPX contains duplicate archive paths")
            seen.add(normalized_identity)
            if raw != normalized:
                raise ExtractorError(
                    "hwpx_path_traversal", "HWPX contains a non-canonical archive path"
                )
            suffix = path.suffix.lower()
            lowered_parts = {part.lower() for part in path.parts}
            if (
                "scripts" in lowered_parts
                or "encryption" in raw.lower()
                or "ole" in path.name.lower()
                or entry.flag_bits & 0x1
                or zip64_extra
                or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or entry.file_size > 0xFFFFFFFF
                or entry.compress_size > 0xFFFFFFFF
                or getattr(entry, "header_offset", 0) > 0xFFFFFFFF
            ):
                raise ExtractorError(
                    "hwpx_active_content",
                    "HWPX active content, OLE, scripts, or encryption are prohibited",
                )
            if suffix in self.ACTIVE_SUFFIXES:
                raise ExtractorError("hwpx_active_content", "HWPX active content is prohibited")
            if suffix in self.ARCHIVE_SUFFIXES:
                raise ExtractorError("hwpx_nested_archive", "Nested archives are prohibited")
            total += entry.file_size
            if entry.file_size > self.max_entry_uncompressed_bytes:
                raise ExtractorError("hwpx_zip_bomb", "HWPX entry exceeds the per-entry byte limit")
            if total > self.max_uncompressed_bytes:
                raise ExtractorError("hwpx_zip_bomb", "HWPX uncompressed size exceeds the limit")
            if entry.file_size and entry.compress_size == 0:
                raise ExtractorError("hwpx_zip_bomb", "HWPX entry has an invalid compression ratio")
            if entry.compress_size and entry.file_size / entry.compress_size > self.max_compression_ratio:
                raise ExtractorError("hwpx_zip_bomb", "HWPX compression ratio exceeds the limit")

    def _preflight_zip_container(self, path: Path) -> None:
        """Validate bounded ZIP structure and physical mimetype before ZipFile."""
        file_size = path.stat().st_size
        if file_size < 22:
            raise ExtractorError("hwpx_structure_invalid", "HWPX EOCD is missing")
        tail_size = min(file_size, 65_557)
        with path.open("rb") as source:
            source.seek(file_size - tail_size)
            tail = source.read(tail_size)
            eocd_index = tail.rfind(b"PK\x05\x06")
            if eocd_index < 0 or eocd_index + 22 > len(tail):
                raise ExtractorError("hwpx_structure_invalid", "HWPX EOCD is missing")
            if b"PK\x06\x06" in tail[:eocd_index] or b"PK\x06\x07" in tail[:eocd_index]:
                raise ExtractorError("hwpx_zip64", "ZIP64 HWPX containers are prohibited")
            eocd = tail[eocd_index:eocd_index + 22]
            disk_number = int.from_bytes(eocd[4:6], "little")
            central_disk = int.from_bytes(eocd[6:8], "little")
            disk_entries = int.from_bytes(eocd[8:10], "little")
            total_entries = int.from_bytes(eocd[10:12], "little")
            central_size = int.from_bytes(eocd[12:16], "little")
            central_offset = int.from_bytes(eocd[16:20], "little")
            comment_size = int.from_bytes(eocd[20:22], "little")
            if eocd_index + 22 + comment_size != len(tail):
                raise ExtractorError("hwpx_structure_invalid", "HWPX EOCD comment is inconsistent")
            if disk_number or central_disk or disk_entries != total_entries:
                raise ExtractorError("hwpx_structure_invalid", "Multi-disk HWPX is prohibited")
            if (
                total_entries == 0xFFFF
                or central_size == 0xFFFFFFFF
                or central_offset == 0xFFFFFFFF
            ):
                raise ExtractorError("hwpx_zip64", "ZIP64 HWPX containers are prohibited")
            if total_entries > self.max_entries:
                raise ExtractorError("hwpx_zip_bomb", "HWPX has too many archive entries")
            absolute_eocd = file_size - tail_size + eocd_index
            if central_offset + central_size != absolute_eocd:
                raise ExtractorError("hwpx_structure_invalid", "HWPX central directory is inconsistent")

            source.seek(central_offset)
            central = source.read(central_size)
            cursor = 0
            mimetype_offsets: list[int] = []
            for _ in range(total_entries):
                if cursor + 46 > len(central) or central[cursor:cursor + 4] != b"PK\x01\x02":
                    raise ExtractorError("hwpx_structure_invalid", "HWPX central directory is malformed")
                flags = int.from_bytes(central[cursor + 8:cursor + 10], "little")
                compressed_size = int.from_bytes(central[cursor + 20:cursor + 24], "little")
                uncompressed_size = int.from_bytes(central[cursor + 24:cursor + 28], "little")
                name_size = int.from_bytes(central[cursor + 28:cursor + 30], "little")
                extra_size = int.from_bytes(central[cursor + 30:cursor + 32], "little")
                comment_length = int.from_bytes(central[cursor + 32:cursor + 34], "little")
                disk_start = int.from_bytes(central[cursor + 34:cursor + 36], "little")
                local_offset = int.from_bytes(central[cursor + 42:cursor + 46], "little")
                record_end = cursor + 46 + name_size + extra_size + comment_length
                if record_end > len(central):
                    raise ExtractorError("hwpx_structure_invalid", "HWPX central entry is truncated")
                name = central[cursor + 46:cursor + 46 + name_size]
                if (
                    disk_start
                    or compressed_size == 0xFFFFFFFF
                    or uncompressed_size == 0xFFFFFFFF
                    or local_offset == 0xFFFFFFFF
                    or local_offset >= central_offset
                ):
                    raise ExtractorError("hwpx_zip64", "ZIP64 HWPX entries are prohibited")
                if flags & 0x1:
                    raise ExtractorError("hwpx_active_content", "Encrypted HWPX entries are prohibited")
                if name == b"mimetype":
                    mimetype_offsets.append(local_offset)
                cursor = record_end
            if cursor != len(central) or len(mimetype_offsets) != 1:
                raise ExtractorError("hwpx_structure_invalid", "HWPX central directory count is inconsistent")

            source.seek(0)
            local = source.read(30)
            if len(local) != 30 or local[:4] != b"PK\x03\x04":
                raise ExtractorError("hwpx_mimetype_invalid", "HWPX mimetype must be physically first")
            flags = int.from_bytes(local[6:8], "little")
            method = int.from_bytes(local[8:10], "little")
            compressed_size = int.from_bytes(local[18:22], "little")
            uncompressed_size = int.from_bytes(local[22:26], "little")
            name_size = int.from_bytes(local[26:28], "little")
            extra_size = int.from_bytes(local[28:30], "little")
            name = source.read(name_size)
            extra = source.read(extra_size)
            expected = b"application/hwp+zip"
            if (
                mimetype_offsets[0] != 0
                or name != b"mimetype"
                or flags & (0x1 | 0x8)
                or method != zipfile.ZIP_STORED
                or compressed_size != len(expected)
                or uncompressed_size != len(expected)
                or b"\x01\x00" in extra
                or source.read(len(expected)) != expected
            ):
                raise ExtractorError(
                    "hwpx_mimetype_invalid",
                    "HWPX mimetype must be the exact first stored local entry",
                )

    def _validate_package_preamble(self, archive, entries: list[zipfile.ZipInfo]) -> None:
        try:
            mimetype = archive.getinfo("mimetype")
        except KeyError as exc:
            raise ExtractorError(
                "hwpx_mimetype_invalid", "HWPX mimetype must be the first entry"
            ) from exc
        if mimetype.header_offset != 0:
            raise ExtractorError("hwpx_mimetype_invalid", "HWPX mimetype must be the first entry")
        if mimetype.compress_type != zipfile.ZIP_STORED:
            raise ExtractorError("hwpx_mimetype_invalid", "HWPX mimetype must be uncompressed")
        payload = self._read_entry_bounded(archive, "mimetype", 64)
        if payload != b"application/hwp+zip":
            raise ExtractorError("hwpx_mimetype_invalid", "HWPX mimetype is not exact")

    @staticmethod
    def _read_entry_bounded(archive, name: str, limit: int) -> bytes:
        chunks: list[bytes] = []
        observed = 0
        with archive.open(name, "r") as source:
            while True:
                chunk = source.read(min(64 * 1024, limit - observed + 1))
                if not chunk:
                    break
                observed += len(chunk)
                if observed > limit:
                    raise ExtractorError("hwpx_limit_exceeded", "HWPX entry exceeds its byte limit")
                chunks.append(chunk)
        return b"".join(chunks)

    def _trusted_section_names(self, archive, entries, parser) -> tuple[list[str], int]:
        names = {entry.filename for entry in entries}
        package_name = "Contents/content.hpf"
        if package_name not in names:
            raise ExtractorError(
                "hwpx_structure_invalid", "HWPX content.hpf manifest is required"
            )
        payload = self._read_entry_bounded(
            archive, package_name, self.max_entry_uncompressed_bytes
        )
        if len(payload) > self.max_entry_uncompressed_bytes:
            raise ExtractorError("hwpx_limit_exceeded", "HWPX content manifest is too large")
        manifest: dict[str, tuple[str, str]] = {}
        spine: list[str] = []
        depth = 0
        elements = 0
        started_at = time.monotonic()
        try:
            events = parser.iterparse(
                BytesIO(payload),
                events=("start", "end"),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            )
            for event, element in events:
                if time.monotonic() - started_at > self.max_parse_seconds:
                    raise ExtractorError("hwpx_timeout", "HWPX manifest parsing exceeded the deadline")
                if event == "start":
                    depth += 1
                    elements += 1
                    if depth > self.max_xml_depth or elements > self.max_xml_elements:
                        raise ExtractorError("hwpx_limit_exceeded", "HWPX manifest XML exceeds its limits")
                    namespace, local = self._qualified_name(element.tag)
                    if elements == 1:
                        self._validate_root(
                            element,
                            expected_local="package",
                            allowed_namespaces=self.PACKAGE_NAMESPACES,
                        )
                    self._reject_external_attributes(element)
                    if local in {"item", "itemref"} and namespace not in self.PACKAGE_NAMESPACES:
                        raise ExtractorError(
                            "hwpx_structure_invalid", "Foreign HWPX manifest elements are prohibited"
                        )
                    if local == "item":
                        item_id = self._attribute(element, "id")
                        href = self._attribute(element, "href")
                        media_type = self._attribute(element, "media-type")
                        if not item_id or not href or not media_type or item_id in manifest:
                            raise ExtractorError(
                                "hwpx_structure_invalid",
                                "HWPX manifest item is incomplete or duplicate",
                            )
                        candidate = self._canonical_package_href(href)
                        if candidate in {value[0] for value in manifest.values()}:
                            raise ExtractorError(
                                "hwpx_structure_invalid", "HWPX manifest paths must be unique"
                            )
                        manifest[item_id] = (candidate, media_type)
                    elif local == "itemref":
                        item_id = self._attribute(element, "idref")
                        if not item_id or item_id in spine:
                            raise ExtractorError(
                                "hwpx_structure_invalid", "HWPX spine item is incomplete or duplicate"
                            )
                        spine.append(item_id)
                    continue
                depth -= 1
                element.clear()
        except ExtractorError:
            raise
        except Exception as exc:
            raise ExtractorError(
                "hwpx_structure_invalid", "HWPX content manifest is invalid"
            ) from exc
        if not spine or len(spine) > self.max_sections:
            raise ExtractorError("hwpx_structure_invalid", "HWPX spine is empty or too large")
        if any(path not in names for path, _ in manifest.values()):
            raise ExtractorError(
                "hwpx_structure_invalid", "HWPX manifest references a missing package item"
            )
        try:
            selected_material = [manifest[item_id] for item_id in spine]
        except KeyError as exc:
            raise ExtractorError(
                "hwpx_structure_invalid", "HWPX spine references an unknown manifest item"
            ) from exc
        selected = [path for path, _ in selected_material]
        if any(
            not self.SECTION_PATH.fullmatch(path)
            or media_type not in {"application/xml", "text/xml"}
            for path, media_type in selected_material
        ):
            raise ExtractorError(
                "hwpx_structure_invalid", "HWPX spine may reference only section XML"
            )
        actual_sections = {
            name
            for name in names
            if name.startswith("Contents/section") and name.lower().endswith(".xml")
        }
        if (
            len(set(selected)) != len(selected)
            or set(selected) != actual_sections
            or any(name not in names for name in selected)
        ):
            raise ExtractorError(
                "hwpx_structure_invalid", "HWPX manifest and section files do not match"
            )
        return selected, len(payload)

    @staticmethod
    def _qualified_name(tag: str) -> tuple[str, str]:
        value = str(tag)
        if value.startswith("{") and "}" in value:
            namespace, local = value[1:].split("}", 1)
            return namespace, local.lower()
        return "", value.lower()

    @staticmethod
    def _canonical_package_href(href: str) -> str:
        parsed = urlsplit(href)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or href.startswith(("/", "\\"))
            or "\\" in href
            or "\x00" in href
            or any(ord(character) < 32 for character in href)
            or unicodedata.normalize("NFC", href) != href
        ):
            raise ExtractorError(
                "hwpx_external_relationship", "HWPX manifest reference is unsafe"
            )
        candidate = posixpath.normpath(href)
        if (
            candidate in {".", ".."}
            or candidate.startswith("../")
            or ".." in PurePosixPath(href).parts
        ):
            raise ExtractorError(
                "hwpx_path_traversal", "HWPX manifest reference escapes the package"
            )
        if candidate != href:
            raise ExtractorError(
                "hwpx_path_traversal", "HWPX manifest reference is not canonical"
            )
        if HwpxExtractor.LEGACY_SECTION_HREF.fullmatch(candidate):
            candidate = f"Contents/{candidate}"
        return candidate

    @staticmethod
    def _xml_parser():
        try:
            from defusedxml import ElementTree
        except ImportError as exc:
            raise ExtractorError(
                "hwpx_dependency_missing", "defusedxml is required for HWPX parsing"
            ) from exc
        try:
            version = importlib.metadata.version("defusedxml")
        except importlib.metadata.PackageNotFoundError as exc:
            raise ExtractorError(
                "hwpx_dependency_missing", "defusedxml package metadata is missing"
            ) from exc
        if version != HwpxExtractor.DEFUSEDXML_VERSION:
            raise ExtractorError(
                "hwpx_dependency_mismatch", "defusedxml must be pinned to 0.7.1"
            )
        return ElementTree

    def _parse_section_incremental(
        self,
        payload: bytes,
        *,
        section_name: str,
        parser,
        started_at: float,
        remaining_records: int,
        remaining_text_chars: int,
    ) -> tuple[list[GenericEvidenceRecord], int]:
        records: list[GenericEvidenceRecord] = []
        paragraph_stack: list[dict[str, Any]] = []
        table_stack: list[dict[str, Any]] = []
        row_stack: list[dict[str, int]] = []
        cell_stack: list[dict[str, Any]] = []
        paragraph_index = 0
        table_index = 0
        depth = 0
        elements = 0
        text_chars = 0
        try:
            events = parser.iterparse(
                BytesIO(payload),
                events=("start", "end"),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            )
            for event, element in events:
                if time.monotonic() - started_at > self.max_parse_seconds:
                    raise ExtractorError("hwpx_timeout", "HWPX parsing exceeded the deadline")
                namespace, local = self._qualified_name(element.tag)
                if (
                    local in self.PARAGRAPH_ELEMENTS
                    and namespace not in self.PARAGRAPH_NAMESPACES
                ):
                    raise ExtractorError(
                        "hwpx_structure_invalid",
                        "Foreign HWPX body elements are prohibited",
                    )
                if event == "start":
                    depth += 1
                    elements += 1
                    if depth > self.max_xml_depth or elements > self.max_xml_elements:
                        raise ExtractorError(
                            "hwpx_limit_exceeded", "HWPX XML depth or element count exceeds the limit"
                        )
                    if elements == 1:
                        self._validate_root(
                            element,
                            expected_local={"sec", "section"},
                            allowed_namespaces=self.SECTION_NAMESPACES,
                        )
                    self._reject_external_attributes(element)
                    if local == "p":
                        paragraph_stack.append(
                            {
                                "id": self._attribute(element, "id") or f"p-{paragraph_index}",
                                "parts": [],
                            }
                        )
                        paragraph_index += 1
                    elif local == "tbl":
                        table_stack.append(
                            {
                                "id": self._attribute(element, "id") or f"table-{table_index}",
                                "next_row": 0,
                            }
                        )
                        table_index += 1
                    elif local == "tr" and table_stack:
                        row_stack.append(
                            {"row": table_stack[-1]["next_row"], "next_column": 0}
                        )
                        table_stack[-1]["next_row"] += 1
                    elif local in {"tc", "cell"} and table_stack and row_stack:
                        cell_stack.append(
                            {
                                "table_id": table_stack[-1]["id"],
                                "row": row_stack[-1]["row"],
                                "column": row_stack[-1]["next_column"],
                                "parts": [],
                            }
                        )
                        row_stack[-1]["next_column"] += 1
                    continue

                direct_text = (element.text or "") if local == "t" else ""
                if direct_text:
                    text_chars += len(direct_text)
                    if text_chars > remaining_text_chars:
                        raise ExtractorError(
                            "hwpx_limit_exceeded", "HWPX text exceeds the configured limit"
                        )
                    if paragraph_stack:
                        paragraph_stack[-1]["parts"].append(direct_text)
                    if cell_stack:
                        cell_stack[-1]["parts"].append(direct_text)
                if local == "p" and paragraph_stack:
                    paragraph = paragraph_stack.pop()
                    text = "".join(paragraph["parts"]).strip()
                    if text:
                        records.append(
                            GenericEvidenceRecord(
                                kind="text",
                                locator_type="hwpx_path",
                                locator={
                                    "locator_type": "hwpx_path",
                                    "section_path": section_name,
                                    "paragraph_id": paragraph["id"],
                                    "table_id": None,
                                    "row_index": None,
                                    "column_index": None,
                                    "embedded_object_id": None,
                                },
                                text=text,
                            )
                        )
                elif local in {"tc", "cell"} and cell_stack:
                    cell = cell_stack.pop()
                    text = "".join(cell["parts"]).strip()
                    records.append(
                        GenericEvidenceRecord(
                            kind="table",
                            locator_type="hwpx_path",
                            locator={
                                "locator_type": "hwpx_path",
                                "section_path": section_name,
                                "paragraph_id": None,
                                "table_id": cell["table_id"],
                                "row_index": cell["row"],
                                "column_index": cell["column"],
                                "embedded_object_id": None,
                            },
                            text=text,
                            structured_data={
                                "table_id": cell["table_id"],
                                "row": cell["row"],
                                "column": cell["column"],
                            },
                        )
                    )
                elif local == "tr" and row_stack:
                    row_stack.pop()
                elif local == "tbl" and table_stack:
                    table_stack.pop()
                if len(records) > remaining_records:
                    raise ExtractorError(
                        "hwpx_limit_exceeded", "HWPX extracted records exceed the configured limit"
                    )
                element.clear()
                depth -= 1
        except ExtractorError:
            raise
        except Exception as exc:
            raise ExtractorError("hwpx_xml_invalid", "HWPX section XML is invalid") from exc
        return records, text_chars

    def _validate_xml_tree(self, root: Any) -> None:
        elements = 0
        stack = [(root, 1)]
        while stack:
            element, depth = stack.pop()
            elements += 1
            if elements > self.max_xml_elements or depth > self.max_xml_depth:
                raise ExtractorError(
                    "hwpx_limit_exceeded", "HWPX XML depth or element count exceeds the limit"
                )
            stack.extend((child, depth + 1) for child in list(element))

    @staticmethod
    def _attribute(element: Any, name: str) -> str | None:
        for key, value in element.attrib.items():
            if str(key).rsplit("}", 1)[-1].lower() == name:
                return str(value).strip() or None
        return None

    @staticmethod
    def _validate_root(
        root: Any,
        *,
        expected_local: str | set[str],
        allowed_namespaces: set[str],
    ) -> None:
        tag = str(root.tag)
        if tag.startswith("{") and "}" in tag:
            namespace, local = tag[1:].split("}", 1)
        else:
            namespace, local = "", tag
        expected = {expected_local} if isinstance(expected_local, str) else set(expected_local)
        if local.lower() not in expected or namespace not in allowed_namespaces:
            raise ExtractorError(
                "hwpx_structure_invalid",
                "HWPX XML root or namespace is not approved",
            )

    def _reject_external_relationships(self, root: Any) -> None:
        for element in root.iter():
            self._reject_external_attributes(element)

    @staticmethod
    def _reject_external_attributes(element: Any) -> None:
        for key, raw_value in element.attrib.items():
            attribute = str(key).rsplit("}", 1)[-1].lower()
            if attribute not in {"href", "src", "target", "path"}:
                continue
            value = str(raw_value).strip()
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc or value.startswith(("//", "\\\\")):
                raise ExtractorError(
                    "hwpx_external_relationship",
                    "HWPX external relationships are prohibited",
                )

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

