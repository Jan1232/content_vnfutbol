from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings, load_dotenv_manual
from app.max_api import MaxClient
from content.cycle import maybe_sync, publish_one, seconds_until_next_action, sync_catalog
from content.db import count_series, db, init_db
from content.schedule import is_quiet, now_local


def _status() -> None:
    settings = get_settings()
    init_db()
    with db() as conn:
        total, unpublished = count_series(conn)
    now = now_local()
    print(
        f"[content-worker] chat={settings.content_chat_id} "
        f"catalog={unpublished}/{total} unpublished "
        f"now={now.isoformat()} quiet={is_quiet(now)} "
        f"pause={settings.content_min_interval_sec}-{settings.content_max_interval_sec}s",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Публикация сериалов tvoe.live в MAX")
    parser.add_argument("--sync", action="store_true", help="только обновить каталог и выйти")
    parser.add_argument("--once", action="store_true", help="одна публикация (если окно открыто)")
    parser.add_argument("--force", action="store_true", help="игнорировать ночное окно и паузу")
    args = parser.parse_args()

    load_dotenv_manual()
    get_settings.cache_clear()
    init_db()
    _status()

    if args.sync:
        print(sync_catalog(), flush=True)
        return

    if args.once:
        with MaxClient() as client:
            print(publish_one(client, force=args.force), flush=True)
        return

    maybe_sync(force=True)
    _status()
    while True:
        try:
            wait = seconds_until_next_action()
            if wait > 0:
                sleep_for = min(wait, 300.0)
                print(f"[content-worker] sleep {sleep_for:.0f}s (until action {wait:.0f}s)", flush=True)
                time.sleep(sleep_for)
                continue
            maybe_sync()
            with MaxClient() as client:
                result = publish_one(client, force=False)
            print(f"[content-worker] {result}", flush=True)
            if result.get("action") == "empty":
                time.sleep(600)
        except Exception:
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    main()
