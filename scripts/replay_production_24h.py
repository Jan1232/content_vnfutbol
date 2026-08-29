#!/usr/bin/env python3
"""Боевой прогон TG-доноров за последние N часов → полный pipeline → TG-модерация."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings, load_dotenv_manual
from app.db import db, init_db
from editorial.channel_config import get_channel, reload_editorial_channels
from editorial.cycle import advance_item, set_channel_enabled
from editorial.cross_donor import cross_donor_duplicate
from editorial.dedup import filter_new
from editorial.discovery import fetch_replay_window
from editorial.match_enrich import enrich_news_item
from editorial.moderation import dispatch_review_immediate, try_dispatch_memes
from editorial.store import get_news, insert_news, update_news
from editorial.topic_gate import cluster_id_for

TERMINAL = frozenset(
    {
        "ready",
        "held",
        "error",
        "off_topic",
        "skipped",
        "filtered",
        "rejected",
        "published",
        "awaiting_review",
        "deferred",
    }
)


def _entities_json(item) -> str:
    ent = dict(item.entities or {})
    if item.raw:
        ent["raw"] = item.raw
    return json.dumps(ent, ensure_ascii=False)


def _teams_json(item) -> str:
    return json.dumps((item.entities or {}).get("teams") or [], ensure_ascii=False)


def _ingest_replay(channel, items, *, run_id: str) -> list[int]:
    prefix = f"replay24h:{run_id}:"
    remapped = []
    for item in items:
        item.external_id = f"{prefix}{item.external_id}"[:180]
        remapped.append(item)
    fresh = filter_new(channel.slug, remapped)
    ids: list[int] = []
    for item in fresh:
        dup, reason = cross_donor_duplicate(channel.slug, item)
        if dup:
            print(f"[replay] skip cross-donor {item.source}: {reason}", flush=True)
            continue
        if item.event_type == "rumor" and not channel.allow_rumors:
            continue
        if (item.entities or {}).get("meme_source") and (item.event_type or "") not in {
            "lifestyle",
            "meme",
        }:
            continue
        enrich_news_item(item, fetch_article=False)
        nid = insert_news(
            {
                "channel_slug": channel.slug,
                "external_id": item.external_id,
                "cluster_id": item.cluster_id or cluster_id_for(item),
                "source": item.source,
                "url": item.url,
                "event_type": item.event_type,
                "competition": item.competition,
                "is_national": 1 if (item.entities or {}).get("is_national") else 0,
                "teams_json": _teams_json(item),
                "title": item.title,
                "body": item.body,
                "lang": item.lang,
                "source_published_at": item.published_at.strftime("%Y-%m-%d %H:%M:%S"),
                "entities_json": _entities_json(item),
                "status": "new",
                "post_kind": str((item.raw or {}).get("post_kind") or ""),
                "media_type": str((item.raw or {}).get("media_type") or ""),
                "meme_source": 1 if (item.entities or {}).get("meme_source") else 0,
            }
        )
        if nid:
            ids.append(int(nid))
    return ids


def _advance_all(channel, news_ids: list[int], *, max_steps: int = 32) -> dict[str, int]:
    counts: dict[str, int] = {}
    for nid in news_ids:
        st = "new"
        for _ in range(max_steps):
            row = get_news(nid)
            if not row:
                st = "missing"
                break
            st = str(row.get("status") or "")
            if st in TERMINAL:
                break
            st = advance_item(channel, nid)
            if st in TERMINAL:
                break
        counts[st] = counts.get(st, 0) + 1
        update_news(nid, last_error=f"replay24h:{st}")
        print(f"[replay] #{nid} → {st}", flush=True)
    return counts


def _dispatch_to_bot(channel, run_id: str) -> int:
    sent = 0
    prefix = f"replay24h:{run_id}:"
    try_dispatch_memes(channel, limit=50)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, status FROM editorial_news
            WHERE channel_slug=? AND external_id LIKE ?
              AND status IN ('ready', 'awaiting_review')
            ORDER BY id
            """,
            (channel.slug, f"{prefix}%"),
        ).fetchall()
    for row in rows:
        nid = int(row["id"])
        if str(row["status"]) == "awaiting_review":
            sent += 1
            continue
        try:
            res = dispatch_review_immediate(channel, nid)
            if res.get("action") in {"dispatched_review_immediate", "already_review"}:
                sent += 1
        except Exception as e:
            print(f"[replay] dispatch #{nid} fail: {e}", flush=True)
    return sent


def _pm2_stop_editorial() -> None:
    subprocess.run(
        ["pm2", "stop", "max-repost-editorial"],
        check=False,
        capture_output=True,
    )


def _pm2_start_editorial() -> None:
    subprocess.run(
        ["pm2", "start", "max-repost-editorial", "--update-env"],
        check=False,
        capture_output=True,
    )


def run(*, slug: str, hours: int) -> dict:
    load_dotenv_manual()
    init_db()
    reload_editorial_channels()
    channel = get_channel(slug)
    if not channel:
        raise SystemExit(f"channel not found: {slug}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    t0 = time.time()
    _pm2_stop_editorial()
    was_enabled = True
    try:
        from editorial.cycle import _channel_enabled

        was_enabled = _channel_enabled(channel)
        set_channel_enabled(slug, False)

        items = fetch_replay_window(channel, hours=hours)
        print(f"[replay] fetched {len(items)} items from {hours}h window", flush=True)
        news_ids = _ingest_replay(channel, items, run_id=run_id)
        print(f"[replay] ingested {len(news_ids)} new rows", flush=True)
        by_status = _advance_all(channel, news_ids)
        sent = _dispatch_to_bot(channel, run_id)
        summary = {
            "run_id": run_id,
            "hours": hours,
            "fetched": len(items),
            "ingested": len(news_ids),
            "by_status": by_status,
            "dispatched_to_bot": sent,
            "elapsed_sec": round(time.time() - t0, 1),
        }
        out = ROOT / "data" / "editorial" / f"replay24h_{run_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return summary
    finally:
        set_channel_enabled(slug, was_enabled)
        _pm2_start_editorial()


def main() -> None:
    p = argparse.ArgumentParser(description="Production replay: TG donors last N hours → moderation bot")
    p.add_argument("--slug", default="vnf_editorial")
    p.add_argument("--hours", type=int, default=24)
    args = p.parse_args()
    run(slug=args.slug.strip(), hours=int(args.hours))


if __name__ == "__main__":
    main()
