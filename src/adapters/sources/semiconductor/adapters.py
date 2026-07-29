from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from adapters.sources.base import CollectedSourceRecord
from adapters.sources.http import HttpSourceAdapter, SourceSchemaError, parse_source_datetime

from .common import (
    FEED_TYPES,
    HTML_TYPES,
    attachment,
    canonical_url,
    checksum,
    decode_krx,
    document,
    feed_entries,
    in_window,
    record_metadata,
    stable_text,
    utc,
    wp_post_id,
)


class SemiconductorAdapter(HttpSourceAdapter):
    AUTHORITY_TIER = ""
    INDEPENDENCE_GROUP = ""
    RIGHTS_STATUS = ""

    def __init__(self, *, source, config):
        super().__init__(source=source, config=config)
        self.max_pages = _positive(config.get("maxPages", 5), "maxPages", 100)
        self.page_size = _positive(config.get("pageSize", 50), "pageSize", 100)
        self.reconciliation_ids = frozenset(config.get("_reconciliationExternalIds", []))
        self.reconciliation_days = _positive(
            config.get("reconciliationDays", 30), "reconciliationDays", 3660
        )

    def _finalize(self, records):
        seen: set[str] = set()
        result = []
        for record in sorted(records, key=lambda item: item.external_id):
            if record.external_id in seen:
                raise SourceSchemaError("Source returned a duplicate provider identity.")
            seen.add(record.external_id)
            result.append(record)
        return result


class MotirAdapter(SemiconductorAdapter):
    AUTHORITY_TIER = "primary_official"
    INDEPENDENCE_GROUP = "kr-motie"
    RIGHTS_STATUS = "attribution_required"
    _ITEM = re.compile(r"^\s*article\.view\(['\"](?P<id>\d+)['\"]\)\s*;?\s*$")
    _ATTACH = re.compile(r"^/attach/down/([0-9a-fA-F]{32})/([0-9a-fA-F]{32})/([0-9a-fA-F]{32})$")

    def collect(self, *, since: datetime, until: datetime):
        records = []
        discovered: set[str] = set()
        for entrypoint in self.entrypoints:
            for page in range(1, self.max_pages + 1):
                response = self._get(
                    entrypoint,
                    params={
                        "pageIndex": page,
                        "rowPageC": self.page_size,
                        "startDtD": utc(since).strftime("%Y-%m-%d"),
                        "endDtD": utc(until).strftime("%Y-%m-%d"),
                    },
                    expected_content_types=HTML_TYPES,
                )
                doc = document(response)
                items = []
                for node in doc.xpath("//*[@onclick]"):
                    match = self._ITEM.fullmatch(node.attrib.get("onclick", ""))
                    if match:
                        items.append((match.group("id"), " ".join(node.itertext()).strip()))
                if not items:
                    if page == 1:
                        raise SourceSchemaError("MOTIR list markers are missing.")
                    break
                page_ids = {f"motie:81:{item[0]}" for item in items}
                if discovered.intersection(page_ids):
                    raise SourceSchemaError("MOTIR pagination repeated an identity.")
                discovered.update(page_ids)
                for sequence, list_title in items:
                    record = self._detail(entrypoint, sequence, list_title, since, until)
                    if record is not None:
                        records.append(record)
                if len(items) < self.page_size:
                    break
        return self._finalize(records)

    def _detail(self, entrypoint, sequence, list_title, since, until):
        url = _with_query(entrypoint, {"bbsSeqN": sequence, "bbsCdN": "81"})
        response = self._get(url, expected_content_types=HTML_TYPES)
        doc = document(response)
        marker = doc.xpath(
            "//*[@name='bbsSeqN']/@value | //*[@id='bbsSeqN']/@value"
        )
        if marker and marker[0] != sequence:
            raise SourceSchemaError("MOTIR detail identity does not match its list item.")
        bodies = doc.xpath(
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' board-detail ')]"
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' detail-cont ')"
            " and contains(concat(' ', normalize-space(@class), ' '), ' mViewerContents ')]"
        )
        if len(bodies) != 1:
            raise SourceSchemaError("MOTIR detail body marker changed.")
        body = stable_text(bodies[0])
        title_nodes = doc.xpath("//h1|//h2[contains(@class,'title')]|//title")
        title = (" ".join(title_nodes[0].itertext()).strip() if title_nodes else list_title)
        page_text = " ".join(doc.itertext())
        dates = re.findall(r"\b20\d{2}[.-]\d{1,2}[.-]\d{1,2}\b", page_text)
        published = parse_source_datetime(dates[0]) if dates else None
        external_id = f"motie:81:{sequence}"
        reconciliation = external_id in self.reconciliation_ids
        if not in_window(published, since, until) and not reconciliation:
            return None
        attachments = []
        for index, link in enumerate(bodies[0].xpath(".//a[@href]"), 1):
            href = link.attrib["href"]
            match = self._ATTACH.fullmatch(urlsplit(href).path)
            if not match:
                continue
            attachments.append(
                attachment(
                    urljoin(url, href),
                    title=" ".join(link.itertext()).strip() or f"attachment-{index}",
                    external_id="motie:file:" + ":".join(match.groups()).lower(),
                    rights_status=self.RIGHTS_STATUS,
                    locator=f"attachment:{index}",
                )
            )
        metadata = record_metadata(
            self, origin=external_id, syndication="official_html", kinds=_kinds(bodies[0], attachments)
        )
        material = {"title": title, "body": body, "attachments": [item.external_id for item in attachments], "metadata": metadata}
        return CollectedSourceRecord(
            external_id=external_id,
            canonical_url=canonical_url(url),
            title=title,
            publisher=self.source.publisher,
            published_at=published,
            collected_at=datetime.now(UTC),
            body_text=body,
            reconciliation_only=not in_window(published, since, until),
            raw_checksum=checksum(material),
            metadata=metadata,
            attachments=tuple(attachments),
        )


