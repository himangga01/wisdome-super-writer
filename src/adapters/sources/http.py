from __future__ import annotations

import email.utils
import hashlib
import json
import re
import time
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import (
    parse_qsl,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlsplit,
    urlunsplit,
)
from zoneinfo import ZoneInfo

import httpx
from defusedxml import ElementTree

from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)
from wisdome_writer.infrastructure.http_safety import (
    redact_url,
    redact_urls_in_text,
    safe_get,
    safe_post_form,
)
from wisdome_writer.infrastructure.secrets import SecretResolver

from .base import CollectedSourceRecord, SourceAttachment

MAX_RESPONSE_BYTES = 12 * 1024 * 1024
ATTACHMENT_EXTENSIONS = (".pdf", ".hwp", ".hwpx", ".xlsx", ".xls", ".csv")
_SEOUL = ZoneInfo("Asia/Seoul")
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "apikey",
        "api_key",
        "authorization",
        "awsaccesskeyid",
        "client_secret",
        "credential",
        "expires",
        "key",
        "key_pair_id",
        "password",
        "policy",
        "security_token",
        "secret",
        "sig",
        "servicekey",
        "service_key",
        "signature",
        "subscription_key",
        "subscriptionkey",
        "token",
        "x_amz_algorithm",
        "x_amz_credential",
        "x_amz_date",
        "x_amz_expires",
        "x_amz_security_token",
        "x_amz_signature",
        "x_goog_algorithm",
        "x_goog_credential",
        "x_goog_date",
        "x_goog_expires",
        "x_goog_security_token",
        "x_goog_signature",
    }
)
_COMPACT_SENSITIVE_KEYS = frozenset(
    re.sub(r"[^a-z0-9]", "", value)
    for value in _SENSITIVE_QUERY_KEYS
)
_EPHEMERAL_QUERY_KEYS = frozenset(
    {
        "callback",
        "jsessionid",
        "session",
        "sessionid",
        "phpsessid",
        "timestamp",
        "trackingid",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)
_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_AUTHENTICATED_REQUEST_PROFILES = {
    "housing_applyhome": {
        "secretRef": "env://DATA_GO_KR_SERVICE_KEY",
        "host": "api.odcloud.kr",
        "paths": frozenset(
            {
                "/api/ApplyhomeInfoDetailSvc/v1/getAPTLttotPblancDetail",
                "/api/ApplyhomeInfoDetailSvc/v1/getUrbtyOfctlLttotPblancDetail",
                "/api/ApplyhomeInfoDetailSvc/v1/getRemndrLttotPblancDetail",
                "/api/ApplyhomeInfoDetailSvc/v1/getPblPvtRentLttotPblancDetail",
                "/api/ApplyhomeInfoDetailSvc/v1/getOPTLttotPblancDetail",
            }
        ),
    },
    "housing_lh": {
        "secretRef": "env://DATA_GO_KR_SERVICE_KEY",
        "host": "apis.data.go.kr",
        "paths": frozenset(
            {
                "/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1",
                "/B552555/lhLeaseNoticeDtlInfo1/getLeaseNoticeDtlInfo1",
                "/B552555/lhLeaseNoticeSplInfo1/getLeaseNoticeSplInfo1",
            }
        ),
    },
    "open_data_json": {
        "secretRef": "env://DATA_GO_KR_SERVICE_KEY",
        "host": "api.odcloud.kr",
        "paths": frozenset(
            {
                "/api/ApplyhomeInfoDetailSvc/v1/getAPTLttotPblancDetail",
            }
        ),
    },
}
_OPEN_DATA_CANONICAL_QUERY_KEYS = frozenset(
    {
        "HOUSE_MANAGE_NO",
        "PBLANC_NO",
        "CCR_CNNT_SYS_DS_CD",
        "PAN_ID",
        "UPP_AIS_TP_CD",
        "AIS_TP_CD",
        "houseManageNo",
        "pblancNo",
        "ccrCnntSysDsCd",
        "panId",
        "uppAisTpCd",
        "aisTpCd",
        "id",
        "noticeId",
        "fileId",
        "FILE_ID",
        "ATCH_FILE_ID",
        "fileSn",
        "FILE_SN",
        "seq",
        "gv_url",
        "gv_menuId",
        "gv_param",
    }
)


class SourceSchemaError(ValueError):
    """The approved source returned a shape that cannot be identified safely."""


def parse_source_datetime(value: Any) -> datetime | None:
    """Parse official-source timestamps and normalize them to UTC."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
        if parsed is None:
            for pattern in (
                "%Y.%m.%d",
                "%Y-%m-%d",
                "%Y/%m/%d",
                "%Y%m%d",
                "%Y.%m.%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y%m%d%H%M%S",
            ):
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SEOUL)
    return parsed.astimezone(UTC)


def canonical_public_url(
    url: str,
    *,
    allowed_query_keys: Collection[str] | None = None,
) -> str:
    """Keep stable public identity parameters while removing credentials and sessions."""

    try:
        parsed = urlsplit(str(url).strip())
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise SourceSchemaError("Source URL is invalid.") from None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        raise SourceSchemaError("Source URL must use HTTP or HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise SourceSchemaError("Source URL userinfo is not allowed.")

    allowed = (
        {str(key).lower() for key in allowed_query_keys}
        if allowed_query_keys is not None
        else None
    )
    query: list[tuple[str, str]] = []
    for key, value in parse_qsl(
        parsed.query,
        keep_blank_values=True,
        strict_parsing=False,
    ):
        normalized_key = key.lower().replace("-", "_")
        if (
            normalized_key in _SENSITIVE_QUERY_KEYS
            or normalized_key in _EPHEMERAL_QUERY_KEYS
        ):
            continue
        if allowed is not None and key.lower() not in allowed:
            continue
        query.append((key, value))
    query.sort(key=lambda item: (item[0].lower(), item[1]))

    host = hostname.encode("idna").decode("ascii").lower()
    default_port = 443 if scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return urlunsplit(
        (
            scheme,
            authority,
            parsed.path or "/",
            urlencode(query, doseq=True, safe=":,[]"),
            "",
        )
    )


def sanitize_source_payload(
    value: Any,
    *,
    allowed_hosts: Collection[str],
    allowed_query_keys: Collection[str] = (),
    base_url: str | None = None,
) -> Any:
    """Canonicalize URL-shaped payload values before durable persistence."""

    normalized_hosts = {
        str(host).rstrip(".").encode("idna").decode("ascii").lower()
        for host in allowed_hosts
    }

    def replace_url(raw_url: str) -> str:
        canonical = canonical_public_url(
            raw_url,
            allowed_query_keys=allowed_query_keys,
        )
        hostname = (
            urlsplit(canonical).hostname or ""
        ).rstrip(".").lower()
        if hostname not in normalized_hosts:
            return "<redacted-external-url>"
        return canonical

    def sanitize(item: Any) -> Any:
        if isinstance(item, Mapping):
            sanitized: dict[str, Any] = {}
            for key, child in item.items():
                key_text = str(key)
                normalized_key = (
                    key_text.strip().lower().replace("-", "_")
                )
                compact_key = re.sub(
                    r"[^a-z0-9]",
                    "",
                    normalized_key,
                )
                sanitized[key_text] = (
                    "<redacted-secret>"
                    if (
                        normalized_key in _SENSITIVE_QUERY_KEYS
                        or compact_key in _COMPACT_SENSITIVE_KEYS
                    )
                    else sanitize(child)
                )
            return sanitized
        if isinstance(item, list):
            return [sanitize(child) for child in item]
        if isinstance(item, tuple):
            return [sanitize(child) for child in item]
        if not isinstance(item, str):
            return item

        stripped = item.strip()
        if base_url and stripped.startswith(("/", "./", "../")):
            return replace_url(urljoin(base_url, stripped))

        def replace(match: re.Match[str]) -> str:
            return replace_url(match.group(0).rstrip(".,);"))

        return _URL_PATTERN.sub(replace, item)

    return sanitize(value)


def _path_value(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def extract_json_rows(
    payload: Any,
) -> tuple[list[dict[str, Any]], int | None]:
    """Read only explicitly approved ODCloud/data.go.kr envelopes."""

    if not isinstance(payload, Mapping):
        raise SourceSchemaError(
            "Source response is not an approved JSON object envelope."
        )

    if "data" in payload:
        rows = _mapping_rows(
            payload["data"],
            envelope_name="ODCloud data",
        )
        total_count = _optional_total_count(
            payload.get("totalCount"),
            field_name="ODCloud totalCount",
        )
        match_count = _optional_total_count(
            payload.get("matchCount"),
            field_name="ODCloud matchCount",
        )
        if (
            match_count is not None
            and total_count is not None
            and match_count > total_count
        ):
            raise SourceSchemaError(
                "ODCloud matchCount exceeds totalCount."
            )
        return rows, (
            match_count
            if match_count is not None
            else total_count
        )

    body = _path_value(payload, ("response", "body"))
    if body is None:
        body = payload.get("body")
    if body is None and "items" in payload:
        body = payload
    if not isinstance(body, Mapping):
        raise SourceSchemaError(
            "Source response contains no approved item envelope."
        )
    items = body.get("items")
    if isinstance(items, Mapping):
        items = items.get("item")
    rows = _mapping_rows(
        items,
        envelope_name="data.go.kr items.item",
    )
    return rows, _optional_total_count(
        body.get("totalCount"),
        field_name="data.go.kr totalCount",
    )


def _mapping_rows(
    value: Any,
    *,
    envelope_name: str,
) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, list) and all(
        isinstance(row, Mapping) for row in value
    ):
        return [dict(row) for row in value]
    raise SourceSchemaError(
        f"{envelope_name} is not a mapping array."
    )


def _optional_total_count(
    value: Any,
    *,
    field_name: str,
) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(str(value).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise SourceSchemaError(
            f"{field_name} is not an integer."
        ) from exc
    if parsed < 0:
        raise SourceSchemaError(
            f"{field_name} must not be negative."
        )
    return parsed


def _parse_date(value: str | None) -> datetime | None:
    return parse_source_datetime(value)


def _source_datetime_in_window(
    value: datetime,
    *,
    since: datetime,
    until: datetime,
) -> bool:
    normalized_since = (
        since.replace(tzinfo=UTC)
        if since.tzinfo is None
        else since.astimezone(UTC)
    )
    normalized_until = (
        until.replace(tzinfo=UTC)
        if until.tzinfo is None
        else until.astimezone(UTC)
    )
    local_value = value.astimezone(_SEOUL)
    if (
        local_value.hour == 0
        and local_value.minute == 0
        and local_value.second == 0
        and local_value.microsecond == 0
    ):
        return (
            local_value.astimezone(UTC) <= normalized_until
            and (local_value + timedelta(days=1)).astimezone(UTC)
            > normalized_since
        )
    normalized_value = value.astimezone(UTC)
    return normalized_since <= normalized_value <= normalized_until


class _PageParser(HTMLParser):
    def __init__(self, *, ignore_noncontent: bool = False):
        super().__init__()
        self.title = ""
        self.text: list[str] = []
        self.links: list[tuple[str, str, dict[str, Any]]] = []
        self._in_title = False
        self._ignore_noncontent = ignore_noncontent
        self._ignored_depth = 0
        self._current_href: str | None = None
        self._current_link_text: list[str] = []
        self._current_link_metadata: dict[str, Any] = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if (
            self._ignore_noncontent
            and tag in {"script", "style", "noscript"}
        ):
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag in {"a", "button"}:
            target, metadata = _html_link_target(values)
            if target is not None or metadata.get("unresolvedAttachment"):
                self._current_href = target or ""
                self._current_link_metadata = metadata
                self._current_link_text = []
        elif tag == "input":
            target, metadata = _html_link_target(values)
            if target is not None or metadata.get("unresolvedAttachment"):
                self.links.append(
                    (
                        target or "",
                        str(values.get("value", "")).strip(),
                        metadata,
                    )
                )

    def handle_endtag(self, tag):
        if (
            self._ignore_noncontent
            and tag in {"script", "style", "noscript"}
        ):
            self._ignored_depth = max(self._ignored_depth - 1, 0)
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = False
        if (
            tag in {"a", "button"}
            and self._current_href is not None
        ):
            self.links.append(
                (
                    self._current_href,
                    " ".join(self._current_link_text).strip(),
                    dict(self._current_link_metadata),
                )
            )
            self._current_href = None
            self._current_link_text = []
            self._current_link_metadata = {}

    def handle_data(self, data):
        if self._ignored_depth:
            return
        clean = " ".join(data.split())
        if not clean:
            return
        self.text.append(clean)
        if self._in_title:
            self.title += (" " if self.title else "") + clean
        if self._current_href is not None:
            self._current_link_text.append(clean)


_HTML_LINK_ATTRIBUTES = (
    "href",
    "data-url",
    "data-href",
    "data-download-url",
    "formaction",
)
_QUOTED_HANDLER_VALUE = re.compile(
    r"""(?P<quote>["'])(?P<value>.*?)(?P=quote)"""
)
_OFFICIAL_FILE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def _html_link_target(
    attributes: Mapping[str, str | None],
) -> tuple[str | None, dict[str, Any]]:
    metadata: dict[str, Any] = {}
    target: str | None = None
    for key in _HTML_LINK_ATTRIBUTES:
        raw_value = str(attributes.get(key) or "").strip()
        if (
            raw_value
            and not raw_value.lower().startswith("javascript:")
            and raw_value != "#"
        ):
            target = raw_value
            break

    handler = str(attributes.get("onclick") or "")
    handler_values = [
        match.group("value").strip()
        for match in _QUOTED_HANDLER_VALUE.finditer(handler)
        if match.group("value").strip()
    ]
    if target is None:
        target = next(
            (
                value
                for value in handler_values
                if value.startswith(("http://", "https://", "/", "./", "../"))
                or "/" in value
                or "?" in value
            ),
            None,
        )
    official_ids = [
        value
        for value in handler_values
        if _OFFICIAL_FILE_ID.fullmatch(value)
        and value.lower()
        not in {"download", "file", "return", "void", "true", "false"}
        and value != target
    ]
    if official_ids:
        metadata["officialFileId"] = ":".join(official_ids)
    handler_is_attachment = bool(
        re.search(
            r"(download|file|atch)",
            handler,
            flags=re.IGNORECASE,
        )
    )
    if target is None and handler_is_attachment:
        metadata["unresolvedAttachment"] = True
    return target, metadata


def parse_html_page(
    value: str,
    *,
    ignore_noncontent: bool = False,
) -> tuple[
    str,
    str,
    tuple[tuple[str, str, dict[str, Any]], ...],
]:
    parser = _PageParser(ignore_noncontent=ignore_noncontent)
    parser.feed(value)
    return (
        parser.title,
        "\n".join(parser.text),
        tuple(parser.links),
    )


class HttpSourceAdapter:
    def __init__(self, *, source, config):
        self.source = source
        self.config = config
        entrypoints = config.get("entrypoints")
        if (
            not isinstance(entrypoints, list)
            or not entrypoints
            or any(
                not isinstance(entrypoint, str) or not entrypoint
                for entrypoint in entrypoints
            )
        ):
            raise ValueError(
                "Source adapter requires a probe entrypoint."
            )
        self.entrypoints = tuple(entrypoints)
        self.allowed_hosts = {
            host
            for host in (
                urlparse(source.base_url).hostname,
                *(
                    urlparse(value).hostname
                    for value in (
                        *entrypoints,
                        config.get("detailEntrypoint", ""),
                        config.get("supplyEntrypoint", ""),
                    )
                    if value
                ),
            )
            if host
        }
        record_hosts = config.get("recordHosts", [])
        if not isinstance(record_hosts, list) or any(
            not isinstance(host, str) or not host
            for host in record_hosts
        ):
            raise ValueError(
                "Source adapter recordHosts must be a hostname array."
            )
        self.allowed_hosts.update(
            host.rstrip(".").encode("idna").decode("ascii").lower()
            for host in record_hosts
        )
        self.timeout = httpx.Timeout(20, connect=10)
        self.max_requests = _bounded_positive_int(
            config.get("maxRequests", 500),
            field_name="maxRequests",
            maximum=20000,
        )
        self.max_elapsed_seconds = _bounded_positive_int(
            config.get("maxElapsedSeconds", 900),
            field_name="maxElapsedSeconds",
            maximum=3600,
        )
        self._request_count = 0
        self._request_deadline = (
            time.monotonic() + self.max_elapsed_seconds
        )
        self._secret_resolver: SecretResolver | None = None
        self._resolved_secret: str | None = None

    def _get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        authenticated: bool = False,
        credential_parameter: str | None = None,
        expected_content_types: Collection[str] | None = None,
        url_validator=None,
    ) -> httpx.Response:
        remaining = self._request_deadline - time.monotonic()
        if remaining <= 0:
            raise SourceSchemaError(
                "Source elapsed-time budget was exhausted."
            )
        request_url = self._request_url(
            url,
            params=params,
            authenticated=authenticated,
            credential_parameter=credential_parameter,
        )
        headers = {"User-Agent": "WisdomeSuperWriter/0.1 (+admin-managed research bot)"}
        response = safe_get(
            request_url,
            max_bytes=MAX_RESPONSE_BYTES,
            timeout=self.timeout,
            allowed_hosts=self.allowed_hosts,
            headers=headers,
            max_elapsed_seconds=min(20.0, remaining),
            https_only=authenticated or bool(
                getattr(self, "_https_only", False)
            ),
            before_request=self._consume_request_budget,
            url_validator=url_validator,
        )
        response.raise_for_status()
        if expected_content_types is not None:
            allowed_types = {
                str(value).split(";", 1)[0].strip().lower()
                for value in expected_content_types
            }
            actual_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if actual_type not in allowed_types:
                raise SourceSchemaError(
                    "Source response MIME type is outside the approved "
                    "request contract."
                )
        return response

    def _consume_request_budget(self) -> None:
        if self._request_count >= self.max_requests:
            raise SourceSchemaError(
                "Source request budget was exhausted."
            )
        if time.monotonic() >= self._request_deadline:
            raise SourceSchemaError(
                "Source elapsed-time budget was exhausted."
            )
        self._request_count += 1

    def _post_form(
        self,
        url: str,
        *,
        data: Mapping[str, Any],
        expected_content_types: Collection[str] | None = None,
        url_validator=None,
    ) -> httpx.Response:
        remaining = self._request_deadline - time.monotonic()
        if remaining <= 0:
            raise SourceSchemaError("Source elapsed-time budget was exhausted.")
        response = safe_post_form(
            self._request_url(
                url,
                params=None,
                authenticated=False,
                credential_parameter=None,
            ),
            data=data,
            max_bytes=MAX_RESPONSE_BYTES,
            timeout=self.timeout,
            allowed_hosts=self.allowed_hosts,
            headers={
                "User-Agent": (
                    "WisdomeSuperWriter/0.1 "
                    "(+admin-managed research bot)"
                )
            },
            max_elapsed_seconds=min(20.0, remaining),
            before_request=self._consume_request_budget,
            https_only=bool(getattr(self, "_https_only", False)),
            url_validator=url_validator,
        )
        response.raise_for_status()
        if expected_content_types is not None:
            allowed = {
                str(value).split(";", 1)[0].strip().lower()
                for value in expected_content_types
            }
            actual = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if actual not in allowed:
                raise SourceSchemaError(
                    "Source response MIME type is outside the approved "
                    "request contract."
                )
        return response

    def _request_url(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        authenticated: bool,
        credential_parameter: str | None,
    ) -> str:
        parsed = urlsplit(url)
        query = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
        )
        for key, value in (params or {}).items():
            if value is None:
                continue
            normalized_key = str(key).lower().replace("-", "_")
            if normalized_key in _SENSITIVE_QUERY_KEYS:
                raise ValueError(
                    "Source request parameters must not contain credentials."
                )
            if isinstance(value, (list, tuple)):
                query.extend((str(key), str(item)) for item in value)
            else:
                query.append((str(key), str(value)))
        if authenticated:
            reference = self.config.get("secretRef")
            if not isinstance(reference, str) or not reference:
                raise ValueError(
                    "Authenticated source adapter requires a secretRef."
                )
            self._validate_authenticated_target(
                url=url,
                reference=reference,
                credential_parameter=credential_parameter,
            )
            if self._secret_resolver is None:
                self._secret_resolver = SecretResolver()
            if self._resolved_secret is None:
                self._resolved_secret = self._secret_resolver.resolve(
                    reference
                )
            secret = self._resolved_secret
            parameter = credential_parameter
            if parameter is None:
                parameter = (
                    "serviceKey"
                    if (parsed.hostname or "").lower() == "api.odcloud.kr"
                    else "ServiceKey"
                )
            query.append((parameter, unquote(secret)))
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(query, doseq=True, safe=":,[]"),
                "",
            )
        )

    def _validate_authenticated_target(
        self,
        *,
        url: str,
        reference: str,
        credential_parameter: str | None,
    ) -> None:
        adapter_key = str(
            self.config.get("adapterKey")
            or self.config.get("adapter")
            or ""
        )
        profile = _AUTHENTICATED_REQUEST_PROFILES.get(adapter_key)
        if profile is None or reference != profile["secretRef"]:
            raise ValueError(
                "Authenticated source credential is not bound to this adapter."
            )
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").rstrip(".").lower()
            != profile["host"]
            or parsed.port not in {None, 443}
            or parsed.path not in profile["paths"]
            or parsed.fragment
        ):
            raise ValueError(
                "Authenticated source target is outside the approved HTTPS "
                "profile."
            )
        resolved_parameter = credential_parameter or (
            "serviceKey"
            if profile["host"] == "api.odcloud.kr"
            else "ServiceKey"
        )
        if adapter_key == "housing_lh":
            allowed_parameters = {"ServiceKey", "serviceKey"}
        else:
            allowed_parameters = {"serviceKey"}
        if resolved_parameter not in allowed_parameters:
            raise ValueError(
                "Authenticated source credential parameter is not approved."
            )


