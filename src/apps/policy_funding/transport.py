"""Bounded policy transport over the existing DNS-pinned HTTPS implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from apps.policy_funding.catalog import FundingSource
from wisdome_writer.infrastructure.http_safety import (
    safe_get,
    safe_post_form,
    validated_logical_url,
)


@dataclass(frozen=True)
class RetrievedPage:
    url: str
    text: str
    fetched_at: datetime


class OfficialFundingFetcher:
    def __init__(
        self,
        source: FundingSource,
        *,
        max_requests: int = 50,
        max_seconds: float = 90,
        transport=None,
    ) -> None:
        if not 1 <= max_requests <= 150 or not 1 <= max_seconds <= 300:
            raise ValueError("Funding request budget is invalid")
        self.source = source
        self.max_requests = max_requests
        self.deadline = monotonic() + max_seconds
        self.transport = transport
        self.request_count = 0
        self._robots = None
        self._robots_checked = False

    def _claim(self):
        if self.request_count >= self.max_requests or monotonic() >= self.deadline:
            raise ValueError("Official collection request/time budget exhausted")
        self.request_count += 1

    def _validate_outbound(self, url):
        self.source.validate_url(url)
        if self._robots is not None and not self._robots.can_fetch("WisdomePolicyResearch", url):
            raise ValueError("Official robots policy excludes this route")

    def _request(self, url, *, data=None, params=None, robots=False):
        self.source.validate_url(url)
        remaining = min(20.0, self.deadline - monotonic())
        if remaining <= 0:
            raise ValueError("Official collection time budget exhausted")
        options = dict(
            max_bytes=2 * 1024 * 1024,
            timeout=remaining,
            max_elapsed_seconds=remaining,
            allowed_hosts=set(self.source.allowed_hosts),
            https_only=True,
            url_validator=self._validate_outbound,
            before_request=self._claim,
            allowed_content_types={
                "text/html",
                "application/xhtml+xml",
                "application/json",
                "text/plain",
            },
            headers={"User-Agent": "WisdomePolicyResearch/1.0", "Accept-Encoding": "identity"},
            transport=self.transport,
        )
        if data is None:
            # safe_get has no params adapter; encode trusted collector parameters first.
            if params:
                import httpx

                url = str(httpx.URL(url).copy_merge_params(params))
            self._validate_outbound(url)
            response = safe_get(url, **options)
        else:
            response = safe_post_form(url, data=data, **options)
        if robots and response.status_code == 404:
            return None
        if response.status_code not in (200, 201):
            raise ValueError(f"Official source returned HTTP {response.status_code}")
        final_url = validated_logical_url(response)
        self.source.validate_url(final_url)
        if self._robots is not None and not self._robots.can_fetch(
            "WisdomePolicyResearch", final_url
        ):
            raise ValueError("Official robots policy excludes this route")
        try:
            text = response.content.decode(response.encoding or "utf-8", errors="strict")
        except (UnicodeError, LookupError) as exc:
            raise ValueError("Official source encoding is invalid") from exc
        return RetrievedPage(final_url, text, datetime.now(UTC))

    def _check_robots(self, url):
        if not self._robots_checked:
            host = urlsplit(self.source.entry_url).netloc
            page = self._request(f"https://{host}/robots.txt", robots=True)
            if page is not None:
                parser = RobotFileParser()
                parser.parse(page.text.splitlines())
                self._robots = parser
            self._robots_checked = True
        if self._robots is not None and not self._robots.can_fetch("WisdomePolicyResearch", url):
            raise ValueError("Official robots policy excludes this route")

    def get(self, url, *, params=None):
        self.source.validate_url(url)
        self._check_robots(url)
        return self._request(url, params=params)

    def post(self, url, *, data):
        self.source.validate_url(url)
        self._check_robots(url)
        return self._request(url, data=data)
