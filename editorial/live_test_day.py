"""Live test day: full pipeline for a calendar date, cards to TG bot, no MAX publish."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT, get_settings
from app.db import db, init_db
from editorial.channel_config import get_channel, load_editorial_channels, reload_editorial_channels
from editorial.cycle import advance_item
from editorial.discovery import fetch_news_for_date
from editorial.live_test import live_test_scope
from editorial.moderation import try_dispatch_review
from editorial.openai_client import usage_scope
from editorial.store import get_news, insert_news, update_news
from editorial.usage import estimate_usd, usage_dashboard

LIVE_DIR = ROOT / "data" / "editorial" / "live_test"
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
        "benchmark",
        "live_test",
    }
)


def _entities_json_with_raw(item) -> str:
    ent = dict(item.entities or {})
    if item.raw:
        ent["raw"] = item.raw
    return json.dumps(ent, ensure_ascii=False)


def _teams_json(item) -> str:
    teams = (item.entities or {}).get("teams") or []
    return json.dumps(teams, ensure_ascii=False)


def _ingest_live_items(channel, items, *, date_str: str) -> list[int]:
    ids: list[int] = []
    prefix = f"live_test:{date_str}:"
    for item in items:
        ext = f"{prefix}{item.external_id}"[:180]
        with db() as conn:
            exists = conn.execute(
                "SELECT id FROM editorial_news WHERE external_id=? LIMIT 1",
                (ext,),
            ).fetchone()
        if exists:
            continue
        nid = insert_news(
            {
                "channel_slug": channel.slug,
                "external_id": ext,
                "cluster_id": item.cluster_id or "",
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
                "entities_json": _entities_json_with_raw(item),
                "status": "new",
                "post_kind": str((item.raw or {}).get("post_kind") or ""),
                "media_type": str((item.raw or {}).get("media_type") or ""),
                "meme_source": 1 if (item.entities or {}).get("meme_source") else 0,
            }
        )
        if nid:
            ids.append(int(nid))
    return ids


def _dispatch_all_ready_to_bot(channel, date_str: str) -> int:
    """Все ready live_test-карточки → TG (без cadence-лимита)."""
    from app.db import db
    from editorial.moderation import dispatch_review_immediate

    prefix = f"live_test:{date_str}:"
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM editorial_news WHERE external_id LIKE ? AND status='ready' ORDER BY id",
            (f"{prefix}%",),
        ).fetchall()
    sent = 0
    for row in rows:
        nid = int(row["id"])
        try:
            res = dispatch_review_immediate(channel, nid)
            if res.get("action") in {"dispatched_review_immediate", "already_review"}:
                sent += 1
        except Exception as e:
            print(f"[live_test] dispatch #{nid} fail: {e}", flush=True)
    return sent


def _advance_to_terminal(channel, news_id: int, *, max_steps: int = 28) -> str:
    last = "new"
    for _ in range(max_steps):
        row = get_news(news_id)
        if not row:
            return "missing"
        st = str(row.get("status") or "")
        if st in {"awaiting_review", "ready"}:
            try_dispatch_review(channel, force=True)
            row = get_news(news_id) or row
            st = str(row.get("status") or "")
        if st in TERMINAL:
            return st
        last = advance_item(channel, news_id)
        if last in TERMINAL:
            return last
    return last


def _usage_for_day(date_str: str, *, tag: str = "live_test") -> dict[str, Any]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT task, model,
                   COUNT(*) AS calls,
                   SUM(prompt_tokens) AS p_in,
                   SUM(completion_tokens) AS p_out,
                   SUM(cached_tokens) AS cached
            FROM editorial_llm_usage
            WHERE note LIKE ?
            GROUP BY task, model
            ORDER BY SUM(prompt_tokens)+SUM(completion_tokens) DESC
            """,
            (f"%{tag}:{date_str}%",),
        ).fetchall()
    matrix = []
    for r in rows:
        d = dict(r)
        d["usd"] = estimate_usd(int(d.get("p_in") or 0), int(d.get("p_out") or 0), str(d.get("model") or ""))
        matrix.append(d)
    total_usd = sum(float(r.get("usd") or 0) for r in matrix)
    total_cached = sum(int(r.get("cached") or 0) for r in matrix)
    vision_calls = sum(
        int(r.get("calls") or 0)
        for r in matrix
        if str(r.get("task") or "").startswith("image_vision")
    )
    return {
        "matrix": matrix,
        "totals": {"usd": round(total_usd, 4), "cached": total_cached, "vision_calls": vision_calls},
    }


