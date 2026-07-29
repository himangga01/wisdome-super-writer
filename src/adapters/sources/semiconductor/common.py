from __future__ import annotations

import codecs
import json
import re
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from defusedxml import ElementTree
from lxml import html

from adapters.sources.base import SourceAttachment
from adapters.sources.http import SourceSchemaError, parse_source_datetime
from wisdome_writer.domain.hashing import CANONICAL_HASH_SCHEMA_V1, canonical_hash


_WP_ID = re.compile(r"(?:[?&]p=|/wp-json/wp/v2/posts/|post-)(\d+)(?:\D|$)")
_SPACE = re.compile(r"[ \t\r\f\v]+")
_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "nonce",
        "ref",
        "source",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "credential",
        "key",
        "password",
        "secret",
        "signature",
        "token",
        "x-amz-credential",
        "x-amz-security-token",
        "x-amz-signature",
    }
)
_MOTIR_VIEW = re.compile(
    r"^(?:javascript:\s*)?article\.view\(['\"](?P<id>\d+)['\"]\)\s*;?$"
)
_MOTIR_ATTACHMENT = re.compile(
    r"^/attach/down/([0-9a-fA-F]{32})/([0-9a-fA-F]{32})/([0-9a-fA-F]{32})$"
)
_MOTIR_EXTENSION_MIMES = {
    ".pdf": "application/pdf",
    ".hwp": "application/x-hwp",
    ".hwpx": "application/vnd.hancom.hwpx",
    ".zip": "application/zip",
}


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
    """Decode KRX bytes in the frozen header → BOM → meta → fallback order."""

    payload = response.content
    header = response.headers.get("content-type", "")
    declared = re.search(r"charset\s*=\s*[\"']?([\w-]+)", header, re.I)
    if declared:
        try:
            return payload.decode(declared.group(1))
        except (LookupError, UnicodeDecodeError):
            raise SourceSchemaError("KRX declared encoding is invalid.") from None
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if payload.startswith(bom):
            try:
                return payload.decode(encoding)
            except UnicodeDecodeError:
                raise SourceSchemaError("KRX byte-order encoding is invalid.") from None
    head = payload[:4096].decode("ascii", errors="ignore")
    declared = re.search(r"charset\s*=\s*[\"']?([\w-]+)", head, re.I)
    if declared:
        try:
            return payload.decode(declared.group(1))
        except (LookupError, UnicodeDecodeError):
            raise SourceSchemaError("KRX meta encoding is invalid.") from None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return payload.decode("cp949")
        except UnicodeDecodeError:
            raise SourceSchemaError("KRX response encoding is unsupported.") from None


