from __future__ import annotations

import json
from datetime import date, datetime
from importlib import import_module
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from apps.local_content.bundles import ArticleBundle, ArticleBundleWriter, BundleFile

SEOUL = ZoneInfo("Asia/Seoul")
RUN_DATE = date(2026, 10, 8)


def _load(**kwargs):
    return import_module("wisdome_writer.portal.housing").load_housing_snapshot(**kwargs)


def _notice(**changes):
    return {
        "source_key": "applyhome",
        "external_id": "notice-1",
        "canonical_url": "https://www.applyhome.co.kr/notice/1",
        "title": "서울 주택 공급",
        "publisher": "청약홈",
        "category": "sale",
        "region": None,
        "status": "published",
        "published_at": "2026-10-07T23:00:00+00:00",
        "application_start": "2026-10-12",
        "application_end": None,
        "deadline": "2026-10-15",
        "announcement_date": None,
        "supply_count": None,
        "price_summary": None,
        "eligibility_summary": [],
        "restriction_summary": [],
        "facts": [],
        "source_checksum": "a" * 64,
        "parser_version": "test-v1",
        "warning_count": 0,
        "error_codes": [],
        **changes,
    }


def _document(*, notices=None, end="2026-10-08T16:00:00+09:00", **changes):
    return {
        "schema_version": 1,
        "window": {"start": "2026-10-02T00:00:00+09:00", "end": end},
        "complete": True,
        "detail_failures": [],
        "notices": [_notice()] if notices is None else notices,
        "conflicts": [],
        **changes,
    }


def _write_run(
    root: Path, document=None, *, workflow_id="base", legacy=False, bundles=(), run_date=RUN_DATE
):
    writer = ArticleBundleWriter(root)
    lease = writer.reserve_run_directory(run_date, workflow_id=workflow_id)
    metadata = _document() if document is None else document
    files = None
    if not legacy:
        files = {
            "notices.json": json.dumps(metadata, ensure_ascii=False),
            "raw-observations.json": json.dumps(
                {
                    "schema_version": 1,
                    "window": metadata["window"],
                    "sources": [],
                    "observations": [],
                }
            ),
        }
    return writer.write_run(
        run_date, "# Saved housing run\n", bundles, run_directory=lease, run_files=files
    )


@pytest.fixture
def root(settings, tmp_path):
    settings.IS_LOCAL_RUNTIME = True
    settings.LOCAL_ARTICLE_ROOT = tmp_path / "housing"
    return settings.LOCAL_ARTICLE_ROOT


def test_absent_root_is_empty_without_creating_directories(root):
    result = _load()
    assert result["available"] is True
    assert result["total"] == 0
    assert result["notices"] == result["runs"] == []
    assert result["snapshot_name"] is result["observed_at"] is None
    assert result["message"]
    assert not root.exists()


def test_saved_notices_preserve_unknown_dates_and_observed_status(root):
    _write_run(root)
    result = _load()
    assert result["available"] is True
    assert result["total"] == 1
    assert result["snapshot_name"] == "2026-10-08"
    assert result["observed_at"] == datetime(2026, 10, 8, 16, tzinfo=SEOUL)
    assert result["notices"] == [
        {
            "title": "서울 주택 공급",
            "publisher": "청약홈",
            "url": "https://www.applyhome.co.kr/notice/1",
            "published_on": "2026-10-08",
            "application_start": "2026-10-12",
            "application_end": None,
            "deadline": "2026-10-15",
            "region": None,
            "category": "sale",
            "status": "published",
            "source_key": "applyhome",
            "external_id": "notice-1",
            "source_checksum": "a" * 64,
            "subscription_events": [],
            "captured_at": datetime(2026, 10, 8, 16, tzinfo=SEOUL),
            "run_name": "2026-10-08",
        }
    ]


def test_snapshot_order_uses_saved_window_time_instead_of_random_suffix(root):
    _write_run(root, _document(end="2026-10-08T09:00:00+09:00"))
    earlier = _write_run(root, _document(), workflow_id="first")
    latest = _write_run(
        root,
        _document(end="2026-10-08T17:00:00+09:00", notices=[_notice(title="Latest")]),
        workflow_id="second",
    )
    assert earlier.name > latest.name
    result = _load()
    assert result["snapshot_name"] == latest.name
    assert result["notices"][0]["title"] == "Latest"
    assert result["runs"][0]["name"] == latest.name


