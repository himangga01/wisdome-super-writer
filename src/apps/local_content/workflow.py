"""Synchronous, fail-closed local housing collection and article workflow."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Collection, Iterable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Protocol

from apps.local_content.bundles import ArticleBundle, ArticleBundleWriter, BundleFile
from apps.local_content.contracts import (
    CollectionWindow,
    HousingCollectionResult,
    HousingNotice,
    SourceRunReport,
    _require_timezone_aware,
)
from apps.local_content.dates import SEOUL
from apps.local_content.humanizer import protect_article_prose, verify_humanized_candidate
from apps.local_content.images import ImageSet, build_image_set
from apps.local_content.rendering import (
    RenderedArticle,
    notice_slug,
    render_detailed_article,
    render_weekly_index,
)
from apps.local_content.selection import is_residential, needs_detailed_article

PHASE_NAMES = (
    "collect",
    "merge",
    "select",
    "render",
    "images",
    "humanize",
    "verify",
    "write",
)
LOCK_FILENAME = ".collect-recent-housing.lock"
ACCEPTANCE_FILENAME = "acceptance-report.json"
_SHA256_CHECKSUM = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
_MARKDOWN_DETAIL_LINK = r"\[[^\]\r\n]+\]\(\./%s/article\.md\)"
_MAX_LOCK_BYTES = 4096
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class Collector(Protocol):
    def collect(self, window: CollectionWindow) -> SourceRunReport: ...


class Humanizer(Protocol):
    def transform(self, document: str) -> str: ...


class WorkflowLockedError(RuntimeError):
    """Another workflow owns the configured local writer lock."""


@dataclass(frozen=True)
class PhaseReport:
    name: str
    status: str = "pending"
    count: int = 0
    paths: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "count": self.count,
            "paths": list(self.paths),
            "error_codes": list(self.error_codes),
        }


@dataclass(frozen=True)
class SourceWorkflowReport:
    source_key: str
    status: str
    notice_count: int
    warning_count: int
    error_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "source_key": self.source_key,
            "status": self.status,
            "notice_count": self.notice_count,
            "warning_count": self.warning_count,
            "error_codes": list(self.error_codes),
        }


@dataclass(frozen=True)
class ArticleWorkflowReport:
    source_key: str
    external_id: str
    slug: str
    status: str
    humanization_status: str
    article_path: str | None = None
    draft_path: str | None = None
    error_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "source_key": self.source_key,
            "external_id": self.external_id,
            "slug": self.slug,
            "status": self.status,
            "humanization_status": self.humanization_status,
            "article_path": self.article_path,
            "draft_path": self.draft_path,
            "error_codes": list(self.error_codes),
        }


@dataclass(frozen=True)
class WorkflowReport:
    workflow_id: str
    started_at: datetime
    window: CollectionWindow
    mode: str
    complete: bool
    blocked: bool
    live_success: bool
    sources: tuple[SourceWorkflowReport, ...]
    phases: tuple[PhaseReport, ...]
    articles: tuple[ArticleWorkflowReport, ...]
    notice_count: int
    conflict_count: int
    excluded_count: int
    run_path: str | None
    report_path: str | None
    error_codes: tuple[str, ...] = ()

    @property
    def article_count(self) -> int:
        return len(self.articles)

    @property
    def written_count(self) -> int:
        return sum(article.status == "written" for article in self.articles)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "workflow_id": self.workflow_id,
            "started_at": self.started_at.isoformat(),
            "mode": self.mode,
            "complete": self.complete,
            "blocked": self.blocked,
            "live_success": self.live_success,
            "window": {
                "start": self.window.start.isoformat(),
                "end": self.window.end.isoformat(),
            },
            "counts": {
                "sources": len(self.sources),
                "notices": self.notice_count,
                "conflicts": self.conflict_count,
                "excluded": self.excluded_count,
                "selected_articles": self.article_count,
                "written_articles": self.written_count,
            },
            "run_path": self.run_path,
            "report_path": self.report_path,
            "error_codes": list(self.error_codes),
            "sources": [source.as_dict() for source in self.sources],
            "phases": [phase.as_dict() for phase in self.phases],
            "articles": [article.as_dict() for article in self.articles],
        }


@dataclass
class _ArticleWork:
    notice: HousingNotice
    slug: str
    status: str = "selected"
    humanization_status: str = "pending"
    errors: list[str] = field(default_factory=list)
    rendered: RenderedArticle | None = None
    images: ImageSet | None = None
    protected: object | None = None
    protected_input: str | None = None
    candidate_output: str | None = None
    final_article: RenderedArticle | None = None
    final_bundle: ArticleBundle | None = None
    article_path: str | None = None
    draft_path: str | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.errors)

    def fail(self, code: str, *, humanization_failed: bool = False) -> None:
        if code not in self.errors:
            self.errors.append(code)
        self.status = "blocked"
        if humanization_failed:
            self.humanization_status = "failed"

    def report(self) -> ArticleWorkflowReport:
        return ArticleWorkflowReport(
            source_key=self.notice.source_key,
            external_id=self.notice.external_id,
            slug=self.slug,
            status=self.status,
            humanization_status=self.humanization_status,
            article_path=self.article_path,
            draft_path=self.draft_path,
            error_codes=tuple(self.errors),
        )


class LocalHousingWorkflow:
    """Run all local housing phases synchronously under one filesystem lock."""

    def __init__(
        self,
        *,
        collectors: Iterable[Collector],
        humanizer: Humanizer | None,
        output_root: Path,
        mode: str = "live",
        dry_run: bool = False,
    ) -> None:
        if mode not in {"live", "fixture"}:
            raise ValueError("workflow mode must be live or fixture")
        self.collectors = tuple(collectors)
        self.humanizer = humanizer
        self.output_root = Path(output_root)
        self.mode = "dry_run" if dry_run else mode
        self.dry_run = dry_run

    def run(
        self,
        now: datetime,
        days: int = 7,
        humanize: bool = True,
        write_articles: bool = True,
        selected_ids: Collection[str] = (),
    ) -> WorkflowReport:
        _require_timezone_aware(now, "now")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 31:
            raise ValueError("days must be an integer from 1 through 31")
        if not all(isinstance(value, str) and value for value in selected_ids):
            raise ValueError("selected IDs must be non-empty strings")
        selected = tuple(dict.fromkeys(selected_ids))
        observed = now.astimezone(SEOUL)
        window = CollectionWindow(
            start=datetime.combine(
                observed.date() - timedelta(days=days - 1),
                time.min,
                tzinfo=SEOUL,
            ),
            end=observed,
        )
        workflow_id = uuid.uuid4().hex
        run_relative = observed.date().isoformat()
        report_relative = f"{run_relative}/{ACCEPTANCE_FILENAME}"
        phases = [PhaseReport(name) for name in PHASE_NAMES]
        source_reports: tuple[SourceRunReport, ...] = ()
        source_summaries: tuple[SourceWorkflowReport, ...] = ()
        collection: HousingCollectionResult | None = None
        article_work: list[_ArticleWork] = []
        errors: list[str] = []
        all_sources_failed = False
        final_state = False

        lock = _WorkflowLock(self.output_root, workflow_id, observed)
        temporary_context = (
            tempfile.TemporaryDirectory(prefix="wsw-local-housing-")
            if write_articles and not self.dry_run
            else nullcontext(None)
        )

        def snapshot() -> WorkflowReport:
            blocked = all_sources_failed or any(article.blocked for article in article_work)
            incomplete_sources = any(not report.complete for report in source_reports)
            report_errors = tuple(
                dict.fromkeys(
                    [*errors, *(code for article in article_work for code in article.errors)]
                )
            )
            complete = (
                final_state
                and not incomplete_sources
                and not blocked
                and "SELECTED_ID_NOT_FOUND" not in errors
                and "WRITE_FAILED" not in errors
            )
            return WorkflowReport(
                workflow_id=workflow_id,
                started_at=observed,
                window=window,
                mode=self.mode,
                complete=complete,
                blocked=blocked,
                live_success=(
                    complete
                    and self.mode == "live"
                    and not self.dry_run
                    and write_articles
                ),
                sources=source_summaries,
                phases=tuple(phases),
                articles=tuple(article.report() for article in article_work),
                notice_count=len(collection.notices) if collection else 0,
                conflict_count=len(collection.conflicts) if collection else 0,
                excluded_count=collection.excluded_count if collection else 0,
                run_path=None if self.dry_run else run_relative,
                report_path=None if self.dry_run else report_relative,
                error_codes=report_errors,
            )

        def persist() -> None:
            if not self.dry_run:
                _atomic_write_json(lock.root / report_relative, snapshot().as_dict())

        try:
            lock.acquire()
            with temporary_context as temporary_root:
                collected: list[SourceRunReport] = []
                for collector in self.collectors:
                    try:
                        report = collector.collect(window)
                        if not isinstance(report, SourceRunReport):
                            raise TypeError
                    except Exception:
                        report = SourceRunReport(
                            source_key=_collector_source_key(collector),
                            errors=("collector raised an exception",),
                        )
                    collected.append(report)
                source_reports = tuple(collected)
                source_summaries = tuple(
                    SourceWorkflowReport(
                        source_key=report.source_key,
                        status="complete" if report.complete else "failed",
                        notice_count=len(report.notices),
                        warning_count=len(report.warnings),
                        error_codes=("SOURCE_COLLECTION_FAILED",) if report.errors else (),
                    )
                    for report in sorted(
                        source_reports,
                        key=lambda item: (item.source_key.casefold(), item.source_key),
                    )
                )
                failed_sources = sum(not report.complete for report in source_reports)
                if failed_sources:
                    errors.append("SOURCE_INCOMPLETE")
                all_sources_failed = failed_sources >= 2 and failed_sources == len(source_reports)
                if all_sources_failed:
                    errors.append("ALL_SOURCES_FAILED")
                phases[0] = PhaseReport(
                    "collect",
                    "completed" if not failed_sources else "completed_with_errors",
                    count=sum(len(report.notices) for report in source_reports),
                    error_codes=("SOURCE_INCOMPLETE",) if failed_sources else (),
                )
                persist()

                collection = merge_source_reports(source_reports, window)
                phases[1] = PhaseReport(
                    "merge",
                    "completed",
                    count=len(collection.notices),
                    error_codes=("IDENTITY_CONFLICT",) if collection.conflicts else (),
                )
                persist()

                notice_ids = {notice.external_id for notice in collection.notices}
                if any(value not in notice_ids for value in selected):
                    errors.append("SELECTED_ID_NOT_FOUND")
                selected_notices = tuple(
                    notice
                    for notice in collection.notices
                    if needs_detailed_article(notice, selected_ids=selected)
                )
                article_work = [
                    _ArticleWork(notice=notice, slug=notice_slug(notice))
                    for notice in selected_notices
                ]
                phases[2] = PhaseReport(
                    "select",
                    "completed_with_errors"
                    if "SELECTED_ID_NOT_FOUND" in errors
                    else "completed",
                    count=len(article_work),
                    error_codes=("SELECTED_ID_NOT_FOUND",)
                    if "SELECTED_ID_NOT_FOUND" in errors
                    else (),
                )
                persist()

                if write_articles:
                    for article in article_work:
                        try:
                            article.rendered = render_detailed_article(article.notice)
                            article.status = "rendered"
                            if _has_detail_failure(article.notice):
                                article.fail("DETAIL_FAILED")
                        except Exception:
                            article.fail("RENDER_FAILED")
                    render_errors = sum(article.blocked for article in article_work)
                    render_codes = tuple(
                        code
                        for code in ("DETAIL_FAILED", "RENDER_FAILED")
                        if any(code in article.errors for article in article_work)
                    )
                    phases[3] = PhaseReport(
                        "render",
                        "completed_with_errors" if render_errors else "completed",
                        count=sum(article.rendered is not None for article in article_work),
                        error_codes=render_codes,
                    )
                else:
                    for article in article_work:
                        article.status = "not_written"
                        article.humanization_status = "not_requested"
                    phases[3] = PhaseReport("render", "skipped", count=0)
                persist()

                if write_articles and not self.dry_run:
                    assert temporary_root is not None
                    image_root = Path(temporary_root)
                    for article in article_work:
                        if article.blocked:
                            continue
                        try:
                            article.images = build_image_set(
                                article.notice,
                                image_root / article.slug,
                            )
                            article.status = "images_ready"
                        except Exception:
                            article.fail("IMAGE_FAILED")
                    image_errors = sum("IMAGE_FAILED" in article.errors for article in article_work)
                    phases[4] = PhaseReport(
                        "images",
                        "completed_with_errors" if image_errors else "completed",
                        count=sum(article.images is not None for article in article_work),
                        error_codes=("IMAGE_FAILED",) if image_errors else (),
                    )
                else:
                    phases[4] = PhaseReport("images", "skipped", count=0)
                persist()

                if write_articles and not self.dry_run and humanize:
                    for article in article_work:
                        if article.blocked or article.rendered is None:
                            continue
                        try:
                            protected = protect_article_prose(
                                article.rendered.prose_blocks,
                                article.rendered.protected_anchors,
                            )
                            article.protected = protected
                            article.protected_input = protected.document
                            if self.humanizer is None:
                                raise RuntimeError
                            article.candidate_output = self.humanizer.transform(protected.document)
                            article.humanization_status = "candidate"
                            article.status = "humanized"
                        except Exception:
                            article.fail("HUMANIZE_FAILED", humanization_failed=True)
                    humanize_errors = sum(
                        "HUMANIZE_FAILED" in article.errors for article in article_work
                    )
                    phases[5] = PhaseReport(
                        "humanize",
                        "completed_with_errors" if humanize_errors else "completed",
                        count=sum(
                            article.humanization_status == "candidate"
                            for article in article_work
                        ),
                        error_codes=("HUMANIZE_FAILED",) if humanize_errors else (),
                    )
                elif write_articles and not self.dry_run:
                    for article in article_work:
                        if not article.blocked:
                            article.humanization_status = "disabled"
                            article.status = "humanize_disabled"
                    phases[5] = PhaseReport("humanize", "skipped", count=0)
                else:
                    phases[5] = PhaseReport("humanize", "skipped", count=0)
                persist()

                if write_articles and not self.dry_run:
                    for article in article_work:
                        if article.blocked or article.rendered is None or article.images is None:
                            continue
                        try:
                            if humanize:
                                if article.protected is None or article.candidate_output is None:
                                    raise RuntimeError
                                verified_blocks = verify_humanized_candidate(
                                    article.protected,
                                    article.candidate_output,
                                )
                                final_article = replace(
                                    article.rendered,
                                    prose_blocks=verified_blocks,
                                )
                                _require_fact_identity(article.rendered, final_article)
                                article.humanization_status = "verified"
                            else:
                                final_article = article.rendered
                            article.final_article = final_article
                            article.final_bundle = _final_bundle(article, observed.date())
                            article.status = "verified"
                        except Exception:
                            article.fail("HUMANIZE_VERIFY_FAILED", humanization_failed=True)
                    verify_errors = sum(
                        "HUMANIZE_VERIFY_FAILED" in article.errors
                        for article in article_work
                    )
                    phases[6] = PhaseReport(
                        "verify",
                        "completed_with_errors" if verify_errors else "completed",
                        count=sum(article.final_bundle is not None for article in article_work),
                        error_codes=("HUMANIZE_VERIFY_FAILED",) if verify_errors else (),
                    )
                else:
                    phases[6] = PhaseReport("verify", "skipped", count=0)
                persist()

                if self.dry_run:
                    phases[7] = PhaseReport("write", "skipped", count=0)
                    final_state = True
                elif all_sources_failed:
                    phases[7] = PhaseReport(
                        "write",
                        "blocked",
                        count=0,
                        error_codes=("ALL_SOURCES_FAILED",),
                    )
                    final_state = True
                    persist()
                else:
                    try:
                        writer = ArticleBundleWriter(lock.root)
                        final_bundles: list[ArticleBundle] = []
                        for article in article_work:
                            if article.final_bundle is not None:
                                published = writer.write(article.final_bundle)
                                article.article_path = _relative_path(lock.root, published)
                                article.status = "written"
                                final_bundles.append(article.final_bundle)
                            elif article.blocked:
                                diagnostic = _diagnostic_bundle(article, observed.date())
                                published = writer.write(diagnostic)
                                article.draft_path = _relative_path(lock.root, published)
                        index = _workflow_index(collection, article_work)
                        _atomic_write_json(
                            lock.root / run_relative / "notices.json",
                            _notices_document(collection),
                        )
                        run_path = writer.write_run(
                            observed.date(),
                            index,
                            tuple(final_bundles),
                        )
                        phases[7] = PhaseReport(
                            "write",
                            "completed",
                            count=sum(article.status == "written" for article in article_work),
                            paths=(_relative_path(lock.root, run_path),),
                        )
                        final_state = True
                    except Exception:
                        errors.append("WRITE_FAILED")
                        for article in article_work:
                            if article.status == "verified":
                                article.fail("WRITE_FAILED")
                        phases[7] = PhaseReport(
                            "write",
                            "failed",
                            count=sum(article.status == "written" for article in article_work),
                            error_codes=("WRITE_FAILED",),
                        )
                        final_state = True
                    persist()
        finally:
            lock.release()
            if self.dry_run:
                lock.remove_created_empty_root()
        return snapshot()


def merge_source_reports(
    reports: Iterable[SourceRunReport], window: CollectionWindow
) -> HousingCollectionResult:
    """Select, deduplicate, and quarantine notices without changing source reports."""

    source_reports = tuple(
        sorted(reports, key=lambda report: (report.source_key.casefold(), report.source_key))
    )
    grouped: dict[tuple[str, str], dict[str, HousingNotice]] = {}
    conflicts: list[HousingNotice] = []
    for report in source_reports:
        for notice in report.notices:
            if _SHA256_CHECKSUM.fullmatch(notice.source_checksum) is None:
                conflicts.append(notice)
                continue
            key = (notice.source_key, notice.external_id)
            grouped.setdefault(key, {}).setdefault(notice.source_checksum.lower(), notice)

    accepted: list[HousingNotice] = []
    excluded_count = 0
    for variants in grouped.values():
        if len(variants) > 1:
            conflicts.extend(variants.values())
            continue
        notice = next(iter(variants.values()))
        if not window.contains_publication(notice.published_at) or not is_residential(notice):
            excluded_count += 1
            continue
        accepted.append(notice)

    return HousingCollectionResult(
        window=window,
        notices=_ordered_notices(accepted),
        source_reports=source_reports,
        conflicts=_ordered_notices(conflicts),
        excluded_count=excluded_count,
    )


def _ordered_notices(notices: Iterable[HousingNotice]) -> tuple[HousingNotice, ...]:
    return tuple(
        sorted(
            notices,
            key=lambda notice: (
                -notice.published_at.timestamp(),
                notice.publisher.casefold(),
                notice.source_key.casefold(),
                notice.external_id,
                notice.source_checksum,
            ),
        )
    )


def _collector_source_key(collector: object) -> str:
    value = getattr(collector, "source_key", None)
    return value if isinstance(value, str) and value else collector.__class__.__name__.casefold()


def _has_detail_failure(notice: HousingNotice) -> bool:
    return any(
        warning == "DETAIL_COLLECTION_FAILED"
        or warning.casefold().startswith("detail fetch failed")
        for warning in notice.warnings
    )


def _require_fact_identity(draft: RenderedArticle, final: RenderedArticle) -> None:
    if (
        draft.title != final.title
        or draft.slug != final.slug
        or draft.frontmatter != final.frontmatter
        or draft.factual_markdown != final.factual_markdown
        or draft.sources != final.sources
        or draft.protected_anchors != final.protected_anchors
    ):
        raise ValueError("verified prose changed protected article material")


def _final_bundle(article: _ArticleWork, run_date) -> ArticleBundle:
    assert article.rendered is not None
    assert article.final_article is not None
    assert article.images is not None
    files = [
        BundleFile.text("article.draft.md", article.rendered.to_markdown(), "text/markdown"),
        BundleFile.text("article.md", article.final_article.to_markdown(), "text/markdown"),
        BundleFile.text("sources.json", _sources_document(article.rendered), "application/json"),
        BundleFile.text(
            "verification.json",
            _json_text(
                {
                    "status": "verified",
                    "source_key": article.notice.source_key,
                    "external_id": article.notice.external_id,
                    "source_checksum": article.notice.source_checksum,
                    "humanization_status": article.humanization_status,
                }
            ),
            "application/json",
        ),
    ]
    if article.protected_input is not None and article.candidate_output is not None:
        files.extend(
            (
                BundleFile.text("humanize/input.md", article.protected_input, "text/markdown"),
                BundleFile.text("humanize/output.md", article.candidate_output, "text/markdown"),
                BundleFile.text(
                    "humanize/events.ndjson",
                    _humanize_events("completed", "verified"),
                    "application/x-ndjson",
                ),
                BundleFile.text(
                    "humanize/verification.json",
                    _json_text(
                        {
                            "status": "verified",
                            "block_ids": [
                                block.block_id for block in article.final_article.prose_blocks
                            ],
                            "error_codes": [],
                        }
                    ),
                    "application/json",
                ),
            )
        )
    files.extend(
        BundleFile.from_path(
            image.bundle_path,
            image.path,
            image.mime_type,
            image_metadata=image,
        )
        for image in article.images.images
    )
    return ArticleBundle(run_date=run_date, slug=article.slug, files=tuple(files))


def _diagnostic_bundle(article: _ArticleWork, run_date) -> ArticleBundle:
    files = [
        BundleFile.text(
            "status.json",
            _json_text(
                {
                    "status": "draft_blocked",
                    "source_key": article.notice.source_key,
                    "external_id": article.notice.external_id,
                    "error_codes": article.errors,
                    "article_path": None,
                }
            ),
            "application/json",
        ),
        BundleFile.text(
            "verification.json",
            _json_text({"status": "blocked", "error_codes": article.errors}),
            "application/json",
        ),
    ]
    if article.rendered is not None:
        files.extend(
            (
                BundleFile.text(
                    "article.draft.md", article.rendered.to_markdown(), "text/markdown"
                ),
                BundleFile.text(
                    "sources.json", _sources_document(article.rendered), "application/json"
                ),
            )
        )
    if article.protected_input is not None:
        files.append(
            BundleFile.text("humanize/input.md", article.protected_input, "text/markdown")
        )
    if article.candidate_output is not None:
        files.append(
            BundleFile.text("humanize/output.md", article.candidate_output, "text/markdown")
        )
    if article.protected_input is not None:
        files.extend(
            (
                BundleFile.text(
                    "humanize/events.ndjson",
                    _humanize_events(
                        "failed" if "HUMANIZE_FAILED" in article.errors else "completed",
                        "blocked",
                    ),
                    "application/x-ndjson",
                ),
                BundleFile.text(
                    "humanize/verification.json",
                    _json_text({"status": "blocked", "error_codes": article.errors}),
                    "application/json",
                ),
            )
        )
    if article.images is not None:
        files.extend(
            BundleFile.from_path(
                image.bundle_path,
                image.path,
                image.mime_type,
                image_metadata=image,
            )
            for image in article.images.images
        )
    return ArticleBundle(
        run_date=run_date,
        slug=f"{article.slug}-draft-blocked",
        files=tuple(files),
    )


def _workflow_index(
    collection: HousingCollectionResult,
    articles: Sequence[_ArticleWork],
) -> str:
    selected_ids = tuple(article.notice.external_id for article in articles)
    markdown = render_weekly_index(
        _sanitized_index_collection(collection),
        selected_ids=selected_ids,
    ).to_markdown()
    for article in articles:
        pattern = re.compile(_MARKDOWN_DETAIL_LINK % re.escape(article.slug))
        if article.status == "written" and article.article_path is not None:
            target_name = Path(article.article_path).name
            replacement = f"[DETAIL](./{target_name}/article.md)"
        elif article.errors:
            replacement = f"[BLOCKED: {article.errors[0]}]"
        else:
            replacement = "[NOT WRITTEN: WRITE_DISABLED]"
        markdown = pattern.sub(lambda _match, value=replacement: value, markdown)
    return markdown


def _sanitized_index_collection(
    collection: HousingCollectionResult,
) -> HousingCollectionResult:
    source_reports = tuple(
        SourceRunReport(
            source_key=report.source_key,
            notices=report.notices,
            warnings=("SOURCE_WARNING",) if report.warnings else (),
            errors=("SOURCE_COLLECTION_FAILED",) if report.errors else (),
        )
        for report in collection.source_reports
    )
    return HousingCollectionResult(
        window=collection.window,
        notices=collection.notices,
        source_reports=source_reports,
        conflicts=collection.conflicts,
        excluded_count=collection.excluded_count,
    )


def _sources_document(article: RenderedArticle) -> str:
    return _json_text([source.as_dict() for source in article.sources])


def _notices_document(collection: HousingCollectionResult) -> dict[str, object]:
    return {
        "schema_version": 1,
        "window": {
            "start": collection.window.start.isoformat(),
            "end": collection.window.end.isoformat(),
        },
        "complete": collection.complete,
        "notices": [
            {
                "source_key": notice.source_key,
                "external_id": notice.external_id,
                "canonical_url": notice.canonical_url,
                "title": notice.title,
                "publisher": notice.publisher,
                "category": notice.category,
                "region": notice.region,
                "status": notice.status,
                "published_at": notice.published_at.isoformat(),
                "application_start": (
                    notice.application_start.isoformat() if notice.application_start else None
                ),
                "application_end": (
                    notice.application_end.isoformat() if notice.application_end else None
                ),
                "deadline": notice.deadline.isoformat() if notice.deadline else None,
                "announcement_date": (
                    notice.announcement_date.isoformat() if notice.announcement_date else None
                ),
                "supply_count": notice.supply_count,
                "price_summary": notice.price_summary,
                "eligibility_summary": list(notice.eligibility_summary),
                "restriction_summary": list(notice.restriction_summary),
                "facts": [list(pair) for pair in notice.facts],
                "source_checksum": notice.source_checksum,
                "parser_version": notice.parser_version,
                "warning_count": len(notice.warnings),
            }
            for notice in collection.notices
        ],
        "conflicts": [
            {
                "source_key": notice.source_key,
                "external_id": notice.external_id,
                "source_checksum": notice.source_checksum,
                "status": "quarantined",
            }
            for notice in collection.conflicts
        ],
    }


def _humanize_events(humanize_status: str, verify_status: str) -> str:
    return "\n".join(
        (
            json.dumps(
                {"type": "workflow", "phase": "humanize", "status": humanize_status},
                sort_keys=True,
                separators=(",", ":"),
            ),
            json.dumps(
                {"type": "workflow", "phase": "verify", "status": verify_status},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    ) + "\n"


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _atomic_write_json(path: Path, payload: object) -> None:
    target = Path(path)
    _reject_link_ancestors(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_link_ancestors(target.parent)
    encoded = _json_text(payload).encode("utf-8")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _reject_link_ancestors(target.parent)
        os.replace(temporary, target)
        _reject_link_ancestors(target.parent)
        _fsync_directory(target.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class _WorkflowLock:
    def __init__(self, root: Path, workflow_id: str, started_at: datetime) -> None:
        self.root = Path(os.path.abspath(root))
        self.path = self.root / LOCK_FILENAME
        self.workflow_id = workflow_id
        self.started_at = started_at
        self.fingerprint: tuple[object, ...] | None = None
        self.root_created = False

    def acquire(self) -> None:
        _reject_link_ancestors(self.root)
        self.root_created = not self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        _reject_link_ancestors(self.root)
        for _attempt in range(2):
            try:
                self._create()
                return
            except FileExistsError:
                self._recover_stale()
        raise WorkflowLockedError("local housing workflow is already running")

    def _create(self) -> None:
        payload = _json_text(
            {
                "pid": os.getpid(),
                "started_at": self.started_at.isoformat(),
                "workflow_id": self.workflow_id,
            }
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
            self.fingerprint = _metadata_fingerprint(os.fstat(descriptor))
        except Exception:
            os.close(descriptor)
            descriptor = -1
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        _fsync_directory(self.root)

    def _recover_stale(self) -> None:
        try:
            metadata, document = _read_lock(self.path)
            if set(document) != {"pid", "started_at", "workflow_id"}:
                raise ValueError
            pid = document["pid"]
            started_at = document["started_at"]
            workflow_id = document["workflow_id"]
            if (
                isinstance(pid, bool)
                or not isinstance(pid, int)
                or pid <= 0
                or not isinstance(started_at, str)
                or not isinstance(workflow_id, str)
                or not workflow_id
            ):
                raise ValueError
            parsed_start = datetime.fromisoformat(started_at)
            if parsed_start.tzinfo is None or parsed_start.utcoffset() is None:
                raise ValueError
        except (OSError, ValueError, json.JSONDecodeError):
            raise WorkflowLockedError(
                "local housing workflow lock owner cannot be proven absent"
            ) from None
        if _pid_state(pid) != "absent":
            raise WorkflowLockedError("local housing workflow is already running")
        try:
            current = os.lstat(self.path)
        except OSError:
            raise WorkflowLockedError(
                "local housing workflow lock changed during recovery"
            ) from None
        if _metadata_fingerprint(current) != _metadata_fingerprint(metadata):
            raise WorkflowLockedError("local housing workflow lock changed during recovery")
        try:
            self.path.unlink()
            _fsync_directory(self.root)
        except OSError:
            raise WorkflowLockedError("stale workflow lock could not be recovered") from None

    def release(self) -> None:
        if self.fingerprint is None:
            return
        try:
            metadata, document = _read_lock(self.path)
            if (
                _metadata_fingerprint(metadata) == self.fingerprint
                and document.get("workflow_id") == self.workflow_id
                and document.get("pid") == os.getpid()
            ):
                self.path.unlink()
                _fsync_directory(self.root)
        except (OSError, ValueError, json.JSONDecodeError):
            return
        finally:
            self.fingerprint = None

    def remove_created_empty_root(self) -> None:
        if self.root_created:
            try:
                self.root.rmdir()
            except OSError:
                pass


def _read_lock(path: Path) -> tuple[os.stat_result, dict[str, object]]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or _is_reparse(metadata):
            raise ValueError("workflow lock is not a regular file")
        if metadata.st_size > _MAX_LOCK_BYTES:
            raise ValueError("workflow lock is too large")
        payload = os.read(descriptor, _MAX_LOCK_BYTES + 1)
        if len(payload) > _MAX_LOCK_BYTES:
            raise ValueError("workflow lock is too large")
    finally:
        os.close(descriptor)
    document = json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_strict_json_pairs,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(document, dict) or not all(isinstance(key, str) for key in document):
        raise ValueError("workflow lock schema is invalid")
    return metadata, document


def _strict_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate workflow lock key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-standard workflow lock value")


def _pid_state(pid: int) -> str:
    """Return active, absent, or unknown without sending a signal on Windows."""

    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not handle:
            return "absent" if ctypes.get_last_error() == 87 else "unknown"
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return "unknown"
            return "active" if exit_code.value == 259 else "absent"
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "active"
    except OSError as exc:
        return "absent" if exc.errno == errno.ESRCH else "unknown"
    return "active"


def _reject_link_ancestors(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    existing: list[Path] = []
    cursor = absolute
    while True:
        if cursor.exists():
            existing.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    for candidate in reversed(existing):
        metadata = os.lstat(candidate)
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise WorkflowLockedError("workflow output root contains a link or reparse point")


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _metadata_fingerprint(metadata: os.stat_result) -> tuple[object, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        getattr(metadata, "st_file_attributes", 0),
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
