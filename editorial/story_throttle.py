"""Story throttle: limit duplicate story angles per channel day."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db import db
from editorial.catalogs import canonical_team, load_players, norm_name
from editorial.models import NewsItem

RANK_LOW = 1
RANK_MATCH = 2
RANK_CONFIRMED = 3
RANK_OFFICIAL = 4

_TRANSFER_MARKERS = re.compile(
    r"transfer|трансфер|подписал|подписан|переш|аренд|medical|медосмотр|here we go",
    re.I,
)
_MATCH_MARKERS = re.compile(
    r"full.?time|full time|\d+:\d+|победил|уступил|ничья|матч|счёт|счет",
    re.I,
)
_OFFICIAL_MARKERS = re.compile(
    r"official|официальн|here we go|подтвердил переход|объявил о переходе|"
    r"официально переш|официально подпис",
    re.I,
)


@dataclass(frozen=True)
class StoryThrottleConfig:
    max_per_day: int = 3
    hard_cap: int = 4
    min_gap_posts: int = 3
    min_gap_min: int = 180


def throttle_config() -> StoryThrottleConfig:
    s = get_settings()
    return StoryThrottleConfig(
        max_per_day=int(getattr(s, "story_max_per_day", 3) or 3),
        hard_cap=int(getattr(s, "story_hard_cap", 4) or 4),
        min_gap_posts=int(getattr(s, "story_min_gap_posts", 3) or 3),
        min_gap_min=int(getattr(s, "story_min_gap_min", 180) or 180),
    )


def channel_day(when: datetime | None = None) -> str:
    s = get_settings()
    tz = ZoneInfo(getattr(s, "matchday_tz", "Europe/Moscow") or "Europe/Moscow")
    dt = when or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d")


def _entities_of(item: NewsItem | dict[str, Any]) -> dict[str, Any]:
    if isinstance(item, NewsItem):
        return dict(item.entities or {})
    try:
        ent = json.loads(item.get("entities_json") or "{}")
        return ent if isinstance(ent, dict) else {}
    except Exception:
        ent = item.get("entities")
        return ent if isinstance(ent, dict) else {}


def _title_body(item: NewsItem | dict[str, Any]) -> tuple[str, str]:
    if isinstance(item, NewsItem):
        return str(item.title or ""), str(item.body or "")
    return str(item.get("title") or ""), str(item.get("body") or "")


def _event_type(item: NewsItem | dict[str, Any]) -> str:
    if isinstance(item, NewsItem):
        return str(item.event_type or "other")
    return str(item.get("event_type") or "other")


def _player_slug(name: str) -> str:
    players = load_players()
    n = norm_name(name)
    if n in players:
        n = norm_name(players[n])
    parts = [p for p in n.split() if p]
    if len(parts) >= 2:
        return f"{parts[0]}_{parts[-1]}"
    return (parts[-1] if parts else n).replace(" ", "_")


def _slug_entity(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return ""
    players = load_players()
    n = norm_name(raw)
    if n in players:
        return _player_slug(players[n])
    parts = n.split()
    if parts:
        last = parts[-1]
        for alias, canon in players.items():
            cn = norm_name(canon)
            if cn == n or (cn.split() and cn.split()[-1] == last):
                return _player_slug(canon)
            if alias.split() and alias.split()[-1] == last:
                return _player_slug(canon)
        if len(last) >= 4:
            return last.replace(" ", "_")
    team = canonical_team(raw)
    if team:
        return norm_name(team).replace(" ", "_")
    return n.replace(" ", "_")


def story_key(item: NewsItem | dict[str, Any]) -> str:
    """Ключ сюжета: entity|family. Матч схлопывает превью/счёт/реакции."""
    entities = _entities_of(item)
    title, body = _title_body(item)
    blob = f"{title}\n{body[:800]}"
    et = _event_type(item)
    players = [_slug_entity(str(p)) for p in (entities.get("players") or []) if str(p).strip()]
    players = [p for p in players if p]
    # если entities пусты — вытащить игрока из текста (иначе Батраков×N не схлопнется)
    if not players:
        blob_n = norm_name(blob)
        for alias, canon in sorted(load_players().items(), key=lambda kv: len(kv[0]), reverse=True):
            if len(alias) < 4:
                continue
            if alias in blob_n:
                slug = _player_slug(canon)
                if slug:
                    players = [slug]
                    break
    teams = sorted(
        {_slug_entity(str(t)) for t in (entities.get("teams") or []) if str(t).strip()}
    )
    teams = [t for t in teams if t]

    transferish = et == "transfer" or bool(_TRANSFER_MARKERS.search(blob))
    matchish = et in {"match_result", "lineup"} or (
        len(teams) >= 2 and bool(_MATCH_MARKERS.search(blob))
    )

    if players and transferish:
        return f"{players[0]}|transfer"
    if len(teams) >= 2 and matchish:
        day = ""
        if isinstance(item, NewsItem) and item.published_at:
            day = item.published_at.strftime("%Y-%m-%d")
        elif isinstance(item, dict):
            day = str(item.get("source_published_at") or "")[:10]
        if day:
            return f"{teams[0]}+{teams[1]}|{day}|match"
        return f"{teams[0]}+{teams[1]}|match"
    if players:
        return f"{players[0]}|{et or 'other'}"
    if teams:
        return f"{teams[0]}|{et or 'other'}"
    return f"other|{et or 'other'}"


def is_official(item: NewsItem | dict[str, Any]) -> bool:
    if _event_type(item) == "official_statement":
        return True
    title, body = _title_body(item)
    return bool(_OFFICIAL_MARKERS.search(f"{title}\n{body[:600]}"))


def subtype_rank(item: NewsItem | dict[str, Any]) -> int:
    if is_official(item):
        return RANK_OFFICIAL
    et = _event_type(item)
    title, body = _title_body(item)
    blob = f"{title}\n{body[:600]}"
    if et == "transfer" and re.search(r"подписал|переш|€|£|\d+\s*млн", blob, re.I):
        return RANK_CONFIRMED
    if et == "match_result":
        return RANK_MATCH
    if et == "transfer":
        return RANK_LOW
    if re.search(r"слух|rumor|интерес|может|рассматрива", blob, re.I):
        return RANK_LOW
    return RANK_LOW


def _story_rows(channel_slug: str, key: str, day: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM editorial_story_log
            WHERE channel_slug=? AND story_key=? AND day=?
            ORDER BY post_index ASC, id ASC
            """,
            (channel_slug, key, day),
        ).fetchall()
    return [dict(r) for r in rows]


