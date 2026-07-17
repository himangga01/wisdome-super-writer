from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from adapters.extractors.base import (
    ExtractionBlock,
    ExtractionOutput,
    ExtractorError,
    PageExtraction,
    sha256_file,
)


@dataclass(frozen=True)
class PdfPageSignal:
    page_index: int
    text_chars: int
    replacement_ratio: float
    complex_layout: bool
    image_count: int
    block_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "page_index": self.page_index,
            "text_chars": self.text_chars,
            "replacement_ratio": self.replacement_ratio,
            "complex_layout": self.complex_layout,
            "image_count": self.image_count,
            "block_count": self.block_count,
        }


@dataclass(frozen=True)
class PdfInspection:
    page_count: int
    encrypted: bool
    checksum: str
    page_signals: Sequence[PdfPageSignal]


class NativePdfExtractor:
    engine = "native_pdf"

    def __init__(self, config: Mapping[str, object] | None = None) -> None:
        self.config = dict(config or {})
        self.max_file_bytes = int(self.config.get("max_file_bytes", 200 * 1024 * 1024))
        self.max_pages = int(self.config.get("max_pages", 1000))
        self.minimum_text_chars = int(self.config.get("minimum_text_chars", 40))
        self.maximum_replacement_ratio = float(self.config.get("maximum_replacement_ratio", 0.02))

    @staticmethod
    def _fitz():
        try:
            import fitz

            return fitz
        except ImportError as exc:
            raise ExtractorError("native_pdf_dependency_missing", "PyMuPDF is not installed") from exc

    def inspect(self, path: Path) -> PdfInspection:
        if path.stat().st_size > self.max_file_bytes:
            raise ExtractorError("unsafe_pdf", "PDF exceeds the configured byte limit")
        if not path.read_bytes()[:5] == b"%PDF-":
            raise ExtractorError("unsafe_pdf", "Input does not have a PDF signature")
        fitz = self._fitz()
        try:
            document = fitz.open(path)
        except Exception as exc:
            raise ExtractorError("corrupt_pdf", "PDF parser rejected the input") from exc
        try:
            if document.needs_pass:
                raise ExtractorError("encrypted_pdf", "Encrypted PDFs are not processed")
            if document.page_count < 1 or document.page_count > self.max_pages:
                raise ExtractorError("page_limit_exceeded", "PDF page count is outside the configured limit")
            signals: list[PdfPageSignal] = []
            for index, page in enumerate(document):
                text = page.get_text("text", sort=True) or ""
                text_chars = len("".join(text.split()))
                replacement_ratio = text.count("\ufffd") / max(1, len(text))
                blocks = page.get_text("blocks", sort=True) or []
                images = page.get_images(full=True) or []
                columns = self._estimate_columns(blocks)
                table_like = self._has_table_drawings(page)
                signals.append(PdfPageSignal(
                    page_index=index,
                    text_chars=text_chars,
                    replacement_ratio=replacement_ratio,
                    complex_layout=columns > 1 or table_like,
                    image_count=len(images),
                    block_count=len(blocks),
                ))
            return PdfInspection(document.page_count, False, sha256_file(path), signals)
        finally:
            document.close()

    @staticmethod
    def _estimate_columns(blocks: Sequence[Sequence[object]]) -> int:
        starts = sorted({round(float(block[0]) / 40) for block in blocks if len(block) >= 5 and str(block[4]).strip()})
        if len(starts) < 2:
            return 1
        separated = sum(1 for left, right in zip(starts, starts[1:]) if right - left >= 4)
        return min(3, 1 + separated)

    @staticmethod
    def _has_table_drawings(page) -> bool:
        try:
            drawings = page.get_drawings()
        except Exception:
            return False
        horizontal = 0
        vertical = 0
        for drawing in drawings[:1000]:
            rect = drawing.get("rect")
            if not rect:
                continue
            if rect.width > 30 and rect.height < 3:
                horizontal += 1
            if rect.height > 30 and rect.width < 3:
                vertical += 1
        return horizontal >= 3 and vertical >= 3

    def extract(self, path: Path, page_indices: Iterable[int]) -> ExtractionOutput:
        fitz = self._fitz()
        indices = sorted(set(int(index) for index in page_indices))
        if not indices:
            raise ExtractorError("page_incomplete", "Native extraction received an empty page set")
        document = fitz.open(path)
        pages: list[PageExtraction] = []
        try:
            if document.needs_pass:
                raise ExtractorError("encrypted_pdf", "Encrypted PDFs are not processed")
            if indices[0] < 0 or indices[-1] >= document.page_count:
                raise ExtractorError("page_incomplete", "Requested page is outside the PDF")
            for index in indices:
                page = document.load_page(index)
                blocks: list[ExtractionBlock] = []
                for order, raw in enumerate(page.get_text("blocks", sort=True) or []):
                    x0, y0, x1, y1, text = raw[:5]
                    text = str(text).strip()
                    if not text:
                        continue
                    block_type = "title" if order == 0 and len(text) < 180 else "text"
                    blocks.append(ExtractionBlock(
                        block_id=f"p{index}-native-{order}",
                        block_type=block_type,
                        reading_order=order,
                        text=text,
                        confidence=1.0,
                        bbox=[float(x0), float(y0), float(x1), float(y1)],
                    ))
                pages.append(PageExtraction(
                    page_index=index,
                    width=float(page.rect.width),
                    height=float(page.rect.height),
                    rotation=int(page.rotation),
                    blocks=blocks,
                ))
        finally:
            document.close()
        try:
            version = importlib.metadata.version("PyMuPDF")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        return ExtractionOutput(
            engine=self.engine,
            processed_page_indices=indices,
            pages=pages,
            runtime_version=version,
            package_version=version,
            confidence_summary={"text": 1.0},
        )

