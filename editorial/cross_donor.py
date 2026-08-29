"""Cross-donor dedup: одна новость от нескольких TG-доноров → один пост."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.db import db, init_db
from editorial.models import NewsItem
from editorial.story_throttle import story_key


def _source_of(item: NewsItem | dict[str, Any]) -> str:
    if isinstance(item, NewsItem):
        return str(item.source or "")
    return str(item.get("source") or "")


def cross_donor_duplicate(channel_slug: str, item: NewsItem | dict[str, Any]) -> tuple[bool, str]:
    """True если тот же сюжет уже взят от другого донора в окне."""
    settings = get_settings()
    window_min = int(getattr(settings, "cross_donor_window_min", 180) or 180)
    if window_min <= 0:
        return False, ""
    key = story_key(item)
    source = _source_of(item)
    if not key or not source:
        return False, ""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
    init_db()
    with db() as conn:
        row = conn.execute(
            """
            SELECT source, id FROM editorial_news
            WHERE channel_slug=?
              AND status NOT IN ('rejected','skipped','filtered','off_topic')
              AND COALESCE(source_published_at, updated_at) >= ?
              AND (
                json_extract(entities_json, '$.story_key') = ?
                OR cluster_id = ?
              )
            ORDER BY id ASC
            LIMIT 1
            """,
            (channel_slug, cutoff, key, key),
        ).fetchone()
    if not row:
        return False, ""
    first_source = str(row["source"] or "")
    if first_source and first_source != source:
        return True, f"cross-donor duplicate (first={first_source})"
    return False, ""
