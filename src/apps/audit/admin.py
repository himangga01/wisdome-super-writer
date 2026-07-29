import json

from django.contrib import admin

from .models import AuditEvent, RetentionBatch, RetentionBatchItem, RetentionHold
from .redaction import (
    AuditRedactionError,
    sanitize_reason,
    validate_stored_metadata,
)


REDACTION_VALIDATION_FAILED = "redaction_validation_failed"


def _audit_event_admin_fields() -> tuple[str, ...]:
    fields: list[str] = []
    for field in AuditEvent._meta.fields:
        if field.name == "metadata_redacted":
            fields.append("validated_metadata")
        elif field.name == "reason_code":
            fields.append("validated_reason")
        else:
            fields.append(field.name)
    return tuple(fields)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "actor_type", "action", "entity_type", "entity_id")
    list_filter = ("actor_type", "action", "entity_type")
    search_fields = ("correlation_id", "entity_id", "actor__email")
    fields = _audit_event_admin_fields()
    readonly_fields = fields

    @admin.display(description="Validated reason")
    def validated_reason(self, obj):
        try:
            return sanitize_reason(obj.reason_code)
        except AuditRedactionError:
            return REDACTION_VALIDATION_FAILED

    @admin.display(description="Validated metadata")
    def validated_metadata(self, obj):
        try:
            metadata = validate_stored_metadata(
                action=obj.action,
                metadata_schema_version=obj.metadata_schema_version,
                redaction_policy_version=obj.redaction_policy_version,
                redaction_policy_hash_value=obj.redaction_policy_hash,
                metadata=obj.metadata_redacted,
            )
        except AuditRedactionError:
            return REDACTION_VALIDATION_FAILED
        return json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register([RetentionHold, RetentionBatch, RetentionBatchItem])
