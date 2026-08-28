"""Собрать JSON-сводку live_test_day из БД (без повторного прогона конвейера)."""

from __future__ import annotations

import argparse
import json

from app.db import init_db
from editorial.live_test_day import LIVE_DIR, _compare_prior_day, _usage_for_day, _print_summary
from app.config import get_settings


def build_summary(date_str: str) -> dict:
    from app.db import db

    init_db()
    prefix = f"live_test:{date_str}:"
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, status, last_error
            FROM editorial_news
            WHERE external_id LIKE ?
            """,
            (f"{prefix}%",),
        ).fetchall()
    news_ids = [int(r["id"]) for r in rows]
    by_status: dict[str, str] = {}
    to_bot = 0
    filtered = 0
    for r in rows:
        err = str(r["last_error"] or "")
        st = str(r["status"] or "")
        if err.startswith(f"live_test:{date_str}:"):
            final = err.split(":", 2)[-1]
        else:
            final = st
        by_status[str(r["id"])] = final
        if final == "awaiting_review" or st == "awaiting_review":
            to_bot += 1
        elif final in {"filtered", "off_topic", "skipped", "rejected", "held"} or st in {
            "filtered",
            "off_topic",
            "held",
        }:
            filtered += 1
    usage = _usage_for_day(date_str)
    summary = {
        "date": date_str,
        "channel": "vnf_editorial",
        "news_ids": news_ids,
        "stats": {
            "date": date_str,
            "ingested": len(news_ids),
            "to_bot": to_bot,
            "filtered": filtered,
            "by_status": by_status,
        },
        "usage": usage,
        "compare": _compare_prior_day(date_str),
        "config": {
            "classify_model": get_settings().editorial_classify_model,
            "vision_skip_for_og": get_settings().vision_skip_for_og,
            "story_relation_hybrid": get_settings().story_relation_hybrid,
        },
    }
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LIVE_DIR / f"{date_str}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["file"] = str(out_path)
    _print_summary(summary)
    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True)
    args = p.parse_args()
    build_summary(args.date.strip())


if __name__ == "__main__":
    main()
