from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import time
from typing import Any, Callable

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

MAX_RECONCILE_PAGES = 15
MAX_RECONCILE_ITEMS = 750
RECONCILE_DEADLINE_SECONDS = 30.0


class BloggerPublisher:
    """Google Blogger API v3 adapter using an OAuth bearer token."""

    capabilities = PublisherCapabilities(
        create=True,
        update=True,
        unpublish=True,
        mark_withdrawn=True,
        draft=True,
        schedule=True,
        media_upload=False,
        supported_media_types=(),
    )
    api_url = "https://www.googleapis.com/blogger/v3"

    def __init__(
        self,
        *,
        blog_id: str,
        access_token: str,
        revocation_token: str | None = None,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
        write_guard: Callable[[], None] | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ):
        self.blog_id = str(blog_id)
        self.revocation_token = str(revocation_token or "").strip()
        self.write_guard = write_guard or (lambda: None)
        self.monotonic_clock = monotonic_clock
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "WisdomeWriter/1.0",
            },
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
                "blogger_unknown_outcome" if write else "blogger_unavailable",
                category="unknown_outcome" if write else "retryable",
                detail_redacted=exc.__class__.__name__,
            ) from exc
        if response.status_code < 400:
            return response
        if response.status_code == 401:
            category, code = "refreshable_auth", "blogger_token_expired"
        elif response.status_code == 403:
            category, code = "permanent", "blogger_scope_or_owner_denied"
        elif response.status_code in (408, 429) or response.status_code >= 500:
            if write:
                category, code = "unknown_outcome", "blogger_unknown_outcome"
            else:
                category, code = "retryable", "blogger_temporarily_unavailable"
        else:
            category, code = "permanent", "blogger_request_rejected"
        retry_after = response.headers.get("Retry-After")
        raise PublisherError(
            code,
            category=category,
            http_status=response.status_code,
            retry_after_seconds=parse_retry_after_seconds(retry_after),
            detail_redacted=f"HTTP {response.status_code}",
        )

    @staticmethod
    def lookup_label(remote_lookup_key: str) -> str:
        return f"wisdome-{remote_lookup_key}"[:200]

    def preflight_connection(self) -> PreflightResult:
        checks: list[dict[str, Any]] = []
        try:
            response = self._request(
                "GET",
                f"/blogs/{self.blog_id}",
                params={"fields": "id,name,url,status"},
            )
            blog = response.json()
            identity_matches = str(blog.get("id")) == self.blog_id
            checks.extend(
                [
                    {"code": "blog_identity", "passed": identity_matches},
                    {"code": "blog_https_url", "passed": str(blog.get("url", "")).startswith("https://")},
                ]
            )
            return PreflightResult(
                passed=all(check["passed"] for check in checks),
                remote_identity=str(blog.get("id")) if blog.get("id") else None,
                remote_url=blog.get("url"),
                capabilities=self.capabilities,
                checks=tuple(checks),
            )
        except PublisherError as exc:
            checks.append({"code": exc.code, "passed": False})
            return PreflightResult(
                passed=False,
                remote_identity=None,
                remote_url=None,
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
            if article.canonical_link_state != "resolved" or not article.canonical_source_url:
                raise PublisherError("canonical_wordpress_url_required", category="permanent")
            labels = list(dict.fromkeys([*article.labels, self.lookup_label(command.remote_lookup_key)]))
            body = f"{article.body_html}\n{publication_content_marker(command)}"
            response = self._request(
                "POST",
                f"/blogs/{self.blog_id}/posts",
                write=True,
                params={"isDraft": "false"},
                json={"kind": "blogger#post", "title": article.title, "content": body, "labels": labels},
            )
            return self._verify_mutation(command, self._post_result(response))

        if command.action in {"update", "mark_withdrawn"}:
            article = command.rendered_article
            assert article is not None
            body = f"{article.body_html}\n{publication_content_marker(command)}"
            labels = list(
                dict.fromkeys([*article.labels, self.lookup_label(command.remote_lookup_key)])
            )
            response = self._request(
                "PATCH",
                f"/blogs/{self.blog_id}/posts/{command.remote_post_id}",
                write=True,
                json={"title": article.title, "content": body, "labels": labels},
            )
            return self._verify_mutation(command, self._post_result(response))

        if command.action == "unpublish":
            response = self._request(
                "POST",
                f"/blogs/{self.blog_id}/posts/{command.remote_post_id}/revert",
                write=True,
            )
            return self._verify_mutation(command, self._post_result(response))
        raise PublisherError("unsupported_action", category="permanent")

    def reconcile(self, command: PublishCommand) -> PublishResult:
        if command.action != "create":
            if not command.remote_post_id:
                raise PublisherError("remote_post_id_required", category="permanent")
            response = self._request(
                "GET",
                f"/blogs/{self.blog_id}/posts/{command.remote_post_id}",
                params={"view": "ADMIN"},
            )
            item = response.json()
            if (
                str(item.get("id")) != str(command.remote_post_id)
                or str((item.get("blog") or {}).get("id")) != self.blog_id
            ):
                return self._reconcile_mismatch(response.status_code)
            if command.action == "unpublish":
                if str(item.get("status", "")).lower() != "draft":
                    return self._reconcile_mismatch(response.status_code)
                return self._payload_result(item, response.status_code, response.headers)
            article = command.rendered_article
            if article is None:
                raise PublisherError("render_required", category="permanent")
            expected_content = (
                f"{article.body_html}\n{publication_content_marker(command)}"
            )
            if (
                item.get("content", "") != expected_content
                or item.get("title") != article.title
                or str(item.get("status", "")).lower() != "live"
            ):
                return self._reconcile_mismatch(response.status_code)
            return self._payload_result(item, response.status_code, response.headers)

        marker = publication_content_marker(command)
        article = command.rendered_article
        if article is None:
            raise PublisherError("render_required", category="permanent")
        expected_content = f"{article.body_html}\n{marker}"
        label = self.lookup_label(command.remote_lookup_key)
        matches: dict[str, dict[str, Any]] = {}
        last_status = None
        page_count = 0
        item_count = 0
        deadline = self.monotonic_clock() + RECONCILE_DEADLINE_SECONDS
        for state in ("live", "draft", "scheduled"):
            page_token = None
            for _page in range(5):
                remaining = deadline - self.monotonic_clock()
                if remaining <= 0:
                    return PublishResult(
                        status="manual_required",
                        remote_state="unknown",
                        reconcile_required=True,
                        http_status=last_status,
                        error_code="remote_reconcile_deadline_exceeded",
                    )
                page_count += 1
                if page_count > MAX_RECONCILE_PAGES:
                    return PublishResult(
                        status="manual_required",
                        remote_state="unknown",
                        reconcile_required=True,
                        http_status=last_status,
                        error_code="remote_reconcile_page_limit_exceeded",
                    )
                params = {
                    "labels": label,
                    "status": state,
                    "fetchBodies": "true",
                    "maxResults": 50,
                    "fields": (
                        "items(id,blog/id,url,status,published,updated,title,content,labels),"
                        "nextPageToken"
                    ),
                }
                if page_token:
                    params["pageToken"] = page_token
                response = self._request(
                    "GET",
                    f"/blogs/{self.blog_id}/posts",
                    params=params,
                    timeout=remaining,
                )
                if self.monotonic_clock() >= deadline:
                    return self._reconcile_deadline(last_status)
                last_status = response.status_code
                payload = response.json()
                if self.monotonic_clock() >= deadline:
                    return self._reconcile_deadline(last_status)
                items = payload.get("items", [])
                if not isinstance(items, list):
                    return self._reconcile_mismatch(last_status)
                item_count += len(items)
                if item_count > MAX_RECONCILE_ITEMS:
                    return PublishResult(
                        status="manual_required",
                        remote_state="unknown",
                        reconcile_required=True,
                        http_status=last_status,
                        error_code="remote_reconcile_item_limit_exceeded",
                    )
                for item in items:
                    if self.monotonic_clock() >= deadline:
                        return self._reconcile_deadline(last_status)
                    if marker in item.get("content", "") and label in item.get("labels", []):
                        matches[str(item["id"])] = item
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
            if page_token:
                return PublishResult(
                    status="manual_required",
                    remote_state="unknown",
                    reconcile_required=True,
                    http_status=last_status,
                    error_code="remote_reconcile_page_limit_exceeded",
                )
        if self.monotonic_clock() >= deadline:
            return self._reconcile_deadline(last_status)
        if len(matches) == 1:
            item = next(iter(matches.values()))
            if (
                str((item.get("blog") or {}).get("id")) != self.blog_id
                or str(item.get("status", "")).lower() != "live"
                or item.get("title") != article.title
                or item.get("content", "") != expected_content
            ):
                return self._reconcile_mismatch(last_status)
            return self._payload_result(item, last_status)
        return PublishResult(
            status="retryable_failed" if not matches else "manual_required",
            remote_state="unknown",
            reconcile_required=True,
            http_status=last_status,
            error_code="remote_match_not_unique" if len(matches) > 1 else "remote_match_not_found",
        )

    @staticmethod
    def _reconcile_deadline(http_status: int | None) -> PublishResult:
        return PublishResult(
            status="manual_required",
            remote_state="unknown",
            reconcile_required=True,
            http_status=http_status,
            error_code="remote_reconcile_deadline_exceeded",
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
        response = self._request(
            "GET", f"/blogs/{self.blog_id}/posts/{remote_post_id}", params={"view": "ADMIN"}
        )
        return self._post_result(response)

    def delete_post(self, remote_post_id: str) -> PublishResult:
        response = self._request(
            "DELETE", f"/blogs/{self.blog_id}/posts/{remote_post_id}", write=True
        )
        return PublishResult(
            status="succeeded",
            remote_post_id=remote_post_id,
            remote_state="deleted",
            request_id=response.headers.get("X-GUploader-UploadID"),
            http_status=response.status_code,
        )

    def revoke_credentials(self) -> None:
        token = self.revocation_token
        if not token:
            raise PublisherError("blogger_token_missing", category="permanent")
        self.write_guard()
        try:
            response = self.client.post(
                "https://oauth2.googleapis.com/revoke",
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise PublisherError(
                "blogger_revoke_unknown_outcome",
                category="unknown_outcome",
                detail_redacted=exc.__class__.__name__,
            ) from exc
        if response.status_code == 200:
            return
        error_code = ""
        if response.status_code == 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            error_code = str(payload.get("error") or "")
            if error_code == "invalid_token":
                return
        if response.status_code != 200:
            raise PublisherError(
                "blogger_revoke_failed",
                category=(
                    "unknown_outcome"
                    if response.status_code in (408, 429)
                    or response.status_code >= 500
                    else "permanent"
                ),
                http_status=response.status_code,
                detail_redacted=error_code,
            )

    def _post_result(self, response: httpx.Response) -> PublishResult:
        return self._payload_result(response.json(), response.status_code, response.headers)

    def _verify_mutation(
        self,
        command: PublishCommand,
        factual_result: PublishResult,
    ) -> PublishResult:
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
    def _payload_result(
        payload: dict[str, Any],
        http_status: int | None,
        headers: httpx.Headers | None = None,
    ) -> PublishResult:
        status = str(payload.get("status", "unknown")).lower()
        state_map = {"live": "published", "draft": "draft", "scheduled": "scheduled"}
        published_at = None
        if payload.get("published"):
            try:
                published_at = datetime.fromisoformat(payload["published"].replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                published_at = None
        return PublishResult(
            status="succeeded",
            remote_post_id=str(payload.get("id")) if payload.get("id") is not None else None,
            remote_url=payload.get("url"),
            remote_state=state_map.get(status, "unknown"),
            remote_revision=payload.get("updated"),
            published_at=published_at,
            request_id=headers.get("X-GUploader-UploadID") if headers else None,
            http_status=http_status,
        )
