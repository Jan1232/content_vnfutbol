#!/usr/bin/env python3
"""Smoke: fixtures provider (football-data + ESPN), proxy, rate-budget, significant matches."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.db import init_db
from app.http_util import openai_proxy
from editorial.fixtures import (
    FINISHED_STATUSES,
    get_provider,
    reset_provider,
    significant_matches,
)


def main() -> int:
    init_db()
    reset_provider()
    settings = get_settings()
    proxy = openai_proxy()
    print(f"[smoke] backend={settings.fixtures_backend} live={settings.fixtures_live} proxy={proxy or 'NONE'}")
    if not proxy:
        print("FAIL: нет OPENAI_HTTP_PROXY — football-data с VPS ловит unsupported_country", flush=True)
        return 1
    if not (settings.football_data_token or "").strip():
        print("FAIL: FOOTBALL_DATA_TOKEN пуст", flush=True)
        return 1

    from editorial.fixtures import _fd_budget

    before = len(_fd_budget._hits)
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    provider = get_provider()
    matches = provider.matches_on(today)
    sig = significant_matches(matches, always_priority=("Russia",))
    print(f"[smoke] matches_on({today}) total={len(matches)} significant={len(sig)}")
    for m in sig[:12]:
        score = (
            f"{m.score_home}:{m.score_away}"
            if m.score_home is not None
            else "—"
        )
        print(
            f"  {m.kickoff_msk.strftime('%H:%M')} {m.competition} {m.home_ru} — {m.away_ru} "
            f"{m.status} {score} id={m.provider_id}"
        )
    finished = [m for m in sig if m.status in FINISHED_STATUSES]
    if finished:
        pick = finished[0]
        fresh = provider.match_status(pick.provider_id) or pick
        print(
            f"[smoke] finished {fresh.home_ru} {fresh.score_home}:{fresh.score_away} {fresh.away_ru} "
            f"({fresh.competition}) delay=free-tier"
        )
    else:
        print("[smoke] сегодня нет завершённых значимых — это нормально вне игрового окна")

    used = len(_fd_budget._hits) - before
    print(f"[smoke] football-data req this run={used} budget={_fd_budget.per_min}/min (limit 10)")
    if used > 10:
        print("FAIL: уложиться в 10 req/min не получилось", flush=True)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
