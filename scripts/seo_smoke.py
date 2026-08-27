#!/usr/bin/env python3
"""Smoke / force SEO rotation.

Examples:
  PYTHONPATH=/var/max-repost .venv/bin/python scripts/seo_smoke.py --dry-title
  PYTHONPATH=/var/max-repost .venv/bin/python scripts/seo_smoke.py --synthetic
  PYTHONPATH=/var/max-repost .venv/bin/python scripts/seo_smoke.py --live
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_dotenv_manual
from app.db import init_db
from app.max_api import MaxClient
from seo.channel_config import load_seo_channels, reload_seo_channels
from seo.cycle import rotate_channel, run_seo_tick
from seo.fixtures import Match, pick_top_match
from seo.titles import build_seo_title, team_name_ru


def dry_title() -> None:
    samples = [
        ("Paris Saint-Germain FC", "Aston Villa FC"),
        ("Real Madrid CF", "FC Barcelona"),
        ("FC Bayern München", "Manchester City FC"),
    ]
    for h, a in samples:
        t = build_seo_title(h, a)
        print(f"{h} vs {a}")
        print(f"  ru: {team_name_ru(h)} / {team_name_ru(a)}")
        print(f"  title ({len(t)}): {t}")


def synthetic_rotate(slug: str = "uefafootball") -> None:
    reload_seo_channels()
    cfgs = {c.slug: c for c in load_seo_channels()}
    cfg = cfgs.get(slug)
    if not cfg:
        raise SystemExit(f"slug not found: {slug}")
    kick = datetime.now(timezone.utc) + timedelta(days=2, hours=3)
    match = Match(
        match_id=f"synthetic-{int(kick.timestamp())}",
        competition="CL",
        competition_name="UEFA Champions League",
        home_team="Paris Saint-Germain FC",
        away_team="Aston Villa FC",
        utc_date=kick,
        status="TIMED",
        stage="LEAGUE_STAGE",
        matchday=1,
    )
    init_db()
    with MaxClient() as client:
        res = rotate_channel(client, cfg, match, force=True)
    print(res)


def main() -> None:
    load_dotenv_manual()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-title", action="store_true")
    ap.add_argument("--synthetic", action="store_true", help="Force rotate with synthetic PSG-Villa match")
    ap.add_argument("--live", action="store_true", help="Run real football-data tick")
    ap.add_argument("--slug", default="uefafootball")
    args = ap.parse_args()
    if args.dry_title:
        dry_title()
        return
    if args.synthetic:
        synthetic_rotate(args.slug)
        return
    if args.live:
        print(run_seo_tick(force_slug=args.slug))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
