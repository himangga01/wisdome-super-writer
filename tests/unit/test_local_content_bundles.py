from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import shutil
import stat
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

import apps.local_content.bundles as bundle_module
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


def test_concurrent_identical_publish_cleans_its_validated_losing_stage(
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
            raise OSError(errno.EEXIST, "injected concurrent winner")
        original_replace(source, target)

    monkeypatch.setattr(bundle_module.os, "replace", publish_competing_copy_then_fail)

    published = writer.write(bundle)

    assert published.name == bundle.slug
    assert not list((tmp_path / "output").rglob("*.tmp-*"))


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
    writer.write_run(date(2026, 8, 28), index, (bundle,))
    assert writer.validate_run(date(2026, 8, 28)) == run_path
    (run_path / "manifest.json").unlink()
    with pytest.raises(BundlePublishError, match="commit marker"):
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

    writer.write_run(date(2026, 8, 28), index, (bundle,))

    assert writer.validate_run(date(2026, 8, 28)).is_dir()
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

    abandoned = tuple((tmp_path / "output" / "2026-08-28").glob(".manifest.json.tmp-*"))
    assert len(abandoned) == 2
    assert len({path.name for path in abandoned}) == 2


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
