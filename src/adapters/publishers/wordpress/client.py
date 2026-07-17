from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from apps.publishing.contracts import (
    PreflightResult,
    PublishCommand,
    PublisherCapabilities,
    PublisherError,
    PublishResult,
)


class WordPressPublisher:
    """WordPress Core REST adapter using HTTPS Application Password auth."""

    capabilities = PublisherCapabilities(
        create=True,
        update=True,
        unpublish=True,
        mark_withdrawn=True,
        draft=True,
        schedule=True,
        media_upload=True,
        supported_media_types=("image/jpeg", "image/png", "image/webp", "image/gif"),
    )

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        application_password: str,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
        write_guard: Callable[[], None] | None = None,
    ):
        if not base_url.lower().startswith("https://"):
            raise ValueError("WordPress Application Password 연결에는 HTTPS가 필요합니다.")
        self.site_url = base_url.rstrip("/")
        self.api_url = f"{self.site_url}/wp-json/wp/v2"
        self.timeout_seconds = timeout_seconds
        self.write_guard = write_guard or (lambda: None)
        self.client = client or httpx.Client(
            auth=(username, application_password),
            timeout=timeout_seconds,
            headers={"Accept": "application/json", "User-Agent": "WisdomeWriter/1.0"},
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, *, write: bool = False, **kwargs: Any) -> httpx.Response:
        if write:
            self.write_guard()
        try:
            response = self.client.request(method, f"{self.api_url}{path}", **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise PublisherError(
                "wordpress_unknown_outcome" if write else "wordpress_unavailable",
                category="unknown_outcome" if write else "retryable",
                detail_redacted=exc.__class__.__name__,
            ) from exc
        if response.status_code < 400:
            return response
        if response.status_code in (401, 403):
            category, code = "permanent", "wordpress_auth_or_capability_denied"
        elif response.status_code == 429 or response.status_code >= 500:
            category, code = "retryable", "wordpress_temporarily_unavailable"
        else:
            category, code = "permanent", "wordpress_request_rejected"
        retry_after = response.headers.get("Retry-After")
        raise PublisherError(
            code,
            category=category,
            http_status=response.status_code,
            retry_after_seconds=int(retry_after) if retry_after and retry_after.isdigit() else None,
            detail_redacted=f"HTTP {response.status_code}",
        )

    def preflight_connection(self) -> PreflightResult:
        checks: list[dict[str, Any]] = [{"code": "https", "passed": True}]
        try:
            user_response = self._request("GET", "/users/me", params={"context": "edit"})
            user = user_response.json()
            checks.append({"code": "authenticated_user", "passed": bool(user.get("id"))})
            for route in ("/posts", "/media"):
                self._request("GET", route, params={"context": "edit", "per_page": 1})
                checks.append({"code": f"route:{route}", "passed": True})
            return PreflightResult(
                passed=all(item["passed"] for item in checks),
                remote_identity=str(user.get("id")) if user.get("id") else None,
                remote_url=self.site_url,
                capabilities=self.capabilities,
                checks=tuple(checks),
            )
        except PublisherError as exc:
            checks.append({"code": exc.code, "passed": False})
            return PreflightResult(
                passed=False,
                remote_identity=None,
                remote_url=self.site_url,
                capabilities=self.capabilities,
                checks=tuple(checks),
                error_code=exc.code,
            )

    def execute(self, command: PublishCommand) -> PublishResult:
        if command.action in {"create", "update", "mark_withdrawn"} and not command.rendered_article:
            raise PublisherError("render_required", category="permanent")
        if command.action != "create" and not command.remote_post_id:
            raise PublisherError("remote_post_id_required", category="permanent")

        if command.action == "create":
            existing = self.reconcile(command)
            if existing.status == "succeeded":
                return existing
            if existing.error_code == "remote_match_not_unique":
                return existing
            article = command.rendered_article
            assert article is not None
            payload = {
                "slug": command.remote_lookup_key,
                "title": article.title,
                "content": article.body_html,
                "status": "publish",
            }
            response = self._request("POST", "/posts", write=True, json=payload)
            return self._post_result(response)

        if command.action in {"update", "mark_withdrawn"}:
            article = command.rendered_article
            assert article is not None
            response = self._request(
                "POST",
                f"/posts/{command.remote_post_id}",
                write=True,
                json={"title": article.title, "content": article.body_html},
            )
            return self._post_result(response)

        if command.action == "unpublish":
            response = self._request(
                "POST",
                f"/posts/{command.remote_post_id}",
                write=True,
                json={"status": "draft"},
            )
            result = self._post_result(response)
            return PublishResult(
                status=result.status,
                remote_post_id=result.remote_post_id,
                remote_url=result.remote_url,
                remote_state="withdrawn",
                remote_revision=result.remote_revision,
                request_id=result.request_id,
                http_status=result.http_status,
            )
        raise PublisherError("unsupported_action", category="permanent")

    def reconcile(self, command: PublishCommand) -> PublishResult:
        response = self._request(
            "GET",
            "/posts",
            params={
                "slug": command.remote_lookup_key,
                "status": "draft,publish,future,pending,private",
                "context": "edit",
                "per_page": 10,
            },
        )
        matches = response.json()
        if len(matches) == 1:
            return self._payload_result(matches[0], response)
        return PublishResult(
            status="manual_required",
            remote_state="unknown",
            reconcile_required=True,
            http_status=response.status_code,
            error_code="remote_match_not_unique" if len(matches) > 1 else "remote_match_not_found",
        )

    def fetch_remote_state(self, remote_post_id: str) -> PublishResult:
        response = self._request("GET", f"/posts/{remote_post_id}", params={"context": "edit"})
        return self._post_result(response)

    def verify_public_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            return False
        try:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as public_client:
                response = public_client.get(url, headers={"User-Agent": "WisdomeWriter/1.0"})
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def upload_media(
        self,
        *,
        content: bytes,
        filename: str,
        mime_type: str,
        remote_lookup_key: str,
        alt_text: str,
        caption: str,
        description_marker: str,
    ) -> PublishResult:
        existing = self.find_media(remote_lookup_key, description_marker)
        if existing.status == "succeeded":
            return existing
        if existing.error_code == "remote_match_not_unique":
            return existing
        safe_name = PurePath(filename).name.replace('"', "") or "asset"
        mime_type = mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        response = self._request(
            "POST",
            "/media",
            write=True,
            content=content,
            headers={
                "Content-Type": mime_type,
                "Content-Disposition": f'attachment; filename="{safe_name}"',
            },
        )
        media = response.json()
        media_id = str(media["id"])
        self._request(
            "POST",
            f"/media/{media_id}",
            write=True,
            json={
                "slug": remote_lookup_key,
                "alt_text": alt_text,
                "caption": caption,
                "description": description_marker,
            },
        )
        return PublishResult(
            status="succeeded",
            remote_post_id=media_id,
            remote_url=media.get("source_url"),
            remote_state="published",
            request_id=response.headers.get("X-WP-Request-ID"),
            http_status=response.status_code,
        )

    def find_media(self, remote_lookup_key: str, description_marker: str) -> PublishResult:
        response = self._request(
            "GET",
            "/media",
            params={"slug": remote_lookup_key, "context": "edit", "per_page": 10},
        )
        matches = []
        for item in response.json():
            description = (item.get("description") or {}).get("raw") or (item.get("description") or {}).get("rendered", "")
            if description_marker in description:
                matches.append(item)
        if len(matches) == 1:
            item = matches[0]
            return PublishResult(
                status="succeeded",
                remote_post_id=str(item["id"]),
                remote_url=item.get("source_url"),
                remote_state="published",
                http_status=response.status_code,
            )
        return PublishResult(
            status="manual_required",
            reconcile_required=True,
            error_code="remote_match_not_unique" if len(matches) > 1 else "remote_match_not_found",
            http_status=response.status_code,
        )

    def delete_media(self, remote_media_id: str) -> PublishResult:
        response = self._request("DELETE", f"/media/{remote_media_id}", write=True, params={"force": True})
        payload = response.json()
        return PublishResult(
            status="succeeded",
            remote_post_id=str((payload.get("previous") or {}).get("id") or remote_media_id),
            remote_state="deleted",
            request_id=response.headers.get("X-WP-Request-ID"),
            http_status=response.status_code,
        )

    def delete_post(self, remote_post_id: str) -> PublishResult:
        response = self._request(
            "DELETE", f"/posts/{remote_post_id}", write=True, params={"force": True}
        )
        payload = response.json()
        previous = payload.get("previous") or payload
        return PublishResult(
            status="succeeded",
            remote_post_id=str(previous.get("id") or remote_post_id),
            remote_state="deleted",
            request_id=response.headers.get("X-WP-Request-ID"),
            http_status=response.status_code,
        )

    def revoke_credentials(self) -> None:
        user = self._request("GET", "/users/me", params={"context": "edit"}).json()
        user_id = user.get("id")
        if not user_id:
            raise PublisherError("wordpress_user_identity_missing", category="permanent")
        response = self._request(
            "GET", f"/users/{user_id}/application-passwords/introspect"
        )
        application = response.json()
        app_uuid = application.get("uuid")
        if not app_uuid:
            raise PublisherError("wordpress_application_password_identity_missing", category="permanent")
        self._request(
            "DELETE",
            f"/users/{user_id}/application-passwords/{app_uuid}",
            write=True,
        )

    def _post_result(self, response: httpx.Response) -> PublishResult:
        return self._payload_result(response.json(), response)

    @staticmethod
    def _payload_result(payload: dict[str, Any], response: httpx.Response) -> PublishResult:
        status = payload.get("status", "unknown")
        state_map = {
            "publish": "published",
            "future": "scheduled",
            "draft": "draft",
            "pending": "draft",
            "trash": "deleted",
        }
        published_at = None
        if status == "publish" and payload.get("date_gmt"):
            try:
                published_at = datetime.fromisoformat(payload["date_gmt"].replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                published_at = None
        return PublishResult(
            status="succeeded",
            remote_post_id=str(payload.get("id")) if payload.get("id") is not None else None,
            remote_url=payload.get("link"),
            remote_state=state_map.get(status, "unknown"),
            remote_revision=response.headers.get("ETag") or payload.get("modified_gmt"),
            published_at=published_at,
            request_id=response.headers.get("X-WP-Request-ID"),
            http_status=response.status_code,
        )
