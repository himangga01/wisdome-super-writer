from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import Any, Mapping

from adapters.extractors.base import ExtractorError, GenericEvidenceRecord, GenericExtractionOutput, sniff_mime


class MediaMetadataExtractor:
    engine = "media_parser"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})

    def extract(self, path: Path) -> GenericExtractionOutput:
        mime_type = sniff_mime(path)
        if not mime_type.startswith("image/"):
            raise ExtractorError("media_format_unsupported", "MVP media metadata parser supports static images")
        try:
            from PIL import Image
            with Image.open(path) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise ExtractorError("unsupported_multiframe_image", "Animated/multi-frame media is rejected")
                metadata = {
                    "format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "mode": image.mode,
                    "exif_present": bool(image.getexif()),
                }
        except ExtractorError:
            raise
        except Exception as exc:
            raise ExtractorError("media_decode_failed", "Media decoder rejected the input") from exc
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

