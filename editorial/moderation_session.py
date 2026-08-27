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


def get_awaiting_input_session(admin_id: int | str) -> dict[str, Any] | None:
    """Единственная сессия админа, ждущая текстового ввода."""
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM editorial_moderation_session
            WHERE admin_id=? AND step IN ('photo_query', 'edit_text')
            ORDER BY id DESC LIMIT 1
            """,
            (str(admin_id),),
        ).fetchone()
    return dict(row) if row else None


def get_session_by_prompt_message(admin_id: int | str, message_id: int) -> dict[str, Any] | None:
    """Сессия по reply: prompt_message_id или tg_message_id карточки."""
    mid = int(message_id or 0)
    if mid <= 0:
        return None
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM editorial_moderation_session
            WHERE admin_id=?
              AND step NOT IN ('idle', 'done')
              AND (
                COALESCE(prompt_message_id, 0)=?
                OR tg_message_id=?
              )
            ORDER BY id DESC LIMIT 1
            """,
            (str(admin_id), mid, mid),
        ).fetchone()
    return dict(row) if row else None


def clear_input_step(admin_id: int | str, *, except_news_id: int) -> int:
    """Снять ожидание ввода с прочих сессий админа → review (пост не теряем)."""
    with db() as conn:
        cur = conn.execute(
            """
            UPDATE editorial_moderation_session
            SET step='review', prompt_message_id=0, updated_at=?
            WHERE admin_id=?
              AND step IN ('photo_query', 'edit_text')
              AND news_id != ?
            """,
            (utcnow_iso(), str(admin_id), int(except_news_id)),
        )
        return int(cur.rowcount or 0)


def upsert_session(
    news_id: int,
    *,
    admin_id: int | str,
    step: str = "idle",
    tg_chat_id: int | str = "",
    tg_message_id: int = 0,
    prompt_message_id: int | None = None,
    draft_text: str = "",
    photo_query: str = "",
    photo_pool: list[dict[str, Any]] | None = None,
) -> None:
    pool_json = json.dumps(photo_pool or [], ensure_ascii=False)
    with db() as conn:
        row = conn.execute(
            "SELECT id, tg_message_id, prompt_message_id FROM editorial_moderation_session "
            "WHERE news_id=? ORDER BY id DESC LIMIT 1",
            (int(news_id),),
        ).fetchone()
        if row:
            keep_card = int(tg_message_id or 0) or int(row["tg_message_id"] or 0)
            if prompt_message_id is None:
                try:
                    keep_prompt = int(row["prompt_message_id"] or 0)
                except (KeyError, IndexError, TypeError):
                    keep_prompt = 0
            else:
                keep_prompt = int(prompt_message_id or 0)
            conn.execute(
                """
                UPDATE editorial_moderation_session
                SET admin_id=?, step=?, tg_chat_id=?, tg_message_id=?,
                    prompt_message_id=?,
                    draft_text=?, photo_query=?, photo_pool_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    str(admin_id),
                    step,
                    str(tg_chat_id),
                    keep_card,
                    keep_prompt,
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
                  (news_id, admin_id, step, tg_chat_id, tg_message_id, prompt_message_id,
                   draft_text, photo_query, photo_pool_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(news_id),
                    str(admin_id),
                    step,
                    str(tg_chat_id),
                    int(tg_message_id or 0),
                    int(prompt_message_id or 0),
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
