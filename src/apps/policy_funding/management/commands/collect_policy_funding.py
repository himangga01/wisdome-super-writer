"""Collect official funding notices or explicitly labeled offline parser fixtures."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.policy_funding.catalog import REGIONS
from apps.policy_funding.collectors import CollectionLimits, CollectionOutcome
from apps.policy_funding.parsing import (
    parse_bizinfo_detail,
    parse_bizinfo_list,
    parse_ols_detail,
    parse_ols_list,
)
from apps.policy_funding.storage import read_run
from apps.policy_funding.workflow import publish_collection, run_collection


class Command(BaseCommand):
    help = "Collect bounded official policy-funding notices and write validated Korean guides."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sources", nargs="+", choices=("semas_agency", "semas_direct", "bizinfo_finance")
        )
        parser.add_argument("--pages", type=int, default=1, choices=range(1, 6))
        parser.add_argument("--max-notices", type=int, default=20, choices=range(1, 101))
        parser.add_argument("--seconds", type=int, default=90, choices=range(1, 301))
        parser.add_argument(
            "--as-of", dest="as_of", help="KST run date, YYYY-MM-DD; does not change source dates"
        )
        parser.add_argument("--output", help="Independent policy output root")
        parser.add_argument(
            "--region",
            choices=(*REGIONS, "national", "unspecified"),
            help=(
                "Post-collection region filter;"
                " regional choices include "
                "national-institution notices"
            ),
        )
        parser.add_argument(
            "--fixture",
            action="store_true",
            help="Use only repository parser fixtures; never live acceptance",
        )

    def handle(self, *args, **options):
        try:
            day = (
                date.fromisoformat(options["as_of"])
                if options["as_of"]
                else datetime.now(ZoneInfo("Asia/Seoul")).date()
            )
            root = Path(options["output"] or settings.POLICY_FUNDING_ROOT)
            if options["fixture"]:
                chosen = tuple(options["sources"] or ("semas_agency", "bizinfo_finance"))
                if "semas_direct" in chosen:
                    raise ValueError(
                        "No direct-loan fixture is registered; use "
                        "a live collector or capture an approved "
                        "fixture"
                    )
                fixtures = settings.REPOSITORY_ROOT / "tests/fixtures/policy_funding"
                stamp = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                notices, outcomes = [], []
                for source in chosen:
                    if source == "semas_agency":
                        links, _ = parse_ols_list(
                            json.loads((fixtures / "ols-list.json").read_text(encoding="utf-8"))
                        )
                        rows = tuple(
                            parse_ols_detail(
                                (fixtures / f"ols-{link.external_id}.html").read_text(
                                    encoding="utf-8"
                                ),
                                link,
                                source_id=source,
                                fetched_at=stamp,
                            )
                            for link in links
                        )
                    else:
                        link = parse_bizinfo_list(
                            (fixtures / "bizinfo-list-N.html").read_text(encoding="utf-8")
                        )[0]
                        rows = (
                            parse_bizinfo_detail(
                                (fixtures / "bizinfo-detail.html").read_text(encoding="utf-8"),
                                link,
                                fetched_at=stamp,
                                listing_state="open_list",
                            ),
                        )
                    notices.extend(rows)
                    outcomes.append(CollectionOutcome(source, rows, "completed", (), 0, True))
                path = publish_collection(
                    root,
                    tuple(notices),
                    tuple(outcomes),
                    run_date=day,
                    mode="fixture",
                    region=options["region"],
                )
            else:
                path = run_collection(
                    root,
                    sources=tuple(
                        options["sources"] or ("semas_agency", "semas_direct", "bizinfo_finance")
                    ),
                    run_date=day,
                    region=options["region"],
                    limits=CollectionLimits(
                        options["pages"], options["max_notices"], options["seconds"]
                    ),
                )
            result = read_run(root, path.name)
        except (ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {
                    "run": str(path),
                    "mode": result["mode"],
                    "notice_count": len(result["notices"]),
                    "source_outcomes": result["source_outcomes"],
                    "changes": result["changes"],
                    "coverage_limited": True,
                    "selection": result.get("selection"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if any(row["status"] in {"partial", "failed"} for row in result["source_outcomes"]):
            raise CommandError(
                "Collection has source failures; the immutable run records the actual coverage"
            )
