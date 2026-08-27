from django.core.exceptions import ImproperlyConfigured


def validate_runtime_mode(environment: str, runtime_mode: str) -> None:
    """Reject unsupported and production-incompatible runtime selections."""
    if runtime_mode not in {"local", "distributed"}:
        raise ImproperlyConfigured(
            "WISDOME_RUNTIME_MODE must be 'local' or 'distributed'."
        )
    if environment == "production" and runtime_mode == "local":
        raise ImproperlyConfigured("The local runtime is development-only.")
