from __future__ import annotations

import hashlib
import json
import mimetypes
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence


class ExtractorError(RuntimeError):
    def __init__(self, code: str, detail_redacted: str, *, retryable: bool = False) -> None:
        super().__init__(detail_redacted)
        self.code = code
        self.detail_redacted = detail_redacted[:1000]
        self.retryable = retryable


def normalize_text(text: str | None) -> str | None:
    if text is None:
        return None
    return unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")


def canonical_bytes(value: Any) -> bytes:
    try:
        import rfc8785  # type: ignore

        return rfc8785.dumps(value)
    except ImportError:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sniff_mime(path: Path) -> str:
    head = path.read_bytes()[:16]
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        if path.suffix.lower() == ".hwpx":
            return "application/vnd.hancom.hwpx"
        if path.suffix.lower() in {".xlsx", ".xlsm"}:
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return "application/zip"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


@dataclass(frozen=True)
class ExtractionBlock:
    block_id: str
    block_type: str
    reading_order: int
    text: str | None = None
    confidence: float | None = None
    polygon: list[list[float]] | None = None
    bbox: list[float] | None = None
    structured_data: Mapping[str, Any] | None = None
    crop_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["text"] = normalize_text(value["text"])
        return value


@dataclass(frozen=True)
class PageExtraction:
    page_index: int
    width: float
    height: float
    rotation: int
    blocks: Sequence[ExtractionBlock] = field(default_factory=tuple)
    preprocessing_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "preprocessing_hash": self.preprocessing_hash,
            "blocks": [block.as_dict() for block in self.blocks],
        }


@dataclass(frozen=True)
class ExtractionOutput:
    engine: str
    processed_page_indices: Sequence[int]
    pages: Sequence[PageExtraction]
    runtime_version: str
    package_version: str
    pipeline_name: str | None = None
    device_type: str | None = None
    confidence_summary: Mapping[str, Any] = field(default_factory=dict)
    low_confidence_reasons: Sequence[Mapping[str, Any]] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "v1",
            "engine": self.engine,
            "processed_page_indices": list(self.processed_page_indices),
            "pages": [page.as_dict() for page in self.pages],
            "runtime_version": self.runtime_version,
            "package_version": self.package_version,
            "pipeline_name": self.pipeline_name,
            "device_type": self.device_type,
            "confidence_summary": dict(self.confidence_summary),
            "low_confidence_reasons": list(self.low_confidence_reasons),
        }

    @property
    def checksum(self) -> str:
        return sha256_bytes(canonical_bytes(self.as_dict()))


@dataclass(frozen=True)
class GenericEvidenceRecord:
    kind: str
    locator_type: str
    locator: Mapping[str, Any]
    text: str | None = None
    structured_data: Mapping[str, Any] | Sequence[Any] | None = None
    object_path: str | None = None
    mime_type: str | None = None
    confidence: float | None = None
    alt_text: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["text"] = normalize_text(value["text"])
        return value


@dataclass(frozen=True)
class GenericExtractionOutput:
    engine: str
    extractor_version: str
    validation_mode: str
    records: Sequence[GenericEvidenceRecord]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    low_confidence_reasons: Sequence[Mapping[str, Any]] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "v1",
            "engine": self.engine,
            "extractor_version": self.extractor_version,
            "validation_mode": self.validation_mode,
            "records": [record.as_dict() for record in self.records],
            "metadata": dict(self.metadata),
            "low_confidence_reasons": list(self.low_confidence_reasons),
        }

    @property
    def checksum(self) -> str:
        return sha256_bytes(canonical_bytes(self.as_dict()))


class DocumentExtractor(Protocol):
    def extract(self, path: Path, page_indices: Iterable[int]) -> ExtractionOutput: ...


class GenericExtractor(Protocol):
    def extract(self, path: Path) -> GenericExtractionOutput: ...

