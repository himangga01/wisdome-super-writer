from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.management.base import CommandError

from apps.local_content.bundles import (
    ArticleBundle,
    ArticleBundleWriter,
    BundleFile,
    BundlePublishError,
)
from apps.local_content.contracts import HousingNotice, SourceRunReport
from apps.local_content.dates import SEOUL
from apps.local_content.management.commands import collect_recent_housing as command_module
from apps.local_content.management.commands.collect_recent_housing import Command
from apps.local_content.rendering import notice_slug
from apps.local_content.workflow import (
    LocalHousingWorkflow,
    WorkflowError,
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


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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


def test_same_date_rerun_publishes_immutable_revision_without_touching_prior_run(
    tmp_path: Path,
) -> None:
    notice = _notice("same-date", checksum_digit="1")
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome", notice), _collector("lh")),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        state_root=tmp_path / "state",
        mode="fixture",
    )

    first = _run(workflow)
    first_root = tmp_path / first.run_path
    before = _tree_bytes(first_root)
    second = _run(workflow)

    assert first.run_path == "2026-08-28"
    assert second.run_path is not None
    assert second.run_path.startswith("2026-08-28--run-")
    assert second.report_path == f"{second.run_path}/acceptance-report.json"
    assert _tree_bytes(first_root) == before
    assert (tmp_path / second.run_path / "index.md").is_file()
    assert (tmp_path / second.report_path).is_file()


def test_failed_same_date_rerun_keeps_prior_run_byte_identical(tmp_path: Path) -> None:
    notice = _notice("same-date-failure", checksum_digit="2")
    common = {
        "collectors": (_collector("applyhome", notice), _collector("lh")),
        "output_root": tmp_path,
        "state_root": tmp_path / "state",
        "mode": "fixture",
    }
    first = _run(LocalHousingWorkflow(humanizer=EchoHumanizer(), **common))
    first_root = tmp_path / first.run_path
    before = _tree_bytes(first_root)

    second = _run(LocalHousingWorkflow(humanizer=FailFirstHumanizer(), **common))

    assert second.complete is False
    assert second.run_path is not None
    assert second.run_path.startswith("2026-08-28--run-")
    assert _tree_bytes(first_root) == before
    assert not list((tmp_path / second.run_path).glob("*/article.md"))
    assert (tmp_path / second.report_path).is_file()


def test_existing_partial_date_run_forces_new_revision_without_overwrite(
    tmp_path: Path,
) -> None:
    failed = LocalHousingWorkflow(
        collectors=(_failed_collector("applyhome"), _failed_collector("lh")),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        state_root=tmp_path / "state",
        mode="fixture",
    )
    first = _run(failed)
    partial_root = tmp_path / first.run_path
    before = _tree_bytes(partial_root)
    assert not (partial_root / "index.md").exists()

    healthy = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", _notice("after-partial", checksum_digit="c")),
            _collector("lh"),
        ),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        state_root=tmp_path / "state",
        mode="fixture",
    )
    second = _run(healthy)

    assert second.run_path is not None
    assert second.run_path.startswith("2026-08-28--run-")
    assert _tree_bytes(partial_root) == before
    assert (tmp_path / second.run_path / "index.md").is_file()


def test_existing_revision_alone_prevents_reuse_of_primary_date_name(tmp_path: Path) -> None:
    prior_revision = tmp_path / "2026-08-28--run-000000000000"
    prior_revision.mkdir()
    marker = prior_revision / "partial.marker"
    marker.write_bytes(b"partial")
    workflow = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", _notice("after-orphan-revision", checksum_digit="e")),
            _collector("lh"),
        ),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        state_root=tmp_path / "state",
        mode="fixture",
    )

    report = _run(workflow)

    assert report.run_path is not None
    assert report.run_path.startswith("2026-08-28--run-")
    assert report.run_path != prior_revision.name
    assert marker.read_bytes() == b"partial"
    assert not (tmp_path / "2026-08-28").exists()


