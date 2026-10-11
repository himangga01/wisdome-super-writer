"""Explicit local-only official semiconductor metadata collection."""

import json
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from wisdome_writer.portal.news_archive import SEOUL, collect_news_archive, read_news_archive


class Command(BaseCommand):
    help = "Collect bounded official semiconductor metadata into a local immutable archive."

    def add_arguments(self, parser):
        now = datetime.now(SEOUL)
        parser.add_argument("--year", type=int, default=now.year)
        parser.add_argument("--month", type=int, default=now.month)
        parser.add_argument("--max-items", type=int, default=60)
        parser.add_argument("--seconds", type=int, default=180)

    def handle(self, *args, **options):
        if not settings.IS_LOCAL_RUNTIME:
            raise CommandError("collect_semiconductor_news requires local runtime")
        root = getattr(
            settings,
            "SEMICONDUCTOR_ARCHIVE_ROOT",
            settings.REPOSITORY_ROOT / "output/semiconductor",
        )
        try:
            path = collect_news_archive(
                root,
                options["year"],
                options["month"],
                max_items=options["max_items"],
                max_seconds=options["seconds"],
            )
            saved = read_news_archive(root, path.name)
        except (ValueError, OSError) as exc:
            code = getattr(exc, "code", "invalid_archive")
            raise CommandError(f"semiconductor metadata collection failed: {code}") from None
        self.stdout.write(
            json.dumps(
                {
                    "run_name": path.name,
                    "mode": saved["mode"],
                    "count": len(saved["records"]),
                    "coverage_limited": True,
                    "window": saved["window"],
                    "source_outcomes": saved["source_outcomes"],
                },
                ensure_ascii=False,
            )
        )
        if not any(row["status"] in {"completed", "partial"} for row in saved["source_outcomes"]):
            raise CommandError("No supported official source completed; failure archive was saved")
