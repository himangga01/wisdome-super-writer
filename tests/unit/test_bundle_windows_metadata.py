"""Bind reconciliation to the observed Windows lstat/native disagreement."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import apps.local_content.bundles as bundles

QUERY_FLAG = 0x10000000


def _with_metadata(metadata, **changes):
    values = {name: getattr(metadata, name) for name in dir(metadata) if name.startswith("st_")}
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("contaminated_reads", [1, 2])
def test_writer_accepts_only_native_verified_transient_query_flag(
    tmp_path, monkeypatch, contaminated_reads
):
    writer = bundles.ArticleBundleWriter(tmp_path / "output")
    bundle = bundles.ArticleBundle(
        date(2026, 10, 8),
        "native-fixture",
        (bundles.BundleFile.text("article.md", "# Native fixture\n", "text/markdown"),),
    )
    real_lstat = bundles.os.lstat
    contaminated = {}
    native_queries = []

    def one_contaminated_lstat(path, *args, **kwargs):
        metadata = real_lstat(path, *args, **kwargs)
        candidate = Path(path)
        if candidate.name.startswith(".native-fixture.tmp-"):
            count = contaminated.get(candidate, 0)
            contaminated[candidate] = count + 1
            if count < contaminated_reads:
                return _with_metadata(
                    metadata,
                    st_file_attributes=getattr(metadata, "st_file_attributes", 0) | QUERY_FLAG,
                )
        return metadata

    def native_attributes(path):
        native_queries.append(path)
        return getattr(real_lstat(path), "st_file_attributes", 0)

    monkeypatch.setattr(bundles.os, "lstat", one_contaminated_lstat)
    monkeypatch.setattr(bundles, "_windows_file_attributes", native_attributes, raising=False)
    published = writer.write(bundle)

    assert (published / "article.md").read_text(encoding="utf-8") == "# Native fixture\n"
    assert len(contaminated) == 1
    assert native_queries == list(contaminated) * 2


@pytest.mark.parametrize(
    "change",
    [
        "inode", "device", "type", "readonly", "hidden", "reparse",
        "size", "mtime", "ctime", "other_query_flag",
    ],
)
def test_query_reconciliation_rejects_other_metadata_changes(tmp_path, monkeypatch, change):
    path = tmp_path / "owned-stage"
    path.mkdir()
    metadata = bundles.os.lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    first = _with_metadata(metadata, st_file_attributes=attributes | QUERY_FLAG)
    fields = {
        "inode": {"st_ino": metadata.st_ino + 1},
        "device": {"st_dev": metadata.st_dev + 1},
        "type": {"st_mode": 0o100600},
        "readonly": {"st_file_attributes": attributes ^ 0x1},
        "hidden": {"st_file_attributes": attributes ^ 0x2},
        "reparse": {"st_file_attributes": attributes | 0x400},
        "size": {"st_size": metadata.st_size + 1},
        "mtime": {"st_mtime_ns": metadata.st_mtime_ns + 1},
        "ctime": {"st_ctime_ns": metadata.st_ctime_ns + 1},
        "other_query_flag": {"st_file_attributes": attributes | QUERY_FLAG | 0x20000000},
    }
    second = _with_metadata(metadata, **fields[change])
    snapshots = iter([first, second])
    monkeypatch.setattr(bundles.os, "lstat", lambda _path: next(snapshots))
    monkeypatch.setattr(bundles, "_windows_file_attributes", lambda _path: attributes)

    with pytest.raises(bundles.BundlePublishError, match="changed|disagree|reparse"):
        bundles._path_identity_fingerprint(path, "owned-stage", bundles.BundlePublishError)


@pytest.mark.parametrize("native_state", ["genuine_bit", "unsupported", "readonly", "reparse"])
def test_query_flag_is_not_blindly_masked(tmp_path, monkeypatch, native_state):
    path = tmp_path / "owned-stage"
    path.mkdir()
    real_lstat = bundles.os.lstat
    metadata = real_lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    first = _with_metadata(metadata, st_file_attributes=attributes | QUERY_FLAG)
    snapshots = iter([first, metadata])
    monkeypatch.setattr(bundles.os, "lstat", lambda _path: next(snapshots))
    native = {
        "genuine_bit": attributes | QUERY_FLAG,
        "unsupported": None,
        "readonly": attributes | 0x1,
        "reparse": attributes | 0x400,
    }[native_state]
    monkeypatch.setattr(bundles, "_windows_file_attributes", lambda _path: native, raising=False)

    if native_state in {"genuine_bit", "unsupported"}:
        identity = bundles._path_identity_fingerprint(
            path, "owned-stage", bundles.BundlePublishError
        )
        assert identity[3] & QUERY_FLAG
        with pytest.raises(bundles.BundlePublishError, match="owned identity"):
            bundles._require_owned_identity(path, identity, "owned-stage")
    else:
        with pytest.raises(bundles.BundlePublishError, match="disagree|reparse"):
            bundles._path_identity_fingerprint(path, "owned-stage", bundles.BundlePublishError)


@pytest.mark.parametrize("boundary", ["path", "handle"])
def test_full_bound_reader_preserves_query_flag_binding(tmp_path, monkeypatch, boundary):
    path = tmp_path / "source.txt"
    path.write_bytes(b"fixture")
    fingerprint = bundles._path_fingerprint(path, "source", bundles.BundlePublishError)
    real_lstat = bundles.os.lstat
    real_fstat = bundles.os.fstat

    def contaminated(metadata):
        return _with_metadata(
            metadata, st_file_attributes=getattr(metadata, "st_file_attributes", 0) | QUERY_FLAG
        )

    if boundary == "path":
        monkeypatch.setattr(
            bundles.os, "lstat", lambda candidate: contaminated(real_lstat(candidate))
        )
    else:
        monkeypatch.setattr(
            bundles.os, "fstat", lambda descriptor: contaminated(real_fstat(descriptor))
        )
    with pytest.raises(
        bundles.BundlePublishError, match="identity changed|opened identity differs"
    ):
        bundles._read_bound_file(path, fingerprint, max_bytes=7, label="source")


@pytest.mark.parametrize("error_call", [1, 2])
def test_native_attribute_query_error_stays_fail_closed(tmp_path, monkeypatch, error_call):
    path = tmp_path / "owned-stage"
    path.mkdir()
    metadata = bundles.os.lstat(path)
    first = _with_metadata(
        metadata, st_file_attributes=getattr(metadata, "st_file_attributes", 0) | QUERY_FLAG
    )
    snapshots = iter([first, metadata])
    monkeypatch.setattr(bundles.os, "lstat", lambda _path: next(snapshots))
    calls = 0

    def failing_native(_path):
        nonlocal calls
        calls += 1
        if calls == error_call:
            raise OSError(5, "fixture native read failure")
        return getattr(metadata, "st_file_attributes", 0)

    monkeypatch.setattr(bundles, "_windows_file_attributes", failing_native)
    with pytest.raises(bundles.BundlePublishError, match="native attributes cannot be inspected"):
        bundles._path_identity_fingerprint(path, "owned-stage", bundles.BundlePublishError)


def test_native_query_is_unavailable_on_other_platforms(tmp_path, monkeypatch):
    monkeypatch.setattr(bundles, "os", SimpleNamespace(name="posix"))
    assert bundles._windows_file_attributes(tmp_path) is None


@pytest.mark.parametrize("new_bit", [0x1, 0x400, QUERY_FLAG])
def test_repeated_native_attribute_disagreement_is_rejected(tmp_path, monkeypatch, new_bit):
    path = tmp_path / "owned-stage"
    path.mkdir()
    metadata = bundles.os.lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    snapshots = iter([
        _with_metadata(metadata, st_file_attributes=attributes | QUERY_FLAG), metadata
    ])
    native_snapshots = iter([attributes, attributes ^ new_bit])
    monkeypatch.setattr(bundles.os, "lstat", lambda _path: next(snapshots))
    monkeypatch.setattr(bundles, "_windows_file_attributes", lambda _path: next(native_snapshots))

    with pytest.raises(bundles.BundlePublishError, match="disagree|changed|reparse"):
        bundles._path_identity_fingerprint(path, "owned-stage", bundles.BundlePublishError)
