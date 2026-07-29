from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from adapters.sources.base import CollectedSourceRecord
from adapters.sources.http import (
    HttpSourceAdapter,
    PublicHtmlAdapter,
    SourceSchemaError,
    canonical_public_url,
    extract_json_rows,
    sanitize_source_payload,
)

from .common import (
    canonical_checksum,
    canonical_json,
    explicit_status,
    extract_attachments,
    extract_html_detail,
    finalize_records,
    first_value,
    merge_attachments,
    page_has_more,
    page_signature,
    positive_config_int,
    record_datetime,
    record_is_in_window,
    require_allowed_url_host,
    require_text,
    response_json,
    response_metadata,
)


_CANONICAL_QUERY_KEYS = frozenset(
    {
        "houseManageNo",
        "pblancNo",
        "houseSecd",
        "suplyTy",
        "menuId",
        "HOUSE_MANAGE_NO",
        "PBLANC_NO",
    }
)
_ATTACHMENT_QUERY_KEYS = _CANONICAL_QUERY_KEYS | frozenset(
    {
        "fileId",
        "fileid",
        "FILE_ID",
        "atchFileId",
        "ATCH_FILE_ID",
        "fileSn",
        "FILE_SN",
        "seq",
    }
)
_PUBLISHED_FIELDS = (
    "RCRIT_PBLANC_DE",
    "PBLANC_DE",
    "PBLANC_DT",
    "ANNOUNCEMENT_DATE",
    "REG_DT",
)
_MODIFIED_FIELDS = (
    "PBLANC_CHG_DT",
    "PBLANC_UPD_DT",
    "UPD_DT",
    "UPDT_DT",
    "MDFCN_DT",
    "LAST_MODIFIED",
)
_STATUS_FIELDS = frozenset(
    {
        "PBLANC_ST",
        "PBLANC_STATUS",
        "PBLANC_STTUS",
        "RCRIT_PBLANC_STTUS",
        "NOTICE_STATUS",
        "STATUS",
    }
)
_CORRECTION_FLAGS = frozenset(
    {
        "PBLANC_CHG_YN",
        "PBLANC_UPD_YN",
        "CORR_YN",
        "AMEND_YN",
    }
)
_CANCELLATION_FLAGS = frozenset(
    {
        "PBLANC_CANCL_YN",
        "PBLANC_CANCEL_YN",
        "CANCEL_YN",
        "RTRCT_YN",
    }
)
_ENDPOINT_CATEGORIES = (
    ("urbtyoftcllttotpblanc", "urban_officetel"),
    ("pblpvtrentlttotpblanc", "public_private_rental"),
    ("remndrlttotpblanc", "remaining"),
    ("optlttotpblanc", "optional_supply"),
    ("aptlttotpblanc", "apt"),
)
_SEOUL = ZoneInfo("Asia/Seoul")
_OFFICIAL_HOSTS = frozenset(
    {
        "api.odcloud.kr",
        "applyhome.co.kr",
        "www.applyhome.co.kr",
    }
)


