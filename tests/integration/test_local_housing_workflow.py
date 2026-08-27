from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.management.base import CommandError

from apps.local_content.contracts import HousingNotice, SourceRunReport
from apps.local_content.dates import SEOUL
from apps.local_content.management.commands import collect_recent_housing as command_module
from apps.local_content.management.commands.collect_recent_housing import Command
from apps.local_content.rendering import notice_slug
from apps.local_content.workflow import (
    LocalHousingWorkflow,
    WorkflowLockedError,
)


@dataclass
class StaticCollector:
    report: SourceRunReport
    observed_lock: dict[str, object] | None = None
    lock_path: Path | None = None

    @property
    def source_key(self) -> str:
        return self.report.source_key

    def collect(self, _window):
        if self.lock_path is not None:
            self.observed_lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        return self.report


class EchoHumanizer:
    def __init__(self) -> None:
        self.documents: list[str] = []

    def transform(self, document: str) -> str:
        self.documents.append(document)
        return document


class FailFirstHumanizer(EchoHumanizer):
    def transform(self, document: str) -> str:
        self.documents.append(document)
        if len(self.documents) == 1:
            raise RuntimeError("body that must never enter a report")
        return document


class TamperingHumanizer(EchoHumanizer):
    def transform(self, document: str) -> str:
        self.documents.append(document)
        return document.replace("[[P0001]]", "", 1)


def _notice(
    external_id: str,
    *,
    source_key: str = "applyhome",
    title: str | None = None,
    category: str = "apt",
    checksum_digit: str = "a",
    warnings: tuple[str, ...] = (),
) -> HousingNotice:
    canonical_url = (
        "https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancDetailView.do?"
        f"houseManageNo={external_id}&pblancNo={external_id}&houseSecd=01"
        if source_key == "applyhome"
        else "https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancInfo.do?"
        f"aisTpCd=05&ccrCnntSysDsCd=02&mi=1026&panId={external_id}&uppAisTpCd=05"
    )
    return HousingNotice(
        source_key=source_key,
        external_id=external_id,
        canonical_url=canonical_url,
        title=title or f"Official fixture notice {external_id}",
        publisher="ApplyHome" if source_key == "applyhome" else "LH",
        category=category,
        region="Seoul",
        status="open",
        published_at=datetime(2026, 8, 25, 9, 0, tzinfo=SEOUL),
        supply_count=12,
        facts=(("project", external_id),),
        source_checksum=checksum_digit * 64,
        parser_version="fixture-v1",
        warnings=warnings,
    )


def _collector(source_key: str, *notices: HousingNotice) -> StaticCollector:
    return StaticCollector(SourceRunReport(source_key=source_key, notices=notices))


def _failed_collector(source_key: str) -> StaticCollector:
    return StaticCollector(
        SourceRunReport(
            source_key=source_key,
            errors=(f"secret response body from {source_key}",),
        )
    )


def _run(workflow: LocalHousingWorkflow, **overrides):
    options = {
        "now": datetime(2026, 8, 28, 12, 0, tzinfo=SEOUL),
        "days": 7,
        "humanize": True,
        "write_articles": True,
    }
    options.update(overrides)
    return workflow.run(**options)


def test_workflow_collects_indexes_humanizes_and_writes_immutable_articles(
    tmp_path: Path,
) -> None:
    title = "Secret fixture title must not enter workflow reports"
    selected = _notice("applyhome-1", title=title)
    index_only = _notice(
        "lh-1",
        source_key="lh",
        category="public_rental",
        checksum_digit="b",
    )
    humanizer = EchoHumanizer()
    workflow = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", selected),
            _collector("lh", index_only),
        ),
        humanizer=humanizer,
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    assert report.window.start.date().isoformat() == "2026-08-22"
    assert report.complete is True
    assert report.blocked is False
    assert report.live_success is False
    assert [phase.name for phase in report.phases] == [
        "collect",
        "merge",
        "select",
        "render",
        "images",
        "humanize",
        "verify",
        "write",
    ]
    assert all(phase.status == "completed" for phase in report.phases)
    run_root = tmp_path / "2026-08-28"
    assert (run_root / "index.md").is_file()
    assert (run_root / "notices.json").is_file()
    article = report.articles[0]
    assert article.humanization_status == "verified"
    assert article.status == "written"
    assert article.article_path is not None
    final_root = tmp_path / article.article_path
    assert (final_root / "article.md").is_file()
    assert (final_root / "article.draft.md").is_file()
    assert (final_root / "humanize" / "input.md").is_file()
    assert (final_root / "humanize" / "output.md").is_file()
    assert (final_root / "humanize" / "events.ndjson").is_file()
    assert (final_root / "humanize" / "verification.json").is_file()
    assert (final_root / "verification.json").is_file()
    assert len(humanizer.documents) == 1
    assert title not in humanizer.documents[0]
    acceptance = (run_root / "acceptance-report.json").read_text(encoding="utf-8")
    assert title not in acceptance
    assert "secret response body" not in acceptance
    assert json.loads(acceptance)["phases"][-1]["name"] == "write"


