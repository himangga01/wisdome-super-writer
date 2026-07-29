from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from adapters.sources.base import CollectedSourceRecord
from adapters.sources.http import (
    HttpSourceAdapter,
    PublicHtmlAdapter,
    SourceSchemaError,
    extract_json_rows,
    sanitize_source_payload,
)

from .common import (
    canonical_checksum,
    canonical_json,
    canonical_notice_url,
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
    require_config_url,
    require_text,
    response_json,
    response_metadata,
    sorted_mapping_rows,
)


_IDENTITY_FIELDS = (
    "CCR_CNNT_SYS_DS_CD",
    "PAN_ID",
    "UPP_AIS_TP_CD",
    "AIS_TP_CD",
)
_CANONICAL_QUERY_KEYS = frozenset(
    {
        *_IDENTITY_FIELDS,
        "ccrCnntSysDsCd",
        "panId",
        "uppAisTpCd",
        "aisTpCd",
        "mi",
        "gv_url",
        "gv_menuId",
        "gv_param",
    }
)
_ATTACHMENT_QUERY_KEYS = _CANONICAL_QUERY_KEYS | frozenset(
    {
        "fileid",
        "fileId",
        "FILE_ID",
        "atchFileId",
        "ATCH_FILE_ID",
        "fileSn",
        "FILE_SN",
        "seq",
    }
)
_PUBLISHED_FIELDS = (
    "PAN_NT_ST_DT",
    "PAN_NT_ST_DTTM",
    "PBLANC_DT",
    "NOTICE_DATE",
    "REG_DT",
)
_MODIFIED_FIELDS = (
    "PAN_UPD_DT",
    "UPD_DT",
    "UPDT_DT",
    "MDFCN_DT",
    "LAST_MODIFIED",
)
_STATUS_FIELDS = frozenset(
    {
        "PAN_SS",
        "PAN_ST",
        "PAN_ST_NM",
        "PAN_NT_ST",
        "PAN_NT_ST_NM",
        "PAN_STAT",
        "PAN_STATUS",
        "NOTICE_STATUS",
        "STATUS",
    }
)
_CORRECTION_FLAGS = frozenset(
    {
        "PAN_CHG_YN",
        "PAN_UPD_YN",
        "CORR_YN",
        "AMEND_YN",
    }
)
_CANCELLATION_FLAGS = frozenset(
    {
        "PAN_CANCL_YN",
        "PAN_CANCEL_YN",
        "CANCEL_YN",
        "RTRCT_YN",
    }
)
_DETAIL_URL_FIELDS = (
    "PAN_DTL_URL",
    "DETAIL_URL",
    "DTL_URL",
    "PAN_URL",
    "HMPG_ADR",
    "LINK_URL",
)
_SEOUL = ZoneInfo("Asia/Seoul")
_OFFICIAL_HOSTS = frozenset(
    {
        "apis.data.go.kr",
        "apply.lh.or.kr",
    }
)


