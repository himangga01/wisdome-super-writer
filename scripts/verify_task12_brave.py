"""Run all-page local preview acceptance with installed Brave via Playwright."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Page, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from apps.local_content.acceptance import preview_headers_match  # noqa: E402

DEFAULT_BRAVE = Path(
    r"C:\Users\c\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"
)
KOREAN = re.compile(r"[가-힣]")
ALLOWED_OFFICIAL_HOSTS = frozenset({"www.applyhome.co.kr", "apply.lh.or.kr"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requested-date", required=True)
    parser.add_argument("--expected-details", required=True, type=int)
    parser.add_argument("--brave", type=Path, default=DEFAULT_BRAVE)
    parser.add_argument("--django-pid", action="append", type=int, default=[])
    arguments = parser.parse_args(argv)
    run_root = arguments.run_root.resolve()
    evidence_root = arguments.evidence_root.resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    brave = arguments.brave.resolve()
    if not brave.is_file():
        raise FileNotFoundError("configured Brave executable is missing")
    before_pids = _brave_pids()
    console_errors: list[dict[str, str]] = []
    page_errors: list[str] = []
    failed_requests: list[dict[str, str | None]] = []
    bad_local_responses: list[dict[str, str | int]] = []
    local_response_count = 0
    owned_pids: set[int] = set()
    detail_records: list[dict[str, object]] = []
    screenshot_paths = {
        "requested_index": evidence_root / "brave-index-desktop.png",
        "run_index": evidence_root / "brave-live-index-desktop.png",
        "detail_desktop": evidence_root / "brave-detail-desktop.png",
        "detail_mobile": evidence_root / "brave-detail-mobile.png",
    }
    browser = None
    requested_record: dict[str, object] = {}
    run_record: dict[str, object] = {}
    mobile_record: dict[str, object] = {}
    traversal_statuses: list[int] = []
    status_record: dict[str, object] = {}
    asset_header_valid = False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(brave),
                headless=True,
            )
            owned_pids = _brave_pids() - before_pids
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="ko-KR",
            )
            page = context.new_page()

            def on_console(message) -> None:  # type: ignore[no-untyped-def]
                if message.type == "error":
                    console_errors.append(
                        {"type": message.type, "text": message.text[:500]}
                    )

            def on_response(response) -> None:  # type: ignore[no-untyped-def]
                nonlocal local_response_count
                if urlsplit(response.url).hostname != "127.0.0.1":
                    return
                local_response_count += 1
                if response.status >= 400:
                    bad_local_responses.append(
                        {"url": response.url, "status": response.status}
                    )

            page.on("console", on_console)
            page.on("pageerror", lambda error: page_errors.append(str(error)[:500]))
            page.on(
                "requestfailed",
                lambda request: failed_requests.append(
                    {"url": request.url, "failure": request.failure}
                ),
            )
            page.on("response", on_response)

            requested_url = (
                f"{arguments.base_url}/local-articles/{arguments.requested_date}/"
            )
            requested_record = _inspect_index(page, requested_url)
            page.screenshot(path=str(screenshot_paths["requested_index"]), full_page=True)

            run_url = f"{arguments.base_url}/local-articles/{run_root.name}/"
            run_record = _inspect_index(page, run_url)
            detail_links = sorted(
                {
                    link
                    for link in page.locator("a[href]").evaluate_all(
                        "els => els.map(el => el.getAttribute('href'))"
                    )
                    if isinstance(link, str)
                    and f"/local-articles/{run_root.name}/" in link
                    and "/assets/" not in link
                    and link != f"/local-articles/{run_root.name}/"
                }
            )
            page.screenshot(path=str(screenshot_paths["run_index"]), full_page=True)
            for index, link in enumerate(detail_links):
                record = _inspect_detail(page, urljoin(arguments.base_url, link))
                detail_records.append(record)
                if index == 0:
                    page.screenshot(
                        path=str(screenshot_paths["detail_desktop"]),
                        full_page=True,
                    )

            representative_url = str(detail_records[0]["url"]) if detail_records else ""
            page.set_viewport_size({"width": 390, "height": 844})
            if representative_url:
                mobile_record = _inspect_detail(page, representative_url)
                mobile_record["body_font_size"] = page.evaluate(
                    "getComputedStyle(document.body).fontSize"
                )
                page.screenshot(
                    path=str(screenshot_paths["detail_mobile"]),
                    full_page=True,
                )

            first_image = (
                str(detail_records[0]["images"][0]["src"])
                if detail_records
                else ""
            )
            if first_image:
                asset_response = context.request.get(first_image)
                asset_header_valid = asset_response.status == 200 and preview_headers_match(
                    dict(asset_response.headers),
                    cache_control="public, max-age=31536000, immutable",
                )
                parsed_asset = urlsplit(first_image)
                traversal_path = (
                    parsed_asset.path.rsplit("/", 1)[0] + "/%2e%2e%2fmanifest.json"
                )
                traversal_statuses.append(
                    context.request.get(f"{arguments.base_url}{traversal_path}").status
                )
            traversal_statuses.append(
                context.request.get(
                    f"{arguments.base_url}/local-articles/assets/{run_root.name}/"
                    f"%252e%252e/{'0' * 64}/manifest.json"
                ).status
            )
            status_response = context.request.get(
                f"{arguments.base_url}/api/v1/local-articles/status"
            )
            status_payload = status_response.json()
            status_record = {
                "status": status_response.status,
                "headers_exact": preview_headers_match(
                    dict(status_response.headers),
                    cache_control="no-store",
                ),
                "humanizer_ready": (
                    isinstance(status_payload, dict)
                    and status_payload.get("humanizer", {}).get("status") == "ready"
                ),
            }
            context.close()
            browser.close()
            browser = None
    finally:
        if browser is not None:
            browser.close()

    surviving = _wait_for_owned_brave_exit(owned_pids)
    screenshots = {
        name: {
            "path": path.as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for name, path in screenshot_paths.items()
        if path.is_file()
    }
    all_detail_pages_passed = (
        len(detail_records) == arguments.expected_details
        and all(record["passed"] is True for record in detail_records)
    )
    mobile_passed = bool(mobile_record) and mobile_record.get("passed") is True and float(
        str(mobile_record.get("body_font_size", "0px")).removesuffix("px")
    ) >= 16
    assertions = {
        "brave_executable_exact": brave == DEFAULT_BRAVE,
        "requested_index": requested_record.get("passed") is True,
        "run_index": run_record.get("passed") is True,
        "all_detail_pages": all_detail_pages_passed,
        "representative_mobile": mobile_passed,
        "asset_headers_exact": asset_header_valid,
        "no_console_or_page_errors": not console_errors and not page_errors,
        "no_failed_requests": not failed_requests,
        "no_bad_local_responses": not bad_local_responses,
        "traversal_rejected": len(traversal_statuses) == 2
        and all(status in {400, 404} for status in traversal_statuses),
        "status_ready_headers_exact": status_record.get("status") == 200
        and status_record.get("headers_exact") is True
        and status_record.get("humanizer_ready") is True,
        "owned_brave_processes_closed": bool(owned_pids) and not surviving,
    }
    evidence = {
        "schema_version": 2,
        "checked_at": datetime.now(UTC).isoformat(),
        "brave_executable": str(brave),
        "brave_owned_pids": sorted(owned_pids),
        "brave_surviving_owned_pids": sorted(surviving),
        "django_owned_pids": sorted(set(arguments.django_pid)),
        "requested_index": requested_record,
        "run_index": run_record,
        "detail_pages": detail_records,
        "mobile": mobile_record,
        "status_endpoint": status_record,
        "traversal_statuses": traversal_statuses,
        "diagnostics": {
            "console_errors": console_errors,
            "page_errors": page_errors,
            "failed_requests": failed_requests,
            "bad_local_responses": bad_local_responses,
            "local_response_count": local_response_count,
        },
        "screenshots": screenshots,
        "assertions": assertions,
        "passed": all(assertions.values()),
    }
    evidence_path = evidence_root / "brave-evidence.json"
    _write_json(evidence_path, evidence)
    _merge_report(
        arguments.report,
        {
            "passed": evidence["passed"],
            "evidence_path": evidence_path.as_posix(),
            "detail_page_count": len(detail_records),
            "mobile_representative": mobile_record.get("url"),
            "screenshots": screenshots,
            "brave": {
                "executable": str(brave),
                "owned_pids": sorted(owned_pids),
                "closed": not surviving,
            },
            "django_owned_pids": sorted(set(arguments.django_pid)),
        },
    )
    print(
        json.dumps(
            {
                "passed": evidence["passed"],
                "detail_page_count": len(detail_records),
                "assertions": assertions,
                "evidence": evidence_path.as_posix(),
                "brave_owned_pids": sorted(owned_pids),
                "brave_surviving_owned_pids": sorted(surviving),
            },
            sort_keys=True,
        )
    )
    return 0 if evidence["passed"] is True else 1


def _inspect_index(page: Page, url: str) -> dict[str, object]:
    response = page.goto(url, wait_until="networkidle")
    if response is None:
        return {"url": url, "passed": False, "error": "NO_RESPONSE"}
    heading = page.locator("h1").first.text_content() or ""
    overflow = page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )
    headers = dict(response.headers)
    headers_exact = preview_headers_match(headers, cache_control="no-store")
    return {
        "url": url,
        "status": response.status,
        "heading_has_korean": bool(KOREAN.search(heading)),
        "html_lang": page.locator("html").get_attribute("lang"),
        "horizontal_overflow": overflow,
        "security_headers": _security_headers(headers),
        "headers_exact": headers_exact,
        "passed": response.status == 200
        and bool(KOREAN.search(heading))
        and page.locator("html").get_attribute("lang") == "ko"
        and not overflow
        and headers_exact,
    }


def _inspect_detail(page: Page, url: str) -> dict[str, object]:
    response = page.goto(url, wait_until="networkidle")
    if response is None:
        return {"url": url, "passed": False, "error": "NO_RESPONSE"}
    heading = page.locator("h1").first.text_content() or ""
    overflow = page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )
    images = page.locator("img").evaluate_all(
        "els => els.map(el => ({src: el.currentSrc, alt: el.alt, complete: el.complete, "
        "width: el.naturalWidth, height: el.naturalHeight}))"
    )
    official_links = page.locator("a[href^='https://']").evaluate_all(
        "els => els.map(el => el.href)"
    )
    images_valid = len(images) == 3 and all(
        row["complete"]
        and row["width"] > 0
        and row["height"] > 0
        and row["alt"].strip()
        for row in images
    )
    official_valid = bool(official_links) and all(
        urlsplit(link).scheme == "https"
        and urlsplit(link).hostname in ALLOWED_OFFICIAL_HOSTS
        for link in official_links
    )
    headers = dict(response.headers)
    headers_exact = preview_headers_match(headers, cache_control="no-store")
    return {
        "url": url,
        "status": response.status,
        "heading_has_korean": bool(KOREAN.search(heading)),
        "horizontal_overflow": overflow,
        "images": images,
        "official_link_count": len(official_links),
        "security_headers": _security_headers(headers),
        "headers_exact": headers_exact,
        "passed": response.status == 200
        and bool(KOREAN.search(heading))
        and not overflow
        and images_valid
        and official_valid
        and headers_exact,
    }


def _security_headers(headers: dict[str, str]) -> dict[str, str | None]:
    normalized = {key.casefold(): value for key, value in headers.items()}
    return {
        name: normalized.get(name)
        for name in (
            "content-security-policy",
            "referrer-policy",
            "x-content-type-options",
            "x-frame-options",
            "cache-control",
        )
    }


def _brave_pids() -> set[int]:
    command = (
        "$p=Get-Process -Name brave -ErrorAction SilentlyContinue; "
        "if($p){@($p.Id)|ConvertTo-Json -Compress}else{'[]'}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(completed.stdout.strip() or "[]")
    if isinstance(payload, int):
        return {payload}
    return {int(value) for value in payload}


def _wait_for_owned_brave_exit(owned_pids: set[int]) -> set[int]:
    deadline = time.monotonic() + 10
    surviving = _brave_pids() & owned_pids
    while surviving and time.monotonic() < deadline:
        time.sleep(0.1)
        surviving = _brave_pids() & owned_pids
    return surviving


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _merge_report(path: Path, browser: dict[str, object]) -> None:
    target = Path(path)
    report = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("acceptance report must be a JSON object")
    section = report.setdefault("task12_acceptance", {})
    if not isinstance(section, dict):
        raise ValueError("task12 acceptance section must be a JSON object")
    section["browser"] = browser
    _write_json(target, report)


if __name__ == "__main__":
    raise SystemExit(main())