def test_crash_between_run_files_never_changes_prior_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice = _notice("same-date-crash", checksum_digit="3")
    common = {
        "collectors": (_collector("applyhome", notice), _collector("lh")),
        "humanizer": EchoHumanizer(),
        "output_root": tmp_path,
        "state_root": tmp_path / "state",
        "mode": "fixture",
    }
    first = _run(LocalHousingWorkflow(**common))
    first_root = tmp_path / first.run_path
    before = _tree_bytes(first_root)

    def fail_run_commit(*_args, **_kwargs):
        raise OSError("sensitive crash body")

    monkeypatch.setattr(ArticleBundleWriter, "write_run", fail_run_commit)
    second = _run(LocalHousingWorkflow(**common))

    assert "RUN_COMMIT_FAILED" in second.error_codes
    assert second.run_path is not None
    assert second.run_path.startswith("2026-08-28--run-")
    assert _tree_bytes(first_root) == before
    assert "sensitive crash body" not in json.dumps(second.as_dict())


def test_validated_prior_manual_detail_promotes_explicit_correction(
    tmp_path: Path,
) -> None:
    external_id = "prior-manual-rental"
    original = _notice(
        external_id,
        category="purchase_lease",
        checksum_digit="4",
    )
    common = {
        "humanizer": EchoHumanizer(),
        "output_root": tmp_path,
        "state_root": tmp_path / "state",
        "mode": "fixture",
    }
    first = _run(
        LocalHousingWorkflow(
            collectors=(_collector("applyhome", original), _collector("lh")),
            **common,
        ),
        selected_ids=(external_id,),
    )
    assert first.articles[0].status == "written"

    correction = replace(
        original,
        status="corrected",
        source_checksum="5" * 64,
    )
    second = _run(
        LocalHousingWorkflow(
            collectors=(_collector("applyhome", correction), _collector("lh")),
            **common,
        )
    )

    assert second.run_path is not None
    assert second.run_path.startswith("2026-08-28--run-")
    assert [article.external_id for article in second.articles] == [external_id]
    assert second.articles[0].status == "written"


def test_unvalidated_prior_files_cannot_promote_correction(tmp_path: Path) -> None:
    forged = tmp_path / "2026-08-28" / "forged"
    forged.mkdir(parents=True)
    (forged / "verification.json").write_text(
        json.dumps(
            {
                "status": "verified",
                "source_key": "applyhome",
                "external_id": "forged-prior",
                "source_checksum": "f" * 64,
                "humanization_status": "verified",
            }
        ),
        encoding="utf-8",
    )
    correction = replace(
        _notice(
            "forged-prior",
            category="purchase_lease",
            checksum_digit="d",
        ),
        status="corrected",
    )
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome", correction), _collector("lh")),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        state_root=tmp_path / "state",
        mode="fixture",
    )

    report = _run(workflow)

    assert report.articles == ()


def test_later_date_validated_manual_detail_promotes_correction(tmp_path: Path) -> None:
    external_id = "later-date-prior"
    original = _notice(
        external_id,
        category="purchase_lease",
        checksum_digit="4",
    )
    common = {
        "humanizer": EchoHumanizer(),
        "output_root": tmp_path,
        "state_root": tmp_path / "state",
        "mode": "fixture",
    }
    first = _run(
        LocalHousingWorkflow(
            collectors=(_collector("applyhome", original), _collector("lh")),
            **common,
        ),
        now=datetime(2026, 8, 27, 12, 0, tzinfo=SEOUL),
        selected_ids=(external_id,),
    )
    assert first.run_path == "2026-08-27"

    correction = replace(
        original,
        status="corrected",
        source_checksum="5" * 64,
    )
    second = _run(
        LocalHousingWorkflow(
            collectors=(_collector("applyhome", correction), _collector("lh")),
            **common,
        ),
        now=datetime(2026, 8, 28, 12, 0, tzinfo=SEOUL),
    )

    assert [article.external_id for article in second.articles] == [external_id]
    assert second.articles[0].status == "written"