def _compare_prior_day(date_str: str) -> dict[str, Any]:
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d")
        prior = (day - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return {}
    dash = usage_dashboard()
    prior_usd = None
    for block in (dash.get("periods") or {}).values():
        pass
    with db() as conn:
        row = conn.execute(
            """
            SELECT SUM(
                (prompt_tokens * 1.0 / 1000000) + (completion_tokens * 1.0 / 1000000)
            ) AS tok
            FROM editorial_llm_usage
            WHERE date(ts) = ?
            """,
            (prior,),
        ).fetchone()
    prior_tok = float(row["tok"] or 0) if row and row["tok"] is not None else 0.0
    return {"prior_date": prior, "prior_token_m": round(prior_tok, 2)}


def run_live_test_day(*, date_str: str, slug: str | None = None) -> dict[str, Any]:
    init_db()
    reload_editorial_channels()
    channels = load_editorial_channels()
    if not channels:
        raise RuntimeError("no editorial channels")
    channel = get_channel(slug) if slug else channels[0]
    if not channel:
        channel = channels[0]
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    stats: dict[str, Any] = {
        "date": date_str,
        "channel": channel.slug,
        "ingested": 0,
        "to_bot": 0,
        "filtered": 0,
        "by_status": {},
    }
    news_ids: list[int] = []
    with live_test_scope(date_str):
        with usage_scope(task="live_test"):
            items = fetch_news_for_date(channel, date_str)
            news_ids = _ingest_live_items(channel, items, date_str=date_str)
            stats["ingested"] = len(news_ids)
            for nid in news_ids:
                final = _advance_to_terminal(channel, nid)
                stats["by_status"][str(nid)] = final
                if final == "awaiting_review":
                    stats["to_bot"] += 1
                elif final in {"filtered", "off_topic", "skipped", "rejected", "held"}:
                    stats["filtered"] += 1
                update_news(nid, status="live_test", last_error=f"live_test:{date_str}:{final}")
            dispatched = _dispatch_all_ready_to_bot(channel, date_str)
            stats["dispatched_to_bot"] = dispatched
            stats["to_bot"] += dispatched
    usage = _usage_for_day(date_str)
    summary = {
        "date": date_str,
        "channel": channel.slug,
        "elapsed_sec": round(time.time() - t0, 1),
        "news_ids": news_ids,
        "stats": stats,
        "usage": usage,
        "compare": _compare_prior_day(date_str),
        "config": {
            "classify_model": get_settings().editorial_classify_model,
            "vision_skip_for_og": get_settings().vision_skip_for_og,
            "story_relation_hybrid": get_settings().story_relation_hybrid,
        },
    }
    out_path = LIVE_DIR / f"{date_str}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["file"] = str(out_path)
    _print_summary(summary)
    return summary


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"\n=== live_test_day {summary.get('date')} ===", flush=True)
    st = summary.get("stats") or {}
    print(
        f"ingested={st.get('ingested')} to_bot={st.get('to_bot')} filtered={st.get('filtered')}",
        flush=True,
    )
    totals = (summary.get("usage") or {}).get("totals") or {}
    print(
        f"ИТОГО: ${totals.get('usd')} cached={totals.get('cached')} "
        f"vision_calls={totals.get('vision_calls')}",
        flush=True,
    )
    for row in (summary.get("usage") or {}).get("matrix") or []:
        print(
            f"  {row.get('task'):22} {str(row.get('model') or ''):18} "
            f"calls={row.get('calls')} cached={row.get('cached')} ${float(row.get('usd') or 0):.4f}",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Editorial live test day")
    parser.add_argument("--date", type=str, required=True, help="YYYY-MM-DD in MATCHDAY_TZ")
    parser.add_argument("--slug", type=str, default="")
    args = parser.parse_args()
    run_live_test_day(date_str=args.date.strip(), slug=args.slug or None)


if __name__ == "__main__":
    main()
