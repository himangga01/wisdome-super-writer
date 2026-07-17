from django.apps import AppConfig


class InfrastructureConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "wisdome_writer.infrastructure"
    label = "infrastructure"
    verbose_name = "공통 인프라"