class KrxKindAdapter(SemiconductorAdapter):
    AUTHORITY_TIER = "primary_regulatory"
    INDEPENDENCE_GROUP = "krx-kind"
    RIGHTS_STATUS = "internal_analysis_only"
    _ITEM = re.compile(r"^\s*openDisclsViewer\(['\"](\d{14})['\"],\s*['\"]['\"]\)\s*;?\s*$")

    def collect(self, *, since: datetime, until: datetime):
        endpoint = self.entrypoints[0]
        issuer_codes = self.config.get("issuerCodes")
        if issuer_codes != ["A005930", "A000660"]:
            raise SourceSchemaError("KRX issuer scope is not the approved frozen set.")
        records = []
        seen: set[str] = set()
        for issuer in issuer_codes:
            for page in range(1, self.max_pages + 1):
                data = {
                    "method": "searchDetailsSub",
                    "forward": "details_sub",
                    "currentPageSize": self.page_size,
                    "pageIndex": page,
                    "fromDate": utc(since).strftime("%Y%m%d"),
                    "toDate": utc(until).strftime("%Y%m%d"),
                    "repIsuSrtCd": issuer,
                    "marketType": "",
                    "searchType": "",
                    "searchCorpName": "",
                }
                response = self._post_form(
                    urljoin(endpoint, "/disclosure/details.do"),
                    data=data,
                    expected_content_types=HTML_TYPES,
                )
                text = decode_krx(response)
                doc = document(_TextResponse(response, text))
                items = []
                for node in doc.xpath("//*[@onclick]"):
                    match = self._ITEM.fullmatch(node.attrib.get("onclick", ""))
                    if match:
                        items.append((match.group(1), " ".join(node.itertext()).strip()))
                if not items:
                    if page == 1:
                        raise SourceSchemaError("KRX list markers are missing.")
                    break
                for acpt_no, title in items:
                    external_id = f"krx-kind:{acpt_no}"
                    if external_id in seen:
                        raise SourceSchemaError("KRX pagination returned a duplicate identity.")
                    seen.add(external_id)
                    records.append(self._detail(endpoint, acpt_no, title))
                if len(items) < self.page_size:
                    break
        return self._finalize(records)

    def _detail(self, endpoint, acpt_no, list_title):
        base = urljoin(endpoint, "/disclosure/details.do")
        init = self._get(
            base,
            params={"method": "searchInitInfo", "acptno": acpt_no},
            expected_content_types=HTML_TYPES,
        )
        text = decode_krx(init)
        if acpt_no not in text:
            raise SourceSchemaError("KRX detail identity marker is missing.")
        doc = document(_TextResponse(init, text))
        documents = []
        for index, node in enumerate(doc.xpath("//*[@docno or @data-docno or contains(@onclick,'docNo')]"), 1):
            raw = " ".join(node.attrib.values())
            match = re.search(r"(\d+)\|([YN])", raw) or re.search(r"docNo\D+(\d+)", raw)
            if not match:
                continue
            doc_no = match.group(1)
            lineage = match.group(2) if match.lastindex and match.lastindex > 1 else "Y"
            content = self._get(
                base,
                params={"method": "searchContents", "acptno": acpt_no, "docNo": doc_no},
                expected_content_types=HTML_TYPES,
            )
            content_text = decode_krx(content)
            if "<html" not in content_text.lower():
                raise SourceSchemaError("KRX document wrapper marker is missing.")
            content_doc = document(_TextResponse(content, content_text))
            documents.append(
                {
                    "docNo": doc_no,
                    "lineage": lineage,
                    "body": stable_text(content_doc),
                    "locator": f"document:{index}",
                }
            )
        if not documents:
            raise SourceSchemaError("KRX detail has no main or attached document.")
        body = "\n".join(item["body"] for item in documents if item["body"])
        title = list_title or f"KRX disclosure {acpt_no}"
        corrected = title.strip().startswith("[정정]")
        origin = f"krx-kind:{acpt_no}"
        metadata = record_metadata(self, origin=origin, syndication="regulatory_html", kinds=("html",))
        metadata["documentLineage"] = [{k: item[k] for k in ("docNo", "lineage", "locator")} for item in documents]
        metadata["correction"] = corrected
        return CollectedSourceRecord(
            external_id=origin,
            canonical_url=canonical_url(
                _with_query(urljoin(endpoint, "/disclosure/details.do"), {"method": "searchDetailsMain", "acptno": acpt_no})
            ),
            title=title,
            publisher=self.source.publisher,
            published_at=parse_source_datetime(acpt_no[:8]),
            collected_at=datetime.now(UTC),
            body_text=body,
            status="corrected" if corrected else "active",
            raw_checksum=checksum({"body": body, "metadata": metadata}),
            metadata=metadata,
        )


