"""Rights-safe image manifests and deterministic Pillow article cards."""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL import __version__ as PILLOW_VERSION

from apps.local_content.contracts import HousingNotice
from apps.local_content.rendering import GENERIC_HERO_CAPTION, UNKNOWN_VALUE

CARD_SIZE = (1200, 630)
GENERIC_HERO_PATH = (
    Path(__file__).resolve().parents[2] / "static" / "local_articles" / "generic-housing-hero.png"
)
GENERIC_HERO_SHA256 = "885a39f027ee1693147840039bb70624bd26333bcb49f014461250c86d6047cd"
_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/malgun.ttf"),
    Path("C:/Windows/Fonts/malgunbd.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansKR-Regular.otf"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
)


class ImageRenderError(RuntimeError):
    """Raised when a deterministic, rights-safe image cannot be produced."""


@dataclass(frozen=True)
class ArticleImage:
    """Complete manifest material for one owned or generated image."""

    path: Path
    bundle_path: str
    sha256: str
    mime_type: str
    width: int
    height: int
    alt: str
    caption: str
    creator: str
    source: str
    source_url: str | None
    rights_status: str
    rights_basis: str
    attribution: str
    renderer_fingerprint: str

    def as_manifest(self, *, path: str | None = None) -> dict[str, object]:
        return {
            "path": path or self.bundle_path,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "alt": self.alt,
            "caption": self.caption,
            "creator": self.creator,
            "source": self.source,
            "source_url": self.source_url,
            "rights_status": self.rights_status,
            "rights_basis": self.rights_basis,
            "attribution": self.attribution,
            "renderer_fingerprint": self.renderer_fingerprint,
        }


@dataclass(frozen=True)
class ImageSet:
    images: tuple[ArticleImage, ...]


