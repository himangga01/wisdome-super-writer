from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import shutil
import stat
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

import apps.local_content.bundles as bundle_module
import apps.local_content.images as image_module
from apps.local_content.bundles import (
    ArticleBundle,
    ArticleBundleWriter,
    BundleFile,
    BundlePublishError,
    BundleValidationError,
    build_article_bundle,
)
from apps.local_content.contracts import HousingNotice
from apps.local_content.images import build_image_set
from apps.local_content.rendering import render_detailed_article

SEOUL = ZoneInfo("Asia/Seoul")


def _notice(**overrides: object) -> HousingNotice:
    fields: dict[str, object] = {
        "source_key": "applyhome",
        "external_id": "notice-1",
        "canonical_url": "https://www.applyhome.co.kr/notice/1",
        "title": "서울 해오름 A단지 주택공급",
        "publisher": "청약홈",
        "category": "apt",
        "region": "서울특별시",
        "status": "공고중",
        "published_at": datetime(2026, 8, 28, 9, tzinfo=SEOUL),
        "source_checksum": "a" * 64,
    }
    fields.update(overrides)
    return HousingNotice(**fields)  # type: ignore[arg-type]


def _bundle(tmp_path: Path, **notice_overrides: object) -> ArticleBundle:
    notice = _notice(**notice_overrides)
    rendered = render_detailed_article(notice)
    images = build_image_set(notice, tmp_path / "rendered-images")
    return build_article_bundle(date(2026, 8, 28), rendered, images)