def test_validated_draft_only_bundle_cannot_promote_correction(tmp_path: Path) -> None:
    external_id = "draft-only-prior"
    verification = json.dumps(
        {
            "status": "verified",
            "source_key": "applyhome",
            "external_id": external_id,
            "source_checksum": "6" * 64,
            "humanization_status": "verified",
        }
    )
    forged = ArticleBundle(
        run_date=datetime(2026, 8, 28, tzinfo=SEOUL).date(),
        slug="forged-draft-only",
        files=(
            BundleFile.text("article.draft.md", "# draft\n", "text/markdown"),
            BundleFile.text("verification.json", verification, "application/json"),
        ),
    )
    ArticleBundleWriter(tmp_path).write_run(
        forged.run_date,
        "# prior\n",
        (forged,),
    )
    correction = replace(
        _notice(
            external_id,
            category="purchase_lease",
            checksum_digit="7",
        ),
        status="corrected",
    )
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome", correction), _collector("lh")),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        state_root=tmp_path / "state",
        mode="fixture",
    )

    report = _run(workflow)

    assert report.articles == ()


def test_advisory_guard_is_held_before_collect_and_persists_after_release(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    state_root = tmp_path / "state"
    lock_path = state_root / "locks" / (
        hashlib.sha256(
            os.path.normcase(os.path.realpath(output_root)).encode("utf-8")
        ).hexdigest()
        + ".guard"
    )
    observed_guard: list[bool] = []

    class GuardObservingCollector:
        source_key = "applyhome"

        def collect(self, _window):
            observed_guard.append(lock_path.is_file())
            return SourceRunReport(source_key=self.source_key)

    workflow = LocalHousingWorkflow(
        collectors=(GuardObservingCollector(),),
        humanizer=EchoHumanizer(),
        output_root=output_root,
        state_root=state_root,
        mode="fixture",
    )

    _run(workflow, humanize=False, write_articles=False)

    metadata = json.loads(lock_path.read_text("utf-8"))
    assert observed_guard == [True]
    assert set(metadata) == {"pid", "started_at", "workflow_id"}
    assert lock_path.exists()
    assert not (output_root / ".collect-recent-housing.lock").exists()


def test_simultaneous_workflows_have_exactly_one_advisory_lock_owner(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingCollector:
        source_key = "applyhome"

        def collect(self, _window):
            entered.set()
            assert release.wait(5)
            return SourceRunReport(source_key=self.source_key)

    options = {
        "collectors": (BlockingCollector(),),
        "humanizer": EchoHumanizer(),
        "output_root": tmp_path / "output",
        "state_root": tmp_path / "state",
        "mode": "fixture",
    }
    first = LocalHousingWorkflow(**options)
    second = LocalHousingWorkflow(**options)
    third = LocalHousingWorkflow(**options)
    outcome: list[object] = []

    thread = threading.Thread(
        target=lambda: outcome.append(
            _run(first, humanize=False, write_articles=False)
        )
    )
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(WorkflowLockedError, match="WORKFLOW_ALREADY_RUNNING"):
            _run(second, humanize=False, write_articles=False)
        with pytest.raises(WorkflowLockedError, match="WORKFLOW_ALREADY_RUNNING"):
            _run(third, humanize=False, write_articles=False)
    finally:
        release.set()
        thread.join(5)

    assert not thread.is_alive()
    assert len(outcome) == 1


def test_reparse_guard_fails_closed_before_collector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    output_root = tmp_path / "output"
    state_root = tmp_path / "state"
    guard_root = state_root / "locks"
    guard_root.mkdir(parents=True)
    guard_path = guard_root / (
        hashlib.sha256(
            os.path.normcase(os.path.realpath(output_root)).encode("utf-8")
        ).hexdigest()
        + ".guard"
    )
    guard_path.write_bytes(b"guard")
    collector = _collector("applyhome")
    real_lstat = workflow_module.os.lstat

    def reparse_lstat(path):
        metadata = real_lstat(path)
        if Path(path) != guard_path:
            return metadata
        values = {
            name: getattr(metadata, name)
            for name in dir(metadata)
            if name.startswith("st_")
        }
        values["st_file_attributes"] = 0x400
        return SimpleNamespace(**values)

    monkeypatch.setattr(workflow_module.os, "lstat", reparse_lstat)
    workflow = LocalHousingWorkflow(
        collectors=(collector,),
        humanizer=EchoHumanizer(),
        output_root=output_root,
        state_root=state_root,
        mode="fixture",
    )

    with pytest.raises(WorkflowError, match="WORKFLOW_GUARD_UNSAFE"):
        _run(workflow, humanize=False, write_articles=False)

    assert collector.observed_lock is None


def test_hardlinked_guard_is_rejected_without_mutating_link_target(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    state_root = tmp_path / "state"
    guard_root = state_root / "locks"
    guard_root.mkdir(parents=True)
    guard_path = guard_root / (
        hashlib.sha256(
            os.path.normcase(os.path.realpath(output_root)).encode("utf-8")
        ).hexdigest()
        + ".guard"
    )
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"must-remain-unchanged")
    os.link(outside, guard_path)
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome"),),
        humanizer=EchoHumanizer(),
        output_root=output_root,
        state_root=state_root,
        mode="fixture",
    )

    with pytest.raises(WorkflowError, match="WORKFLOW_GUARD_UNSAFE"):
        _run(workflow, humanize=False, write_articles=False)

    assert outside.read_bytes() == b"must-remain-unchanged"


def test_noncontention_lock_error_is_not_mislabeled_as_active_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    if workflow_module.os.name != "nt":
        pytest.skip("Windows msvcrt classification regression")

    def fail_lock(_descriptor: int, mode: int, _length: int) -> None:
        if mode == workflow_module.msvcrt.LK_NBLCK:
            raise OSError(errno.EIO, "sensitive device body")

    monkeypatch.setattr(workflow_module.msvcrt, "locking", fail_lock)
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome"),),
        humanizer=EchoHumanizer(),
        output_root=tmp_path / "output",
        state_root=tmp_path / "state",
        mode="fixture",
    )

    with pytest.raises(WorkflowError, match="WORKFLOW_LOCK_FAILED") as raised:
        _run(workflow, humanize=False, write_articles=False)

    assert "sensitive device body" not in str(raised.value)


