"""Cost benchmark: full pipeline on N posts without publish/moderation."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import ROOT, get_settings
from app.db import init_db
from editorial.channel_config import get_channel, load_editorial_channels, reload_editorial_channels
from editorial.cycle import advance_item
from editorial.discovery import fetch_fresh_news
from editorial.openai_client import benchmark_scope
from editorial.store import get_news, insert_news, update_news
from editorial.usage import estimate_usd

BENCH_DIR = ROOT / "data" / "editorial" / "benchmark"
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
        "benchmark",
    }
)


def _entities_json(item) -> str:
    import json as _json

    return _json.dumps(item.entities or {}, ensure_ascii=False)


def _entities_json_with_raw(item) -> str:
    import json as _json

    ent = dict(item.entities or {})
    if item.raw:
        ent["raw"] = item.raw
    return _json.dumps(ent, ensure_ascii=False)


def _teams_json(item) -> str:
    import json as _json

    teams = (item.entities or {}).get("teams") or []
    return _json.dumps(teams, ensure_ascii=False)


def _reset_benchmark_candidates(channel_slug: str, count: int, *, run_id: str) -> list[int]:
    """Взять N RSS-новостей из held/published и сбросить в new для полного прогона."""
    from app.db import db

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id FROM editorial_news
            WHERE channel_slug=?
              AND source NOT IN ('soccerblog_memes', 'yt_bot_topics')
              AND status IN ('held', 'published')
              AND length(COALESCE(body, '')) > 80
              AND title IS NOT NULL AND title != ''
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (channel_slug, int(count)),
        ).fetchall()
    ids: list[int] = []
    for row in rows:
        nid = int(row[0])
        update_news(
            nid,
            status="new",
            last_error=f"benchmark_reset:{run_id}",
            retry_count=0,
            factcheck_status="",
            factcheck_conf=0,
            post_text="",
            headline="",
            caption="",
            caption_line1="",
            caption_line2="",
            cover_path="",
            image_path="",
            media_path="",
        )
        ids.append(nid)
    return ids


def _ingest_benchmark_items(channel, count: int, *, run_id: str) -> list[int]:
    """Вставить N постов для прогона: клон discovery с уникальным external_id."""
    items = fetch_fresh_news(channel)
    ids: list[int] = []
    for item in items:
        if len(ids) >= count:
            break
        # полный конвейер с vision — RSS/новости, не мем-ветка
        if (item.entities or {}).get("meme_source"):
            continue
        ext = f"benchmark:{run_id}:{item.external_id}"[:180]
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
                "meme_source": 0,
            }
        )
        if nid:
            ids.append(int(nid))
    return ids


def _advance_to_terminal(channel, news_id: int, *, max_steps: int = 24) -> str:
    last = "new"
    for _ in range(max_steps):
        row = get_news(news_id)
        if not row:
            return "missing"
        st = str(row.get("status") or "")
        if st in TERMINAL:
            return st
        last = advance_item(channel, news_id)
        if last in TERMINAL:
            return last
    return last


def _summary_from_db(run_id: str) -> dict[str, Any]:
    from app.db import db

    with db() as conn:
        rows = conn.execute(
            """
            SELECT stage, model,
                   COUNT(*) AS calls,
                   SUM(p_in) AS p_in,
                   SUM(p_out) AS p_out,
                   SUM(cached) AS cached,
                   SUM(usd) AS usd,
                   SUM(ms) AS ms
            FROM editorial_cost_benchmark
            WHERE run_id=?
            GROUP BY stage, model
            ORDER BY SUM(p_in)+SUM(p_out) DESC
            """,
            (run_id,),
        ).fetchall()
        news_rows = conn.execute(
            """
            SELECT news_id, stage, model, p_in, p_out, cached, usd, ms
            FROM editorial_cost_benchmark
            WHERE run_id=?
            ORDER BY news_id, id
            """,
            (run_id,),
        ).fetchall()
    matrix = [dict(r) for r in rows]
    per_news: dict[str, list[dict[str, Any]]] = {}
    for r in news_rows:
        d = dict(r)
        per_news.setdefault(str(d.get("news_id") or ""), []).append(d)
    total_in = sum(int(r.get("p_in") or 0) for r in matrix)
    total_out = sum(int(r.get("p_out") or 0) for r in matrix)
    total_cached = sum(int(r.get("cached") or 0) for r in matrix)
    total_usd = sum(float(r.get("usd") or 0) for r in matrix)
    n_news = len(per_news)
    return {
        "run_id": run_id,
        "matrix": matrix,
        "per_news": per_news,
        "totals": {
            "news_count": n_news,
            "p_in": total_in,
            "p_out": total_out,
            "cached": total_cached,
            "usd": round(total_usd, 4),
            "avg_usd_per_post": round(total_usd / n_news, 4) if n_news else 0,
        },
        "vision_ab": {
            "mini": next((r for r in matrix if r.get("stage") == "image_vision_ab_mini"), None),
            "luna": next((r for r in matrix if r.get("stage") == "image_vision_ab_luna"), None),
        },
    }


def run_cost_benchmark(*, count: int = 10, slug: str | None = None, from_held: bool = True) -> dict[str, Any]:
    init_db()
    reload_editorial_channels()
    channels = load_editorial_channels()
    if not channels:
        raise RuntimeError("no editorial channels")
    channel = get_channel(slug) if slug else channels[0]
    if not channel:
        channel = channels[0]
    run_id = uuid.uuid4().hex[:12]
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    news_ids: list[int] = []
    with benchmark_scope(run_id):
        if from_held:
            news_ids = _reset_benchmark_candidates(channel.slug, count, run_id=run_id)
            if len(news_ids) < count:
                print(
                    f"[benchmark] held pool {len(news_ids)}/{count}, дополняем из RSS",
                    flush=True,
                )
                news_ids.extend(_ingest_benchmark_items(channel, count - len(news_ids), run_id=run_id))
        else:
            news_ids = _ingest_benchmark_items(channel, count, run_id=run_id)
        if len(news_ids) < count:
            print(f"[benchmark] итого {len(news_ids)} постов (wanted {count})", flush=True)
        for nid in news_ids:
            final = _advance_to_terminal(channel, nid)
            update_news(nid, status="benchmark", last_error=f"benchmark:{run_id}:{final}")
    summary = _summary_from_db(run_id)
    summary["channel"] = channel.slug
    summary["elapsed_sec"] = round(time.time() - t0, 1)
    summary["news_ids"] = news_ids
    if summary["totals"]["news_count"]:
        per_day = 30
        summary["extrapolation"] = {
            "posts_per_day": per_day,
            "usd_per_day": round(summary["totals"]["avg_usd_per_post"] * per_day, 2),
            "usd_per_month": round(summary["totals"]["avg_usd_per_post"] * per_day * 30, 2),
        }
    elif news_ids:
        per_post = summary["totals"]["usd"] / len(news_ids)
        summary["totals"]["avg_usd_per_post"] = round(per_post, 4)
        summary["extrapolation"] = {
            "posts_per_day": 30,
            "usd_per_day": round(per_post * 30, 2),
            "usd_per_month": round(per_post * 30 * 30, 2),
        }
    out_path = BENCH_DIR / f"{run_id}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["file"] = str(out_path)
    _print_summary(summary)
    return summary


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"\n=== cost_benchmark run_id={summary.get('run_id')} ===", flush=True)
    print(f"news: {summary.get('news_ids')} elapsed={summary.get('elapsed_sec')}s", flush=True)
    totals = summary.get("totals") or {}
    print(
        f"ИТОГО: ${totals.get('usd')} | cached={totals.get('cached')} | "
        f"avg/post=${totals.get('avg_usd_per_post')}",
        flush=True,
    )
    for row in summary.get("matrix") or []:
        print(
            f"  {row.get('stage'):22} {str(row.get('model') or ''):18} "
            f"calls={row.get('calls')} in={row.get('p_in')} out={row.get('p_out')} "
            f"cached={row.get('cached')} ${float(row.get('usd') or 0):.4f}",
            flush=True,
        )
    ext = summary.get("extrapolation")
    if ext:
        print(
            f"Экстраполяция {ext.get('posts_per_day')}/день → "
            f"${ext.get('usd_per_day')}/день, ${ext.get('usd_per_month')}/мес",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Editorial cost benchmark")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--slug", type=str, default="")
    parser.add_argument("--from-held", action="store_true", default=False)
    parser.add_argument("--fresh-only", action="store_true", help="только RSS clone, без held")
    args = parser.parse_args()
    run_cost_benchmark(
        count=max(1, args.count),
        slug=args.slug or None,
        from_held=args.from_held and not args.fresh_only,
    )


if __name__ == "__main__":
    main()
