from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.evidence.profiles import load_profile_documents, profile_snapshot_values


class Command(BaseCommand):
    help = "Verify PaddleOCR 3.7.0, PaddlePaddle 3.x and every local model checksum."

    def add_arguments(self, parser):
        parser.add_argument("--profiles", default="config/extraction-profiles/paddleocr")

    def handle(self, *args, **options):
        root = Path(options["profiles"])
        if not root.is_absolute():
            root = settings.REPOSITORY_ROOT / root
        try:
            documents = load_profile_documents(root)
            values = [profile_snapshot_values(document) for document in documents]
        except (KeyError, ValidationError) as exc:
            raise CommandError(str(exc)) from exc
        if not values:
            raise CommandError("No PaddleOCR profile documents were found")
        self.stdout.write(self.style.SUCCESS(f"verified {len(values)} PaddleOCR profiles"))

