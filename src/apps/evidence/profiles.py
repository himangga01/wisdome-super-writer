from __future__ import annotations

import importlib.metadata
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from django.conf import settings
from django.core.exceptions import ValidationError

from adapters.extractors.base import ExtractorError, sha256_file
from adapters.extractors.legacy_hwp import (
    probe_legacy_hwp_sandbox,
    validate_legacy_hwp_activation_config,
)

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

PADDLEOCR_VERSION = "3.7.0"
PADDLEOCR_PIPELINE = "PPStructureV3"
PADDLEOCR_REQUIRED_MODELS = frozenset({
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
})

LEGACY_HWP_CONVERTER = {
    "name": "rhwp",
    "version": "0.8.2",
    "source_commit": "9b16aa9e23f476e2b335d7c029fc9f24a199d63c",
    "rust_version": "1.93.1",
    "cargo_locked": True,
}
LEGACY_HWP_REQUIRED_MANIFEST_ROLES = frozenset(
    {
        "converter", "qpdf", "wrapper", "config", "font", "fontconfig", "runtime",
        "library", "license", "lockfile", "build-metadata",
    }
)


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
        _validate_paddle_model_manifest(model_manifest, verify_files=True)
        document["model_manifest"] = model_manifest

    config = document.get("config")
    if document.get("engine") == "legacy_hwp_converter" and isinstance(config, dict):
        if (
            config.get("golden_corpus_approved") is True
            and not validate_legacy_hwp_activation_config(config)
        ) or (
            config.get("golden_corpus_approved") is not True
            and config.get("golden_corpus_acceptance") is not None
        ):
            raise ValidationError(
                "Legacy HWP activation requires an immutable all-pass T032 acceptance artifact"
            )
        manifest_path = str(config.get("converter_manifest_path", ""))
        path, manifest = _load_json_file(manifest_path, label="Legacy HWP converter manifest")
        expected_hash = str(config.get("converter_manifest_hash", ""))
        _validate_legacy_hwp_manifest(manifest)
        if not _is_nonzero_sha256(expected_hash) or sha256_file(path) != expected_hash:
            raise ValidationError(
                "Legacy HWP converter manifest differs from the release-pinned expected hash"
            )
        config["converter_manifest_path"] = str(path)
        config["converter_manifest_hash"] = expected_hash
        config["converter_manifest"] = manifest
    return document


def _is_nonzero_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) != {"0"}
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_legacy_hwp_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != "v1" or manifest.get("converter") != LEGACY_HWP_CONVERTER:
        raise ValidationError("Legacy HWP converter identity is not the approved pinned material")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValidationError("Legacy HWP converter manifest has no verified file entries")
    paths: set[str] = set()
    roles: set[str] = set()
    for raw_file in files:
        if not isinstance(raw_file, Mapping):
            raise ValidationError("Legacy HWP converter manifest entry is invalid")
        role = raw_file.get("role")
        raw_path = raw_file.get("path")
        file_hash = raw_file.get("sha256")
        byte_size = raw_file.get("byte_size")
        if not isinstance(raw_path, str):
            raise ValidationError("Legacy HWP converter manifest path is invalid")
        manifest_path = PurePosixPath(raw_path)
        normalized = manifest_path.as_posix()
        if (
            role not in LEGACY_HWP_REQUIRED_MANIFEST_ROLES
            or not manifest_path.is_absolute()
            or ".." in manifest_path.parts
            or normalized != raw_path
            or normalized in paths
            or not _is_nonzero_sha256(file_hash)
            or type(byte_size) is not int
            or byte_size < 1
        ):
            raise ValidationError("Legacy HWP converter manifest file entry is invalid")
        paths.add(normalized)
        roles.add(str(role))
    if not LEGACY_HWP_REQUIRED_MANIFEST_ROLES.issubset(roles):
        raise ValidationError("Legacy HWP converter manifest is missing a required material role")


