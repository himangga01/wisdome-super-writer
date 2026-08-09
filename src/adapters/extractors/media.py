from __future__ import annotations

import importlib.metadata
import time
import warnings
from pathlib import Path
from typing import Any, Mapping

from adapters.extractors.base import ExtractorError, GenericEvidenceRecord, GenericExtractionOutput, sniff_mime


MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_DECOMPRESSED_BYTES = 160 * 1024 * 1024
MAX_IMAGE_FRAMES = 1
MAX_IMAGE_DECODE_SECONDS = 30.0


def inspect_static_image(path: Path, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = dict(config or {})
    max_width = min(int(config.get("max_image_width", config.get("max_width", MAX_IMAGE_DIMENSION))), MAX_IMAGE_DIMENSION)
    max_height = min(int(config.get("max_image_height", config.get("max_height", MAX_IMAGE_DIMENSION))), MAX_IMAGE_DIMENSION)
    max_pixels = min(int(config.get("max_image_pixels", config.get("max_pixels", MAX_IMAGE_PIXELS))), MAX_IMAGE_PIXELS)
    max_decompressed = min(
        int(config.get("max_image_decompressed_bytes", config.get("max_decompressed_bytes", MAX_IMAGE_DECOMPRESSED_BYTES))),
        MAX_IMAGE_DECOMPRESSED_BYTES,
    )
    max_frames = min(int(config.get("max_image_frames", config.get("max_frames", 1))), MAX_IMAGE_FRAMES)
    max_seconds = min(float(config.get("max_image_decode_seconds", MAX_IMAGE_DECODE_SECONDS)), MAX_IMAGE_DECODE_SECONDS)
    started_at = time.monotonic()
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                frame_count = int(getattr(image, "n_frames", 1))
                width = int(image.width)
                height = int(image.height)
                if frame_count < 1 or frame_count > max_frames:
                    raise ExtractorError(
                        "unsupported_multiframe_image", "Animated/multi-frame images are rejected"
                    )
                if width < 1 or height < 1 or width > max_width or height > max_height:
                    raise ExtractorError("image_dimension_exceeded", "Image dimensions exceed the limit")
                pixels = width * height
                if pixels > max_pixels:
                    raise ExtractorError("image_pixel_limit_exceeded", "Image pixel count exceeds the limit")
                bands = max(1, len(image.getbands()))
                decompressed_bytes = pixels * bands
                if decompressed_bytes > max_decompressed:
                    raise ExtractorError(
                        "image_decompressed_limit_exceeded",
                        "Image decompressed bytes exceed the limit",
                    )
                image.load()
                if time.monotonic() - started_at > max_seconds:
                    raise ExtractorError("image_decode_timeout", "Image decoding exceeded the deadline")
                return {
                    "format": image.format,
                    "width": width,
                    "height": height,
                    "mode": image.mode,
                    "frame_count": frame_count,
                    "pixels": pixels,
                    "decompressed_bytes": decompressed_bytes,
                    "exif_present": bool(image.getexif()),
                }
    except ExtractorError:
        raise
    except Exception as exc:
        raise ExtractorError("media_decode_failed", "Image decoder rejected the input") from exc


class MediaMetadataExtractor:
    engine = "media_parser"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})

    def extract(self, path: Path) -> GenericExtractionOutput:
        mime_type = sniff_mime(path)
        if not mime_type.startswith("image/"):
            raise ExtractorError("media_format_unsupported", "MVP media metadata parser supports static images")
        metadata = inspect_static_image(path, self.config)
        return GenericExtractionOutput(
            engine=self.engine,
            extractor_version=importlib.metadata.version("Pillow"),
            validation_mode="deterministic",
            records=[GenericEvidenceRecord(
                kind="image",
                locator_type="image_region",
                locator={
                    "locator_type": "image_region", "bbox": [0, 0, metadata["width"], metadata["height"]],
                    "polygon": None,
                },
                structured_data=metadata,
                mime_type=mime_type,
                alt_text="수집된 이미지 자료",
            )],
            metadata={"embedded_metadata_only": True},
        )

