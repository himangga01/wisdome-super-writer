import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.topics.models import (
    SourceDefinitionSnapshot,
    SourceRegistrySnapshot,
    TopicRegistryHead,
)
from apps.topics.services import (
    SOURCE_SNAPSHOT_SCHEMA_V2,
    registry_manifest_hash,
)
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)


class Command(BaseCommand):
    help = (
        "Verify approved topic heads, immutable source snapshots and complete "
        "source registry manifests."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--root",
            default=str(
                settings.BASE_DIR.parent / "config" / "source-registry"
            ),
        )
        parser.add_argument(
            "--require-approved-mvp",
            action="store_true",
        )

    def handle(self, *args, **options):
        del args
        root = Path(options["root"])
        configured: dict[str, set[str]] = {}
        for path in sorted(root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                configured[str(payload["topicCode"])] = {
                    str(source["key"]) for source in payload["sources"]
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise CommandError(
                    f"Invalid source registry manifest: {path.name}"
                ) from exc
        if not configured:
            raise CommandError("No source registry manifests were found.")

        failures: list[str] = []
        for topic_code, configured_keys in configured.items():
            head = (
                TopicRegistryHead.objects.select_related(
                    "current_approved_registry"
                )
                .filter(topic_code=topic_code)
                .first()
            )
            if head is None or head.current_approved_registry is None:
                if options["require_approved_mvp"]:
                    failures.append(f"{topic_code}: approved head missing")
                continue
            registry = head.current_approved_registry
            if (
                registry.state
                != SourceRegistrySnapshot.State.APPROVED
                or registry.version != head.current_approved_version
                or registry.manifest_hash
                != head.current_approved_manifest_hash
            ):
                failures.append(f"{topic_code}: head projection mismatch")
                continue
            if registry_manifest_hash(registry) != registry.manifest_hash:
                failures.append(f"{topic_code}: registry manifest mismatch")
                continue

            members = list(
                registry.memberships.select_related(
                    "source_definition",
                    "source_snapshot",
                )
            )
            actual_keys = {
                member.source_definition.key for member in members
            }
            if actual_keys != configured_keys:
                failures.append(
                    f"{topic_code}: configured membership set mismatch"
                )
            for member in members:
                snapshot = member.source_snapshot
                if (
                    member.source_definition_id != snapshot.source_id
                    or snapshot.topic_code != topic_code
                ):
                    failures.append(
                        f"{topic_code}: membership identity mismatch"
                    )
                    continue
                if member.enabled and snapshot.state != (
                    SourceDefinitionSnapshot.State.APPROVED
                ):
                    failures.append(
                        f"{topic_code}: enabled snapshot is not approved"
                    )
                if (
                    snapshot.config.get("schemaVersion")
                    != SOURCE_SNAPSHOT_SCHEMA_V2
                ):
                    failures.append(
                        f"{topic_code}: legacy snapshot cannot be current"
                    )
                    continue
                actual_hash = canonical_hash(
                    snapshot.config,
                    schema_version=CANONICAL_HASH_SCHEMA_V1,
                )
                if actual_hash != snapshot.config_hash:
                    failures.append(
                        f"{topic_code}: source snapshot hash mismatch"
                    )

        if failures:
            raise CommandError("; ".join(sorted(set(failures))))
        self.stdout.write(
            self.style.SUCCESS(
                f"Verified {len(configured)} source registry topic(s)."
            )
        )
