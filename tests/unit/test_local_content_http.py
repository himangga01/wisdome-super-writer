from __future__ import annotations

import socket
from datetime import UTC, datetime

import httpx
import pytest

from apps.local_content import http as official_http
from apps.local_content.http import OfficialHtmlFetcher, OfficialSourceError
from wisdome_writer.infrastructure import http_safety


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        http_safety.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ],
    )


def test_fetcher_rejects_non_https_and_unapproved_hosts() -> None:
    fetcher = OfficialHtmlFetcher(allowed_hosts={"www.applyhome.co.kr"})

    with pytest.raises(OfficialSourceError, match="https"):
        fetcher.get("http://www.applyhome.co.kr/list")
    with pytest.raises(OfficialSourceError, match="host"):
        fetcher.get("https://127.0.0.1/list")


def test_fetcher_rejects_credentials_and_paths_outside_literal_prefix() -> None:
    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        path_prefixes=("/notices/",),
    )

    with pytest.raises(OfficialSourceError, match="credentials"):
        fetcher.get("https://user:secret@example.go.kr/notices/list")
    with pytest.raises(OfficialSourceError, match="path"):
        fetcher.get("https://example.go.kr/notices-archive/list")


def test_fetcher_matches_unicode_hosts_to_their_literal_idna_allowlist() -> None:
    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"xn--3e0b707e.go.kr"},
        path_prefixes=("/notices/",),
    )

    with pytest.raises(OfficialSourceError, match="path"):
        fetcher.get("https://한국.go.kr/outside")


def test_fetcher_returns_decoded_html_metadata_through_injected_transport(
    public_dns: None,
) -> None:
    payload = "<html><body>안녕</body></html>"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=euc-kr"},
            content=payload.encode("euc-kr"),
        )
    )
    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        path_prefixes=("/notices/",),
        transport=transport,
    )

    result = fetcher.get("https://example.go.kr/notices/list")

    assert result.url == "https://example.go.kr/notices/list"
    assert result.status_code == 200
    assert result.content_type == "text/html; charset=euc-kr"
    assert result.body == payload
    assert result.fetched_at.tzinfo is UTC
    assert result.fetched_at <= datetime.now(UTC)


def test_fetcher_sends_get_params_and_post_form_without_exposing_values(
    public_dns: None,
) -> None:
    def echo(request: httpx.Request) -> httpx.Response:
        material = f"{request.method} {request.url.query.decode()} {request.content.decode()}"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=f"<html>{material}</html>".encode(),
        )

    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        transport=httpx.MockTransport(echo),
    )

    get_result = fetcher.get(
        "https://example.go.kr/list?existing=yes",
        params={"serviceKey": "top-secret"},
    )
    post_result = fetcher.post(
        "https://example.go.kr/search",
        data={"query": "housing"},
    )

    assert "existing=yes&serviceKey=top-secret" in get_result.body
    assert "POST  query=housing" in post_result.body
    assert "top-secret" not in get_result.url


@pytest.mark.parametrize(
    ("headers", "content", "message"),
    [
        ({"Content-Type": "application/octet-stream"}, b"x", "content type"),
        (
            {"Content-Type": "text/html", "Content-Length": str(5 * 1024 * 1024 + 1)},
            b"x",
            "size limit",
        ),
    ],
)
def test_fetcher_rejects_non_html_or_oversized_responses(
    public_dns: None,
    headers: dict[str, str],
    content: bytes,
    message: str,
) -> None:
    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, content=content)
        ),
    )

    with pytest.raises(OfficialSourceError, match=message):
        fetcher.get("https://example.go.kr/list")


def test_fetcher_revalidates_redirect_target_scope(public_dns: None) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302,
            headers={"Location": "https://example.go.kr/private/secret"},
        )
    )
    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        path_prefixes=("/notices/",),
        transport=transport,
    )

    with pytest.raises(OfficialSourceError, match="path"):
        fetcher.get("https://example.go.kr/notices/list")


def test_fetcher_requires_status_200_and_redacts_query_values(public_dns: None) -> None:
    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        transport=httpx.MockTransport(lambda request: httpx.Response(404, content=b"missing")),
    )

    with pytest.raises(OfficialSourceError, match="status 404") as raised:
        fetcher.get("https://example.go.kr/list?serviceKey=top-secret")

    assert "top-secret" not in str(raised.value)


def test_fetcher_retries_retryable_status_and_honors_retry_after(
    public_dns: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter((503, 200))
    delays: list[float] = []
    monkeypatch.setattr(official_http, "sleep", delays.append, raising=False)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            next(statuses),
            headers={"Content-Type": "text/html", "Retry-After": "2"},
            content=b"<html>ready</html>",
        )
    )
    fetcher = OfficialHtmlFetcher(allowed_hosts={"example.go.kr"}, transport=transport)

    result = fetcher.get("https://example.go.kr/list")

    assert result.body == "<html>ready</html>"
    assert delays == [2.0]


def test_fetcher_limits_all_physical_requests_to_three(
    public_dns: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = 0
    monkeypatch.setattr(official_http, "sleep", lambda delay: None, raising=False)

    def redirect_forever(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            302,
            headers={"Location": f"https://example.go.kr/notices/{requests}"},
        )

    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        path_prefixes=("/notices/",),
        transport=httpx.MockTransport(redirect_forever),
    )

    with pytest.raises(OfficialSourceError, match="attempt limit"):
        fetcher.get("https://example.go.kr/notices/0")

    assert requests == 3


def test_fetcher_does_not_wait_past_total_deadline(
    public_dns: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(official_http, "sleep", delays.append, raising=False)
    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                429,
                headers={"Retry-After": "31"},
                content=b"busy",
            )
        ),
    )

    with pytest.raises(OfficialSourceError, match="time limit"):
        fetcher.get("https://example.go.kr/list")

    assert delays == []


def test_fetcher_rejects_invalid_declared_encoding(public_dns: None) -> None:
    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=not-a-codec"},
                content=b"<html></html>",
            )
        ),
    )

    with pytest.raises(OfficialSourceError, match="encoding"):
        fetcher.get("https://example.go.kr/list")


def test_fetcher_redacts_values_from_unexpected_transport_errors(public_dns: None) -> None:
    def fail_with_request_url(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"transport failed for {request.url}")

    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        transport=httpx.MockTransport(fail_with_request_url),
    )

    with pytest.raises(OfficialSourceError, match="request failed") as raised:
        fetcher.get("https://example.go.kr/list?serviceKey=top-secret")

    assert "top-secret" not in str(raised.value)


def test_fetcher_preserves_public_ip_validation_with_injected_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = 0
    monkeypatch.setattr(
        http_safety.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))
        ],
    )

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"ok")

    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(OfficialSourceError, match="non-public"):
        fetcher.get("https://example.go.kr/list")

    assert requests == 0


@pytest.mark.parametrize(
    "path",
    (
        "/notices/%2e%2e/private",
        "/notices/%2Fprivate",
        "/notices/..\\private",
    ),
)
def test_fetcher_rejects_ambiguous_or_traversing_paths(path: str) -> None:
    fetcher = OfficialHtmlFetcher(
        allowed_hosts={"example.go.kr"},
        path_prefixes=("/notices/",),
    )

    with pytest.raises(OfficialSourceError, match="path"):
        fetcher.get(f"https://example.go.kr{path}")
