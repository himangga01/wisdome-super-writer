import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.topics.models import (
    SourceDefinitionSnapshot,
    SourceRegistrySnapshot,
    TopicCode,
    TopicPolicy,
    TopicRegistryHead,
)
from apps.topics.services import (
    SOURCE_SNAPSHOT_SCHEMA_V2,
    normalize_registry_import,
    registry_manifest_hash,
    source_snapshot_hash,
)
from wisdome_writer.domain.errors import DomainError
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
        configured: dict[str, dict] = {}
        for path in sorted(root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                normalized = normalize_registry_import(payload)
                topic_code = str(normalized["topic_code"])
                if topic_code in configured:
                    raise CommandError(
                        f"Duplicate source registry topic: {topic_code}"
                    )
                configured[topic_code] = normalized
            except (DomainError, KeyError, TypeError, ValueError) as exc:
                raise CommandError(
                    f"Invalid source registry manifest: {path.name}"
                ) from exc
        if not configured:
            raise CommandError("No source registry manifests were found.")

        failures: list[str] = []
        if options["require_approved_mvp"]:
            missing_topics = sorted(
                set(TopicCode.values) - set(configured)
            )
            for topic_code in missing_topics:
                failures.append(
                    f"{topic_code}: repository manifest missing"
                )
        for topic_code, configured_topic in configured.items():
            policy = TopicPolicy.objects.filter(
                code=topic_code,
                version=configured_topic["policy_version"],
            ).first()
            expected_policy_hash = canonical_hash(
                configured_topic["policy"],
                schema_version=CANONICAL_HASH_SCHEMA_V1,
            )
            if policy is None:
                failures.append(
                    f"{topic_code}: repository topic policy missing"
                )
            elif any(
                (
                    not policy.active,
                    policy.title != configured_topic["title"],
                    policy.freshness_minutes
                    != configured_topic["freshness_minutes"],
                    policy.policy != configured_topic["policy"],
                    policy.policy_hash != expected_policy_hash,
                )
            ):
                failures.append(
                    f"{topic_code}: repository topic policy mismatch"
                )
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
            expected_sources = {
                item["key"]: {
                    "config_hash": source_snapshot_hash(
                        item["material"]
                    ),
                    "enabled": bool(item["material"]["enabled"]),
                    "display_order": display_order,
                }
                for display_order, item in enumerate(
                    configured_topic["sources"]
                )
            }
            configured_keys = set(expected_sources)
            actual_keys = {
                member.source_definition.key for member in members
            }
            if actual_keys != configured_keys:
                failures.append(
                    f"{topic_code}: configured membership set mismatch"
                )
            for member in members:
                snapshot = member.source_snapshot
                key = member.source_definition.key
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
                expected = expected_sources.get(key)
                if expected is None:
                    continue
                if snapshot.config_hash != expected["config_hash"]:
                    failures.append(
                        f"{topic_code}/{key}: repository material hash mismatch"
                    )
                if member.enabled != expected["enabled"]:
                    failures.append(
                        f"{topic_code}/{key}: repository enabled flag mismatch"
                    )
                if member.display_order != expected["display_order"]:
                    failures.append(
                        f"{topic_code}/{key}: repository display order mismatch"
                    )
                if (
                    snapshot.frozen_config.get("schemaVersion")
                    != SOURCE_SNAPSHOT_SCHEMA_V2
                ):
                    failures.append(
                        f"{topic_code}: legacy snapshot cannot be current"
                    )
                    continue
                actual_hash = canonical_hash(
                    snapshot.frozen_config,
                    schema_version=CANONICAL_HASH_SCHEMA_V1,
                )
                if (
                    actual_hash != snapshot.frozen_config_hash
                    or snapshot.frozen_config_hash
                    != snapshot.config_hash
                ):
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
