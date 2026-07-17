from __future__ import annotations

import email.utils
import ipaddress
import socket
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from defusedxml import ElementTree

from .base import CollectedSourceRecord, SourceAttachment


MAX_RESPONSE_BYTES = 12 * 1024 * 1024
ATTACHMENT_EXTENSIONS = (".pdf", ".hwp", ".hwpx", ".xlsx", ".xls", ".csv")


def _public_host(host: str) -> bool:
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return False
    return True


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC)
        except ValueError:
            return None


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._in_title = False
        self._current_href: str | None = None
        self._current_link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "a" and values.get("href"):
            self._current_href = values["href"]
            self._current_link_text = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._current_href:
            self.links.append((self._current_href, " ".join(self._current_link_text).strip()))
            self._current_href = None
            self._current_link_text = []

    def handle_data(self, data):
        clean = " ".join(data.split())
        if not clean:
            return
        self.text.append(clean)
        if self._in_title:
            self.title += (" " if self.title else "") + clean
        if self._current_href:
            self._current_link_text.append(clean)


class HttpSourceAdapter:
    def __init__(self, *, source, config):
        self.source = source
        self.config = config
        self.allowed_hosts = {urlparse(source.base_url).hostname}
        self.timeout = httpx.Timeout(20, connect=10)

    def _get(self, url: str) -> httpx.Response:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts:
            raise ValueError(f"URL outside approved source host: {url}")
        if not _public_host(parsed.hostname):
            raise ValueError(f"Source host does not resolve to a public address: {parsed.hostname}")
        headers = {"User-Agent": "WisdomeSuperWriter/0.1 (+admin-managed research bot)"}
        with httpx.Client(timeout=self.timeout, follow_redirects=False, headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ValueError("Source response exceeds the configured safety limit")
        return response


class PublicHtmlAdapter(HttpSourceAdapter):
    def collect(self, *, since: datetime, until: datetime) -> list[CollectedSourceRecord]:
        now = datetime.now(UTC)
        records: list[CollectedSourceRecord] = []
        for entrypoint in self.config.get("entrypoints", []):
            response = self._get(entrypoint)
            parser = _PageParser()
            parser.feed(response.text)
            attachments = tuple(
                SourceAttachment(url=urljoin(entrypoint, href), title=label or href.rsplit("/", 1)[-1])
                for href, label in parser.links
                if urlparse(href).path.lower().endswith(ATTACHMENT_EXTENSIONS)
            )
            records.append(
                CollectedSourceRecord(
                    external_id=response.headers.get("ETag") or entrypoint,
                    canonical_url=entrypoint,
                    title=parser.title or self.source.display_name,
                    publisher=self.source.owner_name,
                    published_at=_parse_date(response.headers.get("Last-Modified")),
                    collected_at=now,
                    body_text="\n".join(parser.text),
                    metadata={"contentType": response.headers.get("Content-Type", "text/html")},
                    attachments=attachments,
                )
            )
        return records


class RssAdapter(HttpSourceAdapter):
    def collect(self, *, since: datetime, until: datetime) -> list[CollectedSourceRecord]:
        now = datetime.now(UTC)
        records: list[CollectedSourceRecord] = []
        for entrypoint in self.config.get("entrypoints", []):
            root = ElementTree.fromstring(self._get(entrypoint).content)
            for item in root.findall(".//item"):
                link = (item.findtext("link") or "").strip()
                published = _parse_date(item.findtext("pubDate"))
                if published and not (since <= published <= until):
                    continue
                records.append(
                    CollectedSourceRecord(
                        external_id=(item.findtext("guid") or link).strip(),
                        canonical_url=link,
                        title=(item.findtext("title") or "제목 없음").strip(),
                        publisher=self.source.owner_name,
                        published_at=published,
                        collected_at=now,
                        body_text=(item.findtext("description") or "").strip(),
                    )
                )
        return records


class OpenDataJsonAdapter(HttpSourceAdapter):
    def collect(self, *, since: datetime, until: datetime) -> list[CollectedSourceRecord]:
        now = datetime.now(UTC)
        records: list[CollectedSourceRecord] = []
        for entrypoint in self.config.get("entrypoints", []):
            payload = self._get(entrypoint).json()
            rows = payload.get("items", payload if isinstance(payload, list) else [])
            for row in rows:
                url = row.get("url") or entrypoint
                records.append(
                    CollectedSourceRecord(
                        external_id=str(row.get("id") or row.get("noticeId") or url),
                        canonical_url=url,
                        title=str(row.get("title") or row.get("name") or "제목 없음"),
                        publisher=self.source.owner_name,
                        published_at=_parse_date(row.get("publishedAt") or row.get("date")),
                        collected_at=now,
                        body_text=str(row.get("content") or row.get("summary") or ""),
                        metadata={"structured": row},
                    )
                )
        return records