def _snapshot_tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _encoded_image(format_name: str, size: tuple[int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, format=format_name, quality=82, method=6)
    return output.getvalue()


def _declared_image_file(
    tmp_path: Path,
    *,
    bundle_path: str,
    payload: bytes,
    mime_type: str,
    width: int,
    height: int,
) -> BundleFile:
    template = build_image_set(_notice(), tmp_path / "metadata-template").images[1]
    checksum = hashlib.sha256(payload).hexdigest()
    metadata = replace(
        template,
        path=tmp_path / "in-memory-not-used",
        bundle_path=bundle_path,
        sha256=checksum,
        mime_type=mime_type,
        width=width,
        height=height,
    )
    return BundleFile(
        path=bundle_path,
        content=payload,
        mime_type=mime_type,
        sha256=checksum,
        image_metadata=metadata,
    )


def _manifest_bundle_hash(manifest: dict[str, object]) -> str:
    files = manifest["files"]
    assert isinstance(files, dict)
    material = [
        {
            "path": path,
            "sha256": entry["sha256"],
            "mime_type": entry["mime_type"],
            "image": entry.get("image"),
        }
        for path, entry in sorted(files.items())
    ]
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _nested_json_document(depth: int) -> str:
    return '{"value":' * depth + '"safe"' + "}" * depth


def test_bundle_write_is_atomic_and_repeatable(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    root = tmp_path / "output"
    writer = ArticleBundleWriter(root)

    first = writer.write(bundle)
    second = writer.write(bundle)

    assert first == second
    assert (first / "article.draft.md").exists()
    assert (first / "sources.json").exists()
    assert (first / "manifest.sha256").exists()
    assert (first / "manifest.json").exists()
    assert not list(root.rglob("*.tmp-*"))
    manifest = json.loads((first / "manifest.json").read_text("utf-8"))
    assert manifest["bundle_hash"] == bundle.bundle_hash
    assert list(manifest["files"])[-1] == "sources.json"


def test_run_directory_write_requires_writer_issued_lease(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")

    with pytest.raises(BundlePublishError, match="lease"):
        writer.write(
            bundle,
            run_directory="2026-08-28--run-000000000000",
        )


def test_new_writer_default_write_gets_revision_without_reopening_primary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "output"
    bundle = _bundle(tmp_path)
    first = ArticleBundleWriter(root).write(bundle)
    before = _snapshot_tree_bytes(first.parent)

    second = ArticleBundleWriter(root).write(bundle)

    assert first.parent.name == "2026-08-28"
    assert second.parent.name.startswith("2026-08-28--run-")
    assert second.parent != first.parent
    assert _snapshot_tree_bytes(first.parent) == before


def test_completed_implicit_lease_rolls_same_writer_to_revision(tmp_path: Path) -> None:
    root = tmp_path / "output"
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(root)
    first_run = writer.write_run(date(2026, 8, 28), "# weekly\n", (bundle,))
    before = _snapshot_tree_bytes(first_run)

    revised_article = writer.write(bundle)

    assert revised_article.parent.name.startswith("2026-08-28--run-")
    assert revised_article.parent != first_run
    assert _snapshot_tree_bytes(first_run) == before


def test_post_replace_failed_owned_article_cannot_be_reopened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "output"
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(root)
    real_fsync_directory = bundle_module._fsync_directory
    injected = False

    def fail_after_replace(path: Path) -> None:
        nonlocal injected
        if (path / bundle.slug).is_dir() and not injected:
            injected = True
            raise BundlePublishError("injected post-replace failure")
        real_fsync_directory(path)

    monkeypatch.setattr(bundle_module, "_fsync_directory", fail_after_replace)
    with pytest.raises(BundlePublishError, match="injected post-replace"):
        writer.write(bundle)
    monkeypatch.setattr(bundle_module, "_fsync_directory", real_fsync_directory)

    with pytest.raises(BundlePublishError, match="failed-owned|reopen|lease"):
        writer.write(bundle)


def test_run_lease_rejects_unregistered_writer_token_shaped_stage(tmp_path: Path) -> None:
    root = tmp_path / "output"
    writer = ArticleBundleWriter(root)
    lease = writer.reserve_run_directory(
        date(2026, 8, 28),
        workflow_id="workflow-forged-stage",
    )
    forged = (
        root
        / lease.name
        / f".forged.tmp-deadbeefcafe-{lease._writer_token}-0123456789abcdef"
    )
    forged.mkdir()

    with pytest.raises(BundlePublishError, match="unowned|identity|lease"):
        writer.verify_run_directory_lease(lease)


def test_run_directory_lease_rejects_substituted_partial_directory(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    root = tmp_path / "output"
    writer = ArticleBundleWriter(root)
    lease = writer.reserve_run_directory(
        date(2026, 8, 28),
        workflow_id="workflow-substitution",
    )
    original = root / lease.name
    backup = tmp_path / "owned-run-backup"
    original.rename(backup)
    original.mkdir()

    with pytest.raises(BundlePublishError, match="lease|identity|substitut"):
        writer.write(bundle, run_directory=lease)


def test_bundle_directory_fsync_primary_failure_survives_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = tmp_path / "bundle-dual-failure-probe"
    probe.write_bytes(b"probe")
    real_open = bundle_module.os.open
    real_close = bundle_module.os.close
    source_descriptor = real_open(probe, os.O_RDONLY)
    opened: list[int] = []

    def duplicate_probe(_path, _flags):
        descriptor = os.dup(source_descriptor)
        opened.append(descriptor)
        return descriptor

    def fail_fsync(_descriptor: int) -> None:
        raise OSError(errno.EIO, "sensitive fsync body")

    def fail_close(descriptor: int) -> None:
        if descriptor in opened:
            raise OSError(errno.EIO, "sensitive close body")
        real_close(descriptor)

    monkeypatch.setattr(bundle_module.os, "open", duplicate_probe)
    monkeypatch.setattr(bundle_module.os, "fsync", fail_fsync)
    monkeypatch.setattr(bundle_module.os, "close", fail_close)
    try:
        with pytest.raises(BundlePublishError, match="directory fsync failed"):
            bundle_module._fsync_directory(tmp_path)
    finally:
        monkeypatch.setattr(bundle_module.os, "close", real_close)
        for descriptor in opened:
            real_close(descriptor)
        real_close(source_descriptor)


def test_run_reservation_wraps_directory_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = tmp_path / "bundle-close-probe"
    probe.write_bytes(b"probe")
    real_open = bundle_module.os.open
    real_close = bundle_module.os.close
    source_descriptor = real_open(probe, os.O_RDONLY)
    opened: list[int] = []

    def duplicate_probe(_path, _flags):
        descriptor = os.dup(source_descriptor)
        opened.append(descriptor)
        return descriptor

    def fail_close(descriptor: int) -> None:
        if descriptor in opened:
            raise OSError(errno.EIO, "sensitive reservation close body")
        real_close(descriptor)

    monkeypatch.setattr(bundle_module.os, "open", duplicate_probe)
    monkeypatch.setattr(bundle_module.os, "fsync", lambda _descriptor: None)
    monkeypatch.setattr(bundle_module.os, "close", fail_close)
    try:
        with pytest.raises(BundlePublishError, match="directory close failed"):
            ArticleBundleWriter(tmp_path / "output").reserve_run_directory(
                date(2026, 8, 28),
                workflow_id="reservation-close",
            )
    finally:
        monkeypatch.setattr(bundle_module.os, "close", real_close)
        for descriptor in opened:
            real_close(descriptor)
        real_close(source_descriptor)


@pytest.mark.parametrize("entrypoint", ["replay", "validate_run", "history"])
@pytest.mark.parametrize(
    ("failure_kind", "expected_message"),
    [
        ("close_only", "handle close failed"),
        ("read_and_close", "handle read failed"),
        ("identity_and_close", "opened identity differs"),
    ],
)
def test_bound_file_close_preserves_primary_for_all_reader_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    failure_kind: str,
    expected_message: str,
) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    if entrypoint == "replay":
        bundle = _bundle(tmp_path)
        published = writer.write(bundle)
        target = published / "manifest.json"

        def invoke() -> object:
            return writer.write(bundle)

    else:
        run_path = writer.write_run(date(2026, 8, 28), "# weekly\n")
        target = run_path / "manifest.json"

        def invoke() -> object:
            if entrypoint == "validate_run":
                return writer.validate_run(date(2026, 8, 28))
            return writer.validated_run_article_metadata(date(2026, 8, 28))

    real_open = bundle_module.os.open
    real_read = bundle_module.os.read
    real_fstat = bundle_module.os.fstat
    real_close = bundle_module.os.close
    tracked_descriptors: set[int] = set()

    def track_target_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == target:
            tracked_descriptors.add(descriptor)
        return descriptor

    def fail_target_read(descriptor: int, size: int) -> bytes:
        if descriptor in tracked_descriptors and failure_kind == "read_and_close":
            raise OSError(errno.EIO, "sensitive bound read body")
        return real_read(descriptor, size)

    def change_target_identity(descriptor: int):
        metadata = real_fstat(descriptor)
        if descriptor in tracked_descriptors and failure_kind == "identity_and_close":
            return _changed_identity(metadata)
        return metadata

    def fail_target_close(descriptor: int) -> None:
        if descriptor in tracked_descriptors:
            raise OSError(errno.EIO, "sensitive bound close body")
        real_close(descriptor)

    monkeypatch.setattr(bundle_module.os, "open", track_target_open)
    monkeypatch.setattr(bundle_module.os, "read", fail_target_read)
    monkeypatch.setattr(bundle_module.os, "fstat", change_target_identity)
    monkeypatch.setattr(bundle_module.os, "close", fail_target_close)
    try:
        with pytest.raises(BundlePublishError, match=expected_message) as raised:
            invoke()
        assert "sensitive bound close body" not in str(raised.value)
    finally:
        monkeypatch.setattr(bundle_module.os, "close", real_close)
        for descriptor in tracked_descriptors:
            try:
                real_close(descriptor)
            except OSError:
                pass


def test_bound_file_final_path_substitution_precedes_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "bound-file.txt"
    target.write_bytes(b"owned payload")
    expected_fingerprint = bundle_module._path_fingerprint(
        target,
        "test bound file",
        BundlePublishError,
    )
    backup = tmp_path / "bound-file-original.txt"
    replacement = tmp_path / "bound-file-replacement.txt"
    replacement.write_bytes(b"substituted payload")
    real_close = bundle_module.os.close
    real_replace = bundle_module.os.replace
    close_calls = 0

    def substitute_then_fail_close(descriptor: int) -> None:
        nonlocal close_calls
        close_calls += 1
        real_close(descriptor)
        real_replace(target, backup)
        real_replace(replacement, target)
        raise OSError(errno.EIO, "sensitive close body")

    monkeypatch.setattr(bundle_module.os, "close", substitute_then_fail_close)

    with pytest.raises(BundlePublishError, match="identity changed during replay") as raised:
        bundle_module._read_bound_file(
            target,
            expected_fingerprint,
            max_bytes=64,
            label="test bound file",
        )

    assert close_calls == 1
    assert "sensitive close body" not in str(raised.value)
    assert backup.read_bytes() == b"owned payload"
    assert target.read_bytes() == b"substituted payload"


def test_bound_file_read_failure_precedes_final_substitution_and_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "bound-file.txt"
    target.write_bytes(b"owned payload")
    expected_fingerprint = bundle_module._path_fingerprint(
        target,
        "test bound file",
        BundlePublishError,
    )
    backup = tmp_path / "bound-file-original.txt"
    replacement = tmp_path / "bound-file-replacement.txt"
    replacement.write_bytes(b"substituted payload")
    real_lstat = bundle_module.os.lstat
    real_close = bundle_module.os.close
    real_replace = bundle_module.os.replace
    read_error = OSError(errno.EIO, "sensitive read body")
    close_calls = 0
    target_lstat_calls = 0

    def count_target_lstat(path: os.PathLike[str] | str):
        nonlocal target_lstat_calls
        if Path(path) == target:
            target_lstat_calls += 1
        return real_lstat(path)

    def fail_read(_descriptor: int, _size: int) -> bytes:
        raise read_error

    def close_then_fail(descriptor: int) -> None:
        nonlocal close_calls
        close_calls += 1
        real_close(descriptor)
        real_replace(target, backup)
        real_replace(replacement, target)
        raise OSError(errno.EIO, "sensitive close body")

    monkeypatch.setattr(bundle_module.os, "lstat", count_target_lstat)
    monkeypatch.setattr(bundle_module.os, "read", fail_read)
    monkeypatch.setattr(bundle_module.os, "close", close_then_fail)

    with pytest.raises(BundlePublishError, match="handle read failed") as raised:
        bundle_module._read_bound_file(
            target,
            expected_fingerprint,
            max_bytes=64,
            label="test bound file",
        )

    assert raised.value.__cause__ is read_error
    assert close_calls == 1
    assert target_lstat_calls >= 2
    assert "sensitive read body" not in str(raised.value)
    assert "sensitive close body" not in str(raised.value)
    assert backup.read_bytes() == b"owned payload"
    assert target.read_bytes() == b"substituted payload"


def test_bound_file_close_only_error_is_stable_after_final_path_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "bound-file.txt"
    target.write_bytes(b"owned payload")
    expected_fingerprint = bundle_module._path_fingerprint(
        target,
        "test bound file",
        BundlePublishError,
    )
    real_lstat = bundle_module.os.lstat
    real_close = bundle_module.os.close
    close_calls = 0
    target_lstat_calls = 0

    def count_target_lstat(path: os.PathLike[str] | str):
        nonlocal target_lstat_calls
        if Path(path) == target:
            target_lstat_calls += 1
        return real_lstat(path)

    def close_then_fail(descriptor: int) -> None:
        nonlocal close_calls
        close_calls += 1
        real_close(descriptor)
        raise OSError(errno.EIO, "sensitive close body")

    monkeypatch.setattr(bundle_module.os, "lstat", count_target_lstat)
    monkeypatch.setattr(bundle_module.os, "close", close_then_fail)

    with pytest.raises(BundlePublishError, match="handle close failed") as raised:
        bundle_module._read_bound_file(
            target,
            expected_fingerprint,
            max_bytes=64,
            label="test bound file",
        )

    assert close_calls == 1
    assert target_lstat_calls >= 2
    assert "sensitive close body" not in str(raised.value)


@pytest.mark.parametrize("error_type", [RuntimeError, MemoryError, KeyboardInterrupt])
def test_bound_file_unexpected_primary_closes_once_and_remains_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    target = tmp_path / "bound-file.txt"
    target.write_bytes(b"owned payload")
    expected_fingerprint = bundle_module._path_fingerprint(
        target,
        "test bound file",
        BundlePublishError,
    )
    real_open = bundle_module.os.open
    real_close = bundle_module.os.close
    primary_error = error_type("unexpected primary body")
    tracked_descriptors: set[int] = set()
    close_calls = 0

    def track_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == target:
            tracked_descriptors.add(descriptor)
        return descriptor

    def fail_read(_descriptor: int, _size: int) -> bytes:
        raise primary_error

    def close_then_fail(descriptor: int) -> None:
        nonlocal close_calls
        close_calls += 1
        real_close(descriptor)
        raise OSError(errno.EIO, "sensitive close body")

    monkeypatch.setattr(bundle_module.os, "open", track_open)
    monkeypatch.setattr(bundle_module.os, "read", fail_read)
    monkeypatch.setattr(bundle_module.os, "close", close_then_fail)
    try:
        with pytest.raises(error_type) as raised:
            bundle_module._read_bound_file(
                target,
                expected_fingerprint,
                max_bytes=64,
                label="test bound file",
            )

        assert raised.value is primary_error
        assert close_calls == 1
    finally:
        for descriptor in tracked_descriptors:
            try:
                real_close(descriptor)
            except OSError:
                pass


def test_completed_run_directory_lease_cannot_be_reused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    lease = writer.reserve_run_directory(
        date(2026, 8, 28),
        workflow_id="workflow-complete",
    )
    writer.write_run(
        date(2026, 8, 28),
        "# weekly\n",
        (bundle,),
        run_directory=lease,
    )

    with pytest.raises(BundlePublishError, match="completed|reused|lease"):
        writer.write(bundle, run_directory=lease)


def test_completed_run_lease_rejects_directory_identity_substitution(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    root = tmp_path / "output"
    writer = ArticleBundleWriter(root)
    lease = writer.reserve_run_directory(
        date(2026, 8, 28),
        workflow_id="workflow-completed-substitution",
    )
    writer.write_run(
        date(2026, 8, 28),
        "# weekly\n",
        (bundle,),
        run_directory=lease,
    )
    run_root = root / lease.name
    backup = tmp_path / "completed-run-backup"
    run_root.rename(backup)
    shutil.copytree(backup, run_root)

    with pytest.raises(BundlePublishError, match="identity|substitut"):
        writer.verify_run_directory_lease(lease, allow_completed=True)


def test_lease_from_another_writer_has_no_write_authority(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    root = tmp_path / "output"
    issuer = ArticleBundleWriter(root)
    lease = issuer.reserve_run_directory(
        date(2026, 8, 28),
        workflow_id="workflow-authority",
    )

    with pytest.raises(BundlePublishError, match="lease|authority"):
        ArticleBundleWriter(root).write(bundle, run_directory=lease)


def test_exact_replay_rechecks_root_identity_after_existing_hash_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    backup = tmp_path / "validated-replay-root"
    replacement = tmp_path / "substituted-replay-root"
    real_existing_hash = writer._existing_hash
    real_replace = bundle_module.os.replace
    substituted = False

    def substitute_after_existing_hash(
        path: Path,
        *,
        expected_slug: str | None = None,
        allow_staging_name: bool = False,
    ) -> bundle_module._ExistingBundleValidation:
        nonlocal substituted
        observed = real_existing_hash(
            path,
            expected_slug=expected_slug,
            allow_staging_name=allow_staging_name,
        )
        if path == published and not substituted:
            shutil.copytree(path, replacement)
            real_replace(path, backup)
            real_replace(replacement, path)
            substituted = True
        return observed

    monkeypatch.setattr(writer, "_existing_hash", substitute_after_existing_hash)

    with pytest.raises(BundlePublishError, match="identity|substitut|reparse|changed"):
        writer.write(bundle)
    assert substituted is True
    assert backup.is_dir()


def test_changed_content_gets_deterministic_revision_without_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "output"
    writer = ArticleBundleWriter(root)
    original_bundle = _bundle(tmp_path / "original")
    changed_bundle = _bundle(tmp_path / "changed", title="서울 해오름 A단지 정정공고")

    original = writer.write(original_bundle)
    original_manifest = (original / "manifest.json").read_bytes()
    revision = writer.write(changed_bundle)

    assert revision.name == f"{changed_bundle.slug}--rev-{changed_bundle.bundle_hash[:12]}"
    assert writer.write(changed_bundle) == revision
    assert (original / "manifest.json").read_bytes() == original_manifest
    assert revision != original


def test_repeat_rejects_a_tampered_completed_bundle(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    (published / "article.draft.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(BundlePublishError, match="checksum"):
        writer.write(bundle)


def test_repeat_rejects_tampered_manifest_identity_fields(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    manifest_path = published / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["slug"] = "different-slug"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BundlePublishError, match="identity|slug"):
        writer.write(bundle)


@pytest.mark.parametrize(
    "raw_manifest",
    [
        "[1]",
        "42",
        "NaN",
        '{"schema_version": NaN}',
        _nested_json_document(80),
        _nested_json_document(2_000),
    ],
)
def test_article_manifest_requires_strict_json_object(
    tmp_path: Path, raw_manifest: str
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    (published / "manifest.json").write_text(raw_manifest, encoding="utf-8")

    with pytest.raises(BundlePublishError, match="JSON object|JSON manifest"):
        writer._existing_hash(published)


def test_article_manifest_rejects_duplicate_keys(tmp_path: Path) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(_bundle(tmp_path))
    manifest_path = published / "manifest.json"
    raw = manifest_path.read_text("utf-8").replace(
        '"schema_version": 1',
        '"schema_version": 1,\n  "schema_version": 1',
        1,
    )
    manifest_path.write_text(raw, encoding="utf-8")

    with pytest.raises(BundlePublishError, match="duplicate|JSON manifest"):
        writer._existing_hash(published)


@pytest.mark.parametrize(
    ("relative", "is_directory"),
    [
        ("article.md", False),
        ("assets/extra.png", False),
        ("unexpected", True),
    ],
)
def test_repeat_rejects_every_unmanifested_file_or_directory(
    tmp_path: Path, relative: str, is_directory: bool
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    extra = published / relative
    if is_directory:
        extra.mkdir(parents=True)
    else:
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(b"unmanifested")

    with pytest.raises(BundlePublishError, match="unmanifested"):
        writer.write(bundle)


def test_lstat_link_detection_does_not_depend_on_windows_symlink_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = tmp_path / "simulated-link"
    real_lstat = bundle_module.os.lstat

    def fake_lstat(path: os.PathLike[str] | str):
        if Path(path) == probe:
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        return real_lstat(path)

    monkeypatch.setattr(bundle_module.os, "lstat", fake_lstat)

    with pytest.raises(BundleValidationError, match="symlink|reparse"):
        ArticleBundleWriter._assert_no_symlink(probe)


def test_repeat_rejects_unmanifested_symlink(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    symlink = published / "article.md"
    try:
        symlink.symlink_to(published / "article.draft.md")
    except OSError:
        pytest.skip("real symlinks are unavailable; lstat fallback is covered separately")

    with pytest.raises(BundlePublishError, match="symlink|reparse|unmanifested"):
        writer.write(bundle)


def test_repeat_lstat_rejects_simulated_unmanifested_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    simulated = published / "article.md"
    simulated.write_bytes(b"placeholder")
    real_lstat = bundle_module.os.lstat

    def fake_lstat(path: os.PathLike[str] | str):
        if Path(path) == simulated:
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        return real_lstat(path)

    monkeypatch.setattr(bundle_module.os, "lstat", fake_lstat)

    with pytest.raises(BundlePublishError, match="symlink|reparse"):
        writer.write(bundle)


def _changed_identity(metadata: os.stat_result) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=metadata.st_mode,
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino + 1,
        st_size=metadata.st_size,
        st_mtime_ns=metadata.st_mtime_ns,
        st_ctime_ns=metadata.st_ctime_ns,
        st_file_attributes=getattr(metadata, "st_file_attributes", 0),
    )


def test_replay_rejects_entry_identity_change_between_lstat_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    target = published / "article.draft.md"
    real_lstat = bundle_module.os.lstat
    calls = 0

    def changing_lstat(path: os.PathLike[str] | str):
        nonlocal calls
        metadata = real_lstat(path)
        if Path(path) == target:
            calls += 1
            if calls > 1:
                return _changed_identity(metadata)
        return metadata

    monkeypatch.setattr(bundle_module.os, "lstat", changing_lstat)

    with pytest.raises(BundlePublishError, match="changed|TOCTOU|identity"):
        writer._existing_hash(published)


def test_replay_rejects_ancestor_identity_change_between_lstat_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    ancestor = published.parent
    real_lstat = bundle_module.os.lstat
    calls = 0

    def changing_lstat(path: os.PathLike[str] | str):
        nonlocal calls
        metadata = real_lstat(path)
        if Path(path) == ancestor:
            calls += 1
            if calls > 1:
                return _changed_identity(metadata)
        return metadata

    monkeypatch.setattr(bundle_module.os, "lstat", changing_lstat)

    with pytest.raises(BundlePublishError, match="ancestor|changed|TOCTOU|identity"):
        writer._existing_hash(published)


def test_handle_bound_replay_rejects_a_to_b_to_a_path_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    target = published / "article.draft.md"
    attacker = tmp_path / "attacker.md"
    attacker_payload = b"# attacker-controlled markdown\n"
    attacker.write_bytes(attacker_payload)
    backup = tmp_path / "original-backup.md"
    manifest_path = published / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["files"]["article.draft.md"]["sha256"] = hashlib.sha256(
        attacker_payload
    ).hexdigest()
    manifest["files"]["article.draft.md"]["bytes"] = len(attacker_payload)
    manifest["bundle_hash"] = _manifest_bundle_hash(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    checksum_path = published / "manifest.sha256"
    checksum_lines = checksum_path.read_text("utf-8").splitlines()
    checksum_lines = [
        f"{hashlib.sha256(attacker_payload).hexdigest()}  article.draft.md"
        if line.endswith("  article.draft.md")
        else line
        for line in checksum_lines
    ]
    checksum_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    real_path_read = Path.read_bytes
    real_open = bundle_module.os.open
    real_read = bundle_module.os.read
    target_fd: int | None = None
    substituted = False

    def swap_read_restore(read_operation):
        nonlocal substituted
        substituted = True
        target.replace(backup)
        attacker.replace(target)
        try:
            return read_operation()
        finally:
            target.replace(attacker)
            backup.replace(target)

    def swapping_path_read(path: Path) -> bytes:
        if path == target and not substituted:
            return swap_read_restore(lambda: real_path_read(path))
        return real_path_read(path)

    def tracking_open(path, flags, *args, **kwargs):
        nonlocal target_fd
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == target:
            target_fd = descriptor
        return descriptor

    def swapping_fd_read(descriptor: int, amount: int) -> bytes:
        if descriptor == target_fd and not substituted:
            return swap_read_restore(lambda: real_read(descriptor, amount))
        return real_read(descriptor, amount)

    monkeypatch.setattr(Path, "read_bytes", swapping_path_read)
    monkeypatch.setattr(bundle_module.os, "open", tracking_open)
    monkeypatch.setattr(bundle_module.os, "read", swapping_fd_read)

    with pytest.raises(BundlePublishError, match="checksum|identity|TOCTOU|substitut|handle"):
        writer._existing_hash(published)
    assert substituted is True
    assert target.read_bytes() != attacker_payload


@pytest.mark.parametrize(
    "bad_path",
    ["../escape.md", "/absolute.md", "assets/../../escape.md", "assets\\escape.md"],
)
def test_bundle_rejects_path_traversal_and_non_portable_paths(
    tmp_path: Path, bad_path: str
) -> None:
    bundle = ArticleBundle(
        run_date=date(2026, 8, 28),
        slug="safe",
        files=(BundleFile.text(bad_path, "unsafe"),),
    )

    with pytest.raises(BundleValidationError, match="path"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


def test_bundle_rejects_duplicate_paths_case_insensitively(tmp_path: Path) -> None:
    bundle = ArticleBundle(
        run_date=date(2026, 8, 28),
        slug="safe",
        files=(BundleFile.text("A.md", "one"), BundleFile.text("a.md", "two")),
    )

    with pytest.raises(BundleValidationError, match="duplicate"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


@pytest.mark.parametrize(
    "bad_path",
    [
        "assets//card.webp",
        "./article.draft.md",
        "assets/./card.webp",
        "assets/",
        "line\nbreak.md",
        "trailing-dot.",
        "trailing-space ",
        "CON",
        "CONIN$",
        "CONOUT$",
        "COM¹",
        "LPT².txt",
        "dir/NUL.txt",
        "manifest.json.",
        "MANIFEST.JSON",
    ],
)
def test_bundle_path_must_be_exact_canonical_portable_posix(
    tmp_path: Path, bad_path: str
) -> None:
    bundle = ArticleBundle(
        run_date=date(2026, 8, 28),
        slug="safe",
        files=(BundleFile.text(bad_path, "unsafe"),),
    )

    with pytest.raises(BundleValidationError, match="path|reserved|portable|canonical"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


def test_bundle_rejects_unicode_normalization_path_alias(tmp_path: Path) -> None:
    decomposed = "e\u0301.md"
    bundle = ArticleBundle(
        run_date=date(2026, 8, 28),
        slug="safe",
        files=(BundleFile.text("é.md", "one"), BundleFile.text(decomposed, "two")),
    )

    with pytest.raises(BundleValidationError, match="canonical|duplicate"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


@pytest.mark.parametrize("alias", ["ARTICLE.DRAFT.MD", "e\u0301.md", "MANIFEST.JSON"])
def test_replay_manifest_rejects_case_unicode_and_reserved_path_aliases(
    tmp_path: Path, alias: str
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    manifest_path = published / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["files"][alias] = dict(manifest["files"]["article.draft.md"])
    manifest["bundle_hash"] = _manifest_bundle_hash(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BundlePublishError, match="alias|canonical|reserved"):
        writer._existing_hash(published)


def test_bundle_rejects_windows_device_slug(tmp_path: Path) -> None:
    bundle = ArticleBundle(
        run_date=date(2026, 8, 28),
        slug="con",
        files=(BundleFile.text("article.draft.md", "safe"),),
    )

    with pytest.raises(BundleValidationError, match="slug"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


def test_bundle_rejects_checksum_mismatch(tmp_path: Path) -> None:
    bundle = ArticleBundle(
        run_date=date(2026, 8, 28),
        slug="safe",
        files=(
            BundleFile(
                path="article.draft.md",
                content="valid utf-8",
                mime_type="text/markdown",
                sha256="0" * 64,
            ),
        ),
    )

    with pytest.raises(BundleValidationError, match="checksum"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


def test_bundle_rejects_non_utf8_text(tmp_path: Path) -> None:
    payload = b"\xff\xfe"
    bundle = ArticleBundle(
        run_date=date(2026, 8, 28),
        slug="safe",
        files=(
            BundleFile(
                path="article.draft.md",
                content=payload,
                mime_type="text/markdown",
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )

    with pytest.raises(BundleValidationError, match="UTF-8"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


@pytest.mark.parametrize(
    ("path", "mime_type", "payload"),
    [
        ("sources.json", "application/json", b'{"value":"\xff"}'),
        (
            "sources.ndjson",
            "application/x-ndjson",
            b'{"safe":1}\n{"value":"\xff"}\n',
        ),
    ],
)
def test_invalid_utf8_json_artifacts_raise_publish_error_at_bundle_boundary(
    tmp_path: Path,
    path: str,
    mime_type: str,
    payload: bytes,
) -> None:
    file = BundleFile(
        path=path,
        content=payload,
        mime_type=mime_type,
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    with pytest.raises(BundlePublishError, match="UTF-8") as captured:
        ArticleBundleWriter(tmp_path / "output").write(
            ArticleBundle(date(2026, 8, 28), "safe", (file,))
        )
    assert type(captured.value) is BundlePublishError


@pytest.mark.parametrize(
    ("path", "mime_type", "payload"),
    [
        ("payload.bin", "application/octet-stream", b"generic-binary"),
        ("notes.txt", "text/plain", b"plain text"),
        ("sources.json", "application/json", b"not-json"),
    ],
)
def test_bundle_allows_only_utf8_markdown_or_valid_json_and_png_webp(
    tmp_path: Path, path: str, mime_type: str, payload: bytes
) -> None:
    file = BundleFile(
        path=path,
        content=payload,
        mime_type=mime_type,
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    with pytest.raises(BundleValidationError, match="file type|JSON"):
        ArticleBundleWriter(tmp_path / "output").write(
            ArticleBundle(date(2026, 8, 28), "safe", (file,))
        )


@pytest.mark.parametrize(
    "document",
    [
        '{"safe": 1, "safe": 2}',
        _nested_json_document(80),
        _nested_json_document(2_000),
        json.dumps({"values": [0] * 10_100}),
    ],
    ids=["duplicate-key", "depth-limit", "parser-recursion", "node-limit"],
)
def test_bundle_json_rejects_duplicate_or_resource_exhausting_documents(
    tmp_path: Path, document: str
) -> None:
    file = BundleFile.text("sources.json", document, "application/json")

    with pytest.raises(BundlePublishError, match="JSON|depth|node|resource"):
        ArticleBundleWriter(tmp_path / "output").write(
            ArticleBundle(date(2026, 8, 28), "safe", (file,))
        )


def test_json_node_budget_counts_pending_stack_before_wide_child_expansion() -> None:
    class IterationTrackedList(list[object]):
        iterated = False

        def __iter__(self) -> Iterator[object]:
            self.iterated = True
            return super().__iter__()

    wide_child = IterationTrackedList(["safe"] * 5_000)
    nested_wide_document: list[object] = ["safe"] * 6_000
    nested_wide_document.append(wide_child)

    with pytest.raises(BundlePublishError, match="node resource limit"):
        bundle_module._reject_html_in_json_values(
            nested_wide_document,
            "nested-wide JSON",
        )
    assert wide_child.iterated is False


@pytest.mark.parametrize(
    ("path", "mime_type", "payload"),
    [
        (
            "article.draft.md",
            "text/markdown",
            ("safe Markdown\n" + "x" * 2048 + "\n<svg><path/></svg>").encode(),
        ),
        (
            "sources.json",
            "application/json",
            json.dumps({"safe": "x" * 2048 + "<div>raw html</div>"}).encode(),
        ),
        (
            "sources.json",
            "application/json",
            b'{"safe":"\\u003csvg\\u003eescaped\\u003c/svg\\u003e"}',
        ),
    ],
)
def test_text_artifacts_reject_raw_html_svg_anywhere_in_full_document(
    tmp_path: Path, path: str, mime_type: str, payload: bytes
) -> None:
    file = BundleFile(
        path=path,
        content=payload,
        mime_type=mime_type,
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    with pytest.raises(BundleValidationError, match="raw HTML|SVG"):
        ArticleBundleWriter(tmp_path / "output").write(
            ArticleBundle(date(2026, 8, 28), "safe", (file,))
        )


@pytest.mark.parametrize(
    ("mime_type", "payload"),
    [
        ("image/jpeg", b"\xff\xd8\xff\xe0"),
        ("image/gif", b"GIF89a"),
        ("image/bmp", b"BM"),
        ("image/tiff", b"II*\x00"),
        ("image/x-icon", b"\x00\x00\x01\x00"),
        ("image/avif", b"\x00\x00\x00\x18ftypavif"),
        ("image/heic", b"\x00\x00\x00\x18ftypheic"),
        ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    ],
)
def test_bundle_rejects_every_non_png_webp_image_format(
    tmp_path: Path, mime_type: str, payload: bytes
) -> None:
    file = BundleFile(
        path="payload.bin",
        content=payload,
        mime_type=mime_type,
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    with pytest.raises(BundleValidationError, match="image metadata|allowed|MIME"):
        ArticleBundleWriter(tmp_path / "output").write(
            ArticleBundle(date(2026, 8, 28), "safe", (file,))
        )


def test_bundle_rejects_images_without_complete_manifest_metadata(tmp_path: Path) -> None:
    payload = b"not-even-an-image"
    bundle = ArticleBundle(
        run_date=date(2026, 8, 28),
        slug="safe",
        files=(
            BundleFile(
                path="assets/image.png",
                content=payload,
                mime_type="image/png",
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )

    with pytest.raises(BundleValidationError, match="image metadata"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


def test_image_signature_cannot_hide_behind_octet_stream_declaration(tmp_path: Path) -> None:
    payload = _encoded_image("PNG", (10, 10))
    bundle = ArticleBundle(
        run_date=date(2026, 8, 28),
        slug="safe",
        files=(
            BundleFile(
                path="assets/hero.png",
                content=payload,
                mime_type="application/octet-stream",
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )

    with pytest.raises(BundleValidationError, match="image signature"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


def test_bundle_rejects_forged_generic_hero_provenance(tmp_path: Path) -> None:
    payload = _encoded_image("PNG", (1200, 630))
    checksum = hashlib.sha256(payload).hexdigest()
    approved = build_image_set(_notice(), tmp_path / "template").images[0]
    forged = replace(approved, sha256=checksum, width=1200, height=630)
    image_file = BundleFile(
        path="assets/hero.png",
        content=payload,
        mime_type="image/png",
        sha256=checksum,
        image_metadata=forged,
    )

    with pytest.raises(BundleValidationError, match="hero provenance"):
        ArticleBundleWriter(tmp_path / "output").write(
            ArticleBundle(date(2026, 8, 28), "safe", (image_file,))
        )


def test_bundle_rejects_image_mime_signature_and_extension_mismatch(tmp_path: Path) -> None:
    payload = _encoded_image("PNG", (10, 10))
    image_file = _declared_image_file(
        tmp_path,
        bundle_path="assets/summary-card.webp",
        payload=payload,
        mime_type="image/webp",
        width=10,
        height=10,
    )
    bundle = ArticleBundle(date(2026, 8, 28), "safe", (image_file,))

    with pytest.raises(BundleValidationError, match="MIME|signature|extension"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


def test_bundle_rejects_image_outside_exact_asset_placement(tmp_path: Path) -> None:
    payload = _encoded_image("WEBP", (10, 10))
    image_file = _declared_image_file(
        tmp_path,
        bundle_path="hero.webp",
        payload=payload,
        mime_type="image/webp",
        width=10,
        height=10,
    )

    with pytest.raises(BundleValidationError, match="asset path"):
        ArticleBundleWriter(tmp_path / "output").write(
            ArticleBundle(date(2026, 8, 28), "safe", (image_file,))
        )


def test_bundle_rejects_compressed_image_with_oversized_dimensions(tmp_path: Path) -> None:
    payload = _encoded_image("WEBP", (5000, 1))
    image_file = _declared_image_file(
        tmp_path,
        bundle_path="assets/summary-card.webp",
        payload=payload,
        mime_type="image/webp",
        width=5000,
        height=1,
    )

    with pytest.raises(BundlePublishError, match="dimensions"):
        ArticleBundleWriter(tmp_path / "output").write(
            ArticleBundle(date(2026, 8, 28), "safe", (image_file,))
        )


def test_bundle_rejects_derived_card_with_unbound_font_material(tmp_path: Path) -> None:
    payload = _encoded_image("WEBP", (1200, 630))
    image_file = _declared_image_file(
        tmp_path,
        bundle_path="assets/summary-card.webp",
        payload=payload,
        mime_type="image/webp",
        width=1200,
        height=630,
    )
    assert image_file.image_metadata is not None
    forged = replace(image_file.image_metadata, renderer_fingerprint="0" * 64)
    image_file = replace(image_file, image_metadata=forged)

    with pytest.raises(BundleValidationError, match="card provenance"):
        ArticleBundleWriter(tmp_path / "output").write(
            ArticleBundle(date(2026, 8, 28), "safe", (image_file,))
        )


def test_bundle_rejects_arbitrary_webp_with_copied_card_labels_and_fingerprint(
    tmp_path: Path,
) -> None:
    payload = _encoded_image("WEBP", (1200, 630))
    copied_labels = _declared_image_file(
        tmp_path,
        bundle_path="assets/summary-card.webp",
        payload=payload,
        mime_type="image/webp",
        width=1200,
        height=630,
    )

    with pytest.raises(BundleValidationError, match="rerender|renderer input|card bytes"):
        ArticleBundleWriter(tmp_path / "output").write(
            ArticleBundle(date(2026, 8, 28), "safe", (copied_labels,))
        )


def test_bundle_converts_card_measurement_failure_to_publish_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(tmp_path)

    def fail_measurement(*_args: object, **_kwargs: object) -> float:
        raise OverflowError("synthetic Pillow measurement overflow")

    image_module._rerender_card_bytes_cached.cache_clear()
    monkeypatch.setattr(image_module.ImageDraw.ImageDraw, "textlength", fail_measurement)

    with pytest.raises(BundlePublishError, match="renderer|rerender|measurement"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


@pytest.mark.parametrize(
    "renderer_input",
    [
        (("kind", "summary", "unexpected"),),
        ((["kind"], "summary"),),
    ],
)
def test_bundle_normalizes_malformed_renderer_input_pairs_to_publish_error(
    tmp_path: Path,
    renderer_input: object,
) -> None:
    bundle = _bundle(tmp_path)
    summary = next(item for item in bundle.files if item.path == "assets/summary-card.webp")
    assert summary.image_metadata is not None
    malformed_summary = replace(
        summary,
        image_metadata=replace(summary.image_metadata, renderer_input=renderer_input),
    )
    malformed_bundle = replace(
        bundle,
        files=tuple(
            malformed_summary if item.path == malformed_summary.path else item
            for item in bundle.files
        ),
    )

    with pytest.raises(BundlePublishError):
        ArticleBundleWriter(tmp_path / "output").write(malformed_bundle)


def test_exact_replay_reapplies_image_rights_and_provenance_semantics(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    published = writer.write(bundle)
    manifest_path = published / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    hero = manifest["files"]["assets/hero.png"]["image"]
    hero["rights_status"] = "owned"
    hero["creator"] = "forged creator"
    manifest["bundle_hash"] = _manifest_bundle_hash(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BundlePublishError, match="rights|provenance"):
        writer._existing_hash(published)


def test_bundle_rejects_symlink_source(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("source", encoding="utf-8")
    symlink = tmp_path / "link.md"
    try:
        symlink.symlink_to(source)
    except OSError:
        pytest.skip("symlinks are unavailable in this Windows environment")
    bundle = ArticleBundle(
        run_date=date(2026, 8, 28),
        slug="safe",
        files=(BundleFile.from_path("article.draft.md", symlink, "text/markdown"),),
    )

    with pytest.raises(BundleValidationError, match="symlink"):
        ArticleBundleWriter(tmp_path / "output").write(bundle)


def test_write_run_publishes_index_last_with_article_links(tmp_path: Path) -> None:
    root = tmp_path / "output"
    bundle = _bundle(tmp_path)
    index = f"# 주간 주거 공고\n\n- [상세](./{bundle.slug}/article.md)\n"

    run_path = ArticleBundleWriter(root).write_run(date(2026, 8, 28), index, (bundle,))

    assert run_path == root / "2026-08-28"
    assert (run_path / "index.md").read_text("utf-8") == index
    run_manifest = json.loads((run_path / "manifest.json").read_text("utf-8"))
    assert run_manifest["articles"] == {bundle.slug: bundle.bundle_hash}


def test_write_run_links_a_changed_article_to_its_immutable_revision(tmp_path: Path) -> None:
    root = tmp_path / "output"
    writer = ArticleBundleWriter(root)
    original = _bundle(tmp_path / "original")
    changed = _bundle(tmp_path / "changed", title="서울 해오름 A단지 정정공고")
    writer.write(original)
    index = f"# 주간 주거 공고\n\n- [상세](./{changed.slug}/article.md)\n"

    run_path = writer.write_run(date(2026, 8, 28), index, (changed,))
    revision_name = f"{changed.slug}--rev-{changed.bundle_hash[:12]}"

    assert f"./{revision_name}/article.md" in (run_path / "index.md").read_text("utf-8")


def test_article_retry_uses_new_stage_without_deleting_abandoned_write_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    original_write = bundle_module._write_bytes_fsynced
    calls = 0

    def fail_second_write(path: Path, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "injected write interruption")
        original_write(path, payload)

    monkeypatch.setattr(bundle_module, "_write_bytes_fsynced", fail_second_write)
    with pytest.raises(BundlePublishError, match="write|publish"):
        writer.write(bundle)
    abandoned = tuple((tmp_path / "output").rglob("*.tmp-*"))
    assert len(abandoned) == 1
    monkeypatch.setattr(bundle_module, "_write_bytes_fsynced", original_write)

    published = writer.write(bundle)

    assert (published / "manifest.json").is_file()
    assert tuple((tmp_path / "output").rglob("*.tmp-*")) == abandoned


def test_article_retry_uses_new_stage_without_deleting_abandoned_replace_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    original_replace = bundle_module.os.replace

    def fail_bundle_replace(source: os.PathLike[str] | str, target: os.PathLike[str] | str) -> None:
        if Path(target).name == bundle.slug:
            raise OSError(errno.EIO, "injected replace interruption")
        original_replace(source, target)

    monkeypatch.setattr(bundle_module.os, "replace", fail_bundle_replace)
    with pytest.raises(BundlePublishError, match="atomic bundle publish"):
        writer.write(bundle)
    abandoned = tuple((tmp_path / "output").rglob("*.tmp-*"))
    assert len(abandoned) == 1
    monkeypatch.setattr(bundle_module.os, "replace", original_replace)

    assert writer.write(bundle).name == bundle.slug
    assert tuple((tmp_path / "output").rglob("*.tmp-*")) == abandoned


def test_concurrent_identical_publish_retains_losing_stage_and_injected_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    original_replace = bundle_module.os.replace

    def publish_competing_copy_then_fail(
        source: os.PathLike[str] | str, target: os.PathLike[str] | str
    ) -> None:
        if Path(target).name == bundle.slug:
            shutil.copytree(source, target)
            (Path(source) / "foreign-injected.txt").write_bytes(b"must remain untouched")
            raise OSError(errno.EEXIST, "injected concurrent winner")
        original_replace(source, target)

    monkeypatch.setattr(bundle_module.os, "replace", publish_competing_copy_then_fail)

    with pytest.raises(BundlePublishError, match="lease|concurrent"):
        writer.write(bundle)

    stages = list((tmp_path / "output").rglob("*.tmp-*"))
    assert len(stages) == 1
    assert (stages[0] / "foreign-injected.txt").read_bytes() == b"must remain untouched"


def test_concurrent_winner_rechecks_root_identity_after_existing_hash_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    target = tmp_path / "output" / "2026-08-28" / bundle.slug
    backup = tmp_path / "validated-winner-root"
    replacement = tmp_path / "substituted-winner-root"
    real_existing_hash = writer._existing_hash
    real_replace = bundle_module.os.replace
    substituted = False

    def publish_competing_copy_then_fail(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
    ) -> None:
        if Path(destination) == target:
            shutil.copytree(source, destination)
            raise OSError(errno.EEXIST, "injected concurrent winner")
        real_replace(source, destination)

    def substitute_after_existing_hash(
        path: Path,
        *,
        expected_slug: str | None = None,
        allow_staging_name: bool = False,
    ) -> bundle_module._ExistingBundleValidation:
        nonlocal substituted
        observed = real_existing_hash(
            path,
            expected_slug=expected_slug,
            allow_staging_name=allow_staging_name,
        )
        if path == target and not substituted:
            shutil.copytree(path, replacement)
            real_replace(path, backup)
            real_replace(replacement, path)
            substituted = True
        return observed

    monkeypatch.setattr(bundle_module.os, "replace", publish_competing_copy_then_fail)
    monkeypatch.setattr(writer, "_existing_hash", substitute_after_existing_hash)

    with pytest.raises(
        BundlePublishError,
        match="identity|substitut|reparse|changed|lease|concurrent",
    ):
        writer.write(bundle)
    assert substituted is True
    assert backup.is_dir()


def test_article_publish_rejects_owned_stage_substitution_during_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    real_replace = bundle_module.os.replace
    backup = tmp_path / "owned-stage-backup"
    replacement = tmp_path / "replacement-stage"

    def substituting_replace(source, target) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if target_path.name == bundle.slug:
            shutil.copytree(source_path, replacement)
            real_replace(source_path, backup)
            real_replace(replacement, source_path)
            real_replace(source_path, target_path)
            return
        real_replace(source_path, target_path)

    monkeypatch.setattr(bundle_module.os, "replace", substituting_replace)

    with pytest.raises(BundlePublishError, match="owned|substitut|identity"):
        writer.write(bundle)
    assert backup.is_dir()


def test_article_publish_rechecks_owned_target_after_complete_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")
    target = tmp_path / "output" / "2026-08-28" / bundle.slug
    backup = tmp_path / "validated-owned-target"
    replacement = tmp_path / "substituted-owned-target"
    real_existing_hash = writer._existing_hash
    real_replace = bundle_module.os.replace
    substituted = False

    def substitute_after_identity_before_validation(
        path: Path,
        *,
        expected_slug: str | None = None,
        allow_staging_name: bool = False,
    ) -> bundle_module._ExistingBundleValidation:
        nonlocal substituted
        if path == target and not substituted:
            substituted = True
            shutil.copytree(path, replacement)
            real_replace(path, backup)
            real_replace(replacement, path)
        return real_existing_hash(
            path,
            expected_slug=expected_slug,
            allow_staging_name=allow_staging_name,
        )

    monkeypatch.setattr(writer, "_existing_hash", substitute_after_identity_before_validation)

    with pytest.raises(BundlePublishError, match="owned|substitut|identity"):
        writer.write(bundle)
    assert substituted is True
    assert backup.is_dir()


def test_foreign_staging_material_is_never_deleted_and_cannot_block_retry(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    root = tmp_path / "output"
    date_root = root / "2026-08-28"
    date_root.mkdir(parents=True)
    staging = date_root / f".{bundle.slug}.tmp-{bundle.bundle_hash[:12]}"
    staging.mkdir()
    unexpected = staging / "attacker-controlled.txt"
    unexpected.write_text("do not delete", encoding="utf-8")

    published = ArticleBundleWriter(root).write(bundle)

    assert published.name == bundle.slug
    assert unexpected.read_text("utf-8") == "do not delete"


def test_each_failed_invocation_uses_a_unique_writer_owned_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    writer = ArticleBundleWriter(tmp_path / "output")

    def always_fail(_path: Path, _payload: bytes) -> None:
        raise OSError(errno.EIO, "injected interruption")

    monkeypatch.setattr(bundle_module, "_write_bytes_fsynced", always_fail)
    for _attempt in range(2):
        with pytest.raises(BundlePublishError, match="write|publish"):
            writer.write(bundle)

    stages = tuple((tmp_path / "output").rglob("*.tmp-*"))
    assert len(stages) == 2
    assert len({stage.name for stage in stages}) == 2


def test_run_manifest_is_a_required_verified_commit_marker(tmp_path: Path) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    bundle = _bundle(tmp_path)
    index = f"# 주간 주거 공고\n\n- [상세](./{bundle.slug}/article.md)\n"
    run_path = writer.write_run(date(2026, 8, 28), index, (bundle,))

    assert writer.validate_run(date(2026, 8, 28)) == run_path
    (run_path / "index.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(BundlePublishError, match="index checksum"):
        writer.validate_run(date(2026, 8, 28))
    repaired = writer.write_run(date(2026, 8, 28), index, (bundle,))
    assert repaired != run_path
    assert repaired.name.startswith("2026-08-28--run-")
    assert writer.validate_run(
        date(2026, 8, 28),
        run_directory=repaired.name,
    ) == repaired
    (run_path / "manifest.json").unlink()
    with pytest.raises(BundlePublishError, match="commit marker"):
        writer.validate_run(date(2026, 8, 28))


@pytest.mark.parametrize(
    "raw_manifest",
    [
        "[]",
        "null",
        "Infinity",
        '{"run_date": NaN}',
        _nested_json_document(80),
        _nested_json_document(2_000),
    ],
)
def test_run_manifest_requires_strict_json_object(
    tmp_path: Path, raw_manifest: str
) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    bundle = _bundle(tmp_path)
    index = f"# 주간 주거 공고\n\n- [상세](./{bundle.slug}/article.md)\n"
    run_path = writer.write_run(date(2026, 8, 28), index, (bundle,))
    (run_path / "manifest.json").write_text(raw_manifest, encoding="utf-8")

    with pytest.raises(BundlePublishError, match="JSON object|JSON manifest"):
        writer.validate_run(date(2026, 8, 28))


def test_run_manifest_rejects_duplicate_keys(tmp_path: Path) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    run_path = writer.write_run(date(2026, 8, 28), "# 주간 주거 공고\n")
    manifest_path = run_path / "manifest.json"
    raw = manifest_path.read_text("utf-8").replace(
        '"schema_version": 1',
        '"schema_version": 1,\n  "schema_version": 1',
        1,
    )
    manifest_path.write_text(raw, encoding="utf-8")

    with pytest.raises(BundlePublishError, match="duplicate|JSON manifest"):
        writer.validate_run(date(2026, 8, 28))


def test_run_reader_reapplies_article_image_semantics(tmp_path: Path) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    bundle = _bundle(tmp_path)
    index = f"# 주간 주거 공고\n\n- [상세](./{bundle.slug}/article.md)\n"
    run_path = writer.write_run(date(2026, 8, 28), index, (bundle,))
    article_manifest_path = run_path / bundle.slug / "manifest.json"
    article_manifest = json.loads(article_manifest_path.read_text("utf-8"))
    article_manifest["files"]["assets/hero.png"]["image"]["rights_status"] = "owned"
    article_manifest["bundle_hash"] = _manifest_bundle_hash(article_manifest)
    article_manifest_path.write_text(json.dumps(article_manifest), encoding="utf-8")
    run_manifest_path = run_path / "manifest.json"
    run_manifest = json.loads(run_manifest_path.read_text("utf-8"))
    run_manifest["articles"][bundle.slug] = article_manifest["bundle_hash"]
    run_manifest_path.write_text(json.dumps(run_manifest), encoding="utf-8")

    with pytest.raises(BundlePublishError, match="rights|provenance"):
        writer.validate_run(date(2026, 8, 28))


def test_run_retry_repairs_interruption_between_index_and_commit_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    bundle = _bundle(tmp_path)
    index = f"# 주간 주거 공고\n\n- [상세](./{bundle.slug}/article.md)\n"
    original_replace = bundle_module.os.replace

    def fail_manifest_replace(
        source: os.PathLike[str] | str, target: os.PathLike[str] | str
    ) -> None:
        if Path(source).name.startswith(".manifest.json.tmp-"):
            raise OSError(errno.EIO, "injected commit-marker interruption")
        original_replace(source, target)

    monkeypatch.setattr(bundle_module.os, "replace", fail_manifest_replace)
    with pytest.raises(BundlePublishError, match="run publish"):
        writer.write_run(date(2026, 8, 28), index, (bundle,))
    abandoned = tuple((tmp_path / "output" / "2026-08-28").glob(".manifest.json.tmp-*"))
    assert len(abandoned) == 1
    with pytest.raises(BundlePublishError, match="commit marker"):
        writer.validate_run(date(2026, 8, 28))
    monkeypatch.setattr(bundle_module.os, "replace", original_replace)

    repaired = writer.write_run(date(2026, 8, 28), index, (bundle,))

    assert repaired.name.startswith("2026-08-28--run-")
    assert writer.validate_run(
        date(2026, 8, 28),
        run_directory=repaired.name,
    ).is_dir()
    assert tuple((tmp_path / "output" / "2026-08-28").glob(".manifest.json.tmp-*")) == abandoned


def test_foreign_run_temp_files_are_never_deleted_or_reused(tmp_path: Path) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    bundle = _bundle(tmp_path)
    run_root = tmp_path / "output" / "2026-08-28"
    run_root.mkdir(parents=True)
    foreign = run_root / ".manifest.json.tmp-deadbeefcafe"
    foreign.write_text("foreign", encoding="utf-8")
    index = f"# 주간 주거 공고\n\n- [상세](./{bundle.slug}/article.md)\n"

    writer.write_run(date(2026, 8, 28), index, (bundle,))

    assert foreign.read_text("utf-8") == "foreign"


def test_each_failed_run_invocation_leaves_a_unique_owned_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    bundle = _bundle(tmp_path)
    index = f"# 주간 주거 공고\n\n- [상세](./{bundle.slug}/article.md)\n"
    original_replace = bundle_module.os.replace

    def fail_manifest_replace(
        source: os.PathLike[str] | str, target: os.PathLike[str] | str
    ) -> None:
        if Path(source).name.startswith(".manifest.json.tmp-"):
            raise OSError(errno.EIO, "injected commit-marker interruption")
        original_replace(source, target)

    monkeypatch.setattr(bundle_module.os, "replace", fail_manifest_replace)
    for _attempt in range(2):
        with pytest.raises(BundlePublishError, match="run publish"):
            writer.write_run(date(2026, 8, 28), index, (bundle,))

    abandoned = tuple(
        (tmp_path / "output").glob("2026-08-28*/.manifest.json.tmp-*")
    )
    assert len(abandoned) == 2
    assert len({path.name for path in abandoned}) == 2


@pytest.mark.parametrize("entry_kind", ["run_temp", "partial_target"])
def test_partial_run_lease_rejects_owned_entry_identity_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    root = tmp_path / "output"
    writer = ArticleBundleWriter(root)
    lease = writer.reserve_run_directory(
        date(2026, 8, 28),
        workflow_id=f"workflow-substituted-{entry_kind}",
    )
    real_replace = bundle_module.os.replace

    def fail_manifest_replace(source, target) -> None:
        if Path(target).name == "manifest.json":
            raise OSError(errno.EIO, "injected commit-marker interruption")
        real_replace(source, target)

    monkeypatch.setattr(bundle_module.os, "replace", fail_manifest_replace)
    with pytest.raises(BundlePublishError, match="run publish"):
        writer.write_run(
            date(2026, 8, 28),
            "# weekly\n",
            run_directory=lease,
        )
    monkeypatch.setattr(bundle_module.os, "replace", real_replace)

    run_root = root / lease.name
    owned = (
        next(run_root.glob(".manifest.json.tmp-*"))
        if entry_kind == "run_temp"
        else run_root / "index.md"
    )
    backup = tmp_path / f"owned-{entry_kind}-backup"
    replacement = tmp_path / f"substituted-{entry_kind}"
    shutil.copy2(owned, replacement)
    real_replace(owned, backup)
    real_replace(replacement, owned)

    with pytest.raises(BundlePublishError, match="owned|substitut|identity|changed"):
        writer.verify_run_directory_lease(lease)


def test_partial_run_lease_rejects_unregistered_writer_token_shaped_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "output"
    writer = ArticleBundleWriter(root)
    lease = writer.reserve_run_directory(
        date(2026, 8, 28),
        workflow_id="workflow-forged-run-temp",
    )
    real_replace = bundle_module.os.replace

    def fail_manifest_replace(source, target) -> None:
        if Path(target).name == "manifest.json":
            raise OSError(errno.EIO, "injected commit-marker interruption")
        real_replace(source, target)

    monkeypatch.setattr(bundle_module.os, "replace", fail_manifest_replace)
    with pytest.raises(BundlePublishError, match="run publish"):
        writer.write_run(
            date(2026, 8, 28),
            "# weekly\n",
            run_directory=lease,
        )

    forged = (
        root
        / lease.name
        / f".forged.tmp-deadbeefcafe-{lease._writer_token}-0123456789abcdef"
    )
    forged.write_bytes(b"foreign")

    with pytest.raises(BundlePublishError, match="unowned|identity|lease"):
        writer.verify_run_directory_lease(lease)


@pytest.mark.parametrize("target_name", ["index.md", "manifest.json"])
def test_run_publish_rejects_owned_temp_substitution_during_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_name: str
) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    bundle = _bundle(tmp_path)
    index = f"# 주간 주거 공고\n\n- [상세](./{bundle.slug}/article.md)\n"
    real_replace = bundle_module.os.replace
    backup = tmp_path / f"owned-{target_name}-backup"
    replacement = tmp_path / f"replacement-{target_name}"

    def substituting_replace(source, target) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if target_path.name == target_name and source_path.name.startswith(f".{target_name}.tmp-"):
            shutil.copy2(source_path, replacement)
            real_replace(source_path, backup)
            real_replace(replacement, source_path)
            real_replace(source_path, target_path)
            return
        real_replace(source_path, target_path)

    monkeypatch.setattr(bundle_module.os, "replace", substituting_replace)

    with pytest.raises(BundlePublishError, match="owned|substitut|identity"):
        writer.write_run(date(2026, 8, 28), index, (bundle,))
    assert backup.is_file()


def test_run_temp_substitution_after_handle_read_is_rejected_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    run_root = tmp_path / "output" / "2026-08-28"
    backup = tmp_path / "read-owned-index-temp"
    replacement = tmp_path / "substituted-index-temp"
    real_read_bound_file = bundle_module._read_bound_file
    real_replace = bundle_module.os.replace
    substituted = False

    def substitute_after_handle_read(
        path: Path,
        expected_fingerprint: tuple[object, ...],
        *,
        max_bytes: int,
        label: str,
    ) -> bytes:
        nonlocal substituted
        observed = real_read_bound_file(
            path,
            expected_fingerprint,
            max_bytes=max_bytes,
            label=label,
        )
        if label == "run index temp" and not substituted:
            substituted = True
            shutil.copy2(path, replacement)
            real_replace(path, backup)
            real_replace(replacement, path)
        return observed

    monkeypatch.setattr(bundle_module, "_read_bound_file", substitute_after_handle_read)

    with pytest.raises(BundlePublishError, match="owned|substitut|identity"):
        writer.write_run(date(2026, 8, 28), "# 주간 주거 공고\n")
    assert substituted is True
    assert backup.is_file()
    assert not (run_root / "index.md").exists()


@pytest.mark.parametrize("target_name", ["index.md", "manifest.json"])
def test_run_target_rechecks_identity_after_handle_bound_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    backup = tmp_path / f"validated-{target_name}"
    replacement = tmp_path / f"substituted-{target_name}"
    real_read_bound_file = bundle_module._read_bound_file
    real_replace = bundle_module.os.replace
    substituted = False

    def substitute_after_handle_read(
        path: Path,
        expected_fingerprint: tuple[object, ...],
        *,
        max_bytes: int,
        label: str,
    ) -> bytes:
        nonlocal substituted
        observed = real_read_bound_file(
            path,
            expected_fingerprint,
            max_bytes=max_bytes,
            label=label,
        )
        expected_label = (
            f"published run {target_name.removesuffix('.md').removesuffix('.json')}"
        )
        if label == expected_label and not substituted:
            substituted = True
            shutil.copy2(path, replacement)
            real_replace(path, backup)
            real_replace(replacement, path)
        return observed

    monkeypatch.setattr(bundle_module, "_read_bound_file", substitute_after_handle_read)

    with pytest.raises(BundlePublishError, match="owned|substitut|identity"):
        writer.write_run(date(2026, 8, 28), "# 주간 주거 공고\n")
    assert substituted is True
    assert backup.is_file()


def test_run_publish_rechecks_owned_targets_after_complete_run_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = ArticleBundleWriter(tmp_path / "output")
    backup = tmp_path / "fully-validated-run-manifest"
    replacement = tmp_path / "substituted-run-manifest"
    real_validate_run = writer.validate_run
    real_replace = bundle_module.os.replace

    def validate_then_substitute(
        run_date: date,
        *,
        run_directory: str | None = None,
    ) -> Path:
        run_root = real_validate_run(run_date, run_directory=run_directory)
        target = run_root / "manifest.json"
        shutil.copy2(target, replacement)
        real_replace(target, backup)
        real_replace(replacement, target)
        return run_root

    monkeypatch.setattr(writer, "validate_run", validate_then_substitute)

    with pytest.raises(BundlePublishError, match="owned|substitut|identity"):
        writer.write_run(date(2026, 8, 28), "# 주간 주거 공고\n")
    assert backup.is_file()


def test_directory_fsync_propagates_real_io_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundle_module.os, "open", lambda *_args, **_kwargs: 41)
    monkeypatch.setattr(
        bundle_module.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError(errno.EIO, "injected fsync")),
    )
    monkeypatch.setattr(bundle_module.os, "close", lambda _descriptor: None)

    with pytest.raises(BundlePublishError, match="fsync"):
        bundle_module._fsync_directory(Path("safe"))


def test_bundle_fsyncs_nested_and_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(bundle_module, "_fsync_directory", lambda path: calls.append(Path(path)))
    bundle = _bundle(tmp_path)
    root = tmp_path / "output"

    ArticleBundleWriter(root).write(bundle)

    assert any(path.name == "assets" for path in calls)
    assert any(".tmp-" in path.name for path in calls)
    assert root.resolve() in {path.resolve() for path in calls}