def test_unlock_failure_returns_stable_workflow_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    if workflow_module.os.name != "nt":
        pytest.skip("Windows msvcrt unlock regression")
    real_locking = workflow_module.msvcrt.locking

    def fail_unlock(descriptor: int, mode: int, length: int) -> None:
        if mode == workflow_module.msvcrt.LK_UNLCK:
            raise OSError(errno.EIO, "sensitive unlock body")
        real_locking(descriptor, mode, length)

    monkeypatch.setattr(workflow_module.msvcrt, "locking", fail_unlock)
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome"),),
        humanizer=EchoHumanizer(),
        output_root=tmp_path / "output",
        state_root=tmp_path / "state",
        mode="fixture",
    )

    with pytest.raises(WorkflowError, match="WORKFLOW_UNLOCK_FAILED") as raised:
        _run(workflow, humanize=False, write_articles=False)

    assert "sensitive unlock body" not in str(raised.value)


def test_guard_close_failure_returns_stable_workflow_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    guard = workflow_module._WorkflowGuard(
        tmp_path / "output",
        tmp_path / "state",
        "close-failure",
        datetime(2026, 8, 28, 12, 0, tzinfo=SEOUL),
    )
    guard.acquire()
    descriptor = guard.descriptor
    assert descriptor is not None
    real_close = workflow_module.os.close

    def fail_owned_close(value: int) -> None:
        if value == descriptor:
            raise OSError(errno.EIO, "sensitive close body")
        real_close(value)

    monkeypatch.setattr(workflow_module.os, "close", fail_owned_close)
    try:
        with pytest.raises(WorkflowError, match="WORKFLOW_GUARD_CLOSE_FAILED") as raised:
            guard.release()
        assert "sensitive close body" not in str(raised.value)
    finally:
        monkeypatch.setattr(workflow_module.os, "close", real_close)
        if guard.descriptor is not None:
            real_close(guard.descriptor)
            guard.descriptor = None


