"""Cadence: random 40–55 min slots + priority bypass for big match scores."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from editorial.catalogs import is_grand, norm_name
from editorial.channel_config import EditorialChannelConfig
from editorial.fifa_ranking import fifa_name_set
from editorial.models import utcnow, utcnow_iso
from editorial.pick import pick_tag_of
from editorial.store import get_channel_state, upsert_channel_state


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _teams_of(item: dict[str, Any]) -> list[str]:
    raw = item.get("teams_json") or "[]"
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        data = []
    if isinstance(data, list):
        return [str(x) for x in data if x]
    return []


def is_priority(item: dict[str, Any], channel: EditorialChannelConfig) -> bool:
    if (item.get("event_type") or "") != "match_result":
        return False
    if not channel.cadence.priority_bypass:
        return False
    teams = _teams_of(item)
    competition = (item.get("competition") or "").upper()
    if competition == "CL":
        return True
    if any(is_grand(team) for team in teams):
        return True
    always = {norm_name(t) for t in channel.always_priority_teams}
    if any(norm_name(t) in always for t in teams):
        return True
    is_national = bool(int(item.get("is_national") or 0))
    if is_national and teams:
        top = fifa_name_set()
        if top and all(norm_name(t) in top for t in teams):
            return True
    return False


def random_gap_minutes(channel: EditorialChannelConfig) -> int:
    lo = int(channel.cadence.min_gap_min or 40)
    hi = int(channel.cadence.max_gap_min or 55)
    if hi < lo:
        lo, hi = hi, lo
    return random.randint(lo, hi)


def next_slot(channel: EditorialChannelConfig) -> datetime:
    state = get_channel_state(channel.slug)
    parsed = _parse_iso(state.get("next_slot_at"))
    if parsed:
        return parsed
    # first run: allow immediately, then a slot will be written after publish/check
    return utcnow() - timedelta(seconds=1)


def slot_ready(channel: EditorialChannelConfig) -> bool:
    return utcnow() >= next_slot(channel)


def mark_normal_published(channel: EditorialChannelConfig) -> datetime:
    gap = random_gap_minutes(channel)
    nxt = utcnow() + timedelta(minutes=gap)
    upsert_channel_state(
        channel.slug,
        last_published_at=utcnow_iso(),
        next_slot_at=nxt.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return nxt


def mark_priority_published(channel: EditorialChannelConfig) -> None:
    """Priority publish does NOT move next_slot_at."""
    state = get_channel_state(channel.slug)
    upsert_channel_state(
        channel.slug,
        last_published_at=utcnow_iso(),
        next_slot_at=state.get("next_slot_at"),
    )


def ensure_slot_initialized(channel: EditorialChannelConfig) -> None:
    state = get_channel_state(channel.slug)
    if state.get("next_slot_at"):
        return
    # bootstrap: first normal post can go out immediately
    upsert_channel_state(
        channel.slug,
        next_slot_at=(utcnow() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
    )


_TAG_RANK = {
    "match_narrative": 5,
    "rpl_exception": 5,
    "transfer_money": 4,
    "top_name": 4,
    "sensation": 4,
    "addition": 3,
    "bright_quote": 2,
    "human_factor": 1,
}


def pick_best(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None

    def score(it: dict[str, Any]) -> tuple:
        tag_rank = _TAG_RANK.get(pick_tag_of(it), 2)
        teams = _teams_of(it)
        prestige = sum(1 for t in teams if is_grand(t))
        published = str(it.get("source_published_at") or it.get("created_at") or "")
        return (tag_rank, prestige, published, int(it.get("id") or 0))

    return max(items, key=score)
