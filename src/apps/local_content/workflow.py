"""Synchronous, fail-closed local housing collection and article workflow."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from collections.abc import Collection, Iterable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from apps.local_content.bundles import (
    ArticleBundle,
    ArticleBundleWriter,
    BundleFile,
    BundlePublishError,
    BundleValidationError,
    RunDirectoryLease,
)
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
ACCEPTANCE_FILENAME = "acceptance-report.json"
_SHA256_CHECKSUM = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
_MARKDOWN_DETAIL_LINK = r"\[[^\]\r\n]+\]\(\./%s/article\.md\)"
_MAX_LOCK_BYTES = 4096
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class Collector(Protocol):
    def collect(self, window: CollectionWindow) -> SourceRunReport: ...


class Humanizer(Protocol):
    def transform(self, document: str) -> str: ...


class WorkflowError(RuntimeError):
    """A metadata-safe workflow failure with one stable public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkflowLockedError(WorkflowError):
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
        state_root: Path | None = None,
        mode: str = "live",
        dry_run: bool = False,
    ) -> None:
        if mode not in {"live", "fixture"}:
            raise ValueError("workflow mode must be live or fixture")
        self.collectors = tuple(collectors)
        self.humanizer = humanizer
        self.output_root = Path(output_root)
        self.state_root = (
            Path(state_root)
            if state_root is not None
            else self.output_root.parent / ".local-state"
        )
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
        run_relative: str | None = None
        report_relative: str | None = None
        run_root: Path | None = None
        run_lease: RunDirectoryLease | None = None
        writer: ArticleBundleWriter | None = None
        phases = [PhaseReport(name) for name in PHASE_NAMES]
        source_reports: tuple[SourceRunReport, ...] = ()
        source_summaries: tuple[SourceWorkflowReport, ...] = ()
        collection: HousingCollectionResult | None = None
        article_work: list[_ArticleWork] = []
        errors: list[str] = []
        detail_failure_ids: tuple[str, ...] = ()
        detail_failure_records: tuple[tuple[str, str], ...] = ()
        all_sources_failed = False
        final_state = False
        run_committed = False

        lock = _WorkflowGuard(
            self.output_root,
            self.state_root,
            workflow_id,
            observed,
        )
        temporary_context = (
            tempfile.TemporaryDirectory(prefix="wsw-local-housing-")
            if write_articles and not self.dry_run
            else nullcontext(None)
        )

        def snapshot() -> WorkflowReport:
            blocked = all_sources_failed or any(article.blocked for article in article_work)
            incomplete_sources = any(not report.complete for report in source_reports) or bool(
                detail_failure_ids
            )
            report_errors = tuple(
                dict.fromkeys(
                    [*errors, *(code for article in article_work for code in article.errors)]
                )
            )
            complete = (
                final_state
                and not incomplete_sources
                and not blocked
                and not errors
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

        def verify_generation(*, allow_completed: bool = False) -> None:
            lock.verify()
            if writer is not None and run_lease is not None:
                try:
                    writer.verify_run_directory_lease(
                        run_lease,
                        allow_completed=allow_completed,
                    )
                except BundleValidationError:
                    raise WorkflowError("RUN_LEASE_CHANGED") from None

        def persist() -> None:
            if not self.dry_run:
                if run_root is None or writer is None or run_lease is None:
                    raise WorkflowError("RUN_PATH_UNAVAILABLE")
                verify_generation()
                _atomic_write_json(
                    run_root / ACCEPTANCE_FILENAME,
                    snapshot().as_dict(),
                )
                verify_generation()

        try:
            lock.acquire()
            if self.dry_run:
                lock.bind_output_root(create=False)
            else:
                lock.bind_output_root(create=True)
                lock.verify()
                writer = ArticleBundleWriter(lock.output_root)
                try:
                    run_lease = writer.reserve_run_directory(
                        observed.date(),
                        workflow_id=workflow_id,
                    )
                except BundlePublishError:
                    raise WorkflowError("RUN_LEASE_RESERVATION_FAILED") from None
                run_relative = run_lease.name
                run_root = lock.output_root / run_relative
                report_relative = f"{run_relative}/{ACCEPTANCE_FILENAME}"
                verify_generation()
            verify_generation()
            with temporary_context as temporary_root:
                collected: list[SourceRunReport] = []
                for collector in self.collectors:
                    verify_generation()
                    try:
                        report = collector.collect(window)
                        if not isinstance(report, SourceRunReport):
                            raise TypeError
                    except Exception:
                        report = SourceRunReport(
                            source_key=_collector_source_key(collector),
                            errors=("collector raised an exception",),
                        )
                    verify_generation()
                    collected.append(report)
                source_reports = tuple(collected)
                detail_failure_records = tuple(
                    (report.source_key, notice.external_id)
                    for report in source_reports
                    for notice in report.notices
                    if _has_detail_failure(notice)
                )
                detail_failure_ids = tuple(
                    external_id for _source_key, external_id in detail_failure_records
                )
                source_summaries = tuple(
                    SourceWorkflowReport(
                        source_key=report.source_key,
                        status=(
                            "failed"
                            if not report.complete
                            else (
                                "incomplete"
                                if any(
                                    _has_detail_failure(notice)
                                    for notice in report.notices
                                )
                                else "complete"
                            )
                        ),
                        notice_count=len(report.notices),
                        warning_count=len(report.warnings),
                        error_codes=tuple(
                            code
                            for code, present in (
                                ("SOURCE_COLLECTION_FAILED", bool(report.errors)),
                                (
                                    "DETAIL_COLLECTION_FAILED",
                                    any(
                                        _has_detail_failure(notice)
                                        for notice in report.notices
                                    ),
                                ),
                            )
                            if present
                        ),
                    )
                    for report in sorted(
                        source_reports,
                        key=lambda item: (item.source_key.casefold(), item.source_key),
                    )
                )
                failed_sources = sum(not report.complete for report in source_reports)
                if failed_sources:
                    errors.append("SOURCE_INCOMPLETE")
                if detail_failure_ids:
                    errors.append("DETAIL_INCOMPLETE")
                all_sources_failed = failed_sources >= 2 and failed_sources == len(source_reports)
                if all_sources_failed:
                    errors.append("ALL_SOURCES_FAILED")
                phases[0] = PhaseReport(
                    "collect",
                    "completed"
                    if not failed_sources and not detail_failure_ids
                    else "completed_with_errors",
                    count=sum(len(report.notices) for report in source_reports),
                    error_codes=tuple(
                        code
                        for code, present in (
                            ("SOURCE_INCOMPLETE", bool(failed_sources)),
                            ("DETAIL_INCOMPLETE", bool(detail_failure_ids)),
                        )
                        if present
                    ),
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
                verify_generation()
                prior_detailed_ids = _validated_prior_detailed_ids(
                    lock.output_root,
                    current_run=run_relative,
                )
                verify_generation()
                selected_notices = tuple(
                    notice
                    for notice in collection.notices
                    if needs_detailed_article(
                        notice,
                        selected_ids=selected,
                        prior_detailed_ids=prior_detailed_ids,
                    )
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
                            verify_generation()
                            try:
                                article.images = build_image_set(
                                    article.notice,
                                    image_root / article.slug,
                                )
                            finally:
                                verify_generation()
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
                        except Exception:
                            article.fail(
                                "HUMANIZE_PROTECTION_FAILED",
                                humanization_failed=True,
                            )
                            continue
                        try:
                            verify_generation()
                            if self.humanizer is None:
                                raise RuntimeError
                            try:
                                article.candidate_output = self.humanizer.transform(
                                    protected.document
                                )
                            finally:
                                verify_generation()
                            article.humanization_status = "candidate"
                            article.status = "humanized"
                        except Exception:
                            article.fail(
                                "HUMANIZE_TRANSFORM_FAILED",
                                humanization_failed=True,
                            )
                    humanize_codes = tuple(
                        code
                        for code in (
                            "HUMANIZE_PROTECTION_FAILED",
                            "HUMANIZE_TRANSFORM_FAILED",
                        )
                        if any(code in article.errors for article in article_work)
                    )
                    phases[5] = PhaseReport(
                        "humanize",
                        "completed_with_errors" if humanize_codes else "completed",
                        count=sum(
                            article.humanization_status == "candidate"
                            for article in article_work
                        ),
                        error_codes=humanize_codes,
                    )
                elif write_articles and not self.dry_run:
                    for article in article_work:
                        if not article.blocked:
                            article.humanization_status = "disabled"
                            article.fail("HUMANIZATION_DISABLED")
                    phases[5] = PhaseReport(
                        "humanize",
                        "completed_with_errors" if article_work else "skipped",
                        count=0,
                        error_codes=("HUMANIZATION_DISABLED",) if article_work else (),
                    )
                else:
                    phases[5] = PhaseReport("humanize", "skipped", count=0)
                persist()

                if write_articles and not self.dry_run:
                    for article in article_work:
                        if article.blocked or article.rendered is None or article.images is None:
                            continue
                        try:
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
                            article.final_article = final_article
                            article.status = "verified"
                        except Exception:
                            article.fail("HUMANIZE_VERIFY_FAILED", humanization_failed=True)
                            continue
                        try:
                            article.final_bundle = _final_bundle(article, observed.date())
                        except Exception:
                            article.fail("BUNDLE_ASSEMBLY_FAILED")
                    verify_codes = tuple(
                        code
                        for code in (
                            "HUMANIZE_VERIFY_FAILED",
                            "BUNDLE_ASSEMBLY_FAILED",
                        )
                        if any(code in article.errors for article in article_work)
                    )
                    phases[6] = PhaseReport(
                        "verify",
                        "completed_with_errors" if verify_codes else "completed",
                        count=sum(article.final_bundle is not None for article in article_work),
                        error_codes=verify_codes,
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
                    assert run_relative is not None
                    assert run_root is not None
                    assert writer is not None
                    assert run_lease is not None
                    final_bundles: list[ArticleBundle] = []

                    def publish_diagnostic(article: _ArticleWork) -> None:
                        try:
                            verify_generation()
                            diagnostic = _diagnostic_bundle(article, observed.date())
                            published = writer.write(
                                diagnostic,
                                run_directory=run_lease,
                            )
                            article.draft_path = _relative_path(
                                lock.output_root,
                                published,
                            )
                            verify_generation()
                        except (BundleValidationError, OSError, ValueError):
                            article.fail("BUNDLE_DIAGNOSTIC_WRITE_FAILED")

                    for article in article_work:
                        if article.final_bundle is not None:
                            try:
                                verify_generation()
                                published = writer.write(
                                    article.final_bundle,
                                    run_directory=run_lease,
                                )
                                article.article_path = _relative_path(
                                    lock.output_root,
                                    published,
                                )
                                article.status = "written"
                                final_bundles.append(article.final_bundle)
                                verify_generation()
                            except (BundleValidationError, OSError):
                                article.fail("BUNDLE_WRITE_FAILED")
                                publish_diagnostic(article)
                        elif article.blocked:
                            publish_diagnostic(article)
                    index = _workflow_index(
                        collection,
                        article_work,
                        detail_failures=detail_failure_records,
                    )
                    verify_generation()
                    _atomic_write_json(
                        run_root / "notices.json",
                        _notices_document(
                            collection,
                            detail_failures=detail_failure_records,
                        ),
                    )
                    verify_generation()
                    storage_codes = tuple(
                        code
                        for code in (
                            "BUNDLE_WRITE_FAILED",
                            "BUNDLE_DIAGNOSTIC_WRITE_FAILED",
                        )
                        if any(code in article.errors for article in article_work)
                    )
                    phases[7] = PhaseReport(
                        "write",
                        "completed_with_errors" if storage_codes else "completed",
                        count=sum(article.status == "written" for article in article_work),
                        paths=(run_relative,),
                        error_codes=storage_codes,
                    )
                    final_state = True
                    persist()
                    try:
                        verify_generation()
                        run_path = writer.write_run(
                            observed.date(),
                            index,
                            tuple(final_bundles),
                            run_directory=run_lease,
                        )
                        if _relative_path(lock.output_root, run_path) != run_relative:
                            raise BundlePublishError("run lease returned another path")
                        run_committed = True
                        verify_generation(allow_completed=True)
                    except (BundleValidationError, OSError):
                        errors.append("RUN_COMMIT_FAILED")
                        phases[7] = PhaseReport(
                            "write",
                            "failed",
                            count=sum(article.status == "written" for article in article_work),
                            paths=(run_relative,),
                            error_codes=(*storage_codes, "RUN_COMMIT_FAILED"),
                        )
                        final_state = True
                        try:
                            persist()
                        except (WorkflowError, BundleValidationError):
                            pass
                verify_generation(allow_completed=run_committed)
        finally:
            primary_error = sys.exc_info()[0] is not None
            try:
                lock.release()
            except WorkflowError:
                if not primary_error:
                    raise
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


def _validated_prior_detailed_ids(
    output_root: Path,
    *,
    current_run: str | None,
) -> tuple[str, ...]:
    if not _workflow_path_exists(output_root):
        return ()
    _reject_link_ancestors(output_root)
    pattern = re.compile(
        r"(?P<date>\d{4}-\d{2}-\d{2})(?:--run-[0-9a-f]{12})?"
    )
    try:
        candidates = tuple(
            sorted(
                (path.name, match.group("date"))
                for path in output_root.iterdir()
                if path.name != current_run
                and (match := pattern.fullmatch(path.name)) is not None
            )
        )
    except OSError:
        raise WorkflowError("PRIOR_RUN_SCAN_FAILED") from None
    if len(candidates) > 512:
        raise WorkflowError("PRIOR_RUN_SCAN_LIMIT")
    writer = ArticleBundleWriter(output_root)
    result: set[str] = set()
    for run_name, run_date_text in candidates:
        try:
            run_date = date.fromisoformat(run_date_text)
            metadata_rows = writer.validated_run_article_metadata(
                run_date,
                run_directory=run_name,
            )
        except (BundlePublishError, OSError):
            continue
        for metadata in metadata_rows:
            if set(metadata) != {
                "status",
                "source_key",
                "external_id",
                "source_checksum",
                "humanization_status",
            }:
                continue
            external_id = metadata.get("external_id")
            if (
                metadata.get("status") == "verified"
                and metadata.get("humanization_status") == "verified"
                and isinstance(metadata.get("source_key"), str)
                and isinstance(external_id, str)
                and re.fullmatch(r"[A-Za-z0-9:._-]{1,512}", external_id) is not None
                and isinstance(metadata.get("source_checksum"), str)
                and _SHA256_CHECKSUM.fullmatch(metadata["source_checksum"]) is not None
            ):
                result.add(external_id)
    return tuple(sorted(result))


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
    humanization_codes = {
        "HUMANIZE_PROTECTION_FAILED",
        "HUMANIZE_TRANSFORM_FAILED",
        "HUMANIZE_VERIFY_FAILED",
        "HUMANIZATION_DISABLED",
    }
    if article.protected_input is not None or humanization_codes.intersection(
        article.errors
    ):
        if "HUMANIZATION_DISABLED" in article.errors:
            humanize_status = "disabled"
        elif "HUMANIZE_PROTECTION_FAILED" in article.errors:
            humanize_status = "protection_failed"
        elif "HUMANIZE_TRANSFORM_FAILED" in article.errors:
            humanize_status = "failed"
        else:
            humanize_status = "completed"
        verify_status = (
            "verified" if article.humanization_status == "verified" else "blocked"
        )
        files.extend(
            (
                BundleFile.text(
                    "humanize/events.ndjson",
                    _humanize_events(humanize_status, verify_status),
                    "application/x-ndjson",
                ),
                BundleFile.text(
                    "humanize/verification.json",
                    _json_text(
                        {"status": verify_status, "error_codes": article.errors}
                    ),
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
    *,
    detail_failures: Sequence[tuple[str, str]] = (),
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
    affected = tuple(dict.fromkeys(detail_failures))
    if affected:
        lines = ["", "## Workflow notice status", ""]
        lines.extend(
            f"- `{_safe_status_id(source_key)}:{_safe_status_id(external_id)}`: "
            "DETAIL_COLLECTION_FAILED"
            for source_key, external_id in affected
        )
        markdown = markdown.rstrip() + "\n" + "\n".join(lines) + "\n"
    return markdown


def _sanitized_index_collection(
    collection: HousingCollectionResult,
) -> HousingCollectionResult:
    source_reports = tuple(
        SourceRunReport(
            source_key=report.source_key,
            notices=report.notices,
            warnings=("SOURCE_WARNING",) if report.warnings else (),
            errors=tuple(
                code
                for code, present in (
                    ("SOURCE_COLLECTION_FAILED", bool(report.errors)),
                    (
                        "DETAIL_COLLECTION_FAILED",
                        any(_has_detail_failure(notice) for notice in report.notices),
                    ),
                )
                if present
            ),
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


def _safe_status_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9:._-]", "_", value)[:240]


def _notices_document(
    collection: HousingCollectionResult,
    *,
    detail_failures: Sequence[tuple[str, str]] = (),
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "window": {
            "start": collection.window.start.isoformat(),
            "end": collection.window.end.isoformat(),
        },
        "complete": collection.complete and not detail_failures,
        "detail_failures": [
            {
                "source_key": source_key,
                "external_id": external_id,
                "error_codes": ["DETAIL_COLLECTION_FAILED"],
            }
            for source_key, external_id in dict.fromkeys(detail_failures)
        ],
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
                "error_codes": (
                    ["DETAIL_COLLECTION_FAILED"]
                    if _has_detail_failure(notice)
                    else []
                ),
            }
            for notice in collection.notices
        ],
        "conflicts": [
            {
                "source_key": notice.source_key,
                "external_id": notice.external_id,
                "source_checksum": notice.source_checksum,
                "status": "quarantined",
                "error_codes": (
                    ["DETAIL_COLLECTION_FAILED"]
                    if _has_detail_failure(notice)
                    else []
                ),
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
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise WorkflowError("REPORT_DIRECTORY_PREPARE_FAILED") from None
    _reject_link_ancestors(target.parent)
    try:
        encoded = _json_text(payload).encode("utf-8")
    except (TypeError, UnicodeError, ValueError):
        raise WorkflowError("REPORT_SERIALIZE_FAILED") from None
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except OSError:
            raise WorkflowError("REPORT_OPEN_FAILED") from None
        offset = 0
        try:
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError
                offset += written
        except OSError:
            raise WorkflowError("REPORT_WRITE_FAILED") from None
        try:
            os.fsync(descriptor)
        except OSError:
            raise WorkflowError("REPORT_FILE_FSYNC_FAILED") from None
        try:
            os.close(descriptor)
        except OSError:
            raise WorkflowError("REPORT_CLOSE_FAILED") from None
        descriptor = None
        _reject_link_ancestors(target.parent)
        try:
            os.replace(temporary, target)
        except OSError:
            raise WorkflowError("REPORT_REPLACE_FAILED") from None
        _reject_link_ancestors(target.parent)
        _fsync_directory(target.parent)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class _WorkflowGuard:
    def __init__(
        self,
        output_root: Path,
        state_root: Path,
        workflow_id: str,
        started_at: datetime,
    ) -> None:
        _reject_link_ancestors(Path(output_root))
        self.output_root = Path(_canonical_path(output_root))
        self.state_root = Path(os.path.abspath(state_root))
        self.guard_root = self.state_root / "locks"
        digest = hashlib.sha256(
            _canonical_path(output_root).encode("utf-8")
        ).hexdigest()
        self.path = self.guard_root / f"{digest}.guard"
        self.workflow_id = workflow_id
        self.started_at = started_at
        self.descriptor: int | None = None
        self.locked = False
        self.state_identity: tuple[object, ...] | None = None
        self.guard_root_identity: tuple[object, ...] | None = None
        self.guard_identity: tuple[object, ...] | None = None
        self.output_identity: tuple[object, ...] | None = None
        self.output_absent_parent: tuple[Path, tuple[object, ...]] | None = None

    def acquire(self) -> None:
        if os.name not in {"nt", "posix"}:
            raise WorkflowError("WORKFLOW_LOCK_UNSUPPORTED")
        try:
            _reject_link_ancestors(self.state_root)
            self.state_root.mkdir(parents=True, exist_ok=True)
            _reject_link_ancestors(self.state_root)
            self.guard_root.mkdir(parents=True, exist_ok=True)
            _reject_link_ancestors(self.guard_root)
            self.state_identity = _directory_identity(self.state_root, "state root")
            self.guard_root_identity = _directory_identity(
                self.guard_root,
                "guard root",
            )
            if _workflow_path_exists(self.path):
                _regular_file_identity(self.path, "workflow guard")
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            descriptor = os.open(self.path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or _is_reparse(metadata)
                or metadata.st_nlink != 1
            ):
                os.close(descriptor)
                raise WorkflowError("WORKFLOW_GUARD_UNSAFE")
            self.descriptor = descriptor
            self.guard_identity = _identity_fingerprint(metadata)
            self._verify_guard_paths()
            self._lock_nonblocking()
            self._verify_guard_paths()
            self._write_metadata()
            self._verify_guard_paths()
        except WorkflowError:
            try:
                self.release()
            except WorkflowError:
                pass
            raise
        except OSError:
            try:
                self.release()
            except WorkflowError:
                pass
            raise WorkflowError("WORKFLOW_LOCK_UNAVAILABLE") from None

    def bind_output_root(self, *, create: bool) -> None:
        try:
            _reject_link_ancestors(self.output_root)
            if create:
                self.output_root.mkdir(parents=True, exist_ok=True)
                _reject_link_ancestors(self.output_root)
            if _workflow_path_exists(self.output_root):
                self.output_identity = _directory_identity(
                    self.output_root,
                    "output root",
                )
                self.output_absent_parent = None
            else:
                parent = _nearest_existing_parent(self.output_root)
                self.output_absent_parent = (
                    parent,
                    _directory_identity(parent, "output ancestor"),
                )
                self.output_identity = None
        except WorkflowError:
            raise
        except OSError:
            raise WorkflowError("OUTPUT_ROOT_UNSAFE") from None

    def verify(self) -> None:
        self._verify_guard_paths()
        _reject_link_ancestors(self.output_root)
        if self.output_identity is not None:
            if _directory_identity(self.output_root, "output root") != self.output_identity:
                raise WorkflowError("OUTPUT_ROOT_CHANGED")
        elif self.output_absent_parent is not None:
            if _workflow_path_exists(self.output_root):
                raise WorkflowError("OUTPUT_ROOT_CHANGED")
            parent, identity = self.output_absent_parent
            if _directory_identity(parent, "output ancestor") != identity:
                raise WorkflowError("OUTPUT_ROOT_CHANGED")

    def _verify_guard_paths(self) -> None:
        if self.state_identity is None or self.guard_root_identity is None:
            raise WorkflowError("WORKFLOW_GUARD_UNAVAILABLE")
        if _directory_identity(self.state_root, "state root") != self.state_identity:
            raise WorkflowError("WORKFLOW_STATE_ROOT_CHANGED")
        if (
            _directory_identity(self.guard_root, "guard root")
            != self.guard_root_identity
        ):
            raise WorkflowError("WORKFLOW_GUARD_ROOT_CHANGED")
        if self.guard_identity is None or (
            _regular_file_identity(self.path, "workflow guard")
            != self.guard_identity
        ):
            raise WorkflowError("WORKFLOW_GUARD_CHANGED")

    def _lock_nonblocking(self) -> None:
        if self.descriptor is None:
            raise WorkflowError("WORKFLOW_GUARD_UNAVAILABLE")
        try:
            if os.name == "nt":
                os.lseek(self.descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self.descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locked = True
        except OSError as exc:
            contention = {errno.EACCES, errno.EAGAIN}
            if hasattr(errno, "EDEADLK"):
                contention.add(errno.EDEADLK)
            unsupported = {errno.ENOSYS}
            for name in ("ENOTSUP", "EOPNOTSUPP"):
                if hasattr(errno, name):
                    unsupported.add(getattr(errno, name))
            if exc.errno in contention:
                raise WorkflowLockedError("WORKFLOW_ALREADY_RUNNING") from None
            if exc.errno in unsupported:
                raise WorkflowError("WORKFLOW_LOCK_UNSUPPORTED") from None
            raise WorkflowError("WORKFLOW_LOCK_FAILED") from None

    def _write_metadata(self) -> None:
        if self.descriptor is None:
            raise WorkflowError("WORKFLOW_GUARD_UNAVAILABLE")
        payload = _json_text(
            {
                "pid": os.getpid(),
                "started_at": self.started_at.isoformat(),
                "workflow_id": self.workflow_id,
            }
        ).encode("utf-8")
        if len(payload) > _MAX_LOCK_BYTES:
            raise WorkflowError("WORKFLOW_GUARD_METADATA_INVALID")
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        os.ftruncate(self.descriptor, 0)
        offset = 0
        while offset < len(payload):
            written = os.write(self.descriptor, payload[offset:])
            if written <= 0:
                raise WorkflowError("WORKFLOW_GUARD_METADATA_WRITE_FAILED")
            offset += written
        os.ftruncate(self.descriptor, len(payload))
        os.fsync(self.descriptor)
        os.lseek(self.descriptor, 0, os.SEEK_SET)

    def release(self) -> None:
        descriptor = self.descriptor
        if descriptor is None:
            return
        unlock_error = False
        close_error = False
        try:
            if self.locked and os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            elif self.locked and os.name == "posix":
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            unlock_error = True
        try:
            os.close(descriptor)
        except OSError:
            close_error = True
        else:
            self.descriptor = None
            self.locked = False
        if unlock_error:
            raise WorkflowError("WORKFLOW_UNLOCK_FAILED")
        if close_error:
            raise WorkflowError("WORKFLOW_GUARD_CLOSE_FAILED")


def _reject_link_ancestors(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    cursor = absolute
    while True:
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError:
            pass
        except OSError:
            raise WorkflowError("WORKFLOW_PATH_INSPECTION_FAILED") from None
        else:
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                raise WorkflowError("PATH_LINK_OR_REPARSE_UNSAFE")
        if cursor == cursor.parent:
            break
        cursor = cursor.parent


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _identity_fingerprint(metadata: os.stat_result) -> tuple[object, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        getattr(metadata, "st_file_attributes", 0),
    )


def _canonical_path(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _directory_identity(path: Path, _label: str) -> tuple[object, ...]:
    try:
        metadata = os.lstat(path)
    except OSError:
        raise WorkflowError("WORKFLOW_PATH_UNAVAILABLE") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
    ):
        raise WorkflowError("WORKFLOW_DIRECTORY_UNSAFE")
    return _identity_fingerprint(metadata)


def _regular_file_identity(path: Path, _label: str) -> tuple[object, ...]:
    try:
        metadata = os.lstat(path)
    except OSError:
        raise WorkflowError("WORKFLOW_GUARD_UNAVAILABLE") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or metadata.st_nlink != 1
    ):
        raise WorkflowError("WORKFLOW_GUARD_UNSAFE")
    return _identity_fingerprint(metadata)


def _nearest_existing_parent(path: Path) -> Path:
    cursor = path.parent
    while not _workflow_path_exists(cursor):
        if cursor == cursor.parent:
            raise WorkflowError("OUTPUT_ROOT_UNAVAILABLE")
        cursor = cursor.parent
    return cursor


def _workflow_path_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        raise WorkflowError("WORKFLOW_PATH_INSPECTION_FAILED") from None
    return True


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if _directory_fsync_is_unsupported(exc):
            return
        raise WorkflowError("REPORT_DIRECTORY_FSYNC_OPEN_FAILED") from None
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if not _directory_fsync_is_unsupported(exc):
            raise WorkflowError("REPORT_DIRECTORY_FSYNC_FAILED") from None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            raise WorkflowError("REPORT_DIRECTORY_CLOSE_FAILED") from None


def _directory_fsync_is_unsupported(exc: OSError) -> bool:
    unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}
    if hasattr(errno, "EOPNOTSUPP"):
        unsupported.add(errno.EOPNOTSUPP)
    if os.name == "nt":
        unsupported.add(errno.EACCES)
    return exc.errno in unsupported
