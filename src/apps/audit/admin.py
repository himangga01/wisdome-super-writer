from django.contrib import admin

from .models import AuditEvent, RetentionBatch, RetentionBatchItem, RetentionHold


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "actor_type", "action", "entity_type", "entity_id")
    list_filter = ("actor_type", "action", "entity_type")
    search_fields = ("correlation_id", "entity_id", "actor__email")
    readonly_fields = tuple(field.name for field in AuditEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register([RetentionHold, RetentionBatch, RetentionBatchItem])
