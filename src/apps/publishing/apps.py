from django.apps import AppConfig

from wisdome_writer.external_publishing import external_publishing_boundary

_SERVICE_BOUNDARIES = (
    "_assert_external_writes_allowed",
    "complete_blogger_oauth",
    "create_auto_publish_validation",
    "create_canary_run",
    "create_publication_intent",
    "create_target",
    "decide_approval",
    "decide_auto_publish_validation",
    "disconnect_target",
    "dispatch_publication",
    "prepare_public_delivery",
    "prepare_wordpress_media",
    "publisher_for_target",
    "request_target_preflight",
    "retry_publication_attempt",
    "set_auto_publish",
    "start_blogger_oauth",
    "update_target",
)
_TASK_BOUNDARIES = (
    "delete_public_delivery_asset",
    "dispatch_scheduled_run_publication",
    "execute_media_delivery_operation",
    "execute_publication_attempt",
    "reconcile_publication_attempt",
    "reconcile_remote_media",
    "revoke_target_credentials",
    "run_target_canary",
    "run_target_preflight_task",
    "schedule_orphan_media_cleanup",
)


class PublishingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.publishing"
    verbose_name = "발행"

    def ready(self) -> None:
        from . import services, tasks

        for name in _SERVICE_BOUNDARIES:
            function = getattr(services, name)
            if not getattr(function, "_external_publishing_boundary", False):
                guarded = external_publishing_boundary(function)
                if hasattr(function, "__wrapped__"):
                    guarded.__wrapped__ = function.__wrapped__
                guarded._external_publishing_boundary = True  # type: ignore[attr-defined]
                setattr(services, name, guarded)
        for name in _TASK_BOUNDARIES:
            task = getattr(tasks, name)
            function = task.run
            if not getattr(function, "_external_publishing_boundary", False):
                guarded = external_publishing_boundary(function)
                guarded._external_publishing_boundary = True  # type: ignore[attr-defined]
                task.run = guarded

