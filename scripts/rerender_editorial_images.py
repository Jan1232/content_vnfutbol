#!/usr/bin/env python3
"""Пересобрать обложки уже опубликованных sim-постов новым imagery v2. В MAX не уходит."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings, load_dotenv_manual
from app.db import db, init_db
from editorial.channel_config import get_channel, reload_editorial_channels
from editorial.cycle import _channel_enabled, set_channel_enabled
from editorial.imagery import find_photo
from editorial.render import BADGE_FOR_EVENT, render_post
from editorial.store import get_news, update_news


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-08-19")
    parser.add_argument("--slug", default="vnf_editorial")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    load_dotenv_manual()
    init_db()
    reload_editorial_channels()
    cfg = get_channel(args.slug)
    if not cfg:
        raise SystemExit(f"нет канала {args.slug}")
    day = date.fromisoformat(args.date)
    prefix = f"sim-{day.isoformat()}:"

    was = _channel_enabled(cfg)
    set_channel_enabled(cfg.slug, False)
    print(f"[rerender] paused worker was_enabled={was}", flush=True)
    ok = n_hold = n_err = 0
    try:
        with db() as conn:
            rows = conn.execute(
                """
                SELECT id FROM editorial_news
                WHERE channel_slug=? AND external_id LIKE ? AND status='published'
                ORDER BY id ASC
                """,
                (cfg.slug, prefix + "%"),
            ).fetchall()
        ids = [int(r["id"]) for r in rows]
        if args.limit:
            ids = ids[: args.limit]
        print(f"[rerender] published sim posts: {len(ids)}", flush=True)
        backup = ROOT / "data" / "editorial" / "covers" / f"before_v2_{day.isoformat()}"
        backup.mkdir(parents=True, exist_ok=True)
        for news_id in ids:
            row = get_news(news_id) or {}
            old = Path(row.get("cover_path") or "")
            if old.is_file():
                shutil.copy2(old, backup / old.name)
            template = cfg.template_for(row.get("event_type") or "other")
            print(f"[rerender] #{news_id} {template} {(row.get('title') or '')[:80]}", flush=True)
            try:
                path = find_photo(row, template_name=template)
            except Exception as e:
                print(f"[rerender]   find_photo fail: {e}", flush=True)
                n_err += 1
                continue
            if not path:
                n_hold += 1
                print("[rerender]   keep old cover: нет нового релевантного фото", flush=True)
            else:
                badge = BADGE_FOR_EVENT.get(row.get("event_type") or "", "НОВОСТЬ")
                cover = render_post(
                    template,
                    path,
                    row.get("caption_line1") or "",
                    row.get("caption_line2") or None,
                    badge,
                    {
                        "name": cfg.brand.name,
                        "logo": cfg.brand.logo,
                        "accent_color": cfg.brand.accent_color,
                    },
                    news_id=news_id,
                )
                update_news(news_id, image_path=path, cover_path=cover, last_error="")
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE posts
                        SET media_json=?
                        WHERE external_id=? AND publish_status='simulated'
                        """,
                        (
                            json.dumps(
                                [
                                    {
                                        "type": "image",
                                        "local_path": cover,
                                        "filename": "cover.png",
                                        "news_id": news_id,
                                    }
                                ],
                                ensure_ascii=False,
                            ),
                            f"editorial:{news_id}",
                        ),
                    )
                ok += 1
                print(f"[rerender]   ok {cover}", flush=True)
            time.sleep(1.2)
    finally:
        set_channel_enabled(cfg.slug, was)
        print(f"[rerender] restored enabled={was}", flush=True)
    print(f"[rerender] done ok={ok} held={n_hold} err={n_err} db={get_settings().db_path}", flush=True)
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
