from __future__ import annotations

import json
from datetime import UTC, date, datetime
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.core.management.base import CommandError

from apps.local_content.bundles import ArticleBundleWriter
from apps.local_content.contracts import HousingNotice, SourceRunReport
from apps.local_content.management.commands import collect_recent_housing as command_module
from apps.local_content.workflow import LocalHousingWorkflow

SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 8, 12, tzinfo=SEOUL)


class WindowCollector:
    source_key = "applyhome"

    def __init__(self):
        self.windows = []

    def collect(self, window):
        self.windows.append(window)
        return SourceRunReport(
            source_key=self.source_key,
            notices=(
                HousingNotice(
                    source_key=self.source_key,
                    external_id="applyhome:apt:earlier:earlier",
                    canonical_url="https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancDetail.do",
                    title="September announcement with October subscription",
                    publisher="ApplyHome",
                    category="sale",
                    region=None,
                    status="published",
                    published_at=datetime(2026, 9, 2, tzinfo=SEOUL),
                    facts=(("1순위", "2026-10-06"),),
                    source_checksum="a" * 64,
                    parser_version="schedule-fixture-v1",
                ),
            ),
        )


def _workflow(tmp_path):
    collector = WindowCollector()
    workflow = LocalHousingWorkflow(
        collectors=(collector,),
        humanizer=None,
        output_root=tmp_path / "housing",
        state_root=tmp_path / "state",
        mode="fixture",
    )
    return workflow, collector


def test_explicit_start_includes_earlier_announcements_and_commits_bound_metadata(tmp_path):
    workflow, collector = _workflow(tmp_path)

    report = workflow.run(
        now=NOW,
        days=8,
        collection_start=datetime(2026, 8, 31, 15, tzinfo=UTC),
        humanize=False,
        write_articles=False,
    )

    assert report.complete is True
    assert report.notice_count == 1
    assert report.window.start == datetime(2026, 9, 1, tzinfo=SEOUL)
    assert collector.windows[0].end == NOW
    run = ArticleBundleWriter(workflow.output_root).validate_run(
        date(2026, 10, 8), run_directory=report.run_path
    )
    notices = json.loads((run / "notices.json").read_text("utf-8"))
    assert notices["window"]["start"] == "2026-09-01T00:00:00+09:00"
    assert notices["notices"][0]["facts"] == [["1순위", "2026-10-06"]]


@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 9, 1),
        datetime(2026, 10, 9, tzinfo=SEOUL),
        datetime(2025, 10, 7, tzinfo=SEOUL),
    ],
)
def test_invalid_start_fails_before_collection_or_storage(tmp_path, start):
    workflow, collector = _workflow(tmp_path)

    with pytest.raises(ValueError, match="collection_start"):
        workflow.run(now=NOW, collection_start=start, humanize=False, write_articles=False)

    assert collector.windows == []
    assert not workflow.output_root.exists()
    assert not workflow.state_root.exists()


def test_default_day_window_remains_unchanged(tmp_path):
    workflow, _collector = _workflow(tmp_path)

    report = workflow.run(now=NOW, days=8, humanize=False, write_articles=False)

    assert report.window.start == datetime(2026, 10, 1, tzinfo=SEOUL)
    assert report.notice_count == 0


def test_cli_since_is_strict_iso_date_and_keeps_old_default():
    parser = command_module.Command().create_parser("manage.py", "collect_recent_housing")

    assert parser.parse_args([]).since is None
    assert parser.parse_args(["--since", "2026-09-01"]).since == date(2026, 9, 1)
    with pytest.raises(CommandError, match="YYYY-MM-DD"):
        parser.parse_args(["--since", "20260901"])


def test_cli_since_forwards_midnight_kst_to_real_notice_only_workflow(tmp_path, monkeypatch):
    workflow, _collector = _workflow(tmp_path)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(command_module, "datetime", FixedDateTime)
    monkeypatch.setattr(command_module, "_build_workflow", lambda **_kwargs: workflow)
    output = StringIO()
    command = command_module.Command(stdout=output)
    command.handle(
        days=8,
        since=date(2026, 9, 1),
        humanize=False,
        write_articles=False,
        selected_id=[],
        fixture_root=None,
        dry_run=False,
    )

    result = json.loads(output.getvalue())
    run = workflow.output_root / result["run_path"]
    assert (
        json.loads((run / "notices.json").read_text("utf-8"))["window"]["start"]
        == "2026-09-01T00:00:00+09:00"
    )
    assert result["written_count"] == 0
