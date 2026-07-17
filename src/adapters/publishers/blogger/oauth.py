from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from apps.publishing.contracts import PublisherError


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
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
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

    def exchange_code(self, code: str) -> dict[str, Any]:
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
        if response.status_code >= 400:
            raise PublisherError(
                "blogger_oauth_exchange_rejected",
                category="permanent",
                http_status=response.status_code,
                detail_redacted=f"HTTP {response.status_code}",
            )
        payload = response.json()
        if payload.get("token_type", "").lower() != "bearer" or not payload.get("access_token"):
            raise PublisherError("blogger_oauth_token_invalid", category="permanent")
        granted = set(str(payload.get("scope", "")).split())
        if granted and self.scope not in granted:
            raise PublisherError("blogger_oauth_scope_missing", category="permanent")
        return payload

    def close(self) -> None:
        self.client.close()