def test_lock_is_exclusive_and_exists_before_collectors_run(tmp_path: Path) -> None:
    lock_path = tmp_path / ".collect-recent-housing.lock"
    collector = _collector("applyhome")
    collector.lock_path = lock_path
    workflow = LocalHousingWorkflow(
        collectors=(collector,),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    _run(workflow, humanize=False, write_articles=False)

    assert collector.observed_lock is not None
    assert set(collector.observed_lock) == {"pid", "started_at", "workflow_id"}
    assert collector.observed_lock["pid"] == os.getpid()
    assert not lock_path.exists()

    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_at": "2026-08-28T12:00:00+09:00",
                "workflow_id": "active-workflow",
            }
        ),
        encoding="utf-8",
    )
    collector.observed_lock = None
    with pytest.raises(WorkflowLockedError, match="already running"):
        _run(workflow, humanize=False, write_articles=False)
    assert collector.observed_lock is None
    assert lock_path.exists()


@pytest.mark.parametrize("pid_state", ["active", "unknown"])
def test_lock_recovery_refuses_when_owner_absence_is_not_proven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pid_state: str,
) -> None:
    lock_path = tmp_path / ".collect-recent-housing.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 991_337,
                "started_at": "2026-08-28T12:00:00+09:00",
                "workflow_id": "uncertain-workflow",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("apps.local_content.workflow._pid_state", lambda _pid: pid_state)
    collector = _collector("applyhome")
    workflow = LocalHousingWorkflow(
        collectors=(collector,),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    with pytest.raises(WorkflowLockedError):
        _run(workflow, humanize=False, write_articles=False)

    assert lock_path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b'{"pid":991338,"pid":991339,"started_at":"2026-08-28T12:00:00+09:00",'
        b'"workflow_id":"ambiguous"}',
        b'{"pid":991338,"started_at":"2026-08-28T12:00:00",'
        b'"workflow_id":"naive-time"}',
    ],
)
def test_stale_lock_recovery_refuses_malformed_or_ambiguous_ownership_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    lock_path = tmp_path / ".collect-recent-housing.lock"
    lock_path.write_bytes(payload)
    monkeypatch.setattr("apps.local_content.workflow._pid_state", lambda _pid: "absent")
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome"),),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    with pytest.raises(WorkflowLockedError, match="cannot be proven absent"):
        _run(workflow, humanize=False, write_articles=False)

    assert lock_path.read_bytes() == payload


def test_stale_lock_is_recovered_only_after_owner_is_proven_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / ".collect-recent-housing.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 991_338,
                "started_at": "2026-08-28T12:00:00+09:00",
                "workflow_id": "stale-workflow",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("apps.local_content.workflow._pid_state", lambda _pid: "absent")
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome"),),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow, humanize=False, write_articles=False)

    assert report.complete is True
    assert not lock_path.exists()


def test_acceptance_report_is_atomically_replaced_after_every_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    writes: list[tuple[str, ...]] = []
    real_atomic_write = workflow_module._atomic_write_json

    def recording_write(path: Path, payload: dict[str, object]) -> None:
        if "phases" in payload:
            writes.append(
                tuple(
                    phase["name"]
                    for phase in payload["phases"]
                    if phase["status"] != "pending"
                )
            )
        real_atomic_write(path, payload)

    monkeypatch.setattr(workflow_module, "_atomic_write_json", recording_write)
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome"), _collector("lh")),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow, humanize=False, write_articles=False)

    assert report.complete is True
    assert len(writes) == 8
    assert writes == [
        ("collect",),
        ("collect", "merge"),
        ("collect", "merge", "select"),
        ("collect", "merge", "select", "render"),
        ("collect", "merge", "select", "render", "images"),
        ("collect", "merge", "select", "render", "images", "humanize"),
        (
            "collect",
            "merge",
            "select",
            "render",
            "images",
            "humanize",
            "verify",
        ),
        (
            "collect",
            "merge",
            "select",
            "render",
            "images",
            "humanize",
            "verify",
            "write",
        ),
    ]


