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
from opros.channels import channel_keys
from opros.cycle import publish_one, seconds_until_next_action
from opros.db import count_posts, db, get_next_publish_ts, init_db
from opros.schedule import is_quiet, now_local


def _status() -> None:
    settings = get_settings()
    init_db()
    now = now_local()
    parts: list[str] = []
    with db() as conn:
        for key in channel_keys():
            total = count_posts(conn, key)
            ts = get_next_publish_ts(conn, key)
            parts.append(f"{key}={total} next={ts or '-'}")
    print(
        f"[opros-worker] model={settings.opros_openai_model} "
        f"tz={settings.opros_timezone} quiet={is_quiet(now)} "
        f"window={settings.opros_quiet_end_hour:02d}:00-{settings.opros_quiet_start_hour:02d}:00 "
        f"pause={settings.opros_min_interval_sec}-{settings.opros_max_interval_sec}s "
        f"now={now.isoformat()} {' '.join(parts)}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Публикация анонимных опросов в MAX")
    parser.add_argument("--once", action="store_true", help="одна публикация")
    parser.add_argument("--force", action="store_true", help="игнорировать окно и паузу")
    parser.add_argument(
        "--channel",
        choices=list(channel_keys()),
        help="конкретный канал (18plus|lichnoe|anon)",
    )
    args = parser.parse_args()

    load_dotenv_manual()
    get_settings.cache_clear()
    init_db()
    _status()

    if args.once:
        with MaxClient() as client:
            print(publish_one(client, force=args.force, channel=args.channel), flush=True)
        return

    _status()
    while True:
        try:
            wait = seconds_until_next_action()
            if wait > 0:
                sleep_for = min(wait, 300.0)
                print(
                    f"[opros-worker] sleep {sleep_for:.0f}s (until action {wait:.0f}s)",
                    flush=True,
                )
                time.sleep(sleep_for)
                continue
            with MaxClient() as client:
                result = publish_one(client, force=False, channel=None)
            print(f"[opros-worker] {result}", flush=True)
            if result.get("action") == "error":
                time.sleep(60)
        except Exception:
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    main()
