from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from django.test import override_settings

from apps.local_content.acceptance import preview_headers_match
from apps.local_content.bundles import (
    ArticleBundle,
    ArticleBundleWriter,
    BundleFile,
    build_article_bundle,
)
from apps.local_content.contracts import HousingNotice
from apps.local_content.images import build_image_set
from apps.local_content.rendering import render_detailed_article

SEOUL = ZoneInfo("Asia/Seoul")
ASSET_URL = re.compile(
    rb'/local-articles/assets/2026-08-28/sample/[0-9a-f]{64}/assets/hero\.png'
)


def _notice(slug_seed: str = "sample") -> HousingNotice:
    return HousingNotice(
        source_key="applyhome",
        external_id=slug_seed,
        canonical_url="https://www.applyhome.co.kr/notice/1",
        title="서울 한빛 주택공급",
        publisher="청약홈",
        category="apt",
        region="서울특별시",
        status="공고중",
        published_at=datetime(2026, 8, 28, 9, tzinfo=SEOUL),
        source_checksum="a" * 64,
    )


def _preview_root(tmp_path: Path) -> Path:
    root = tmp_path / "housing"
    writer = ArticleBundleWriter(root)
    rendered = render_detailed_article(_notice())
    images = build_image_set(_notice(), tmp_path / "images")
    draft = build_article_bundle(date(2026, 8, 28), rendered, images)
    markdown = rendered.to_markdown() + (
        "\n[허용되지 않은 링크](https://example.com/should-not-link)\n"
    )
    final = ArticleBundle(
        run_date=draft.run_date,
        slug="sample",
        files=draft.files + (BundleFile.text("article.md", markdown, "text/markdown"),),
    )
    blocked = ArticleBundle(
        run_date=draft.run_date,
        slug="blocked-draft",
        files=(BundleFile.text("article.draft.md", "# 차단 초안\n", "text/markdown"),),
    )

    first_lease = writer.reserve_run_directory(date(2026, 8, 28), workflow_id="first")
    writer.write_run(
        date(2026, 8, 28),
        "# 2026-08-28 주간 공고\n\n"
        "- [상세 보기](./sample/article.md)\n"
        "- [차단 초안](./blocked-draft/article.md)\n",
        (final, blocked),
        run_directory=first_lease,
    )

    second = replace(final, slug="sample-second")
    second_lease = writer.reserve_run_directory(date(2026, 8, 28), workflow_id="second")
    writer.write_run(
        date(2026, 8, 28),
        "# 2026-08-28 두 번째 실행\n\n- [상세 보기](./sample-second/article.md)\n",
        (second,),
        run_directory=second_lease,
    )
    return root


@pytest.fixture
def preview_root(tmp_path: Path) -> Path:
    return _preview_root(tmp_path)


