from __future__ import annotations

import json
import mimetypes
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from lxml import html

from adapters.sources.base import CollectedSourceRecord, SourceAttachment
from adapters.sources.http import HttpSourceAdapter, SourceSchemaError, parse_source_datetime

from .common import (
    attachment,
    canonical_source_url,
    checksum,
    decode_krx,
    document,
    feed_entries,
    in_window,
    parse_krx_document_lineage,
    parse_motir_detail,
    parse_motir_list,
    parse_sia_list,
    record_metadata,
    stable_text,
    utc,
    wordpress_jsonld,
    wp_post_id,
)


class SemiconductorAdapter(HttpSourceAdapter):
    AUTHORITY_TIER = ""
    INDEPENDENCE_GROUP = ""
    RIGHTS_STATUS = ""
    IDENTITY_NAMESPACE = ""
    URL_PROFILES: dict[str, tuple[frozenset[str], tuple[str, ...], frozenset[str]]] = {}

    def __init__(self, *, source, config):
        super().__init__(source=source, config=config)
        self._https_only = True
        self.max_pages = _positive(config.get("maxPages"), "maxPages", 100)
        self.page_size = _positive(config.get("pageSize"), "pageSize", 100)
        self.reconciliation_days = _positive(
            config.get("reconciliationDays"), "reconciliationDays", 3660
        )
        self._mimes = {
            key: _mime_set(config, key)
            for key in (
                "feedContentTypes",
                "listContentTypes",
                "detailContentTypes",
                "attachmentContentTypes",
            )
            if key in config
        }
        if (
            source.authority_tier != self.AUTHORITY_TIER
            or source.independence_group != self.INDEPENDENCE_GROUP
            or config.get("rightsStatus") != self.RIGHTS_STATUS
            or config.get("identityNamespace") != self.IDENTITY_NAMESPACE
        ):
            raise ValueError(
                "Frozen source provenance does not match the adapter profile."
            )
        self.reconciliation_records: dict[str, dict] = {}
        for raw in config.get("_reconciliationRecords", []):
            if not isinstance(raw, dict):
                raise ValueError("Reconciliation record context is invalid.")
            external_id = raw.get("externalId")
            if (
                not isinstance(external_id, str)
                or not external_id
                or external_id in self.reconciliation_records
            ):
                raise ValueError(
                    "Reconciliation record identities must be unique."
                )
            self.reconciliation_records[external_id] = dict(raw)
        legacy_ids = config.get("_reconciliationExternalIds", [])
        if not isinstance(legacy_ids, list) or any(
            not isinstance(value, str) for value in legacy_ids
        ):
            raise ValueError("Reconciliation identity context is invalid.")
        self.reconciliation_ids = frozenset(
            {*legacy_ids, *self.reconciliation_records}
        )

    def _profile(self, kind: str):
        try:
            return self.URL_PROFILES[kind]
        except KeyError as exc:
            raise SourceSchemaError("Source request purpose is not approved.") from exc

    def _canonical(self, value: str, kind: str) -> str:
        hosts, paths, query = self._profile(kind)
        return canonical_source_url(
            value,
            hosts=hosts,
            paths=paths,
            allowed_query_keys=query,
        )

    def _validate_request_url(self, value: str, kind: str) -> None:
        canonical = self._canonical(value, kind)
        parsed = urlsplit(value)
        canonical_parsed = urlsplit(canonical)
        original_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        canonical_pairs = parse_qsl(
            canonical_parsed.query, keep_blank_values=True
        )
        if (
            sorted(original_pairs) != sorted(canonical_pairs)
            or (parsed.path or "/") != canonical_parsed.path
            or (parsed.hostname or "").rstrip(".").lower()
            != canonical_parsed.hostname
            or parsed.fragment
        ):
            raise SourceSchemaError(
                "Source request URL contains unstable or unapproved query material."
            )
        self._validate_query_values(kind, dict(original_pairs))

    def _validate_query_values(self, kind: str, query: dict[str, str]) -> None:
        del kind, query

    def _get_source(
        self,
        url: str,
        *,
        kind: str,
        mime_field: str,
        params=None,
    ):
        normalized = self._canonical(url, kind)
        return self._get(
            normalized,
            params=params,
            expected_content_types=self._mimes[mime_field],
            url_validator=lambda value: self._validate_request_url(value, kind),
        )

    def _post_source(self, url: str, *, kind: str, data, mime_field: str):
        normalized = self._canonical(url, kind)
        return self._post_form(
            normalized,
            data=data,
            expected_content_types=self._mimes[mime_field],
            url_validator=lambda value: self._validate_request_url(value, kind),
        )

    def _unavailable(self, context: dict) -> CollectedSourceRecord:
        metadata = {
            key: value
            for key, value in dict(context.get("metadata") or {}).items()
            if key != "_collection"
        }
        return CollectedSourceRecord(
            external_id=context["externalId"],
            canonical_url=context["canonicalUrl"],
            title=context.get("title") or context["externalId"],
            publisher=self.source.publisher,
            published_at=_context_datetime(context.get("publishedAt")),
            modified_at=_context_datetime(context.get("modifiedAt")),
            collected_at=datetime.now(UTC),
            body_text=str(context.get("bodyText") or ""),
            status="unavailable",
            reconciliation_only=True,
            raw_checksum=checksum(
                {
                    "externalId": context["externalId"],
                    "status": "unavailable",
                }
            ),
            metadata=metadata,
            attachments=(),
        )

    def _finalize(self, records):
        seen: set[str] = set()
        result = []
        for record in sorted(
            (item for item in records if item is not None),
            key=lambda item: item.external_id,
        ):
            if record.external_id in seen:
                raise SourceSchemaError(
                    "Source returned a duplicate provider identity."
                )
            seen.add(record.external_id)
            result.append(record)
        return result


