import uuid
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.audit.services import AuditContext
from apps.topics.services import import_registry_manifest


class Command(BaseCommand):
    help = "Import immutable draft source registries from config/source-registry."

    def add_arguments(self, parser):
        parser.add_argument(
            "--root",
            default=str(
                settings.BASE_DIR.parent / "config" / "source-registry"
            ),
        )

    def handle(self, *args, **options):
        del args
        audit_context = AuditContext.for_system(
            correlation_id=uuid.uuid4(),
            operation_key="source-registry-import",
            reason_code="source registry manifest import",
        )
        paths = sorted(Path(options["root"]).glob("*.json"))
        if not paths:
            raise CommandError(
                "No source registry manifests were found."
            )
        for path in paths:
            result = import_registry_manifest(
                path,
                audit_context=audit_context,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"{result.topic_code}: registry={result.registry_id} sources={result.source_count} created={result.created}"
                )
            )
