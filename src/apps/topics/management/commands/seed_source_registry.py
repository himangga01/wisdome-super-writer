import uuid
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

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
        parser.add_argument(
            "--version",
            choices=("initial",),
            default="initial",
            help="Repository manifest set to import.",
        )

    def handle(self, *args, **options):
        del args
        if options["version"] != "initial":
            raise ValueError("Only the initial source registry set exists.")
        audit_context = AuditContext.for_system(
            correlation_id=uuid.uuid4(),
            operation_key="source-registry-import",
            reason_code="source registry manifest import",
        )
        for path in sorted(Path(options["root"]).glob("*.json")):
            result = import_registry_manifest(
                path,
                audit_context=audit_context,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"{result.topic_code}: registry={result.registry_id} sources={result.source_count} created={result.created}"
                )
            )
