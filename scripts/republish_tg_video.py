#!/usr/bin/env python3
"""Переопубликовать пост с oversized TG-видео (например Neymar 7215).

Пример:
  cd /var/max-repost && .venv/bin/python scripts/republish_tg_video.py 309
  cd /var/max-repost && .venv/bin/python scripts/republish_tg_video.py --ref Neymar_jru/7215
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_dotenv_manual
from app.db import db, init_db, mark_post
from app.tg_mtproto import download_tg_media, is_tg_ready


def main() -> int:
    load_dotenv_manual()
    ap = argparse.ArgumentParser()
    ap.add_argument("post_id", nargs="?", type=int, help="ID поста в БД")
    ap.add_argument("--ref", help="tg ref вида Neymar_jru/7215")
    args = ap.parse_args()

    init_db()
    if not is_tg_ready():
        print("Нет TG-сессии. Сначала: .venv/bin/python scripts/tg-login.py")
        return 1

    with db() as conn:
        if args.post_id:
            row = conn.execute(
                "SELECT id, external_id, text, media_json, source_url FROM posts WHERE id=?",
                (args.post_id,),
            ).fetchone()
        elif args.ref:
            ext = args.ref if args.ref.startswith("tg:") else f"tg:{args.ref}"
            row = conn.execute(
                "SELECT id, external_id, text, media_json, source_url FROM posts WHERE external_id=?",
                (ext,),
            ).fetchone()
        else:
            print("Укажите post_id или --ref")
            return 1
        if not row:
            print("Пост не найден")
            return 1

        media = json.loads(row["media_json"] or "[]")
        tg_ref = ""
        for m in media:
            if m.get("tg_ref"):
                tg_ref = m["tg_ref"]
                break
        if not tg_ref and args.ref:
            tg_ref = args.ref.replace("tg:", "")
        if not tg_ref and row["external_id"]:
            # tg:Neymar_jru/7215
            tg_ref = str(row["external_id"]).removeprefix("tg:")
        if not tg_ref:
            print("Не удалось определить tg_ref")
            return 1

        print(f"post={row['id']} ref={tg_ref}")
        path = download_tg_media(tg_ref)
        if not path:
            print("Скачивание не удалось")
            return 1

        media = [
            {
                "type": "video",
                "url": path.resolve().as_uri(),
                "tg_ref": tg_ref,
                "too_big": False,
                "local_path": str(path.resolve()),
            }
        ]
        # убрать старую бронь дубля
        conn.execute(
            "DELETE FROM send_log WHERE post_id=? OR external_id=?",
            (row["id"], row["external_id"]),
        )
        conn.execute(
            """
            UPDATE posts SET media_json=?, publish_status='pending', publish_error='',
                   publish_at=?, sent_at=NULL WHERE id=?
            """,
            (json.dumps(media, ensure_ascii=False), time.time(), row["id"]),
        )
        print(f"OK: media={path} ({path.stat().st_size} bytes), status=pending")
        print("Воркер подхватит в ближайшие секунды.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