class MotirAdapter(SemiconductorAdapter):
    AUTHORITY_TIER = "primary_official"
    INDEPENDENCE_GROUP = "kr-motie"
    RIGHTS_STATUS = "attribution_required"
    IDENTITY_NAMESPACE = "motie:81"
    _HOSTS = frozenset({"www.motir.go.kr"})
    _LIST_PATHS = (
        r"/kor/article/ATCL3f49a5a8c",
        r"/kor/article/ATCLe0854704d",
    )
    URL_PROFILES = {
        "list": (
            _HOSTS,
            _LIST_PATHS,
            frozenset({"pageIndex", "rowPageC", "startDtD", "endDtD"}),
        ),
        "detail": (
            _HOSTS,
            tuple(path + r"/\d+/view" for path in _LIST_PATHS),
            frozenset(),
        ),
        "attachment": (
            _HOSTS,
            (
                r"/attach/down/[0-9a-fA-F]{32}/[0-9a-fA-F]{32}/"
                r"[0-9a-fA-F]{32}",
            ),
            frozenset(),
        ),
    }

    def _validate_query_values(self, kind, query):
        if kind != "list":
            if query:
                raise SourceSchemaError("MOTIR non-list request has query fields.")
            return
        if set(query) != {"pageIndex", "rowPageC", "startDtD", "endDtD"}:
            raise SourceSchemaError("MOTIR list query profile is incomplete.")
        if (
            not query["pageIndex"].isdigit()
            or query["rowPageC"] != "50"
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", query["startDtD"])
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", query["endDtD"])
        ):
            raise SourceSchemaError("MOTIR list query values are invalid.")

    def collect(self, *, since: datetime, until: datetime):
        discovered: dict[str, dict] = {}
        for entrypoint in self.entrypoints:
            page_signatures: set[str] = set()
            endpoint_ids: set[str] = set()
            stable_total: int | None = None
            for page in range(1, self.max_pages + 1):
                response = self._get_source(
                    entrypoint,
                    kind="list",
                    mime_field="listContentTypes",
                    params={
                        "pageIndex": page,
                        "rowPageC": 50,
                        "startDtD": utc(since).strftime("%Y-%m-%d"),
                        "endDtD": utc(until).strftime("%Y-%m-%d"),
                    },
                )
                doc = document(response)
                rows = parse_motir_list(doc)
                signature = checksum(
                    [(row["sequence"], row["title"], row["published"]) for row in rows]
                )
                if signature in page_signatures:
                    raise SourceSchemaError("MOTIR list page repeated.")
                page_signatures.add(signature)
                total = _page_total(doc)
                if total is not None:
                    if stable_total is not None and total != stable_total:
                        raise SourceSchemaError("MOTIR total count changed during paging.")
                    stable_total = total
                    if total > self.max_pages * 50:
                        raise SourceSchemaError("MOTIR total exceeds the frozen page bound.")
                for row in rows:
                    external_id = f"motie:81:{row['sequence']}"
                    if external_id in endpoint_ids:
                        raise SourceSchemaError(
                            "MOTIR one list repeated a provider identity."
                        )
                    endpoint_ids.add(external_id)
                    candidate = {
                        **row,
                        "entrypoints": {entrypoint},
                    }
                    existing = discovered.get(external_id)
                    if existing is None:
                        discovered[external_id] = candidate
                    elif (
                        existing["title"] != row["title"]
                        or existing["published"] != row["published"]
                    ):
                        raise SourceSchemaError(
                            "MOTIR entrypoints disagree for one provider identity."
                        )
                    else:
                        existing["entrypoints"].add(entrypoint)
                has_more = (
                    page * 50 < stable_total
                    if stable_total is not None
                    else len(rows) == 50
                )
                if page == self.max_pages and has_more:
                    raise SourceSchemaError("MOTIR pagination bound was exhausted.")
                if not has_more:
                    if stable_total is not None and len(endpoint_ids) != stable_total:
                        raise SourceSchemaError(
                            "MOTIR list coverage does not match its total count."
                        )
                    break
        records = []
        resolved: set[str] = set()
        for external_id, item in discovered.items():
            published = parse_source_datetime(item["published"])
            if not in_window(published, since, until) and external_id not in self.reconciliation_ids:
                continue
            entrypoint = sorted(item["entrypoints"])[0]
            records.append(
                self._detail(
                    f"{entrypoint.rstrip('/')}/{item['sequence']}/view",
                    sequence=item["sequence"],
                    since=since,
                    until=until,
                    list_item=item,
                    force_reconciliation=not in_window(published, since, until),
                )
            )
            resolved.add(external_id)
        records.extend(self._reconcile_missing(resolved, since, until))
        return self._finalize(records)

    def _detail(
        self,
        url,
        *,
        sequence,
        since,
        until,
        list_item=None,
        force_reconciliation=False,
        prior_metadata=None,
    ):
        response = self._get_source(
            url,
            kind="detail",
            mime_field="detailContentTypes",
        )
        parsed = parse_motir_detail(document(response), expected_sequence=sequence)
        published = parse_source_datetime(parsed["published"])
        external_id = f"motie:81:{sequence}"
        if (
            not force_reconciliation
            and not in_window(published, since, until)
            and external_id not in self.reconciliation_ids
        ):
            return None
        attachments = []
        for index, raw in enumerate(parsed["attachments"], 1):
            mime_type = raw["mimeType"]
            if mime_type not in self._mimes["attachmentContentTypes"]:
                raise SourceSchemaError("MOTIR attachment MIME is not approved.")
            asset_url = self._canonical(urljoin(url, raw["href"]), "attachment")
            attachments.append(
                attachment(
                    asset_url,
                    title=raw["title"],
                    mime_type=mime_type,
                    external_id=raw["externalId"],
                    rights_status=self.RIGHTS_STATUS,
                    locator=f"board-detail:attachment:{index}",
                    metadata={"originIdentity": external_id},
                )
            )
        metadata = record_metadata(
            self,
            origin=external_id,
            syndication="official_html",
            kinds=_content_kinds(parsed["bodyNode"], attachments),
        )
        metadata["listingCategories"] = (
            list(dict(prior_metadata or {}).get("listingCategories") or [])
            if prior_metadata is not None
            else sorted(
                urlsplit(value).path
                for value in (list_item or {}).get("entrypoints", [])
            )
        )
        metadata["documentIdentity"] = {"bbsCdN": "81", "bbsSeqN": sequence}
        return CollectedSourceRecord(
            external_id=external_id,
            canonical_url=self._canonical(url, "detail"),
            title=parsed["title"],
            publisher=self.source.publisher,
            published_at=published,
            collected_at=datetime.now(UTC),
            body_text=parsed["body"],
            reconciliation_only=force_reconciliation or not in_window(
                published, since, until
            ),
            raw_checksum=checksum(
                {
                    "title": parsed["title"],
                    "body": parsed["body"],
                    "attachments": [
                        _attachment_material(value) for value in attachments
                    ],
                    "metadata": metadata,
                }
            ),
            metadata=metadata,
            attachments=tuple(attachments),
        )

    def _reconcile_missing(self, resolved, since, until):
        records = []
        for external_id, context in sorted(self.reconciliation_records.items()):
            if external_id in resolved:
                continue
            match = re.fullmatch(r"motie:81:(\d+)", external_id)
            if not match:
                raise SourceSchemaError("MOTIR reconciliation identity is invalid.")
            sequence = match.group(1)
            stored = str(context["canonicalUrl"])
            try:
                detail_url = self._canonical(stored, "detail")
            except SourceSchemaError:
                detail_url = (
                    f"{self.entrypoints[0].rstrip('/')}/{sequence}/view"
                )
            try:
                record = self._detail(
                    detail_url,
                    sequence=sequence,
                    since=since,
                    until=until,
                    force_reconciliation=True,
                    prior_metadata=context.get("metadata"),
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {404, 410}:
                    raise
                record = self._unavailable(
                    {**context, "canonicalUrl": detail_url}
                )
            records.append(record)
        return records


class KrxKindAdapter(SemiconductorAdapter):
    AUTHORITY_TIER = "primary_regulatory"
    INDEPENDENCE_GROUP = "krx-kind"
    RIGHTS_STATUS = "internal_analysis_only"
    IDENTITY_NAMESPACE = "krx-kind"
    _HOSTS = frozenset({"kind.krx.co.kr"})
    URL_PROFILES = {
        "list": (
            _HOSTS,
            (r"/disclosure/details\.do",),
            frozenset({"method"}),
        ),
        "post": (
            _HOSTS,
            (r"/disclosure/details\.do",),
            frozenset(),
        ),
        "viewer": (
            _HOSTS,
            (r"/common/disclsviewer\.do",),
            frozenset({"method", "acptno", "docNo"}),
        ),
        "static": (
            _HOSTS,
            (
                r"/(?:common|disclosure|html|viewer|repository|upload|download)"
                r"/[A-Za-z0-9_./%+-]+",
            ),
            frozenset({"acptno", "docNo"}),
        ),
    }
    _ITEM = re.compile(
        r"^\s*openDisclsViewer\(['\"](\d{14})['\"],\s*['\"]['\"]\)\s*;?\s*$"
    )

    def _validate_query_values(self, kind, query):
        if kind == "list":
            if query != {"method": "searchDetailsMain"}:
                raise SourceSchemaError("KRX list endpoint query is invalid.")
            return
        if kind == "viewer":
            method = query.get("method")
            if method not in {"search", "searchInitInfo", "searchContents"}:
                raise SourceSchemaError("KRX viewer method is not approved.")
            if not re.fullmatch(r"\d{14}", query.get("acptno", "")):
                raise SourceSchemaError("KRX viewer receipt identity is invalid.")
            if method == "searchContents" and not query.get("docNo", "").isdigit():
                raise SourceSchemaError("KRX viewer document identity is invalid.")
            if method != "searchContents" and "docNo" in query:
                raise SourceSchemaError("KRX viewer query contains an extra docNo.")
        elif kind == "static":
            if (
                "acptno" in query
                and not re.fullmatch(r"\d{14}", query["acptno"])
            ) or (
                "docNo" in query
                and not query["docNo"].isdigit()
            ):
                raise SourceSchemaError(
                    "KRX static document query identity is invalid."
                )
        elif kind == "post" and query:
            raise SourceSchemaError("KRX form POST endpoint has a query.")

    def collect(self, *, since: datetime, until: datetime):
        if self.config.get("issuerCodes") != ["A005930", "A000660"]:
            raise SourceSchemaError("KRX issuer scope is not approved.")
        endpoint = self.entrypoints[0]
        records = []
        resolved: set[str] = set()
        page_seen: set[str] = set()
        for issuer in self.config["issuerCodes"]:
            issuer_seen: set[str] = set()
            stable_total = None
            for page in range(1, self.max_pages + 1):
                response = self._post_source(
                    urljoin(endpoint, "/disclosure/details.do"),
                    kind="post",
                    data={
                        "method": "searchDetailsSub",
                        "forward": "details_sub",
                        "currentPageSize": self.page_size,
                        "pageIndex": page,
                        "fromDate": utc(since).strftime("%Y-%m-%d"),
                        "toDate": utc(until).strftime("%Y-%m-%d"),
                        "repIsuSrtCd": issuer,
                        "marketType": "",
                        "kosdaqSegment": "",
                        "settlementMonth": "",
                        "securities": "",
                        "searchCorpName": "",
                        "reportNm": "",
                        "business": "",
                    },
                    mime_field="listContentTypes",
                )
                text = decode_krx(response)
                _reject_krx_error_page(text)
                doc = html.fromstring(text)
                items = _krx_list_items(doc, self._ITEM)
                if not items:
                    if page == 1:
                        raise SourceSchemaError("KRX list markers are missing.")
                    break
                signature = checksum(
                    [(item["acptNo"], item["published"]) for item in items]
                )
                if signature in page_seen:
                    raise SourceSchemaError("KRX list page repeated.")
                page_seen.add(signature)
                total = _page_total(doc)
                if total is not None:
                    if stable_total is not None and stable_total != total:
                        raise SourceSchemaError("KRX total changed during paging.")
                    stable_total = total
                    if total > self.max_pages * self.page_size:
                        raise SourceSchemaError("KRX total exceeds the page bound.")
                for item in items:
                    external_id = f"krx-kind:{item['acptNo']}"
                    if external_id in issuer_seen or external_id in resolved:
                        raise SourceSchemaError("KRX list repeated a provider identity.")
                    issuer_seen.add(external_id)
                    published = parse_source_datetime(item["published"])
                    if not in_window(published, since, until):
                        if external_id not in self.reconciliation_ids:
                            raise SourceSchemaError(
                                "KRX returned a new identity outside the requested period."
                            )
                        force = True
                    else:
                        force = False
                    records.append(
                        self._detail(
                            item["acptNo"],
                            title=item["title"],
                            published=published,
                            since=since,
                            until=until,
                            force_reconciliation=force,
                        )
                    )
                    resolved.add(external_id)
                has_more = (
                    page * self.page_size < stable_total
                    if stable_total is not None
                    else len(items) == self.page_size
                )
                if page == self.max_pages and has_more:
                    raise SourceSchemaError("KRX pagination bound was exhausted.")
                if not has_more:
                    if stable_total is not None and len(issuer_seen) != stable_total:
                        raise SourceSchemaError("KRX total coverage is incomplete.")
                    break
        records.extend(self._reconcile_missing(resolved, since, until))
        return self._finalize(records)

    def _detail(
        self,
        acpt_no,
        *,
        title,
        published,
        since,
        until,
        force_reconciliation=False,
    ):
        base = "https://kind.krx.co.kr/common/disclsviewer.do"
        init = self._get_source(
            base,
            kind="viewer",
            mime_field="detailContentTypes",
            params={"method": "searchInitInfo", "acptno": acpt_no},
        )
        init_text = decode_krx(init)
        _reject_krx_error_page(init_text)
        if acpt_no not in init_text:
            raise SourceSchemaError("KRX init receipt marker is missing.")
        lineage = parse_krx_document_lineage(init_text)
        documents = []
        attachments = []
        for index, item in enumerate(lineage, 1):
            wrapper = self._get_source(
                base,
                kind="viewer",
                mime_field="detailContentTypes",
                params={
                    "method": "searchContents",
                    "acptno": acpt_no,
                    "docNo": item["docNo"],
                },
            )
            wrapper_text = decode_krx(wrapper)
            _reject_krx_error_page(wrapper_text)
            if acpt_no not in wrapper_text and item["docNo"] not in wrapper_text:
                raise SourceSchemaError("KRX wrapper identity marker is missing.")
            static_url = _krx_static_url(wrapper_text, base)
            static_url = self._canonical(static_url, "static")
            static = self._get_source(
                static_url,
                kind="static",
                mime_field="attachmentContentTypes",
            )
            mime_type = _response_mime(static)
            if mime_type == "text/html":
                static_text = decode_krx(static)
                _reject_krx_error_page(static_text)
                if (
                    acpt_no not in static_text
                    and item["docNo"] not in static_text
                ):
                    raise SourceSchemaError(
                        "KRX static document identity marker is missing."
                    )
                static_doc = html.fromstring(static_text)
                body = stable_text(static_doc)
            elif (
                mime_type == "application/pdf"
                and static.content.startswith(b"%PDF-")
            ):
                body = f"[application/pdf document {item['docNo']}]"
            else:
                raise SourceSchemaError(
                    "KRX static document MIME or magic is invalid."
                )
            if not body.strip():
                raise SourceSchemaError("KRX static document body is empty.")
            locator = f"{item['role']}:{index}"
            doc_external_id = (
                f"krx-kind:{acpt_no}:doc:{item['docNo']}:{item['lineage']}"
            )
            attachments.append(
                attachment(
                    static_url,
                    title=f"{item['role']} {item['docNo']}",
                    mime_type=mime_type,
                    external_id=doc_external_id,
                    rights_status=self.RIGHTS_STATUS,
                    locator=locator,
                    metadata={
                        "originIdentity": f"krx-kind:{acpt_no}",
                        "documentRole": item["role"],
                        "docNo": item["docNo"],
                        "lineage": item["lineage"],
                    },
                )
            )
            documents.append(
                {
                    **item,
                    "staticUrl": static_url,
                    "locator": locator,
                    "body": body,
                }
            )
        body_text = "\n".join(item["body"] for item in documents)
        origin = f"krx-kind:{acpt_no}"
        corrected = title.strip().startswith("[정정]")
        metadata = record_metadata(
            self,
            origin=origin,
            syndication="regulatory_html",
            kinds={"html", *("pdf" for value in attachments if value.mime_type == "application/pdf")},
        )
        metadata["documentLineage"] = [
            {
                key: item[key]
                for key in ("role", "docNo", "lineage", "staticUrl", "locator")
            }
            for item in documents
        ]
        metadata["correction"] = corrected
        correction_of = _krx_correction_of(init_text, acpt_no)
        if correction_of:
            metadata["correctionOf"] = f"krx-kind:{correction_of}"
        return CollectedSourceRecord(
            external_id=origin,
            canonical_url=self._canonical(
                _with_query(
                    base,
                    {"method": "search", "acptno": acpt_no},
                ),
                "viewer",
            ),
            title=title,
            publisher=self.source.publisher,
            published_at=published,
            collected_at=datetime.now(UTC),
            body_text=body_text,
            status="corrected" if corrected else "active",
            reconciliation_only=force_reconciliation,
            raw_checksum=checksum(
                {
                    "body": body_text,
                    "attachments": [
                        _attachment_material(value) for value in attachments
                    ],
                    "metadata": metadata,
                }
            ),
            metadata=metadata,
            attachments=tuple(attachments),
        )

    def _reconcile_missing(self, resolved, since, until):
        records = []
        for external_id, context in sorted(self.reconciliation_records.items()):
            if external_id in resolved:
                continue
            match = re.fullmatch(r"krx-kind:(\d{14})", external_id)
            if not match:
                raise SourceSchemaError("KRX reconciliation identity is invalid.")
            acpt_no = match.group(1)
            try:
                record = self._detail(
                    acpt_no,
                    title=str(context.get("title") or external_id),
                    published=_context_datetime(context.get("publishedAt")),
                    since=since,
                    until=until,
                    force_reconciliation=True,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {404, 410}:
                    raise
                safe_context = {
                    **context,
                    "canonicalUrl": self._canonical(
                        _with_query(
                            "https://kind.krx.co.kr/common/disclsviewer.do",
                            {"method": "search", "acptno": acpt_no},
                        ),
                        "viewer",
                    ),
                }
                record = self._unavailable(safe_context)
            records.append(record)
        return records


class WordPressNewsroomAdapter(SemiconductorAdapter):
    ID_PREFIX = ""
    BODY_CLASS = ""
    TITLE_CLASS = ""
    REQUIRE_ARTICLE_ID = False
    JSONLD_TIME_FIRST = False
    REQUIRE_JSONLD_ID = False
    REQUIRE_REST_ID = False

    def collect(self, *, since: datetime, until: datetime):
        merged: dict[str, dict] = {}
        for feed_url in self.entrypoints:
            seen_pages: set[str] = set()
            for page in range(1, self.max_pages + 1):
                response = self._get_source(
                    _with_query(feed_url, {"paged": page}),
                    kind="feed",
                    mime_field="feedContentTypes",
                )
                entries = feed_entries(response.content)
                if not entries:
                    if page == 1:
                        raise SourceSchemaError("Newsroom feed is empty.")
                    break
                normalized_urls = [
                    self._canonical(item["url"], "detail") for item in entries
                ]
                signature = checksum(normalized_urls)
                if signature in seen_pages:
                    raise SourceSchemaError("Newsroom feed page repeated.")
                seen_pages.add(signature)
                for item, canonical in zip(entries, normalized_urls, strict=True):
                    existing = merged.setdefault(
                        canonical,
                        {
                            "title": item["title"],
                            "url": canonical,
                            "aliases": set(),
                            "kinds": set(),
                            "rssPublished": None,
                            "rssUpdated": None,
                            "atomPublished": None,
                            "atomUpdated": None,
                            "enclosures": [],
                        },
                    )
                    if existing["title"] != item["title"]:
                        raise SourceSchemaError(
                            "Newsroom feeds disagree on a canonical title."
                        )
                    if item["id"]:
                        existing["aliases"].add(_stable_alias(item["id"]))
                    existing["kinds"].add(item["kind"])
                    prefix = "atom" if item["kind"] == "atom" else "rss"
                    if item["published"]:
                        existing[f"{prefix}Published"] = item["published"]
                    if item["updated"]:
                        existing[f"{prefix}Updated"] = item["updated"]
                    existing["enclosures"].extend(item["enclosures"])
                has_more = len(entries) == self.page_size
                if page == self.max_pages and has_more:
                    raise SourceSchemaError("Newsroom feed page bound was exhausted.")
                if not has_more:
                    break
        records = []
        resolved: set[str] = set()
        for item in merged.values():
            expected = self._feed_wp_id(item)
            external_id = f"{self.ID_PREFIX}{expected}" if expected else None
            feed_published = item["atomPublished"] or item["rssPublished"]
            if (
                not in_window(feed_published, since, until)
                and external_id not in self.reconciliation_ids
            ):
                continue
            record = self._detail(
                item,
                expected=expected,
                since=since,
                until=until,
                force_reconciliation=(
                    external_id in self.reconciliation_ids
                    and not in_window(feed_published, since, until)
                ),
            )
            if record is not None:
                records.append(record)
                resolved.add(record.external_id)
        records.extend(self._reconcile_missing(resolved, since, until))
        return self._finalize(records)

    def _feed_wp_id(self, item):
        candidates: set[int] = set()
        for value in item["aliases"]:
            match = re.search(r"(?:[?&]p=|/archives/|post-)(\d+)(?:\D|$)", value)
            if match:
                candidates.add(int(match.group(1)))
        if len(candidates) > 1:
            raise SourceSchemaError("Newsroom feed identities disagree.")
        if self.REQUIRE_ARTICLE_ID and not candidates:
            raise SourceSchemaError("SK hynix feed WordPress identity is missing.")
        return next(iter(candidates), None)

    def _detail(
        self,
        item,
        *,
        expected,
        since,
        until,
        force_reconciliation=False,
        prior_attachments=(),
    ):
        response = self._get_source(
            item["url"],
            kind="detail",
            mime_field="detailContentTypes",
        )
        doc = document(response)
        canonical_values = doc.xpath("//link[@rel='canonical']/@href")
        if len(canonical_values) != 1:
            raise SourceSchemaError("Newsroom canonical marker changed.")
        canonical = self._canonical(canonical_values[0], "detail")
        article_ids = {
            int(match.group(1))
            for value in doc.xpath("//article/@id")
            if (match := re.fullmatch(r"post-(\d+)", value))
        }
        if self.REQUIRE_ARTICLE_ID and len(article_ids) != 1:
            raise SourceSchemaError("SK hynix article identity marker changed.")
        jsonld = wordpress_jsonld(doc)
        linked_ids = _wordpress_link_ids(doc, response)
        if self.REQUIRE_JSONLD_ID and not jsonld["identities"]:
            raise SourceSchemaError(
                "Newsroom JSON-LD provider identity marker is missing."
            )
        if self.REQUIRE_REST_ID and not linked_ids:
            raise SourceSchemaError(
                "Newsroom REST or shortlink provider identity marker is missing."
            )
        post_id = wp_post_id(
            doc,
            expected=expected,
            additional_ids={
                *article_ids,
                *jsonld["identities"],
                *linked_ids,
            },
        )
        origin = f"{self.ID_PREFIX}{post_id}"
        bodies = doc.xpath(
            f"//*[contains(concat(' ', normalize-space(@class), ' '),"
            f" ' {self.BODY_CLASS} ')]"
        )
        titles = doc.xpath(
            f"//*[contains(concat(' ', normalize-space(@class), ' '),"
            f" ' {self.TITLE_CLASS} ')]"
        )
        if len(bodies) != 1 or len(titles) != 1:
            raise SourceSchemaError("Newsroom detail DOM markers changed.")
        body = stable_text(bodies[0])
        title = " ".join(titles[0].itertext()).strip()
        if not title or not body:
            raise SourceSchemaError("Newsroom title or body is empty.")
        if self.JSONLD_TIME_FIRST:
            published = (
                jsonld["published"]
                or item.get("atomPublished")
                or item.get("rssPublished")
            )
            modified = (
                jsonld["modified"]
                or item.get("atomUpdated")
                or item.get("rssUpdated")
            )
        else:
            published = (
                item.get("atomPublished")
                or jsonld["published"]
                or item.get("rssPublished")
            )
            modified = (
                item.get("atomUpdated")
                or jsonld["modified"]
                or item.get("rssUpdated")
            )
        if (
            not force_reconciliation
            and not in_window(published, since, until)
            and origin not in self.reconciliation_ids
        ):
            return None
        attachments = list(prior_attachments)
        for index, enclosure in enumerate(item.get("enclosures", []), 1):
            enclosure_url = urljoin(item["url"], enclosure["url"])
            attachments.append(
                self._media_attachment(
                    origin,
                    enclosure_url,
                    enclosure["mimeType"],
                    "feed-enclosure:"
                    + checksum(
                        [self._canonical(enclosure_url, "media"), enclosure["mimeType"]]
                    ),
                    title=f"Feed enclosure {index}",
                    metadata={
                        "declaredLength": enclosure.get("declaredLength"),
                        "feedDeclaredOnly": True,
                    },
                )
            )
        for index, node in enumerate(
            bodies[0].xpath(
                ".//a[@href] | .//img[@src or @srcset] | .//audio[@src]"
                " | .//video[@src] | .//source[@src]"
            ),
            1,
        ):
            raw_urls = []
            if node.attrib.get("href"):
                raw_urls.append(node.attrib["href"])
            if node.attrib.get("src"):
                raw_urls.append(node.attrib["src"])
            if node.attrib.get("srcset"):
                raw_urls.extend(
                    item.strip().split()[0]
                    for item in node.attrib["srcset"].split(",")
                    if item.strip()
                )
            for variant, raw_url in enumerate(raw_urls, 1):
                mime_type = (
                    node.attrib.get("type")
                    or mimetypes.guess_type(urlsplit(raw_url).path)[0]
                )
                if mime_type not in self._mimes["attachmentContentTypes"]:
                    if node.tag == "a":
                        continue
                    raise SourceSchemaError(
                        "Newsroom embedded media MIME is not approved."
                    )
                attachments.append(
                    self._media_attachment(
                        origin,
                        urljoin(canonical, raw_url),
                        mime_type,
                        f"detail-media:{index}:{variant}",
                        title=node.attrib.get("alt")
                        or " ".join(node.itertext()).strip()
                        or f"Media {index}",
                    )
                )
        attachments = _merge_attachments(attachments)
        kinds = _content_kinds(bodies[0], attachments)
        metadata = record_metadata(
            self,
            origin=origin,
            syndication="+".join(sorted(item.get("kinds") or {"direct"})),
            kinds=kinds,
        )
        metadata["providerAliases"] = sorted(item.get("aliases", []))
        metadata["storedExternalIdentity"] = {
            "wpPostId": post_id,
            "canonicalPath": urlsplit(canonical).path,
        }
        if modified and published and modified < published:
            metadata["rawModifiedAt"] = modified.isoformat()
            modified = None
        return CollectedSourceRecord(
            external_id=origin,
            canonical_url=canonical,
            title=title,
            publisher=self.source.publisher,
            published_at=published,
            modified_at=modified,
            collected_at=datetime.now(UTC),
            body_text=body,
            reconciliation_only=force_reconciliation or not in_window(
                published, since, until
            ),
            raw_checksum=checksum(
                {
                    "body": body,
                    "attachments": [
                        _attachment_material(value) for value in attachments
                    ],
                    "metadata": metadata,
                }
            ),
            metadata=metadata,
            attachments=tuple(attachments),
        )

    def _media_attachment(
        self,
        origin,
        raw_url,
        mime_type,
        locator,
        *,
        title,
        metadata=None,
    ):
        url = self._canonical(raw_url, "media")
        return attachment(
            url,
            title=title,
            mime_type=mime_type,
            external_id=f"{origin}:media:{checksum(url)}",
            rights_status=self.RIGHTS_STATUS,
            locator=locator,
            metadata={
                "originIdentity": origin,
                **{k: v for k, v in (metadata or {}).items() if v is not None},
            },
        )

    def _reconcile_missing(self, resolved, since, until):
        records = []
        identity_pattern = re.compile(re.escape(self.ID_PREFIX) + r"(\d+)")
        for external_id, context in sorted(self.reconciliation_records.items()):
            if external_id in resolved:
                continue
            match = identity_pattern.fullmatch(external_id)
            if not match:
                raise SourceSchemaError("Newsroom reconciliation identity is invalid.")
            post_id = int(match.group(1))
            stored = dict(context.get("metadata") or {}).get(
                "storedExternalIdentity", {}
            )
            if stored and stored.get("wpPostId") != post_id:
                raise SourceSchemaError(
                    "Stored newsroom provider identity is inconsistent."
                )
            canonical = str(context["canonicalUrl"])
            canonical = self._canonical(canonical, "detail")
            prior_metadata = dict(context.get("metadata") or {})
            item = {
                "url": canonical,
                "title": context.get("title") or external_id,
                "aliases": set(prior_metadata.get("providerAliases") or []),
                "kinds": set(
                    str(prior_metadata.get("syndicationKind") or "direct").split("+")
                ),
                "atomPublished": _context_datetime(context.get("publishedAt")),
                "atomUpdated": _context_datetime(context.get("modifiedAt")),
                "rssPublished": None,
                "rssUpdated": None,
                "enclosures": [],
            }
            try:
                record = self._detail(
                    item,
                    expected=post_id,
                    since=since,
                    until=until,
                    force_reconciliation=True,
                    prior_attachments=tuple(
                        _attachment_from_context(value)
                        for value in context.get("attachments", [])
                        if str(
                            dict(value.get("metadata") or {}).get(
                                "locator", ""
                            )
                        ).startswith("feed-enclosure:")
                    ),
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {404, 410}:
                    raise
                record = self._unavailable(
                    {**context, "canonicalUrl": canonical}
                )
            records.append(record)
        return records


class SamsungNewsroomAdapter(WordPressNewsroomAdapter):
    AUTHORITY_TIER = "primary_corporate"
    INDEPENDENCE_GROUP = "samsung"
    RIGHTS_STATUS = "internal_analysis_only"
    IDENTITY_NAMESPACE = "samsung-global:wp-post"
    ID_PREFIX = "samsung-global:wp-post:"
    BODY_CLASS = "single_contents"
    TITLE_CLASS = "single-title"
    JSONLD_TIME_FIRST = True
    REQUIRE_JSONLD_ID = True
    REQUIRE_REST_ID = True
    URL_PROFILES = {
        "feed": (
            frozenset({"news.samsung.com"}),
            (
                r"/global/category/products/semiconductors/feed/?",
                r"/global/category/products/semiconductors/feed/atom/?",
            ),
            frozenset({"paged"}),
        ),
        "detail": (
            frozenset({"news.samsung.com"}),
            (r"/global/(?!category/products/semiconductors/feed(?:/|$)).+",),
            frozenset(),
        ),
        "media": (
            frozenset({"news.samsung.com", "img.global.news.samsung.com"}),
            (r"/.+",),
            frozenset(),
        ),
    }

    def _validate_query_values(self, kind, query):
        if kind == "feed":
            if set(query) != {"paged"} or not query["paged"].isdigit():
                raise SourceSchemaError("Samsung feed page query is invalid.")
        elif query:
            raise SourceSchemaError("Samsung non-feed URL has a query.")


class SkHynixNewsroomAdapter(WordPressNewsroomAdapter):
    AUTHORITY_TIER = "primary_corporate"
    INDEPENDENCE_GROUP = "skhynix"
    RIGHTS_STATUS = "prohibited"
    IDENTITY_NAMESPACE = "skhynix:wp-post"
    ID_PREFIX = "skhynix:wp-post:"
    BODY_CLASS = "post-contents"
    TITLE_CLASS = "post-title"
    REQUIRE_ARTICLE_ID = True
    REQUIRE_REST_ID = True
    URL_PROFILES = {
        "feed": (
            frozenset({"news.skhynix.com"}),
            (r"/en/feed/?", r"/en/feed/atom/?"),
            frozenset({"paged"}),
        ),
        "detail": (
            frozenset({"news.skhynix.com"}),
            (r"/(?!en/feed(?:/|$)).+",),
            frozenset(),
        ),
        "media": (
            frozenset(
                {"news.skhynix.com", "d18r0a86za96sg.cloudfront.net"}
            ),
            (r"/.+",),
            frozenset(),
        ),
    }

    def _validate_query_values(self, kind, query):
        if kind == "feed":
            if set(query) != {"paged"} or not query["paged"].isdigit():
                raise SourceSchemaError("SK hynix feed page query is invalid.")
        elif query:
            raise SourceSchemaError("SK hynix non-feed URL has a query.")


class SiaLatestAdapter(SemiconductorAdapter):
    AUTHORITY_TIER = "trusted_industry"
    INDEPENDENCE_GROUP = "sia"
    RIGHTS_STATUS = "internal_analysis_only"
    IDENTITY_NAMESPACE = "sia:wp-post"
    _HOSTS = frozenset({"www.semiconductors.org"})
    URL_PROFILES = {
        "list": (
            _HOSTS,
            (
                r"/news-events/latest-news/",
                r"/news-events/latest-news/page/\d+/",
            ),
            frozenset(),
        ),
        "detail": (
            _HOSTS,
            (
                r"/(?!wp-admin|wp-login|news-events/latest-news(?:/|$)).+/?",
            ),
            frozenset(),
        ),
        "reconcile": (
            _HOSTS,
            (
                r"/",
                r"/(?!wp-admin|wp-login|news-events/latest-news(?:/|$)).+/?",
            ),
            frozenset({"p"}),
        ),
        "media": (
            _HOSTS,
            (r"/.+",),
            frozenset(),
        ),
    }

    def _validate_query_values(self, kind, query):
        if kind == "reconcile":
            if query and (
                set(query) != {"p"} or not query["p"].isdigit()
            ):
                raise SourceSchemaError("SIA reconciliation query is invalid.")
        elif query:
            raise SourceSchemaError("SIA source URL has an unexpected query.")

    def collect(self, *, since: datetime, until: datetime):
        url = self.entrypoints[0]
        records = []
        resolved: set[str] = set()
        seen_pages: set[str] = set()
        seen_ids: set[str] = set()
        prior_oldest = None
        for page in range(1, self.max_pages + 1):
            response = self._get_source(
                url,
                kind="list",
                mime_field="listContentTypes",
            )
            doc = document(response)
            items = parse_sia_list(doc)
            normalized_urls = [
                self._canonical(urljoin(url, item["url"]), "detail")
                for item in items
            ]
            signature = checksum(normalized_urls)
            if signature in seen_pages:
                raise SourceSchemaError("SIA list page repeated.")
            seen_pages.add(signature)
            page_dates = [parse_source_datetime(item["published"]) for item in items]
            if any(value is None for value in page_dates):
                raise SourceSchemaError("SIA list date is invalid.")
            newest = max(page_dates)
            oldest = min(page_dates)
            if prior_oldest and newest > prior_oldest:
                raise SourceSchemaError("SIA list dates moved forward across pages.")
            prior_oldest = oldest
            for item, detail_url, published in zip(
                items, normalized_urls, page_dates, strict=True
            ):
                record = self._detail(
                    detail_url,
                    list_published=published,
                    since=since,
                    until=until,
                )
                if record is None:
                    continue
                if record.external_id in seen_ids:
                    raise SourceSchemaError("SIA list repeated a provider identity.")
                seen_ids.add(record.external_id)
                resolved.add(record.external_id)
                records.append(record)
            next_links = doc.xpath(
                "//a[@rel='next']/@href | //link[@rel='next']/@href"
            )
            if len(next_links) > 1:
                raise SourceSchemaError("SIA list has multiple next cursors.")
            if not next_links:
                break
            if page == self.max_pages:
                raise SourceSchemaError("SIA pagination bound was exhausted.")
            next_url = self._canonical(urljoin(url, next_links[0]), "list")
            if urlsplit(next_url).path != (
                f"/news-events/latest-news/page/{page + 1}/"
            ):
                raise SourceSchemaError("SIA next cursor is not exactly incremental.")
            url = next_url
        records.extend(self._reconcile_missing(resolved, since, until))
        return self._finalize(records)

    def _detail(
        self,
        url,
        *,
        list_published,
        since,
        until,
        force_reconciliation=False,
        request_kind="detail",
    ):
        response = self._get_source(
            url,
            kind=request_kind,
            mime_field="detailContentTypes",
        )
        doc = document(response)
        mains = doc.xpath("//main[@id='main']")
        canonicals = doc.xpath("//link[@rel='canonical']/@href")
        if len(mains) != 1 or len(canonicals) != 1:
            raise SourceSchemaError("SIA detail main or canonical marker changed.")
        canonical = self._canonical(canonicals[0], "detail")
        jsonld = wordpress_jsonld(doc)
        post_id = wp_post_id(
            doc,
            additional_ids=jsonld["identities"],
        )
        origin = f"sia:wp-post:{post_id}"
        published_values = doc.xpath(
            "//meta[@property='article:published_time']/@content"
            " | //time/@datetime"
        )
        modified_values = doc.xpath(
            "//meta[@property='article:modified_time']/@content"
        )
        published = (
            parse_source_datetime(published_values[0])
            if published_values
            else jsonld["published"] or list_published
        )
        if (
            not force_reconciliation
            and not in_window(published, since, until)
            and origin not in self.reconciliation_ids
        ):
            return None
        modified = (
            parse_source_datetime(modified_values[0])
            if modified_values
            else jsonld["modified"]
        )
        titles = mains[0].xpath(".//h1")
        if len(titles) != 1:
            raise SourceSchemaError("SIA detail title marker changed.")
        title = " ".join(titles[0].itertext()).strip()
        body = stable_text(mains[0])
        attachments = []
        embedded_assets = []
        for index, link in enumerate(mains[0].xpath(".//a[@href]"), 1):
            raw_url = link.attrib["href"]
            mime_type = mimetypes.guess_type(urlsplit(raw_url).path)[0]
            if mime_type not in self._mimes["attachmentContentTypes"]:
                continue
            asset_url = self._canonical(urljoin(canonical, raw_url), "media")
            attachments.append(
                attachment(
                    asset_url,
                    title=" ".join(link.itertext()).strip() or f"Attachment {index}",
                    mime_type=mime_type,
                    external_id=f"{origin}:file:{checksum(asset_url)}",
                    rights_status=self.RIGHTS_STATUS,
                    locator=f"main#main:link:{index}",
                    metadata={"originIdentity": origin},
                )
            )
        for index, image in enumerate(
            mains[0].xpath(".//img[@src or @srcset]"), 1
        ):
            raw_urls = []
            if image.attrib.get("src"):
                raw_urls.append(image.attrib["src"])
            if image.attrib.get("srcset"):
                raw_urls.extend(
                    item.strip().split()[0]
                    for item in image.attrib["srcset"].split(",")
                    if item.strip()
                )
            locator = f"main#main:image:{index}"
            for variant, raw_url in enumerate(raw_urls, 1):
                mime_type = mimetypes.guess_type(urlsplit(raw_url).path)[0]
                if mime_type not in self._mimes["attachmentContentTypes"]:
                    raise SourceSchemaError("SIA image MIME is not approved.")
                asset_url = self._canonical(urljoin(canonical, raw_url), "media")
                is_chart = bool(
                    re.search(
                        r"(chart|graph)",
                        " ".join(
                            (
                                image.attrib.get("class", ""),
                                image.attrib.get("alt", ""),
                                " ".join(
                                    image.xpath("ancestor::figure[1]/@class")
                                ),
                            )
                        ),
                        re.I,
                    )
                )
                attachments.append(
                    attachment(
                        asset_url,
                        title=image.attrib.get("alt") or f"Image {index}",
                        mime_type=mime_type,
                        external_id=f"{origin}:image:{checksum(asset_url)}",
                        rights_status=self.RIGHTS_STATUS,
                        locator=f"{locator}:{variant}",
                        metadata={
                            "originIdentity": origin,
                            "contentKind": "chart" if is_chart else "image",
                        },
                    )
                )
                embedded_assets.append(
                    {
                        "kind": "chart" if is_chart else "image",
                        "url": asset_url,
                        "mimeType": mime_type,
                        "locator": f"{locator}:{variant}",
                        "rightsStatus": self.RIGHTS_STATUS,
                        "originIdentity": origin,
                    }
                )
        for index, table in enumerate(mains[0].xpath(".//table"), 1):
            embedded_assets.append(
                {
                    "kind": "table",
                    "mimeType": "text/html",
                    "locator": f"main#main:table:{index}",
                    "checksum": checksum(stable_text(table)),
                    "rightsStatus": self.RIGHTS_STATUS,
                    "originIdentity": origin,
                }
            )
        attachments = _merge_attachments(attachments)
        metadata = record_metadata(
            self,
            origin=origin,
            syndication="official_html",
            kinds=_content_kinds(mains[0], attachments),
        )
        metadata["embeddedAssets"] = embedded_assets
        metadata["storedExternalIdentity"] = {
            "wpPostId": post_id,
            "canonicalPath": urlsplit(canonical).path,
        }
        if modified and published and modified < published:
            metadata["rawModifiedAt"] = modified.isoformat()
            modified = None
        return CollectedSourceRecord(
            external_id=origin,
            canonical_url=canonical,
            title=title,
            publisher=self.source.publisher,
            published_at=published,
            modified_at=modified,
            collected_at=datetime.now(UTC),
            body_text=body,
            reconciliation_only=force_reconciliation or not in_window(
                published, since, until
            ),
            raw_checksum=checksum(
                {
                    "body": body,
                    "attachments": [
                        _attachment_material(value) for value in attachments
                    ],
                    "metadata": metadata,
                }
            ),
            metadata=metadata,
            attachments=tuple(attachments),
        )

    def _reconcile_missing(self, resolved, since, until):
        records = []
        for external_id, context in sorted(self.reconciliation_records.items()):
            if external_id in resolved:
                continue
            match = re.fullmatch(r"sia:wp-post:(\d+)", external_id)
            if not match:
                raise SourceSchemaError("SIA reconciliation identity is invalid.")
            url = f"https://www.semiconductors.org/?p={match.group(1)}"
            try:
                record = self._detail(
                    url,
                    list_published=_context_datetime(context.get("publishedAt")),
                    since=since,
                    until=until,
                    force_reconciliation=True,
                    request_kind="reconcile",
                )
                if record.external_id != external_id:
                    raise SourceSchemaError(
                        "SIA reconciliation detail identity disagrees."
                    )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {404, 410}:
                    raise
                try:
                    prior_canonical = self._canonical(
                        str(context["canonicalUrl"]), "detail"
                    )
                except SourceSchemaError:
                    prior_canonical = url
                record = self._unavailable(
                    {**context, "canonicalUrl": prior_canonical}
                )
            records.append(record)
        return records


def _positive(value, name, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} is outside its approved range.")
    return value


def _mime_set(config, field):
    values = config.get(field)
    if not isinstance(values, list) or not values:
        raise ValueError(f"{field} is required.")
    normalized = frozenset(str(value).lower() for value in values)
    if len(normalized) != len(values):
        raise ValueError(f"{field} contains duplicates.")
    return normalized


def _with_query(url, values):
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in values.items()})
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(sorted(query.items())),
            "",
        )
    )


