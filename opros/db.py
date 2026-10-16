"""Очередь опросов и мета расписания — отдельно от app.db / content.db."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_key TEXT NOT NULL,
    question TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'published',
    created_at REAL NOT NULL,
    published_at REAL,
    max_message_id TEXT NOT NULL DEFAULT '',
    option_a TEXT NOT NULL DEFAULT '',
    option_b TEXT NOT NULL DEFAULT '',
    pair_key TEXT NOT NULL DEFAULT '',
    image_a_url TEXT NOT NULL DEFAULT '',
    image_b_url TEXT NOT NULL DEFAULT '',
    comment_message_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_posts_channel_created
    ON posts(channel_key, created_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _db_path() -> Path:
    settings = get_settings()
    path = Path(settings.opros_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        for col, decl in (
            ("option_a", "TEXT NOT NULL DEFAULT ''"),
            ("option_b", "TEXT NOT NULL DEFAULT ''"),
            ("pair_key", "TEXT NOT NULL DEFAULT ''"),
            ("image_a_url", "TEXT NOT NULL DEFAULT ''"),
            ("image_b_url", "TEXT NOT NULL DEFAULT ''"),
            ("comment_message_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            _ensure_column(conn, "posts", col, decl)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_posts_pair_key ON posts(pair_key)"
        )


def get_meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def next_publish_meta_key(channel_key: str) -> str:
    return f"next_publish_at:{channel_key}"


def get_next_publish_ts(conn: sqlite3.Connection, channel_key: str) -> float | None:
    raw = get_meta(conn, next_publish_meta_key(channel_key))
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def set_next_publish_ts(conn: sqlite3.Connection, channel_key: str, ts: float) -> None:
    set_meta(conn, next_publish_meta_key(channel_key), str(ts))


def recent_questions(conn: sqlite3.Connection, channel_key: str, limit: int = 30) -> list[str]:
    rows = conn.execute(
        """
        SELECT question FROM posts
        WHERE channel_key=? AND status='published'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (channel_key, max(1, int(limit))),
    ).fetchall()
    return [str(r["question"]).strip() for r in rows if str(r["question"]).strip()]


def question_exists(conn: sqlite3.Connection, channel_key: str, question: str) -> bool:
    needle = (question or "").strip().casefold()
    if not needle:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM posts
        WHERE channel_key=? AND lower(question)=?
        LIMIT 1
        """,
        (channel_key, needle),
    ).fetchone()
    return row is not None


def insert_post(
    conn: sqlite3.Connection,
    *,
    channel_key: str,
    question: str,
    text: str,
    message_id: str = "",
    status: str = "published",
    option_a: str = "",
    option_b: str = "",
    pair_key: str = "",
    image_a_url: str = "",
    image_b_url: str = "",
    comment_message_id: str = "",
) -> int:
    now = time.time()
    cur = conn.execute(
        """
        INSERT INTO posts (
            channel_key, question, text, status, created_at, published_at, max_message_id,
            option_a, option_b, pair_key, image_a_url, image_b_url, comment_message_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            channel_key,
            question.strip(),
            text,
            status,
            now,
            now if status == "published" else None,
            message_id or "",
            (option_a or "").strip(),
            (option_b or "").strip(),
            (pair_key or "").strip(),
            (image_a_url or "").strip(),
            (image_b_url or "").strip(),
            (comment_message_id or "").strip(),
        ),
    )
    return int(cur.lastrowid)


def set_comment_message_id(conn: sqlite3.Connection, post_id: int, comment_mid: str) -> None:
    conn.execute(
        "UPDATE posts SET comment_message_id=? WHERE id=?",
        ((comment_mid or "").strip(), int(post_id)),
    )


def make_pair_key(option_a: str, option_b: str) -> str:
    a = (option_a or "").strip().casefold()
    b = (option_b or "").strip().casefold()
    if not a or not b:
        return ""
    left, right = sorted((a, b))
    return f"{left}|{right}"


def pair_exists(conn: sqlite3.Connection, pair_key: str) -> bool:
    key = (pair_key or "").strip()
    if not key:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM posts
        WHERE pair_key=? AND status='published'
        LIMIT 1
        """,
        (key,),
    ).fetchone()
    return row is not None


def recent_pairs(conn: sqlite3.Connection, limit: int = 40) -> list[tuple[str, str]]:
    """Недавние футбольные пары со всех football-каналов (антидубли между каналами)."""
    rows = conn.execute(
        """
        SELECT option_a, option_b FROM posts
        WHERE pair_key != '' AND pair_key NOT LIKE 'quiz:%' AND status='published'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    out: list[tuple[str, str]] = []
    for r in rows:
        a = str(r["option_a"] or "").strip()
        b = str(r["option_b"] or "").strip()
        if a and b:
            out.append((a, b))
    return out


def recent_quiz_answers(conn: sqlite3.Connection, limit: int = 40) -> list[str]:
    """Ответы квизов со всех quiz-каналов (антидубли)."""
    rows = conn.execute(
        """
        SELECT option_a FROM posts
        WHERE pair_key LIKE 'quiz:%' AND status='published'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [str(r["option_a"]).strip() for r in rows if str(r["option_a"] or "").strip()]


def recent_quiz_image_names(conn: sqlite3.Connection, kind: str, limit: int = 80) -> set[str]:
    rows = conn.execute(
        """
        SELECT image_a_url FROM posts
        WHERE option_b=? AND image_a_url != '' AND status='published'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (kind, max(1, int(limit))),
    ).fetchall()
    names: set[str] = set()
    for r in rows:
        raw = str(r["image_a_url"] or "").strip()
        if not raw:
            continue
        names.add(Path(raw).name)
    return names


def last_quiz_kind(conn: sqlite3.Connection, channel_key: str) -> str:
    row = conn.execute(
        """
        SELECT option_b FROM posts
        WHERE channel_key=? AND pair_key LIKE 'quiz:%' AND status='published'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (channel_key,),
    ).fetchone()
    return str(row["option_b"] or "").strip() if row else ""


def count_posts(conn: sqlite3.Connection, channel_key: str | None = None) -> int:
    if channel_key:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM posts WHERE channel_key=?",
                (channel_key,),
            ).fetchone()[0]
        )
    return int(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0])


def set_last_error(conn: sqlite3.Connection, message: str) -> None:
    set_meta(conn, "last_error", message[:2000])