def test_schema_one_unbound_metadata_is_never_a_trusted_snapshot(root):
    run = _write_run(root, legacy=True)
    (run / "notices.json").write_text(json.dumps(_document()), encoding="utf-8")
    result = _load()
    assert result["available"] is False
    assert result["total"] is None
    assert result["notices"] == []
    assert result["runs"] == [
        {
            "name": "2026-10-08",
            "url": "/local-articles/2026-10-08/",
            "article_count": 0,
            "date": "2026-10-08",
        }
    ]


def test_mutable_acceptance_report_does_not_change_snapshot_or_provenance(root):
    run = _write_run(root)
    before = _load()
    (run / "acceptance-report.json").write_text(
        '{"mode":"live","live_success":true,"started_at":"2099-01-01"}', encoding="utf-8"
    )
    assert _load() == before


@pytest.mark.parametrize("filename", ["notices.json", "raw-observations.json", "index.md"])
def test_tampered_committed_material_is_unavailable(root, filename):
    run = _write_run(root)
    with (run / filename).open("ab") as handle:
        handle.write(b"tampered")
    result = _load()
    assert result["available"] is False
    assert result["total"] is None
    assert result["notices"] == result["runs"] == []
    assert str(root) not in result["message"]


@pytest.mark.parametrize(
    "changes",
    [
        {"title": ["wrong shape"]},
        {"publisher": 123},
        {"status": {}},
        {"region": []},
        {"application_start": "2026-02-30"},
        {"application_end": "20261015"},
        {"published_at": "2026-10-08T12:00:00"},
        {"canonical_url": ["https://www.applyhome.co.kr/"]},
    ],
)
def test_hash_valid_notice_with_invalid_shape_is_unavailable(root, changes):
    _write_run(root, _document(notices=[_notice(**changes)]))
    result = _load()
    assert result["available"] is False
    assert result["total"] is None
    assert result["notices"] == []


@pytest.mark.parametrize(
    "window",
    [
        {"start": "2026-10-02T00:00:00+09:00", "end": "2026-10-08T16:00:00"},
        {"start": "2026-10-09T00:00:00+09:00", "end": "2026-10-08T16:00:00+09:00"},
        {"start": "2026-10-02T00:00:00+09:00", "end": "2026-11-08T16:00:00+09:00"},
    ],
)
def test_invalid_or_mismatched_collection_window_cannot_select_snapshot(root, window):
    _write_run(root, _document(window=window))
    assert _load()["available"] is False


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://example.com/notice",
        "//www.applyhome.co.kr/",
        "https://name@www.applyhome.co.kr/",
        "https://www.applyhome.co.kr:8443/",
        "https://www.applyhome.co.kr/\nspoof",
        "https://www.applyhome.co.kr/\\example.com",
    ],
)
def test_unsafe_official_link_is_plain_text_only(root, url):
    _write_run(root, _document(notices=[_notice(canonical_url=url)]))
    result = _load()
    assert result["available"] is True
    assert result["notices"][0]["url"] is None


def test_final_article_counts_exclude_draft_only_bundles(root):
    final = ArticleBundle(
        RUN_DATE, "final", (BundleFile.text("article.md", "# Final", "text/markdown"),)
    )
    draft = ArticleBundle(
        RUN_DATE, "draft", (BundleFile.text("article.draft.md", "# Draft", "text/markdown"),)
    )
    _write_run(root, bundles=(final, draft))
    result = _load()
    assert result["available"] is True
    assert result["runs"][0]["article_count"] == 1


def test_file_instead_of_root_is_unavailable_without_path_disclosure(root):
    root.write_text("not a directory", encoding="utf-8")
    result = _load()
    assert result["available"] is False
    assert result["total"] is None
    assert str(root) not in result["message"]


def test_incomplete_snapshot_is_available_with_explicit_limit_message(root):
    _write_run(root, _document(complete=False))
    result = _load()
    assert result["available"] is True
    assert result["total"] == 1
    assert "일부" in result["message"]


def test_record_projection_is_bounded_but_total_counts_the_selected_snapshot(root):
    notices = [_notice(external_id=f"n-{index}", title=f"Notice {index}") for index in range(105)]
    _write_run(root, _document(notices=notices))
    result = _load()
    assert result["available"] is True
    assert result["total"] == 105
    assert len(result["notices"]) == 100