def _page_total(doc):
    candidates = doc.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' total ')]/text()"
        " | //*[@data-total]/@data-total"
    )
    candidates.extend(
        re.findall(r"총\s*([\d,]+)\s*건", " ".join(doc.itertext()))
    )
    totals = {
        int(value.replace(",", ""))
        for raw in candidates
        if (value := re.sub(r"[^\d]", "", str(raw)))
    }
    if len(totals) > 1:
        raise SourceSchemaError("Source page has conflicting total counts.")
    return next(iter(totals), None)


def _krx_list_items(doc, pattern):
    items = []
    for table in doc.xpath("//table[.//thead//th]"):
        headers = [
            re.sub(r"\s+", "", " ".join(node.itertext()))
            for node in table.xpath(".//thead//th")
        ]
        date_indexes = [
            index
            for index, value in enumerate(headers)
            if value in {"공시일자", "공시일시", "접수일자", "제출일", "공시일"}
        ]
        title_indexes = [
            index
            for index, value in enumerate(headers)
            if value in {"공시제목", "보고서명", "제목"}
        ]
        if len(date_indexes) != 1:
            continue
        date_index = date_indexes[0]
        for row in table.xpath(".//tbody/tr"):
            cells = row.xpath("./th|./td")
            if date_index >= len(cells):
                raise SourceSchemaError("KRX row does not match its headers.")
            matches = []
            for node in row.xpath(".//*[@onclick]"):
                match = pattern.fullmatch(node.attrib.get("onclick", ""))
                if match:
                    matches.append((node, match.group(1)))
            if not matches:
                continue
            if len(matches) != 1:
                raise SourceSchemaError(
                    "KRX row has conflicting receipt markers."
                )
            node, acpt_no = matches[0]
            date_text = " ".join(cells[date_index].itertext()).strip()
            if parse_source_datetime(date_text) is None:
                raise SourceSchemaError(
                    "KRX row publication date marker changed."
                )
            title = " ".join(node.itertext()).strip()
            if not title and title_indexes and title_indexes[0] < len(cells):
                title = " ".join(cells[title_indexes[0]].itertext()).strip()
            if not title:
                raise SourceSchemaError("KRX row title is empty.")
            items.append(
                {
                    "acptNo": acpt_no,
                    "title": title,
                    "published": date_text,
                }
            )
    return items


