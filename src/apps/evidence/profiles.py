from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from django.conf import settings
from django.core.exceptions import ValidationError

from adapters.extractors.base import sha256_file

from .models import ExtractionEngine, ExtractionProfileSnapshot
from .services import canonical_hash


MVP_PROFILE_KEYS = frozenset({
    "native-pdf-v1",
    "paddle-ko-v1",
    "paddle-en-v1",
    "html-deterministic-v1",
    "structured-deterministic-v1",
    "spreadsheet-deterministic-v1",
    "hwpx-deterministic-v1",
    "legacy-hwp-v1",
    "browser-capture-deterministic-v1",
    "media-deterministic-v1",
    "manual-entry-v1",
})


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def _load_json_file(path_value: str, *, label: str) -> tuple[Path, dict[str, Any]]:
    if not path_value or "$" in path_value or "%" in path_value:
        raise ValidationError(f"{label} path contains an unresolved environment variable")
    path = Path(path_value)
    if not path.is_absolute():
        raise ValidationError(f"{label} path must be absolute")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{label} is missing or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object")
    return path, value


def _resolve_deployment_material(document: dict[str, Any]) -> dict[str, Any]:
    model_manifest_file = document.get("model_manifest_file")
    if model_manifest_file:
        _, model_manifest = _load_json_file(str(model_manifest_file), label="PaddleOCR model manifest")
        if model_manifest.get("schema_version") != "v1" or not model_manifest.get("models"):
            raise ValidationError("PaddleOCR model manifest has no verified model entries")
        document["model_manifest"] = model_manifest

    config = document.get("config")
    if document.get("engine") == "legacy_hwp_converter" and isinstance(config, dict):
        manifest_path = str(config.get("converter_manifest_path", ""))
        path, manifest = _load_json_file(manifest_path, label="Legacy HWP converter manifest")
        if manifest.get("schema_version") != "v1" or not manifest.get("files"):
            raise ValidationError("Legacy HWP converter manifest has no verified file entries")
        config["converter_manifest_path"] = str(path)
        config["converter_manifest_hash"] = sha256_file(path)
    return document


