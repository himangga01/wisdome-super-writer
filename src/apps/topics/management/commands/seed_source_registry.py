from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.topics.services import import_registry_manifest


class Command(BaseCommand):
    help = "Import immutable draft source registries from config/source-registry."

    def add_arguments(self, parser):
        parser.add_argument("--root", default=str(settings.BASE_DIR.parent / "config" / "source-registry"))

    def handle(self, *args, **options):
        for path in sorted(Path(options["root"]).glob("*.json")):
            result = import_registry_manifest(path)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{result.topic_code}: registry={result.registry_id} sources={result.source_count} created={result.created}"
                )
            )