@override_settings(IS_LOCAL_RUNTIME=True)
def test_local_preview_lists_generations_and_renders_only_final_article(
    client,
    settings,
    preview_root: Path,
) -> None:
    settings.LOCAL_ARTICLE_ROOT = preview_root

    listing = client.get("/local-articles/")
    run = client.get("/local-articles/2026-08-28/")
    article = client.get("/local-articles/2026-08-28/sample/")
    blocked = client.get("/local-articles/2026-08-28/blocked-draft/")

    assert listing.status_code == 200
    assert b"2026-08-28--run-" in listing.content
    assert run.status_code == 200
    assert b'href="/local-articles/2026-08-28/sample/"' in run.content
    assert b'href="/local-articles/2026-08-28/blocked-draft/"' not in run.content
    assert article.status_code == 200
    assert all(
        b'rel="icon" href="/static/local_articles/generic-housing-hero.png"'
        in response.content
        for response in (listing, run, article)
    )
    assert "한눈에 보기" in article.content.decode("utf-8")
    assert b'href="https://www.applyhome.co.kr/notice/1"' in article.content
    assert b'href="https://example.com/should-not-link"' not in article.content
    assert ASSET_URL.search(article.content)
    assert blocked.status_code == 404
    assert article["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in article["Content-Security-Policy"]
    assert article["Referrer-Policy"] == "no-referrer"


@override_settings(IS_LOCAL_RUNTIME=True)
def test_local_asset_requires_committed_checksum_and_is_immutable(
    client,
    settings,
    preview_root: Path,
) -> None:
    settings.LOCAL_ARTICLE_ROOT = preview_root
    article = client.get("/local-articles/2026-08-28/sample/")
    asset_url = ASSET_URL.search(article.content)
    assert asset_url is not None

    asset = client.get(asset_url.group().decode("ascii"))

    assert asset.status_code == 200
    assert asset["Content-Type"] == "image/png"
    assert asset["Cache-Control"] == "public, max-age=31536000, immutable"
    assert asset["X-Content-Type-Options"] == "nosniff"

    hero = preview_root / "2026-08-28" / "sample" / "assets" / "hero.png"
    hero.write_bytes(hero.read_bytes() + b"tampered")
    assert client.get(asset_url.group().decode("ascii")).status_code == 404


@override_settings(IS_LOCAL_RUNTIME=True)
def test_preview_rejects_remote_clients_traversal_and_non_local_mode(
    client,
    settings,
    preview_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.LOCAL_ARTICLE_ROOT = preview_root

    assert client.get("/local-articles/", REMOTE_ADDR="192.0.2.10").status_code == 404
    assert (
        client.get(
            "/local-articles/assets/2026-08-28/sample/"
            + "0" * 64
            + "/../manifest.json"
        ).status_code
        == 404
    )

    from apps.local_content import views

    settings.IS_LOCAL_RUNTIME = False
    monkeypatch.setattr(
        views,
        "_list_validated_runs",
        lambda: (_ for _ in ()).throw(AssertionError("filesystem access must be gated")),
    )
    assert client.get("/local-articles/").status_code == 404
    assert client.get("/api/v1/local-articles/status").status_code == 404


@override_settings(IS_LOCAL_RUNTIME=True)
def test_preview_status_is_loopback_only_and_never_cached(
    client,
    settings,
    preview_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.LOCAL_ARTICLE_ROOT = preview_root
    monkeypatch.setattr(
        "apps.local_content.views._humanizer_status",
        lambda: {"status": "ready", "endpoint": "http://127.0.0.1:3210"},
    )

    response = client.get("/api/v1/local-articles/status")

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response["Referrer-Policy"] == "no-referrer"
    assert response.json()["workflow"]["validRunCount"] == 2
    assert response.json()["humanizer"]["status"] == "ready"
    assert (
        client.get("/api/v1/local-articles/status", REMOTE_ADDR="198.51.100.5").status_code
        == 404
    )


def test_markdown_renderer_escapes_raw_html() -> None:
    from apps.local_content.views import render_markdown_preview

    rendered = str(
        render_markdown_preview(
            "# 제목\n\n<script>alert('x')</script>\n",
            run_name="2026-08-28",
            article_name="sample",
            article_files={},
            final_articles=frozenset(),
        )
    )

    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in rendered


@pytest.mark.parametrize(
    ("header", "replacement"),
    [
        ("content-security-policy", "default-src 'self'"),
        ("referrer-policy", "same-origin"),
        ("x-content-type-options", "off"),
        ("x-frame-options", "SAMEORIGIN"),
        ("cache-control", "max-age=0"),
    ],
)
def test_acceptance_requires_exact_preview_security_headers(
    header: str,
    replacement: str,
) -> None:
    headers = {
        "content-security-policy": (
            "default-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'; object-src 'none'; img-src 'self'; "
            "style-src 'self'"
        ),
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "cache-control": "no-store",
    }
    assert preview_headers_match(headers, cache_control="no-store") is True
    headers[header] = replacement
    assert preview_headers_match(headers, cache_control="no-store") is False


@pytest.mark.parametrize("payload", [None, [], "ready", 1])
def test_humanizer_status_treats_non_object_json_as_unavailable(
    settings,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    from apps.local_content import views

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return payload

    settings.HUMANIZER_BASE_URL = "http://127.0.0.1:3210"
    monkeypatch.setattr(views.httpx, "get", lambda *_args, **_kwargs: Response())

    assert views._humanizer_status()["status"] == "unavailable"


def test_humanizer_status_treats_malformed_json_as_unavailable(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.local_content import views

    class Response:
        status_code = 200

        @staticmethod
        def json():
            raise ValueError("malformed")

    settings.HUMANIZER_BASE_URL = "http://127.0.0.1:3210"
    monkeypatch.setattr(views.httpx, "get", lambda *_args, **_kwargs: Response())

    assert views._humanizer_status()["status"] == "unavailable"
