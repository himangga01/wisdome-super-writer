from __future__ import annotations

import importlib.metadata
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from adapters.extractors.base import (
    ExtractionBlock,
    ExtractionOutput,
    ExtractorError,
    PageExtraction,
    canonical_bytes,
    sha256_bytes,
    sha256_file,
    sniff_mime,
)


class PaddleOCRExtractor:
    engine = "paddleocr_ppstructurev3"
    package_version = "3.7.0"
    pipeline_name = "PPStructureV3"
    required_models = frozenset({
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
    })
    model_options = {
        "PP-Chart2Table": ("chart_recognition_model_name", "chart_recognition_model_dir"),
        "PP-DocLayout_plus-L": ("layout_detection_model_name", "layout_detection_model_dir"),
        "PP-FormulaNet_plus-M": ("formula_recognition_model_name", "formula_recognition_model_dir"),
        "PP-LCNet_x1_0_doc_ori": (
            "doc_orientation_classify_model_name",
            "doc_orientation_classify_model_dir",
        ),
        "PP-LCNet_x1_0_textline_ori": (
            "textline_orientation_model_name",
            "textline_orientation_model_dir",
        ),
        "PP-OCRv5_server_det": ("text_detection_model_name", "text_detection_model_dir"),
        "SLANeXt_wired": (
            "wired_table_structure_recognition_model_name",
            "wired_table_structure_recognition_model_dir",
        ),
        "UVDoc": ("doc_unwarping_model_name", "doc_unwarping_model_dir"),
    }

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        model_manifest: Mapping[str, Any],
        model_manifest_hash: str,
        config_hash: str,
    ) -> None:
        self.config = dict(config)
        self.model_manifest = dict(model_manifest)
        self.expected_model_manifest_hash = model_manifest_hash
        self.expected_config_hash = config_hash
        self._pipeline = None
        if self.config.get("network_access", False):
            raise ExtractorError("config_mismatch", "PaddleOCR runtime network access must be disabled")
        if self.config.get("pipeline_name") != self.pipeline_name:
            raise ExtractorError("config_mismatch", "PaddleOCR pipeline must be PPStructureV3")

    def _verify_runtime(self) -> str:
        try:
            installed = importlib.metadata.version("paddleocr")
        except importlib.metadata.PackageNotFoundError as exc:
            raise ExtractorError("paddleocr_dependency_missing", "PaddleOCR is not installed") from exc
        if installed != self.package_version:
            raise ExtractorError("model_manifest_mismatch", "Installed PaddleOCR version is not 3.7.0")
        try:
            runtime = importlib.metadata.version("paddlepaddle")
        except importlib.metadata.PackageNotFoundError:
            try:
                runtime = importlib.metadata.version("paddlepaddle-gpu")
            except importlib.metadata.PackageNotFoundError as exc:
                raise ExtractorError("paddle_runtime_missing", "PaddlePaddle runtime is not installed") from exc
        if not runtime.startswith("3."):
            raise ExtractorError("model_manifest_mismatch", "PaddlePaddle runtime must be pinned to 3.x")
        expected_runtime = str(self.config.get("runtime_version", ""))
        if expected_runtime and runtime != expected_runtime:
            raise ExtractorError("model_manifest_mismatch", "PaddlePaddle runtime version differs from profile")
        return runtime

    def _verify_local_models(self) -> None:
        actual_manifest = {
            "schema_version": "v1",
            "paddleocr_version": self.package_version,
            "pipeline": self.pipeline_name,
            "models": [],
        }
        models = self.model_manifest.get("models")
        if (
            self.model_manifest.get("schema_version") != "v1"
            or self.model_manifest.get("paddleocr_version") != self.package_version
            or self.model_manifest.get("pipeline") != self.pipeline_name
            or not isinstance(models, list)
            or not models
        ):
            raise ExtractorError("model_manifest_mismatch", "PaddleOCR model manifest is empty")
        seen_names = set()
        for model in models:
            model_name = str(model.get("model_name", ""))
            directory = Path(str(model.get("directory", "")))
            files = model.get("files")
            if (
                not model_name
                or model_name in seen_names
                or not directory.is_absolute()
                or not isinstance(files, list)
                or not files
            ):
                raise ExtractorError("model_manifest_mismatch", "PaddleOCR model entry is incomplete")
            seen_names.add(model_name)
            verified_files = []
            for item in files:
                relative = Path(str(item.get("path", "")))
                expected = str(item.get("sha256", ""))
                byte_size = item.get("byte_size")
                if (
                    not relative.parts
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or len(expected) != 64
                    or set(expected) == {"0"}
                    or any(character not in "0123456789abcdef" for character in expected)
                    or not isinstance(byte_size, int)
                    or byte_size < 1
                ):
                    raise ExtractorError("model_manifest_mismatch", "PaddleOCR model file manifest is invalid")
                candidate = (directory / relative).resolve()
                try:
                    candidate.relative_to(directory.resolve())
                except ValueError as exc:
                    raise ExtractorError("model_manifest_mismatch", "PaddleOCR model path escapes its directory") from exc
                if (
                    not candidate.is_file()
                    or candidate.stat().st_size != byte_size
                    or sha256_file(candidate) != expected
                ):
                    raise ExtractorError("model_manifest_mismatch", "PaddleOCR model checksum differs from profile")
                verified_files.append({
                    "path": relative.as_posix(),
                    "sha256": expected,
                    "byte_size": byte_size,
                })
            actual_manifest["models"].append({
                "model_name": model_name,
                "directory": str(directory),
                "files": sorted(verified_files, key=lambda item: item["path"]),
            })
        if seen_names != self.required_models:
            raise ExtractorError("model_manifest_mismatch", "PaddleOCR model manifest has missing or unexpected models")
        actual_manifest["models"].sort(key=lambda item: item["model_name"])
        if sha256_bytes(canonical_bytes(actual_manifest)) != self.expected_model_manifest_hash:
            raise ExtractorError("model_manifest_mismatch", "PaddleOCR model manifest hash differs from profile")

    def _build_pipeline(self):
        if self._pipeline is not None:
            return self._pipeline
        self._verify_local_models()
        try:
            from paddleocr import PPStructureV3
        except ImportError as exc:
            raise ExtractorError("paddleocr_dependency_missing", "PPStructureV3 is unavailable") from exc
        model_dirs = {
            item["model_name"]: item["directory"] for item in self.model_manifest["models"]
        }
        recognition_model = str(self.config.get("text_recognition_model"))
        if recognition_model not in {"korean_PP-OCRv5_mobile_rec", "en_PP-OCRv5_mobile_rec"}:
            raise ExtractorError("config_mismatch", "Profile selected an unapproved recognition model")
        if recognition_model not in model_dirs:
            raise ExtractorError("model_manifest_mismatch", "Recognition model directory is not in the manifest")
        kwargs = {
            "use_doc_orientation_classify": bool(self.config.get("use_doc_orientation_classify", True)),
            "use_doc_unwarping": bool(self.config.get("use_doc_unwarping", True)),
            "use_textline_orientation": bool(self.config.get("use_textline_orientation", True)),
            "text_recognition_model_name": recognition_model,
            "text_recognition_model_dir": model_dirs[recognition_model],
            "use_table_recognition": bool(self.config.get("enable_table_recognition", True)),
            "use_formula_recognition": bool(self.config.get("enable_formula_recognition", True)),
            "use_chart_recognition": bool(self.config.get("enable_chart_recognition", True)),
            "use_seal_recognition": bool(self.config.get("enable_seal_recognition", False)),
            "device": str(self.config.get("device", "cpu")),
        }
        for model_name, (name_argument, directory_argument) in self.model_options.items():
            try:
                kwargs[name_argument] = model_name
                kwargs[directory_argument] = model_dirs[model_name]
            except KeyError as exc:
                raise ExtractorError("model_manifest_mismatch", "PaddleOCR model directory is not in the manifest") from exc
        os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "true"
        os.environ.setdefault("FLAGS_allocator_strategy", "auto_growth")
        try:
            self._pipeline = PPStructureV3(**kwargs)
        except Exception as exc:
            raise ExtractorError("paddleocr_initialization_failed", "PPStructureV3 initialization failed") from exc
        return self._pipeline

    def extract(
        self,
        path: Path,
        page_indices: Iterable[int],
        *,
        input_kind: str = "pdf",
    ) -> ExtractionOutput:
        runtime = self._verify_runtime()
        indices = sorted(set(int(index) for index in page_indices))
        if not indices:
            raise ExtractorError("page_incomplete", "PaddleOCR received an empty page set")
        pipeline = self._build_pipeline()
        pages: list[PageExtraction] = []
        all_confidences: list[float] = []
        reasons: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="wisdome-ocr-") as temp_dir:
            inputs = self._prepare_inputs(path, indices, input_kind=input_kind, temp_dir=Path(temp_dir))
            for page_index, image_path, width, height, scale in inputs:
                try:
                    raw_results = list(pipeline.predict(input=str(image_path)))
                except Exception as exc:
                    message = str(exc).lower()
                    code = "out_of_memory" if "memory" in message else "paddleocr_inference_failed"
                    raise ExtractorError(code, "PPStructureV3 inference failed", retryable=code != "out_of_memory") from exc
                if not raw_results:
                    raise ExtractorError("page_incomplete", "PPStructureV3 returned no page result")
                blocks: list[ExtractionBlock] = []
                for raw_result in raw_results:
                    payload = self._result_payload(raw_result)
                    blocks.extend(self._normalize_blocks(payload, page_index, scale, len(blocks)))
                if not blocks:
                    raise ExtractorError("page_incomplete", "PPStructureV3 returned no normalized blocks")
                thresholds = self.config.get("confidence_thresholds", {})
                for block in blocks:
                    if block.confidence is not None:
                        all_confidences.append(block.confidence)
                        threshold = float(thresholds.get(block.block_type, thresholds.get("default", 0.75)))
                        if block.confidence < threshold:
                            reasons.append({
                                "impact": "high" if block.block_type in {"table", "formula"} else "standard",
                                "scope": block.block_type if block.block_type in {"table", "chart", "formula"} else "document_block",
                                "fieldOrBlockRef": block.block_id,
                                "code": "block_confidence_below_threshold",
                                "observedConfidence": round(block.confidence, 6),
                                "threshold": threshold,
                                "calibrationProfileHash": None,
                                "message": "Recognized block did not meet its approved confidence threshold",
                            })
                pages.append(PageExtraction(
                    page_index=page_index,
                    width=width,
                    height=height,
                    rotation=0,
                    blocks=blocks,
                    preprocessing_hash=sha256_file(image_path),
                ))
        summary = {
            "minimum": min(all_confidences) if all_confidences else None,
            "mean": sum(all_confidences) / len(all_confidences) if all_confidences else None,
            "samples": len(all_confidences),
        }
        return ExtractionOutput(
            engine=self.engine,
            processed_page_indices=[page.page_index for page in pages],
            pages=pages,
            runtime_version=runtime,
            package_version=self.package_version,
            pipeline_name=self.pipeline_name,
            device_type=str(self.config.get("device", "cpu")),
            confidence_summary=summary,
            low_confidence_reasons=reasons,
        )

    def _prepare_inputs(
        self,
        path: Path,
        indices: Sequence[int],
        *,
        input_kind: str,
        temp_dir: Path,
    ) -> list[tuple[int, Path, float, float, float]]:
        if input_kind == "standalone_image":
            if indices != [0] or sniff_mime(path) not in {"image/png", "image/jpeg", "image/tiff"}:
                raise ExtractorError("unsupported_image", "Standalone image input is invalid")
            try:
                from PIL import Image
                with Image.open(path) as image:
                    if getattr(image, "n_frames", 1) != 1:
                        raise ExtractorError("unsupported_multiframe_image", "Animated/multi-frame images are rejected")
                    return [(0, path, float(image.width), float(image.height), 1.0)]
            except ExtractorError:
                raise
            except Exception as exc:
                raise ExtractorError("unsupported_image", "Image decoder rejected the input") from exc
        try:
            import fitz
        except ImportError as exc:
            raise ExtractorError("native_pdf_dependency_missing", "PyMuPDF is required to render OCR pages") from exc
        document = fitz.open(path)
        outputs = []
        dpi = int(self.config.get("render_dpi", 200))
        scale = dpi / 72.0
        try:
            if document.needs_pass:
                raise ExtractorError("encrypted_pdf", "Encrypted PDFs are not processed")
            for index in indices:
                if index < 0 or index >= document.page_count:
                    raise ExtractorError("page_incomplete", "Requested OCR page is outside the PDF")
                page = document.load_page(index)
                image_path = temp_dir / f"page-{index}.png"
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                pixmap.save(image_path)
                outputs.append((index, image_path, float(page.rect.width), float(page.rect.height), scale))
        finally:
            document.close()
        return outputs

    @staticmethod
    def _result_payload(result: Any) -> Mapping[str, Any]:
        if isinstance(result, Mapping):
            return result
        payload = getattr(result, "json", None)
        if callable(payload):
            payload = payload()
        if isinstance(payload, Mapping):
            if isinstance(payload.get("res"), Mapping):
                return payload["res"]
            return payload
        data = getattr(result, "res", None)
        if isinstance(data, Mapping):
            return data
        raise ExtractorError("paddleocr_result_invalid", "PPStructureV3 returned an unknown result shape")

    @classmethod
    def _normalize_blocks(
        cls, payload: Mapping[str, Any], page_index: int, scale: float, offset: int
    ) -> list[ExtractionBlock]:
        candidates = payload.get("parsing_res_list") or payload.get("layout_parsing_result") or payload.get("blocks")
        if isinstance(candidates, Mapping):
            candidates = candidates.get("blocks") or candidates.get("parsing_res_list")
        if not isinstance(candidates, list):
            candidates = []
            texts = payload.get("rec_texts") or []
            scores = payload.get("rec_scores") or []
            boxes = payload.get("rec_boxes") or []
            for index, text in enumerate(texts):
                candidates.append({
                    "block_label": "text",
                    "block_content": text,
                    "block_bbox": boxes[index] if index < len(boxes) else None,
                    "confidence": scores[index] if index < len(scores) else None,
                })
        blocks: list[ExtractionBlock] = []
        for local_index, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping):
                continue
            block_type = cls._block_type(candidate.get("block_label") or candidate.get("label") or candidate.get("type"))
            text = candidate.get("block_content") or candidate.get("text") or candidate.get("content")
            confidence = candidate.get("confidence") or candidate.get("score") or candidate.get("rec_score")
            bbox = candidate.get("block_bbox") or candidate.get("bbox") or candidate.get("box")
            polygon = candidate.get("polygon") or candidate.get("poly")
            scaled_bbox = cls._scale_bbox(bbox, scale)
            scaled_polygon = cls._scale_polygon(polygon, scale)
            if scaled_bbox is None and scaled_polygon is not None:
                scaled_bbox = cls._bbox_from_polygon(scaled_polygon)
            if scaled_polygon is None and scaled_bbox is not None:
                scaled_polygon = cls._polygon_from_bbox(scaled_bbox)
            if scaled_bbox is None or scaled_polygon is None:
                raise ExtractorError(
                    "locator_missing",
                    f"PPStructureV3 block {offset + local_index} has no usable page region",
                )
            structured = {
                key: candidate[key] for key in ("table", "formula", "chart", "html", "cells") if key in candidate
            } or None
            blocks.append(ExtractionBlock(
                block_id=f"p{page_index}-ppstructure-{offset + local_index}",
                block_type=block_type,
                reading_order=offset + local_index,
                text=str(text).strip() if text is not None else None,
                confidence=float(confidence) if confidence is not None else 0.0,
                bbox=scaled_bbox,
                polygon=scaled_polygon,
                structured_data=structured,
            ))
        return blocks

    @staticmethod
    def _block_type(value: Any) -> str:
        label = str(value or "other").lower()
        aliases = {
            "heading": "title", "paragraph": "text", "doc_title": "title",
            "table_caption": "text", "figure": "image", "figure_caption": "text",
            "display_formula": "formula", "inline_formula": "formula",
        }
        label = aliases.get(label, label)
        return label if label in {"title", "text", "list", "table", "chart", "formula", "image", "header", "footer"} else "other"

    @staticmethod
    def _scale_bbox(value: Any, scale: float) -> list[float] | None:
        if not isinstance(value, (list, tuple)) or len(value) < 4:
            return None
        try:
            result = [round(float(item) / scale, 4) for item in value[:4]]
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        if not all(math.isfinite(item) for item in result):
            return None
        if result[2] <= result[0] or result[3] <= result[1]:
            return None
        return result

    @staticmethod
    def _scale_polygon(value: Any, scale: float) -> list[list[float]] | None:
        if not isinstance(value, (list, tuple)) or len(value) < 4:
            return None
        polygon = []
        try:
            for point in value:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    return None
                polygon.append([round(float(point[0]) / scale, 4), round(float(point[1]) / scale, 4)])
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        if not all(math.isfinite(coordinate) for point in polygon for coordinate in point):
            return None
        if len({(point[0], point[1]) for point in polygon}) < 3:
            return None
        return polygon

    @staticmethod
    def _bbox_from_polygon(polygon: Sequence[Sequence[float]]) -> list[float] | None:
        xs = [float(point[0]) for point in polygon]
        ys = [float(point[1]) for point in polygon]
        bbox = [min(xs), min(ys), max(xs), max(ys)]
        return bbox if bbox[2] > bbox[0] and bbox[3] > bbox[1] else None

    @staticmethod
    def _polygon_from_bbox(bbox: Sequence[float]) -> list[list[float]]:
        x0, y0, x1, y1 = (float(item) for item in bbox)
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
