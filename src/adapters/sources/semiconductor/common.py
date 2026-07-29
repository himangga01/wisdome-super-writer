from __future__ import annotations

import json
import mimetypes
import re
from datetime import UTC, datetime
from typing import Any, Iterable
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from defusedxml import ElementTree
from lxml import html

from adapters.sources.base import SourceAttachment
from adapters.sources.http import SourceSchemaError, parse_source_datetime
from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash


HTML_TYPES = ("text/html", "application/xhtml+xml")
FEED_TYPES = (
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
)
_WP_ID = re.compile(r"(?:[?&]p=|/wp-json/wp/v2/posts/)(\d+)(?:\D|$)")
_SPACE = re.compile(r"[ \t\r\f\v]+")


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def in_window(value: datetime | None, since: datetime, until: datetime) -> bool:
    return value is not None and utc(since) <= utc(value) <= utc(until)


def document(response):
    try:
        return html.fromstring(response.content, base_url=str(response.url))
    except (ValueError, TypeError) as exc:
        raise SourceSchemaError("Source HTML is malformed.") from exc


def decode_krx(response) -> str:
    payload = response.content
    if payload.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        return payload.decode("utf-8-sig" if payload.startswith(b"\xef") else "utf-16")
    header = response.headers.get("content-type", "")
    match = re.search(r"charset=([\w-]+)", header, re.I)
    if match:
        try:
            return payload.decode(match.group(1))
        except (LookupError, UnicodeDecodeError):
            raise SourceSchemaError("KRX declared encoding is invalid.") from None
    head = payload[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", head, re.I)
    if match:
        try:
            return payload.decode(match.group(1))
        except (LookupError, UnicodeDecodeError):
            raise SourceSchemaError("KRX meta encoding is invalid.") from None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("cp949")


def canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise SourceSchemaError("Source record URL is invalid.")
    path = re.sub(r"/+", "/", parsed.path or "/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def stable_text(node) -> str:
    for removable in node.xpath(
        ".//*[contains(concat(' ', normalize-space(@class), ' '), ' ai-summary ')"
        " or contains(concat(' ', normalize-space(@class), ' '), ' share ')"
        " or contains(concat(' ', normalize-space(@class), ' '), ' related ')"
        " or contains(@class, 'nonce') or self::script or self::style]"
    ):
        removable.drop_tree()
    lines: list[str] = []
    for element in node.iter():
        if element.tag == "tr":
            cells = [
                _SPACE.sub(" ", " ".join(cell.itertext())).strip()
                for cell in element.xpath("./th|./td")
            ]
            if cells:
                lines.append("\t".join(cells))
        elif element.tag in {"p", "li", "h1", "h2", "h3", "h4", "blockquote"}:
            text = _SPACE.sub(" ", " ".join(element.itertext())).strip()
            if text:
                lines.append(text)
    if not lines:
        text = _SPACE.sub(" ", " ".join(node.itertext())).strip()
        if text:
            lines.append(text)
    return "\n".join(dict.fromkeys(lines))


def wp_post_id(doc, *, expected: int | None = None) -> int:
    candidates: set[int] = set()
    for value in doc.xpath(
        "//link[@rel='shortlink']/@href | //link[@rel='alternate']/@href"
        " | //link[contains(@href, '/wp-json/wp/v2/posts/')]/@href"
    ):
        match = _WP_ID.search(str(value))
        if match:
            candidates.add(int(match.group(1)))
    for script in doc.xpath("//script[@type='application/ld+json']/text()"):
        for match in _WP_ID.finditer(script):
            candidates.add(int(match.group(1)))
        try:
            raw = json.loads(script)
        except (ValueError, TypeError):
            continue
        for item in _walk(raw):
            for key in ("postId", "wpPostId"):
                if isinstance(item.get(key), int):
                    candidates.add(item[key])
    if expected is not None:
        candidates.add(expected)
    if len(candidates) != 1:
        raise SourceSchemaError("WordPress provider identity markers disagree.")
    return next(iter(candidates))


def feed_entries(payload: bytes) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise SourceSchemaError("Source feed XML is malformed.") from exc
    root_name = _local(root.tag)
    if root_name not in {"rss", "feed", "rdf"}:
        raise SourceSchemaError("Source feed root is unsupported.")
    entries = [
        element for element in root.iter() if _local(element.tag) in {"item", "entry"}
    ]
    result: list[dict[str, Any]] = []
    for entry in entries:
        values: dict[str, list[str]] = {}
        links: list[dict[str, str]] = []
        for child in entry:
            name = _local(child.tag)
            if name == "link":
                href = child.attrib.get("href") or (child.text or "").strip()
                if href:
                    links.append(
                        {
                            "href": href,
                            "rel": child.attrib.get("rel", "alternate"),
                            "type": child.attrib.get("type", ""),
                        }
                    )
            text = "".join(child.itertext()).strip()
            if text:
                values.setdefault(name, []).append(text)
        alternate = next(
            (item["href"] for item in links if item["rel"] in {"", "alternate"}),
            None,
        ) or next(iter(values.get("link", [])), None)
        if not alternate or not values.get("title"):
            raise SourceSchemaError("Source feed item lacks title or canonical link.")
        published = next(
            iter(values.get("published", []) or values.get("pubdate", [])),
            None,
        )
        updated = next(iter(values.get("updated", [])), None)
        result.append(
            {
                "title": values["title"][0],
                "url": alternate,
                "id": next(iter(values.get("id", []) or values.get("guid", [])), ""),
                "published": parse_source_datetime(published),
                "updated": parse_source_datetime(updated),
                "kind": "atom" if root_name == "feed" else "rss",
            }
        )
    return result


def attachment(
    url: str,
    *,
    title: str,
    external_id: str,
    rights_status: str,
    locator: str,
    mime_type: str | None = None,
) -> SourceAttachment:
    resolved_mime = mime_type or mimetypes.guess_type(urlsplit(url).path)[0]
    return SourceAttachment(
        url=canonical_url(url),
        title=title,
        mime_type=resolved_mime,
        external_id=external_id,
        rights_status=rights_status,
        metadata={"metadataOnly": True, "locator": locator},
    )


def record_metadata(adapter, *, origin: str, syndication: str, kinds: Iterable[str]):
    return {
        "schemaVersion": "semiconductor-source-record-v1",
        "sourceContext": {
            "authorityTier": adapter.AUTHORITY_TIER,
            "independenceGroup": adapter.INDEPENDENCE_GROUP,
            "publisher": adapter.source.publisher,
        },
        "originIdentity": origin,
        "syndicationKind": syndication,
        "contentKinds": sorted(set(kinds)),
        "rights": {
            "status": adapter.RIGHTS_STATUS,
            "termsUrl": adapter.config.get("termsUrl"),
            "licenseUrl": adapter.config.get("licenseUrl"),
        },
    }


def checksum(value: Any) -> str:
    return canonical_hash(value, schema_version=CANONICAL_HASH_SCHEMA_V1)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
