from __future__ import annotations

import hashlib
import importlib
import json
import os
import tempfile
import unittest
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from django.core.exceptions import ValidationError

from adapters.extractors.base import (
    ExtractorError,
    GenericEvidenceRecord,
    GenericExtractionOutput,
)
from adapters.extractors.hwpx import HwpxExtractor
from adapters.extractors import media
from adapters.extractors.native_pdf.adapter import NativePdfExtractor
from adapters.extractors.paddleocr.adapter import PaddleOCRExtractor
from adapters.extractors.structured import StructuredDataExtractor
from adapters.storage import S3ObjectStorage
from adapters.storage.base import ObjectInfo
from apps.evidence import profiles, tasks
from apps.evidence.models import (
    EvidenceAsset,
    EvidenceDerivationType,
    EvidenceKind,
    ExtractionEngine,
    GenericValidationMode,
    LocatorType,
)
from wisdome_writer.infrastructure.event_routes import payload_schema_for


SHA = "1" * 64
ACTIVE_GENERIC_ENGINES = {
    ExtractionEngine.HTML,
    ExtractionEngine.STRUCTURED,
    ExtractionEngine.SPREADSHEET,
    ExtractionEngine.HWPX,
    ExtractionEngine.LEGACY_HWP,
}


class _ClosableBody(BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.was_closed = False
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        return super().read(size)

    def close(self) -> None:
        self.was_closed = True
        super().close()


class _S3Client:
    def __init__(
        self,
        value: bytes,
        *,
        version_id: str = "frozen-v1",
        content_length: int | None = None,
    ) -> None:
        self.body = _ClosableBody(value)
        self.version_id = version_id
        self.content_length = len(value) if content_length is None else content_length
        self.params = None

    def get_object(self, **kwargs):
        self.params = kwargs
        return {
            "Body": self.body,
            "VersionId": self.version_id,
            "ContentLength": self.content_length,
            "ContentType": "application/octet-stream",
        }


class ActiveRoutingContractTests(unittest.TestCase):
    def test_manifest_and_factory_expose_exactly_implemented_generic_engines(self) -> None:
        root = Path(__file__).resolve().parents[2] / "config" / "extraction-profiles"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        documents = [
            json.loads((root / entry["file"]).read_text(encoding="utf-8"))
            for entry in manifest["profiles"]
        ]
        generic_engines = {
            document["engine"]
            for document in documents
            if document["engine"] not in {
                ExtractionEngine.NATIVE_PDF,
                ExtractionEngine.PADDLEOCR,
            }
        }

        self.assertEqual(generic_engines, ACTIVE_GENERIC_ENGINES)
        self.assertEqual(set(tasks.GENERIC_ENGINE_FACTORIES), ACTIVE_GENERIC_ENGINES)
        self.assertNotIn("browser-capture-deterministic-v1", profiles.MVP_PROFILE_KEYS)
        self.assertNotIn("media-deterministic-v1", profiles.MVP_PROFILE_KEYS)
        self.assertNotIn("manual-entry-v1", profiles.MVP_PROFILE_KEYS)

    def test_generic_output_must_match_frozen_profile_identity(self) -> None:
        profile = SimpleNamespace(
            engine=ExtractionEngine.HTML,
            extractor_version="1.0.0",
            validation_mode=GenericValidationMode.DETERMINISTIC,
        )
        output = GenericExtractionOutput(
            engine=ExtractionEngine.STRUCTURED,
            extractor_version="1.0.0",
            validation_mode=GenericValidationMode.DETERMINISTIC,
            records=[
                GenericEvidenceRecord(
                    kind="text",
                    locator_type="structured_path",
                    locator={
                        "locator_type": "structured_path",
                        "path_type": "json_pointer",
                        "path": "/title",
                    },
                    text="value",
                )
            ],
        )

        with self.assertRaisesRegex(ExtractorError, "frozen profile"):
            tasks._validate_generic_output(profile, output)

    def test_historical_other_ready_v1_schema_keeps_retired_producer_values(self) -> None:
        schema = payload_schema_for("evidence.other_ready", 1)
        self.assertTrue(
            {"browser_capture", "media_parser", "manual_entry"}
            <= schema.choices["engine"]
        )
        self.assertTrue(
            {
                "document_block",
                "visualization",
                "image_region",
                "media_time",
                "manual",
            }
            <= schema.choices["locator_type"]
        )
        self.assertIn("manual", schema.choices["validation_mode"])

    def test_legacy_hwp_output_must_have_exactly_one_conversion_record(self) -> None:
        profile = SimpleNamespace(
            engine=ExtractionEngine.LEGACY_HWP,
            extractor_version="1.0.0",
            validation_mode=GenericValidationMode.DETERMINISTIC,
        )
        record = GenericEvidenceRecord(
            kind="attachment",
            locator_type="hwp_conversion",
            locator={"locator_type": "hwp_conversion"},
            text=None,
        )
        output = GenericExtractionOutput(
            engine=profile.engine,
            extractor_version=profile.extractor_version,
            validation_mode=profile.validation_mode,
            records=[record, record],
        )
        with self.assertRaisesRegex(ExtractorError, "exactly one"):
            tasks._validate_generic_output(profile, output)

    def test_v11_profiles_pin_exact_runtime_and_shared_implementation_files(self) -> None:
        root = Path(__file__).resolve().parents[2] / "config" / "extraction-profiles"
        documents = [
            json.loads((root / entry["file"]).read_text(encoding="utf-8"))
            for entry in json.loads((root / "manifest.json").read_text(encoding="utf-8"))["profiles"]
            if str(entry["file"]).endswith("v1.1.json")
        ]
        expected_packages = {
            ExtractionEngine.NATIVE_PDF: "1.28.0",
            ExtractionEngine.PADDLEOCR: "3.7.0",
            ExtractionEngine.HTML: "0.4.11",
            ExtractionEngine.STRUCTURED: "0.7.1",
            ExtractionEngine.SPREADSHEET: "3.1.5",
            ExtractionEngine.HWPX: "0.7.1",
        }
        self.assertTrue(documents)
        for document in documents:
            self.assertEqual(document["package_version"], expected_packages[document["engine"]])
            self.assertEqual(document["config"]["python_runtime_version"], "3.12.10")
            self.assertIn("src/adapters/extractors/base.py", document["implementation_files"])
            if document["engine"] == ExtractionEngine.PADDLEOCR:
                for required_limit in (
                    "max_pages",
                    "max_total_seconds",
                    "max_output_blocks",
                    "max_total_render_bytes",
                    "max_prediction_results_per_page",
                ):
                    self.assertIn(required_limit, document["config"])

    def test_profile_model_rejects_engine_validation_mode_mismatch(self) -> None:
        from apps.evidence.models import ExtractionProfileSnapshot

        profile = ExtractionProfileSnapshot(
            profile_key="bad-html",
            profile_version="1.1.0",
            engine=ExtractionEngine.HTML,
            extractor_version="1.0.0",
            implementation_manifest_hash=SHA,
            config={},
            config_hash=SHA,
            validation_mode=GenericValidationMode.CALIBRATED,
            profile_material_hash=SHA,
        )
        with self.assertRaises(ValidationError):
            profile.clean()

    def test_legacy_hwp_release_does_not_require_host_python_runtime(self) -> None:
        from apps.evidence.models import ExtractionProfileSnapshot

        profile = ExtractionProfileSnapshot(
            profile_key="legacy-hwp-v1",
            profile_version="1.1.0",
            engine=ExtractionEngine.LEGACY_HWP,
            extractor_version="legacy-hwp-v1.1.0",
            package_version="rhwp-0.8.2+immutable",
            runtime_version="sandbox-uds-v1",
            pipeline_name="rhwp-to-qpdf-verified-pdf",
            implementation_manifest_hash=SHA,
            config={"protocol_version": "wisdome-hwp-uds-v1"},
            config_hash=SHA,
            validation_mode=GenericValidationMode.DETERMINISTIC,
            profile_material_hash=SHA,
        )
        profile.clean()

    def test_paddle_release_requires_exact_transitive_dependency_set(self) -> None:
        self.assertEqual(
            set(profiles._dependencies_for_engine(ExtractionEngine.PADDLEOCR)),
            {
                ("paddleocr", "3.7.0"),
                ("paddlepaddle", "3.2.2"),
                ("PyMuPDF", "1.28.0"),
                ("Pillow", "12.3.0"),
            },
        )

    def test_release_profile_resolution_rejects_superseded_code_snapshot(self) -> None:
        root = Path(__file__).resolve().parents[2] / "config" / "extraction-profiles"
        old = SimpleNamespace(profile_key="native-pdf-v1", profile_version="1.0.0")
        with self.assertRaisesRegex(ValidationError, "active release"):
            profiles.release_profile_snapshot_values(old, root=root)
        current = SimpleNamespace(profile_key="native-pdf-v1", profile_version="1.1.0")
        with patch.object(profiles.importlib.metadata, "version", return_value="1.28.0"), patch.object(
            profiles.platform, "python_version", return_value="3.12.10"
        ):
            values = profiles.release_profile_snapshot_values(current, root=root)
        self.assertEqual(values["package_version"], "1.28.0")
        self.assertEqual(values["profile_version"], "1.1.0")


class BoundedStorageReadTests(unittest.TestCase):
    def test_bounded_read_requires_and_returns_the_exact_frozen_version(self) -> None:
        value = b"bounded-object"
        client = _S3Client(value)
        storage = S3ObjectStorage(bucket="test", client=client)

        with self.assertRaises(ValueError):
            storage.get_bounded_bytes(
                key="evidence/raw/input.bin", version_id="", max_bytes=len(value)
            )

        observed = storage.get_bounded_bytes(
            key="evidence/raw/input.bin",
            version_id="frozen-v1",
            max_bytes=len(value),
        )
        self.assertEqual(observed, value)
        self.assertEqual(client.params["VersionId"], "frozen-v1")
        self.assertTrue(client.body.was_closed)

    def test_bounded_read_rejects_content_length_before_reading(self) -> None:
        client = _S3Client(b"small", content_length=100)
        storage = S3ObjectStorage(bucket="test", client=client)

        with self.assertRaisesRegex(ValueError, "configured byte limit"):
            storage.get_bounded_bytes(
                key="evidence/raw/input.bin", version_id="frozen-v1", max_bytes=10
            )

        self.assertEqual(client.body.read_calls, 0)
        self.assertTrue(client.body.was_closed)

    def test_bounded_read_rejects_mismatched_response_version(self) -> None:
        client = _S3Client(b"value", version_id="different-v2")
        with self.assertRaisesRegex(ValueError, "version"):
            S3ObjectStorage(bucket="test", client=client).get_bounded_bytes(
                key="evidence/raw/input.bin",
                version_id="frozen-v1",
                max_bytes=10,
            )
        self.assertEqual(client.body.read_calls, 0)
        self.assertTrue(client.body.was_closed)

    def test_task_frozen_read_checks_size_and_checksum(self) -> None:
        value = b"verified"
        storage = SimpleNamespace(
            get_bounded_bytes=lambda **kwargs: value,
        )
        observed = tasks._read_frozen_object(
            storage=storage,
            key="evidence/raw/input.bin",
            version_id="v1",
            expected_size=len(value),
            expected_checksum=hashlib.sha256(value).hexdigest(),
            hard_max_bytes=1024,
        )
        self.assertEqual(observed, value)
        with self.assertRaisesRegex(ExtractorError, "version"):
            tasks._read_frozen_object(
                storage=storage,
                key="evidence/raw/input.bin",
                version_id="",
                expected_size=len(value),
                expected_checksum=hashlib.sha256(value).hexdigest(),
                hard_max_bytes=1024,
            )

    def test_file_download_rejects_response_identity_before_first_read(self) -> None:
        cases = [
            _S3Client(b"value", version_id="wrong-v2"),
            _S3Client(b"value", content_length=99),
        ]
        for client in cases:
            with self.subTest(version=client.version_id, size=client.content_length):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    with self.assertRaises(ValueError):
                        S3ObjectStorage(bucket="test", client=client).get_file(
                            key="evidence/raw/input.bin",
                            version_id="frozen-v1",
                            destination=Path(temporary_directory) / "input.bin",
                            expected_checksum_sha256=hashlib.sha256(b"value").hexdigest(),
                            expected_size=5,
                        )
                self.assertEqual(client.body.read_calls, 0)
                self.assertTrue(client.body.was_closed)

    def test_versionless_upload_cannot_be_bound_to_evidence(self) -> None:
        info = ObjectInfo(
            key="evidence/raw/input.bin",
            version_id=None,
            checksum_sha256=SHA,
            size=1,
            content_type="application/octet-stream",
            etag="etag-is-not-a-version",
        )
        with self.assertRaisesRegex(ExtractorError, "version"):
            tasks._required_object_version(info)


class ParserSafetyTests(unittest.TestCase):
    @staticmethod
    def _write_official_hwpx(path: Path, *, section: bytes | None = None) -> None:
        package = b'''<?xml version="1.0" encoding="UTF-8"?>
        <opf:package xmlns:opf="http://www.idpf.org/2007/opf/" version="3.0">
          <opf:manifest>
            <opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>
            <opf:item id="image" href="BinData/image.png" media-type="image/png"/>
            <opf:item id="settings" href="settings.xml" media-type="application/xml"/>
            <opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>
          </opf:manifest>
          <opf:spine><opf:itemref idref="section0"/></opf:spine>
        </opf:package>'''
        section = section or b'''<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
          xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
          <hp:p id="p1"><hp:run><hp:t>official shape</hp:t></hp:run></hp:p>
        </hs:sec>'''
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                zipfile.ZipInfo("mimetype"),
                b"application/hwp+zip",
                compress_type=zipfile.ZIP_STORED,
            )
            archive.writestr("Contents/content.hpf", package)
            archive.writestr("Contents/header.xml", b"<header/>")
            archive.writestr("settings.xml", b"<settings/>")
            archive.writestr("Contents/section0.xml", section)
            archive.writestr("BinData/image.png", b"png")

    def test_official_opf_hwpx_manifest_selects_only_spine_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "official.hwpx"
            self._write_official_hwpx(path)
            output = HwpxExtractor().extract(path)
        self.assertEqual([record.text for record in output.records], ["official shape"])

    def test_hwpx_rejects_foreign_namespace_body_elements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "foreign-body.hwpx"
            self._write_official_hwpx(
                path,
                section=b'''<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
                  xmlns:evil="https://evil.example/schema">
                  <evil:p id="p1"><evil:t>untrusted</evil:t></evil:p>
                </hs:sec>''',
            )
            with self.assertRaises(ExtractorError) as raised:
                HwpxExtractor().extract(path)
        self.assertEqual(raised.exception.code, "hwpx_structure_invalid")

    def test_hwpx_rejects_zip64_locator_before_opening_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "zip64-marker.hwpx"
            self._write_official_hwpx(path)
            raw = path.read_bytes()
            eocd = raw.rfind(b"PK\x05\x06")
            self.assertGreaterEqual(eocd, 0)
            path.write_bytes(raw[:eocd] + b"PK\x06\x07" + (b"\0" * 16) + raw[eocd:])
            with self.assertRaises(ExtractorError) as raised:
                HwpxExtractor().extract(path)
        self.assertEqual(raised.exception.code, "hwpx_zip64")

    def test_hwpx_rejects_oversized_eocd_entry_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "entry-count.hwpx"
            self._write_official_hwpx(path)
            raw = bytearray(path.read_bytes())
            eocd = raw.rfind(b"PK\x05\x06")
            raw[eocd + 8:eocd + 12] = (4096).to_bytes(2, "little") * 2
            path.write_bytes(raw)
            with self.assertRaises(ExtractorError) as raised:
                HwpxExtractor().extract(path)
        self.assertEqual(raised.exception.code, "hwpx_zip_bomb")

    def test_hwpx_rejects_physical_non_mimetype_even_when_central_lists_it_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "physical-order.hwpx"
            package = b'''<opf:package xmlns:opf="http://www.idpf.org/2007/opf/">
            <opf:manifest><opf:item id="s0" href="section0.xml" media-type="application/xml"/></opf:manifest>
            <opf:spine><opf:itemref idref="s0"/></opf:spine></opf:package>'''
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Contents/content.hpf", package)
                archive.writestr(
                    zipfile.ZipInfo("mimetype"),
                    b"application/hwp+zip",
                    compress_type=zipfile.ZIP_STORED,
                )
                archive.writestr(
                    "Contents/section0.xml",
                    b"<hs:sec xmlns:hs='http://www.owpml.org/owpml/2024/section'/>",
                )
            raw = bytearray(path.read_bytes())
            eocd = raw.rfind(b"PK\x05\x06")
            central_size = int.from_bytes(raw[eocd + 12:eocd + 16], "little")
            central_offset = int.from_bytes(raw[eocd + 16:eocd + 20], "little")
            central = bytes(raw[central_offset:central_offset + central_size])
            records = []
            cursor = 0
            while cursor < len(central):
                self.assertEqual(central[cursor:cursor + 4], b"PK\x01\x02")
                size = (
                    46
                    + int.from_bytes(central[cursor + 28:cursor + 30], "little")
                    + int.from_bytes(central[cursor + 30:cursor + 32], "little")
                    + int.from_bytes(central[cursor + 32:cursor + 34], "little")
                )
                records.append(central[cursor:cursor + size])
                cursor += size
            records.sort(
                key=lambda record: 0
                if record[46:46 + int.from_bytes(record[28:30], "little")] == b"mimetype"
                else 1
            )
            raw[central_offset:central_offset + central_size] = b"".join(records)
            path.write_bytes(raw)
            with self.assertRaises(ExtractorError) as raised:
                HwpxExtractor().extract(path)
        self.assertEqual(raised.exception.code, "hwpx_mimetype_invalid")

    def test_hwpx_rejects_central_directory_file_bound_inconsistency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "central-bound.hwpx"
            self._write_official_hwpx(path)
            raw = bytearray(path.read_bytes())
            eocd = raw.rfind(b"PK\x05\x06")
            central_size = int.from_bytes(raw[eocd + 12:eocd + 16], "little")
            raw[eocd + 12:eocd + 16] = (central_size + 1).to_bytes(4, "little")
            path.write_bytes(raw)
            with self.assertRaises(ExtractorError) as raised:
                HwpxExtractor().extract(path)
        self.assertEqual(raised.exception.code, "hwpx_structure_invalid")
    def test_hwpx_rejects_per_entry_limit_and_duplicate_archive_name(self) -> None:
        extractor = HwpxExtractor(
            {
                "max_entries": 10,
                "max_entry_uncompressed_bytes": 4,
                "max_uncompressed_bytes": 20,
                "max_compression_ratio": 10,
            }
        )
        oversized = zipfile.ZipInfo("Contents/section0.xml")
        oversized.file_size = 5
        oversized.compress_size = 5
        with self.assertRaisesRegex(ExtractorError, "entry"):
            extractor._validate_entries([oversized])

        first = zipfile.ZipInfo("Contents/section0.xml")
        first.file_size = first.compress_size = 1
        second = zipfile.ZipInfo("Contents/./section0.xml")
        second.file_size = second.compress_size = 1
        with self.assertRaisesRegex(ExtractorError, "duplicate"):
            extractor._validate_entries([first, second])

    def test_hwpx_rejects_external_spine_and_ole_payload(self) -> None:
        external_hpf = b'''<?xml version="1.0"?>
        <package xmlns="http://www.hancom.co.kr/hwpml/2011/package"><manifest><item id="s0" href="https://evil.example/section0.xml" media-type="application/xml"/></manifest>
        <spine><itemref idref="s0"/></spine></package>'''
        section = b"<section><p id='p1'>safe</p></section>"
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "external.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    zipfile.ZipInfo("mimetype"),
                    b"application/hwp+zip",
                    compress_type=zipfile.ZIP_STORED,
                )
                archive.writestr("Contents/content.hpf", external_hpf)
                archive.writestr("Contents/section0.xml", section)
            with self.assertRaisesRegex(ExtractorError, "external|manifest|spine"):
                HwpxExtractor().extract(path)

            ole_path = Path(temporary_directory) / "ole.hwpx"
            with zipfile.ZipFile(ole_path, "w") as archive:
                archive.writestr(
                    zipfile.ZipInfo("mimetype"),
                    b"application/hwp+zip",
                    compress_type=zipfile.ZIP_STORED,
                )
                archive.writestr(
                    "Contents/content.hpf",
                    b"<package xmlns='http://www.hancom.co.kr/hwpml/2011/package'><manifest><item id='s0' href='section0.xml' media-type='application/xml'/></manifest><spine><itemref idref='s0'/></spine></package>",
                )
                archive.writestr("Contents/section0.xml", section)
                archive.writestr("BinData/OLE1.bin", b"unsafe")
            with self.assertRaisesRegex(ExtractorError, "OLE|active"):
                HwpxExtractor().extract(ole_path)

    def test_hwpx_requires_first_uncompressed_exact_mimetype(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "bad.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Contents/content.hpf", b"<package/>")
                archive.writestr("mimetype", b"application/hwp+zip")
            with self.assertRaisesRegex(ExtractorError, "mimetype"):
                HwpxExtractor().extract(path)

    def test_structured_json_enforces_depth_during_walk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "deep.json"
            path.write_text('{"a":{"b":{"c":1}}}', encoding="utf-8")
            with self.assertRaisesRegex(ExtractorError, "limit"):
                StructuredDataExtractor(
                    {
                        "max_file_bytes": 4096,
                        "max_records": 100,
                        "max_json_depth": 2,
                        "max_json_nodes": 100,
                        "max_text_chars": 100,
                        "max_parse_seconds": 1,
                    }
                ).extract(path)

    def test_structured_json_rejects_deep_input_before_json_loads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "deep-preflight.json"
            path.write_text("[" * 20 + "0" + "]" * 20, encoding="utf-8")
            extractor = StructuredDataExtractor(
                {
                    "max_file_bytes": 4096,
                    "max_records": 100,
                    "max_json_depth": 4,
                    "max_json_nodes": 100,
                    "max_text_chars": 100,
                    "max_parse_seconds": 1,
                }
            )
            with patch.object(json, "loads", side_effect=AssertionError("loads called")):
                with self.assertRaisesRegex(ExtractorError, "limit"):
                    extractor.extract(path)

    def test_structured_json_preflight_bounds_array_nodes_and_string_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cases = (
                ("array.json", "[" + ",".join("0" for _ in range(20)) + "]", 10, 100),
                ("string.json", '"0123456789"', 100, 5),
            )
            for filename, payload, nodes, text_chars in cases:
                path = root / filename
                path.write_text(payload, encoding="utf-8")
                extractor = StructuredDataExtractor(
                    {
                        "max_file_bytes": 4096,
                        "max_records": 100,
                        "max_json_depth": 64,
                        "max_json_nodes": nodes,
                        "max_text_chars": text_chars,
                        "max_parse_seconds": 1,
                    }
                )
                with self.subTest(filename=filename), patch.object(
                    json, "loads", side_effect=AssertionError("loads called")
                ):
                    with self.assertRaisesRegex(ExtractorError, "limit"):
                        extractor.extract(path)

    def test_structured_xml_rejects_dtd_and_entity_declarations(self) -> None:
        payload = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY ext SYSTEM "file:///etc/passwd">]><x>&ext;</x>'
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "payload.xml"
            path.write_bytes(payload)
            with self.assertRaises(ExtractorError) as raised:
                StructuredDataExtractor(
                    {
                        "max_file_bytes": 4096,
                        "max_records": 10,
                        "max_xml_nodes": 10,
                        "max_xml_depth": 4,
                        "max_text_chars": 100,
                        "max_parse_seconds": 1,
                    }
                ).extract(path)
        self.assertIn(raised.exception.code, {"structured_active_xml", "structured_invalid"})

    def test_static_image_inspection_rejects_pixel_and_decompressed_limits(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "large.png"
            Image.new("RGB", (20, 20)).save(path)
            with self.assertRaisesRegex(ExtractorError, "pixel"):
                media.inspect_static_image(path, {"max_pixels": 100})
            with self.assertRaisesRegex(ExtractorError, "decompressed"):
                media.inspect_static_image(
                    path,
                    {
                        "max_pixels": 1000,
                        "max_width": 100,
                        "max_height": 100,
                        "max_decompressed_bytes": 100,
                    },
                )

    def test_native_pdf_inspection_rejects_oversized_page_dimensions(self) -> None:
        page = SimpleNamespace(
            rect=SimpleNamespace(width=2000, height=100),
            get_text=lambda *args, **kwargs: [] if args and args[0] == "blocks" else "text",
            get_images=lambda **kwargs: [],
            get_drawings=lambda: [],
        )

        class _Document:
            needs_pass = False
            page_count = 1

            def __iter__(self):
                return iter([page])

            def close(self):
                return None

        fitz = SimpleNamespace(open=lambda path: _Document())
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "input.pdf"
            path.write_bytes(b"%PDF-1.7\n")
            extractor = NativePdfExtractor(
                {
                    "max_file_bytes": 1024,
                    "max_pages": 1,
                    "max_page_width_points": 1000,
                    "max_page_height_points": 1000,
                }
            )
            with patch.object(extractor, "_fitz", return_value=fitz):
                with self.assertRaisesRegex(ExtractorError, "dimension"):
                    extractor.inspect(path)

    def test_native_deterministic_blocks_do_not_emit_numeric_confidence(self) -> None:
        page = SimpleNamespace(
            rect=SimpleNamespace(width=100, height=100),
            rotation=0,
            get_text=lambda *args, **kwargs: [[0, 0, 10, 10, "text"]],
        )

        class _Document:
            needs_pass = False
            page_count = 1

            def load_page(self, index):
                self.index = index
                return page

            def close(self):
                return None

        extractor = NativePdfExtractor({"max_pages": 1})
        with patch.object(extractor, "_fitz", return_value=SimpleNamespace(open=lambda path: _Document())):
            with tempfile.TemporaryDirectory() as temporary_directory:
                path = Path(temporary_directory) / "input.pdf"
                path.write_bytes(b"%PDF-1.7\n")
                output = extractor.extract(path, [0])
        self.assertIsNone(output.pages[0].blocks[0].confidence)

    def test_paddle_rejects_pdf_render_limit_before_pixmap_allocation(self) -> None:
        page = SimpleNamespace(rect=SimpleNamespace(width=1000, height=1000))
        page.get_pixmap = Mock()

        class _Document:
            needs_pass = False
            page_count = 1

            def load_page(self, index):
                return page

            def close(self):
                return None

        fake_fitz = SimpleNamespace(
            open=lambda path: _Document(),
            Matrix=lambda x, y: (x, y),
        )
        extractor = PaddleOCRExtractor(
            {
                "pipeline_name": "PPStructureV3",
                "network_access": False,
                "render_dpi": 200,
                "max_render_pixels": 100,
            },
            model_manifest={},
            model_manifest_hash=SHA,
            config_hash=SHA,
        )
        with tempfile.TemporaryDirectory() as temporary_directory, patch.dict(
            "sys.modules", {"fitz": fake_fitz}
        ):
            path = Path(temporary_directory) / "input.pdf"
            path.write_bytes(b"%PDF-1.7\n")
            with self.assertRaisesRegex(ExtractorError, "render"):
                list(extractor._prepare_inputs(
                    path,
                    [0],
                    input_kind="pdf",
                    temp_dir=Path(temporary_directory),
                ))
        page.get_pixmap.assert_not_called()

    def test_paddle_pdf_inputs_render_lazily_one_page_at_a_time(self) -> None:
        pixmaps = []
        pages = []
        for index in range(2):
            image_bytes = b"image" + bytes([index])
            pixmap = SimpleNamespace(
                width=10,
                height=10,
                stride=30,
                save=lambda path, value=image_bytes: Path(path).write_bytes(value),
            )
            pixmaps.append(pixmap)
            page = SimpleNamespace(
                rect=SimpleNamespace(width=10, height=10),
                get_pixmap=Mock(return_value=pixmap),
            )
            pages.append(page)

        class _Document:
            needs_pass = False
            page_count = 2

            def load_page(self, index):
                return pages[index]

            def close(self):
                return None

        fake_fitz = SimpleNamespace(open=lambda path: _Document(), Matrix=lambda x, y: (x, y))
        extractor = PaddleOCRExtractor(
            {
                "pipeline_name": "PPStructureV3",
                "network_access": False,
                "render_dpi": 72,
                "max_render_pixels": 1000,
                "max_render_decompressed_bytes": 10000,
            },
            model_manifest={},
            model_manifest_hash=SHA,
            config_hash=SHA,
        )
        with tempfile.TemporaryDirectory() as temporary_directory, patch.dict(
            "sys.modules", {"fitz": fake_fitz}
        ):
            path = Path(temporary_directory) / "input.pdf"
            path.write_bytes(b"%PDF-1.7\n")
            inputs = iter(extractor._prepare_inputs(
                path, [0, 1], input_kind="pdf", temp_dir=Path(temporary_directory)
            ))
            next(inputs)
            pages[0].get_pixmap.assert_called_once()
            pages[1].get_pixmap.assert_not_called()

    def test_paddle_prediction_results_are_bounded_before_materialization(self) -> None:
        extractor = PaddleOCRExtractor(
            {
                "pipeline_name": "PPStructureV3",
                "network_access": False,
                "max_prediction_results_per_page": 2,
                "max_output_blocks": 2,
            },
            model_manifest={},
            model_manifest_hash=SHA,
            config_hash=SHA,
        )
        consumed = []

        def predictions(**kwargs):
            for index in range(5):
                consumed.append(index)
                yield {"index": index}

        pipeline = SimpleNamespace(predict=predictions)
        with patch.object(extractor, "_verify_runtime", return_value="3.2.2"), patch.object(
            extractor, "_build_pipeline", return_value=pipeline
        ), patch.object(
            extractor,
            "_prepare_inputs",
            return_value=iter([(0, Path("page.png"), 10.0, 10.0, 1.0)]),
        ), patch.object(extractor, "_normalize_blocks", return_value=[]):
            with self.assertRaises(ExtractorError) as raised:
                extractor.extract(Path("input.pdf"), [0])
        self.assertEqual(raised.exception.code, "paddleocr_output_limit_exceeded")
        self.assertLessEqual(len(consumed), 3)

    def test_paddle_preserves_typed_extractor_timeout(self) -> None:
        extractor = PaddleOCRExtractor(
            {"pipeline_name": "PPStructureV3", "network_access": False},
            model_manifest={},
            model_manifest_hash=SHA,
            config_hash=SHA,
        )

        def timeout(**kwargs):
            raise ExtractorError("paddleocr_timeout", "deadline")

        pipeline = SimpleNamespace(predict=timeout)
        with patch.object(extractor, "_verify_runtime", return_value="3.2.2"), patch.object(
            extractor, "_build_pipeline", return_value=pipeline
        ), patch.object(
            extractor,
            "_prepare_inputs",
            return_value=iter([(0, Path("page.png"), 10.0, 10.0, 1.0)]),
        ):
            with self.assertRaises(ExtractorError) as raised:
                extractor.extract(Path("input.pdf"), [0])
        self.assertEqual(raised.exception.code, "paddleocr_timeout")

    def test_paddle_runtime_verifies_every_frozen_dependency_exactly(self) -> None:
        dependency_versions = {
            "paddleocr": "3.7.0",
            "paddlepaddle": "3.2.2",
            "PyMuPDF": "1.28.0",
            "Pillow": "12.3.0",
        }
        extractor = PaddleOCRExtractor(
            {
                "pipeline_name": "PPStructureV3",
                "network_access": False,
                "runtime_version": "3.2.2",
                "dependency_versions": dependency_versions,
            },
            model_manifest={},
            model_manifest_hash=SHA,
            config_hash=SHA,
        )
        with patch.object(
            importlib.metadata,
            "version",
            side_effect=lambda package: dependency_versions[package],
        ):
            self.assertEqual(extractor._verify_runtime(), "3.2.2")


class EvidenceLocatorAndConfidenceTests(unittest.TestCase):
    def _asset(self, **overrides) -> EvidenceAsset:
        values = {
            "source_item_id": uuid.uuid4(),
            "origin_run_source_item_id": uuid.uuid4(),
            "derivation_type": EvidenceDerivationType.OTHER,
            "generic_extraction_attempt_id": uuid.uuid4(),
            "kind": EvidenceKind.TEXT,
            "locator_type": LocatorType.HTML_DOM,
            "locator": {
                "locator_type": LocatorType.HTML_DOM,
                "css_selector": "main > p:nth-of-type(1)",
                "xpath": None,
            },
            "extraction_method": ExtractionEngine.HTML,
            "validation_mode": GenericValidationMode.DETERMINISTIC,
            "evidence_content_hash": SHA,
            "review_subject_hash": SHA,
        }
        values.update(overrides)
        return EvidenceAsset(**values)

    def test_locator_requires_type_specific_fields_and_engine_matrix(self) -> None:
        with self.assertRaises(ValidationError):
            self._asset(locator={"locator_type": LocatorType.HTML_DOM}).clean()
        with self.assertRaises(ValidationError):
            self._asset(
                locator_type=LocatorType.STRUCTURED_PATH,
                locator={
                    "locator_type": LocatorType.STRUCTURED_PATH,
                    "path_type": "json_pointer",
                    "path": "/x",
                },
            ).clean()

    def test_numeric_confidence_requires_complete_calibration_material(self) -> None:
        with self.assertRaises(ValidationError):
            self._asset(validation_mode=None, confidence="0.8000000").clean()
        with self.assertRaises(ValidationError):
            self._asset(
                validation_mode=GenericValidationMode.CALIBRATED,
                confidence="0.8000000",
                calibration_profile_hash=SHA,
            ).clean()

    def test_locator_rejects_unknown_fields_and_ambiguous_unions(self) -> None:
        with self.assertRaises(ValidationError):
            self._asset(
                locator={
                    "locator_type": LocatorType.HTML_DOM,
                    "css_selector": "main",
                    "xpath": "/html/body/main",
                }
            ).clean()
        with self.assertRaises(ValidationError):
            self._asset(
                locator={
                    "locator_type": LocatorType.HTML_DOM,
                    "css_selector": "main",
                    "xpath": None,
                    "unexpected": "not allowed",
                }
            ).clean()
        with self.assertRaises(ValidationError):
            self._asset(
                extraction_method=ExtractionEngine.HWPX,
                locator_type=LocatorType.HWPX_PATH,
                locator={
                    "locator_type": LocatorType.HWPX_PATH,
                    "section_path": "Contents/section0.xml",
                    "paragraph_id": "p1",
                    "table_id": "t1",
                    "row_index": 0,
                    "column_index": 0,
                    "embedded_object_id": None,
                },
            ).clean()


if __name__ == "__main__":
    unittest.main()
