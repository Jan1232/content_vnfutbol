#!/usr/bin/env python3
"""Раз в сутки: сводка охватов и подписок в ЛС @контент-мейкер."""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db import db, init_db
from app.digest import (
    build_digest,
    build_err24,
    format_digest,
    format_err24_messages,
    owner_user_ids,
    send_digest,
    snapshot_at_or_before,
)
from app.max_api import MaxClient
from predlozka.client import PredlozkaClient
from predlozka.config import api_base, bot_token, load_dotenv


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--send", action="store_true", help="отправить в ЛС")
    p.add_argument("--dry-run", action="store_true", help="только печать")
    p.add_argument(
        "--rolling",
        action="store_true",
        help="окно последние 24 часа, а не вчерашние календарные сутки",
    )
    args = p.parse_args()

    load_dotenv()
    settings = get_settings()
    init_db()
    tz = ZoneInfo(settings.digest_timezone or "Asia/Yekaterinburg")
    now = datetime.now(tz)
    # Как MaxStars: скользящие 24/48/72ч от текущего момента (и для --send).
    end = now
    start = now - timedelta(hours=24)
    label = now.strftime("%d.%m.%Y %H:%M")
    start_ts, end_ts = start.timestamp(), end.timestamp()
    _ = args.rolling

    with MaxClient() as client:
        rows = build_digest(client, start_ts=start_ts, end_ts=end_ts, refresh=True)

    with db() as conn:
        has_start = any(
            snapshot_at_or_before(conn, r.chat_id, start_ts) is not None for r in rows
        )
    text = format_digest(
        rows, day_label=label, deltas_ready=has_start, views_ready=True
    )
    print(text.replace("<b>", "").replace("</b>", ""), flush=True)
    with PredlozkaClient(token=bot_token(), base=api_base()) as pclient:
        err_rows = build_err24(pclient, now_ts=end.timestamp())
    err_msgs = format_err24_messages(err_rows, day_label=label)
    for msg in err_msgs:
        print(msg.replace("<b>", "").replace("</b>", ""), flush=True)
    if args.dry_run or not args.send:
        if not args.send:
            print("[digest] не отправлено (нет --send)", flush=True)
        return 0
    uids = owner_user_ids()
    print(f"[digest] send user_ids={uids}", flush=True)
    try:
        send_digest(text)
        for msg in err_msgs:
            send_digest(msg)
    except Exception:
        traceback.print_exc()
        return 1
    print("[digest] sent", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
