from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from apps.publishing.contracts import PublisherError
from wisdome_writer.infrastructure.secrets import (
    OAuthTokenBundle,
    OAuthTokenBundleError,
    normalize_oauth_token_bundle,
)


class BloggerOAuthClient:
    authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    revoke_endpoint = "https://oauth2.googleapis.com/revoke"
    scope = "https://www.googleapis.com/auth/blogger"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.client = client or httpx.Client(timeout=30.0, headers={"Accept": "application/json"})

    def authorization_url(self, *, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": self.scope,
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
                "state": state,
            }
        )
        return f"{self.authorization_endpoint}?{query}"

    def _token_response(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise PublisherError(
                "blogger_oauth_exchange_rejected",
                category="permanent",
                http_status=response.status_code,
                detail_redacted=f"HTTP {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublisherError(
                "blogger_oauth_token_invalid",
                category="permanent",
            ) from exc
        if not isinstance(payload, dict):
            raise PublisherError(
                "blogger_oauth_token_invalid",
                category="permanent",
            )
        return payload

    def exchange_code(self, code: str, *, version: str) -> OAuthTokenBundle:
        try:
            response = self.client.post(
                self.token_endpoint,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.redirect_uri,
                },
            )
        except httpx.HTTPError as exc:
            raise PublisherError(
                "blogger_oauth_exchange_unavailable",
                category="retryable",
                detail_redacted=exc.__class__.__name__,
            ) from exc
        try:
            return normalize_oauth_token_bundle(
                self._token_response(response),
                version=version,
                now=self.clock(),
            )
        except OAuthTokenBundleError as exc:
            raise PublisherError(
                "blogger_oauth_token_invalid",
                category="permanent",
            ) from exc

    def refresh_token(
        self,
        current: OAuthTokenBundle,
        *,
        version: str,
    ) -> OAuthTokenBundle:
        try:
            response = self.client.post(
                self.token_endpoint,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": current["refresh_token"],
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise PublisherError(
                "blogger_oauth_refresh_unavailable",
                category="retryable",
                detail_redacted=exc.__class__.__name__,
            ) from exc
        if response.status_code >= 400:
            try:
                error = response.json().get("error")
            except (ValueError, AttributeError):
                error = None
            raise PublisherError(
                (
                    "blogger_oauth_refresh_revoked"
                    if error == "invalid_grant"
                    else "blogger_oauth_refresh_rejected"
                ),
                category=("permanent" if error == "invalid_grant" else "retryable"),
                http_status=response.status_code,
                detail_redacted=f"HTTP {response.status_code}",
            )
        payload = self._token_response(response)
        payload.setdefault("scope", current["scope"])
        payload.setdefault("token_type", current["token_type"])
        try:
            return normalize_oauth_token_bundle(
                payload,
                version=version,
                now=self.clock(),
                previous_refresh_token=current["refresh_token"],
            )
        except OAuthTokenBundleError as exc:
            raise PublisherError(
                "blogger_oauth_token_invalid",
                category="permanent",
            ) from exc

    def close(self) -> None:
        self.client.close()

