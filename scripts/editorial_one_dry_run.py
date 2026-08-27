#!/usr/bin/env python3
"""Один dry_run-проход новости через editorial на Platform API."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import init_db
from editorial.channel_config import get_channel, reload_editorial_channels
from editorial.cycle import advance_item, ingest_channel, set_channel_enabled
from editorial.store import get_news, list_open_news
from editorial.usage import daily_usage_summary


def main() -> int:
    init_db()
    reload_editorial_channels()
    cfg = get_channel("vnf_editorial")
    if not cfg:
        raise SystemExit("нет канала vnf_editorial")
    if not cfg.dry_run:
        raise SystemExit("отказ: dry_run выключен")

    paused = False
    try:
        set_channel_enabled(cfg.slug, False)
        paused = True
        ingested = ingest_channel(cfg)
        print(f"[one] ingested={ingested}", flush=True)
        rows = list_open_news(cfg.slug)
        target = None
        for row in rows:
            if (row.get("status") or "") in {"new", "verifying", "confirmed", "editing"}:
                target = row
                break
        if not target:
            raise SystemExit("нет открытой новости для прогона")
        news_id = int(target["id"])
        print(
            f"[one] start id={news_id} status={target.get('status')} title={target.get('title')!r}",
            flush=True,
        )
        status = advance_item(cfg, news_id)
        row = get_news(news_id) or {}
        print(
            f"[one] done id={news_id} status={status} "
            f"factcheck={row.get('factcheck_status')} err={row.get('last_error')!r}",
            flush=True,
        )
        if row.get("post_text"):
            print(f"[one] post_text:\n{row.get('post_text')[:600]}", flush=True)
        if row.get("caption"):
            print(f"[one] caption={row.get('caption')!r}", flush=True)
        print(f"[one] preview=/editorial/preview/{news_id}", flush=True)
        usage = daily_usage_summary()
        print(
            f"[one] usage n={usage['n']} in={usage['prompt_tokens']} "
            f"out={usage['completion_tokens']} usd≈{usage['usd']:.4f}",
            flush=True,
        )
        return 0
    finally:
        if paused:
            set_channel_enabled(cfg.slug, True)


if __name__ == "__main__":
    raise SystemExit(main())
