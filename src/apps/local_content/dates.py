from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .contracts import CollectionWindow

SEOUL = ZoneInfo("Asia/Seoul")


def seven_day_window(now: datetime) -> CollectionWindow:
    observed = now.astimezone(SEOUL)
    start = datetime.combine(
        observed.date() - timedelta(days=6),
        time.min,
        tzinfo=SEOUL,
    )
    return CollectionWindow(start=start, end=observed)


def published_in_window(published_at: datetime, window: CollectionWindow) -> bool:
    return window.contains_publication(published_at)