def test_atomic_workflow_report_rejects_reparse_parent_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    report_root = tmp_path / "report-root"
    report_root.mkdir()
    real_lstat = workflow_module.os.lstat

    def reparse_lstat(path):
        metadata = real_lstat(path)
        if Path(path) != report_root:
            return metadata
        values = {
            name: getattr(metadata, name)
            for name in dir(metadata)
            if name.startswith("st_")
        }
        values["st_file_attributes"] = 0x400
        return SimpleNamespace(**values)

    monkeypatch.setattr(workflow_module.os, "lstat", reparse_lstat)

    with pytest.raises(WorkflowLockedError, match="link or reparse"):
        workflow_module._atomic_write_json(report_root / "report.json", {})

    assert not (report_root / "report.json").exists()


def test_both_source_failures_block_final_index_and_expose_codes_only(tmp_path: Path) -> None:
    workflow = LocalHousingWorkflow(
        collectors=(_failed_collector("applyhome"), _failed_collector("lh")),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    assert report.complete is False
    assert report.blocked is True
    assert "ALL_SOURCES_FAILED" in report.error_codes
    assert not (tmp_path / "2026-08-28" / "index.md").exists()
    acceptance = json.loads(
        (tmp_path / "2026-08-28" / "acceptance-report.json").read_text("utf-8")
    )
    assert acceptance["sources"] == [
        {
            "source_key": "applyhome",
            "status": "failed",
            "notice_count": 0,
            "warning_count": 0,
            "error_codes": ["SOURCE_COLLECTION_FAILED"],
        },
        {
            "source_key": "lh",
            "status": "failed",
            "notice_count": 0,
            "warning_count": 0,
            "error_codes": ["SOURCE_COLLECTION_FAILED"],
        },
    ]
    assert "secret response body" not in json.dumps(acceptance)


def test_one_source_failure_writes_incomplete_index_and_returns_incomplete_report(
    tmp_path: Path,
) -> None:
    workflow = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", _notice("survivor")),
            _failed_collector("lh"),
        ),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    assert report.complete is False
    assert report.blocked is False
    assert "SOURCE_INCOMPLETE" in report.error_codes
    assert (tmp_path / "2026-08-28" / "index.md").is_file()
    assert report.articles[0].status == "written"
    assert "secret response body" not in (
        tmp_path / "2026-08-28" / "index.md"
    ).read_text("utf-8")


def test_humanizer_failure_keeps_labelled_draft_and_never_creates_misleading_link(
    tmp_path: Path,
) -> None:
    first = _notice("blocked", checksum_digit="c")
    second = _notice("verified", checksum_digit="d")
    workflow = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", first, second),
            _collector("lh"),
        ),
        humanizer=FailFirstHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    blocked, verified = report.articles
    assert blocked.external_id == "blocked"
    assert blocked.status == "blocked"
    assert blocked.humanization_status == "failed"
    assert blocked.error_codes == ("HUMANIZE_FAILED",)
    assert blocked.article_path is None
    assert blocked.draft_path is not None
    blocked_root = tmp_path / blocked.draft_path
    assert "draft-blocked" in blocked_root.name
    assert (blocked_root / "article.draft.md").is_file()
    assert (blocked_root / "status.json").is_file()
    assert not (blocked_root / "article.md").exists()
    events = (blocked_root / "humanize" / "events.ndjson").read_text("utf-8").splitlines()
    assert json.loads(events[0]) == {
        "phase": "humanize",
        "status": "failed",
        "type": "workflow",
    }
    assert verified.status == "written"
    assert verified.article_path is not None
    assert (tmp_path / verified.article_path / "article.md").is_file()
    index = (tmp_path / "2026-08-28" / "index.md").read_text("utf-8")
    assert f"./{notice_slug(first)}/article.md" not in index
    assert "[BLOCKED: HUMANIZE_FAILED]" in index
    assert f"./{notice_slug(second)}/article.md" in index
    assert report.complete is False
    assert report.blocked is True
    assert "HUMANIZE_FAILED" in report.error_codes


def test_humanizer_verification_failure_never_publishes_candidate(tmp_path: Path) -> None:
    notice = _notice("tampered", checksum_digit="9")
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome", notice), _collector("lh")),
        humanizer=TamperingHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    article = report.articles[0]
    assert article.error_codes == ("HUMANIZE_VERIFY_FAILED",)
    assert article.article_path is None
    assert article.draft_path is not None
    draft_root = tmp_path / article.draft_path
    assert (draft_root / "humanize" / "input.md").is_file()
    assert (draft_root / "humanize" / "output.md").is_file()
    assert not (draft_root / "article.md").exists()
    index = (tmp_path / "2026-08-28" / "index.md").read_text("utf-8")
    assert "[BLOCKED: HUMANIZE_VERIFY_FAILED]" in index