def _reject_krx_error_page(value):
    lowered = value.lower()
    if any(
        marker in lowered
        for marker in (
            "please log in",
            "로그인",
            "access denied",
            "captcha",
            "web application firewall",
            "요청하신 페이지를 찾을 수 없습니다",
        )
    ):
        raise SourceSchemaError("KRX returned a login, WAF, or error page.")


def _krx_static_url(wrapper_text, base):
    try:
        doc = html.fromstring(wrapper_text)
    except ValueError as exc:
        raise SourceSchemaError("KRX searchContents wrapper is malformed.") from exc
    raw_targets = doc.xpath(
        "//iframe/@src | //frame/@src | //object/@data | //embed/@src"
    )
    if not raw_targets:
        raw_targets = re.findall(
            r"['\"]((?:/|https://kind\.krx\.co\.kr/)"
            r"(?:common|disclosure|html|viewer|repository|upload|download)"
            r"/[^'\"]+)['\"]",
            wrapper_text,
        )
    resolved = {urljoin(base, value) for value in raw_targets}
    if len(resolved) != 1:
        raise SourceSchemaError(
            "KRX wrapper must expose exactly one static document."
        )
    return next(iter(resolved))


def _krx_correction_of(init_text, current):
    matches = re.findall(
        r"(?:beforeAcptNo|orgAcptNo|oriAcptNo)\D+(\d{14})",
        init_text,
        re.I,
    )
    values = {value for value in matches if value != current}
    if len(values) > 1:
        raise SourceSchemaError("KRX correction origin markers disagree.")
    return next(iter(values), None)