def test_excessive_history_returns_unavailable_instead_of_guessing_latest(root):
    root.mkdir()
    for index in range(129):
        (root / f"2026-10-08--run-{index:012x}").mkdir()
    result = _load()
    assert result["available"] is False
    assert result["total"] is None
    assert result["snapshot_name"] is None


def test_payload_is_bound_to_the_manifest_used_for_projection(root, monkeypatch):
    _write_run(root)
    original = ArticleBundleWriter.read_validated_run_file

    def substituted(self, *args, **kwargs):
        payload = original(self, *args, **kwargs)
        return payload.replace("서울 주택 공급".encode(), "변경된 공고".encode())

    monkeypatch.setattr(ArticleBundleWriter, "read_validated_run_file", substituted)
    assert _load()["available"] is False


def test_unrepresentable_kst_timestamp_returns_unavailable(root):
    _write_run(root, _document(notices=[_notice(published_at="9999-12-31T23:00:00-09:00")]))
    result = _load()
    assert result["available"] is False
    assert result["total"] is None


def test_reading_committed_snapshot_never_requests_directory_creation(root, monkeypatch):
    _write_run(root)

    def forbidden_mkdir(*_args, **_kwargs):
        raise AssertionError("Reading a saved run must not create directories")

    monkeypatch.setattr(Path, "mkdir", forbidden_mkdir)
    assert _load()["available"] is True


def test_invalid_generation_is_excluded_without_hiding_valid_saved_snapshot(root):
    older = _write_run(root)
    newest = _write_run(root, workflow_id="newest")
    (newest / "notices.json").write_text("{}", encoding="utf-8")
    result = _load()
    assert result["available"] is True
    assert result["snapshot_name"] == older.name
    assert len(result["runs"]) == 1
    assert "제외" in result["message"]


@pytest.mark.parametrize("field", ["title", "publisher", "region"])
def test_search_finds_matching_notice_beyond_default_visible_rows(root, field):
    notices = [_notice(external_id=f"new-{index}", title=f"Recent {index}") for index in range(100)]
    notices.append(
        _notice(
            external_id="old-target",
            published_at="2026-10-06T09:00:00+09:00",
            **{field: "Hidden Match"},
        )
    )
    _write_run(root, _document(notices=notices))
    assert all(row[field] != "Hidden Match" for row in _load()["notices"])

    result = _load(query="  hidden MATCH  ")

    assert len(result["notices"]) == 1
    assert result["notices"][0][field] == "Hidden Match"
    assert result["matching_total"] == 1
    assert result["total"] == 101


def test_search_counts_all_matches_before_visible_limit(root):
    notices = [
        _notice(external_id=f"match-{index}", title=f"Matching notice {index}")
        for index in range(105)
    ]
    notices.append(_notice(external_id="excluded", title="Unrelated notice"))
    _write_run(root, _document(notices=notices))

    result = _load(query="matching")

    assert len(result["notices"]) == 100
    assert result["matching_total"] == 105
    assert result["total"] == 106


def test_no_search_matches_preserves_selected_snapshot_counts(root):
    _write_run(root)
    result = _load(query="does-not-exist")
    assert result["available"] is True
    assert result["notices"] == []
    assert result["matching_total"] == 0
    assert result["total"] == 1
    assert result["snapshot_name"] == "2026-10-08"


def test_empty_search_matches_every_notice_in_selected_snapshot(root):
    _write_run(root)
    result = _load(query="  ")
    assert result["matching_total"] == result["total"] == 1
    assert len(result["notices"]) == 1


def test_empty_storage_has_zero_search_matches(root):
    result = _load(query="housing")
    assert result["matching_total"] == result["total"] == 0
    assert not root.exists()


def test_unavailable_storage_has_unknown_search_count(root):
    run = _write_run(root)
    (run / "notices.json").write_text("{}", encoding="utf-8")
    result = _load(query="housing")
    assert result["available"] is False
    assert result["matching_total"] is result["total"] is None


