"""Immutable collection publication and semantic change summaries."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from apps.local_content.bundles import ArticleBundle, ArticleBundleWriter, BundleFile
from apps.policy_funding.catalog import REGIONS
from apps.policy_funding.collectors import CollectionLimits, CollectionOutcome, collect_source
from apps.policy_funding.contracts import FundingNotice, canonical_json, content_hash
from apps.policy_funding.rendering import render_guide, render_notice
from apps.policy_funding.storage import latest_notices, read_run
from apps.policy_funding.verification import verify_provided_case


def publish_collection(
    root: Path,
    notices: tuple[FundingNotice, ...],
    outcomes: tuple[CollectionOutcome, ...],
    *,
    run_date: date,
    mode: str,
    region: str | None = None,
) -> Path:
    if mode not in {"live", "fixture"} or len(notices) > 300:
        raise ValueError("Invalid policy run mode or size")
    if mode == "fixture":
        notices = tuple(replace(row, provenance="fixture") for row in notices)
    elif any(row.provenance != "official_fetch" for row in notices):
        raise ValueError("Fixture notice cannot be published as a live observation")
    if len({row.stable_id for row in notices}) != len(notices):
        raise ValueError("Duplicate policy notice identity")
    if region is not None:
        if region not in {"national", "unspecified", *REGIONS}:
            raise ValueError("Unknown policy region selection")
        notices = tuple(
            row
            for row in notices
            if region in row.regions or region in REGIONS and "national" in row.regions
        )
    previous = {row.stable_id: row for row in latest_notices(root, mode=mode)}
    changes = []
    for row in notices:
        old = previous.get(row.stable_id)
        status = (
            "added"
            if old is None
            else "unchanged"
            if old.content_hash == row.content_hash
            else "changed"
        )
        changed_fields = (
            []
            if old is None
            else [
                key
                for key, value in row.to_dict().items()
                if key not in {"fetched_at", "provenance"} and old.to_dict()[key] != value
            ]
        )
        changes.append({"notice_id": row.stable_id, "status": status, "fields": changed_fields})
    writer = ArticleBundleWriter(root)
    lease = writer.reserve_run_directory(run_date, workflow_id=uuid.uuid4().hex)
    records = {
        "schema_version": 1,
        "topic": "policy_funding",
        "run_name": lease.name,
        "mode": mode,
        "notices": [row.to_dict() for row in notices],
    }
    observations = {
        "schema_version": 1,
        "run_name": lease.name,
        "run_date": run_date.isoformat(),
        "mode": mode,
        "captured_at": datetime.now(UTC).isoformat(),
        "records_hash": content_hash(records),
        "source_outcomes": [
            {
                "source_id": row.source_id,
                "status": row.status,
                "errors": list(row.errors),
                "request_count": row.request_count,
                "count": len(row.notices),
                "coverage_limited": True,
            }
            for row in outcomes
        ],
        "changes": changes,
        "case_verification": verify_provided_case(notices),
        "coverage_limited": True,
        "selection": {"region": region, "include_national": region in REGIONS},
    }
    bundles = [
        ArticleBundle(
            run_date,
            "beginner-guide",
            (BundleFile.text("article.md", render_guide(), "text/markdown"),),
        )
    ]
    for row in notices:
        bundles.append(
            ArticleBundle(
                run_date,
                "funding-" + row.stable_id,
                (
                    BundleFile.text("article.md", render_notice(row), "text/markdown"),
                    BundleFile.text(
                        "verification.json",
                        canonical_json(
                            {
                                "topic": "policy_funding",
                                "notice_id": row.stable_id,
                                "content_hash": row.content_hash,
                                "mode": mode,
                                "source_url": row.official_url,
                                "loan_approval": False,
                            }
                        ),
                        "application/json",
                    ),
                ),
            )
        )
    index = [
        "# 정책자금 수집 결과",
        "",
        f"기준일: {run_date.isoformat()} · 실행 유형: {mode}",
        "",
        (
            "정해진 출처·페이지 범위의 수집 결과입니다. 전국 모든"
            " 공고나 현재 예산 잔액을 보장하지 않습니다."
        ),
        "",
        "[처음 배우는 정책자금](./beginner-guide/article.md)",
        "",
    ]
    index.extend(
        f"- [{row.title.replace('[', '').replace(']', '')}](./funding-{row.stable_id}/article.md)"
        for row in notices
    )
    root_path = writer.write_run(
        run_date,
        "\n".join(index) + "\n",
        bundles,
        run_directory=lease,
        run_files={
            "notices.json": canonical_json(records),
            "raw-observations.json": canonical_json(observations),
        },
    )
    read_run(root, lease.name)
    return root_path


def run_collection(
    root: Path,
    *,
    sources: tuple[str, ...],
    limits: CollectionLimits,
    run_date: date,
    fetchers: dict | None = None,
    mode: str = "live",
    region: str | None = None,
) -> Path:
    if not sources or len(sources) > 3 or len(set(sources)) != len(sources):
        raise ValueError("Choose one to three distinct collection sources")
    if fetchers is not None and mode != "fixture":
        raise ValueError("Injected collector transport must use fixture mode")
    outcomes = tuple(
        collect_source(source, limits, fetcher=(fetchers or {}).get(source)) for source in sources
    )
    notices = tuple(notice for outcome in outcomes for notice in outcome.notices)
    return publish_collection(root, notices, outcomes, run_date=run_date, mode=mode, region=region)