def build_image_set(
    notice: HousingNotice,
    output_dir: Path,
    *,
    hero_path: Path = GENERIC_HERO_PATH,
) -> ImageSet:
    """Build the generic hero manifest and deterministic factual cards."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    hero = _generic_hero(Path(hero_path))
    summary = render_summary_card(notice, output_dir / "summary-card.webp")
    timeline = render_timeline(notice, output_dir / "timeline.webp")
    return ImageSet(images=(hero, summary, timeline))


def render_summary_card(notice: HousingNotice, output_path: Path) -> ArticleImage:
    """Render a stable 1200x630 WebP overview from normalized facts only."""

    output_path = _card_path(output_path, "summary-card.webp")
    image = Image.new("RGB", CARD_SIZE, "#F4F6F2")
    draw = ImageDraw.Draw(image)
    regular = _font(31)
    small = _font(25)
    title_font = _font(48, bold=True)
    label_font = _font(23, bold=True)

    draw.rounded_rectangle((58, 52, 1142, 578), radius=34, fill="#FFFFFF", outline="#DDE4DB")
    draw.text((92, 86), "주거 공고 한눈에 보기", font=label_font, fill="#24705B")
    notice_title = _known(notice.title)
    title = _fit_text(draw, notice_title, title_font, 990)
    draw.text((92, 132), title, font=title_font, fill="#15251F")
    draw.line((92, 216, 1108, 216), fill="#DDE4DB", width=3)

    supply = (
        f"{notice.supply_count:,}세대"
        if notice.supply_count is not None
        else UNKNOWN_VALUE
    )
    facts = (
        ("지역", _known(notice.region)),
        ("공급 규모", supply),
        ("신청 시작", _date(notice.application_start)),
        ("가격·보증금·임대료", _known(notice.price_summary)),
    )
    for index, (label, value) in enumerate(facts):
        column = index % 2
        row = index // 2
        left = 92 + column * 510
        top = 254 + row * 132
        draw.text((left, top), label, font=label_font, fill="#60706A")
        display = _fit_text(draw, value, regular, 455)
        draw.text((left, top + 42), display, font=regular, fill="#15251F")
    draw.text(
        (92, 528),
        "정규화된 공식 공고 정보 · 신청 전 원문 재확인",
        font=small,
        fill="#60706A",
    )
    _save_webp(image, output_path)
    return _card_manifest(
        output_path,
        bundle_path="assets/summary-card.webp",
        alt=(
            f"{notice_title} 요약 카드. 지역 {_known(notice.region)}, "
            f"공급 규모 {supply}, 신청 시작 {_date(notice.application_start)}, "
            f"가격·보증금·임대료 {_known(notice.price_summary)}."
        ),
        caption="정규화된 공식 공고 사실로 만든 요약 이미지",
    )


def render_timeline(notice: HousingNotice, output_path: Path) -> ArticleImage:
    """Render a stable 1200x630 WebP timeline without filling unknown dates."""

    output_path = _card_path(output_path, "timeline.webp")
    image = Image.new("RGB", CARD_SIZE, "#F4F6F2")
    draw = ImageDraw.Draw(image)
    title_font = _font(45, bold=True)
    label_font = _font(23, bold=True)
    date_font = _font(24)
    note_font = _font(22)

    draw.rounded_rectangle((58, 52, 1142, 578), radius=34, fill="#FFFFFF", outline="#DDE4DB")
    draw.text((92, 86), "신청 일정", font=title_font, fill="#15251F")
    draw.text(
        (92, 151),
        _fit_text(draw, _known(notice.title), note_font, 990),
        font=note_font,
        fill="#60706A",
    )

    events = (
        ("신청 시작", _date(notice.application_start)),
        ("신청 종료", _date(notice.application_end)),
        ("마감일", _date(notice.deadline)),
        ("당첨자 발표", _date(notice.announcement_date)),
    )
    centers = (155, 445, 735, 1025)
    draw.line((centers[0], 285, centers[-1], 285), fill="#AAC8BC", width=8)
    for center, (label, value) in zip(centers, events, strict=True):
        draw.ellipse((center - 17, 268, center + 17, 302), fill="#24705B")
        label_width = draw.textlength(label, font=label_font)
        draw.text((center - label_width / 2, 326), label, font=label_font, fill="#24705B")
        for line_index, line in enumerate(_timeline_value_lines(value)):
            value_width = draw.textlength(line, font=date_font)
            draw.text(
                (center - value_width / 2, 372 + line_index * 32),
                line,
                font=date_font,
                fill="#15251F",
            )
    draw.text(
        (92, 523),
        "날짜가 비어 있으면 공고문에서 직접 확인 필요",
        font=note_font,
        fill="#60706A",
    )
    _save_webp(image, output_path)
    alt_events = ", ".join(f"{label} {value}" for label, value in events)
    return _card_manifest(
        output_path,
        bundle_path="assets/timeline.webp",
        alt=f"{_known(notice.title)} 신청 일정. {alt_events}.",
        caption="정규화된 공식 공고 날짜로 만든 신청 일정 이미지",
    )


def card_renderer_fingerprint() -> str:
    """Return the hash-bound Pillow/font/encoding material for derived cards."""

    return _renderer_fingerprint()


def _generic_hero(path: Path) -> ArticleImage:
    approved = Path(os.path.abspath(GENERIC_HERO_PATH))
    candidate = Path(os.path.abspath(path))
    try:
        resolved_approved = GENERIC_HERO_PATH.resolve(strict=True)
        resolved_candidate = path.resolve(strict=True)
    except OSError as exc:
        raise ImageRenderError("generic hero repository asset is unavailable") from exc
    if candidate != approved or resolved_candidate != resolved_approved:
        raise ImageRenderError("generic hero must be the exact repository asset")
    if not path.is_file() or path.is_symlink():
        raise ImageRenderError(f"generic hero is unavailable or unsafe: {path}")
    checksum = _sha256(path)
    if checksum != GENERIC_HERO_SHA256:
        raise ImageRenderError("generic hero SHA-256 does not match the repository asset")
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise ImageRenderError("generic hero must be a PNG image")
            width, height = image.size
            image.verify()
    except (OSError, ValueError) as exc:
        raise ImageRenderError("generic hero is not a valid PNG image") from exc
    return ArticleImage(
        path=path,
        bundle_path="assets/hero.png",
        sha256=checksum,
        mime_type="image/png",
        width=width,
        height=height,
        alt="주거 공고 이해를 위한 일반적인 현대 한국 아파트 도시 전경",
        caption=GENERIC_HERO_CAPTION,
        creator="OpenAI ImageGen",
        source="이 저장소를 위해 생성한 일반 주거 이미지",
        source_url=None,
        rights_status="generated",
        rights_basis="이 저장소 전용 생성 이미지이며 공식 공고 첨부물을 사용하지 않음",
        attribution="OpenAI ImageGen으로 생성",
        renderer_fingerprint=hashlib.sha256(
            f"openai-imagegen:{GENERIC_HERO_SHA256}".encode("ascii")
        ).hexdigest(),
    )


def _card_manifest(
    output_path: Path,
    *,
    bundle_path: str,
    alt: str,
    caption: str,
) -> ArticleImage:
    return ArticleImage(
        path=output_path,
        bundle_path=bundle_path,
        sha256=_sha256(output_path),
        mime_type="image/webp",
        width=CARD_SIZE[0],
        height=CARD_SIZE[1],
        alt=alt,
        caption=caption,
        creator="Wisdome Super Writer / Pillow",
        source="정규화된 공식 공고 사실",
        source_url=None,
        rights_status="owned",
        rights_basis="저장소 코드가 정규화된 사실만으로 직접 렌더링함",
        attribution="Wisdome Super Writer",
        renderer_fingerprint=_renderer_fingerprint(),
    )


def _save_webp(image: Image.Image, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(
        output_path,
        format="WEBP",
        quality=82,
        method=6,
        exact=True,
        exif=b"",
        xmp=b"",
        icc_profile=b"",
    )


def _card_path(path: Path, filename: str) -> Path:
    path = Path(path)
    if path.exists() and path.is_dir():
        return path / filename
    return path


@lru_cache(maxsize=1)
def _font_paths() -> tuple[Path, Path]:
    regular = next((path for path in _FONT_CANDIDATES if path.is_file()), None)
    if regular is None:
        candidates = ", ".join(str(path) for path in _FONT_CANDIDATES)
        raise ImageRenderError(f"no Korean-capable font found; checked: {candidates}")
    bold_candidate = Path("C:/Windows/Fonts/malgunbd.ttf")
    bold = bold_candidate if bold_candidate.is_file() else regular
    return regular, bold


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    regular, bold_path = _font_paths()
    path = bold_path if bold else regular
    try:
        return ImageFont.truetype(str(path), size=size)
    except OSError as exc:
        raise ImageRenderError(f"Korean font could not be loaded: {path}") from exc


@lru_cache(maxsize=1)
def _renderer_fingerprint() -> str:
    regular, bold = _font_paths()
    material = {
        "pillow_version": PILLOW_VERSION,
        "regular_font_sha256": _sha256(regular),
        "bold_font_sha256": _sha256(bold),
        "format": "WEBP",
        "quality": 82,
        "method": 6,
        "exact": True,
        "size": CARD_SIZE,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fit_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    normalized = " ".join(value.split())
    if draw.textlength(normalized, font=font) <= max_width:
        return normalized
    suffix = "…"
    candidate = normalized
    while candidate and draw.textlength(candidate + suffix, font=font) > max_width:
        candidate = candidate[:-1]
    return candidate.rstrip() + suffix


def _known(value: str | None) -> str:
    normalized = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in value or ""
    )
    normalized = " ".join(normalized.split())
    return normalized or UNKNOWN_VALUE


def _date(value: object | None) -> str:
    return value.isoformat() if value is not None else UNKNOWN_VALUE  # type: ignore[union-attr]


def _timeline_value_lines(value: str) -> tuple[str, ...]:
    if value == UNKNOWN_VALUE:
        return ("공고문에서 직접", "확인 필요")
    return (value,)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
