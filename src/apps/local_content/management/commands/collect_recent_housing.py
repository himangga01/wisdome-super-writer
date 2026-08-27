"""Collect recent official housing notices and write local article bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.local_content.dates import SEOUL
from apps.local_content.http import HtmlResponse, OfficialHtmlFetcher
from apps.local_content.humanizer import HumanizerClient
from apps.local_content.sources.applyhome import (
    APT_LIST,
    REMAINING_LIST,
    ApplyHomePublicCollector,
)
from apps.local_content.sources.lh import LH_LIST, LhPublicCollector
from apps.local_content.workflow import LocalHousingWorkflow, WorkflowError

_REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
_APPROVED_FIXTURE_ROOT = _REPOSITORY_ROOT / "tests" / "fixtures" / "local-content"
_FIXTURE_FILES = (
    "applyhome/apt-list.html",
    "applyhome/apt-list-page-2.html",
    "applyhome/remaining-list.html",
    "applyhome/apt-detail.html",
    "lh/notice-list-page-1.html",
    "lh/notice-list-page-2.html",
    "lh/notice-detail.html",
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class Command(BaseCommand):
    help = "Collect recent ApplyHome/LH housing notices into local immutable bundles."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--days", type=int, default=7, choices=range(1, 32))
        parser.add_argument(
            "--humanize",
            action=argparse.BooleanOptionalAction,
            default=True,
        )
        parser.add_argument(
            "--write-articles",
            action=argparse.BooleanOptionalAction,
            default=True,
        )
        parser.add_argument("--selected-id", action="append", default=[])
        parser.add_argument("--fixture-root")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        del args
        if not settings.IS_LOCAL_RUNTIME:
            raise CommandError("collect_recent_housing requires the local runtime")
        try:
            fixture_root = options.get("fixture_root")
            workflow = _build_workflow(
                fixture_root=Path(fixture_root) if fixture_root else None,
                dry_run=bool(options["dry_run"]),
            )
            report = workflow.run(
                now=datetime.now(SEOUL),
                days=options["days"],
                humanize=options["humanize"],
                write_articles=options["write_articles"],
                selected_ids=tuple(options["selected_id"]),
            )
        except WorkflowError as exc:
            raise CommandError(f"local housing workflow failed: {exc.code}") from None
        metadata = {
            "workflow_id": report.workflow_id,
            "mode": report.mode,
            "complete": report.complete,
            "blocked": report.blocked,
            "live_success": report.live_success,
            "article_count": report.article_count,
            "written_count": report.written_count,
            "run_path": report.run_path,
            "report_path": report.report_path,
            "error_codes": list(report.error_codes),
        }
        self.stdout.write(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
        if not report.complete or report.blocked:
            codes = ",".join(report.error_codes) or "WORKFLOW_INCOMPLETE"
            raise CommandError(f"local housing workflow incomplete: {codes}")
        return None


def _build_workflow(*, fixture_root: Path | None, dry_run: bool) -> LocalHousingWorkflow:
    if fixture_root is not None:
        collectors = _fixture_collectors(fixture_root)
        humanizer = _FixtureHumanizer()
        mode = "fixture"
    else:
        applyhome_fetcher = OfficialHtmlFetcher(
            allowed_hosts={"www.applyhome.co.kr"},
            path_prefixes=("/ai/aia/",),
        )
        lh_fetcher = OfficialHtmlFetcher(
            allowed_hosts={"apply.lh.or.kr"},
            path_prefixes=("/lhapply/apply/",),
        )
        collectors = (
            ApplyHomePublicCollector(applyhome_fetcher),
            LhPublicCollector(lh_fetcher),
        )
        humanizer = HumanizerClient(settings.HUMANIZER_BASE_URL)
        mode = "live"
    return LocalHousingWorkflow(
        collectors=collectors,
        humanizer=humanizer,
        output_root=settings.LOCAL_ARTICLE_ROOT,
        state_root=settings.LOCAL_STATE_ROOT,
        mode=mode,
        dry_run=dry_run,
    )


def _fixture_collectors(root: Path):
    try:
        resolved = Path(root).resolve(strict=True)
        approved = _APPROVED_FIXTURE_ROOT.resolve(strict=True)
    except OSError:
        raise WorkflowError("FIXTURE_ROOT_UNAPPROVED") from None
    if resolved != approved:
        raise WorkflowError("FIXTURE_ROOT_UNAPPROVED")
    _verify_fixture_manifest(resolved, approved_root=approved)
    fixture = _FixtureFetcher(resolved)
    return (ApplyHomePublicCollector(fixture), LhPublicCollector(fixture))


def _verify_fixture_manifest(root: Path, *, approved_root: Path) -> None:
    root = Path(root)
    approved_root = Path(approved_root)
    if root.resolve(strict=True) != approved_root.resolve(strict=True):
        raise WorkflowError("FIXTURE_ROOT_UNAPPROVED")
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_fixture_pairs,
        )
    except (OSError, UnicodeError, ValueError):
        raise WorkflowError("FIXTURE_MANIFEST_INVALID") from None
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "files"}
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("files"), dict)
        or tuple(manifest["files"]) != _FIXTURE_FILES
    ):
        raise WorkflowError("FIXTURE_MANIFEST_INVALID")
    fixture_parent = root.parent.resolve(strict=True)
    for relative in _FIXTURE_FILES:
        expected = manifest["files"].get(relative)
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise WorkflowError("FIXTURE_MANIFEST_INVALID")
        path = root.parent / relative
        try:
            resolved = path.resolve(strict=True)
            metadata = path.lstat()
            confined = resolved.is_relative_to(fixture_parent)
            unsafe = (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or bool(
                    getattr(metadata, "st_file_attributes", 0)
                    & _FILE_ATTRIBUTE_REPARSE_POINT
                )
            )
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            raise WorkflowError("FIXTURE_MANIFEST_INVALID") from None
        if not confined or unsafe:
            raise WorkflowError("FIXTURE_MANIFEST_INVALID")
        if observed != expected:
            raise WorkflowError("FIXTURE_CHECKSUM_MISMATCH")


def _strict_fixture_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate fixture manifest key")
        result[key] = value
    return result


class _FixtureHumanizer:
    def transform(self, document: str) -> str:
        return document


class _FixtureFetcher:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        if not self.root.is_dir() or self.root.name != "local-content":
            raise WorkflowError("FIXTURE_ROOT_UNAPPROVED")
        fixture_root = self.root.parent
        self.applyhome = fixture_root / "applyhome"
        self.lh = fixture_root / "lh"
        required = (
            self.applyhome / "apt-list.html",
            self.applyhome / "apt-list-page-2.html",
            self.applyhome / "remaining-list.html",
            self.applyhome / "apt-detail.html",
            self.lh / "notice-list-page-1.html",
            self.lh / "notice-list-page-2.html",
            self.lh / "notice-detail.html",
        )
        if any(not path.is_file() for path in required):
            raise WorkflowError("FIXTURE_MANIFEST_INVALID")

    def get(self, url: str) -> HtmlResponse:
        split = urlsplit(url)
        query = parse_qs(split.query)
        if url.startswith(APT_LIST):
            page = query.get("pageIndex", ["1"])[0]
            path = self.applyhome / (
                "apt-list.html" if page == "1" else "apt-list-page-2.html"
            )
        elif url.startswith(REMAINING_LIST):
            path = self.applyhome / "remaining-list.html"
        elif split.hostname == "www.applyhome.co.kr":
            path = (
                self.applyhome / "apt-detail.html"
                if query.get("houseManageNo") == ["2026000001"]
                else None
            )
        elif split.hostname == "apply.lh.or.kr":
            path = (
                self.lh / "notice-detail.html"
                if query.get("panId") == ["0000061158"]
                else None
            )
        else:
            raise KeyError(url)
        body = (
            path.read_text(encoding="utf-8")
            if path is not None
            else '<main data-notice-detail="apt" data-lh-notice-detail="true"></main>'
        )
        return _fixture_response(url, body)

    def post(self, url: str, *, data: Mapping[str, str]) -> HtmlResponse:
        if url != LH_LIST:
            raise KeyError(url)
        page = data.get("currPage")
        if page not in {"1", "2"}:
            raise KeyError(page)
        path = self.lh / f"notice-list-page-{page}.html"
        body = path.read_text(encoding="utf-8")
        return _fixture_response(url, _assembled_lh_page(body, page))


def _fixture_response(url: str, body: str) -> HtmlResponse:
    return HtmlResponse(
        url=url,
        status_code=200,
        content_type="text/html",
        body=body,
        fetched_at=datetime(2026, 8, 28, 12, 0, tzinfo=SEOUL),
    )


def _assembled_lh_page(body: str, page: str) -> str:
    body = body.replace('data-total-count="2"', 'data-total-count="51"', 1)
    if page != "1":
        return body
    row = re.search(r"<article[\s\S]*?</article>", body)
    if row is None:
        return body
    copies = [
        row.group().replace("0000061158", f"{61158 + offset:010d}", 1)
        for offset in range(1, 50)
    ]
    return body.replace("</main>", f"{''.join(copies)}</main>", 1)
