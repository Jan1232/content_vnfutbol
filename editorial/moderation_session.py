"""SQLite sessions for Telegram moderation FSM."""

from __future__ import annotations

import json
from typing import Any

from app.db import db
from editorial.models import utcnow_iso


def get_session(news_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM editorial_moderation_session WHERE news_id=? ORDER BY id DESC LIMIT 1",
            (int(news_id),),
        ).fetchone()
    return dict(row) if row else None


def get_active_session_for_admin(admin_id: int | str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM editorial_moderation_session
            WHERE admin_id=? AND step NOT IN ('idle', 'done')
            ORDER BY id DESC LIMIT 1
            """,
            (str(admin_id),),
        ).fetchone()
    return dict(row) if row else None


def upsert_session(
    news_id: int,
    *,
    admin_id: int | str,
    step: str = "idle",
    tg_chat_id: int | str = "",
    tg_message_id: int = 0,
    draft_text: str = "",
    photo_query: str = "",
    photo_pool: list[dict[str, Any]] | None = None,
) -> None:
    pool_json = json.dumps(photo_pool or [], ensure_ascii=False)
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM editorial_moderation_session WHERE news_id=? ORDER BY id DESC LIMIT 1",
            (int(news_id),),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE editorial_moderation_session
                SET admin_id=?, step=?, tg_chat_id=?, tg_message_id=?,
                    draft_text=?, photo_query=?, photo_pool_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    str(admin_id),
                    step,
                    str(tg_chat_id),
                    int(tg_message_id),
                    draft_text,
                    photo_query,
                    pool_json,
                    utcnow_iso(),
                    int(row["id"]),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO editorial_moderation_session
                  (news_id, admin_id, step, tg_chat_id, tg_message_id,
                   draft_text, photo_query, photo_pool_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(news_id),
                    str(admin_id),
                    step,
                    str(tg_chat_id),
                    int(tg_message_id),
                    draft_text,
                    photo_query,
                    pool_json,
                    utcnow_iso(),
                ),
            )


def clear_session(news_id: int) -> None:
    upsert_session(int(news_id), admin_id="", step="done")


def photo_pool_from_session(session: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        raw = json.loads(session.get("photo_pool_json") or "[]")
        return raw if isinstance(raw, list) else []
    except Exception:
        return []