def test_output_root_identity_swap_fails_before_collector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    output_root = tmp_path / "output"
    state_root = tmp_path / "state"
    collector = _collector("applyhome")
    real_identity = workflow_module._directory_identity
    output_calls = 0

    def changing_identity(path: Path, label: str):
        nonlocal output_calls
        identity = real_identity(path, label)
        if Path(path) == Path(os.path.normcase(os.path.realpath(output_root))):
            output_calls += 1
            if output_calls >= 3:
                return (*identity, "substituted")
        return identity

    monkeypatch.setattr(workflow_module, "_directory_identity", changing_identity)
    workflow = LocalHousingWorkflow(
        collectors=(collector,),
        humanizer=EchoHumanizer(),
        output_root=output_root,
        state_root=state_root,
        mode="fixture",
    )

    with pytest.raises(WorkflowError, match="OUTPUT_ROOT_CHANGED"):
        _run(workflow, humanize=False, write_articles=False)

    assert collector.observed_lock is None


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

    with pytest.raises(WorkflowError, match="PATH_LINK_OR_REPARSE_UNSAFE"):
        workflow_module._atomic_write_json(report_root / "report.json", {})

    assert not (report_root / "report.json").exists()


def test_atomic_report_open_and_replace_failures_use_body_free_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    target = tmp_path / "report.json"
    real_open = workflow_module.os.open

    def fail_temp_open(path, flags, *args):
        if str(path).endswith(".tmp"):
            raise OSError(errno.EIO, "sensitive report path body")
        return real_open(path, flags, *args)

    monkeypatch.setattr(workflow_module.os, "open", fail_temp_open)
    with pytest.raises(WorkflowError, match="REPORT_OPEN_FAILED") as opened:
        workflow_module._atomic_write_json(target, {})
    assert "sensitive report path body" not in str(opened.value)

    monkeypatch.setattr(workflow_module.os, "open", real_open)

    def fail_replace(_source, _target):
        raise OSError(errno.EIO, "sensitive replace path body")

    monkeypatch.setattr(workflow_module.os, "replace", fail_replace)
    with pytest.raises(WorkflowError, match="REPORT_REPLACE_FAILED") as replaced:
        workflow_module._atomic_write_json(target, {})
    assert "sensitive replace path body" not in str(replaced.value)


def test_report_path_inspection_oserror_is_stable_and_body_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    real_lstat = workflow_module.os.lstat
    failed = False

    def fail_parent(path):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError(errno.EIO, "sensitive inspected path")
        return real_lstat(path)

    monkeypatch.setattr(workflow_module.os, "lstat", fail_parent)
    monkeypatch.setattr(workflow_module.os.path, "lexists", lambda _path: True)
    with pytest.raises(WorkflowError, match="WORKFLOW_PATH_INSPECTION_FAILED") as raised:
        workflow_module._atomic_write_json(tmp_path / "report.json", {})

    assert "sensitive inspected path" not in str(raised.value)


