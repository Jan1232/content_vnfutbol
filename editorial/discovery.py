"""Fetch fresh news for an editorial channel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db import db, get_meta, set_meta
from editorial.channel_config import EditorialChannelConfig
from editorial.models import NewsItem, utcnow
from editorial.sources import DEFAULT_FEEDS, fetch_feed
from editorial.topic_gate import classify_event_rules


def _cursor_key(slug: str, feed_name: str) -> str:
    return f"editorial_feed:{slug}:{feed_name}"


def _bootstrap_key(slug: str, feed_name: str) -> str:
    return f"editorial_boot:{slug}:{feed_name}"


def fetch_fresh_news(channel: EditorialChannelConfig) -> list[NewsItem]:
    settings = get_settings()
    freshness = timedelta(seconds=int(settings.editorial_freshness_sec or 900))
    cutoff = utcnow() - freshness
    feeds = channel.feeds or DEFAULT_FEEDS
    out: list[NewsItem] = []

    for feed in feeds:
        try:
            items = fetch_feed(feed)
        except Exception as e:
            print(f"[editorial] feed {feed.name} fail: {e}", flush=True)
            continue
        if not items:
            continue

        boot_key = _bootstrap_key(channel.slug, feed.name)
        with db() as conn:
            bootstrapped = bool(get_meta(conn, boot_key, ""))

        if not bootstrapped:
            latest = max(items, key=lambda i: i.published_at or datetime.min.replace(tzinfo=timezone.utc))
            with db() as conn:
                set_meta(conn, boot_key, "1")
                set_meta(conn, _cursor_key(channel.slug, feed.name), latest.external_id)
            print(
                f"[editorial] bootstrap {channel.slug}/{feed.name}: skip {len(items)} historical",
                flush=True,
            )
            continue

        batch: list[NewsItem] = []
        for item in items:
            published = item.published_at
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if published < cutoff:
                continue
            if not item.event_type or item.event_type == "other":
                item.event_type = classify_event_rules(f"{item.title}\n{item.body}")
            if channel.competitions and item.competition:
                if item.competition not in channel.competitions and item.competition not in {
                    "CL",
                    "EL",
                    "ECL",
                    "WC",
                    "EC",
                    "UNL",
                    "NT",
                }:
                    continue
            batch.append(item)

        if (feed.kind or "").lower() in {"telegram"} and batch:
            from editorial.story_throttle import channel_day
            from editorial.store import count_meme_source_today

            # per-feed max_per_day, иначе глобальный; 0 у обоих = без лимита
            cap = int(getattr(feed, "max_per_day", 0) or 0)
            if cap <= 0:
                cap = int(getattr(settings, "meme_source_max_per_day", 5) or 0)
            if cap > 0:
                day = channel_day()
                used = count_meme_source_today(channel.slug, day=day, source=feed.name)
                left = max(0, cap - used)
                if left <= 0:
                    batch = []
                else:
                    batch = batch[-left:]
        out.extend(batch)
    return out