def canonical_source_url(
    value: str,
    *,
    hosts: Iterable[str],
    paths: Iterable[str],
    allowed_query_keys: Iterable[str] = (),
) -> str:
    """Validate and stabilize an approved source URL for requests or storage."""

    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    except (TypeError, ValueError):
        raise SourceSchemaError("Source URL is invalid.") from None
    hostname = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    approved_hosts = {
        str(host).rstrip(".").encode("idna").decode("ascii").lower()
        for host in hosts
    }
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or hostname not in approved_hosts
    ):
        raise SourceSchemaError("Source URL is outside the approved HTTPS host profile.")
    raw_path = parsed.path or "/"
    if _unsafe_encoded_path(raw_path):
        raise SourceSchemaError(
            "Source URL path contains encoded or literal traversal material."
        )
    path = re.sub(r"/+", "/", raw_path)
    if not any(
        (
            re.match(pattern, path) is not None
            if pattern.startswith("^")
            else re.fullmatch(pattern, path) is not None
        )
        for pattern in paths
    ):
        raise SourceSchemaError("Source URL path is outside the approved route profile.")
    approved_query = frozenset(allowed_query_keys)
    retained: list[tuple[str, str]] = []
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    normalized_query_keys = [key.lower() for key, _ in query_pairs]
    if len(normalized_query_keys) != len(set(normalized_query_keys)):
        raise SourceSchemaError("Source URL contains duplicate query fields.")
    for key, value in query_pairs:
        normalized = key.lower()
        if normalized in _SENSITIVE_QUERY_KEYS:
            raise SourceSchemaError("Source URL contains a credential-bearing query.")
        if normalized in _TRACKING_QUERY_KEYS:
            continue
        if key not in approved_query:
            raise SourceSchemaError("Source URL query is outside the approved route profile.")
        retained.append((key, value))
    return urlunsplit(
        (
            "https",
            hostname,
            path,
            urlencode(sorted(retained)),
            "",
        )
    )


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
            if element.xpath("ancestor::tr"):
                continue
            text = _SPACE.sub(" ", " ".join(element.itertext())).strip()
            if text:
                lines.append(text)
    if not lines:
        text = _SPACE.sub(" ", " ".join(node.itertext())).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def wp_post_id(
    doc,
    *,
    expected: int | None = None,
    additional_ids: Iterable[int] = (),
    include_jsonld: bool = True,
) -> int:
    candidates: set[int] = {int(value) for value in additional_ids}
    for value in doc.xpath(
        "//link[@rel='shortlink']/@href"
        " | //link[contains(@rel,'alternate')]/@href"
        " | //link[contains(@href, '/wp-json/wp/v2/posts/')]/@href"
    ):
        match = _WP_ID.search(str(value))
        if match:
            candidates.add(int(match.group(1)))
    if include_jsonld:
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
    if not candidates:
        raise SourceSchemaError("WordPress provider identity marker is missing.")
    if len(candidates) != 1:
        raise SourceSchemaError("WordPress provider identity markers disagree.")
    resolved = next(iter(candidates))
    if expected is not None and resolved != expected:
        raise SourceSchemaError("WordPress detail identity does not match its feed item.")
    return resolved


def wordpress_jsonld(
    doc,
    *,
    expected_identity: int | None = None,
    canonical_url: str | None = None,
) -> dict[str, Any]:
    identities: set[int] = set()
    published: list[datetime] = []
    modified: list[datetime] = []
    canonical_path = (
        urlsplit(canonical_url).path.rstrip("/")
        if canonical_url
        else None
    )
    for script in doc.xpath("//script[@type='application/ld+json']/text()"):
        try:
            raw = json.loads(script)
        except (ValueError, TypeError):
            continue
        for item in _walk(raw):
            raw_types = item.get("@type")
            item_types = (
                {str(value) for value in raw_types}
                if isinstance(raw_types, list)
                else {str(raw_types)}
            )
            if not item_types.intersection(
                {"Article", "NewsArticle", "BlogPosting", "Report"}
            ):
                continue
            item_ids: set[int] = set()
            for key in ("postId", "wpPostId"):
                if isinstance(item.get(key), int):
                    item_ids.add(item[key])
            item_urls: set[str] = set()
            for value in item.values():
                if isinstance(value, str):
                    for match in _WP_ID.finditer(value):
                        item_ids.add(int(match.group(1)))
                    if value.startswith(("https://", "http://", "/")):
                        item_urls.add(value)
                elif isinstance(value, Mapping):
                    for key in ("@id", "url"):
                        nested = value.get(key)
                        if isinstance(nested, str):
                            item_urls.add(nested)
                            for match in _WP_ID.finditer(nested):
                                item_ids.add(int(match.group(1)))
            matches_identity = (
                expected_identity is not None
                and expected_identity in item_ids
            )
            matches_canonical = (
                canonical_path is not None
                and any(
                    urlsplit(value).path.rstrip("/") == canonical_path
                    for value in item_urls
                )
            )
            if expected_identity is not None or canonical_path is not None:
                if not matches_identity and not matches_canonical:
                    continue
            identities.update(item_ids)
            for key, target in (("datePublished", published), ("dateModified", modified)):
                if key not in item:
                    continue
                parsed = parse_source_datetime(item.get(key))
                if parsed is None:
                    raise SourceSchemaError(
                        "Current article JSON-LD contains an invalid timestamp."
                    )
                target.append(parsed)
    published_values = {value.isoformat(): value for value in published}
    modified_values = {value.isoformat(): value for value in modified}
    if len(published_values) > 1 or len(modified_values) > 1:
        raise SourceSchemaError(
            "Current article JSON-LD contains conflicting timestamps."
        )
    return {
        "identities": sorted(identities),
        "published": next(iter(published_values.values()), None),
        "modified": next(iter(modified_values.values()), None),
    }


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
        enclosures: list[dict[str, str]] = []
        for child in entry:
            name = _local(child.tag)
            namespace = child.tag.split("}", 1)[0].lstrip("{") if "}" in child.tag else ""
            if name == "link":
                href = child.attrib.get("href") or (child.text or "").strip()
                if href:
                    link = {
                        "href": href,
                        "rel": child.attrib.get("rel", "alternate"),
                        "type": child.attrib.get("type", ""),
                    }
                    links.append(link)
                    if link["rel"] == "enclosure":
                        enclosures.append(_feed_asset(link, child.attrib))
            elif name == "enclosure" or (
                name in {"content", "thumbnail"}
                and ("search.yahoo.com/mrss" in namespace or child.attrib.get("url"))
            ):
                enclosures.append(_feed_asset(child.attrib, child.attrib))
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
                "enclosures": enclosures,
            }
        )
    return result


