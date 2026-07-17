from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.evidence.models import ExtractionProfileSnapshot
from apps.evidence.profiles import load_profile_documents, profile_snapshot_values


class Command(BaseCommand):
    help = "Import immutable extraction profile files as draft snapshots."

    def add_arguments(self, parser):
        parser.add_argument("--root", default="config/extraction-profiles")

    def handle(self, *args, **options):
        root = Path(options["root"])
        if not root.is_absolute():
            root = settings.REPOSITORY_ROOT / root
        try:
            documents = load_profile_documents(root)
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc
        created = 0
        unchanged = 0
        for document in documents:
            try:
                values = profile_snapshot_values(document)
                existing = ExtractionProfileSnapshot.objects.filter(
                    profile_key=values["profile_key"], profile_version=values["profile_version"]
                ).first()
                if existing:
                    if existing.profile_material_hash != values["profile_material_hash"]:
                        raise CommandError(
                            f"Immutable profile changed without a new version: {existing.profile_key}"
                        )
                    unchanged += 1
                    continue
                profile = ExtractionProfileSnapshot(**values)
                profile.full_clean()
                profile.save()
                created += 1
            except (ValidationError, KeyError) as exc:
                raise CommandError(f"Invalid profile {document.get('_profile_file')}: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(f"profiles created={created} unchanged={unchanged}"))

