import socket

import httpx
import pytest

from apps.policy_funding.catalog import source_by_id
from apps.policy_funding.transport import OfficialFundingFetcher
from wisdome_writer.infrastructure import http_safety


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        http_safety.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ],
    )


def test_robot_denial_prevents_notice_request(public_dns):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(
            200,
            text="User-agent: *\nDisallow: /ols/man/\n",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    fetcher = OfficialFundingFetcher(
        source_by_id("semas_agency"), transport=httpx.MockTransport(handle)
    )
    with pytest.raises(ValueError, match="robots"):
        fetcher.get("https://ols.semas.or.kr/ols/man/SMAN051M/page.do")
    assert calls == ["/robots.txt"]


def test_search_accepts_real_201_json_and_retains_notice_url(public_dns):
    def handle(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="missing", headers={"Content-Type": "text/html"})
        assert request.method == "POST"
        return httpx.Response(201, json={"result": []})

    fetcher = OfficialFundingFetcher(
        source_by_id("semas_agency"), transport=httpx.MockTransport(handle)
    )
    result = fetcher.post("https://ols.semas.or.kr/ols/man/SMAN051M/search.do", data={"type": "2"})
    assert result.url == "https://ols.semas.or.kr/ols/man/SMAN051M/search.do"
    assert result.text == '{"result":[]}'
    assert fetcher.request_count == 2


def test_invalid_source_encoding_is_not_silently_replaced(public_dns):
    def handle(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="missing", headers={"Content-Type": "text/html"})
        return httpx.Response(
            200,
            content=b"<html>loan \xff</html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    fetcher = OfficialFundingFetcher(
        source_by_id("semas_agency"), transport=httpx.MockTransport(handle)
    )
    with pytest.raises(ValueError, match="encoding"):
        fetcher.get("https://ols.semas.or.kr/ols/man/SMAN051M/page.do")


def test_physical_request_budget_stops_before_an_extra_request(public_dns):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(404, text="missing", headers={"Content-Type": "text/html"})

    fetcher = OfficialFundingFetcher(
        source_by_id("semas_agency"), max_requests=1, transport=httpx.MockTransport(handle)
    )
    with pytest.raises(ValueError, match="budget"):
        fetcher.get("https://ols.semas.or.kr/ols/man/SMAN051M/page.do")
    assert calls == ["/robots.txt"]
