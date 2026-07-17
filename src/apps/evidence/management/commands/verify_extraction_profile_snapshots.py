from django.core.management.base import BaseCommand, CommandError

from apps.evidence.models import ExtractionProfileSnapshot, ProfileApprovalState
from apps.evidence.profiles import MVP_PROFILE_KEYS


class Command(BaseCommand):
    help = "Verify imported profile snapshots and optional approved MVP coverage."

    def add_arguments(self, parser):
        parser.add_argument("--require-approved-mvp", action="store_true")

    def handle(self, *args, **options):
        snapshots = list(ExtractionProfileSnapshot.objects.all())
        by_key = {profile.profile_key: profile for profile in snapshots}
        missing = sorted(MVP_PROFILE_KEYS - set(by_key))
        if missing:
            raise CommandError(f"Missing MVP extraction profiles: {', '.join(missing)}")
        if options["require_approved_mvp"]:
            inactive = sorted(
                key for key in MVP_PROFILE_KEYS
                if by_key[key].approval_state != ProfileApprovalState.APPROVED
            )
            if inactive:
                raise CommandError(f"MVP profiles are not approved: {', '.join(inactive)}")
        self.stdout.write(self.style.SUCCESS(f"verified {len(snapshots)} extraction profile snapshots"))