class WordPressNewsroomAdapter(SemiconductorAdapter):
    ID_PREFIX = ""
    BODY_CLASS = ""
    TITLE_CLASS = ""
    DETAIL_ID_PATTERN: re.Pattern[str]

    def collect(self, *, since: datetime, until: datetime):
        merged: dict[str, dict] = {}
        for feed_url in self.entrypoints:
            for page in range(1, self.max_pages + 1):
                response = self._get(
                    _with_query(feed_url, {"paged": page}),
                    expected_content_types=FEED_TYPES,
                )
                entries = feed_entries(response.content)
                if not entries:
                    if page == 1:
                        raise SourceSchemaError("Newsroom feed is empty.")
                    break
                for item in entries:
                    url = canonical_url(item["url"])
                    if url in merged:
                        merged[url]["aliases"].add(item["id"])
                        merged[url]["kinds"].add(item["kind"])
                        if item["published"]:
                            merged[url]["published"] = item["published"]
                        if item["updated"]:
                            merged[url]["updated"] = item["updated"]
                    else:
                        merged[url] = {
                            **item,
                            "aliases": {item["id"]} if item["id"] else set(),
                            "kinds": {item["kind"]},
                        }
                if len(entries) < self.page_size:
                    break
        records = []
        for item in merged.values():
            published = item["published"]
            expected = self._feed_wp_id(item)
            external_id = f"{self.ID_PREFIX}{expected}" if expected else ""
            reconciliation = external_id in self.reconciliation_ids
            if not in_window(published, since, until) and not reconciliation:
                continue
            records.append(self._detail(item, expected, since, until))
        return self._finalize(records)

    def _feed_wp_id(self, item):
        for value in [item["id"], item["url"], *item["aliases"]]:
            match = re.search(r"(?:[?&]p=|/archives/|post-)(\d+)(?:\D|$)", value or "")
            if match:
                return int(match.group(1))
        return None

    def _detail(self, item, expected, since, until):
        response = self._get(item["url"], expected_content_types=HTML_TYPES)
        doc = document(response)
        match = self.DETAIL_ID_PATTERN.search(" ".join(doc.xpath("//article/@id")))
        article_id = int(match.group(1)) if match else expected
        post_id = wp_post_id(doc, expected=article_id)
        origin = f"{self.ID_PREFIX}{post_id}"
        canonical_values = doc.xpath("//link[@rel='canonical']/@href")
        canonical = canonical_url(canonical_values[0] if canonical_values else item["url"])
        bodies = doc.xpath(
            f"//*[contains(concat(' ', normalize-space(@class), ' '), ' {self.BODY_CLASS} ')]"
        )
        titles = doc.xpath(
            f"//*[contains(concat(' ', normalize-space(@class), ' '), ' {self.TITLE_CLASS} ')]"
        )
        if len(bodies) != 1 or not titles:
            raise SourceSchemaError("Newsroom detail DOM markers changed.")
        body = stable_text(bodies[0])
        title = " ".join(titles[0].itertext()).strip()
        attachments = []
        for index, node in enumerate(bodies[0].xpath(".//a[@href]|.//img[@src]"), 1):
            href = node.attrib.get("href") or node.attrib.get("src")
            path = urlsplit(href).path.lower()
            if not path.endswith((".pdf", ".xls", ".xlsx", ".png", ".jpg", ".jpeg", ".webp")):
                continue
            attachments.append(
                attachment(
                    urljoin(canonical, href),
                    title=node.attrib.get("alt") or " ".join(node.itertext()).strip() or f"media-{index}",
                    external_id=f"{origin}:media:{checksum(canonical_url(urljoin(canonical, href)))[:20]}",
                    rights_status=self.RIGHTS_STATUS,
                    locator=f"body-media:{index}",
                )
            )
        published = item["published"]
        updated = item["updated"]
        metadata = record_metadata(self, origin=origin, syndication="+".join(sorted(item["kinds"])), kinds=_kinds(bodies[0], attachments))
        metadata["providerAliases"] = sorted(value for value in item["aliases"] if value)
        metadata["storedExternalIdentity"] = {"wpPostId": post_id, "canonicalPath": urlsplit(canonical).path}
        return CollectedSourceRecord(
            external_id=origin,
            canonical_url=canonical,
            title=title,
            publisher=self.source.publisher,
            published_at=published,
            modified_at=updated if updated and (not published or updated >= published) else None,
            collected_at=datetime.now(UTC),
            body_text=body,
            reconciliation_only=not in_window(published, since, until),
            raw_checksum=checksum({"body": body, "attachments": [item.external_id for item in attachments], "metadata": metadata}),
            metadata=metadata,
            attachments=tuple(attachments),
        )


