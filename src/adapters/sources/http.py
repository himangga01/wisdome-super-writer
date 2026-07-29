from __future__ import annotations

import email.utils
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from defusedxml import ElementTree

from wisdome_writer.infrastructure.http_safety import (
    redact_url,
    redact_url_values,
    redact_urls_in_text,
    safe_get,
)

from .base import CollectedSourceRecord, SourceAttachment

MAX_RESPONSE_BYTES = 12 * 1024 * 1024
ATTACHMENT_EXTENSIONS = (".pdf", ".hwp", ".hwpx", ".xlsx", ".xls", ".csv")


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
        self.allowed_hosts = {host for host in (urlparse(source.base_url).hostname,) if host}
        self.timeout = httpx.Timeout(20, connect=10)

    def _get(self, url: str) -> httpx.Response:
        headers = {"User-Agent": "WisdomeSuperWriter/0.1 (+admin-managed research bot)"}
        response = safe_get(
            url,
            max_bytes=MAX_RESPONSE_BYTES,
            timeout=self.timeout,
            allowed_hosts=self.allowed_hosts,
            headers=headers,
            max_elapsed_seconds=20.0,
        )
        response.raise_for_status()
        return response


class PublicHtmlAdapter(HttpSourceAdapter):
    def collect(self, *, since: datetime, until: datetime) -> list[CollectedSourceRecord]:
        now = datetime.now(UTC)
        records: list[CollectedSourceRecord] = []
        for entrypoint in self.config.get("entrypoints", []):
            response = self._get(entrypoint)
            safe_entrypoint = redact_url(entrypoint)
            parser = _PageParser()
            parser.feed(response.text)
            attachments: list[SourceAttachment] = []
            for href, label in parser.links:
                if not urlparse(href).path.lower().endswith(ATTACHMENT_EXTENSIONS):
                    continue
                safe_attachment_url = redact_url(urljoin(entrypoint, href))
                safe_fallback_title = urlparse(safe_attachment_url).path.rsplit("/", 1)[-1]
                attachments.append(
                    SourceAttachment(
                        url=safe_attachment_url,
                        title=redact_urls_in_text(label) if label else safe_fallback_title,
                    )
                )
            records.append(
                CollectedSourceRecord(
                    external_id=response.headers.get("ETag") or safe_entrypoint,
                    canonical_url=safe_entrypoint,
                    title=parser.title or self.source.display_name,
                    publisher=self.source.publisher,
                    published_at=_parse_date(response.headers.get("Last-Modified")),
                    collected_at=now,
                    body_text="\n".join(parser.text),
                    metadata={"contentType": response.headers.get("Content-Type", "text/html")},
                    attachments=tuple(attachments),
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
                safe_link = redact_url(link)
                guid = (item.findtext("guid") or "").strip()
                published = _parse_date(item.findtext("pubDate"))
                if published and not (since <= published <= until):
                    continue
                records.append(
                    CollectedSourceRecord(
                        external_id=redact_urls_in_text(guid) if guid else safe_link,
                        canonical_url=safe_link,
                        title=(item.findtext("title") or "제목 없음").strip(),
                        publisher=self.source.publisher,
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
                safe_url = redact_url(str(url))
                identity = row.get("id") or row.get("noticeId")
                records.append(
                    CollectedSourceRecord(
                        external_id=(
                            redact_urls_in_text(str(identity)) if identity is not None else safe_url
                        ),
                        canonical_url=safe_url,
                        title=str(row.get("title") or row.get("name") or "제목 없음"),
                        publisher=self.source.publisher,
                        published_at=_parse_date(row.get("publishedAt") or row.get("date")),
                        collected_at=now,
                        body_text=str(row.get("content") or row.get("summary") or ""),
                        metadata={"structured": redact_url_values(row)},
                    )
                )
        return records
