from __future__ import annotations

import codecs
import email.utils
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic, sleep
from urllib.parse import unquote, urlsplit

import httpx

from wisdome_writer.infrastructure.http_safety import (
    HttpSafetyError,
    OutboundRequestFailed,
    OutboundResolutionFailed,
    OutboundResponseMimeRejected,
    OutboundResponseTooLarge,
    redact_url,
    redact_urls_in_text,
    safe_get,
    safe_post_form,
)

MAX_HTML_BYTES = 5 * 1024 * 1024
TOTAL_BUDGET_SECONDS = 30.0
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_CHARSET_PATTERN = re.compile(r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\s\"']+)", re.IGNORECASE)


class OfficialSourceError(Exception):
    """A persistence-safe failure while reading an approved official source."""


@dataclass(frozen=True)
class HtmlResponse:
    url: str
    status_code: int
    content_type: str
    body: str
    fetched_at: datetime


class OfficialHtmlFetcher:
    def __init__(
        self,
        *,
        allowed_hosts: set[str],
        path_prefixes: tuple[str, ...] = ("/",),
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        try:
            self._allowed_hosts = frozenset(_idna_host(host) for host in allowed_hosts)
        except (TypeError, UnicodeError):
            raise ValueError("allowed_hosts must contain valid hostnames") from None
        if not self._allowed_hosts:
            raise ValueError("allowed_hosts must not be empty")
        if not path_prefixes or any(not prefix.startswith("/") for prefix in path_prefixes):
            raise ValueError("path_prefixes must contain absolute paths")
        self._path_prefixes = tuple(path_prefixes)
        self._transport = transport

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> HtmlResponse:
        self._validate_url(url)
        try:
            request_url = str(httpx.URL(url).copy_merge_params(params or {}))
        except (TypeError, ValueError):
            raise OfficialSourceError("Official source URL parameters are invalid") from None
        return self._request("GET", request_url)

    def post(self, url: str, *, data: Mapping[str, str]) -> HtmlResponse:
        self._validate_url(url)
        return self._request("POST", url, data=data)

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
    ) -> HtmlResponse:
        request = safe_get if method == "GET" else safe_post_form
        deadline = monotonic() + TOTAL_BUDGET_SECONDS
        physical_attempts = 0

        def claim_attempt() -> None:
            nonlocal physical_attempts
            if physical_attempts >= 3:
                raise OfficialSourceError(
                    f"Official source request attempt limit exceeded: {redact_url(url)}"
                )
            physical_attempts += 1

        response: httpx.Response | None = None
        for logical_attempt in range(3):
            remaining = _remaining_seconds(deadline, url)
            arguments = {
                "max_bytes": MAX_HTML_BYTES,
                "timeout": remaining,
                "allowed_hosts": self._allowed_hosts,
                "max_elapsed_seconds": remaining,
                "https_only": True,
                "before_request": claim_attempt,
                "url_validator": self._validate_url,
                "allowed_content_types": HTML_CONTENT_TYPES,
                "transport": self._transport,
            }
            if data is not None:
                arguments["data"] = data
            try:
                response = request(url, **arguments)
            except OutboundResponseMimeRejected:
                raise OfficialSourceError(
                    f"Official source response content type is not HTML: {redact_url(url)}"
                ) from None
            except OutboundResponseTooLarge:
                raise OfficialSourceError(
                    f"Official source response exceeded the 5 MiB size limit: {redact_url(url)}"
                ) from None
            except OfficialSourceError:
                raise
            except (OutboundRequestFailed, OutboundResolutionFailed) as exc:
                if logical_attempt == 2 or physical_attempts >= 3:
                    raise OfficialSourceError(redact_urls_in_text(str(exc))) from None
                continue
            except HttpSafetyError as exc:
                raise OfficialSourceError(redact_urls_in_text(str(exc))) from None
            except Exception as exc:
                raise OfficialSourceError(
                    "Official source request failed "
                    f"({exc.__class__.__name__}): {redact_url(url)}"
                ) from None

            if response.status_code == 200:
                break
            if not _is_retryable_status(response.status_code):
                raise _status_error(response.status_code, url)
            if logical_attempt == 2 or physical_attempts >= 3:
                raise _status_error(response.status_code, url)
            retry_after = _retry_after_seconds(response.headers.get("retry-after"))
            if retry_after:
                remaining = _remaining_seconds(deadline, url)
                if retry_after >= remaining:
                    raise OfficialSourceError(
                        f"Official source request time limit exceeded: {redact_url(url)}"
                    )
                sleep(retry_after)

        if response is None or response.status_code != 200:
            raise OfficialSourceError(
                f"Official source request failed: {redact_url(url)}"
            )
        content_type = response.headers.get("content-type", "")
        body = _decode_html(response.content, content_type, url)
        return HtmlResponse(
            url=str(response.request.url),
            status_code=response.status_code,
            content_type=content_type,
            body=body,
            fetched_at=datetime.now(UTC),
        )

    def _validate_url(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            _ = parsed.port
        except (TypeError, ValueError):
            raise OfficialSourceError("Official source URL is invalid") from None
        if parsed.scheme.lower() != "https" or not hostname:
            raise OfficialSourceError("Official source URL must use https")
        if parsed.username is not None or parsed.password is not None:
            raise OfficialSourceError("Official source URL credentials are not allowed")
        try:
            normalized_host = _idna_host(hostname)
        except UnicodeError:
            raise OfficialSourceError("Official source URL host is invalid") from None
        if normalized_host not in self._allowed_hosts:
            raise OfficialSourceError("Official source URL host is not approved")
        path = parsed.path or "/"
        decoded_path = unquote(path)
        if (
            "\\" in decoded_path
            or re.search(r"%(?:2f|5c)", path, re.IGNORECASE)
            or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        ):
            raise OfficialSourceError("Official source URL path is ambiguous or traversing")
        if not any(_path_has_prefix(path, prefix) for prefix in self._path_prefixes):
            raise OfficialSourceError("Official source URL path is not approved")


def _idna_host(hostname: str) -> str:
    return hostname.rstrip(".").encode("idna").decode("ascii").lower()


def _path_has_prefix(path: str, prefix: str) -> bool:
    if prefix == "/":
        return path.startswith("/")
    normalized_prefix = prefix.rstrip("/")
    return path == normalized_prefix or path.startswith(f"{normalized_prefix}/")


def _decode_html(content: bytes, content_type: str, url: str) -> str:
    match = _CHARSET_PATTERN.search(content_type)
    encoding = match.group(1) if match else "utf-8"
    try:
        codecs.lookup(encoding)
        return content.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        raise OfficialSourceError(
            f"Official source response encoding is invalid: {redact_url(url)}"
        ) from None


def _remaining_seconds(deadline: float, url: str) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise OfficialSourceError(
            f"Official source request time limit exceeded: {redact_url(url)}"
        )
    return remaining


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _status_error(status_code: int, url: str) -> OfficialSourceError:
    return OfficialSourceError(
        f"Official source returned status {status_code}: {redact_url(url)}"
    )


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(int(value.strip())))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed.astimezone(UTC) - datetime.now(UTC)).total_seconds())
