"""Explicit admission and detail-selection policy for local housing notices."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection

from apps.local_content.contracts import HousingNotice

_WHITESPACE = re.compile(r"\s+")

# Values are normalized official categories, never free-form title fragments.  The
# collectors currently emit ``apt``, ``remaining``, and Korean LH labels, while
# the aliases cover the other approved official category labels.
_CATEGORY_ALIASES = {
    "apt": "sale",
    "apartment": "sale",
    "sale": "sale",
    "분양주택": "sale",
    "민간분양": "sale",
    "민영주택": "sale",
    "public_sale": "public_sale",
    "공공분양": "public_sale",
    "공공분양주택": "public_sale",
    "remaining": "remaining",
    "remaining_supply": "remaining",
    "잔여세대": "remaining",
    "무순위": "remaining",
    "임의공급": "remaining",
    "취소분": "remaining",
    "불법행위재공급": "remaining",
    "optional_supply": "remaining",
    "공공임대": "public_rental",
    "공공임대주택": "public_rental",
    "임대주택": "public_rental",
    "국민임대": "national_rental",
    "국민임대주택": "national_rental",
    "영구임대": "permanent_rental",
    "영구임대주택": "permanent_rental",
    "통합공공임대": "integrated_public_rental",
    "통합공공임대주택": "integrated_public_rental",
    "행복주택": "happy_housing",
    "매입임대": "purchase_lease",
    "매입임대주택": "purchase_lease",
    "전세임대": "purchase_lease",
    "전세임대주택": "purchase_lease",
    "purchase_lease": "purchase_lease",
    "토지": "non_residential",
    "공장": "non_residential",
    "공장용지": "non_residential",
    "산업시설용지": "non_residential",
    "종교시설용지": "non_residential",
    "종교용지": "non_residential",
    "주차장": "non_residential",
    "주차장용지": "non_residential",
    "상가": "non_residential",
    "공공임대상가(추첨)": "non_residential",
    "어린이집": "non_residential",
    "어린이집 운영자": "non_residential",
    "비주거용 경매": "non_residential",
    "비주거 경매": "non_residential",
    "non_residential_auction": "non_residential",
}
_RESIDENTIAL_CATEGORIES = frozenset(
    {
        "sale",
        "public_sale",
        "remaining",
        "public_rental",
        "national_rental",
        "permanent_rental",
        "integrated_public_rental",
        "happy_housing",
        "purchase_lease",
    }
)
_DETAIL_CATEGORIES = frozenset({"sale", "public_sale"})
_DETAIL_TITLE_KEYWORDS = (
    "무순위",
    "잔여세대",
    "임의공급",
    "취소분",
    "불법행위재공급",
)


def normalized_category(category: str) -> str | None:
    """Return the canonical policy category for one exact official category label."""

    normalized = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", category)).strip().casefold()
    return _CATEGORY_ALIASES.get(normalized)


def is_residential(notice: HousingNotice) -> bool:
    """Whether a notice belongs in the weekly residential index."""

    return normalized_category(notice.category) in _RESIDENTIAL_CATEGORIES


def needs_detailed_article(
    notice: HousingNotice,
    selected_ids: Collection[str] = (),
) -> bool:
    """Whether the admitted notice receives an individual detailed article."""

    category = normalized_category(notice.category)
    if category not in _RESIDENTIAL_CATEGORIES:
        return False
    if notice.external_id in selected_ids or category in _DETAIL_CATEGORIES:
        return True
    title = unicodedata.normalize("NFKC", notice.title)
    return any(keyword in title for keyword in _DETAIL_TITLE_KEYWORDS)
