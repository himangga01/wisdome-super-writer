from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

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