def parse_motir_list(
    doc,
    *,
    expected_total: int | None = None,
) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    total = (
        parse_motir_total(doc)
        if expected_total is None
        else expected_total
    )
    tables = doc.xpath("//table[@id='mytable']")
    if len(tables) != 1:
        raise SourceSchemaError("MOTIR official list table marker changed.")
    table = tables[0]
    headers = [
        _SPACE.sub("", " ".join(node.itertext())).strip()
        for node in table.xpath("./thead//th")
    ]
    title_indices = [
        index
        for index, value in enumerate(headers)
        if value in {"제목", "보도자료", "설명자료"}
    ]
    date_index = _header_index(headers, ("등록일", "게시일", "작성일"))
    bodies = table.xpath("./tbody")
    if not title_indices or date_index is None or len(bodies) != 1:
        raise SourceSchemaError("MOTIR official list headers or body changed.")
    rows = bodies[0].xpath("./tr")
    if not rows:
        raise SourceSchemaError("MOTIR official list body is empty.")
    if len(rows) == 1:
        row_text = _SPACE.sub(
            " ", " ".join(rows[0].itertext())
        ).strip()
        if (
            total == 0
            and not rows[0].xpath(".//a[@href or @onclick]")
            and re.search(
                r"(조회|검색).*(결과|내용|자료|내역).*(없습니다|없음)",
                row_text,
            )
        ):
            return []
    if total == 0:
        raise SourceSchemaError(
            "MOTIR zero total conflicts with its list rows."
        )
    for row in rows:
        cells = row.xpath("./th|./td")
        if max(*title_indices, date_index) >= len(cells):
            raise SourceSchemaError("MOTIR list row does not match its headers.")
        matched = []
        for title_index in title_indices:
            links = cells[title_index].xpath(".//a[@href or @onclick]")
            for link in links:
                targets = [
                    value
                    for value in (
                        link.attrib.get("href"),
                        link.attrib.get("onclick"),
                    )
                    if value
                ]
                matches = [
                    match
                    for value in targets
                    if (match := _MOTIR_VIEW.fullmatch(value.strip()))
                ]
                if len(matches) > 1 and any(
                    match.group("id") != matches[0].group("id")
                    for match in matches[1:]
                ):
                    raise SourceSchemaError(
                        "MOTIR list link identity markers disagree."
                    )
                if matches:
                    matched.append((link, matches[0].group("id")))
        if len(matched) != 1:
            raise SourceSchemaError(
                "MOTIR list row must expose exactly one article.view identity."
            )
        link, sequence = matched[0]
        title = _SPACE.sub(" ", " ".join(link.itertext())).strip()
        published = _SPACE.sub(" ", " ".join(cells[date_index].itertext())).strip()
        if not title or parse_source_datetime(published) is None:
            raise SourceSchemaError("MOTIR list title or date is invalid.")
        parsed.append(
            {"sequence": sequence, "title": title, "published": published}
        )
    if not parsed:
        raise SourceSchemaError("MOTIR official list table markers are missing.")
    return parsed


