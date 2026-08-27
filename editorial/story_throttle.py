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
from editorial.catalogs import load_players, norm_name
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
_INCIDENT_MARKERS = re.compile(
    r"драк|потасовк|стычк|конфликт|скандал|дисквалиф|санкц|разбирательств|"
    r"апелляц|расследов|наказани|отстранил|\bбан\b|red card|удалени",
    re.I,
)
_INCIDENT_UPGRADE_MARKERS = re.compile(
    r"дисквалиф|санкц|вердикт|отстранил|\bбан\b|апелляц|федерац|"
    r"травм|перелом|растяжен",
    re.I,
)


@dataclass(frozen=True)
class StoryThrottleConfig:
    max_per_day: int = 3
    hard_cap: int = 4
    min_gap_posts: int = 3
    min_gap_min: int = 180
    incident_window_days: int = 3
    llm_relation_enabled: bool = True


def throttle_config() -> StoryThrottleConfig:
    s = get_settings()
    return StoryThrottleConfig(
        max_per_day=int(getattr(s, "story_max_per_day", 3) or 3),
        hard_cap=int(getattr(s, "story_hard_cap", 4) or 4),
        min_gap_posts=int(getattr(s, "story_min_gap_posts", 3) or 3),
        min_gap_min=int(getattr(s, "story_min_gap_min", 180) or 180),
        incident_window_days=int(getattr(s, "story_incident_window_days", 3) or 3),
        llm_relation_enabled=bool(getattr(s, "story_llm_relation_enabled", True)),
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
    return str(item.get("title") or ""), str(item.get("body") or item.get("post_text") or "")


def _event_type(item: NewsItem | dict[str, Any]) -> str:
    if isinstance(item, NewsItem):
        return str(item.event_type or "other")
    return str(item.get("event_type") or "other")


def _player_slug(name: str) -> str:
    """Фамилия (стабильный ключ: «Батраков» == «Алексей Батраков»)."""
    players = load_players()
    n = norm_name(name)
    if n in players:
        n = norm_name(players[n])
    parts = [p for p in n.split() if p]
    return (parts[-1] if parts else n).replace(" ", "_")


def _slug_entity(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return ""
    from editorial.catalogs import load_team_aliases

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
    # только реальный алиас клуба (canonical_team иначе вернёт вход как есть)
    aliases = load_team_aliases()
    if n in aliases:
        return norm_name(aliases[n]).replace(" ", "_")
    if parts and len(parts[-1]) >= 4:
        return parts[-1].replace(" ", "_")
    return n.replace(" ", "_")


def _extract_players_teams(
    item: NewsItem | dict[str, Any], blob: str
) -> tuple[list[str], list[str]]:
    entities = _entities_of(item)
    players = [_slug_entity(str(p)) for p in (entities.get("players") or []) if str(p).strip()]
    players = [p for p in players if p]
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
    return players, teams


def _item_day(item: NewsItem | dict[str, Any]) -> str:
    if isinstance(item, NewsItem) and item.published_at:
        return item.published_at.strftime("%Y-%m-%d")
    if isinstance(item, dict):
        raw = str(item.get("source_published_at") or item.get("published_at") or "")[:10]
        if len(raw) == 10:
            return raw
    return channel_day()


def story_key(item: NewsItem | dict[str, Any]) -> str:
    """Ключ сюжета: entity|family. Матч/инцидент схлопывают цепочки углов."""
    title, body = _title_body(item)
    blob = f"{title}\n{body[:800]}"
    et = _event_type(item)
    players, teams = _extract_players_teams(item, blob)

    transferish = et == "transfer" or bool(_TRANSFER_MARKERS.search(blob))
    matchish = et in {"match_result", "lineup"} or (
        len(teams) >= 2 and bool(_MATCH_MARKERS.search(blob))
    )
    incidentish = bool(_INCIDENT_MARKERS.search(blob))

    if players and transferish:
        return f"{players[0]}|transfer"
    if len(teams) >= 2 and matchish and not incidentish:
        day = _item_day(item)
        if day:
            return f"{teams[0]}+{teams[1]}|{day}|match"
        return f"{teams[0]}+{teams[1]}|match"
    # инцидент: драка → санкции → травма — один ключ, независимо от event_type
    if incidentish:
        day = _item_day(item)
        if len(teams) >= 2:
            if day:
                return f"{teams[0]}+{teams[1]}|{day}|incident"
            return f"{teams[0]}+{teams[1]}|incident"
        if players:
            return f"{players[0]}|incident"
        if teams:
            return f"{teams[0]}|incident"
        return f"other|{day}|incident" if day else "other|incident"
    if players:
        return f"{players[0]}|{et or 'other'}"
    if teams:
        return f"{teams[0]}|{et or 'other'}"
    return f"other|{et or 'other'}"


def is_incident_key(key: str) -> bool:
    return str(key or "").endswith("|incident") or "|incident" in str(key or "")


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
    if _INCIDENT_MARKERS.search(blob) and _INCIDENT_UPGRADE_MARKERS.search(blob):
        if re.search(r"дисквалиф|санкц|вердикт|отстранил|\bбан\b|федерац", blob, re.I):
            return RANK_OFFICIAL
        return RANK_CONFIRMED
    if et == "transfer" and re.search(r"подписал|переш|€|£|\d+\s*млн", blob, re.I):
        return RANK_CONFIRMED
    if et == "match_result":
        return RANK_MATCH
    if et == "transfer":
        return RANK_LOW
    if re.search(r"слух|rumor|интерес|может|рассматрива", blob, re.I):
        return RANK_LOW
    return RANK_LOW


def make_story_summary(item: NewsItem | dict[str, Any]) -> str:
    title, body = _title_body(item)
    post = ""
    if isinstance(item, dict):
        post = str(item.get("post_text") or "").strip()
    text = " ".join(x for x in (title.strip(), (post or body).strip()) if x)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


def _window_days_list(day: str, window_days: int) -> list[str]:
    try:
        d0 = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return [day]
    out: list[str] = []
    for i in range(max(1, int(window_days))):
        out.append((d0 - timedelta(days=i)).strftime("%Y-%m-%d"))
    return out


def _story_rows(
    channel_slug: str,
    key: str,
    day: str,
    *,
    window_days: int = 1,
) -> list[dict[str, Any]]:
    days = _window_days_list(day, window_days)
    placeholders = ",".join("?" for _ in days)
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM editorial_story_log
            WHERE channel_slug=? AND story_key=? AND day IN ({placeholders})
            ORDER BY posted_at ASC, id ASC
            """,
            (channel_slug, key, *days),
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


def _parse_posted_at(raw: str) -> datetime | None:
    try:
        posted = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return posted
    except Exception:
        return None


def _gap_ok(
    channel_slug: str,
    key: str,
    story_rows: list[dict[str, Any]],
    conf: StoryThrottleConfig,
    *,
    day: str,
    now: datetime | None,
) -> tuple[bool, str]:
    if not story_rows:
        return True, "ok"
    last = story_rows[-1]
    posted_raw = str(last.get("posted_at") or "")
    other_since = 0
    with db() as conn:
        if posted_raw:
            last_id = int(last.get("id") or 0)
            since = conn.execute(
                """
                SELECT story_key FROM editorial_story_log
                WHERE channel_slug=?
                  AND (
                    datetime(posted_at) > datetime(?)
                    OR (datetime(posted_at) = datetime(?) AND id > ?)
                  )
                """,
                (channel_slug, posted_raw, posted_raw, last_id),
            ).fetchall()
            other_since = sum(1 for r in since if str(r["story_key"]) != key)
        else:
            last_idx = int(last.get("post_index") or 0)
            last_day = str(last.get("day") or day)
            since = conn.execute(
                """
                SELECT story_key FROM editorial_story_log
                WHERE channel_slug=? AND day=? AND post_index > ?
                """,
                (channel_slug, last_day, last_idx),
            ).fetchall()
            other_since = sum(1 for r in since if str(r["story_key"]) != key)
    ok_posts = other_since >= conf.min_gap_posts
    ok_time = False
    posted = _parse_posted_at(posted_raw)
    if posted is not None:
        dt_now = now or datetime.now(timezone.utc)
        if dt_now.tzinfo is None:
            dt_now = dt_now.replace(tzinfo=timezone.utc)
        ok_time = (dt_now - posted) >= timedelta(minutes=conf.min_gap_min)
    if not (ok_posts or ok_time):
        return (
            False,
            f"story gap: need {conf.min_gap_posts} posts or {conf.min_gap_min}m",
        )
    return True, "ok"


def can_publish_story(
    channel_slug: str,
    key: str,
    subtype: int,
    *,
    day: str | None = None,
    now: datetime | None = None,
    window_days: int | None = None,
    as_development: bool = False,
) -> tuple[bool, str]:
    """Проверка перед постановкой в очередь / публикацией."""
    conf = throttle_config()
    day = day or channel_day(now)
    if window_days is None:
        window_days = conf.incident_window_days if is_incident_key(key) else 1
    story_rows = _story_rows(channel_slug, key, day, window_days=window_days)
    n = len(story_rows)
    if n >= conf.hard_cap:
        return False, f"story hard cap {conf.hard_cap}"

    gap_ok, gap_reason = _gap_ok(channel_slug, key, story_rows, conf, day=day, now=now)
    if not gap_ok:
        return False, gap_reason

    if as_development:
        return True, "development"

    if n < conf.max_per_day:
        return True, "ok"

    max_rank = max(int(r.get("subtype_rank") or 1) for r in story_rows) if story_rows else 0
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
    summary: str = "",
) -> None:
    day = day or channel_day()
    idx = post_index
    if idx is None:
        idx = current_post_index(channel_slug, day=day) + 1
    with db() as conn:
        conn.execute(
            """
            INSERT INTO editorial_story_log
              (channel_slug, story_key, news_id, subtype_rank, day, post_index, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_slug,
                key,
                str(news_id),
                int(subtype),
                day,
                int(idx),
                (summary or "")[:400],
            ),
        )


def _llm_fallback(
    item: NewsItem | dict[str, Any],
    rank: int,
    story_rows: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Без LLM: выше ранг → пропуск; иначе deferred."""
    max_rank = max(int(r.get("subtype_rank") or 1) for r in story_rows) if story_rows else 0
    if rank > max_rank:
        return True, "fallback rank upgrade"
    return False, "повтор сюжета (fallback, LLM недоступен)"


def story_gate(
    channel_slug: str,
    item: NewsItem | dict[str, Any],
    *,
    day: str | None = None,
) -> tuple[bool, str, str, int]:
    """Удобный вход: (ok, reason, story_key, subtype_rank)."""
    conf = throttle_config()
    key = story_key(item)
    rank = subtype_rank(item)
    day = day or channel_day()
    window = conf.incident_window_days if is_incident_key(key) else 1
    prior = _story_rows(channel_slug, key, day, window_days=window)

    if not prior:
        ok, reason = can_publish_story(
            channel_slug, key, rank, day=day, window_days=window
        )
        return ok, reason, key, rank

    # сюжет уже был → LLM только здесь
    if not conf.llm_relation_enabled:
        ok, reason = _llm_fallback(item, rank, prior)
        if ok:
            ok2, reason2 = can_publish_story(
                channel_slug,
                key,
                rank,
                day=day,
                window_days=window,
                as_development=True,
            )
            return ok2, reason2 if ok2 else reason2, key, max(rank, RANK_CONFIRMED)
        return False, reason, key, rank

    try:
        from editorial.llm import story_relation

        title, body = _title_body(item)
        summaries = [str(r.get("summary") or "").strip() for r in prior]
        summaries = [s for s in summaries if s] or [
            f"news_id={r.get('news_id')} rank={r.get('subtype_rank')}" for r in prior
        ]
        verdict = story_relation(title, body, summaries)
        relation = str(verdict.get("relation") or "").strip().lower()
        conf_score = float(verdict.get("confidence") or 0)
        print(
            f"[editorial] story_relation key={key} relation={relation} "
            f"conf={conf_score:.2f} reason={str(verdict.get('reason') or '')[:120]}",
            flush=True,
        )
    except Exception as e:
        print(f"[editorial] story_relation fail: {e}", flush=True)
        ok, reason = _llm_fallback(item, rank, prior)
        if ok:
            ok2, reason2 = can_publish_story(
                channel_slug,
                key,
                rank,
                day=day,
                window_days=window,
                as_development=True,
            )
            return (ok2, reason2 if ok2 else reason2, key, max(rank, RANK_CONFIRMED))
        return False, reason, key, rank

    if relation == "unrelated":
        return True, "unrelated", key, rank

    if relation == "duplicate":
        return False, "повтор сюжета (LLM)", key, rank

    if relation == "development":
        ok, reason = can_publish_story(
            channel_slug,
            key,
            max(rank, RANK_CONFIRMED),
            day=day,
            window_days=window,
            as_development=True,
        )
        return ok, reason if ok else reason, key, max(rank, RANK_CONFIRMED)

    # неизвестный вердикт → консервативно
    ok, reason = _llm_fallback(item, rank, prior)
    if ok:
        ok2, reason2 = can_publish_story(
            channel_slug,
            key,
            rank,
            day=day,
            window_days=window,
            as_development=True,
        )
        return ok2, reason2, key, rank
    return False, reason, key, rank
