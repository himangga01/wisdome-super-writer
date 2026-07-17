from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Bootstrap alias: verify profile files then imported profile snapshots."

    def add_arguments(self, parser):
        parser.add_argument("--root", default="config/extraction-profiles")
        parser.add_argument("--require-approved-mvp", action="store_true")

    def handle(self, *args, **options):
        call_command("verify_extraction_profile_files", root=options["root"])
        call_command(
            "verify_extraction_profile_snapshots",
            require_approved_mvp=options["require_approved_mvp"],
        )