def parse_motir_total(doc) -> int:
    markers = doc.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' board-count ')]"
    )
    if len(markers) != 1:
        raise SourceSchemaError("MOTIR board-count marker changed.")
    text = _SPACE.sub(" ", " ".join(markers[0].itertext())).strip()
    values = {
        int(value.replace(",", ""))
        for value in re.findall(r"(?<!\d)([\d,]+)(?!\d)", text)
        if value.replace(",", "").isdigit()
    }
    if len(values) != 1:
        raise SourceSchemaError("MOTIR board-count total is ambiguous.")
    return next(iter(values))


def parse_motir_detail(doc, *, expected_sequence: str) -> dict[str, Any]:
    details = doc.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' board-detail ')]"
    )
    if len(details) != 1:
        raise SourceSchemaError("MOTIR board-detail marker changed.")
    detail = details[0]
    root = doc.getroottree().getroot()
    sequence_markers = root.xpath(
        "//*[@name='bbsSeqN' or @id='bbsSeqN']/@value"
    )
    board_markers = root.xpath(
        "//*[@name='bbsCdN' or @id='bbsCdN']/@value"
    )
    if (
        len(sequence_markers) != 1
        or sequence_markers[0] != expected_sequence
        or len(board_markers) != 1
        or board_markers[0] != "81"
    ):
        raise SourceSchemaError("MOTIR detail provider identity markers disagree.")
    title_scopes = detail.xpath(
        ".//*[contains(concat(' ', normalize-space(@class), ' '), ' detail-tit ')]"
    )
    info_scopes = detail.xpath(
        ".//*[contains(concat(' ', normalize-space(@class), ' '), ' detail-info ')]"
    )
    bodies = detail.xpath(
        ".//*[contains(concat(' ', normalize-space(@class), ' '), ' detail-cont ')"
        " and contains(concat(' ', normalize-space(@class), ' '), ' mViewerContents ')]"
    )
    if len(title_scopes) != 1 or len(info_scopes) != 1 or len(bodies) != 1:
        raise SourceSchemaError("MOTIR detail title, info, or body marker changed.")
    title = _SPACE.sub(" ", " ".join(title_scopes[0].itertext())).strip()
    info_text = _SPACE.sub(" ", " ".join(info_scopes[0].itertext())).strip()
    date_candidates = re.findall(
        r"(?<!\d)\d{4}[./-]\d{1,2}[./-]\d{1,2}(?!\d)",
        info_text,
    )
    parsed_dates = {
        value: parse_source_datetime(value)
        for value in date_candidates
        if parse_source_datetime(value) is not None
    }
    if len({value.isoformat() for value in parsed_dates.values()}) != 1:
        raise SourceSchemaError("MOTIR detail publication date is ambiguous.")
    published = next(iter(parsed_dates), None)
    if not title or published is None:
        raise SourceSchemaError("MOTIR detail title or publication date is missing.")
    body_text = stable_text(bodies[0])
    if not body_text:
        raise SourceSchemaError("MOTIR detail body is empty.")
    attachments: list[dict[str, str]] = []
    download_scopes = detail.xpath(
        ".//*[contains(concat(' ', normalize-space(@class), ' '), ' info-down ')]"
    )
    if len(download_scopes) > 1:
        raise SourceSchemaError("MOTIR attachment scope is ambiguous.")
    for link in (
        download_scopes[0].xpath(".//a[@href]")
        if download_scopes
        else []
    ):
        href = link.attrib["href"]
        match = _MOTIR_ATTACHMENT.fullmatch(urlsplit(href).path)
        if not match:
            continue
        filename = (
            link.attrib.get("download")
            or link.attrib.get("data-filename")
            or link.attrib.get("title")
            or _SPACE.sub(" ", " ".join(link.itertext())).strip()
        )
        current_suffix = (
            "." + filename.rsplit(".", 1)[-1].lower()
            if "." in filename
            else ""
        )
        if current_suffix not in _MOTIR_EXTENSION_MIMES:
            parent_text = _SPACE.sub(
                " ", " ".join(link.getparent().itertext())
            ).strip()
            candidate = re.search(
                r"([^\s/]+\.(?:pdf|hwp|hwpx|zip))\b",
                parent_text,
                re.I,
            )
            if candidate:
                filename = candidate.group(1)
        suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        declared_mime = link.attrib.get("data-mime")
        inferred_mime = _MOTIR_EXTENSION_MIMES.get(suffix)
        if (
            declared_mime
            and inferred_mime
            and declared_mime.lower() != inferred_mime
        ):
            raise SourceSchemaError(
                "MOTIR attachment MIME markers disagree."
            )
        mime_type = declared_mime or inferred_mime
        if not filename or not mime_type:
            raise SourceSchemaError("MOTIR attachment MIME cannot be determined.")
        item = {
            "href": href,
            "title": filename,
            "mimeType": mime_type.lower(),
            "externalId": "motie:file:" + ":".join(match.groups()).lower(),
        }
        if any(
            value["externalId"] == item["externalId"]
            and value != item
            for value in attachments
        ):
            raise SourceSchemaError("MOTIR attachment identity markers disagree.")
        if not any(value["externalId"] == item["externalId"] for value in attachments):
            attachments.append(item)
    return {
        "title": title,
        "published": published,
        "body": body_text,
        "bodyNode": bodies[0],
        "attachments": attachments,
    }