class ApplyHomeAdapter(HttpSourceAdapter):
    """Collect immutable notice-level observations from ApplyHome ODCloud APIs."""

    def collect(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> list[CollectedSourceRecord]:
        if self.source.access_method == "public_html":
            return PublicHtmlAdapter.collect(
                self,
                since=since,
                until=until,
            )
        page_size = positive_config_int(
            self.config,
            "pageSize",
            default=100,
            maximum=1000,
        )
        max_pages = positive_config_int(
            self.config,
            "maxPages",
            default=100,
            maximum=1000,
        )
        collected_at = datetime.now(UTC)
        records: list[CollectedSourceRecord] = []
        self._reconciliation_external_ids = frozenset(
            str(value)
            for value in self.config.get(
                "_reconciliationExternalIds",
                [],
            )
        )
        query_since = since
        if self.config.get("_runtimeMode") != "source_check":
            reconciliation_days = positive_config_int(
                self.config,
                "reconciliationDays",
                default=366,
                maximum=3650,
            )
            query_since = since - timedelta(
                days=reconciliation_days
            )
        for entrypoint in self.config.get("entrypoints", []):
            records.extend(
                self._collect_entrypoint(
                    entrypoint=str(entrypoint),
                    since=since,
                    query_since=query_since,
                    until=until,
                    collected_at=collected_at,
                    page_size=page_size,
                    max_pages=max_pages,
                )
            )
        return finalize_records(records)

    def _collect_entrypoint(
        self,
        *,
        entrypoint: str,
        since: datetime,
        query_since: datetime,
        until: datetime,
        collected_at: datetime,
        page_size: int,
        max_pages: int,
    ) -> list[CollectedSourceRecord]:
        records: list[CollectedSourceRecord] = []
        signatures: set[str] = set()
        total_count_seen: int | None = None
        received = 0
        authenticated = bool(self.config.get("secretRef"))

        for page in range(1, max_pages + 1):
            response = self._get(
                entrypoint,
                params={
                    "page": page,
                    "perPage": page_size,
                    "cond[RCRIT_PBLANC_DE::GTE]": _date_parameter(
                        query_since
                    ),
                    "cond[RCRIT_PBLANC_DE::LTE]": _date_parameter(until),
                },
                authenticated=authenticated,
                expected_content_types=self.config.get(
                    "apiContentTypes",
                    ("application/json",),
                ),
            )
            payload = response_json(
                response,
                endpoint_name="ApplyHome list endpoint",
            )
            rows, total_count = extract_json_rows(payload)
            if total_count is not None:
                if (
                    total_count_seen is not None
                    and total_count != total_count_seen
                ):
                    raise SourceSchemaError(
                        "ApplyHome totalCount changed during pagination."
                    )
                total_count_seen = total_count
            if not rows:
                if (
                    total_count_seen is not None
                    and received < total_count_seen
                ):
                    raise SourceSchemaError(
                        "ApplyHome pagination ended before totalCount."
                    )
                break

            signature = page_signature(rows)
            if signature in signatures:
                raise SourceSchemaError(
                    "ApplyHome pagination repeated a response page."
                )
            signatures.add(signature)
            received += len(rows)

            for row in rows:
                records_for_row = self._record_from_row(
                    entrypoint=entrypoint,
                    row=row,
                    response=response,
                    since=since,
                    until=until,
                    collected_at=collected_at,
                )
                if records_for_row is not None:
                    records.append(records_for_row)

            has_more = page_has_more(
                page=page,
                page_size=page_size,
                row_count=len(rows),
                total_count=total_count_seen,
            )
            if not has_more:
                break
            if page == max_pages:
                raise SourceSchemaError(
                    "ApplyHome pagination exceeded maxPages."
                )
        if (
            total_count_seen is not None
            and received < total_count_seen
        ):
            raise SourceSchemaError(
                "ApplyHome pagination did not cover totalCount."
            )
        return records

    def _record_from_row(
        self,
        *,
        entrypoint: str,
        row: dict[str, Any],
        response,
        since: datetime,
        until: datetime,
        collected_at: datetime,
    ) -> CollectedSourceRecord | None:
        category = self._category(entrypoint, row)
        house_manage_no = require_text(
            row,
            ("HOUSE_MANAGE_NO", "houseManageNo"),
            field_name="ApplyHome HOUSE_MANAGE_NO",
        )
        pblanc_no = require_text(
            row,
            ("PBLANC_NO", "pblancNo"),
            field_name="ApplyHome PBLANC_NO",
        )
        external_id = (
            f"applyhome:{category}:{house_manage_no}:{pblanc_no}"
        )
        published_at = record_datetime((row,), _PUBLISHED_FIELDS)
        modified_at = record_datetime((row,), _MODIFIED_FIELDS)
        in_requested_window = record_is_in_window(
            published_at=published_at,
            modified_at=modified_at,
            since=since,
            until=until,
        )
        if (
            not in_requested_window
            and self.config.get("_runtimeMode") == "source_check"
        ):
            return None
        if (
            not in_requested_window
            and external_id
            not in self._reconciliation_external_ids
        ):
            return None

        pblanc_url = require_text(
            row,
            ("PBLANC_URL", "pblancUrl"),
            field_name="ApplyHome PBLANC_URL",
        )
        canonical_url = require_allowed_url_host(
            canonical_public_url(
                urljoin(self.source.base_url, pblanc_url),
                allowed_query_keys=_CANONICAL_QUERY_KEYS,
            ),
            allowed_hosts=_OFFICIAL_HOSTS,
        )
        status = explicit_status(
            (row,),
            status_fields=_STATUS_FIELDS,
            correction_flag_fields=_CORRECTION_FLAGS,
            cancellation_flag_fields=_CANCELLATION_FLAGS,
        )
        title_value = first_value(
            row,
            ("HOUSE_NM", "PBLANC_NM", "NOTICE_TITLE", "TITLE"),
        )
        title = (
            str(title_value).strip()
            if title_value is not None
            else f"ApplyHome notice {pblanc_no}"
        )
        structured = sanitize_source_payload(
            dict(row),
            allowed_hosts=_OFFICIAL_HOSTS,
            allowed_query_keys=_ATTACHMENT_QUERY_KEYS,
            base_url=canonical_url,
        )
        attachments = extract_attachments(
            (structured,),
            base_url=canonical_url,
            allowed_query_keys=_ATTACHMENT_QUERY_KEYS,
            excluded_urls=(canonical_url,),
            rights_status=self.config.get("rightsStatus"),
            allowed_hosts=_OFFICIAL_HOSTS,
        )
        detail_page: dict[str, Any] | None = None
        detail_response = None
        fetch_detail_page = (
            self.config.get("_runtimeMode") != "source_check"
            or (
                in_requested_window
                and not getattr(
                    self, "_source_check_detail_fetched", False
                )
            )
        )
        if fetch_detail_page:
            self._source_check_detail_fetched = True
            detail_response = self._get(
                canonical_url,
                expected_content_types=self.config.get(
                    "detailContentTypes",
                    ("text/html",),
                ),
            )
            detail_page, page_attachments = extract_html_detail(
                detail_response,
                base_url=canonical_url,
                allowed_query_keys=_ATTACHMENT_QUERY_KEYS,
                allowed_hosts=_OFFICIAL_HOSTS,
                rights_status=self.config.get("rightsStatus"),
            )
            detail_page = sanitize_source_payload(
                detail_page,
                allowed_hosts=_OFFICIAL_HOSTS,
                allowed_query_keys=_ATTACHMENT_QUERY_KEYS,
                base_url=canonical_url,
            )
            attachments = merge_attachments(
                attachments,
                page_attachments,
            )
        durable_structured = {
            "api": structured,
            "detailPage": detail_page,
        }
        return CollectedSourceRecord(
            external_id=external_id,
            canonical_url=canonical_url,
            title=title,
            publisher=self.source.publisher,
            published_at=published_at,
            modified_at=modified_at,
            collected_at=collected_at,
            body_text=canonical_json(durable_structured),
            status=status,
            reconciliation_only=not in_requested_window,
            metadata={
                "schemaVersion": "housing-applyhome-record-v1",
                "category": category,
                "houseManageNo": house_manage_no,
                "pblancNo": pblanc_no,
                "structured": durable_structured,
            },
            attachments=attachments,
            http_metadata={
                **response_metadata(
                    response,
                    include_validators=False,
                ),
                "endpoint": urlsplit(entrypoint).path,
                **(
                    {
                        "detailPage": response_metadata(
                            detail_response
                        )
                    }
                    if detail_response is not None
                    else {}
                ),
            },
            raw_checksum=canonical_checksum(durable_structured),
        )

    @staticmethod
    def _category(
        entrypoint: str,
        row: dict[str, Any],
    ) -> str:
        normalized_path = urlsplit(entrypoint).path.lower()
        for marker, category in _ENDPOINT_CATEGORIES:
            if marker in normalized_path:
                return category
        explicit = first_value(
            row,
            (
                "CATEGORY",
                "CATEGORY_CODE",
                "PBLANC_SECD",
            ),
        )
        if explicit is not None:
            normalized = str(explicit).strip().lower()
            if normalized:
                return normalized.replace(":", "_")
        raise SourceSchemaError(
            "ApplyHome notice category cannot be determined."
        )


def _date_parameter(value: datetime) -> str:
    normalized = (
        value.replace(tzinfo=_SEOUL)
        if value.tzinfo is None
        else value.astimezone(_SEOUL)
    )
    return normalized.strftime("%Y-%m-%d")
