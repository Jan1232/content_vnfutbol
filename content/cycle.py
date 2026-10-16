"""Синхронизация каталога и одна публикация."""

from __future__ import annotations

import time
from typing import Any

from app.config import get_settings
from app.max_api import MaxClient
from content.db import (
    count_series,
    db,
    get_meta,
    init_db,
    load_genres,
    mark_published,
    pick_unpublished,
    set_meta,
    upsert_series,
)
from content.parser import SeriesItem, fetch_catalog, fetch_series_details
from content.publish import extract_message_id, publish_series
from content.schedule import is_quiet, next_publish_at, next_window_start, now_local


def _row_to_item(row: Any) -> SeriesItem:
    return SeriesItem(
        id=str(row["id"]),
        slug=str(row["slug"] or ""),
        title=str(row["title"]),
        year=int(row["year"]) if row["year"] is not None else None,
        seasons=int(row["seasons"]) if row["seasons"] is not None else None,
        description=str(row["description"] or ""),
        poster_url=str(row["poster_url"] or ""),
        url=str(row["url"]),
        genres=load_genres(row["genres"] if "genres" in row.keys() else ()),
    )


def _due_ts() -> float | None:
    with db() as conn:
        raw = get_meta(conn, "next_publish_at")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def sync_catalog() -> dict[str, int]:
    init_db()
    items = fetch_catalog()
    with db() as conn:
        for item in items:
            upsert_series(conn, item.as_dict())
        set_meta(conn, "last_sync_at", str(time.time()))
        total, unpublished = count_series(conn)
    print(
        f"[content] catalog synced items={len(items)} total={total} unpublished={unpublished}",
        flush=True,
    )
    return {"fetched": len(items), "total": total, "unpublished": unpublished}


def maybe_sync(*, force: bool = False) -> dict[str, int] | None:
    settings = get_settings()
    init_db()
    with db() as conn:
        total, _ = count_series(conn)
        last_raw = get_meta(conn, "last_sync_at")
    last = float(last_raw) if last_raw else 0.0
    stale = (time.time() - last) >= max(300, int(settings.content_sync_interval_sec))
    if force or total == 0 or stale:
        return sync_catalog()
    return None


def enrich_item(item: SeriesItem) -> SeriesItem:
    try:
        extra = fetch_series_details(item.url)
    except Exception as e:
        print(f"[content] details fail {item.url}: {e}", flush=True)
        return item
    desc = extra.get("description") or item.description
    poster = extra.get("poster_url") or item.poster_url
    genres = tuple(extra.get("genres") or item.genres)
    if desc == item.description and poster == item.poster_url and genres == item.genres:
        return item
    return SeriesItem(
        id=item.id,
        slug=item.slug,
        title=item.title,
        year=item.year,
        seasons=item.seasons,
        description=desc,
        poster_url=poster,
        url=item.url,
        genres=genres,
    )


def publish_one(
    client: MaxClient | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    init_db()
    settings = get_settings()
    now = now_local()
    if not force and is_quiet(now):
        nxt = next_window_start(now)
        with db() as conn:
            set_meta(conn, "next_publish_at", str(nxt.timestamp()))
        return {
            "action": "quiet",
            "until": nxt.isoformat(),
        }

    due = _due_ts()
    if not force and due is not None and time.time() < due:
        return {"action": "wait", "until_ts": due}

    maybe_sync()
    with db() as conn:
        row = pick_unpublished(conn)
        total, unpublished = count_series(conn)
    if row is None:
        print("[content] catalog exhausted, resync", flush=True)
        sync_catalog()
        with db() as conn:
            row = pick_unpublished(conn)
            total, unpublished = count_series(conn)
        if row is None:
            nxt = next_publish_at()
            with db() as conn:
                set_meta(conn, "next_publish_at", str(nxt.timestamp()))
            return {
                "action": "empty",
                "total": total,
                "unpublished": unpublished,
                "next": nxt.isoformat(),
            }

    item = enrich_item(_row_to_item(row))
    own_client = client is None
    client = client or MaxClient()
    try:
        sent = publish_series(
            client,
            item,
            chat_id=int(settings.content_chat_id),
            button_text=settings.content_button_text,
            ref=settings.content_ref,
        )
    finally:
        if own_client:
            client.close()

    mid = extract_message_id(sent.get("response") if isinstance(sent, dict) else None)
    nxt = next_publish_at()
    with db() as conn:
        mark_published(
            conn,
            item.id,
            message_id=mid,
            description=item.description,
            genres=item.genres,
        )
        set_meta(conn, "next_publish_at", str(nxt.timestamp()))
        set_meta(conn, "last_published_id", item.id)
        _, unpublished = count_series(conn)
    print(
        f"[content] published {item.title!r} id={item.id} mid={mid or '-'} "
        f"next={nxt.isoformat()} left={unpublished}",
        flush=True,
    )
    return {
        "action": "published",
        "id": item.id,
        "title": item.title,
        "url": sent["watch_url"],
        "message_id": mid,
        "next": nxt.isoformat(),
        "unpublished": unpublished,
    }


def seconds_until_next_action() -> float:
    now = now_local()
    if is_quiet(now):
        return max(30.0, (next_window_start(now) - now).total_seconds())
    due = _due_ts()
    if due is None:
        return 0.0
    return max(0.0, due - time.time())