def parse_krx_document_lineage(value) -> list[dict[str, str]]:
    pairs: list[tuple[str, str]] = []
    if hasattr(value, "xpath"):
        for role in ("mainDoc", "attachedDoc"):
            for raw in value.xpath(
                f"//*[@id='{role}' or @name='{role}']/@value"
            ):
                pairs.append((role, raw))
        source = html.tostring(value, encoding="unicode")
    else:
        source = json.dumps(value, ensure_ascii=False) if isinstance(value, Mapping) else str(value)
    for role in ("mainDoc", "attachedDoc"):
        patterns = (
            rf"(?:var\s+)?{role}\s*=\s*['\"]([^'\"]+)['\"]",
            rf"['\"]{role}['\"]\s*:\s*['\"]([^'\"]+)['\"]",
        )
        for pattern in patterns:
            for raw in re.findall(pattern, source):
                pairs.append((role, raw))
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    doc_contract: dict[str, tuple[str, str]] = {}
    for role, raw in pairs:
        matches = re.findall(r"(?<!\d)(\d+)\|([YN])(?![A-Z])", raw)
        if not matches:
            raise SourceSchemaError(f"KRX {role} has no exact docNo lineage marker.")
        for doc_no, lineage in matches:
            existing = doc_contract.get(doc_no)
            if existing is not None and existing != (role, lineage):
                raise SourceSchemaError(
                    "KRX document role or lineage markers disagree."
                )
            doc_contract[doc_no] = (role, lineage)
            key = (role, doc_no, lineage)
            if key not in seen:
                seen.add(key)
                result.append({"role": role, "docNo": doc_no, "lineage": lineage})
    if not result or not any(item["role"] == "mainDoc" for item in result):
        raise SourceSchemaError("KRX mainDoc lineage marker is missing.")
    return result


