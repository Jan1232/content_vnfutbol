"""Окно публикаций: 08:00–22:00 МСК, пауза 2–3 часа."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().content_timezone or "Europe/Moscow")


def now_local() -> datetime:
    return datetime.now(tz())


def is_quiet(dt: datetime | None = None) -> bool:
    """True с 22:00 до 08:00 по МСК (не публикуем)."""
    settings = get_settings()
    current = dt.astimezone(tz()) if dt else now_local()
    start = int(settings.content_quiet_start_hour)
    end = int(settings.content_quiet_end_hour)
    hour = current.hour
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def next_window_start(dt: datetime | None = None) -> datetime:
    """Ближайшие 08:00 МСК, если сейчас ночное окно."""
    settings = get_settings()
    current = dt.astimezone(tz()) if dt else now_local()
    end = int(settings.content_quiet_end_hour)
    candidate = current.replace(hour=end, minute=0, second=0, microsecond=0)
    if current >= candidate:
        candidate += timedelta(days=1)
    return candidate


def random_interval_sec() -> int:
    settings = get_settings()
    lo = max(60, int(settings.content_min_interval_sec))
    hi = max(lo, int(settings.content_max_interval_sec))
    return random.randint(lo, hi)


def next_publish_at(after: datetime | None = None, interval_sec: int | None = None) -> datetime:
    """Следующий слот: now + 2–3 ч, сдвинутый из ночного окна на 08:00."""
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