def test_directory_fsync_propagates_io_and_allows_exact_unsupported_errno(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    probe = tmp_path / "directory-fsync-probe"
    probe.write_bytes(b"probe")
    real_open = workflow_module.os.open
    probe_descriptor = real_open(probe, os.O_RDONLY)

    def duplicate_probe(_path, _flags):
        return os.dup(probe_descriptor)

    monkeypatch.setattr(workflow_module.os, "open", duplicate_probe)

    def fail_io(_descriptor: int) -> None:
        raise OSError(errno.EIO, "sensitive fsync path body")

    monkeypatch.setattr(workflow_module.os, "fsync", fail_io)
    with pytest.raises(WorkflowError, match="REPORT_DIRECTORY_FSYNC_FAILED") as raised:
        workflow_module._fsync_directory(tmp_path)
    assert "sensitive fsync path body" not in str(raised.value)

    def unsupported(_descriptor: int) -> None:
        raise OSError(errno.EINVAL, "unsupported")

    monkeypatch.setattr(workflow_module.os, "fsync", unsupported)
    try:
        workflow_module._fsync_directory(tmp_path)
    finally:
        os.close(probe_descriptor)


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


def test_unselected_detail_failure_makes_source_and_run_incomplete_without_body(
    tmp_path: Path,
) -> None:
    affected = _notice(
        "unselected-rental-detail-failure",
        category="purchase_lease",
        checksum_digit="6",
        warnings=("DETAIL_COLLECTION_FAILED",),
    )
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome", affected), _collector("lh")),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    assert report.articles == ()
    assert report.complete is False
    assert report.blocked is False
    assert "DETAIL_INCOMPLETE" in report.error_codes
    applyhome = next(source for source in report.sources if source.source_key == "applyhome")
    assert applyhome.status == "incomplete"
    assert applyhome.error_codes == ("DETAIL_COLLECTION_FAILED",)
    run_root = tmp_path / report.run_path
    notices = json.loads((run_root / "notices.json").read_text("utf-8"))
    assert notices["complete"] is False
    row = notices["notices"][0]
    assert row["external_id"] == affected.external_id
    assert row["error_codes"] == ["DETAIL_COLLECTION_FAILED"]
    index = (run_root / "index.md").read_text("utf-8")
    assert "complete: false" in index
    assert affected.external_id in index
    assert "DETAIL_COLLECTION_FAILED" in index
    assert "sensitive" not in json.dumps(report.as_dict())


@pytest.mark.parametrize("kind", ["excluded", "conflicting"])
def test_raw_detail_failures_survive_merge_exclusion_and_conflict_status(
    tmp_path: Path,
    kind: str,
) -> None:
    affected = _notice(
        f"raw-detail-{kind}",
        category=("non_residential_auction" if kind == "excluded" else "apt"),
        checksum_digit="2",
        warnings=("DETAIL_COLLECTION_FAILED",),
    )
    notices = (affected,)
    if kind == "conflicting":
        notices = (affected, replace(affected, source_checksum="3" * 64))
    workflow = LocalHousingWorkflow(
        collectors=(_collector("applyhome", *notices), _collector("lh")),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    assert report.complete is False
    assert "DETAIL_INCOMPLETE" in report.error_codes
    run_root = tmp_path / report.run_path
    metadata = json.loads((run_root / "notices.json").read_text("utf-8"))
    assert metadata["complete"] is False
    assert metadata["detail_failures"] == [
        {
            "source_key": "applyhome",
            "external_id": affected.external_id,
            "error_codes": ["DETAIL_COLLECTION_FAILED"],
        }
    ]
    index = (run_root / "index.md").read_text("utf-8")
    assert affected.external_id in index
    assert "DETAIL_COLLECTION_FAILED" in index


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
    assert blocked.error_codes == ("HUMANIZE_TRANSFORM_FAILED",)
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
    assert "[BLOCKED: HUMANIZE_TRANSFORM_FAILED]" in index
    assert f"./{notice_slug(second)}/article.md" in index
    assert report.complete is False
    assert report.blocked is True
    assert "HUMANIZE_TRANSFORM_FAILED" in report.error_codes


def test_protection_failure_writes_body_free_humanize_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    def fail_protection(*_args, **_kwargs):
        raise ValueError("sensitive prose body")

    monkeypatch.setattr(workflow_module, "protect_article_prose", fail_protection)
    workflow = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", _notice("protect-failure", checksum_digit="a")),
            _collector("lh"),
        ),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    article = report.articles[0]
    assert article.error_codes == ("HUMANIZE_PROTECTION_FAILED",)
    assert article.draft_path is not None
    draft_root = tmp_path / article.draft_path
    assert (draft_root / "humanize" / "events.ndjson").is_file()
    assert (draft_root / "humanize" / "verification.json").is_file()
    assert not (draft_root / "humanize" / "input.md").exists()
    diagnostics = (
        draft_root / "humanize" / "verification.json"
    ).read_text("utf-8")
    assert "sensitive prose body" not in diagnostics
    assert "HUMANIZE_PROTECTION_FAILED" in diagnostics


def test_no_humanize_never_creates_final_article(tmp_path: Path) -> None:
    workflow = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", _notice("not-humanized", checksum_digit="b")),
            _collector("lh"),
        ),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow, humanize=False)

    article = report.articles[0]
    assert report.complete is False
    assert report.blocked is True
    assert article.humanization_status == "disabled"
    assert article.error_codes == ("HUMANIZATION_DISABLED",)
    assert article.article_path is None
    assert article.draft_path is not None
    draft_root = tmp_path / article.draft_path
    assert (draft_root / "article.draft.md").is_file()
    assert not (draft_root / "article.md").exists()
    assert (draft_root / "humanize" / "events.ndjson").is_file()
    assert (draft_root / "humanize" / "verification.json").is_file()


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


