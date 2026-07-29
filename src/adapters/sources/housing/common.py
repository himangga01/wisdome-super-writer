from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)
from zoneinfo import ZoneInfo

from adapters.sources.base import CollectedSourceRecord, SourceAttachment
from adapters.sources.http import (
    SourceSchemaError,
    canonical_public_url,
    parse_html_page,
    parse_source_datetime,
)
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)


_TRUE_VALUES = frozenset({"1", "true", "t", "y", "yes", "예", "유", "있음"})
_STATUS_PRECEDENCE = {
    "active": 0,
    "corrected": 1,
    "unavailable": 2,
    "retracted": 3,
}
_STATUS_VALUES = {
    "corrected": frozenset(
        {
            "corrected",
            "amended",
            "revised",
            "정정",
            "정정공고",
            "변경",
            "변경공고",
            "수정",
            "수정공고",
        }
    ),
    "retracted": frozenset(
        {
            "cancelled",
            "canceled",
            "withdrawn",
            "retracted",
            "취소",
            "공고취소",
            "모집취소",
            "철회",
            "삭제",
        }
    ),
    "unavailable": frozenset(
        {
            "unavailable",
            "notfound",
            "not_found",
            "접근불가",
            "자료없음",
            "공고없음",
        }
    ),
    "active": frozenset(
        {
            "active",
            "published",
            "normal",
            "정상",
            "게시",
            "게시중",
            "공고",
            "공고중",
        }
    ),
}
_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_HREF_PATTERN = re.compile(
    r"""href\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_CHECKSUM_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_SEOUL = ZoneInfo("Asia/Seoul")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def canonical_checksum(value: Any) -> str:
    return canonical_hash(
        value,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def response_json(response, *, endpoint_name: str) -> Any:
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise SourceSchemaError(
            f"{endpoint_name} returned a non-JSON response."
        ) from exc


def response_metadata(
    response,
    *,
    page: int | None = None,
    include_validators: bool = True,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "statusCode": int(response.status_code),
        "contentType": response.headers.get("Content-Type"),
    }
    if include_validators:
        metadata["etag"] = response.headers.get("ETag")
        metadata["lastModified"] = response.headers.get("Last-Modified")
    if page is not None:
        metadata["page"] = page
    return metadata


def positive_config_int(
    config: Mapping[str, Any],
    key: str,
    *,
    default: int,
    maximum: int,
) -> int:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise SourceSchemaError(f"{key} must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SourceSchemaError(f"{key} must be a positive integer.") from exc
    if parsed <= 0 or parsed > maximum:
        raise SourceSchemaError(
            f"{key} must be between 1 and {maximum}."
        )
    return parsed


def require_config_url(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SourceSchemaError(f"{key} is required.")
    return value.strip()


def require_allowed_url_host(
    url: str,
    *,
    allowed_hosts: Collection[str],
) -> str:
    hostname = (urlsplit(url).hostname or "").rstrip(".").lower()
    normalized_allowed = {
        str(value).rstrip(".").lower() for value in allowed_hosts
    }
    if hostname not in normalized_allowed:
        raise SourceSchemaError(
            "A housing source URL uses an unapproved host."
        )
    return url


def require_text(
    record: Mapping[str, Any],
    keys: Sequence[str],
    *,
    field_name: str,
) -> str:
    value = first_value(record, keys)
    if value is None:
        raise SourceSchemaError(f"{field_name} is missing.")
    text = str(value).strip()
    if not text:
        raise SourceSchemaError(f"{field_name} is empty.")
    return text


def first_value(
    record: Mapping[str, Any],
    keys: Sequence[str],
) -> Any | None:
    indexed = {_normalized_key(key): value for key, value in record.items()}
    for key in keys:
        value = indexed.get(_normalized_key(key))
        if value is not None and str(value).strip():
            return value
    return None


def parse_housing_datetime(value: Any) -> datetime | None:
    if isinstance(value, str):
        stripped = value.strip()
        digits = re.sub(r"\D", "", stripped)
        formats = {
            8: "%Y%m%d",
            12: "%Y%m%d%H%M",
            14: "%Y%m%d%H%M%S",
        }
        date_format = formats.get(len(digits))
        if date_format and (
            stripped == digits
            or re.fullmatch(r"[\d\s./:-]+", stripped) is not None
        ):
            try:
                return datetime.strptime(digits, date_format).replace(
                    tzinfo=_SEOUL
                ).astimezone(UTC)
            except ValueError:
                pass
    parsed = parse_source_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def record_datetime(
    records: Iterable[Mapping[str, Any]],
    keys: Sequence[str],
) -> datetime | None:
    for record in records:
        value = first_value(record, keys)
        parsed = parse_housing_datetime(value)
        if parsed is not None:
            return parsed
    return None


def record_is_in_window(
    *,
    published_at: datetime | None,
    modified_at: datetime | None,
    since: datetime,
    until: datetime,
) -> bool:
    observed_at = modified_at or published_at
    if observed_at is None:
        raise SourceSchemaError(
            "A housing notice has no parseable publication or modification date."
        )
    normalized_since = _as_utc(since)
    normalized_until = _as_utc(until)
    local_observed_at = observed_at.astimezone(_SEOUL)
    if (
        local_observed_at.hour == 0
        and local_observed_at.minute == 0
        and local_observed_at.second == 0
        and local_observed_at.microsecond == 0
    ):
        next_day = (local_observed_at + timedelta(days=1)).astimezone(
            UTC
        )
        return (
            local_observed_at.astimezone(UTC) <= normalized_until
            and next_day > normalized_since
        )
    return normalized_since <= observed_at <= normalized_until


def explicit_status(
    records: Iterable[Mapping[str, Any]],
    *,
    status_fields: Collection[str],
    correction_flag_fields: Collection[str] = (),
    cancellation_flag_fields: Collection[str] = (),
) -> str:
    normalized_status_fields = {
        _normalized_key(value) for value in status_fields
    }
    normalized_correction_fields = {
        _normalized_key(value) for value in correction_flag_fields
    }
    normalized_cancellation_fields = {
        _normalized_key(value) for value in cancellation_flag_fields
    }
    result = "active"
    for record in records:
        for key, value in record.items():
            normalized_key = _normalized_key(key)
            normalized_value = _normalized_status_value(value)
            candidate: str | None = None
            if normalized_key in normalized_status_fields:
                candidate = _map_status_value(normalized_value)
            elif (
                normalized_key in normalized_correction_fields
                and normalized_value in _TRUE_VALUES
            ):
                candidate = "corrected"
            elif (
                normalized_key in normalized_cancellation_fields
                and normalized_value in _TRUE_VALUES
            ):
                candidate = "retracted"
            if (
                candidate is not None
                and _STATUS_PRECEDENCE[candidate]
                > _STATUS_PRECEDENCE[result]
            ):
                result = candidate
    return result


def canonical_notice_url(
    records: Iterable[Mapping[str, Any]],
    *,
    url_fields: Sequence[str],
    base_url: str,
    fallback_url: str,
    fallback_params: Mapping[str, Any],
    allowed_query_keys: Collection[str],
) -> str:
    for record in records:
        candidate = first_value(record, url_fields)
        if candidate is None:
            continue
        absolute = urljoin(base_url, str(candidate).strip())
        return canonical_public_url(
            absolute,
            allowed_query_keys=allowed_query_keys,
        )
    return canonical_public_url(
        _url_with_query(fallback_url, fallback_params),
        allowed_query_keys=allowed_query_keys,
    )


def extract_attachments(
    payloads: Iterable[Any],
    *,
    base_url: str,
    allowed_query_keys: Collection[str],
    excluded_urls: Collection[str] = (),
    rights_status: str | None = None,
    allowed_hosts: Collection[str] = (),
) -> tuple[SourceAttachment, ...]:
    excluded = set(excluded_urls)
    candidates: list[SourceAttachment] = []
    for payload in payloads:
        for record in _walk_mappings(payload):
            external_id = _attachment_external_id(record)
            title = _attachment_title(record)
            mime_type = _attachment_mime_type(record)
            size_bytes = _optional_positive_int(
                first_value(
                    record,
                    (
                        "FILE_SIZE",
                        "FILE_SZ",
                        "ATCH_FILE_SIZE",
                        "SIZE_BYTES",
                        "fileSize",
                    ),
                )
            )
            checksum = _attachment_checksum(record)
            for raw_url in _attachment_urls(record):
                absolute = urljoin(base_url, raw_url)
                try:
                    safe_url = canonical_public_url(
                        absolute,
                        allowed_query_keys=allowed_query_keys,
                    )
                    if allowed_hosts:
                        require_allowed_url_host(
                            safe_url,
                            allowed_hosts=allowed_hosts,
                        )
                except ValueError as exc:
                    raise SourceSchemaError(
                        "A housing attachment URL is invalid."
                    ) from exc
                if safe_url in excluded:
                    continue
                resolved_external_id = external_id or (
                    "url:"
                    + hashlib.sha256(safe_url.encode("utf-8")).hexdigest()
                )
                resolved_title = title or _url_filename(safe_url)
                candidates.append(
                    SourceAttachment(
                        url=safe_url,
                        title=resolved_title or "attachment",
                        mime_type=mime_type or _mime_from_url(safe_url),
                        external_id=resolved_external_id,
                        size_bytes=size_bytes,
                        checksum=checksum,
                        rights_status=rights_status,
                        metadata={
                            "officialFileId": external_id,
                        },
                    )
                )
    unique: dict[tuple[str, str], SourceAttachment] = {}
    for attachment in candidates:
        key = (str(attachment.external_id), attachment.url)
        unique.setdefault(key, attachment)
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                str(item.external_id),
                item.url,
                item.title,
            ),
        )
    )


def extract_html_detail(
    response,
    *,
    base_url: str,
    allowed_query_keys: Collection[str],
    allowed_hosts: Collection[str],
    rights_status: str | None,
) -> tuple[dict[str, Any], tuple[SourceAttachment, ...]]:
    title, text, links = parse_html_page(
        response.text,
        ignore_noncontent=True,
    )
    attachments: list[SourceAttachment] = []
    for href, label, link_metadata in links:
        normalized_href = href.strip()
        if not normalized_href:
            if link_metadata.get("unresolvedAttachment"):
                raise SourceSchemaError(
                    "Official detail page exposes a scripted attachment "
                    "without a resolvable public URL."
                )
            continue
        path_and_query = normalized_href.lower()
        if (
            _mime_from_url(normalized_href) is None
            and not any(
                marker in path_and_query
                for marker in (
                    "download",
                    "fileid",
                    "file_id",
                    "atchfile",
                    "atch_file",
                )
            )
        ):
            continue
        safe_url = require_allowed_url_host(
            canonical_public_url(
                urljoin(base_url, normalized_href),
                allowed_query_keys=allowed_query_keys,
            ),
            allowed_hosts=allowed_hosts,
        )
        official_file_id = (
            str(link_metadata.get("officialFileId") or "").strip()
            or _official_file_id_from_url(safe_url)
            or None
        )
        attachments.append(
            SourceAttachment(
                url=safe_url,
                title=label.strip() or _url_filename(safe_url) or "attachment",
                mime_type=_mime_from_url(safe_url),
                external_id=(
                    official_file_id
                    or (
                        "url:"
                        + hashlib.sha256(
                            safe_url.encode("utf-8")
                        ).hexdigest()
                    )
                ),
                rights_status=rights_status,
                metadata={
                    "discoveredFrom": "officialDetailPage",
                    "officialFileId": official_file_id,
                },
            )
        )
    unique = {
        (str(item.external_id), item.url): item
        for item in attachments
    }
    ordered_attachments = tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                str(item.external_id),
                item.url,
            ),
        )
    )
    detail_material = {
        "title": title,
        "text": text,
        "links": [
            {
                "url": item.url,
                "title": item.title,
                "externalId": item.external_id,
            }
            for item in ordered_attachments
        ],
    }
    detail = {
        **detail_material,
        "checksum": canonical_checksum(detail_material),
        "http": response_metadata(response),
    }
    return (
        detail,
        ordered_attachments,
    )


def _official_file_id_from_url(url: str) -> str | None:
    values: list[str] = []
    for key, value in parse_qsl(
        urlsplit(url).query,
        keep_blank_values=False,
    ):
        normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
        if normalized_key in {
            "fileid",
            "atchfileid",
            "filesn",
            "seq",
        }:
            clean = value.strip()
            if clean:
                values.append(clean)
    return ":".join(values) if values else None


def merge_attachments(
    *groups: Iterable[SourceAttachment],
) -> tuple[SourceAttachment, ...]:
    unique: dict[tuple[str, str], SourceAttachment] = {}
    for group in groups:
        for attachment in group:
            key = (str(attachment.external_id), attachment.url)
            unique.setdefault(key, attachment)
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                str(item.external_id),
                item.url,
                item.title,
            ),
        )
    )


def sorted_mapping_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized = [dict(row) for row in rows]
    return sorted(normalized, key=canonical_json)


def finalize_records(
    records: Iterable[CollectedSourceRecord],
) -> list[CollectedSourceRecord]:
    result = list(records)
    identities: set[str] = set()
    for record in result:
        if record.external_id in identities:
            raise SourceSchemaError(
                f"Duplicate housing notice identity: {record.external_id}"
            )
        identities.add(record.external_id)
    return sorted(
        result,
        key=lambda item: (
            item.external_id,
            item.canonical_url,
            item.title,
        ),
    )


def page_signature(rows: Iterable[Mapping[str, Any]]) -> str:
    return canonical_checksum([dict(row) for row in rows])


def page_has_more(
    *,
    page: int,
    page_size: int,
    row_count: int,
    total_count: int | None,
) -> bool:
    if total_count is not None:
        return page * page_size < total_count
    return row_count >= page_size


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    return str(value)


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def _normalized_status_value(value: Any) -> str:
    return re.sub(r"[\s_.:/-]+", "", str(value).strip().lower())


def _map_status_value(value: str) -> str | None:
    for status, values in _STATUS_VALUES.items():
        normalized_candidates = {
            _normalized_status_value(candidate) for candidate in values
        }
        if value in normalized_candidates:
            return status
        if status != "active" and any(
            candidate and candidate in value
            for candidate in normalized_candidates
        ):
            return status
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _url_with_query(
    url: str,
    params: Mapping[str, Any],
) -> str:
    parsed = urlsplit(url)
    query = urlencode(
        [
            (str(key), str(value))
            for key, value in sorted(
                params.items(),
                key=lambda item: str(item[0]),
            )
            if value is not None
        ]
    )
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            query,
            "",
        )
    )


def _walk_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _attachment_urls(record: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    for key, value in record.items():
        if not isinstance(value, str):
            continue
        normalized_key = _normalized_key(key)
        key_looks_like_attachment = (
            any(
                token in normalized_key
                for token in ("FILE", "ATCH", "ATTACH", "DOWNLOAD", "DOC")
            )
            and any(
                token in normalized_key
                for token in ("URL", "LINK", "PATH", "ADDR", "ADR")
            )
        )
        value_looks_like_file = _mime_from_url(value) is not None
        normalized_value = value.lower()
        value_looks_like_download = any(
            marker in normalized_value
            for marker in (
                "fileid",
                "file_id",
                "atchfile",
                "atch_file",
                "download",
            )
        )
        if (
            key_looks_like_attachment
            or value_looks_like_file
            or value_looks_like_download
        ):
            urls.extend(_extract_urls(value))
    return list(dict.fromkeys(urls))


def _extract_urls(value: str) -> list[str]:
    stripped = value.strip()
    found = [
        match.group(1).strip()
        for match in _HREF_PATTERN.finditer(stripped)
    ]
    found.extend(
        match.group(0).rstrip(".,);")
        for match in _URL_PATTERN.finditer(stripped)
    )
    if not found and (
        stripped.startswith("/")
        or stripped.startswith("./")
        or stripped.startswith("../")
    ):
        found.append(stripped)
    return list(dict.fromkeys(found))


def _attachment_external_id(record: Mapping[str, Any]) -> str | None:
    file_id = first_value(
        record,
        (
            "FILE_ID",
            "FILEID",
            "ATCH_FILE_ID",
            "ATCHFILEID",
            "ATCH_FILE_IDNTFC_NO",
            "fileId",
            "atchFileId",
        ),
    )
    file_sequence = first_value(
        record,
        (
            "FILE_SN",
            "fileSn",
            "ATCH_FILE_SN",
        ),
    )
    if file_id is None and file_sequence is None:
        return None
    material = [
        str(value).strip()
        for value in (file_id, file_sequence)
        if value is not None and str(value).strip()
    ]
    return ":".join(material) or None


def _attachment_title(record: Mapping[str, Any]) -> str | None:
    value = first_value(
        record,
        (
            "FILE_NM",
            "FILE_NAME",
            "ORIGNL_FILE_NM",
            "ATCH_FILE_NM",
            "fileName",
            "title",
        ),
    )
    return str(value).strip() if value is not None else None


def _attachment_mime_type(record: Mapping[str, Any]) -> str | None:
    value = first_value(
        record,
        (
            "MIME_TYPE",
            "CONTENT_TYPE",
            "FILE_TYPE",
            "fileType",
        ),
    )
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if "/" in text else None


def _attachment_checksum(record: Mapping[str, Any]) -> str | None:
    value = first_value(
        record,
        (
            "SHA256",
            "SHA_256",
            "CHECKSUM",
            "FILE_HASH",
        ),
    )
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if _CHECKSUM_PATTERN.fullmatch(text) else None


def _optional_positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _url_filename(url: str) -> str:
    return PurePosixPath(urlsplit(url).path).name


def _mime_from_url(url: str) -> str | None:
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".hwp": "application/haansofthwp",
        ".hwpx": "application/hwp+zip",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        ".csv": "text/csv",
        ".zip": "application/zip",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(suffix)
