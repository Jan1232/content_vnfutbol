"""Incremental TG donor cursor + text-hash dedup (round-9)."""

from __future__ import annotations

import hashlib
import re

from app.db import db, init_db

_WS = re.compile(r"\s+")


def normalize_text_for_hash(text: str) -> str:
    t = _WS.sub(" ", (text or "").strip().lower())
    return t[:4000]


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text_for_hash(text).encode("utf-8")).hexdigest()[:32]


def get_last_seen_id(handle: str) -> int:
    init_db()
    h = (handle or "").strip().lstrip("@").lower()
    if not h:
        return 0
    with db() as conn:
        row = conn.execute(
            "SELECT last_seen_id FROM tg_donor_cursor WHERE handle=? LIMIT 1",
            (h,),
        ).fetchone()
    try:
        return int(row["last_seen_id"] or 0) if row else 0
    except (TypeError, ValueError):
        return 0


def set_last_seen_id(handle: str, message_id: int) -> None:
    init_db()
    h = (handle or "").strip().lstrip("@").lower()
    if not h or message_id <= 0:
        return
    with db() as conn:
        conn.execute(
            """
            INSERT INTO tg_donor_cursor (handle, last_seen_id, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(handle) DO UPDATE SET
              last_seen_id=MAX(tg_donor_cursor.last_seen_id, excluded.last_seen_id),
              updated_at=datetime('now')
            """,
            (h, int(message_id)),
        )


def is_text_seen(handle: str, digest: str) -> bool:
    init_db()
    h = (handle or "").strip().lstrip("@").lower()
    if not h or not digest:
        return False
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM tg_donor_text_seen WHERE handle=? AND text_hash=? LIMIT 1",
            (h, digest),
        ).fetchone()
    return bool(row)


def mark_text_seen(handle: str, digest: str, *, post_id: int = 0) -> None:
    init_db()
    h = (handle or "").strip().lstrip("@").lower()
    if not h or not digest:
        return
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO tg_donor_text_seen (handle, text_hash, post_id, seen_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (h, digest, int(post_id or 0)),
        )