def load_profile_documents(root: Path) -> list[dict[str, Any]]:
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Invalid extraction profile manifest: {manifest_path}") from exc
    entries = manifest.get("profiles")
    if not isinstance(entries, list):
        raise ValidationError("Extraction profile manifest must contain a profiles array")
    documents = []
    for entry in entries:
        relative = Path(str(entry.get("file", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError("Profile paths must remain within the profile root")
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
            document = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Invalid extraction profile file: {relative}") from exc
        document = _resolve_deployment_material(_expand(document))
        document["_profile_file"] = relative.as_posix()
        documents.append(document)
    return documents


def implementation_manifest(document: Mapping[str, Any], repository_root: Path | None = None) -> dict[str, Any]:
    repository_root = repository_root or settings.REPOSITORY_ROOT
    entries = []
    for raw in document.get("implementation_files", []):
        relative = Path(str(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError("Implementation paths must remain within the repository")
        path = (repository_root / relative).resolve()
        try:
            path.relative_to(repository_root.resolve())
        except ValueError as exc:
            raise ValidationError("Implementation path escaped the repository") from exc
        if not path.is_file():
            raise ValidationError(f"Implementation file is missing: {relative.as_posix()}")
        entries.append({"path": relative.as_posix(), "sha256": sha256_file(path)})
    if not entries:
        raise ValidationError("At least one implementation file is required")
    return {"schema_version": "v1", "files": sorted(entries, key=lambda item: item["path"])}


def profile_snapshot_values(document: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(document.get("config") or {})
    config_hash = canonical_hash(config)
    code_manifest = implementation_manifest(document)
    implementation_hash = canonical_hash(code_manifest)
    model_manifest = _normalize_model_manifest(document.get("model_manifest"))
    model_manifest_hash = canonical_hash(model_manifest) if model_manifest is not None else None
    values = {
        "profile_key": document["profile_key"],
        "profile_version": str(document["profile_version"]),
        "engine": document["engine"],
        "extractor_version": str(document["extractor_version"]),
        "package_version": document.get("package_version"),
        "runtime_version": document.get("runtime_version"),
        "pipeline_name": document.get("pipeline_name"),
        "implementation_manifest_hash": implementation_hash,
        "config": config,
        "config_hash": config_hash,
        "validation_mode": document.get("validation_mode"),
        "calibration_profile_key": document.get("calibration_profile_key"),
        "calibration_profile_version": document.get("calibration_profile_version"),
        "calibration_manifest_object_key": document.get("calibration_manifest_object_key"),
        "calibration_manifest_object_version": document.get("calibration_manifest_object_version"),
        "calibration_profile_hash": document.get("calibration_profile_hash"),
        "model_manifest": model_manifest,
        "model_manifest_hash": model_manifest_hash,
    }
    values["profile_material_hash"] = canonical_hash({
        "extractor_version": values["extractor_version"],
        "package_version": values["package_version"],
        "runtime_version": values["runtime_version"],
        "pipeline_name": values["pipeline_name"],
        "implementation_manifest_hash": values["implementation_manifest_hash"],
        "config_hash": values["config_hash"],
        "validation_mode": values["validation_mode"],
        "calibration_profile_key": values["calibration_profile_key"],
        "calibration_profile_version": values["calibration_profile_version"],
        "calibration_profile_hash": values["calibration_profile_hash"],
        "model_manifest_hash": values["model_manifest_hash"],
    })
    return values


def _normalize_model_manifest(value: Any) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("models"), list):
        return value
    models = []
    for raw in value["models"]:
        model = dict(raw)
        if isinstance(model.get("files"), list):
            model["files"] = sorted((dict(item) for item in model["files"]), key=lambda item: item.get("path", ""))
        models.append(model)
    normalized = dict(value)
    normalized["models"] = sorted(models, key=lambda item: item.get("model_name", ""))
    return normalized


def verify_local_profile(profile: ExtractionProfileSnapshot) -> dict[str, Any]:
    stages = []
    samples = []

    def stage(code: str, passed: bool, detail: str | None = None) -> None:
        stages.append({
            "code": code,
            "result": "passed" if passed else "failed",
            "detailRedacted": detail,
        })

    stage("config.hash", canonical_hash(profile.config) == profile.config_hash)
    dependency = _dependency_for_engine(profile.engine)
    if dependency:
        package, expected = dependency
        try:
            installed = importlib.metadata.version(package)
            passed = expected is None or installed == expected or installed.startswith(expected)
            stage("dependency.version", passed, f"{package}={installed}")
        except importlib.metadata.PackageNotFoundError:
            stage("dependency.version", False, f"{package} is not installed")
    else:
        stage("dependency.version", True, "stdlib/local wrapper")
    if profile.engine == ExtractionEngine.PADDLEOCR:
        stage("paddle.pipeline", profile.pipeline_name == "PPStructureV3")
        stage("paddle.package", profile.package_version == "3.7.0")
        models_passed = True
        for model in (profile.model_manifest or {}).get("models", []):
            directory = Path(str(model.get("directory", "")))
            files = model.get("files") or []
            if not directory.is_absolute() or not files:
                models_passed = False
                continue
            for item in files:
                expected = str(item.get("sha256", ""))
                candidate = directory / str(item.get("path", ""))
                if not candidate.is_file() or set(expected) == {"0"} or sha256_file(candidate) != expected:
                    models_passed = False
        stage("paddle.local_models", models_passed, "All model files must be preloaded with exact SHA-256")
    if profile.engine == ExtractionEngine.LEGACY_HWP:
        command = profile.config.get("sandbox_command") or []
        manifest_hash = str(profile.config.get("converter_manifest_hash", ""))
        wrapper = Path(str(command[0])) if command else Path("")
        stage(
            "hwp.sandbox_wrapper",
            bool(command) and wrapper.is_absolute() and wrapper.is_file(),
            "A deployed absolute sandbox wrapper is required",
        )
        stage(
            "hwp.converter_manifest",
            len(manifest_hash) == 64 and set(manifest_hash) != {"0"},
            "Replace the deployment placeholder with the approved converter manifest hash",
        )
    sample_hash = canonical_hash({"profile": str(profile.id), "material": profile.profile_material_hash})
    overall = all(item["result"] == "passed" for item in stages)
    samples.append({
        "sampleId": "static-profile-contract",
        "inputClass": profile.engine,
        "sampleHash": sample_hash,
        "expectedOutcomeHash": canonical_hash({"result": "passed"}),
        "observedOutcomeHash": canonical_hash({"result": "passed" if overall else "failed"}),
        "result": "passed" if overall else "failed",
        "sensitiveDataExcluded": True,
        "detailRedacted": "Static dependency, manifest and configuration verification",
    })
    return {
        "subjectType": "extraction_profile",
        "subjectId": str(profile.id),
        "subjectMaterialHash": profile.profile_material_hash,
        "sampleManifestHash": canonical_hash(samples),
        "samples": samples,
        "metrics": [{"metricKey": "stages.passed", "value": sum(
            item["result"] == "passed" for item in stages
        ), "unit": "count", "sampleCount": len(stages)}],
        "thresholds": [{
            "metricKey": "stages.passed", "comparator": "eq", "threshold": len(stages),
            "observed": sum(item["result"] == "passed" for item in stages),
            "result": "passed" if overall else "failed",
        }],
        "stageResults": stages,
        "overallResult": "passed" if overall else "failed",
    }


def _dependency_for_engine(engine: str) -> tuple[str, str | None] | None:
    return {
        ExtractionEngine.NATIVE_PDF: ("PyMuPDF", None),
        ExtractionEngine.PADDLEOCR: ("paddleocr", "3.7.0"),
        ExtractionEngine.HTML: ("selectolax", None),
        ExtractionEngine.SPREADSHEET: ("openpyxl", None),
        ExtractionEngine.HWPX: ("defusedxml", None),
        ExtractionEngine.BROWSER_CAPTURE: ("playwright", None),
        ExtractionEngine.MEDIA: ("Pillow", None),
    }.get(engine)