class PublicHtmlAdapter(HttpSourceAdapter):
    def collect(self, *, since: datetime, until: datetime) -> list[CollectedSourceRecord]:
        now = datetime.now(UTC)
        records: list[CollectedSourceRecord] = []
        for entrypoint in self.entrypoints:
            response = self._get(entrypoint)
            safe_entrypoint = canonical_public_url(entrypoint)
            parser = _PageParser()
            parser.feed(response.text)
            attachments: list[SourceAttachment] = []
            for href, label, _metadata in parser.links:
                if not urlparse(href).path.lower().endswith(ATTACHMENT_EXTENSIONS):
                    continue
                safe_attachment_url = canonical_public_url(
                    urljoin(entrypoint, href)
                )
                safe_fallback_title = urlparse(safe_attachment_url).path.rsplit("/", 1)[-1]
                attachments.append(
                    SourceAttachment(
                        url=safe_attachment_url,
                        title=redact_urls_in_text(label) if label else safe_fallback_title,
                        rights_status=self.config.get("rightsStatus"),
                    )
                )
            published_at = _parse_date(response.headers.get("Last-Modified"))
            if published_at and not (since <= published_at <= until):
                continue
            records.append(
                CollectedSourceRecord(
                    external_id=safe_entrypoint,
                    canonical_url=safe_entrypoint,
                    title=parser.title or self.source.display_name,
                    publisher=self.source.publisher,
                    published_at=published_at,
                    collected_at=now,
                    body_text="\n".join(parser.text),
                    raw_checksum=hashlib.sha256(response.content).hexdigest(),
                    http_metadata={
                        "etag": response.headers.get("ETag"),
                        "lastModified": response.headers.get("Last-Modified"),
                        "contentType": response.headers.get(
                            "Content-Type",
                            "text/html",
                        ),
                    },
                    attachments=tuple(attachments),
                )
            )
        return records


