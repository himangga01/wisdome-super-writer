from django.contrib import admin

from .models import (
    Approval,
    ArticleChannelRender,
    AutoPublishActivation,
    AutoPublishValidation,
    AutoPublishValidationDecision,
    Publication,
    PublicationAttempt,
    PublicationIntent,
    PublicationMedia,
    PublicationTarget,
    PublicationTargetSnapshot,
    PublicDeliveryAsset,
    RemoteMedia,
    TargetCanaryRun,
    TargetDisconnectDecision,
)


class ReadOnlyPublishingAdmin(admin.ModelAdmin):
    """Keep Django admin as an inspection surface; mutations use audited services."""

    actions = None

    def get_readonly_fields(self, request, obj=None):
        return tuple(
            field.name
            for field in (*self.model._meta.fields, *self.model._meta.many_to_many)
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PublicationTarget)
class PublicationTargetAdmin(ReadOnlyPublishingAdmin):
    list_display = (
        "display_name",
        "channel",
        "environment",
        "connection_state",
        "preflight_state",
        "canary_state",
        "pilot_state",
        "auto_publish_enabled",
    )
    list_filter = ("channel", "environment", "connection_state", "auto_publish_enabled")
    search_fields = ("display_name", "base_url", "remote_blog_id")
    readonly_fields = (
        "current_snapshot_id",
        "current_snapshot_version",
        "current_config_hash",
        "publisher_adapter_manifest_hash",
        "latest_auto_publish_activation_id",
    )


@admin.register(PublicationIntent)
class PublicationIntentAdmin(ReadOnlyPublishingAdmin):
    list_display = ("id", "article_id", "revision_no", "approval_mode", "state", "created_at")
    list_filter = ("approval_mode", "state")
    readonly_fields = ("intent_hash", "target_snapshot_manifest_hash", "created_at")


@admin.register(Publication)
class PublicationAdmin(ReadOnlyPublishingAdmin):
    list_display = ("id", "article_id", "target", "state", "remote_state", "published_at")
    list_filter = ("state", "remote_state", "target__channel")
    search_fields = ("remote_post_id", "remote_url", "remote_lookup_key")


@admin.register(PublicationAttempt)
class PublicationAttemptAdmin(ReadOnlyPublishingAdmin):
    list_display = ("id", "publication", "resolved_action", "state", "attempt_no", "created_at")
    list_filter = ("resolved_action", "state")
    readonly_fields = ("idempotency_key", "request_fingerprint", "error_detail_redacted")


for model in (
    PublicationTargetSnapshot,
    TargetCanaryRun,
    AutoPublishValidation,
    AutoPublishValidationDecision,
    AutoPublishActivation,
    ArticleChannelRender,
    Approval,
    RemoteMedia,
    PublicDeliveryAsset,
    PublicationMedia,
    TargetDisconnectDecision,
):
    admin.site.register(model, ReadOnlyPublishingAdmin)