class SamsungNewsroomAdapter(WordPressNewsroomAdapter):
    AUTHORITY_TIER = "primary_corporate"
    INDEPENDENCE_GROUP = "samsung"
    RIGHTS_STATUS = "internal_analysis_only"
    ID_PREFIX = "samsung-global:wp-post:"
    BODY_CLASS = "single_contents"
    TITLE_CLASS = "single-title"
    DETAIL_ID_PATTERN = re.compile(r"post-(\d+)")


class SkHynixNewsroomAdapter(WordPressNewsroomAdapter):
    AUTHORITY_TIER = "primary_corporate"
    INDEPENDENCE_GROUP = "skhynix"
    RIGHTS_STATUS = "prohibited"
    ID_PREFIX = "skhynix:wp-post:"
    BODY_CLASS = "post-contents"
    TITLE_CLASS = "post-title"
    DETAIL_ID_PATTERN = re.compile(r"post-(\d+)")


class SiaLatestAdapter(SemiconductorAdapter):
    AUTHORITY_TIER = "trusted_industry"
    INDEPENDENCE_GROUP = "sia"
    RIGHTS_STATUS = "internal_analysis_only"

    def collect(self, *, since: datetime, until: datetime):
        url = self.entrypoints[0]
        records = []
        seen_pages: set[str] = set()
        seen_ids: set[str] = set()
        prior_oldest = None
        for page in range(1, self.max_pages + 1):
            if url in seen_pages:
                raise SourceSchemaError("SIA pagination cursor repeated.")
            seen_pages.add(url)
            response = self._get(url, expected_content_types=HTML_TYPES)
            doc = document(response)
            items = doc.xpath(
                "//*[contains(concat(' ', normalize-space(@class), ' '), ' resource-item ')]"
            )
            if not items:
                raise SourceSchemaError("SIA list page has no resource items.")
            page_dates = []
            for item in items:
                hrefs = item.xpath(".//a[@href]/@href")
                if not hrefs:
                    raise SourceSchemaError("SIA resource item lacks a detail link.")
                date_text = " ".join(item.xpath(".//time/@datetime | .//time/text()"))
                published = parse_source_datetime(date_text)
                if published:
                    page_dates.append(published)
                record = self._detail(urljoin(url, hrefs[0]), published, since, until)
                if record is not None:
                    if record.external_id in seen_ids:
                        raise SourceSchemaError("SIA pagination repeated an identity.")
                    seen_ids.add(record.external_id)
                    records.append(record)
            if page_dates:
                newest = max(page_dates)
                oldest = min(page_dates)
                if prior_oldest and newest > prior_oldest:
                    raise SourceSchemaError("SIA list dates moved forward across pages.")
                prior_oldest = oldest
            next_links = doc.xpath("//a[@rel='next']/@href | //link[@rel='next']/@href")
            if not next_links:
                break
            next_url = canonical_url(urljoin(url, next_links[0]))
            expected_path = f"/news-events/latest-news/page/{page + 1}/"
            if urlsplit(next_url).hostname != urlsplit(url).hostname or urlsplit(next_url).path != expected_path:
                raise SourceSchemaError("SIA next cursor is outside the frozen sequence.")
            url = next_url
        return self._finalize(records)

    def _detail(self, url, list_published, since, until):
        response = self._get(url, expected_content_types=HTML_TYPES)
        doc = document(response)
        mains = doc.xpath("//main[@id='main']")
        if len(mains) != 1:
            raise SourceSchemaError("SIA detail main marker changed.")
        post_id = wp_post_id(doc)
        origin = f"sia:wp-post:{post_id}"
        reconciliation = origin in self.reconciliation_ids
        published_values = doc.xpath("//meta[@property='article:published_time']/@content | //time/@datetime")
        modified_values = doc.xpath("//meta[@property='article:modified_time']/@content")
        published = parse_source_datetime(published_values[0]) if published_values else list_published
        if not in_window(published, since, until) and not reconciliation:
            return None
        modified = parse_source_datetime(modified_values[0]) if modified_values else None
        body = stable_text(mains[0])
        titles = mains[0].xpath(".//h1")
        if not titles:
            raise SourceSchemaError("SIA detail title marker changed.")
        title = " ".join(titles[0].itertext()).strip()
        attachments = []
        for index, link in enumerate(mains[0].xpath(".//a[@href]"), 1):
            href = link.attrib["href"]
            if not urlsplit(href).path.lower().endswith((".pdf", ".xls", ".xlsx")):
                continue
            attachments.append(
                attachment(
                    urljoin(url, href),
                    title=" ".join(link.itertext()).strip() or f"attachment-{index}",
                    external_id=f"{origin}:file:{checksum(canonical_url(urljoin(url, href)))[:20]}",
                    rights_status=self.RIGHTS_STATUS,
                    locator=f"main-link:{index}",
                )
            )
        kinds = _kinds(mains[0], attachments)
        metadata = record_metadata(self, origin=origin, syndication="official_html", kinds=kinds)
        if modified and published and modified < published:
            metadata["rawModifiedAt"] = modified.isoformat()
            modified = None
        return CollectedSourceRecord(
            external_id=origin,
            canonical_url=canonical_url(url),
            title=title,
            publisher=self.source.publisher,
            published_at=published,
            modified_at=modified,
            collected_at=datetime.now(UTC),
            body_text=body,
            reconciliation_only=not in_window(published, since, until),
            raw_checksum=checksum({"body": body, "attachments": [item.external_id for item in attachments], "metadata": metadata}),
            metadata=metadata,
            attachments=tuple(attachments),
        )


def _positive(value, name, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} is outside its approved range.")
    return value


def _with_query(url, values):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query.update({key: [str(value)] for key, value in values.items()})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ""))


def _kinds(node, attachments):
    kinds = {"html"}
    if node.xpath(".//table"):
        kinds.add("table")
    if node.xpath(".//img"):
        kinds.update({"image", "chart"})
    for item in attachments:
        if item.mime_type == "application/pdf":
            kinds.add("pdf")
        elif item.mime_type and "sheet" in item.mime_type:
            kinds.add("spreadsheet")
        elif item.mime_type and item.mime_type.startswith("image/"):
            kinds.add("image")
    return kinds


class _TextResponse:
    def __init__(self, response, text):
        self.content = text.encode("utf-8")
        self.url = response.request.url
