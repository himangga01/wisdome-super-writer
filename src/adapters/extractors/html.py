from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from adapters.extractors.base import ExtractorError, GenericEvidenceRecord, GenericExtractionOutput


class HtmlExtractor:
    engine = "html_parser"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.max_bytes = int(self.config.get("max_file_bytes", 20 * 1024 * 1024))
        self.max_records = int(self.config.get("max_records", 5000))

    def extract(self, path: Path) -> GenericExtractionOutput:
        if path.stat().st_size > self.max_bytes:
            raise ExtractorError("html_limit_exceeded", "HTML exceeds the configured byte limit")
        raw = path.read_bytes()
        try:
            from selectolax.parser import HTMLParser
        except ImportError as exc:
            raise ExtractorError("html_dependency_missing", "selectolax is not installed") from exc
        tree = HTMLParser(raw)
        for selector in ("script", "style", "template", "noscript", "iframe", "object", "embed"):
            for node in tree.css(selector):
                node.decompose()
        records: list[GenericEvidenceRecord] = []
        nodes = tree.css("h1,h2,h3,h4,h5,h6,p,li,blockquote,table,figure,img")
        for node in nodes[: self.max_records]:
            tag = node.tag.lower()
            selector = self._stable_selector(node)
            if tag == "table":
                rows = []
                for row in node.css("tr"):
                    rows.append([cell.text(strip=True) for cell in row.css("th,td")])
                if rows:
                    records.append(GenericEvidenceRecord(
                        kind="table",
                        locator_type="html_dom",
                        locator={"locator_type": "html_dom", "css_selector": selector, "xpath": None},
                        text="\n".join(" | ".join(row) for row in rows),
                        structured_data={"rows": rows},
                    ))
            elif tag == "img":
                src = node.attributes.get("src")
                alt = node.attributes.get("alt") or None
                if src and not src.lower().startswith(("javascript:", "data:text/html")):
                    records.append(GenericEvidenceRecord(
                        kind="image",
                        locator_type="html_dom",
                        locator={"locator_type": "html_dom", "css_selector": selector, "xpath": None},
                        structured_data={"source_ref": src, "external_fetch_performed": False},
                        alt_text=alt,
                    ))
            else:
                text = node.text(separator=" ", strip=True)
                if text:
                    records.append(GenericEvidenceRecord(
                        kind="text",
                        locator_type="html_dom",
                        locator={"locator_type": "html_dom", "css_selector": selector, "xpath": None},
                        text=text,
                        structured_data={"html_tag": tag},
                    ))
        return GenericExtractionOutput(
            engine=self.engine,
            extractor_version="1.0.0",
            validation_mode="deterministic",
            records=records,
            metadata={"record_count": len(records), "network_fetches": 0},
        )

    @staticmethod
    def _stable_selector(node) -> str:
        parts = []
        current = node
        while current is not None and not current.tag.startswith("-"):
            position = 1
            sibling = current.prev
            while sibling is not None:
                if sibling.tag == current.tag:
                    position += 1
                sibling = sibling.prev
            parts.append(f"{current.tag}:nth-of-type({position})")
            current = current.parent
        return " > ".join(reversed(parts))

