from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Mapping

from adapters.extractors.base import ExtractorError, GenericEvidenceRecord, GenericExtractionOutput


class BrowserCaptureExtractor:
    """Captures only a previously downloaded local HTML file; it never navigates to a remote URL."""

    engine = "browser_capture"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})

    def extract(self, path: Path) -> GenericExtractionOutput:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ExtractorError("browser_capture_dependency_missing", "Playwright is not installed") from exc
        with tempfile.TemporaryDirectory(prefix="wisdome-capture-") as temp_dir:
            output = Path(temp_dir) / "capture.png"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(args=["--disable-network", "--no-sandbox"])
                context = browser.new_context(
                    viewport={"width": int(self.config.get("viewport_width", 1440)),
                              "height": int(self.config.get("viewport_height", 1200))},
                    service_workers="block",
                )
                page = context.new_page()
                page.route("**/*", lambda route: route.abort() if not route.request.url.startswith("file:") else route.continue_())
                page.goto(path.resolve().as_uri(), wait_until="domcontentloaded")
                page.screenshot(path=str(output), full_page=True)
                title = page.title()
                context.close()
                browser.close()
            durable = Path(str(path) + ".capture.png")
            output.replace(durable)
        return GenericExtractionOutput(
            engine=self.engine,
            extractor_version="playwright-v1",
            validation_mode="deterministic",
            records=[GenericEvidenceRecord(
                kind="screenshot",
                locator_type="image_region",
                locator={"locator_type": "image_region", "bbox": [0, 0, 1440, 1200], "polygon": None},
                object_path=str(durable),
                mime_type="image/png",
                alt_text=title or "원문 페이지 캡처",
            )],
            metadata={"network_requests_allowed": 0},
        )

