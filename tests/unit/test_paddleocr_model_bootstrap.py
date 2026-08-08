import json
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django
from django.core.exceptions import ValidationError
from django.core.management import call_command

django.setup()

from adapters.extractors.base import ExtractorError
from adapters.extractors.paddleocr import PaddleOCRExtractor
from apps.evidence.services import canonical_hash
from apps.evidence.profiles import load_profile_documents


class PaddleOCRModelBootstrapTests(unittest.TestCase):
    maxDiff = None
    model_names = (
        "PP-Chart2Table",
        "PP-DocLayout_plus-L",
        "PP-FormulaNet_plus-M",
        "PP-LCNet_x1_0_doc_ori",
        "PP-LCNet_x1_0_table_cls",
        "PP-LCNet_x1_0_textline_ori",
        "PP-OCRv5_server_det",
        "RT-DETR-L_wired_table_cell_det",
        "RT-DETR-L_wireless_table_cell_det",
        "SLANet_plus",
        "SLANeXt_wired",
        "UVDoc",
        "en_PP-OCRv5_mobile_rec",
        "korean_PP-OCRv5_mobile_rec",
    )

    def _manifest_for(self, model_root: Path) -> dict:
        for model_name in self.model_names:
            model_dir = model_root / model_name
            model_dir.mkdir(parents=True)
            (model_dir / "model.bin").write_bytes(b"x")
        subprocess.run(
            [
                sys.executable,
                "deploy/containers/paddleocr-model-bootstrap/bootstrap.py",
                "--model-root",
                str(model_root),
                "--manifest-only",
            ],
            cwd=Path(__file__).resolve().parents[2],
            check=True,
        )
        return json.loads((model_root / "manifest.json").read_text(encoding="utf-8"))

    def _profile_root_for(self, temporary_root: Path) -> Path:
        profiles_root = temporary_root / "profiles"
        profiles_root.mkdir()
        (profiles_root / "manifest.json").write_text(
            json.dumps({"profiles": [{"file": "paddle.json"}]}), encoding="utf-8"
        )
        (profiles_root / "paddle.json").write_text(
            json.dumps(
                {
                    "profile_key": "paddle-test-v1",
                    "profile_version": "1.0.0",
                    "engine": "paddleocr_ppstructurev3",
                    "model_manifest_file": "${PADDLEOCR_MODEL_MANIFEST_ROOT}/manifest.json",
                }
            ),
            encoding="utf-8",
        )
        return profiles_root

    def _extractor_for(self, manifest: dict) -> PaddleOCRExtractor:
        return PaddleOCRExtractor(
            {"pipeline_name": "PPStructureV3", "network_access": False},
            model_manifest=manifest,
            model_manifest_hash=canonical_hash(manifest),
            config_hash="0" * 64,
        )

    def test_profile_loading_rejects_unmanifested_model_file(self) -> None:
        """An extra deployed file must not escape the immutable model material."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            models_root = temporary_root / "models"
            self._manifest_for(models_root)
            (models_root / "PP-DocLayout_plus-L" / "unexpected.bin").write_bytes(b"unexpected")
            with patch.dict(os.environ, {"PADDLEOCR_MODEL_MANIFEST_ROOT": str(models_root)}):
                with self.assertRaises(ValidationError):
                    load_profile_documents(self._profile_root_for(temporary_root))

    def test_profile_loading_rejects_duplicate_model_manifest_path(self) -> None:
        """A duplicated manifest path must not mask an ambiguous deployed file contract."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            models_root = temporary_root / "models"
            manifest = self._manifest_for(models_root)
            manifest["models"][0]["files"].append(dict(manifest["models"][0]["files"][0]))
            (models_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch.dict(os.environ, {"PADDLEOCR_MODEL_MANIFEST_ROOT": str(models_root)}):
                with self.assertRaises(ValidationError):
                    load_profile_documents(self._profile_root_for(temporary_root))

    def test_runtime_rejects_unmanifested_model_file(self) -> None:
        """The worker must re-check that a model directory contains only manifest files."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            models_root = Path(temporary_directory) / "models"
            manifest = self._manifest_for(models_root)
            (models_root / "PP-DocLayout_plus-L" / "unexpected.bin").write_bytes(b"unexpected")
            with self.assertRaises(ExtractorError):
                self._extractor_for(manifest)._verify_local_models()

    def test_runtime_rejects_duplicate_model_manifest_path(self) -> None:
        """The worker must reject duplicate file entries before constructing PPStructureV3."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            models_root = Path(temporary_directory) / "models"
            manifest = self._manifest_for(models_root)
            manifest["models"][0]["files"].append(dict(manifest["models"][0]["files"][0]))
            with self.assertRaises(ExtractorError):
                self._extractor_for(manifest)._verify_local_models()

    def test_offline_command_validates_profile_documents_without_database_snapshots(self) -> None:
        """A completed model volume must be accepted before profile snapshots exist."""
        model_names = (
            "PP-Chart2Table",
            "PP-DocLayout_plus-L",
            "PP-FormulaNet_plus-M",
            "PP-LCNet_x1_0_doc_ori",
            "PP-LCNet_x1_0_table_cls",
            "PP-LCNet_x1_0_textline_ori",
            "PP-OCRv5_server_det",
            "RT-DETR-L_wired_table_cell_det",
            "RT-DETR-L_wireless_table_cell_det",
            "SLANet_plus",
            "SLANeXt_wired",
            "UVDoc",
            "en_PP-OCRv5_mobile_rec",
            "korean_PP-OCRv5_mobile_rec",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            model_root = Path(temporary_directory) / "models"
            for model_name in model_names:
                model_dir = model_root / model_name
                model_dir.mkdir(parents=True)
                (model_dir / "model.bin").write_bytes(b"x")
            subprocess.run(
                [
                    sys.executable,
                    "deploy/containers/paddleocr-model-bootstrap/bootstrap.py",
                    "--model-root",
                    str(model_root),
                    "--manifest-only",
                ],
                cwd=Path(__file__).resolve().parents[2],
                check=True,
            )
            with patch.dict(os.environ, {"PADDLEOCR_MODEL_MANIFEST_ROOT": str(model_root)}):
                output = StringIO()
                try:
                    call_command(
                        "verify_ocr_manifest",
                        profiles="config/extraction-profiles/paddleocr",
                        stdout=output,
                    )
                    result = output.getvalue()
                except Exception as exc:  # The pre-T014 command attempts a database query.
                    result = str(exc)
        self.assertIn("verified 2 PaddleOCR profiles", result)

    def test_profile_loading_rejects_zero_hash_model_manifest(self) -> None:
        """A placeholder digest would otherwise permit an unverified OCR deployment."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            profiles_root = temporary_root / "profiles"
            models_root = temporary_root / "models"
            profiles_root.mkdir()
            model_dir = models_root / "PP-DocLayout_plus-L"
            model_dir.mkdir(parents=True)
            (model_dir / "model.pdmodel").write_bytes(b"model")
            (models_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "v1",
                        "paddleocr_version": "3.7.0",
                        "pipeline": "PPStructureV3",
                        "models": [
                            {
                                "model_name": "PP-DocLayout_plus-L",
                                "directory": str(model_dir.resolve()),
                                "files": [
                                    {
                                        "path": "model.pdmodel",
                                        "sha256": "0" * 64,
                                        "byte_size": 5,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (profiles_root / "manifest.json").write_text(
                json.dumps({"profiles": [{"file": "paddle.json"}]}), encoding="utf-8"
            )
            (profiles_root / "paddle.json").write_text(
                json.dumps(
                    {
                        "profile_key": "paddle-test-v1",
                        "profile_version": "1.0.0",
                        "engine": "paddleocr_ppstructurev3",
                        "model_manifest_file": "${PADDLEOCR_MODEL_MANIFEST_ROOT}/manifest.json",
                    }
                ),
                encoding="utf-8",
            )
            previous_root = os.environ.get("PADDLEOCR_MODEL_MANIFEST_ROOT")
            os.environ["PADDLEOCR_MODEL_MANIFEST_ROOT"] = str(models_root)
            try:
                with self.assertRaises(ValidationError):
                    load_profile_documents(profiles_root)
            finally:
                if previous_root is None:
                    os.environ.pop("PADDLEOCR_MODEL_MANIFEST_ROOT", None)
                else:
                    os.environ["PADDLEOCR_MODEL_MANIFEST_ROOT"] = previous_root

    def test_manifest_only_writes_sorted_file_hashes_for_preloaded_models(self) -> None:
        """A changed checksum, size, or file ordering must invalidate deployment material."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            model_root = Path(temporary_directory) / "models"
            model_dir = model_root / "PP-DocLayout_plus-L"
            model_dir.mkdir(parents=True)
            (model_dir / "z.bin").write_bytes(b"z")
            (model_dir / "a.bin").write_bytes(b"abc")

            result = subprocess.run(
                [
                    sys.executable,
                    "deploy/containers/paddleocr-model-bootstrap/bootstrap.py",
                    "--model-root",
                    str(model_root),
                    "--manifest-only",
                ],
                cwd=Path(__file__).resolve().parents[2],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((model_root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest,
                {
                    "schema_version": "v1",
                    "paddleocr_version": "3.7.0",
                    "pipeline": "PPStructureV3",
                    "models": [
                        {
                            "model_name": "PP-DocLayout_plus-L",
                            "directory": str(model_dir.resolve()),
                            "files": [
                                {
                                    "path": "a.bin",
                                    "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                                    "byte_size": 3,
                                },
                                {
                                    "path": "z.bin",
                                    "sha256": "594e519ae499312b29433b7dd8a97ff068defcba9755b6d5d00e84c524d67b06",
                                    "byte_size": 1,
                                },
                            ],
                        }
                    ],
                },
            )
