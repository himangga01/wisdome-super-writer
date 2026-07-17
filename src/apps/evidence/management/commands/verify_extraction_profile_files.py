import json
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from adapters.extractors.base import canonical_bytes
from adapters.storage import S3ObjectStorage
from apps.evidence.models import ExtractionProfileSnapshot
from apps.evidence.profiles import load_profile_documents, profile_snapshot_values, verify_local_profile
from apps.evidence.services import canonical_hash


class Command(BaseCommand):
    help = "Verify profile JSON, implementation files and deterministic material hashes."

    def add_arguments(self, parser):
        parser.add_argument("--root", default="config/extraction-profiles")

    def handle(self, *args, **options):
        root = Path(options["root"])
        if not root.is_absolute():
            root = settings.REPOSITORY_ROOT / root
        try:
            documents = load_profile_documents(root)
            results = []
            storage = S3ObjectStorage()
            for document in documents:
                values = profile_snapshot_values(document)
                result = {
                    "profileKey": document["profile_key"],
                    "profileVersion": str(document["profile_version"]),
                    "materialHash": values["profile_material_hash"],
                    "result": "passed",
                }
                profile = ExtractionProfileSnapshot.objects.filter(
                    profile_key=document["profile_key"], profile_version=str(document["profile_version"])
                ).first()
                if profile:
                    report = verify_local_profile(profile)
                    now = timezone.now().isoformat()
                    report.update({"generatedAt": now, "verifiedAt": now})
                    report_hash = canonical_hash(report)
                    key = f"evidence/verification-reports/extraction-profile/{profile.id}/{report_hash}.json"
                    report.update({
                        "reportObjectKey": key,
                        "reportObjectVersion": report_hash,
                        "reportHash": report_hash,
                    })
                    info = storage.put_bytes(
                        key=key,
                        data=canonical_bytes(report),
                        content_type="application/json",
                        metadata={"subject_material_hash": profile.profile_material_hash, "report_hash": report_hash},
                    )
                    profile.verification_report_object_key = key
                    profile.verification_report_object_version = report_hash
                    profile.verification_report_hash = report_hash
                    profile.save(update_fields=(
                        "verification_report_object_key", "verification_report_object_version",
                        "verification_report_hash", "updated_at",
                    ))
                    result["result"] = report["overallResult"]
                    result["reportHash"] = report_hash
                results.append(result)
        except (ValidationError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(results, ensure_ascii=False, indent=2))
        self.stdout.write(self.style.SUCCESS(f"verified {len(results)} profile files"))