def _validate_paddle_model_manifest(manifest: Mapping[str, Any], *, verify_files: bool) -> None:
    if (
        manifest.get("schema_version") != "v1"
        or manifest.get("paddleocr_version") != PADDLEOCR_VERSION
        or manifest.get("pipeline") != PADDLEOCR_PIPELINE
    ):
        raise ValidationError("PaddleOCR model manifest version or pipeline is invalid")
    models = manifest.get("models")
    if not isinstance(models, list) or not models:
        raise ValidationError("PaddleOCR model manifest has no verified model entries")
    names: set[str] = set()
    for raw_model in models:
        if not isinstance(raw_model, Mapping):
            raise ValidationError("PaddleOCR model entry is invalid")
        model_name = str(raw_model.get("model_name", ""))
        directory = Path(str(raw_model.get("directory", "")))
        files = raw_model.get("files")
        if not model_name or model_name in names or not directory.is_absolute() or not isinstance(files, list) or not files:
            raise ValidationError("PaddleOCR model entry is incomplete")
        names.add(model_name)
        manifest_paths: set[str] = set()
        for raw_file in files:
            if not isinstance(raw_file, Mapping):
                raise ValidationError("PaddleOCR model file entry is invalid")
            relative = Path(str(raw_file.get("path", "")))
            expected_hash = str(raw_file.get("sha256", ""))
            byte_size = raw_file.get("byte_size")
            if (
                not relative.parts
                or relative.is_absolute()
                or ".." in relative.parts
                or len(expected_hash) != 64
                or set(expected_hash) == {"0"}
                or any(character not in "0123456789abcdef" for character in expected_hash)
                or not isinstance(byte_size, int)
                or byte_size < 1
            ):
                raise ValidationError("PaddleOCR model file manifest is invalid")
            relative_path = relative.as_posix()
            if relative_path in manifest_paths:
                raise ValidationError("PaddleOCR model manifest has duplicate file paths")
            manifest_paths.add(relative_path)
            candidate = (directory / relative).resolve()
            try:
                candidate.relative_to(directory.resolve())
            except ValueError as exc:
                raise ValidationError("PaddleOCR model path escapes its directory") from exc
            if verify_files and (
                not candidate.is_file()
                or candidate.is_symlink()
                or candidate.stat().st_size != byte_size
                or sha256_file(candidate) != expected_hash
            ):
                raise ValidationError("PaddleOCR model file differs from its manifest")
        if verify_files:
            actual_paths = {
                path.relative_to(directory).as_posix()
                for path in directory.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            if actual_paths != manifest_paths:
                raise ValidationError("PaddleOCR model directory differs from its manifest")
    if names != PADDLEOCR_REQUIRED_MODELS:
        raise ValidationError("PaddleOCR model manifest has missing or unexpected models")


def load_profile_documents(root: Path) -> list[dict[str, Any]]:
    manifest_path = root / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Invalid extraction profile manifest: {manifest_path}") from exc
        entries = manifest.get("profiles")
        if not isinstance(entries, list):
            raise ValidationError("Extraction profile manifest must contain a profiles array")
    else:
        entries = [{"file": path.name} for path in sorted(root.glob("*.json"))]
        if not entries:
            raise ValidationError(f"No extraction profile documents found: {root}")
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
        stage("paddle.pipeline", profile.pipeline_name == PADDLEOCR_PIPELINE)
        stage("paddle.package", profile.package_version == PADDLEOCR_VERSION)
        try:
            _validate_paddle_model_manifest(profile.model_manifest or {}, verify_files=True)
            models_passed = True
        except ValidationError:
            models_passed = False
        stage("paddle.local_models", models_passed, "All approved model files must be preloaded with exact SHA-256 and size")
    if profile.engine == ExtractionEngine.LEGACY_HWP:
        socket_path = Path(str(profile.config.get("sandbox_socket_path", "")))
        manifest_path = Path(str(profile.config.get("converter_manifest_path", "")))
        manifest_hash = str(profile.config.get("converter_manifest_hash", ""))
        socket_passed = False
        try:
            socket_passed = (
                socket_path.is_absolute()
                and not socket_path.is_symlink()
                and stat.S_ISSOCK(socket_path.lstat().st_mode)
            )
        except OSError:
            pass
        stage(
            "hwp.sandbox_socket",
            socket_passed,
            "A deployed absolute Unix-domain socket (not a path placeholder) is required",
        )
        manifest_passed = False
        try:
            _, local_manifest = _load_json_file(
                str(manifest_path), label="Legacy HWP converter manifest"
            )
            _validate_legacy_hwp_manifest(local_manifest)
            manifest_passed = sha256_file(manifest_path) == manifest_hash
        except ValidationError:
            pass
        stage(
            "hwp.converter_manifest",
            manifest_passed,
            "The read-only manifest must match the release-pinned expected hash",
        )
        probe_passed = False
        if socket_passed and manifest_passed:
            try:
                probe_legacy_hwp_sandbox(str(socket_path), manifest_hash)
                probe_passed = True
            except ExtractorError:
                pass
        stage(
            "hwp.sandbox_identity_probe",
            probe_passed,
            "The bounded UDS protocol must echo the reviewed manifest and no-network policy",
        )
        stage(
            "hwp.golden_corpus",
            validate_legacy_hwp_activation_config(profile.config),
            "T032 immutable acceptance, image digest, manifest hash, schema and all-pass are required",
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