def test_detail_failure_marker_preserves_draft_and_continues_other_articles(
    tmp_path: Path,
) -> None:
    failed = _notice(
        "detail-failed",
        checksum_digit="7",
        warnings=("DETAIL_COLLECTION_FAILED",),
    )
    healthy = _notice("detail-healthy", checksum_digit="8")
    humanizer = EchoHumanizer()
    workflow = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", failed, healthy),
            _collector("lh"),
        ),
        humanizer=humanizer,
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    blocked, written = report.articles
    assert blocked.external_id == "detail-failed"
    assert blocked.error_codes == ("DETAIL_FAILED",)
    assert blocked.draft_path is not None
    assert (tmp_path / blocked.draft_path / "article.draft.md").is_file()
    assert not (tmp_path / blocked.draft_path / "article.md").exists()
    assert written.status == "written"
    assert len(humanizer.documents) == 1


def test_image_failure_blocks_only_affected_article_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    first = _notice("bad-image", checksum_digit="e")
    second = _notice("good-image", checksum_digit="f")
    real_build_image_set = workflow_module.build_image_set

    def selective_image_builder(notice: HousingNotice, output_dir: Path):
        if notice.external_id == "bad-image":
            raise RuntimeError("sensitive pillow detail")
        return real_build_image_set(notice, output_dir)

    monkeypatch.setattr(workflow_module, "build_image_set", selective_image_builder)
    workflow = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", first, second),
            _collector("lh"),
        ),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    assert report.articles[0].error_codes == ("IMAGE_FAILED",)
    assert report.articles[0].article_path is None
    assert report.articles[0].draft_path is not None
    failed_root = tmp_path / report.articles[0].draft_path
    assert (failed_root / "article.draft.md").is_file()
    assert not (failed_root / "article.md").exists()
    assert report.articles[1].status == "written"
    assert "IMAGE_FAILED" in report.error_codes
    assert "sensitive pillow detail" not in json.dumps(report.as_dict())


def test_dry_run_leaves_no_files_and_cannot_report_live_success(tmp_path: Path) -> None:
    output_root = tmp_path / "not-created"
    humanizer = EchoHumanizer()
    workflow = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", _notice("dry-run")),
            _collector("lh"),
        ),
        humanizer=humanizer,
        output_root=output_root,
        mode="live",
        dry_run=True,
    )

    report = _run(workflow)

    assert report.mode == "dry_run"
    assert report.live_success is False
    assert report.report_path is None
    assert not output_root.exists()
    assert humanizer.documents == []


def test_management_command_declares_exact_defaults_and_boolean_flags() -> None:
    parser = Command().create_parser("manage.py", "collect_recent_housing")

    defaults = vars(parser.parse_args([]))
    disabled = vars(parser.parse_args(["--no-humanize", "--no-write-articles"]))

    assert defaults["days"] == 7
    assert defaults["humanize"] is True
    assert defaults["write_articles"] is True
    assert defaults["selected_id"] == []
    assert defaults["fixture_root"] is None
    assert defaults["dry_run"] is False
    assert disabled["humanize"] is False
    assert disabled["write_articles"] is False


def test_management_command_raises_command_error_for_incomplete_or_blocked_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(
        complete=False,
        blocked=True,
        live_success=False,
        mode="fixture",
        workflow_id="workflow-1",
        error_codes=("ALL_SOURCES_FAILED",),
        article_count=0,
        written_count=0,
        run_path=None,
        report_path=None,
    )
    fake_workflow = SimpleNamespace(run=lambda **_options: result)
    monkeypatch.setattr(command_module, "_build_workflow", lambda **_options: fake_workflow)

    with pytest.raises(CommandError, match="ALL_SOURCES_FAILED"):
        Command().handle(
            days=7,
            humanize=True,
            write_articles=True,
            selected_id=[],
            fixture_root=None,
            dry_run=False,
        )


def test_fixture_collectors_use_the_checked_in_local_content_assembly() -> None:
    fixture_root = Path(__file__).parents[1] / "fixtures" / "local-content"

    collectors = command_module._fixture_collectors(fixture_root)
    workflow = LocalHousingWorkflow(
        collectors=collectors,
        humanizer=EchoHumanizer(),
        output_root=fixture_root / "must-not-be-written",
        mode="fixture",
        dry_run=True,
    )
    report = _run(workflow, selected_ids=("lh:02:0000061158:05:05",))

    assert [source.source_key for source in report.sources] == ["applyhome", "lh"]
    assert all(source.status == "complete" for source in report.sources)
    assert report.mode == "dry_run"
    assert not (fixture_root / "must-not-be-written").exists()
