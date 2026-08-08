from __future__ import annotations

import email.utils
import hashlib
import json
import random
import re
import time
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
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
from urllib import robotparser
from zoneinfo import ZoneInfo

import httpx
from django.conf import settings
from defusedxml import ElementTree
from redis import Redis
from redis.exceptions import RedisError

from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)
from wisdome_writer.infrastructure.http_safety import (
    HttpSafetyError,
    OutboundRequestFailed,
    OutboundResponseMimeRejected,
    OutboundResponseTooLarge,
    UnsafeOutboundUrl,
    redact_url,
    redact_urls_in_text,
    safe_get,
    safe_post_form,
)
from wisdome_writer.infrastructure.secrets import SecretResolver

from .base import CollectedSourceRecord, SourceAttachment
from .errors import SourceAccessError

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


def _frozen_material_hash(value: Any) -> str:
    return canonical_hash(
        value,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
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


class SourceSchemaError(SourceAccessError, ValueError):
    """The approved source returned a shape that cannot be identified safely."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code="source_schema_invalid",
            category="schema",
            detail=redact_urls_in_text(detail),
            remediation="Review the frozen source contract and source response.",
        )


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


class _RedisSourceLimiter:
    """Redis-backed source-wide rate and concurrency boundary."""

    _RATE_SCRIPT = """
local now = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens') or capacity)
local updated = tonumber(redis.call('HGET', KEYS[1], 'updated') or now)
tokens = math.min(capacity, tokens + math.max(0, now - updated) * refill)
if tokens < 1 then
  redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated', now)
  redis.call('EXPIRE', KEYS[1], ARGV[4])
  return {0, math.ceil((1 - tokens) / refill)}
end
redis.call('HSET', KEYS[1], 'tokens', tokens - 1, 'updated', now)
redis.call('EXPIRE', KEYS[1], ARGV[4])
return {1, 0}
"""
    _LEASE_SCRIPT = """
local now = tonumber(ARGV[1])
local expiry = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZSCORE', KEYS[1], ARGV[4]) or redis.call('ZCARD', KEYS[1]) < limit then
  redis.call('ZADD', KEYS[1], expiry, ARGV[4])
  redis.call('PEXPIRE', KEYS[1], math.max(1, expiry - now))
  return {1, 0}
end
local next_expiry = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')[2]
return {0, math.max(1, math.ceil((tonumber(next_expiry) - now) / 1000))}
"""

    def __init__(
        self,
        *,
        source_id: Any,
        traffic_scope: str,
        requests_per_minute: int,
        burst: int,
        max_concurrency: int,
        lease_seconds: int,
        operation_id: str,
    ) -> None:
        self._prefix = "wisdome:source-http:" + hashlib.sha256(
            f"{source_id}:{traffic_scope}".encode("utf-8")
        ).hexdigest()
        self._rpm = requests_per_minute
        self._burst = burst
        self._max_concurrency = max_concurrency
        self._lease_seconds = lease_seconds
        self._operation_id = operation_id
        self._client: Redis | None = None

    @property
    def _redis(self) -> Redis:
        if self._client is None:
            try:
                self._client = Redis.from_url(
                    settings.REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
            except Exception as exc:
                raise _infrastructure_error("Redis limiter is unavailable.") from exc
        return self._client

    def reserve_poll(self, interval_seconds: int) -> None:
        try:
            poll_key = f"{self._prefix}:poll"
            if self._redis.get(poll_key) == self._operation_id:
                return
            acquired = self._redis.set(
                poll_key,
                self._operation_id,
                nx=True,
                ex=max(1, interval_seconds),
            )
            if acquired:
                return
            delay = self._redis.ttl(poll_key)
        except RedisError as exc:
            raise _infrastructure_error("Redis poll reservation failed.") from exc
        raise SourceAccessError(
            code="source_poll_interval_reserved",
            category="transient",
            detail="Source polling is reserved by another operation.",
            remediation="Retry after the source poll interval reservation expires.",
            retryable=True,
            retry_after_seconds=max(1, int(delay) if isinstance(delay, int) else 1),
        )

    def before_request(self, *, max_requests: int) -> int:
        now_ms = int(time.time() * 1000)
        lease_expiry = now_ms + self._lease_seconds * 1000
        try:
            lease = self._redis.eval(
                self._LEASE_SCRIPT,
                1,
                f"{self._prefix}:leases",
                now_ms,
                lease_expiry,
                self._max_concurrency,
                self._operation_id,
            )
            if not lease or int(lease[0]) != 1:
                raise SourceAccessError(
                    code="source_concurrency_limited",
                    category="transient",
                    detail="Source concurrency limit is currently exhausted.",
                    remediation="Retry after an active source operation lease expires.",
                    retryable=True,
                    retry_after_seconds=max(1, int(lease[1]) if lease else 1),
                )
            rate = self._redis.eval(
                self._RATE_SCRIPT,
                1,
                f"{self._prefix}:rate",
                time.time(),
                self._rpm / 60,
                self._burst,
                max(60, int(120 * self._burst / max(self._rpm, 1))),
            )
            if not rate or int(rate[0]) != 1:
                raise SourceAccessError(
                    code="source_rate_limited",
                    category="transient",
                    detail="Source rate limit is currently exhausted.",
                    remediation="Retry after the source rate limit refills.",
                    retryable=True,
                    retry_after_seconds=max(1, int(rate[1]) if rate else 1),
                )
            budget_key = (
                f"{self._prefix}:budget:"
                + hashlib.sha256(
                    self._operation_id.encode("utf-8")
                ).hexdigest()
            )
            request_count = int(self._redis.incr(budget_key))
            if request_count == 1:
                self._redis.expire(
                    budget_key,
                    max(self._lease_seconds * 2, 3600),
                )
            if request_count > max_requests:
                raise SourceAccessError(
                    code="source_request_budget_exhausted",
                    category="policy",
                    detail="Durable source request budget was exhausted.",
                    remediation=(
                        "Reduce the source operation scope or approve a "
                        "larger request budget."
                    ),
                )
            return request_count
        except SourceAccessError:
            raise
        except RedisError as exc:
            raise _infrastructure_error("Redis request limiter failed.") from exc

    def release(self) -> None:
        try:
            self._redis.zrem(
                f"{self._prefix}:leases",
                self._operation_id,
            )
        except RedisError as exc:
            raise _infrastructure_error(
                "Redis concurrency lease release failed."
            ) from exc


def _infrastructure_error(detail: str) -> SourceAccessError:
    return SourceAccessError(
        code="source_limiter_infrastructure_unavailable",
        category="infrastructure",
        detail=detail,
        remediation="Restore the configured Redis service before retrying.",
        retryable=True,
    )


class HttpSourceAdapter:
    def __init__(self, *, source, config):
        self.source = source
        self.config = config
        self._access_policy = self._load_access_policy(config)
        expected_access_policy_hash = config.get(
            "accessPolicyHash"
        )
        if (
            not isinstance(expected_access_policy_hash, str)
            or expected_access_policy_hash
            != _frozen_material_hash(self._access_policy)
        ):
            raise SourceAccessError(
                code="source_access_policy_hash_mismatch",
                category="security",
                detail="Frozen source access policy hash is inconsistent.",
                remediation=(
                    "Re-approve the source snapshot before collection."
                ),
            )
        self._access_policy_hash = expected_access_policy_hash
        self._runtime_mode = str(config.get("_runtimeMode", "collection"))
        runtime_scope_mode = {
            "collection": "collection",
            "attachment": "collection",
            "source_check": "source_check",
        }.get(self._runtime_mode)
        allowed_runtime_modes = {
            "collection": {"collection"},
            "source_check": {"source_check"},
            "collection_and_source_check": {"collection", "source_check"},
        }.get(self._access_policy["trafficScope"], set())
        if runtime_scope_mode not in allowed_runtime_modes:
            raise self._policy_error(
                "Source runtime mode is outside the frozen traffic scope."
            )
        self._operation_id = str(
            config.get("_operationId")
            or config.get("operationId")
            or hashlib.sha256(
                f"{source.id}:{time.time_ns()}".encode("utf-8")
            ).hexdigest()
        )
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
        self.allowed_hosts.update(
            str(origin["host"]).rstrip(".").encode("idna").decode("ascii").lower()
            for origin in self._access_policy["originPolicies"]
        )
        self.timeout = httpx.Timeout(20, connect=10)
        self.max_requests = _bounded_positive_int(
            self._access_policy["maxRequests"],
            field_name="maxRequests",
            maximum=20000,
        )
        self.max_elapsed_seconds = _bounded_positive_int(
            self._access_policy["maxElapsedSeconds"],
            field_name="maxElapsedSeconds",
            maximum=3600,
        )
        self._request_count = 0
        self._request_deadline = (
            time.monotonic() + self.max_elapsed_seconds
        )
        self._secret_resolver: SecretResolver | None = None
        self._resolved_secret: str | None = None
        self._robots_checked: dict[str, robotparser.RobotFileParser] = {}
        rate_policy = config.get("rateLimitPolicy")
        if not isinstance(rate_policy, Mapping):
            raise self._policy_error("Source rate limit policy is missing.")
        rpm = rate_policy.get("rateLimitPerMinute", rate_policy.get("requestsPerMinute"))
        burst = rate_policy.get("burst")
        concurrency = rate_policy.get("maxConcurrency")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (rpm, burst, concurrency)):
            raise self._policy_error("Source rate limit policy is invalid.")
        self._limiter = _RedisSourceLimiter(
            source_id=source.id,
            traffic_scope=self._access_policy["trafficScope"],
            requests_per_minute=int(rpm),
            burst=int(burst),
            max_concurrency=int(concurrency),
            lease_seconds=self.max_elapsed_seconds,
            operation_id=self._operation_id,
        )
        if self._runtime_mode == "collection":
            poll_interval = config.get("pollIntervalSeconds")
            if isinstance(poll_interval, bool) or not isinstance(poll_interval, int) or poll_interval < 1:
                raise self._policy_error("Source poll interval is invalid.")
            self._limiter.reserve_poll(poll_interval)

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def access_policy_hash(self) -> str:
        return self._access_policy_hash

    def close(self) -> None:
        self._limiter.release()

    def _policy_error(self, detail: str) -> SourceAccessError:
        return SourceAccessError(
            code="source_access_policy_denied",
            category="policy",
            detail=detail,
            remediation="Approve a complete frozen source access policy before collection.",
        )

    def _load_access_policy(self, config: Mapping[str, Any]) -> dict[str, Any]:
        raw = config.get("accessPolicyV1", config.get("accessPolicy"))
        if not isinstance(raw, Mapping):
            raise self._policy_error("Source access policy is missing.")
        policy = dict(raw)
        if policy.get("schemaVersion") != "source-access-policy-v1" or policy.get("decision") != "approved":
            raise self._policy_error("Source access policy is not approved.")
        if not isinstance(policy.get("reviewedAt"), str) or not isinstance(policy.get("userAgent"), str) or not policy["userAgent"].strip():
            raise self._policy_error("Source access policy review metadata is invalid.")
        if policy.get("trafficScope") not in {
            "collection",
            "source_check",
            "collection_and_source_check",
        }:
            raise self._policy_error("Source access policy traffic scope is invalid.")
        legal_decisions = {
            "approved",
            "not_applicable",
            "not_applicable_official_api",
        }
        if (
            policy.get("termsDecision") not in legal_decisions
            or policy.get("licenseDecision") not in legal_decisions
        ):
            raise self._policy_error("Source terms or license decision is not approved.")
        for field_name, minimum, maximum in (
            ("maxHttpAttempts", 1, 5),
            ("maxRetryDelaySeconds", 0, 3600),
            ("maxRedirects", 0, 20),
            ("maxRequests", 1, 20000),
            ("maxElapsedSeconds", 1, 3600),
        ):
            value = policy.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise self._policy_error(f"Source access policy {field_name} is invalid.")
        origins = policy.get("originPolicies")
        if not isinstance(origins, list) or not origins:
            raise self._policy_error("Source access policy has no origin policies.")
        for origin in origins:
            if not isinstance(origin, Mapping):
                raise self._policy_error("Source origin policy is invalid.")
            if not isinstance(origin.get("host"), str) or not origin["host"].strip():
                raise self._policy_error("Source origin host is invalid.")
            if not isinstance(origin.get("purposes"), list) or not isinstance(origin.get("methods"), list) or not isinstance(origin.get("pathPrefixes"), list):
                raise self._policy_error("Source origin policy is incomplete.")
            if origin.get("robotsMode") not in {"runtime_fetch", "not_applicable_official_api"}:
                raise self._policy_error("Source origin robots mode is invalid.")
        return policy

    def _get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        authenticated: bool = False,
        credential_parameter: str | None = None,
        expected_content_types: Collection[str] | None = None,
        url_validator=None,
        purpose: str | None = None,
    ) -> httpx.Response:
        remaining = self._request_deadline - time.monotonic()
        if remaining <= 0:
            self._remaining_budget()
        request_url = self._request_url(
            url,
            params=params,
            authenticated=authenticated,
            credential_parameter=credential_parameter,
        )
        selected_purpose = purpose or self._default_purpose()
        allowed_types = self._approved_mime_types(expected_content_types)
        return self._with_retries(
            lambda: safe_get(
                request_url,
                max_bytes=MAX_RESPONSE_BYTES,
                timeout=self.timeout,
                allowed_hosts=self.allowed_hosts,
                headers={"User-Agent": self._access_policy["userAgent"]},
                max_redirects=self._access_policy["maxRedirects"],
                max_elapsed_seconds=min(20.0, self._remaining_budget()),
                https_only=True,
                before_request=self._consume_request_budget,
                url_validator=self._hop_validator(
                    purpose=selected_purpose,
                    method="GET",
                    extra_validator=url_validator,
                ),
                allowed_content_types=allowed_types,
            ),
            request_url=request_url,
        )

    def _consume_request_budget(self) -> None:
        if self._request_count >= self.max_requests:
            raise SourceAccessError(
                code="source_request_budget_exhausted",
                category="policy",
                detail="Source request budget was exhausted.",
                remediation="Reduce the source operation scope or approve a larger request budget.",
            )
        if time.monotonic() >= self._request_deadline:
            raise SourceAccessError(
                code="source_elapsed_budget_exhausted",
                category="policy",
                detail="Source elapsed-time budget was exhausted.",
                remediation="Reduce the source operation scope or approve a larger time budget.",
            )
        self._request_count = self._limiter.before_request(
            max_requests=self.max_requests
        )

    def _post_form(
        self,
        url: str,
        *,
        data: Mapping[str, Any],
        expected_content_types: Collection[str] | None = None,
        url_validator=None,
        purpose: str | None = None,
    ) -> httpx.Response:
        remaining = self._request_deadline - time.monotonic()
        if remaining <= 0:
            self._remaining_budget()
        request_url = self._request_url(
            url,
            params=None,
            authenticated=False,
            credential_parameter=None,
        )
        selected_purpose = purpose or self._default_purpose()
        return self._with_retries(
            lambda: safe_post_form(
                request_url,
                data=data,
                max_bytes=MAX_RESPONSE_BYTES,
                timeout=self.timeout,
                allowed_hosts=self.allowed_hosts,
                headers={"User-Agent": self._access_policy["userAgent"]},
                max_redirects=0,
                max_elapsed_seconds=min(20.0, self._remaining_budget()),
                before_request=self._consume_request_budget,
                https_only=True,
                url_validator=self._hop_validator(
                    purpose=selected_purpose,
                    method="POST",
                    extra_validator=url_validator,
                ),
                allowed_content_types=self._approved_mime_types(expected_content_types),
            ),
            request_url=request_url,
        )

    def _default_purpose(self) -> str:
        return "source_check" if self._runtime_mode == "source_check" else "collection"

    def _remaining_budget(self) -> float:
        remaining = self._request_deadline - time.monotonic()
        if remaining <= 0:
            raise SourceAccessError(
                code="source_elapsed_budget_exhausted",
                category="policy",
                detail="Source elapsed-time budget was exhausted.",
                remediation="Reduce the source operation scope or approve a larger budget.",
            )
        return remaining

    def _approved_mime_types(
        self,
        expected_content_types: Collection[str] | None,
    ) -> Collection[str]:
        raw = expected_content_types
        if raw is None:
            raw = self.config.get("allowedContentTypes")
        if not isinstance(raw, Collection) or isinstance(raw, (str, bytes)):
            raise self._policy_error("Source request MIME contract is missing.")
        normalized = tuple(
            str(value).split(";", 1)[0].strip().lower()
            for value in raw
            if isinstance(value, str) and value.strip()
        )
        if not normalized:
            raise self._policy_error("Source request MIME contract is empty.")
        return normalized

    def _hop_validator(self, *, purpose: str, method: str, extra_validator):
        def validate(url: str) -> None:
            self._enforce_access_policy(url, purpose=purpose, method=method)
            if extra_validator is not None:
                extra_validator(url)

        return validate

    def _enforce_access_policy(
        self,
        url: str,
        *,
        purpose: str,
        method: str,
    ) -> None:
        try:
            parsed = urlsplit(url)
            hostname = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        except (TypeError, ValueError, UnicodeError):
            raise self._policy_error("Source request URL is invalid.") from None
        if parsed.scheme.lower() != "https" or not hostname or parsed.username or parsed.password:
            raise self._policy_error("Source request must use approved HTTPS without userinfo.")
        matches = []
        for origin in self._access_policy["originPolicies"]:
            origin_host = str(origin["host"]).rstrip(".").encode("idna").decode("ascii").lower()
            prefixes = tuple(str(prefix) for prefix in origin["pathPrefixes"] if isinstance(prefix, str))
            if (
                hostname == origin_host
                and purpose in origin["purposes"]
                and method in origin["methods"]
                and any((parsed.path or "/").startswith(prefix) for prefix in prefixes)
            ):
                matches.append(origin)
        if len(matches) != 1:
            raise self._policy_error("Source request is outside the approved origin policy.")
        origin = matches[0]
        if purpose != "robots" and origin["robotsMode"] == "runtime_fetch":
            self._ensure_robots(origin, hostname, url)

    def _ensure_robots(
        self,
        origin: Mapping[str, Any],
        hostname: str,
        requested_url: str,
    ) -> None:
        if hostname in self._robots_checked:
            parser = self._robots_checked[hostname]
        else:
            robots_url = origin.get("robotsUrl") or f"https://{hostname}/robots.txt"
            try:
                robots_parsed = urlsplit(str(robots_url))
            except (TypeError, ValueError):
                raise self._policy_error("Source robots URL is invalid.") from None
            if robots_parsed.scheme.lower() != "https" or (robots_parsed.hostname or "").rstrip(".").lower() != hostname:
                raise self._policy_error("Source robots URL is outside its approved origin.")
            response = self._with_retries(
                lambda: safe_get(
                    str(robots_url),
                    max_bytes=128 * 1024,
                    timeout=self.timeout,
                    allowed_hosts={hostname},
                    headers={"User-Agent": self._access_policy["userAgent"]},
                    max_redirects=self._access_policy["maxRedirects"],
                    max_elapsed_seconds=min(20.0, self._remaining_budget()),
                    https_only=True,
                    before_request=self._consume_request_budget,
                    url_validator=lambda value: self._validate_robots_hop(value, hostname),
                    allowed_content_types=("text/plain", "text/html"),
                ),
                request_url=str(robots_url),
            )
            parser = robotparser.RobotFileParser()
            parser.set_url(str(robots_url))
            parser.parse(response.text.splitlines())
            self._robots_checked[hostname] = parser
        if not parser.can_fetch(self._access_policy["userAgent"], requested_url):
            raise self._policy_error("Source robots policy disallows this user agent.")

    def _validate_robots_hop(self, url: str, hostname: str) -> None:
        try:
            parsed = urlsplit(url)
        except (TypeError, ValueError):
            raise self._policy_error("Source robots redirect URL is invalid.") from None
        if parsed.scheme.lower() != "https" or (parsed.hostname or "").rstrip(".").lower() != hostname:
            raise self._policy_error("Source robots redirect is outside its approved origin.")

    def _with_retries(self, request, *, request_url: str) -> httpx.Response:
        attempts = self._access_policy["maxHttpAttempts"]
        for attempt in range(1, attempts + 1):
            try:
                response = request()
            except SourceAccessError as exc:
                failure = exc
            except OutboundResponseMimeRejected:
                raise SourceAccessError(
                    code="source_response_mime_rejected",
                    category="policy",
                    detail="Source response MIME type is outside the approved request contract.",
                    remediation="Approve the expected response MIME type before retrying.",
                ) from None
            except (UnsafeOutboundUrl, OutboundResponseTooLarge):
                raise SourceAccessError(
                    code="source_outbound_security_rejected",
                    category="security",
                    detail="Source outbound request failed a transport safety check.",
                    remediation="Review the frozen origin policy and source URL.",
                ) from None
            except (OutboundRequestFailed, HttpSafetyError):
                failure = SourceAccessError(
                    code="source_transport_unavailable",
                    category="transient",
                    detail="Source transport request failed.",
                    remediation="Retry the source operation after the remote service recovers.",
                    retryable=True,
                )
            else:
                if response.status_code < 400:
                    return response
                failure = self._http_failure(response)
            if not failure.retryable:
                raise failure
            if attempt == attempts:
                raise failure
            retry_after = failure.retry_after_seconds
            short_limit = min(5, self._access_policy["maxRetryDelaySeconds"])
            if retry_after is not None and retry_after > short_limit:
                raise failure
            delay = retry_after if retry_after is not None else min(
                short_limit,
                (2 ** (attempt - 1)) + random.uniform(0, 0.5),
            )
            if delay > 0:
                time.sleep(delay)
        raise SourceAccessError(
            code="source_transport_unavailable",
            category="transient",
            detail=f"Source transport request failed for {redact_url(request_url)}.",
            remediation="Retry the source operation.",
            retryable=True,
        )

    def _http_failure(self, response: httpx.Response) -> SourceAccessError:
        status = response.status_code
        if status in {401, 403}:
            return SourceAccessError(
                code="source_authentication_failed",
                category="authentication",
                detail="Source authentication was rejected.",
                remediation="Review the approved source credential and access grant.",
                http_status=status,
            )
        if status in {429, 502, 503, 504}:
            return SourceAccessError(
                code="source_http_transient_failure",
                category="transient",
                detail=f"Source returned transient HTTP status {status}.",
                remediation="Retry after the source service recovers.",
                retryable=True,
                retry_after_seconds=self._retry_after_seconds(response),
                http_status=status,
            )
        return SourceAccessError(
            code="source_http_permanent_failure",
            category="policy",
            detail=f"Source returned HTTP status {status}.",
            remediation="Review the source endpoint and approved access policy.",
            http_status=status,
        )

    def _retry_after_seconds(self, response: httpx.Response) -> int | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        maximum = self._access_policy["maxRetryDelaySeconds"]
        try:
            return min(maximum, max(0, int(value.strip())))
        except ValueError:
            try:
                retry_at = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if retry_at is None:
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return min(
                maximum,
                max(
                    0,
                    int(
                        (
                            retry_at.astimezone(UTC)
                            - datetime.now(UTC)
                        ).total_seconds()
                    ),
                ),
            )

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


def download_source_attachment(
    source_snapshot,
    url: str,
    operation_id: str | None = None,
    expected_content_types: Collection[str] | None = None,
) -> httpx.Response:
    """Download an approved attachment through the source HTTP boundary."""

    material = getattr(source_snapshot, "frozen_config", None)
    if not isinstance(material, Mapping):
        raise SourceAccessError(
            code="source_snapshot_invalid",
            category="schema",
            detail="Frozen source snapshot is unavailable for attachment download.",
            remediation="Use a valid frozen source snapshot.",
        )
    external_config = material.get("externalConfig")
    if not isinstance(external_config, Mapping):
        raise SourceAccessError(
            code="source_snapshot_invalid",
            category="schema",
            detail="Frozen source attachment access configuration is unavailable.",
            remediation="Approve a source snapshot with access configuration.",
        )
    config = dict(external_config)
    config.update(
        {
            "entrypoints": [url],
            "allowedContentTypes": list(material.get("allowedMimeTypes") or []),
            "pollIntervalSeconds": material.get("pollIntervalSeconds"),
            "rateLimitPolicy": dict(material.get("rateLimitPolicy") or {}),
            "accessPolicy": dict(
                material.get("accessPolicy") or {}
            ),
            "accessPolicyHash": material.get("accessPolicyHash"),
            "_runtimeMode": "attachment",
            "_operationId": operation_id,
        }
    )
    adapter = HttpSourceAdapter(
        source=SimpleNamespace(
            id=getattr(source_snapshot, "source_id", None),
            base_url=material.get("baseUrl", ""),
        ),
        config=config,
    )
    try:
        response = adapter._get(
            url,
            purpose="attachment",
            expected_content_types=expected_content_types
            if expected_content_types is not None
            else config["allowedContentTypes"],
        )
    except BaseException:
        try:
            adapter.close()
        except SourceAccessError:
            pass
        raise
    else:
        adapter.close()
        return response


class PublicHtmlAdapter(HttpSourceAdapter):
    def collect(self, *, since: datetime, until: datetime) -> list[CollectedSourceRecord]:
        now = datetime.now(UTC)
        records: list[CollectedSourceRecord] = []
        for entrypoint in self.entrypoints:
            response = self._get(
                entrypoint,
                purpose=self._default_purpose(),
                expected_content_types=self.config.get("allowedContentTypes"),
            )
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
            root = ElementTree.fromstring(
                self._get(
                    entrypoint,
                    purpose=self._default_purpose(),
                    expected_content_types=self.config.get("allowedContentTypes"),
                ).content
            )
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
                    purpose=self._default_purpose(),
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
