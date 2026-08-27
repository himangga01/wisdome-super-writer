from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

from apps.local_content.contracts import HousingNotice
from apps.local_content.images import (
    GENERIC_HERO_CAPTION,
    _timeline_value_lines,
    build_image_set,
    render_summary_card,
    render_timeline,
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