def test_final_bundle_assembly_failure_is_not_mislabeled_as_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import workflow as workflow_module

    def fail_assembly(*_args, **_kwargs):
        raise ValueError("sensitive bundle material")

    monkeypatch.setattr(workflow_module, "_final_bundle", fail_assembly)
    workflow = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", _notice("bundle-assembly", checksum_digit="1")),
            _collector("lh"),
        ),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    article = report.articles[0]
    assert article.error_codes == ("BUNDLE_ASSEMBLY_FAILED",)
    assert "HUMANIZE_VERIFY_FAILED" not in report.error_codes
    assert article.article_path is None
    assert article.draft_path is not None


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


def test_article_storage_failure_does_not_stop_healthy_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_notice = _notice("storage-bad", checksum_digit="8")
    healthy_notice = _notice("storage-good", checksum_digit="9")
    failed_slug = notice_slug(failed_notice)
    real_write = ArticleBundleWriter.write

    def fail_one_article(self, bundle, *, run_directory=None):
        if bundle.slug == failed_slug:
            raise BundlePublishError("sensitive storage path body")
        return real_write(self, bundle, run_directory=run_directory)

    monkeypatch.setattr(ArticleBundleWriter, "write", fail_one_article)
    workflow = LocalHousingWorkflow(
        collectors=(
            _collector("applyhome", failed_notice, healthy_notice),
            _collector("lh"),
        ),
        humanizer=EchoHumanizer(),
        output_root=tmp_path,
        mode="fixture",
    )

    report = _run(workflow)

    failed, healthy = report.articles
    assert failed.external_id == failed_notice.external_id
    assert "BUNDLE_WRITE_FAILED" in failed.error_codes
    assert failed.article_path is None
    assert healthy.status == "written"
    assert healthy.article_path is not None
    assert (tmp_path / healthy.article_path / "article.md").is_file()
    index = (tmp_path / report.run_path / "index.md").read_text("utf-8")
    assert f"./{notice_slug(healthy_notice)}/article.md" in index
    assert f"./{failed_slug}/article.md" not in index
    assert report.phases[-1].status == "completed_with_errors"
    assert report.blocked is True
    assert "sensitive storage path body" not in json.dumps(report.as_dict())


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


def test_management_command_maps_workflow_errors_to_stable_command_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_build(**_options):
        raise WorkflowError("WORKFLOW_GUARD_UNSAFE")

    monkeypatch.setattr(command_module, "_build_workflow", fail_build)

    with pytest.raises(CommandError, match="WORKFLOW_GUARD_UNSAFE") as raised:
        Command().handle(
            days=7,
            humanize=True,
            write_articles=True,
            selected_id=[],
            fixture_root=None,
            dry_run=False,
        )

    assert "traceback" not in str(raised.value).casefold()


def test_fixture_collectors_use_the_checked_in_local_content_assembly(tmp_path: Path) -> None:
    fixture_root = Path(__file__).parents[1] / "fixtures" / "local-content"

    collectors = command_module._fixture_collectors(fixture_root)
    workflow = LocalHousingWorkflow(
        collectors=collectors,
        humanizer=EchoHumanizer(),
        output_root=fixture_root / "must-not-be-written",
        state_root=tmp_path / "state",
        mode="fixture",
        dry_run=True,
    )
    report = _run(workflow, selected_ids=("lh:02:0000061158:05:05",))

    assert [source.source_key for source in report.sources] == ["applyhome", "lh"]
    assert all(source.status == "complete" for source in report.sources)
    assert report.mode == "dry_run"
    assert not (fixture_root / "must-not-be-written").exists()