class LhApplyAdapter(HttpSourceAdapter):
    """Collect LH list, detail, supply and attachment observations."""

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
        detail_entrypoint = require_config_url(
            self.config,
            "detailEntrypoint",
        )
        supply_entrypoint = require_config_url(
            self.config,
            "supplyEntrypoint",
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
                default=31,
                maximum=3650,
            )
            query_since = since - timedelta(
                days=reconciliation_days
            )
        for entrypoint in self.config.get("entrypoints", []):
            records.extend(
                self._collect_entrypoint(
                    entrypoint=str(entrypoint),
                    detail_entrypoint=detail_entrypoint,
                    supply_entrypoint=supply_entrypoint,
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
        detail_entrypoint: str,
        supply_entrypoint: str,
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
                    "PAGE": page,
                    "PG_SZ": page_size,
                    "PAN_NT_ST_DT": _date_parameter(query_since),
                    "CLSG_DT": _date_parameter(until),
                },
                authenticated=authenticated,
                expected_content_types=self.config.get(
                    "apiContentTypes",
                    ("application/json",),
                ),
            )
            payload = response_json(
                response,
                endpoint_name="LH list endpoint",
            )
            _require_lh_success(
                payload,
                endpoint_name="LH list endpoint",
            )
            rows, total_count = _extract_lh_rows(payload)
            if total_count is not None:
                if (
                    total_count_seen is not None
                    and total_count != total_count_seen
                ):
                    raise SourceSchemaError(
                        "LH ALL_CNT changed during pagination."
                    )
                total_count_seen = total_count
            if not rows:
                if (
                    total_count_seen is not None
                    and received < total_count_seen
                ):
                    raise SourceSchemaError(
                        "LH pagination ended before ALL_CNT."
                    )
                break

            signature = page_signature(rows)
            if signature in signatures:
                raise SourceSchemaError(
                    "LH pagination repeated a response page."
                )
            signatures.add(signature)
            received += len(rows)

            for row in rows:
                record = self._record_from_row(
                    list_entrypoint=entrypoint,
                    detail_entrypoint=detail_entrypoint,
                    supply_entrypoint=supply_entrypoint,
                    row=row,
                    list_response=response,
                    since=since,
                    until=until,
                    collected_at=collected_at,
                    authenticated=authenticated,
                )
                if record is not None:
                    records.append(record)

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
                    "LH pagination exceeded maxPages."
                )
        if (
            total_count_seen is not None
            and received < total_count_seen
        ):
            raise SourceSchemaError(
                "LH pagination did not cover ALL_CNT."
            )
        return records

    def _record_from_row(
        self,
        *,
        list_entrypoint: str,
        detail_entrypoint: str,
        supply_entrypoint: str,
        row: dict[str, Any],
        list_response,
        since: datetime,
        until: datetime,
        collected_at: datetime,
        authenticated: bool,
    ) -> CollectedSourceRecord | None:
        identity = {
            field: require_text(
                row,
                (field,),
                field_name=f"LH {field}",
            )
            for field in _IDENTITY_FIELDS
        }
        external_id = "lh:" + ":".join(
            identity[field] for field in _IDENTITY_FIELDS
        )
        request_params = {
            **identity,
            "SPL_INF_TP_CD": require_text(
                row,
                ("SPL_INF_TP_CD",),
                field_name="LH SPL_INF_TP_CD",
            ),
        }
        published_at = record_datetime((row,), _PUBLISHED_FIELDS)
        modified_at = record_datetime((row,), _MODIFIED_FIELDS)
        list_in_requested_window = record_is_in_window(
            published_at=published_at,
            modified_at=modified_at,
            since=since,
            until=until,
        )
        if (
            not list_in_requested_window
            and self.config.get("_runtimeMode") == "source_check"
        ):
            return None
        if (
            not list_in_requested_window
            and external_id
            not in self._reconciliation_external_ids
        ):
            return None

        detail_response = self._get(
            detail_entrypoint,
            params=request_params,
            authenticated=authenticated,
            credential_parameter="serviceKey",
            expected_content_types=self.config.get(
                "apiContentTypes",
                ("application/json",),
            ),
        )
        detail_payload = response_json(
            detail_response,
            endpoint_name="LH detail endpoint",
        )
        _require_lh_success(
            detail_payload,
            endpoint_name="LH detail endpoint",
        )
        detail_rows, _ = _extract_lh_rows(detail_payload)
        if not detail_rows:
            raise SourceSchemaError(
                f"LH detail response is empty for {external_id}."
            )

        supply_response = self._get(
            supply_entrypoint,
            params=request_params,
            authenticated=authenticated,
            expected_content_types=self.config.get(
                "apiContentTypes",
                ("application/json",),
            ),
        )
        supply_payload = response_json(
            supply_response,
            endpoint_name="LH supply endpoint",
        )
        _require_lh_success(
            supply_payload,
            endpoint_name="LH supply endpoint",
        )
        supply_rows, _ = _extract_lh_rows(supply_payload)

        sorted_detail = sorted_mapping_rows(detail_rows)
        sorted_supply = sorted_mapping_rows(supply_rows)
        merged_records = (row, *sorted_detail, *sorted_supply)
        published_at = (
            record_datetime(merged_records, _PUBLISHED_FIELDS)
            or published_at
        )
        modified_at = (
            record_datetime(merged_records, _MODIFIED_FIELDS)
            or modified_at
        )
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
        canonical_url = require_allowed_url_host(
            canonical_notice_url(
                merged_records,
                url_fields=_DETAIL_URL_FIELDS,
                base_url=self.source.base_url,
                fallback_url=detail_entrypoint,
                fallback_params=identity,
                allowed_query_keys=_CANONICAL_QUERY_KEYS,
            ),
            allowed_hosts=_OFFICIAL_HOSTS,
        )
        status = explicit_status(
            merged_records,
            status_fields=_STATUS_FIELDS,
            correction_flag_fields=_CORRECTION_FLAGS,
            cancellation_flag_fields=_CANCELLATION_FLAGS,
        )
        title_value = first_value(
            sorted_detail[0] if sorted_detail else row,
            ("PAN_NM", "PAN_NM_CN", "NOTICE_TITLE", "TITLE"),
        ) or first_value(
            row,
            ("PAN_NM", "PAN_NM_CN", "NOTICE_TITLE", "TITLE"),
        )
        title = (
            str(title_value).strip()
            if title_value is not None
            else f"LH notice {identity['PAN_ID']}"
        )
        safe_list = sanitize_source_payload(
            dict(row),
            allowed_hosts=_OFFICIAL_HOSTS,
            allowed_query_keys=_ATTACHMENT_QUERY_KEYS,
            base_url=canonical_url,
        )
        safe_detail = sanitize_source_payload(
            sorted_detail,
            allowed_hosts=_OFFICIAL_HOSTS,
            allowed_query_keys=_ATTACHMENT_QUERY_KEYS,
            base_url=canonical_url,
        )
        safe_supply = sanitize_source_payload(
            sorted_supply,
            allowed_hosts=_OFFICIAL_HOSTS,
            allowed_query_keys=_ATTACHMENT_QUERY_KEYS,
            base_url=canonical_url,
        )
        structured = {
            "list": safe_list,
            "detail": safe_detail,
            "supply": safe_supply,
        }
        attachments = extract_attachments(
            (safe_detail, safe_supply),
            base_url=canonical_url,
            allowed_query_keys=_ATTACHMENT_QUERY_KEYS,
            excluded_urls=(canonical_url,),
            rights_status=self.config.get("rightsStatus"),
            allowed_hosts=_OFFICIAL_HOSTS,
        )
        detail_page: dict[str, Any] | None = None
        page_response = None
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
            page_response = self._get(
                canonical_url,
                expected_content_types=self.config.get(
                    "detailContentTypes",
                    ("text/html",),
                ),
            )
            detail_page, page_attachments = extract_html_detail(
                page_response,
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
        structured["detailPage"] = detail_page
        return CollectedSourceRecord(
            external_id=external_id,
            canonical_url=canonical_url,
            title=title,
            publisher=self.source.publisher,
            published_at=published_at,
            modified_at=modified_at,
            collected_at=collected_at,
            body_text=canonical_json(structured),
            status=status,
            reconciliation_only=not in_requested_window,
            metadata={
                "schemaVersion": "housing-lh-record-v1",
                "identity": identity,
                "structured": structured,
            },
            attachments=attachments,
            http_metadata={
                "list": {
                    **response_metadata(
                        list_response,
                        include_validators=False,
                    ),
                    "endpoint": urlsplit(list_entrypoint).path,
                },
                "detail": {
                    **response_metadata(detail_response),
                    "endpoint": urlsplit(detail_entrypoint).path,
                },
                "supply": {
                    **response_metadata(supply_response),
                    "endpoint": urlsplit(supply_entrypoint).path,
                },
                **(
                    {"detailPage": response_metadata(page_response)}
                    if page_response is not None
                    else {}
                ),
            },
            raw_checksum=canonical_checksum(structured),
        )


def _date_parameter(value: datetime) -> str:
    normalized = (
        value.replace(tzinfo=_SEOUL)
        if value.tzinfo is None
        else value.astimezone(_SEOUL)
    )
    return normalized.strftime("%Y.%m.%d")


def _extract_lh_rows(
    payload: Any,
) -> tuple[list[dict[str, Any]], int | None]:
    datasets: list[dict[str, Any]] = []

    def visit_approved_container(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                if isinstance(child, Mapping):
                    visit_approved_container(child)
            return
        if not isinstance(value, Mapping):
            return
        for key, child in value.items():
            normalized_key = re.sub(
                r"[^A-Z0-9]",
                "",
                str(key).upper(),
            )
            if (
                normalized_key.startswith("DS")
                and normalized_key != "DSSCH"
            ):
                if isinstance(child, Mapping):
                    datasets.append(dict(child))
                elif isinstance(child, list) and all(
                    isinstance(item, Mapping)
                    for item in child
                ):
                    datasets.extend(dict(item) for item in child)
                elif child not in (None, ""):
                    raise SourceSchemaError(
                        "LH dataset is not a mapping array."
                    )
            elif normalized_key in {
                "BODY",
                "DATA",
                "RESPONSE",
                "RESULT",
            }:
                visit_approved_container(child)

    visit_approved_container(payload)
    if datasets:
        rows = datasets
        total_count = None
    else:
        rows, total_count = extract_json_rows(payload)
    if total_count is None:
        observed_totals: set[int] = set()
        for row in rows:
            value = first_value(row, ("ALL_CNT", "totalCount"))
            if value is None:
                continue
            try:
                parsed = int(str(value).replace(",", ""))
            except (TypeError, ValueError) as exc:
                raise SourceSchemaError(
                    "LH ALL_CNT is not an integer."
                ) from exc
            if parsed < 0:
                raise SourceSchemaError(
                    "LH ALL_CNT must not be negative."
                )
            observed_totals.add(parsed)
        if len(observed_totals) > 1:
            raise SourceSchemaError(
                "LH response contains conflicting ALL_CNT values."
            )
        if observed_totals:
            total_count = observed_totals.pop()
    return rows, total_count


def _require_lh_success(
    payload: Any,
    *,
    endpoint_name: str,
) -> None:
    status_codes: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized_key = re.sub(
                    r"[^A-Z0-9]",
                    "",
                    str(key).upper(),
                )
                if normalized_key in {"SSCODE", "RESULTCODE"}:
                    status_codes.append(
                        re.sub(
                            r"[^A-Z0-9]",
                            "",
                            str(item).strip().upper(),
                        )
                    )
                elif isinstance(item, (Mapping, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    if not status_codes:
        raise SourceSchemaError(
            f"{endpoint_name} has no provider success code."
        )
    if any(
        value not in {"Y", "0", "00", "NORMALSERVICE"}
        for value in status_codes
    ):
        raise SourceSchemaError(
            f"{endpoint_name} reported a provider error."
        )