@pytest.mark.parametrize("query", ["q" * 201, None, [], 123])
def test_invalid_search_query_is_rejected_before_reading_storage(root, query, monkeypatch):
    def forbidden_read(**_kwargs):
        raise AssertionError("Invalid queries must be rejected before filesystem access")

    monkeypatch.setattr("wisdome_writer.portal.housing._article_root", forbidden_read)
    with pytest.raises(ValueError, match="query"):
        _load(query=query)
    assert not root.exists()


def _period(year=2026, month=10):
    from wisdome_writer.portal.periods import PeriodSelection

    return PeriodSelection(year=year, month=month, basis="schedule")


def test_month_uses_confirmed_special_first_dates_instead_of_publication(root):
    _write_run(
        root,
        _document(
            notices=[
                _notice(
                    external_id="september-publication",
                    published_at="2026-09-18T00:00:00+09:00",
                    facts=[["특별공급", "2026-10-05"], ["1순위", "2026-10-06"]],
                ),
                _notice(external_id="november-first", facts=[["1순위", "2026-11-03"]]),
            ]
        ),
    )

    result = _load(period=_period())

    assert result["matching_total"] == result["total"] == 1
    assert result["notices"][0]["external_id"] == "september-publication"
    assert result["notices"][0]["subscription_events"] == [
        {"kind": "special", "label": "특별공급", "text": "2026-10-05", "dates": ["2026-10-05"]},
        {"kind": "first", "label": "1순위", "text": "2026-10-06", "dates": ["2026-10-06"]},
    ]


def test_generic_dates_remain_unknown_and_do_not_match_month(root):
    _write_run(root, _document(notices=[_notice(facts=[["신청기간", "2026-10-01 ~ 2026-10-30"]])]))

    result = _load(period=_period())

    assert result["notices"] == []
    assert result["total"] == 0
    assert result["unknown_schedule_count"] == 1


@pytest.mark.parametrize("stage,want", [("all", 1), ("first", 1), ("special", 0)])
def test_stage_filter_uses_separate_explicit_dates_and_preserves_regions(root, stage, want):
    _write_run(
        root,
        _document(
            notices=[
                _notice(
                    facts=[
                        ["특별공급", "2026-09-30"],
                        ["1순위 · 해당지역", "2026-10-01"],
                        ["1순위 · 기타지역", "2026-10-02"],
                    ]
                )
            ]
        ),
    )

    result = _load(period=_period(), stage=stage)

    assert result["total"] == want
    if want:
        events = result["notices"][0]["subscription_events"]
        assert [event["label"] for event in events][-2:] == ["1순위 · 해당지역", "1순위 · 기타지역"]


def test_month_filters_history_before_identity_dedup_for_changed_schedule(root):
    _write_run(
        root, _document(notices=[_notice(title="October version", facts=[["1순위", "2026-10-06"]])])
    )
    newest = _write_run(
        root,
        _document(
            end="2026-10-08T17:00:00+09:00",
            notices=[
                _notice(
                    title="November correction",
                    source_checksum="b" * 64,
                    facts=[["1순위", "2026-11-06"]],
                )
            ],
        ),
        workflow_id="corrected",
    )

    result = _load(period=_period())

    assert result["notices"][0]["title"] == "October version"
    assert result["notices"][0]["run_name"] == "2026-10-08"
    assert result["history_total"] == 1
    assert _load()["notices"][0]["run_name"] == newest.name


def test_saved_september_run_is_in_october_schedule_history(root):
    _write_run(
        root,
        _document(
            window={"start": "2026-09-01T00:00:00+09:00", "end": "2026-09-20T12:00:00+09:00"},
            notices=[
                _notice(
                    published_at="2026-09-18T00:00:00+09:00", facts=[["특별공급", "2026-10-05"]]
                )
            ],
        ),
        run_date=date(2026, 9, 20),
    )
    _write_run(root, _document(notices=[]))

    result = _load(period=_period())

    assert result["notices"][0]["run_name"] == "2026-09-20"
    assert result["notices"][0]["captured_at"] == datetime(2026, 9, 20, 12, tzinfo=SEOUL)


def test_month_and_search_apply_before_visible_limit_across_saved_runs(root):
    notices = [
        _notice(
            external_id=f"november-{index}", title=f"New {index}", facts=[["1순위", "2026-11-02"]]
        )
        for index in range(100)
    ]
    notices.append(
        _notice(
            external_id="october-target", title="Hidden target", facts=[["특별공급", "2026-10-03"]]
        )
    )
    _write_run(root, _document(notices=notices))
    _write_run(root, _document(notices=[]), workflow_id="latest")

    result = _load(period=_period(), query="target")

    assert result["total"] == result["matching_total"] == 1
    assert result["notices"][0]["external_id"] == "october-target"