def test_fixture_mode_rejects_arbitrary_same_named_directory(tmp_path: Path) -> None:
    approved = Path(__file__).parents[1] / "fixtures"
    arbitrary = tmp_path / "local-content"
    arbitrary.mkdir()
    shutil.copytree(approved / "applyhome", tmp_path / "applyhome")
    shutil.copytree(approved / "lh", tmp_path / "lh")

    with pytest.raises(WorkflowError, match="FIXTURE_ROOT_UNAPPROVED"):
        command_module._fixture_collectors(arbitrary)


def test_fixture_manifest_rejects_tampered_file_bytes(tmp_path: Path) -> None:
    approved = Path(__file__).parents[1] / "fixtures"
    fixture_root = tmp_path / "local-content"
    fixture_root.mkdir()
    (fixture_root / "README.md").write_text("fixture", encoding="utf-8")
    shutil.copytree(approved / "applyhome", tmp_path / "applyhome")
    shutil.copytree(approved / "lh", tmp_path / "lh")
    relative_files = (
        "applyhome/apt-list.html",
        "applyhome/apt-list-page-2.html",
        "applyhome/remaining-list.html",
        "applyhome/apt-detail.html",
        "lh/notice-list-page-1.html",
        "lh/notice-list-page-2.html",
        "lh/notice-detail.html",
    )
    manifest = {
        "schema_version": 1,
        "files": {
            relative: hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
            for relative in relative_files
        },
    }
    (fixture_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "lh" / "notice-detail.html"
    target.write_bytes(target.read_bytes() + b"tampered")

    with pytest.raises(WorkflowError, match="FIXTURE_CHECKSUM_MISMATCH"):
        command_module._verify_fixture_manifest(
            fixture_root,
            approved_root=fixture_root,
        )


def _isolated_fixture_assembly(tmp_path: Path) -> Path:
    approved = Path(__file__).parents[1] / "fixtures"
    fixture_root = tmp_path / "local-content"
    fixture_root.mkdir()
    shutil.copytree(approved / "applyhome", tmp_path / "applyhome")
    shutil.copytree(approved / "lh", tmp_path / "lh")
    shutil.copy2(approved / "local-content" / "README.md", fixture_root / "README.md")
    shutil.copy2(approved / "local-content" / "manifest.json", fixture_root / "manifest.json")
    return fixture_root


def test_fixture_manifest_rejects_extra_files_in_closed_assembly(tmp_path: Path) -> None:
    fixture_root = _isolated_fixture_assembly(tmp_path)
    (tmp_path / "lh" / "unexpected.html").write_text("extra", encoding="utf-8")

    with pytest.raises(WorkflowError, match="FIXTURE_EXTRA_FILE"):
        command_module._verify_fixture_manifest(
            fixture_root,
            approved_root=fixture_root,
        )


def test_fixture_fetcher_serves_one_verified_in_memory_snapshot(tmp_path: Path) -> None:
    fixture_root = _isolated_fixture_assembly(tmp_path)
    snapshot = command_module._verify_fixture_manifest(
        fixture_root,
        approved_root=fixture_root,
    )
    fetcher = command_module._FixtureFetcher(fixture_root, snapshot)
    url = f"{command_module.APT_LIST}?pageIndex=1"
    before = fetcher.get(url).body
    source = tmp_path / "applyhome" / "apt-list.html"
    source.write_text("tampered after verification", encoding="utf-8")

    assert fetcher.get(url).body == before


def test_fixture_read_oserror_is_stable_and_body_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = _isolated_fixture_assembly(tmp_path)

    def fail_read(_descriptor: int, _size: int) -> bytes:
        raise OSError(errno.EIO, "sensitive fixture path body")

    monkeypatch.setattr(command_module.os, "read", fail_read)
    with pytest.raises(WorkflowError, match="FIXTURE_READ_FAILED") as raised:
        command_module._verify_fixture_manifest(
            fixture_root,
            approved_root=fixture_root,
        )

    assert "sensitive fixture path body" not in str(raised.value)
