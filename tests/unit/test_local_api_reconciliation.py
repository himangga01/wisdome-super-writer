from __future__ import annotations

from datetime import datetime

import pytest

from apps.local_content.contracts import CollectionWindow, HousingNotice, SourceRunReport
from apps.local_content.dates import SEOUL


def _window() -> CollectionWindow:
    return CollectionWindow(
        start=datetime(2026, 8, 22, tzinfo=SEOUL),
        end=datetime(2026, 8, 28, 23, 0, tzinfo=SEOUL),
    )


def _notice(
    *,
    source_key: str = "applyhome",
    external_id: str = "applyhome:apt:2026000001:2026000001",
    title: str = "공식 주택 공급 공고",
    published_at: datetime | None = None,
) -> HousingNotice:
    return HousingNotice(
        source_key=source_key,
        external_id=external_id,
        canonical_url=(
            "https://www.applyhome.co.kr/ai/aia/detail.do?a=1"
            if source_key == "applyhome"
            else "https://apply.lh.or.kr/lhapply/apply/detail.do?a=1"
        ),
        title=title,
        publisher="ApplyHome" if source_key == "applyhome" else "LH",
        category="apt" if source_key == "applyhome" else "공공임대",
        region="서울",
        status="published",
        published_at=published_at or datetime(2026, 8, 28, tzinfo=SEOUL),
        source_checksum="a" * 64,
        parser_version="test",
    )


class HtmlCollector:
    def __init__(self, report: SourceRunReport) -> None:
        self.report = report

    def collect(self, _window: CollectionWindow) -> SourceRunReport:
        return self.report


class ApiObserver:
    def __init__(self, observations=(), failure: Exception | None = None) -> None:
        self.observations = tuple(observations)
        self.failure = failure

    def observe(self, _window: CollectionWindow):
        if self.failure is not None:
            raise self.failure
        return self.observations


def test_no_key_builds_html_only_collectors(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.local_content.management.commands import collect_recent_housing as command
    from apps.local_content.sources.applyhome import ApplyHomePublicCollector
    from apps.local_content.sources.lh import LhPublicCollector

    monkeypatch.delenv("DATA_GO_KR_SERVICE_KEY", raising=False)
    monkeypatch.setattr(
        command,
        "OfficialApiFetcher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("API fetcher must remain inactive without a key")
        ),
        raising=False,
    )

    workflow = command._build_workflow(fixture_root=None, dry_run=True)

    assert tuple(type(value) for value in workflow.collectors) == (
        ApplyHomePublicCollector,
        LhPublicCollector,
    )


def test_matching_api_identity_date_and_title_preserves_html_notice() -> None:
    from apps.local_content.api_reconciliation import (
        ApiObservation,
        ReconciledOfficialCollector,
    )

    notice = _notice()
    collector = ReconciledOfficialCollector(
        HtmlCollector(SourceRunReport(source_key="applyhome", notices=(notice,))),
        ApiObserver(
            (
                ApiObservation(
                    source_key="applyhome",
                    external_id=notice.external_id,
                    title="  공식   주택 공급 공고 ",
                    published_at=notice.published_at,
                ),
            )
        ),
    )

    report = collector.collect(_window())

    assert report.notices == (notice,)
    assert report.errors == ()
    assert report.warnings == ("OFFICIAL_API_RECONCILED",)


@pytest.mark.parametrize("conflict", ["identity", "date", "title"])
def test_material_api_conflict_quarantines_affected_notice(conflict: str) -> None:
    from apps.local_content.api_reconciliation import (
        ApiObservation,
        ReconciledOfficialCollector,
    )

    notice = _notice()
    observation = ApiObservation(
        source_key="applyhome",
        external_id=("applyhome:apt:other:other" if conflict == "identity" else notice.external_id),
        title=("다른 공고" if conflict == "title" else notice.title),
        published_at=(
            datetime(2026, 8, 27, tzinfo=SEOUL)
            if conflict == "date"
            else notice.published_at
        ),
    )
    collector = ReconciledOfficialCollector(
        HtmlCollector(SourceRunReport(source_key="applyhome", notices=(notice,))),
        ApiObserver((observation,)),
    )

    report = collector.collect(_window())

    assert report.notices == ()
    assert report.errors == ("OFFICIAL_API_RECONCILIATION_CONFLICT",)


def test_api_failure_fails_source_closed_without_leaking_secret() -> None:
    from apps.local_content.api_reconciliation import (
        OfficialApiError,
        ReconciledOfficialCollector,
    )

    secret = "data-go-key-must-never-surface"
    collector = ReconciledOfficialCollector(
        HtmlCollector(SourceRunReport(source_key="lh", notices=(_notice(source_key="lh"),))),
        ApiObserver(failure=OfficialApiError("OFFICIAL_API_REQUEST_FAILED", secret)),
    )

    report = collector.collect(_window())

    assert report.notices == ()
    assert report.errors == ("OFFICIAL_API_RECONCILIATION_FAILED",)
    assert secret not in repr(report)
    assert secret not in str(report)


def test_sanitized_applyhome_and_lh_api_payloads_map_existing_adapter_identities() -> None:
    from apps.local_content.api_reconciliation import (
        ApplyHomeApiObserver,
        LhApiObserver,
    )

    class JsonFetcher:
        def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
            self.payloads = payloads

        def get_json(self, url: str, *, params: dict[str, object]) -> dict[str, object]:
            del params
            return self.payloads[url]

    applyhome_url = ApplyHomeApiObserver.ENDPOINTS["apt"]
    lh_url = LhApiObserver.ENDPOINT
    fetcher = JsonFetcher(
        {
            applyhome_url: {
                "data": [
                    {
                        "HOUSE_MANAGE_NO": "2026000001",
                        "PBLANC_NO": "2026000001",
                        "HOUSE_NM": "공식 주택 공급 공고",
                        "RCRIT_PBLANC_DE": "2026-08-28",
                    }
                ],
                "totalCount": 1,
            },
            ApplyHomeApiObserver.ENDPOINTS["remaining"]: {"data": [], "totalCount": 0},
            lh_url: {
                "response": {"resultCode": "00"},
                "data": [
                    {
                        "CCR_CNNT_SYS_DS_CD": "02",
                        "PAN_ID": "0000061158",
                        "UPP_AIS_TP_CD": "05",
                        "AIS_TP_CD": "05",
                        "PAN_NM": "LH 공식 임대 공고",
                        "PAN_NT_ST_DT": "2026.08.28",
                        "ALL_CNT": "1",
                    }
                ],
            },
        }
    )

    applyhome = ApplyHomeApiObserver(fetcher).observe(_window())
    lh = LhApiObserver(fetcher).observe(_window())

    assert applyhome[0].external_id == "applyhome:apt:2026000001:2026000001"
    assert lh[0].external_id == "lh:02:0000061158:05:05"
    assert applyhome[0].published_at.isoformat() == "2026-08-28T00:00:00+09:00"
    assert lh[0].published_at.isoformat() == "2026-08-28T00:00:00+09:00"


def test_api_fetcher_repr_and_failure_never_expose_service_key() -> None:
    from apps.local_content.api_reconciliation import OfficialApiFetcher

    secret = "data-go-key-must-never-surface"
    fetcher = OfficialApiFetcher(secret)

    assert secret not in repr(fetcher)
    with pytest.raises(Exception) as raised:
        fetcher.get_json("https://not-approved.example/api", params={})
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
