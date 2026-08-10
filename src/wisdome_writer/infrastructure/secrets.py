import os
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, TypedDict, runtime_checkable

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


class OAuthTokenBundle(TypedDict):
    access_token: str
    refresh_token: str
    token_type: str
    scope: list[str]
    expires_at: str
    version: str


class OAuthTokenBundleError(ValueError):
    """A secret provider returned an unusable OAuth token bundle."""


def _utc_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise OAuthTokenBundleError("OAuth token expiry is missing")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise OAuthTokenBundleError("OAuth token expiry is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OAuthTokenBundleError("OAuth token expiry must include a timezone")
    return parsed.astimezone(timezone.utc)


def normalize_oauth_token_bundle(
    payload: dict[str, Any],
    *,
    version: str | None = None,
    now: datetime | None = None,
    previous_refresh_token: str | None = None,
) -> OAuthTokenBundle:
    if not isinstance(payload, dict):
        raise OAuthTokenBundleError("OAuth token bundle must be an object")
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token") or previous_refresh_token
    token_type = payload.get("token_type")
    if (
        not isinstance(access_token, str)
        or not access_token
        or len(access_token) > 4096
        or not isinstance(refresh_token, str)
        or not refresh_token
        or len(refresh_token) > 2048
        or not isinstance(token_type, str)
        or token_type.lower() != "bearer"
    ):
        raise OAuthTokenBundleError("OAuth token material is incomplete")
    raw_scope = payload.get("scope")
    if isinstance(raw_scope, str):
        scopes = raw_scope.split()
    elif isinstance(raw_scope, list) and all(
        isinstance(item, str) and item for item in raw_scope
    ):
        scopes = list(raw_scope)
    else:
        raise OAuthTokenBundleError("OAuth token scope is missing")
    scopes = list(dict.fromkeys(scopes))
    blogger_scope = "https://www.googleapis.com/auth/blogger"
    if blogger_scope not in scopes:
        raise OAuthTokenBundleError("Blogger OAuth scope is missing")
    effective_version = version if version is not None else payload.get("version")
    if (
        not isinstance(effective_version, str)
        or not effective_version
        or len(effective_version) > 120
    ):
        raise OAuthTokenBundleError("OAuth token version is missing")
    if "expires_in" in payload:
        expires_in = payload.get("expires_in")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in <= 0
            or expires_in > 604800
        ):
            raise OAuthTokenBundleError("OAuth token lifetime is invalid")
        base = now or datetime.now(timezone.utc)
        if base.tzinfo is None or base.utcoffset() is None:
            raise OAuthTokenBundleError("OAuth token clock must be timezone-aware")
        expires = base.astimezone(timezone.utc) + timedelta(seconds=expires_in)
    else:
        expires = _utc_datetime(payload.get("expires_at"))
    expires_at = expires.isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "scope": scopes,
        "expires_at": expires_at,
        "version": effective_version,
    }


@runtime_checkable
class SecretProvider(Protocol):
    def resolve(self, locator: str, *, version: str = "") -> Any: ...


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

    def _coerce_reference(self, reference: SecretRef | str | object) -> SecretRef:
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
        return reference

    def resolve(self, reference: SecretRef | str | object) -> Any:
        reference = self._coerce_reference(reference)
        try:
            provider = self.providers[reference.provider]
        except KeyError as exc:
            raise ImproperlyConfigured(
                f"secret provider is not configured: {reference.provider}"
            ) from exc
        return provider.resolve(reference.locator, version=reference.version)

    def resolve_oauth_token_bundle(
        self,
        reference: SecretRef | str | object,
    ) -> OAuthTokenBundle:
        resolved_ref = self._coerce_reference(reference)
        raw = self.resolve(resolved_ref)
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise OAuthTokenBundleError(
                    "OAuth token secret is not valid JSON"
                ) from exc
        elif isinstance(raw, dict):
            payload = raw
        else:
            raise OAuthTokenBundleError("OAuth token secret is not an object")
        bundle = normalize_oauth_token_bundle(payload)
        if resolved_ref.version and bundle["version"] != resolved_ref.version:
            raise OAuthTokenBundleError(
                "OAuth token secret version differs from its reference"
            )
        return bundle
