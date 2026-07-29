from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx

DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_ELAPSED_SECONDS = 60.0
STREAM_CHUNK_BYTES = 64 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


class HttpSafetyError(Exception):
    """Base class for outbound failures whose messages are safe to persist."""


class UnsafeOutboundUrl(HttpSafetyError, ValueError):
    """Raised when an outbound URL or its resolved address is not safe."""


class OutboundResponseTooLarge(HttpSafetyError):
    """Raised before an outbound response can exceed the configured memory limit."""


class OutboundRequestFailed(HttpSafetyError):
    """Raised for network failures without exposing the original request URL."""


@dataclass(frozen=True)
class _ValidatedTarget:
    logical_url: str
    parsed: SplitResult
    hostname: str
    port: int
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]


def redact_url(url: str) -> str:
    """Return a persistence-safe URL without userinfo, query values, or fragments."""

    try:
        parsed = urlsplit(str(url))
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return "<redacted-url>"
    if not parsed.scheme or not hostname:
        return "<redacted-url>"
    safe_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        safe_host = f"{safe_host}:{port}"
    return urlunsplit((parsed.scheme.lower(), safe_host, parsed.path or "/", "", ""))


def redact_urls_in_text(text: str) -> str:
    """Redact URL secrets before exception or log text is persisted."""

    return _URL_PATTERN.sub(lambda match: redact_url(match.group(0)), str(text))


