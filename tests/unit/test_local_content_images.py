from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

import apps.local_content.images as image_module
from apps.local_content.contracts import HousingNotice
from apps.local_content.images import (
    GENERIC_HERO_CAPTION,
    GENERIC_HERO_PATH,
    GENERIC_HERO_SHA256,
    ImageRenderError,
    _timeline_value_lines,
    build_image_set,
    canonical_card_renderer_input,
    card_renderer_material,
    fingerprint_renderer_material,
    render_summary_card,
    render_timeline,
    rerender_card_bytes,
)

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
        "application_start": date(2026, 9, 7),
        "application_end": date(2026, 9, 9),
        "announcement_date": date(2026, 9, 16),
        "supply_count": 1147,
        "price_summary": "공고문 기준",
        "source_checksum": "a" * 64,
    }
    fields.update(overrides)
    return HousingNotice(**fields)  # type: ignore[arg-type]


def test_pillow_cards_are_deterministic_1200_by_630_webp(tmp_path: Path) -> None:
    first = render_summary_card(_notice(), tmp_path / "summary-a.webp")
    second = render_summary_card(_notice(), tmp_path / "summary-b.webp")
    timeline = render_timeline(_notice(), tmp_path / "timeline.webp")

    assert first.sha256 == second.sha256
    for asset in (first, timeline):
        assert asset.mime_type == "image/webp"
        assert (asset.width, asset.height) == (1200, 630)
        assert asset.path.stat().st_size < 200_000
        with Image.open(asset.path) as image:
            assert image.size == (1200, 630)
            assert image.format == "WEBP"


def test_unknown_timeline_dates_are_not_inferred(tmp_path: Path) -> None:
    asset = render_timeline(
        _notice(application_start=None, application_end=None, announcement_date=None),
        tmp_path / "timeline.webp",
    )

    assert "공고문에서 직접 확인 필요" in asset.alt
    assert asset.source == "정규화된 공식 공고 사실"


def test_unknown_timeline_value_wraps_without_losing_mandated_wording() -> None:
    lines = _timeline_value_lines("공고문에서 직접 확인 필요")

    assert lines == ("공고문에서 직접", "확인 필요")
    assert " ".join(lines) == "공고문에서 직접 확인 필요"


def test_image_manifest_has_required_rights_and_accessibility_fields(tmp_path: Path) -> None:
    image_set = build_image_set(_notice(), tmp_path)

    assert [asset.bundle_path for asset in image_set.images] == [
        "assets/hero.png",
        "assets/summary-card.webp",
        "assets/timeline.webp",
    ]
    for asset in image_set.images:
        assert asset.sha256
        assert asset.mime_type in {"image/png", "image/webp"}
        assert asset.width > 0 and asset.height > 0
        assert asset.alt
        assert asset.caption
        assert asset.creator
        assert asset.source
        assert asset.rights_status in {"owned", "generated"}
        assert asset.rights_basis
    assert image_set.images[0].caption == GENERIC_HERO_CAPTION
    assert image_set.images[0].rights_status == "generated"
    assert all("attachment" not in str(asset.path).casefold() for asset in image_set.images)


def test_generic_hero_is_locked_to_the_exact_repository_asset(tmp_path: Path) -> None:
    arbitrary = tmp_path / "arbitrary.png"
    Image.new("RGB", (1200, 630), "white").save(arbitrary, format="PNG")

    with pytest.raises(ImageRenderError, match="exact repository asset"):
        build_image_set(_notice(), tmp_path / "cards", hero_path=arbitrary)

    image_set = build_image_set(_notice(), tmp_path / "approved")
    hero = image_set.images[0]
    assert hero.path.resolve() == GENERIC_HERO_PATH.resolve()
    assert hero.sha256 == GENERIC_HERO_SHA256


def test_card_manifest_binds_the_exact_font_and_renderer_material(tmp_path: Path) -> None:
    first = render_summary_card(_notice(), tmp_path / "first.webp")
    second = render_summary_card(_notice(), tmp_path / "second.webp")
    timeline = render_timeline(_notice(), tmp_path / "timeline.webp")

    assert first.sha256 == second.sha256
    assert first.renderer_fingerprint == second.renderer_fingerprint
    assert timeline.renderer_fingerprint == first.renderer_fingerprint
    assert re.fullmatch(r"[0-9a-f]{64}", first.renderer_fingerprint)


def test_renderer_fingerprint_binds_native_freetype_and_libwebp_versions() -> None:
    material = card_renderer_material()
    identical = dict(material)
    changed_freetype = {**material, "freetype_version": "changed-freetype"}
    changed_webp = {**material, "libwebp_version": "changed-libwebp"}

    assert material["freetype_version"]
    assert material["libwebp_version"]
    assert fingerprint_renderer_material(material) == fingerprint_renderer_material(identical)
    assert fingerprint_renderer_material(material) != fingerprint_renderer_material(
        changed_freetype
    )
    assert fingerprint_renderer_material(material) != fingerprint_renderer_material(changed_webp)


