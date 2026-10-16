"""Окно публикаций: 10:00–22:00 Asia/Yekaterinburg, пауза 2–3 часа."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().opros_timezone or "Asia/Yekaterinburg")


def now_local() -> datetime:
    return datetime.now(tz())


def is_quiet(dt: datetime | None = None) -> bool:
    """True с 22:00 до 10:00 по Екатеринбургу (не публикуем)."""
    settings = get_settings()
    current = dt.astimezone(tz()) if dt else now_local()
    start = int(settings.opros_quiet_start_hour)
    end = int(settings.opros_quiet_end_hour)
    hour = current.hour
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def next_window_start(dt: datetime | None = None) -> datetime:
    """Ближайшие 10:00, если сейчас ночное окно."""
    settings = get_settings()
    current = dt.astimezone(tz()) if dt else now_local()
    end = int(settings.opros_quiet_end_hour)
    candidate = current.replace(hour=end, minute=0, second=0, microsecond=0)
    if current >= candidate:
        candidate += timedelta(days=1)
    return candidate


def random_interval_sec() -> int:
    settings = get_settings()
    lo = max(60, int(settings.opros_min_interval_sec))
    hi = max(lo, int(settings.opros_max_interval_sec))
    return random.randint(lo, hi)


def next_publish_at(after: datetime | None = None, interval_sec: int | None = None) -> datetime:
    """Следующий слот: now + 2–3 ч, сдвинутый из ночного окна на 10:00."""
    current = after.astimezone(tz()) if after else now_local()
    wait = interval_sec if interval_sec is not None else random_interval_sec()
    candidate = current + timedelta(seconds=int(wait))
    if is_quiet(candidate):
        return next_window_start(candidate)
    return candidate


def seconds_until(target: datetime, now: datetime | None = None) -> float:
    current = now.astimezone(tz()) if now else now_local()
    delta = target.astimezone(tz()) - current
    return max(0.0, delta.total_seconds())
