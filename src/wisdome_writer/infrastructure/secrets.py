import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from django.core.exceptions import ImproperlyConfigured
from django.conf import settings
from django.utils.module_loading import import_string


@dataclass(frozen=True, slots=True)
class SecretRef:
    provider: str
    locator: str
    version: str = ""

    @classmethod
    def parse(cls, value: str) -> "SecretRef":
        if "://" not in value:
            return cls(provider="database", locator=value)
        provider, locator = value.split("://", 1)
        if not provider or not locator:
            raise ValueError("invalid secret reference")
        return cls(provider=provider, locator=locator)


@runtime_checkable
class SecretProvider(Protocol):
    def resolve(self, locator: str, *, version: str = "") -> str: ...


class EnvironmentSecretProvider:
    def resolve(self, locator: str, *, version: str = "") -> str:
        del version
        try:
            value = os.environ[locator]
        except KeyError as exc:
            raise ImproperlyConfigured(f"secret environment variable is not configured: {locator}") from exc
        if not value:
            raise ImproperlyConfigured(f"secret environment variable is empty: {locator}")
        return value


class SecretResolver:
    """Resolve secret references without exposing values in models, logs, or API responses."""

    def __init__(self, providers: dict[str, SecretProvider] | None = None):
        configured = {
            scheme: import_string(path)()
            for scheme, path in getattr(settings, "SECRET_PROVIDER_CLASSES", {}).items()
        }
        self.providers = {
            "env": EnvironmentSecretProvider(),
            **configured,
            **(providers or {}),
        }

    def resolve(self, reference: SecretRef | str | object) -> str:
        if isinstance(reference, str):
            reference = SecretRef.parse(reference)
        if isinstance(reference, SecretRef) and reference.provider == "database":
            from .models import SecretReference

            stored = SecretReference.objects.only("provider", "locator", "version", "is_active").get(
                code=reference.locator
            )
            if not stored.is_active:
                raise ImproperlyConfigured("secret reference is inactive")
            reference = SecretRef(stored.provider, stored.locator, stored.version)
        elif not isinstance(reference, SecretRef):
            reference = SecretRef(
                provider=str(getattr(reference, "provider")),
                locator=str(getattr(reference, "locator")),
                version=str(getattr(reference, "version", "")),
            )
        try:
            provider = self.providers[reference.provider]
        except KeyError as exc:
            raise ImproperlyConfigured(
                f"secret provider is not configured: {reference.provider}"
            ) from exc
        return provider.resolve(reference.locator, version=reference.version)