def _response_mime(response):
    value = (
        response.headers.get("content-type", "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    if not value:
        raise SourceSchemaError("Source response MIME is missing.")
    return value


def _content_kinds(node, attachments):
    kinds = {"html"}
    if node.xpath(".//table"):
        kinds.add("table")
    if node.xpath(".//img"):
        kinds.add("image")
    for item in attachments:
        if item.metadata.get("contentKind") == "chart":
            kinds.add("chart")
        if item.mime_type == "application/pdf":
            kinds.add("pdf")
        elif item.mime_type and (
            "spreadsheet" in item.mime_type or item.mime_type == "application/vnd.ms-excel"
        ):
            kinds.add("spreadsheet")
        elif item.mime_type and item.mime_type.startswith("image/"):
            kinds.add("image")
        elif item.mime_type and item.mime_type.startswith("audio/"):
            kinds.add("audio")
        elif item.mime_type and item.mime_type.startswith("video/"):
            kinds.add("video")
    return kinds


def _attachment_material(value):
    return {
        "url": value.url,
        "title": value.title,
        "mimeType": value.mime_type,
        "externalId": value.external_id,
        "rightsStatus": value.rights_status,
        "metadata": value.metadata,
    }


def _attachment_from_context(value):
    if not isinstance(value, dict):
        raise SourceSchemaError("Stored attachment context is invalid.")
    return SourceAttachment(
        url=str(value["url"]),
        title=str(value["title"]),
        mime_type=value.get("mime_type"),
        external_id=value.get("external_id"),
        size_bytes=value.get("size_bytes"),
        checksum=value.get("checksum"),
        rights_status=value.get("rights_status"),
        metadata=dict(value.get("metadata") or {}),
    )


def _merge_attachments(values):
    merged = {}
    for value in values:
        key = value.external_id
        if not key:
            raise SourceSchemaError("Source attachment identity is missing.")
        existing = merged.get(key)
        if existing is not None and _attachment_material(existing) != _attachment_material(value):
            raise SourceSchemaError("Source attachment identity has conflicting metadata.")
        merged[key] = value
    return [merged[key] for key in sorted(merged)]


def _context_datetime(value):
    return value if isinstance(value, datetime) else parse_source_datetime(value)


def _stable_alias(value):
    text = str(value).strip()
    if "://" not in text:
        return text
    parsed = urlsplit(text)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise SourceSchemaError("Feed provider alias URL is invalid.")
    retained = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key == "p" and item.isdigit():
            retained.append((key, item))
        elif key.lower().startswith("utm_") or key.lower() in {
            "ref",
            "source",
        }:
            continue
        else:
            raise SourceSchemaError("Feed provider alias query is not stable.")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.hostname.lower(),
            re.sub(r"/+", "/", parsed.path or "/"),
            urlencode(sorted(retained)),
            "",
        )
    )


def _wordpress_link_ids(doc, response):
    values = doc.xpath(
        "//link[@rel='shortlink']/@href"
        " | //link[contains(@href, '/wp-json/wp/v2/posts/')]/@href"
    )
    values.extend(
        re.findall(
            r"<([^>]+/wp-json/wp/v2/posts/\d+)>",
            response.headers.get("link", ""),
            re.I,
        )
    )
    result = set()
    for value in values:
        match = re.search(
            r"(?:[?&]p=|/wp-json/wp/v2/posts/)(\d+)(?:\D|$)",
            value,
        )
        if match:
            result.add(int(match.group(1)))
    return result