def redact_url_values(value: Any) -> Any:
    """Recursively redact URL-shaped strings while preserving non-URL identities."""

    if isinstance(value, Mapping):
        return {
            redact_urls_in_text(key) if isinstance(key, str) else key: redact_url_values(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_url_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_url_values(item) for item in value)
    if isinstance(value, str):
        return redact_urls_in_text(value)
    return value


def safe_get(
    url: str,
    *,
    max_bytes: int,
    timeout: httpx.Timeout | float,
    allowed_hosts: Collection[str | None] | None = None,
    headers: Mapping[str, str] | None = None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS,
    https_only: bool = False,
    before_request: Callable[[], None] | None = None,
    url_validator: Callable[[str], None] | None = None,
) -> httpx.Response:
    """GET a public HTTP(S) resource with DNS pinning, redirect checks, and bounded streaming."""
    return _safe_request(
        "GET",
        url,
        max_bytes=max_bytes,
        timeout=timeout,
        allowed_hosts=allowed_hosts,
        headers=headers,
        max_redirects=max_redirects,
        max_elapsed_seconds=max_elapsed_seconds,
        https_only=https_only,
        before_request=before_request,
        url_validator=url_validator,
    )


def safe_post_form(
    url: str,
    *,
    data: Mapping[str, Any],
    max_bytes: int,
    timeout: httpx.Timeout | float,
    allowed_hosts: Collection[str | None] | None = None,
    headers: Mapping[str, str] | None = None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS,
    https_only: bool = False,
    before_request: Callable[[], None] | None = None,
    url_validator: Callable[[str], None] | None = None,
) -> httpx.Response:
    """POST an encoded form through the same pinned, bounded path as ``safe_get``."""
    return _safe_request(
        "POST",
        url,
        form_data=data,
        max_bytes=max_bytes,
        timeout=timeout,
        allowed_hosts=allowed_hosts,
        headers=headers,
        max_redirects=max_redirects,
        max_elapsed_seconds=max_elapsed_seconds,
        https_only=https_only,
        before_request=before_request,
        url_validator=url_validator,
    )


def _safe_request(
    method: str,
    url: str,
    *,
    max_bytes: int,
    timeout: httpx.Timeout | float,
    allowed_hosts: Collection[str | None] | None,
    headers: Mapping[str, str] | None,
    max_redirects: int,
    max_elapsed_seconds: float,
    https_only: bool,
    before_request: Callable[[], None] | None,
    url_validator: Callable[[str], None] | None,
    form_data: Mapping[str, Any] | None = None,
) -> httpx.Response:

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if max_redirects < 0:
        raise ValueError("max_redirects must not be negative")
    if max_elapsed_seconds <= 0:
        raise ValueError("max_elapsed_seconds must be positive")

    normalized_allowed_hosts = (
        frozenset(_normalize_hostname(host) for host in allowed_hosts if host)
        if allowed_hosts is not None
        else None
    )
    request_headers = dict(headers or {})
    current_url = str(url)
    current_method = method
    current_form = form_data
    deadline = time.monotonic() + max_elapsed_seconds

    for redirect_count in range(max_redirects + 1):
        _remaining_seconds(deadline, current_url)
        if url_validator is not None:
            url_validator(current_url)
        target = _validate_target(
            current_url,
            normalized_allowed_hosts,
            https_only=https_only,
        )
        response = _request_pinned(
            target,
            method=current_method,
            form_data=current_form,
            max_bytes=max_bytes,
            headers=request_headers,
            timeout=timeout,
            deadline=deadline,
            before_request=before_request,
        )
        location = response.headers.get("location")
        if response.status_code not in _REDIRECT_STATUSES or not location:
            return response
        if redirect_count == max_redirects:
            raise UnsafeOutboundUrl(
                f"Outbound redirect limit exceeded for {redact_url(current_url)}"
            )
        current_url = urljoin(current_url, location)
        if response.status_code in {301, 302, 303}:
            current_method = "GET"
            current_form = None

    raise RuntimeError("unreachable")


def _validate_target(
    url: str,
    allowed_hosts: frozenset[str] | None,
    *,
    https_only: bool = False,
) -> _ValidatedTarget:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise UnsafeOutboundUrl("Outbound URL is invalid") from None

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        raise UnsafeOutboundUrl(
            f"Outbound URL must use HTTP or HTTPS: {redact_url(url)}"
        )
    if https_only and scheme != "https":
        raise UnsafeOutboundUrl(
            f"Outbound URL must use HTTPS: {redact_url(url)}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeOutboundUrl(
            f"Outbound URL userinfo is not allowed: {redact_url(url)}"
        )
    if any(ord(character) < 32 for character in url):
        raise UnsafeOutboundUrl("Outbound URL contains invalid control characters")

    normalized_hostname = _normalize_hostname(hostname)
    if allowed_hosts is not None and normalized_hostname not in allowed_hosts:
        raise UnsafeOutboundUrl(
            f"Outbound URL host is not approved: {redact_url(url)}"
        )

    target_port = port or (443 if scheme == "https" else 80)
    addresses = _resolve_public_addresses(normalized_hostname, target_port, url)
    return _ValidatedTarget(
        logical_url=url,
        parsed=parsed,
        hostname=normalized_hostname,
        port=target_port,
        addresses=addresses,
    )


def _normalize_hostname(hostname: str) -> str:
    try:
        return hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise UnsafeOutboundUrl("Outbound URL hostname is invalid") from None


def _resolve_public_addresses(
    hostname: str,
    port: int,
    logical_url: str,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        literal = ipaddress.ip_address(hostname)
        raw_addresses = (literal,)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError:
            raise UnsafeOutboundUrl(
                f"Outbound host resolution failed for {redact_url(logical_url)}"
            ) from None
        raw_addresses = tuple(
            ipaddress.ip_address(address[4][0].split("%", 1)[0])
            for address in resolved
        )

    addresses = tuple(dict.fromkeys(raw_addresses))
    if not addresses:
        raise UnsafeOutboundUrl(
            f"Outbound host resolution failed for {redact_url(logical_url)}"
        )
    if any(not _is_public_address(address) for address in addresses):
        raise UnsafeOutboundUrl(
            f"Outbound host resolved to a non-public address: {redact_url(logical_url)}"
        )
    return addresses


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


def _request_pinned(
    target: _ValidatedTarget,
    *,
    method: str,
    form_data: Mapping[str, Any] | None,
    max_bytes: int,
    headers: Mapping[str, str],
    timeout: httpx.Timeout | float,
    deadline: float,
    before_request: Callable[[], None] | None,
) -> httpx.Response:
    last_failure: str | None = None
    for address in target.addresses:
        pinned_url = _pinned_url(target, address)
        request_headers = dict(headers)
        request_headers["Host"] = _host_header(target)
        try:
            remaining = _remaining_seconds(deadline, target.logical_url)
            if before_request is not None:
                before_request()
            with httpx.Client(
                timeout=_bounded_timeout(timeout, remaining),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(
                    method,
                    pinned_url,
                    headers=request_headers,
                    data=form_data,
                    extensions={"sni_hostname": target.hostname},
                ) as streamed:
                    content = (
                        b""
                        if streamed.status_code in _REDIRECT_STATUSES
                        else _read_bounded(
                            streamed,
                            max_bytes,
                            target.logical_url,
                            deadline,
                        )
                    )
                    safe_request = httpx.Request(method, redact_url(target.logical_url))
                    return httpx.Response(
                        streamed.status_code,
                        headers=streamed.headers,
                        content=content,
                        request=safe_request,
                    )
        except HttpSafetyError:
            raise
        except httpx.HTTPError as exc:
            last_failure = exc.__class__.__name__

    detail = f" ({last_failure})" if last_failure else ""
    raise OutboundRequestFailed(
        f"Outbound request failed for {redact_url(target.logical_url)}{detail}"
    ) from None


def _pinned_url(
    target: _ValidatedTarget,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> str:
    host = f"[{address}]" if address.version == 6 else str(address)
    default_port = 443 if target.parsed.scheme.lower() == "https" else 80
    authority = host if target.port == default_port else f"{host}:{target.port}"
    return urlunsplit(
        (
            target.parsed.scheme.lower(),
            authority,
            target.parsed.path or "/",
            target.parsed.query,
            "",
        )
    )


def _host_header(target: _ValidatedTarget) -> str:
    host = f"[{target.hostname}]" if ":" in target.hostname else target.hostname
    default_port = 443 if target.parsed.scheme.lower() == "https" else 80
    return host if target.port == default_port else f"{host}:{target.port}"


def _bounded_timeout(timeout: httpx.Timeout | float, remaining: float) -> httpx.Timeout:
    configured = httpx.Timeout(timeout)

    def bounded(value: float | None) -> float:
        return remaining if value is None else min(value, remaining)

    return httpx.Timeout(
        connect=bounded(configured.connect),
        read=bounded(configured.read),
        write=bounded(configured.write),
        pool=bounded(configured.pool),
    )


def _remaining_seconds(deadline: float, logical_url: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OutboundRequestFailed(
            f"Outbound request time limit exceeded for {redact_url(logical_url)}"
        )
    return remaining


def _read_bounded(
    response: httpx.Response,
    max_bytes: int,
    logical_url: str,
    deadline: float,
) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise OutboundResponseTooLarge(
                    f"Outbound response exceeds the configured limit: {redact_url(logical_url)}"
                )
        except ValueError:
            pass

    content = bytearray()
    for chunk in response.iter_bytes(chunk_size=STREAM_CHUNK_BYTES):
        _remaining_seconds(deadline, logical_url)
        if len(content) + len(chunk) > max_bytes:
            raise OutboundResponseTooLarge(
                f"Outbound response exceeds the configured limit: {redact_url(logical_url)}"
            )
        content.extend(chunk)
    return bytes(content)
