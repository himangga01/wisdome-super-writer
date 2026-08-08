"""Download the approved PaddleOCR 3.7.0 PPStructureV3 models once and pin them.

The worker never invokes this module.  It consumes the resulting volume read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
from pathlib import Path
from typing import Any


PADDLEOCR_VERSION = "3.7.0"
PIPELINE_NAME = "PPStructureV3"
MODEL_NAMES = (
    "PP-Chart2Table",
    "PP-DocLayout_plus-L",
    "PP-FormulaNet_plus-M",
    "PP-LCNet_x1_0_doc_ori",
    "PP-LCNet_x1_0_textline_ori",
    "PP-OCRv5_server_det",
    "SLANeXt_wired",
    "UVDoc",
    "en_PP-OCRv5_mobile_rec",
    "korean_PP-OCRv5_mobile_rec",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(model_root: Path) -> dict[str, Any]:
    """Build the canonical deployment material from only approved model directories."""
    model_entries = []
    for model_dir in sorted((path for path in model_root.iterdir() if path.is_dir()), key=lambda path: path.name):
        files = []
        for path in sorted((item for item in model_dir.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
            files.append({
                "path": path.relative_to(model_dir).as_posix(),
                "sha256": sha256_file(path),
                "byte_size": path.stat().st_size,
            })
        if files:
            model_entries.append({
                "model_name": model_dir.name,
                "directory": str(model_dir.resolve()),
                "files": files,
            })
    return {
        "schema_version": "v1",
        "paddleocr_version": PADDLEOCR_VERSION,
        "pipeline": PIPELINE_NAME,
        "models": model_entries,
    }


def _download_pinned_models(model_root: Path) -> None:
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(model_root)
    os.environ["PADDLEOCR_HOME"] = str(model_root)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "true"
    installed = importlib.metadata.version("paddleocr")
    if installed != PADDLEOCR_VERSION:
        raise RuntimeError(f"Expected paddleocr=={PADDLEOCR_VERSION}, found {installed}")

    from paddleocr import PPStructureV3

    common = {
        "layout_detection_model_name": "PP-DocLayout_plus-L",
        "chart_recognition_model_name": "PP-Chart2Table",
        "doc_orientation_classify_model_name": "PP-LCNet_x1_0_doc_ori",
        "doc_unwarping_model_name": "UVDoc",
        "text_detection_model_name": "PP-OCRv5_server_det",
        "textline_orientation_model_name": "PP-LCNet_x1_0_textline_ori",
        "wired_table_structure_recognition_model_name": "SLANeXt_wired",
        "formula_recognition_model_name": "PP-FormulaNet_plus-M",
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": True,
        "use_textline_orientation": True,
        "use_table_recognition": True,
        "use_formula_recognition": True,
        "use_chart_recognition": True,
        "use_seal_recognition": False,
        "device": "cpu",
    }
    for recognition_model in ("korean_PP-OCRv5_mobile_rec", "en_PP-OCRv5_mobile_rec"):
        PPStructureV3(
            **common,
            text_recognition_model_name=recognition_model,
        )


def _model_directory(model_root: Path, model_name: str) -> Path:
    for candidate in (model_root / "official_models" / model_name, model_root / model_name):
        if candidate.is_dir():
            return candidate
    cached = Path.home() / ".paddlex" / "official_models" / model_name
    if cached.is_dir():
        destination = model_root / "official_models" / model_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(cached, destination, dirs_exist_ok=True)
        return destination
    raise RuntimeError(f"Pinned model was not downloaded: {model_name}")


def _write_manifest(model_root: Path) -> None:
    approved_root = model_root / "official_models"
    approved_root.mkdir(parents=True, exist_ok=True)
    for model_name in MODEL_NAMES:
        _model_directory(model_root, model_name)
    manifest = build_manifest(approved_root)
    names = [entry["model_name"] for entry in manifest["models"]]
    if names != list(MODEL_NAMES):
        raise RuntimeError(f"Unexpected or missing PaddleOCR models: {names}")
    (model_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-root", default=os.environ.get("PADDLEOCR_MODEL_MANIFEST_ROOT", "/models/paddleocr"))
    parser.add_argument("--manifest-only", action="store_true")
    options = parser.parse_args()
    model_root = Path(options.model_root).resolve()
    model_root.mkdir(parents=True, exist_ok=True)
    if options.manifest_only:
        manifest = build_manifest(model_root)
        (model_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return
    _download_pinned_models(model_root)
    _write_manifest(model_root)


if __name__ == "__main__":
    main()
