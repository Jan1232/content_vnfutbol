"""SQLite helpers for the editorial contour."""

from __future__ import annotations

import json
from typing import Any

from app.db import db
from editorial.models import utcnow_iso

_UPDATABLE = {
    "cluster_id",
    "source",
    "url",
    "event_type",
    "competition",
    "is_national",
    "is_priority",
    "teams_json",
    "title",
    "post_text",
    "caption",
    "image_path",
    "cover_path",
    "topic_status",
    "factcheck_status",
    "factcheck_conf",
    "factcheck_sources",
    "factcheck_reason",
    "status",
    "mid",
    "chat_id",
    "published_at",
    "retry_count",
    "last_error",
    "score_key",
    "entities_json",
    "body",
    "lang",
    "source_published_at",
    "headline",
    "emoji_lead",
    "caption_line1",
    "caption_line2",
    "post_kind",
    "media_type",
    "media_path",
    "meme_source",
    "imagery_meta_json",
    "awaiting_review_at",
}


def row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def get_news(news_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM editorial_news WHERE id=?", (news_id,)).fetchone()
        return row_to_dict(row) if row else None


def get_by_external(channel_slug: str, external_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM editorial_news
            WHERE channel_slug=? AND external_id=?
            """,
            (channel_slug, external_id),
        ).fetchone()
        return row_to_dict(row) if row else None


def count_meme_source_today(
    channel_slug: str, *, day: str, source: str | None = None
) -> int:
    with db() as conn:
        if source:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM editorial_news
                WHERE channel_slug=? AND meme_source=1
                  AND source=?
                  AND substr(created_at, 1, 10)=?
                """,
                (channel_slug, source, day),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM editorial_news
                WHERE channel_slug=? AND meme_source=1
                  AND substr(created_at, 1, 10)=?
                """,
                (channel_slug, day),
            ).fetchone()
    try:
        return int(row["n"] or 0) if row else 0
    except (TypeError, ValueError):
        return 0


def top_stuck_errors(channel_slug: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Топ last_error среди застрявших (held/imaging/verifying)."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT last_error, COUNT(*) AS n
            FROM editorial_news
            WHERE channel_slug=?
              AND status IN ('held', 'imaging', 'verifying')
              AND TRIM(COALESCE(last_error, '')) != ''
            GROUP BY last_error
            ORDER BY n DESC
            LIMIT ?
            """,
            (channel_slug, int(limit)),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def insert_news(payload: dict[str, Any]) -> int | None:
    """Insert if new. Returns id or None if duplicate."""
    fields = [
        "channel_slug",
        "external_id",
        "cluster_id",
        "source",
        "url",
        "event_type",
        "competition",
        "is_national",
        "is_priority",
        "teams_json",
        "title",
        "body",
        "lang",
        "source_published_at",
        "entities_json",
        "status",
        "post_kind",
        "media_type",
        "media_path",
        "meme_source",
    ]
    _defaults: dict[str, Any] = {
        "post_kind": "news",
        "media_type": "",
        "media_path": "",
        "meme_source": 0,
    }
    values = [payload.get(k, _defaults.get(k)) for k in fields]
    placeholders = ", ".join("?" for _ in fields)
    cols = ", ".join(fields)
    try:
        with db() as conn:
            cur = conn.execute(
                f"INSERT INTO editorial_news ({cols}) VALUES ({placeholders})",
                values,
            )
            return int(cur.lastrowid)
    except Exception as e:
        if "UNIQUE" in str(e).upper() or "unique" in str(e).lower():
            return None
        raise


def update_news(news_id: int, **fields: Any) -> None:
    clean = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not clean:
        return
    clean["updated_at"] = utcnow_iso()
    assignments = ", ".join(f"{k}=?" for k in clean)
    values = list(clean.values()) + [news_id]
    with db() as conn:
        conn.execute(f"UPDATE editorial_news SET {assignments} WHERE id=?", values)


def bump_retry(news_id: int, error: str, *, max_retry: int) -> str:
    """Increment retry; return new status ('error' if exhausted)."""
    item = get_news(news_id)
    if not item:
        return "error"
    n = int(item.get("retry_count") or 0) + 1
    status = "error" if n >= max_retry else (item.get("status") or "new")
    update_news(
        news_id,
        retry_count=n,
        last_error=(error or "")[:800],
        status=status if status == "error" else item.get("status"),
    )
    if n >= max_retry:
        update_news(news_id, status="error", last_error=(error or "")[:800])
        return "error"
    return item.get("status") or "new"


def list_open_news(channel_slug: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM editorial_news
            WHERE channel_slug=?
              AND status IN (
                'new','deferred','verifying','confirmed','editing','imaging',
                'captioning','rendering'
              )
            ORDER BY id ASC
            """,
            (channel_slug,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def list_ready(channel_slug: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM editorial_news
            WHERE channel_slug=? AND status='ready'
            ORDER BY id DESC
            """,
            (channel_slug,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def list_by_status(
    channel_slug: str,
    statuses: list[str] | tuple[str, ...],
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    st = [str(s) for s in statuses if str(s).strip()]
    if not st:
        return []
    placeholders = ",".join("?" for _ in st)
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM editorial_news
            WHERE channel_slug=? AND status IN ({placeholders})
            ORDER BY id DESC
            LIMIT ?
            """,
            (channel_slug, *st, int(limit)),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def list_moderation(limit: int = 80) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM editorial_news
            WHERE status IN ('held','error')
               OR factcheck_status='uncertain'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def status_counts(channel_slug: str | None = None) -> list[dict[str, Any]]:
    with db() as conn:
        if channel_slug:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM editorial_news WHERE channel_slug=?
                GROUP BY status
                """,
                (channel_slug,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM editorial_news GROUP BY status"
            ).fetchall()
        return [row_to_dict(r) for r in rows]


def recent_published(channel_slug: str, limit: int = 12) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM editorial_news
            WHERE channel_slug=? AND status='published'
            ORDER BY published_at DESC, id DESC
            LIMIT ?
            """,
            (channel_slug, limit),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def list_covers(channel_slug: str, limit: int = 24) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM editorial_news
            WHERE channel_slug=?
              AND cover_path IS NOT NULL
              AND trim(cover_path) != ''
            ORDER BY id DESC
            LIMIT ?
            """,
            (channel_slug, limit),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def cluster_published(cluster_id: str, chat_id: str, score_key: str = "") -> bool:
    if not cluster_id:
        return False
    with db() as conn:
        if score_key:
            row = conn.execute(
                """
                SELECT id FROM editorial_news
                WHERE cluster_id=? AND chat_id=? AND status='published'
                  AND score_key=?
                LIMIT 1
                """,
                (cluster_id, str(chat_id), score_key),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id FROM editorial_news
                WHERE cluster_id=? AND chat_id=? AND status='published'
                LIMIT 1
                """,
                (cluster_id, str(chat_id)),
            ).fetchone()
        return row is not None


def list_recent_corpus(window_sec: int) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM editorial_news
            WHERE created_at >= datetime('now', ?)
            ORDER BY id DESC
            LIMIT 400
            """,
            (f"-{int(window_sec)} seconds",),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def record_domain(cluster_id: str, domain: str) -> None:
    if not cluster_id or not domain:
        return
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO editorial_seen_domains (cluster_id, domain)
            VALUES (?, ?)
            """,
            (cluster_id, domain.lower()),
        )


def cluster_domains(cluster_id: str) -> set[str]:
    if not cluster_id:
        return set()
    with db() as conn:
        rows = conn.execute(
            "SELECT domain FROM editorial_seen_domains WHERE cluster_id=?",
            (cluster_id,),
        ).fetchall()
        return {str(r["domain"]).lower() for r in rows}


def get_channel_state(slug: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM editorial_channel_state WHERE channel_slug=?",
            (slug,),
        ).fetchone()
        return row_to_dict(row) if row else {}


def upsert_channel_state(
    slug: str,
    *,
    last_published_at: str | None = None,
    next_slot_at: str | None = None,
    matchday_last_date: str | None = None,
) -> None:
    now = utcnow_iso()
    current = get_channel_state(slug)
    last = last_published_at if last_published_at is not None else current.get("last_published_at")
    nxt = next_slot_at if next_slot_at is not None else current.get("next_slot_at")
    md = matchday_last_date if matchday_last_date is not None else current.get("matchday_last_date")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO editorial_channel_state (
                channel_slug, last_published_at, next_slot_at, matchday_last_date, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_slug) DO UPDATE SET
                last_published_at=excluded.last_published_at,
                next_slot_at=excluded.next_slot_at,
                matchday_last_date=excluded.matchday_last_date,
                updated_at=excluded.updated_at
            """,
            (slug, last, nxt, md or "", now),
        )


def expire_stale(channel_slug: str, ttl_sec: int) -> int:
    with db() as conn:
        cur = conn.execute(
            """
            UPDATE editorial_news
            SET status='expired', last_error='TTL', updated_at=datetime('now')
            WHERE channel_slug=? AND status IN ('ready','deferred','awaiting_review')
              AND created_at < datetime('now', ?)
            """,
            (channel_slug, f"-{int(ttl_sec)} seconds"),
        )
        return int(cur.rowcount or 0)


def replace_fifa_top100(rows: list[dict[str, Any]]) -> None:
    now = utcnow_iso()
    with db() as conn:
        conn.execute("DELETE FROM fifa_top100")
        for row in rows:
            conn.execute(
                """
                INSERT INTO fifa_top100 (rank, team, team_ru, points, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(row["rank"]),
                    str(row["team"]),
                    str(row.get("team_ru") or ""),
                    row.get("points"),
                    now,
                ),
            )


def list_fifa_top100() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM fifa_top100 ORDER BY rank ASC").fetchall()
        return [row_to_dict(r) for r in rows]


def upsert_fixture(match: Any) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO fixtures_cache (
                provider_id, competition, home, away, kickoff_utc, status,
                score_home, score_away, stage, is_national, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(provider_id) DO UPDATE SET
                competition=excluded.competition,
                home=excluded.home,
                away=excluded.away,
                kickoff_utc=excluded.kickoff_utc,
                status=excluded.status,
                score_home=excluded.score_home,
                score_away=excluded.score_away,
                stage=excluded.stage,
                is_national=excluded.is_national,
                updated_at=excluded.updated_at
            """,
            (
                match.provider_id,
                match.competition,
                match.home,
                match.away,
                match.kickoff_utc.isoformat(),
                match.status,
                match.score_home,
                match.score_away,
                match.stage,
                1 if match.is_national else 0,
            ),
        )


def list_today_fixtures() -> list[dict[str, Any]]:
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    msk = ZoneInfo("Europe/Moscow")
    today = datetime.now(msk).date().isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM fixtures_cache ORDER BY kickoff_utc ASC"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for raw in rows:
        d = row_to_dict(raw)
        ko = str(d.get("kickoff_utc") or "")
        try:
            dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.astimezone(msk).date().isoformat() != today:
                continue
        except Exception:
            continue
        out.append(d)
    return out


def result_already_posted(provider_id: str, channel_slug: str) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM match_results_posted WHERE provider_id=? AND channel_slug=?",
            (provider_id, channel_slug),
        ).fetchone()
        return bool(row)


def mark_result_posted(provider_id: str, channel_slug: str, mid: str = "") -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO match_results_posted (provider_id, channel_slug, posted_at, mid)
            VALUES (?, ?, datetime('now'), ?)
            ON CONFLICT(provider_id) DO UPDATE SET
                channel_slug=excluded.channel_slug,
                posted_at=excluded.posted_at,
                mid=excluded.mid
            """,
            (provider_id, channel_slug, mid),
        )


def list_recent_results(channel_slug: str, limit: int = 8) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT p.provider_id, p.posted_at, p.mid,
                   f.competition, f.home, f.away, f.score_home, f.score_away, f.kickoff_utc
            FROM match_results_posted p
            LEFT JOIN fixtures_cache f ON f.provider_id = p.provider_id
            WHERE p.channel_slug=?
            ORDER BY p.posted_at DESC
            LIMIT ?
            """,
            (channel_slug, limit),
        ).fetchall()
        return [row_to_dict(r) for r in rows]