@pytest.mark.parametrize(
    ("key", "changed"),
    [
        ("image_mode", "RGBA"),
        ("exif_hex", "00"),
        ("xmp_hex", "00"),
        ("icc_profile_hex", "00"),
        ("text_layout_engine", "RAQM"),
    ],
)
def test_renderer_fingerprint_binds_mode_metadata_and_text_layout(
    key: str, changed: str
) -> None:
    material = card_renderer_material()

    assert key in material
    assert fingerprint_renderer_material(material) != fingerprint_renderer_material(
        {**material, key: changed}
    )


def test_summary_alt_describes_every_meaningful_visible_fact(tmp_path: Path) -> None:
    summary = render_summary_card(_notice(), tmp_path / "summary.webp")
    unknown = render_summary_card(
        _notice(application_start=None, price_summary=None),
        tmp_path / "summary-unknown.webp",
    )

    for expected in ("서울특별시", "1,147세대", "2026-09-07", "공고문 기준"):
        assert expected in summary.alt
    assert unknown.alt.count("공고문에서 직접 확인 필요") >= 2


def test_timeline_alt_describes_all_four_visible_schedule_fields(tmp_path: Path) -> None:
    timeline = render_timeline(_notice(deadline=date(2026, 9, 10)), tmp_path / "timeline.webp")

    for expected in ("2026-09-07", "2026-09-09", "2026-09-10", "2026-09-16"):
        assert expected in timeline.alt


def test_whitespace_title_is_unknown_in_card_pixels_and_alt(tmp_path: Path) -> None:
    summary = render_summary_card(_notice(title=" \t"), tmp_path / "summary.webp")
    timeline = render_timeline(_notice(title=" \n"), tmp_path / "timeline.webp")

    assert summary.alt.startswith("공고문에서 직접 확인 필요")
    assert timeline.alt.startswith("공고문에서 직접 확인 필요")


def test_card_renderer_input_requires_exact_canonical_summary_values() -> None:
    canonical = {
        "kind": "summary",
        "title": "서울 해오름 A단지 주택공급",
        "region": "서울특별시",
        "supply": "1,147세대",
        "application_start": "2026-09-07",
        "price_summary": "공고문 기준",
    }

    assert canonical_card_renderer_input(canonical) == tuple(canonical.items())

    invalid_values = (
        {**canonical, "extra": "attacker-controlled"},
        {**canonical, "title": 123},
        {**canonical, "title": ""},
        {**canonical, "title": "서울\n주택"},
        {**canonical, "region": " 서울특별시 "},
        {**canonical, "price_summary": "공고문\u200b 기준"},
        {**canonical, "supply": "1147세대"},
        {**canonical, "supply": "-1세대"},
        {**canonical, "supply": "999,999,999,999세대"},
        {**canonical, "application_start": "2026-9-7"},
        {**canonical, "application_start": "2026-02-30"},
    )
    for invalid in invalid_values:
        with pytest.raises(ImageRenderError, match="renderer input"):
            canonical_card_renderer_input(invalid)


def test_card_renderer_input_enforces_field_and_total_resource_limits() -> None:
    base = {
        "kind": "summary",
        "title": "서울 주택공급",
        "region": "서울특별시",
        "supply": "1세대",
        "application_start": "2026-09-07",
        "price_summary": "공고문 기준",
    }

    for field in ("title", "region", "price_summary"):
        with pytest.raises(ImageRenderError, match="limit"):
            canonical_card_renderer_input({**base, field: "가" * 10_000})

    byte_heavy = {**base, "title": "😀" * 2_000}
    with pytest.raises(ImageRenderError, match="limit"):
        canonical_card_renderer_input(byte_heavy)

    aggregate_codepoint_heavy = {
        **base,
        "title": "가" * 180,
        "region": "나" * 90,
        "price_summary": "다" * 180,
    }
    with pytest.raises(ImageRenderError, match="total resource limit"):
        canonical_card_renderer_input(aggregate_codepoint_heavy)

    aggregate_byte_heavy = {
        **base,
        "title": "😀" * 150,
        "region": "😀" * 75,
        "price_summary": "😀" * 150,
    }
    with pytest.raises(ImageRenderError, match="total resource limit"):
        canonical_card_renderer_input(aggregate_byte_heavy)


def test_invalid_renderer_input_is_rejected_before_entering_rerender_cache() -> None:
    image_module._rerender_card_bytes_cached.cache_clear()
    malformed = (
        ("kind", "timeline"),
        ("title", "서울 주택공급"),
        ("application_start", "not-a-date"),
        ("application_end", "2026-09-09"),
        ("deadline", "공고문에서 직접 확인 필요"),
        ("announcement_date", "2026-09-16"),
    )

    with pytest.raises(ImageRenderError, match="renderer input"):
        rerender_card_bytes(malformed)

    assert image_module._rerender_card_bytes_cached.cache_info().currsize == 0