def test_same_month_identity_uses_latest_qualifying_observation_and_keeps_sources(root):
    _write_run(root, _document(notices=[_notice(title="Old", facts=[["1순위", "2026-10-06"]])]))
    _write_run(
        root,
        _document(
            end="2026-10-08T17:00:00+09:00",
            notices=[
                _notice(title="New", source_checksum="b" * 64, facts=[["1순위", "2026-10-07"]]),
                _notice(
                    title="Different source", source_key="lh", facts=[["특별공급", "2026-10-06"]]
                ),
            ],
        ),
        workflow_id="new",
    )

    result = _load(period=_period())

    assert result["total"] == 2
    assert {row["title"] for row in result["notices"]} == {"New", "Different source"}


@pytest.mark.parametrize(
    "text",
    [
        "미정",
        "2026-02-30",
        "2026-10-32 ~ 2026-11-01",
        "2026-10-062",
        "12026-10-06",
        "2026-10-062 ~ 2026-11-01",
        "12026-10-05 ~ 2026-10-06",
        "2026-10-05 ~ 2026-10-062",
    ],
)
def test_unconfirmed_or_invalid_stage_dates_stay_unknown(root, text):
    _write_run(root, _document(notices=[_notice(facts=[["특별공급", text]])]))

    result = _load(period=_period())

    assert result["total"] == 0
    assert result["unknown_schedule_count"] == 1


def test_invalid_stage_is_rejected_before_filesystem_access(root, monkeypatch):
    def forbidden_read(**_kwargs):
        raise AssertionError("Invalid stage must not touch storage")

    monkeypatch.setattr("wisdome_writer.portal.housing._article_root", forbidden_read)
    with pytest.raises(ValueError, match="stage"):
        _load(stage="second")


def test_available_years_come_from_confirmed_stage_dates(root):
    _write_run(
        root,
        _document(notices=[_notice(facts=[["1순위", "2027-01-03"], ["특별공급", "2026-10-05"]])]),
    )

    result = _load(period=_period())

    assert result["available_years"] == [2027, 2026]


@pytest.mark.parametrize("label", ["특별공급 자격", "1순위 대상", "1순위 신청자 출생일"])
def test_eligibility_birth_dates_are_not_subscription_events(root, label):
    _write_run(root, _document(notices=[_notice(facts=[[label, "2000-10-01 이후 출생"]])]))

    result = _load(period=_period(2000, 10))

    assert result["total"] == 0
    assert result["unknown_schedule_count"] == 1


def test_conflicting_identities_within_one_run_are_not_silently_deduplicated(root):
    _write_run(
        root,
        _document(
            notices=[
                _notice(facts=[["1순위", "2026-10-05"]]),
                _notice(source_checksum="b" * 64, facts=[["1순위", "2026-10-06"]]),
            ]
        ),
    )

    assert _load(period=_period())["available"] is False


def test_quarantined_conflicts_keep_a_visible_incomplete_caveat(root):
    _write_run(root, _document(conflicts=[{"external_id": "conflict"}]))

    assert "누락" in _load()["message"]


@pytest.mark.parametrize(
    "text", ["2026-10-01 기준 만19세", "2000-10-01 이후 출생", "2026-10-05 (미정)"]
)
def test_stage_named_fact_still_requires_confirmed_calendar_date_text(root, text):
    _write_run(root, _document(notices=[_notice(facts=[["특별공급", text]])]))

    assert _load(period=_period())["unknown_schedule_count"] == 1


def test_calendar_dates_preserve_weekday_and_reception_time_text(root):
    _write_run(
        root, _document(notices=[_notice(facts=[["1순위", "2026.10.05 (월) 09:00 ~ 17:30"]])])
    )

    result = _load(period=_period())

    assert result["notices"][0]["subscription_events"][0]["dates"] == ["2026-10-05"]


@pytest.mark.parametrize("stage", [None, [], 1])
def test_invalid_stage_types_raise_value_error(root, stage):
    with pytest.raises(ValueError, match="stage"):
        _load(stage=stage)