class RssAdapter(HttpSourceAdapter):
    def collect(self, *, since: datetime, until: datetime) -> list[CollectedSourceRecord]:
        now = datetime.now(UTC)
        records: list[CollectedSourceRecord] = []
        for entrypoint in self.entrypoints:
            root = ElementTree.fromstring(self._get(entrypoint).content)
            for item in root.findall(".//item"):
                link = (item.findtext("link") or "").strip()
                safe_link = canonical_public_url(link)
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
        max_pages = _bounded_positive_int(
            self.config.get("maxPages", 20),
            field_name="maxPages",
            maximum=100,
        )
        page_size = _bounded_positive_int(
            self.config.get("pageSize", 100),
            field_name="pageSize",
            maximum=1000,
        )
        identities: set[str] = set()
        for entrypoint in self.entrypoints:
            seen_pages: set[str] = set()
            received = 0
            expected_total_count: int | None = None
            for page in range(1, max_pages + 1):
                odcloud = (
                    (urlsplit(entrypoint).hostname or "").lower()
                    == "api.odcloud.kr"
                )
                params = (
                    {"page": page, "perPage": page_size}
                    if odcloud
                    else {"pageNo": page, "numOfRows": page_size}
                )
                response = self._get(
                    entrypoint,
                    params=params,
                    authenticated=bool(self.config.get("secretRef")),
                    expected_content_types=self.config.get(
                        "apiContentTypes",
                        ("application/json",),
                    ),
                )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SourceSchemaError(
                        "Open-data source did not return JSON."
                    ) from exc
                rows, total_count = extract_json_rows(payload)
                if total_count is not None:
                    if (
                        expected_total_count is not None
                        and total_count != expected_total_count
                    ):
                        raise SourceSchemaError(
                            "Open-data total count changed during pagination."
                        )
                    expected_total_count = total_count
                page_fingerprint = canonical_hash(
                    rows,
                    schema_version=CANONICAL_HASH_SCHEMA_V1,
                )
                if page_fingerprint in seen_pages:
                    raise SourceSchemaError(
                        "Open-data pagination repeated a response page."
                    )
                seen_pages.add(page_fingerprint)
                if not rows:
                    if (
                        expected_total_count is not None
                        and received < expected_total_count
                    ):
                        raise SourceSchemaError(
                            "Open-data pagination ended before totalCount."
                        )
                    break
                received += len(rows)
                for row in rows:
                    safe_row = sanitize_source_payload(
                        row,
                        allowed_hosts=self.allowed_hosts,
                        allowed_query_keys=(
                            _OPEN_DATA_CANONICAL_QUERY_KEYS
                        ),
                        base_url=entrypoint,
                    )
                    title = str(
                        _first_value(
                            row,
                            (
                                "title",
                                "name",
                                "HOUSE_NM",
                                "PAN_NM",
                                "PBLANC_NM",
                            ),
                        )
                        or "제목 없음"
                    ).strip()
                    published = parse_source_datetime(
                        _first_value(
                            row,
                            (
                                "publishedAt",
                                "published_at",
                                "date",
                                "RCRIT_PBLANC_DE",
                                "PAN_NT_ST_DT",
                                "REG_DT",
                            ),
                        )
                    )
                    modified = parse_source_datetime(
                        _first_value(
                            row,
                            (
                                "modifiedAt",
                                "modified_at",
                                "UPD_DT",
                                "UPDT_DT",
                                "CORR_DT",
                            ),
                        )
                    )
                    observed_at = modified or published
                    if observed_at and not _source_datetime_in_window(
                        observed_at,
                        since=since,
                        until=until,
                    ):
                        continue
                    identity = _open_data_identity(row, entrypoint)
                    if identity in identities:
                        raise SourceSchemaError(
                            "Open-data response contains a duplicate stable identity."
                        )
                    identities.add(identity)
                    records.append(
                        CollectedSourceRecord(
                            external_id=identity,
                            canonical_url=_open_data_canonical_url(
                                row,
                                entrypoint,
                                allowed_hosts=self.allowed_hosts,
                            ),
                            title=title,
                            publisher=self.source.publisher,
                            published_at=published,
                            modified_at=modified,
                            collected_at=now,
                            body_text=json.dumps(
                                safe_row,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            status=source_status_from_value(
                                _first_value(
                                    row,
                                    (
                                        "status",
                                        "STATUS",
                                        "PAN_SS",
                                        "PBLANC_STATUS",
                                        "NOTICE_STATUS",
                                    ),
                                ),
                            ),
                            raw_checksum=canonical_hash(
                                safe_row,
                                schema_version=(
                                    CANONICAL_HASH_SCHEMA_V1
                                ),
                            ),
                            http_metadata={
                                "etag": response.headers.get("ETag"),
                                "lastModified": response.headers.get(
                                    "Last-Modified"
                                ),
                                "contentType": response.headers.get(
                                    "Content-Type",
                                    "application/json",
                                ),
                                "page": page,
                            },
                            metadata={
                                "structured": safe_row,
                                "datasetEndpoint": redact_url(entrypoint),
                            },
                            attachments=_open_data_attachments(
                                row,
                                base_url=entrypoint,
                                rights_status=self.config.get(
                                    "rightsStatus"
                                ),
                                allowed_hosts=self.allowed_hosts,
                            ),
                        )
                    )
                if (
                    len(rows) < page_size
                    or (
                        total_count is not None
                        and received >= total_count
                    )
                ):
                    break
                if page == max_pages:
                    raise SourceSchemaError(
                        "Open-data pagination exceeded maxPages."
                    )
            if (
                expected_total_count is not None
                and received < expected_total_count
            ):
                raise SourceSchemaError(
                    "Open-data pagination did not cover totalCount."
                )
        return sorted(records, key=lambda record: record.external_id)


def _bounded_positive_int(
    value: Any,
    *,
    field_name: str,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise SourceSchemaError(f"{field_name} must be an integer.") from None
    if parsed < 1 or parsed > maximum:
        raise SourceSchemaError(
            f"{field_name} must be between 1 and {maximum}."
        )
    return parsed


def _first_value(
    row: Mapping[str, Any],
    field_names: Collection[str],
) -> Any:
    for field_name in field_names:
        value = row.get(field_name)
        if value is not None and value != "":
            return value
    return None


def _applyhome_category(entrypoint: str) -> str:
    operation = urlsplit(entrypoint).path.rsplit("/", 1)[-1].lower()
    categories = (
        ("apt", "apt"),
        ("urbty", "urban"),
        ("remndr", "remainder"),
        ("pblpvtrent", "public_private_rent"),
        ("opt", "optional_supply"),
    )
    for marker, category in categories:
        if marker in operation:
            return category
    return "housing"


def _open_data_identity(
    row: Mapping[str, Any],
    entrypoint: str,
) -> str:
    house_manage_no = _first_value(
        row,
        ("HOUSE_MANAGE_NO", "houseManageNo"),
    )
    pblanc_no = _first_value(row, ("PBLANC_NO", "pblancNo"))
    if house_manage_no is not None and pblanc_no is not None:
        return (
            f"applyhome:{_applyhome_category(entrypoint)}:"
            f"{house_manage_no}:{pblanc_no}"
        )
    pan_id = _first_value(row, ("PAN_ID", "panId"))
    ccr = _first_value(
        row,
        ("CCR_CNNT_SYS_DS_CD", "ccrCnntSysDsCd"),
    )
    upper_type = _first_value(
        row,
        ("UPP_AIS_TP_CD", "uppAisTpCd"),
    )
    item_type = _first_value(row, ("AIS_TP_CD", "aisTpCd")) or "-"
    if pan_id is not None and ccr is not None and upper_type is not None:
        return f"lh:{ccr}:{pan_id}:{upper_type}:{item_type}"
    identity = _first_value(
        row,
        ("id", "ID", "noticeId", "NOTICE_ID", "dataId"),
    )
    if identity is None:
        raise SourceSchemaError(
            "Open-data item has no approved stable identity fields."
        )
    return f"open-data:{identity}"


def _open_data_canonical_url(
    row: Mapping[str, Any],
    entrypoint: str,
    *,
    allowed_hosts: Collection[str],
) -> str:
    value = _first_value(
        row,
        (
            "PBLANC_URL",
            "DTL_URL",
            "canonicalUrl",
            "url",
            "URL",
        ),
    )
    if isinstance(value, str) and value.strip():
        canonical = canonical_public_url(
            urljoin(entrypoint, value.strip()),
            allowed_query_keys=_OPEN_DATA_CANONICAL_QUERY_KEYS,
        )
        _require_allowed_record_host(
            canonical,
            allowed_hosts=allowed_hosts,
        )
        return canonical
    identity_pairs = []
    for field_name in (
        "HOUSE_MANAGE_NO",
        "PBLANC_NO",
        "CCR_CNNT_SYS_DS_CD",
        "PAN_ID",
        "UPP_AIS_TP_CD",
        "AIS_TP_CD",
        "id",
        "noticeId",
    ):
        if row.get(field_name) is not None and row.get(field_name) != "":
            identity_pairs.append((field_name, str(row[field_name])))
    parsed = urlsplit(entrypoint)
    canonical = canonical_public_url(
        urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(identity_pairs, safe=":,[]"),
                "",
            )
        ),
        allowed_query_keys=_OPEN_DATA_CANONICAL_QUERY_KEYS,
    )
    _require_allowed_record_host(
        canonical,
        allowed_hosts=allowed_hosts,
    )
    return canonical


