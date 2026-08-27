"""Reconciliation helpers for normalized local housing source reports."""

from __future__ import annotations

import re
from collections.abc import Iterable

from apps.local_content.contracts import (
    CollectionWindow,
    HousingCollectionResult,
    HousingNotice,
    SourceRunReport,
)
from apps.local_content.selection import is_residential

_SHA256_CHECKSUM = re.compile(r"[0-9a-f]{64}")


def merge_source_reports(
    reports: Iterable[SourceRunReport], window: CollectionWindow
) -> HousingCollectionResult:
    """Select, deduplicate, and quarantine notices without changing source reports.

    A source identity is the pair of source key and official external ID. Exact
    replays (the same identity and checksum) collapse to one item. Multiple
    material checksums for an identity are all retained in ``conflicts`` and
    omitted from the weekly notice set. Blank or malformed checksums are also
    quarantined individually, never deduplicated.
    """

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
            grouped.setdefault(key, {}).setdefault(notice.source_checksum, notice)

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
