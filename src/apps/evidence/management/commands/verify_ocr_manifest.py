from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.evidence.models import ExtractionEngine, ExtractionProfileSnapshot
from apps.evidence.profiles import verify_local_profile


class Command(BaseCommand):
    help = "Verify PaddleOCR 3.7.0, PaddlePaddle 3.x and every local model checksum."

    def add_arguments(self, parser):
        parser.add_argument("--profiles", default="config/extraction-profiles/paddleocr")

    def handle(self, *args, **options):
        profiles = ExtractionProfileSnapshot.objects.filter(engine=ExtractionEngine.PADDLEOCR)
        if not profiles.exists():
            raise CommandError("No PaddleOCR profile snapshot has been imported")
        failed = []
        for profile in profiles:
            report = verify_local_profile(profile)
            if report["overallResult"] != "passed":
                failed.append(profile.profile_key)
        if failed:
            raise CommandError(f"PaddleOCR profile verification failed: {', '.join(failed)}")
        self.stdout.write(self.style.SUCCESS(f"verified {profiles.count()} PaddleOCR profiles"))

