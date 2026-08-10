from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Any
from urllib.parse import urlparse

import httpx

from apps.publishing.contracts import (
    PreflightResult,
    PublishCommand,
    PublisherCapabilities,
    PublisherError,
    PublishResult,
    parse_retry_after_seconds,
    publication_content_marker,
)
from wisdome_writer.infrastructure.http_safety import HttpSafetyError, redact_url, safe_get

MAX_PUBLIC_VERIFICATION_BYTES = 1024 * 1024


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
        self.site_url = redact_url(base_url).rstrip("/")
        public_host = urlparse(self.site_url).hostname
        if not public_host:
            raise ValueError("WordPress public URL host is required")
        self.public_hosts = {public_host}
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
        elif response.status_code in (408, 429) or response.status_code >= 500:
            if write:
                category, code = "unknown_outcome", "wordpress_unknown_outcome"
            else:
                category, code = "retryable", "wordpress_temporarily_unavailable"
        else:
            category, code = "permanent", "wordpress_request_rejected"
        retry_after = response.headers.get("Retry-After")
        raise PublisherError(
            code,
            category=category,
            http_status=response.status_code,
            retry_after_seconds=parse_retry_after_seconds(retry_after),
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
            if not (
                existing.status == "retryable_failed"
                and existing.error_code == "remote_match_not_found"
            ):
                return existing
            article = command.rendered_article
            assert article is not None
            body = f"{article.body_html}\n{publication_content_marker(command)}"
            payload = {
                "slug": command.remote_lookup_key,
                "title": article.title,
                "content": body,
                "status": "publish",
            }
            response = self._request("POST", "/posts", write=True, json=payload)
            return self._verify_mutation(command, self._post_result(response))

        if command.action in {"update", "mark_withdrawn"}:
            article = command.rendered_article
            assert article is not None
            body = f"{article.body_html}\n{publication_content_marker(command)}"
            response = self._request(
                "POST",
                f"/posts/{command.remote_post_id}",
                write=True,
                json={"title": article.title, "content": body},
            )
            return self._verify_mutation(command, self._post_result(response))

        if command.action == "unpublish":
            response = self._request(
                "POST",
                f"/posts/{command.remote_post_id}",
                write=True,
                json={"status": "draft"},
            )
            return self._verify_mutation(command, self._post_result(response))
        raise PublisherError("unsupported_action", category="permanent")

    def reconcile(self, command: PublishCommand) -> PublishResult:
        if command.action != "create":
            if not command.remote_post_id:
                raise PublisherError("remote_post_id_required", category="permanent")
            response = self._request(
                "GET",
                f"/posts/{command.remote_post_id}",
                params={"context": "edit"},
            )
            payload = response.json()
            if str(payload.get("id")) != str(command.remote_post_id):
                return self._reconcile_mismatch(response.status_code)
            if command.action == "unpublish":
                if payload.get("status") not in {"draft", "trash"}:
                    return self._reconcile_mismatch(response.status_code)
                return self._payload_result(payload, response)
            article = command.rendered_article
            if article is None:
                raise PublisherError("render_required", category="permanent")
            content = payload.get("content") or {}
            remote_content = content.get("raw") or ""
            title = payload.get("title") or {}
            remote_title = title.get("raw") or ""
            expected_content = (
                f"{article.body_html}\n{publication_content_marker(command)}"
            )
            if (
                remote_content != expected_content
                or remote_title != article.title
                or payload.get("status") != "publish"
            ):
                return self._reconcile_mismatch(response.status_code)
            return self._payload_result(payload, response)

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
        article = command.rendered_article
        if article is None:
            raise PublisherError("render_required", category="permanent")
        marker = publication_content_marker(command)
        expected_content = f"{article.body_html}\n{marker}"
        matches = [
            item
            for item in response.json()
            if marker
            in (
                (item.get("content") or {}).get("raw")
                or (item.get("content") or {}).get("rendered")
                or ""
            )
        ]
        if len(matches) == 1:
            item = matches[0]
            content = item.get("content") or {}
            title = item.get("title") or {}
            if (
                item.get("status") != "publish"
                or (content.get("raw") or "") != expected_content
                or (title.get("raw") or "") != article.title
            ):
                return self._reconcile_mismatch(response.status_code)
            return self._payload_result(item, response)
        return PublishResult(
            status="retryable_failed" if not matches else "manual_required",
            remote_state="unknown",
            reconcile_required=True,
            http_status=response.status_code,
            error_code="remote_match_not_unique" if len(matches) > 1 else "remote_match_not_found",
        )

    @staticmethod
    def _reconcile_mismatch(http_status: int | None) -> PublishResult:
        return PublishResult(
            status="manual_required",
            remote_state="unknown",
            reconcile_required=True,
            http_status=http_status,
            error_code="remote_projection_mismatch",
        )

    def fetch_remote_state(self, remote_post_id: str) -> PublishResult:
        response = self._request("GET", f"/posts/{remote_post_id}", params={"context": "edit"})
        return self._post_result(response)

    def verify_public_url(self, url: str) -> bool:
        try:
            response = safe_get(
                url,
                max_bytes=MAX_PUBLIC_VERIFICATION_BYTES,
                timeout=self.timeout_seconds,
                allowed_hosts=self.public_hosts,
                headers={"User-Agent": "WisdomeWriter/1.0"},
                max_elapsed_seconds=self.timeout_seconds,
            )
            return response.status_code == 200
        except (HttpSafetyError, httpx.HTTPError):
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
        expected_checksum = hashlib.sha256(content).hexdigest()
        existing = self.find_media(
            remote_lookup_key=remote_lookup_key,
            description_marker=description_marker,
            expected_alt_text=alt_text,
            expected_caption=caption,
            expected_checksum=expected_checksum,
            expected_mime_type=mime_type,
        )
        if existing.status == "succeeded":
            return existing
        if existing.status != "not_found":
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
        verified = self.find_media(
            remote_lookup_key=remote_lookup_key,
            description_marker=description_marker,
            expected_alt_text=alt_text,
            expected_caption=caption,
            expected_checksum=expected_checksum,
            expected_mime_type=mime_type,
        )
        if verified.status == "succeeded" and verified.remote_post_id == media_id:
            return verified
        if verified.status == "manual_required":
            return verified
        return PublishResult(
            status="unknown_outcome",
            remote_post_id=media_id,
            remote_state="unknown",
            reconcile_required=True,
            request_id=response.headers.get("X-WP-Request-ID"),
            http_status=response.status_code,
            error_code="remote_media_projection_unproven",
        )

    def find_media(
        self,
        remote_lookup_key: str,
        description_marker: str,
        *,
        expected_alt_text: str | None = None,
        expected_caption: str | None = None,
        expected_checksum: str | None = None,
        expected_mime_type: str | None = None,
    ) -> PublishResult:
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
        exact_material_requested = all(
            value is not None
            for value in (
                expected_alt_text,
                expected_caption,
                expected_checksum,
                expected_mime_type,
            )
        )
        if len(matches) == 1:
            item = matches[0]
            source_url = item.get("source_url")
            caption = item.get("caption") or {}
            caption_value = (
                caption.get("raw")
                or caption.get("rendered")
                or ""
            )
            if exact_material_requested:
                if (
                    item.get("slug") != remote_lookup_key
                    or item.get("alt_text", "") != expected_alt_text
                    or caption_value != expected_caption
                    or item.get("mime_type") != expected_mime_type
                    or not source_url
                ):
                    return self._media_material_mismatch(response.status_code)
                try:
                    public_response = safe_get(
                        source_url,
                        max_bytes=MAX_PUBLIC_VERIFICATION_BYTES,
                        timeout=self.timeout_seconds,
                        allowed_hosts=self.public_hosts,
                        headers={"User-Agent": "WisdomeWriter/1.0"},
                        max_elapsed_seconds=self.timeout_seconds,
                    )
                except (HttpSafetyError, httpx.HTTPError):
                    return PublishResult(
                        status="unknown_outcome",
                        remote_post_id=str(item["id"]),
                        remote_state="unknown",
                        reconcile_required=True,
                        http_status=response.status_code,
                        error_code="remote_media_public_read_unavailable",
                    )
                observed_type = (
                    public_response.headers.get("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if (
                    public_response.status_code != 200
                    or observed_type != str(expected_mime_type).lower()
                    or hashlib.sha256(public_response.content).hexdigest()
                    != expected_checksum
                ):
                    return self._media_material_mismatch(response.status_code)
            return PublishResult(
                status="succeeded",
                remote_post_id=str(item["id"]),
                remote_url=redact_url(source_url) if source_url else None,
                remote_state="published",
                http_status=response.status_code,
            )
        if len(matches) == 0 and exact_material_requested:
            return PublishResult(
                status="not_found",
                remote_state="unknown",
                reconcile_required=True,
                error_code="remote_match_not_found",
                http_status=response.status_code,
            )
        return PublishResult(
            status="manual_required",
            reconcile_required=True,
            error_code="remote_match_not_unique" if len(matches) > 1 else "remote_match_not_found",
            http_status=response.status_code,
        )

    @staticmethod
    def _media_material_mismatch(http_status: int | None) -> PublishResult:
        return PublishResult(
            status="manual_required",
            remote_state="unknown",
            reconcile_required=True,
            http_status=http_status,
            error_code="remote_media_material_mismatch",
        )

    def delete_media(self, remote_media_id: str) -> PublishResult:
        response = self._request("DELETE", f"/media/{remote_media_id}", write=True, params={"force": True})
        payload = response.json()
        try:
            self._request("GET", f"/media/{remote_media_id}", params={"context": "edit"})
        except PublisherError as exc:
            if exc.http_status not in {404, 410}:
                return PublishResult(
                    status="unknown_outcome",
                    remote_post_id=str(remote_media_id),
                    remote_state="unknown",
                    reconcile_required=True,
                    request_id=response.headers.get("X-WP-Request-ID"),
                    http_status=exc.http_status or response.status_code,
                    error_code="remote_media_delete_unproven",
                )
        else:
            return PublishResult(
                status="manual_required",
                remote_post_id=str(remote_media_id),
                remote_state="published",
                reconcile_required=True,
                request_id=response.headers.get("X-WP-Request-ID"),
                http_status=response.status_code,
                error_code="remote_media_delete_not_applied",
            )
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
        verification_path = (
            f"/users/{user_id}/application-passwords/{app_uuid}"
        )
        try:
            verification = self.client.request(
                "GET",
                f"{self.api_url}{verification_path}",
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise PublisherError(
                "wordpress_revoke_verification_unknown",
                category="unknown_outcome",
                detail_redacted=exc.__class__.__name__,
            ) from exc
        if verification.status_code in (404, 410):
            return
        if verification.status_code >= 500 or verification.status_code in (408, 429):
            raise PublisherError(
                "wordpress_revoke_verification_unknown",
                category="unknown_outcome",
                http_status=verification.status_code,
            )
        raise PublisherError(
            "wordpress_application_password_still_present",
            category="unknown_outcome",
            http_status=verification.status_code,
        )

    def _post_result(self, response: httpx.Response) -> PublishResult:
        try:
            payload = response.json()
        except ValueError:
            return PublishResult(
                status="unknown_outcome",
                remote_state="unknown",
                reconcile_required=True,
                http_status=response.status_code,
                error_code="remote_mutation_response_invalid",
            )
        return self._payload_result(payload, response)

    def _verify_mutation(
        self,
        command: PublishCommand,
        factual_result: PublishResult,
    ) -> PublishResult:
        if factual_result.status != "succeeded":
            return factual_result
        remote_post_id = factual_result.remote_post_id or command.remote_post_id
        if not remote_post_id:
            return PublishResult(
                status="unknown_outcome",
                remote_state="unknown",
                reconcile_required=True,
                http_status=factual_result.http_status,
                error_code="remote_projection_identity_missing",
            )
        verification_command = (
            command
            if command.action == "create"
            else replace(command, remote_post_id=remote_post_id)
        )
        try:
            verified = self.reconcile(verification_command)
        except PublisherError as exc:
            return PublishResult(
                status="unknown_outcome",
                remote_post_id=remote_post_id,
                remote_state="unknown",
                reconcile_required=True,
                http_status=exc.http_status or factual_result.http_status,
                error_code="remote_projection_verification_unavailable",
            )
        if (
            verified.status == "succeeded"
            and str(verified.remote_post_id) == str(remote_post_id)
        ):
            return verified
        if verified.status == "manual_required":
            return verified
        return PublishResult(
            status="unknown_outcome",
            remote_post_id=remote_post_id,
            remote_state="unknown",
            reconcile_required=True,
            http_status=verified.http_status or factual_result.http_status,
            error_code=verified.error_code or "remote_projection_unproven",
        )

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
            remote_url=redact_url(payload["link"]) if payload.get("link") else None,
            remote_state=state_map.get(status, "unknown"),
            remote_revision=response.headers.get("ETag") or payload.get("modified_gmt"),
            published_at=published_at,
            request_id=response.headers.get("X-WP-Request-ID"),
            http_status=response.status_code,
        )