def parse_sia_list(doc) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    items = doc.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' resource-item ')]"
    )
    if not items:
        raise SourceSchemaError("SIA list page has no resource items.")
    for item in items:
        links = item.xpath(".//a[h3][@href]")
        metadata = item.xpath(
            ".//*[contains(concat(' ', normalize-space(@class), ' '),"
            " ' resource-item-meta ')]"
        )
        if len(links) != 1 or len(metadata) != 1:
            raise SourceSchemaError("SIA resource item markers changed.")
        headings = links[0].xpath("./h3")
        if len(headings) != 1:
            raise SourceSchemaError("SIA resource title marker changed.")
        title = _SPACE.sub(" ", " ".join(headings[0].itertext())).strip()
        date_text = _SPACE.sub(" ", " ".join(metadata[0].itertext())).strip()
        parsed_date = _flexible_date(date_text)
        if not title or parsed_date is None:
            raise SourceSchemaError("SIA resource title or date is invalid.")
        result.append(
            {
                "url": links[0].attrib["href"],
                "title": title,
                "published": parsed_date.isoformat(),
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
    mime_type: str,
    metadata: Mapping[str, Any] | None = None,
) -> SourceAttachment:
    if not mime_type:
        raise SourceSchemaError("Source attachment MIME is required.")
    return SourceAttachment(
        url=url,
        title=title,
        mime_type=mime_type.lower(),
        external_id=external_id,
        rights_status=rights_status,
        metadata={
            "metadataOnly": True,
            "locator": locator,
            **dict(metadata or {}),
        },
    )


def record_metadata(adapter, *, origin: str, syndication: str, kinds: Iterable[str]):
    return {
        "schemaVersion": "semiconductor-source-record-v1",
        "sourceContext": {
            "authorityTier": adapter.source.authority_tier,
            "independenceGroup": adapter.source.independence_group,
            "publisher": adapter.source.publisher,
            "displayName": adapter.source.display_name,
            "ownerName": adapter.source.owner_name,
            "editorialControlName": adapter.source.editorial_control_name,
        },
        "originIdentity": origin,
        "syndicationKind": syndication,
        "contentKinds": sorted(set(kinds)),
        "rights": {
            "status": adapter.config["rightsStatus"],
            "termsUrl": adapter.config.get("termsUrl"),
            "licenseUrl": adapter.config.get("licenseUrl"),
        },
    }


def checksum(value: Any) -> str:
    return canonical_hash(value, schema_version=CANONICAL_HASH_SCHEMA_V1)


def _feed_asset(values: Mapping[str, str], attributes: Mapping[str, str]) -> dict[str, str]:
    url = values.get("href") or values.get("url")
    mime_type = values.get("type") or attributes.get("type")
    if not url or not mime_type:
        raise SourceSchemaError("Feed enclosure lacks URL or MIME.")
    result = {"url": url, "mimeType": mime_type.lower()}
    declared = attributes.get("length") or attributes.get("fileSize")
    if declared:
        result["declaredLength"] = declared
    return result


def _header_index(headers: list[str], names: Iterable[str]) -> int | None:
    normalized_names = {_SPACE.sub("", value) for value in names}
    return next(
        (index for index, value in enumerate(headers) if value in normalized_names),
        None,
    )


def _flexible_date(value: str) -> datetime | None:
    parsed = parse_source_datetime(value)
    if parsed is not None:
        return parsed
    for pattern in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value.strip(), pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _unsafe_encoded_path(value: str) -> bool:
    current = value
    for _ in range(8):
        if (
            "\\" in current
            or re.search(r"%(?:2e|2f|5c)", current, re.I)
            or any(
                segment in {".", ".."}
                for segment in current.split("/")
            )
        ):
            return True
        decoded = unquote(current)
        if decoded == current:
            return False
        current = decoded
    return True


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