def current_post_index(channel_slug: str, *, day: str | None = None) -> int:
    day = day or channel_day()
    with db() as conn:
        row = conn.execute(
            """
            SELECT MAX(post_index) AS mx FROM editorial_story_log
            WHERE channel_slug=? AND day=?
            """,
            (channel_slug, day),
        ).fetchone()
    try:
        return int(row["mx"] or 0) if row else 0
    except (TypeError, ValueError):
        return 0


def can_publish_story(
    channel_slug: str,
    key: str,
    subtype: int,
    *,
    day: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Проверка перед постановкой в очередь / публикацией."""
    conf = throttle_config()
    day = day or channel_day(now)
    story_rows = _story_rows(channel_slug, key, day)
    n = len(story_rows)
    if n >= conf.hard_cap:
        return False, f"story hard cap {conf.hard_cap}"

    if story_rows:
        last = story_rows[-1]
        last_idx = int(last.get("post_index") or 0)
        with db() as conn:
            since = conn.execute(
                """
                SELECT story_key FROM editorial_story_log
                WHERE channel_slug=? AND day=? AND post_index > ?
                ORDER BY post_index ASC
                """,
                (channel_slug, day, last_idx),
            ).fetchall()
        other_since = sum(1 for r in since if str(r["story_key"]) != key)
        ok_posts = other_since >= conf.min_gap_posts
        ok_time = False
        try:
            posted_raw = str(last.get("posted_at") or "")
            posted = datetime.fromisoformat(posted_raw.replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            dt_now = now or datetime.now(timezone.utc)
            if dt_now.tzinfo is None:
                dt_now = dt_now.replace(tzinfo=timezone.utc)
            ok_time = (dt_now - posted) >= timedelta(minutes=conf.min_gap_min)
        except Exception:
            ok_time = False
        if not (ok_posts or ok_time):
            return (
                False,
                f"story gap: need {conf.min_gap_posts} posts or {conf.min_gap_min}m",
            )

    if n < conf.max_per_day:
        return True, "ok"

    max_rank = max(int(r.get("subtype_rank") or 1) for r in story_rows)
    if subtype > max_rank and n < conf.hard_cap:
        return True, "official upgrade"
    if subtype >= RANK_OFFICIAL and max_rank < RANK_OFFICIAL and n < conf.hard_cap:
        return True, "official upgrade"
    return False, f"story day limit {conf.max_per_day}"


def record_story_post(
    channel_slug: str,
    key: str,
    news_id: int | str,
    subtype: int,
    *,
    day: str | None = None,
    post_index: int | None = None,
) -> None:
    day = day or channel_day()
    idx = post_index
    if idx is None:
        idx = current_post_index(channel_slug, day=day) + 1
    with db() as conn:
        conn.execute(
            """
            INSERT INTO editorial_story_log
              (channel_slug, story_key, news_id, subtype_rank, day, post_index)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (channel_slug, key, str(news_id), int(subtype), day, int(idx)),
        )


def story_gate(
    channel_slug: str,
    item: NewsItem | dict[str, Any],
    *,
    day: str | None = None,
) -> tuple[bool, str, str, int]:
    """Удобный вход: (ok, reason, story_key, subtype_rank)."""
    key = story_key(item)
    rank = subtype_rank(item)
    ok, reason = can_publish_story(channel_slug, key, rank, day=day)
    return ok, reason, key, rank
