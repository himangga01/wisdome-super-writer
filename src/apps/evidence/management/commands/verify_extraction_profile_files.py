import json
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from adapters.extractors.base import canonical_bytes
from adapters.storage import S3ObjectStorage
from apps.evidence.models import (
    ExtractionProfileSnapshot,
    ProfileApprovalState,
)
from apps.evidence.profiles import load_profile_documents, profile_snapshot_values, verify_local_profile
from apps.evidence.services import _validate_profile_report_bytes, canonical_hash


def _persist_verification_report(*, profile, report, storage) -> str:
    """Store immutable core bytes; the DB row is the object-version envelope."""
    if profile.approval_state != ProfileApprovalState.DRAFT:
        if profile.approval_state not in {
            ProfileApprovalState.APPROVED,
            ProfileApprovalState.RETIRED,
        } or not (
            profile.verification_report_object_key
            and profile.verification_report_object_version
            and profile.verification_report_hash
        ):
            raise ValidationError(
                "A non-draft profile requires its existing frozen report envelope"
            )
        stored_version = profile.verification_report_object_version
        try:
            raw = storage.get_bytes(
                key=profile.verification_report_object_key,
                version_id=(
                    None if stored_version.startswith("etag:") else stored_version
                ),
            )
        except Exception as exc:
            raise ValidationError(
                "The frozen profile verification report is unavailable"
            ) from exc
        _validate_profile_report_bytes(profile, raw)
        return profile.verification_report_hash

    report_hash = canonical_hash(report)
    key = (
        "evidence/verification-reports/extraction-profile/"
        f"{profile.id}/{report_hash}.json"
    )
    info = storage.put_bytes(
        key=key,
        data=canonical_bytes(report),
        content_type="application/json",
        checksum_sha256=report_hash,
        metadata={
            "subject_material_hash": profile.profile_material_hash,
            "report_hash": report_hash,
        },
    )
    object_version = info.version_id or (
        f"etag:{info.etag}" if info.etag else None
    )
    if not object_version:
        raise ValidationError(
            "Verification report storage returned neither VersionId nor ETag"
        )
    profile.verification_report_object_key = info.key
    profile.verification_report_object_version = object_version
    profile.verification_report_hash = report_hash
    profile.save(
        update_fields=(
            "verification_report_object_key",
            "verification_report_object_version",
            "verification_report_hash",
            "updated_at",
        )
    )
    return report_hash


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
                    report_hash = _persist_verification_report(
                        profile=profile,
                        report=report,
                        storage=storage,
                    )
                    result["result"] = report["overallResult"]
                    result["reportHash"] = report_hash
                results.append(result)
        except (ValidationError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(results, ensure_ascii=False, indent=2))
        self.stdout.write(self.style.SUCCESS(f"verified {len(results)} profile files"))
