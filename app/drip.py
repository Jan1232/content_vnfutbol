"""Планировщик капельной публикации: N постов в сутки и пауза между ними."""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def _zone(tz_name: str) -> ZoneInfo:
    return ZoneInfo(tz_name or "Europe/Moscow")


def _local_dt(ts: float, zone: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ts, zone)


def _day_key(ts: float, zone: ZoneInfo):
    return _local_dt(ts, zone).date()


def _next_midnight(ts: float, zone: ZoneInfo) -> float:
    local = _local_dt(ts, zone)
    nxt = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


def parse_publish_times(times: list[str] | tuple[str, ...] | None) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for raw in times or ():
        parts = str(raw).strip().split(":")
        if len(parts) < 2:
            continue
        try:
            h = int(parts[0])
            m = int(parts[1])
        except ValueError:
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            pair = (h, m)
            if pair not in out:
                out.append(pair)
    out.sort()
    return out


def _slot_key(ts: float, zone: ZoneInfo, slots: list[tuple[int, int]], *, slack_sec: int = 120):
    if not slots:
        return None
    local = _local_dt(ts, zone)
    for h, m in slots:
        slot = local.replace(hour=h, minute=m, second=0, microsecond=0)
        if abs(slot.timestamp() - ts) <= slack_sec:
            return (local.date(), h, m)
    return None


def compute_next_slot_at(
    existing: list[float],
    *,
    now: float,
    publish_times: list[str] | tuple[str, ...] | None,
    tz_name: str = "Europe/Moscow",
) -> float:
    """Ближайший свободный фиксированный слот HH:MM в локальной зоне."""
    slots = parse_publish_times(publish_times)
    if not slots:
        return float(now)
    zone = _zone(tz_name)
    occupied = set()
    for ts in existing:
        try:
            stamp = float(ts)
        except (TypeError, ValueError):
            continue
        key = _slot_key(stamp, zone, slots)
        if key:
            occupied.add(key)
    local = _local_dt(now, zone)
    for day_off in range(0, 60):
        day_dt = local + timedelta(days=day_off)
        day = day_dt.date()
        for h, m in slots:
            cand = datetime(
                day.year, day.month, day.day, h, m, 0, 0, tzinfo=zone
            )
            ts = cand.timestamp()
            if ts + 1 < now:
                continue
            if (day, h, m) in occupied:
                continue
            return ts
    return float(now)


def compute_next_publish_at(
    existing: list[float],
    *,
    now: float,
    min_gap_sec: int,
    max_gap_sec: int,
    max_per_day: int,
    tz_name: str = "Europe/Moscow",
    rng: random.Random | None = None,
    publish_times: list[str] | tuple[str, ...] | None = None,
) -> float:
    """Следующий слот: фиксированные HH:MM либо случайная пауза и max_per_day."""
    if parse_publish_times(publish_times):
        return compute_next_slot_at(
            existing, now=now, publish_times=publish_times, tz_name=tz_name
        )

    zone = _zone(tz_name)
    stamps = sorted(float(x) for x in existing if x)
    last = stamps[-1] if stamps else 0.0
    lo = max(0, int(min_gap_sec or 0))
    hi = max(lo, int(max_gap_sec or lo))
    roll = rng.randint if rng is not None else random.randint
    if last <= 0:
        candidate = float(now)
    else:
        gap = roll(lo, hi) if hi else 0
        candidate = max(float(now), last + gap)

    if max_per_day <= 0:
        return candidate

    for _ in range(64):
        day = _day_key(candidate, zone)
        used = sum(1 for ts in stamps if _day_key(ts, zone) == day)
        if used < max_per_day:
            return candidate
        candidate = _next_midnight(candidate, zone)
        if last and candidate < last + lo:
            candidate = last + lo
        if candidate < now:
            candidate = float(now)
    return candidate


def list_source_schedule_times(conn, source_id: int, *, chat_id: int | None = None) -> list[float]:
    if chat_id is not None:
        rows = conn.execute(
            """
            SELECT COALESCE(p.publish_at, p.sent_at, p.created_at) AS ts
            FROM posts p
            JOIN sources s ON s.id = p.source_id
            WHERE s.chat_id=?
              AND p.is_ad=0
              AND p.publish_status IN ('pending', 'queued', 'sent')
            """,
            (int(chat_id),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT COALESCE(publish_at, sent_at, created_at) AS ts
            FROM posts
            WHERE source_id=?
              AND is_ad=0
              AND publish_status IN ('pending', 'queued', 'sent')
            """,
            (int(source_id),),
        ).fetchall()
    out: list[float] = []
    for row in rows:
        try:
            ts = float(row["ts"] or 0)
        except (TypeError, ValueError, KeyError):
            ts = 0.0
        if ts:
            out.append(ts)
    return out


def count_source_queue(conn, source_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM posts
        WHERE source_id=?
          AND is_ad=0
          AND publish_status IN ('pending', 'queued')
        """,
        (int(source_id),),
    ).fetchone()
    return int(row["n"] if row else 0)


def next_source_publish_at(
    conn,
    source_id: int,
    *,
    min_gap_sec: int,
    max_gap_sec: int,
    max_per_day: int,
    now: float | None = None,
    tz_name: str = "Europe/Moscow",
    publish_times: list[str] | tuple[str, ...] | None = None,
    chat_id: int | None = None,
) -> float:
    existing = list_source_schedule_times(conn, source_id, chat_id=chat_id)
    return compute_next_publish_at(
        existing,
        now=time.time() if now is None else now,
        min_gap_sec=min_gap_sec,
        max_gap_sec=max_gap_sec,
        max_per_day=max_per_day,
        tz_name=tz_name,
        publish_times=publish_times,
    )


def drip_queue_cap(max_per_day: int) -> int:
    """Сколько постов держать в очереди заранее (~неделя)."""
    per = max(1, int(max_per_day or 2))
    return per * 7
