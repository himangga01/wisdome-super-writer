"""Optional data.go.kr observations reconciled against official public HTML."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from adapters.sources.housing.lh import _extract_lh_rows, _require_lh_success
from apps.local_content.contracts import CollectionWindow, HousingNotice, SourceRunReport
from apps.local_content.dates import SEOUL
from wisdome_writer.infrastructure.http_safety import HttpSafetyError, safe_get

_MAX_API_BYTES = 5 * 1024 * 1024
_MAX_PAGES = 100
_DATE = re.compile(
    r"(?P<year>\d{4})\s*[./-]\s*(?P<month>\d{1,2})\s*[./-]\s*(?P<day>\d{1,2})"
)
_ALLOWED_API_PATHS = {
    "api.odcloud.kr": frozenset(
        {
            "/api/ApplyhomeInfoDetailSvc/v1/getAPTLttotPblancDetail",
            "/api/ApplyhomeInfoDetailSvc/v1/getRemndrLttotPblancDetail",
        }
    ),
    "apis.data.go.kr": frozenset(
        {"/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"}
    ),
}


class OfficialApiError(RuntimeError):
    """Stable machine code without upstream body, URL query, or credential material."""

    def __init__(self, code: str, _unsafe_detail: object | None = None) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ApiObservation:
    source_key: str
    external_id: str
    title: str
    published_at: datetime


class ApiObserver(Protocol):
    def observe(self, window: CollectionWindow) -> tuple[ApiObservation, ...]: ...


class HtmlCollector(Protocol):
    def collect(self, window: CollectionWindow) -> SourceRunReport: ...


class JsonFetcher(Protocol):
    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, object],
    ) -> dict[str, object]: ...


class OfficialApiFetcher:
    """Bounded official JSON fetcher with an opaque in-memory service key."""

    __slots__ = ("_service_key", "_transport")

    def __init__(
        self,
        service_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not isinstance(service_key, str) or not service_key.strip():
            raise ValueError("official API service key is required")
        self._service_key = service_key.strip()
        self._transport = transport

    def __repr__(self) -> str:
        return "OfficialApiFetcher(service_key=<redacted>)"

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, object],
    ) -> dict[str, object]:
        host = _approved_api_target(url)
        if any(not isinstance(key, str) or not key for key in params):
            raise OfficialApiError("OFFICIAL_API_PARAMETERS_INVALID")
        credential_name = "serviceKey" if host == "api.odcloud.kr" else "ServiceKey"
        try:
            request_url = str(
                httpx.URL(url).copy_merge_params(
                    {
                        **{key: str(value) for key, value in params.items()},
                        credential_name: self._service_key,
                    }
                )
            )
            response = safe_get(
                request_url,
                max_bytes=_MAX_API_BYTES,
                timeout=20,
                max_elapsed_seconds=30,
                max_redirects=0,
                allowed_hosts={host},
                https_only=True,
                allowed_content_types={"application/json", "text/json"},
                transport=self._transport,
            )
        except (HttpSafetyError, TypeError, ValueError):
            raise OfficialApiError("OFFICIAL_API_REQUEST_FAILED") from None
        if response.status_code != 200:
            raise OfficialApiError("OFFICIAL_API_STATUS_FAILED")
        try:
            value = json.loads(
                response.content.decode("utf-8", errors="strict"),
                object_pairs_hook=_strict_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeError, ValueError):
            raise OfficialApiError("OFFICIAL_API_SCHEMA_FAILED") from None
        if not isinstance(value, dict):
            raise OfficialApiError("OFFICIAL_API_SCHEMA_FAILED")
        return value


class ReconciledOfficialCollector:
    """Fail closed on missing, conflicting, duplicate, or unavailable API evidence."""

    def __init__(self, html_collector: HtmlCollector, api_observer: ApiObserver) -> None:
        self._html_collector = html_collector
        self._api_observer = api_observer

    def collect(self, window: CollectionWindow) -> SourceRunReport:
        html = self._html_collector.collect(window)
        if html.errors:
            return html
        try:
            observed = self._api_observer.observe(window)
            api_by_id = _unique_observations(observed, source_key=html.source_key)
        except Exception:
            return SourceRunReport(
                source_key=html.source_key,
                warnings=html.warnings,
                errors=(*html.errors, "OFFICIAL_API_RECONCILIATION_FAILED"),
            )
        html_ids = {notice.external_id for notice in html.notices}
        matched: list[HousingNotice] = []
        conflict = set(api_by_id) != html_ids
        for notice in html.notices:
            observation = api_by_id.get(notice.external_id)
            if observation is None or not _material_agrees(notice, observation):
                conflict = True
                continue
            matched.append(notice)
        return SourceRunReport(
            source_key=html.source_key,
            notices=tuple(matched),
            warnings=(*html.warnings, "OFFICIAL_API_RECONCILED"),
            errors=("OFFICIAL_API_RECONCILIATION_CONFLICT",) if conflict else (),
        )


class ApplyHomeApiObserver:
    ENDPOINTS = {
        "apt": (
            "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/"
            "getAPTLttotPblancDetail"
        ),
        "remaining": (
            "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/"
            "getRemndrLttotPblancDetail"
        ),
    }

    def __init__(self, fetcher: JsonFetcher) -> None:
        self._fetcher = fetcher

    def observe(self, window: CollectionWindow) -> tuple[ApiObservation, ...]:
        observations: list[ApiObservation] = []
        for category, endpoint in self.ENDPOINTS.items():
            observations.extend(self._endpoint(window, category=category, endpoint=endpoint))
        return tuple(sorted(observations, key=lambda value: value.external_id))

    def _endpoint(
        self,
        window: CollectionWindow,
        *,
        category: str,
        endpoint: str,
    ) -> tuple[ApiObservation, ...]:
        rows: list[ApiObservation] = []
        received = 0
        expected_total: int | None = None
        for page in range(1, _MAX_PAGES + 1):
            payload = self._fetcher.get_json(
                endpoint,
                params={
                    "page": page,
                    "perPage": 1000,
                    "cond[RCRIT_PBLANC_DE::GTE]": window.start.date().isoformat(),
                    "cond[RCRIT_PBLANC_DE::LTE]": window.end.date().isoformat(),
                },
            )
            values = payload.get("data")
            total = payload.get("totalCount", 0)
            if not isinstance(values, list) or type(total) is not int or total < 0:
                raise OfficialApiError("OFFICIAL_API_SCHEMA_FAILED")
            if expected_total is None:
                expected_total = total
            elif expected_total != total:
                raise OfficialApiError("OFFICIAL_API_PAGINATION_FAILED")
            for value in values:
                if not isinstance(value, dict):
                    raise OfficialApiError("OFFICIAL_API_SCHEMA_FAILED")
                house = _required_text(value, "HOUSE_MANAGE_NO", "houseManageNo")
                notice = _required_text(value, "PBLANC_NO", "pblancNo")
                published_at = _required_date(
                    value,
                    "RCRIT_PBLANC_DE",
                    "PBLANC_DE",
                    "PBLANC_DT",
                )
                if not window.contains_publication(published_at):
                    raise OfficialApiError("OFFICIAL_API_WINDOW_FAILED")
                rows.append(
                    ApiObservation(
                        source_key="applyhome",
                        external_id=f"applyhome:{category}:{house}:{notice}",
                        title=_required_text(
                            value,
                            "HOUSE_NM",
                            "PBLANC_NM",
                            "NOTICE_TITLE",
                            "TITLE",
                        ),
                        published_at=published_at,
                    )
                )
            received += len(values)
            if received >= total:
                break
            if not values or page == _MAX_PAGES:
                raise OfficialApiError("OFFICIAL_API_PAGINATION_FAILED")
        return tuple(rows)


class LhApiObserver:
    ENDPOINT = "https://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"

    def __init__(self, fetcher: JsonFetcher) -> None:
        self._fetcher = fetcher

    def observe(self, window: CollectionWindow) -> tuple[ApiObservation, ...]:
        observations: list[ApiObservation] = []
        received = 0
        expected_total: int | None = None
        for page in range(1, _MAX_PAGES + 1):
            payload = self._fetcher.get_json(
                self.ENDPOINT,
                params={
                    "PAGE": page,
                    "PG_SZ": 1000,
                    "PAN_NT_ST_DT": window.start.strftime("%Y.%m.%d"),
                    "CLSG_DT": window.end.strftime("%Y.%m.%d"),
                },
            )
            try:
                _require_lh_success(payload, endpoint_name="LH list endpoint")
                values, total = _extract_lh_rows(payload)
            except Exception:
                raise OfficialApiError("OFFICIAL_API_SCHEMA_FAILED") from None
            if total is None:
                total = len(values)
            if expected_total is None:
                expected_total = total
            elif expected_total != total:
                raise OfficialApiError("OFFICIAL_API_PAGINATION_FAILED")
            for value in values:
                published_at = _required_date(
                    value,
                    "PAN_NT_ST_DT",
                    "PAN_NT_ST_DTTM",
                    "PBLANC_DT",
                )
                if not window.contains_publication(published_at):
                    raise OfficialApiError("OFFICIAL_API_WINDOW_FAILED")
                ccr = _required_text(value, "CCR_CNNT_SYS_DS_CD")
                pan_id = _required_text(value, "PAN_ID")
                upp = _required_text(value, "UPP_AIS_TP_CD")
                ais = _required_text(value, "AIS_TP_CD")
                observations.append(
                    ApiObservation(
                        source_key="lh",
                        external_id=f"lh:{ccr}:{pan_id}:{upp}:{ais}",
                        title=_required_text(value, "PAN_NM", "PAN_NM_CN", "TITLE"),
                        published_at=published_at,
                    )
                )
            received += len(values)
            if received >= total:
                break
            if not values or page == _MAX_PAGES:
                raise OfficialApiError("OFFICIAL_API_PAGINATION_FAILED")
        return tuple(sorted(observations, key=lambda value: value.external_id))


def _approved_api_target(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise OfficialApiError("OFFICIAL_API_TARGET_UNAPPROVED") from None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_API_PATHS
        or parsed.path not in _ALLOWED_API_PATHS[host]
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise OfficialApiError("OFFICIAL_API_TARGET_UNAPPROVED")
    return host


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _unique_observations(
    observations: tuple[ApiObservation, ...],
    *,
    source_key: str,
) -> dict[str, ApiObservation]:
    result: dict[str, ApiObservation] = {}
    for observation in observations:
        if observation.source_key != source_key or observation.external_id in result:
            raise OfficialApiError("OFFICIAL_API_IDENTITY_FAILED")
        result[observation.external_id] = observation
    return result


def _material_agrees(notice: HousingNotice, observation: ApiObservation) -> bool:
    return (
        notice.external_id == observation.external_id
        and notice.published_at.astimezone(SEOUL).date()
        == observation.published_at.astimezone(SEOUL).date()
        and _normalized_title(notice.title) == _normalized_title(observation.title)
    )


def _normalized_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _required_text(value: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        observed = value.get(key)
        if isinstance(observed, (str, int)) and str(observed).strip():
            return str(observed).strip()
    raise OfficialApiError("OFFICIAL_API_SCHEMA_FAILED")


def _required_date(value: Mapping[str, object], *keys: str) -> datetime:
    text = _required_text(value, *keys)
    match = _DATE.search(text)
    if match is None:
        raise OfficialApiError("OFFICIAL_API_SCHEMA_FAILED")
    try:
        return datetime(
            int(match["year"]),
            int(match["month"]),
            int(match["day"]),
            tzinfo=SEOUL,
        )
    except ValueError:
        raise OfficialApiError("OFFICIAL_API_SCHEMA_FAILED") from None