def _require_allowed_record_host(
    url: str,
    *,
    allowed_hosts: Collection[str],
) -> None:
    hostname = (urlsplit(url).hostname or "").rstrip(".").lower()
    if hostname not in {
        str(host).rstrip(".").lower() for host in allowed_hosts
    }:
        raise SourceSchemaError(
            "Open-data item URL uses an unapproved host."
        )


def source_status_from_value(
    value: Any,
) -> str:
    material = str(value or "").strip().lower()
    if any(token in material for token in ("접근 불가", "unavailable")):
        return "unavailable"
    if any(
        token in material
        for token in ("취소공고", "공고취소", "철회", "retracted")
    ):
        return "retracted"
    if any(
        token in material
        for token in ("정정공고", "정정 공고", "정정", "corrected")
    ):
        return "corrected"
    return "active"


def _attachment_mime_type(url: str) -> str | None:
    path = urlsplit(url).path.lower()
    if path.endswith(".pdf"):
        return "application/pdf"
    if path.endswith(".hwp"):
        return "application/haansofthwp"
    if path.endswith(".hwpx"):
        return "application/hwp+zip"
    if path.endswith(".xlsx"):
        return (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
    if path.endswith(".xls"):
        return "application/vnd.ms-excel"
    if path.endswith(".csv"):
        return "text/csv"
    return None


def _open_data_attachments(
    row: Mapping[str, Any],
    *,
    base_url: str,
    rights_status: str | None,
    allowed_hosts: Collection[str],
) -> tuple[SourceAttachment, ...]:
    attachments: list[SourceAttachment] = []
    seen: set[str] = set()
    for key, value in row.items():
        normalized_key = str(key).lower()
        if not isinstance(value, str):
            continue
        if not value.startswith(
            ("http://", "https://", "/", "./", "../")
        ):
            continue
        if not any(
            marker in normalized_key
            for marker in ("attach", "file", "download", "첨부")
        ):
            continue
        url = canonical_public_url(
            urljoin(base_url, value),
            allowed_query_keys=_OPEN_DATA_CANONICAL_QUERY_KEYS,
        )
        _require_allowed_record_host(
            url,
            allowed_hosts=allowed_hosts,
        )
        if url in seen:
            continue
        seen.add(url)
        filename = urlsplit(url).path.rsplit("/", 1)[-1] or str(key)
        attachments.append(
            SourceAttachment(
                url=url,
                title=filename,
                mime_type=_attachment_mime_type(url),
                external_id=str(
                    _first_value(
                        row,
                        ("fileId", "FILE_ID", "ATCH_FILE_ID"),
                    )
                    or url
                ),
                rights_status=rights_status,
                metadata={"sourceField": str(key)},
            )
        )
    return tuple(sorted(attachments, key=lambda item: item.url))
