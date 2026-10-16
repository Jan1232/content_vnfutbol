from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from predlozka.config import db_path
from predlozka.logic import make_slug

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    chat_type TEXT NOT NULL DEFAULT 'channel',
    slug TEXT NOT NULL UNIQUE,
    owner_user_id INTEGER NOT NULL,
    added_by_user_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    started_at REAL NOT NULL,
    FOREIGN KEY(chat_id) REFERENCES channels(chat_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_channels_owner ON channels(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_channels_slug ON channels(slug);

CREATE TABLE IF NOT EXISTS bans (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    banned_by INTEGER,
    created_at REAL NOT NULL,
    PRIMARY KEY (chat_id, user_id),
    FOREIGN KEY(chat_id) REFERENCES channels(chat_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_submissions_rate
    ON submissions(chat_id, user_id, created_at);

CREATE TABLE IF NOT EXISTS admin_state (
    user_id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    chat_id INTEGER,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS senders (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS bad_users (
    user_id INTEGER PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_chat_id INTEGER,
    first_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bad_users_source ON bad_users(source);
CREATE INDEX IF NOT EXISTS idx_bad_users_updated ON bad_users(updated_at);

CREATE TABLE IF NOT EXISTS erid_ads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    mid TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(day, chat_id, mid)
);
CREATE INDEX IF NOT EXISTS idx_erid_ads_day_chat ON erid_ads(day, chat_id);
"""

DEFAULT_RATE_PER_HOUR = 10
DEFAULT_MAX_CHARS = 2000


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_file = path or db_path()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_channels(conn: sqlite3.Connection) -> None:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(channels)")}
    if "rate_per_hour" not in cols:
        conn.execute(
            "ALTER TABLE channels ADD COLUMN rate_per_hour INTEGER NOT NULL DEFAULT 10"
        )
    if "max_chars" not in cols:
        conn.execute(
            "ALTER TABLE channels ADD COLUMN max_chars INTEGER NOT NULL DEFAULT 2000"
        )


def _migrate_erid_ads(conn: sqlite3.Connection) -> None:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(erid_ads)")}
    if cols and "views" not in cols:
        conn.execute(
            "ALTER TABLE erid_ads ADD COLUMN views INTEGER NOT NULL DEFAULT 0"
        )


def init_db(path: Path | None = None) -> None:
    with db(path) as conn:
        conn.executescript(SCHEMA)
        _migrate_channels(conn)
        _migrate_erid_ads(conn)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def list_active_channels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM channels
        WHERE status='active'
        ORDER BY title COLLATE NOCASE, chat_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_erid_ad(
    conn: sqlite3.Connection,
    *,
    day: str,
    chat_id: int,
    mid: str,
    url: str,
    views: int = 0,
) -> bool:
    """True, если запись новая. Просмотры обновляются при каждом заходе."""
    day_s = (day or "").strip()
    mid_s = (mid or "").strip()
    url_s = (url or "").strip()
    existed = conn.execute(
        "SELECT 1 FROM erid_ads WHERE day=? AND chat_id=? AND mid=?",
        (day_s, int(chat_id), mid_s),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO erid_ads(day, chat_id, mid, url, views, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(day, chat_id, mid) DO UPDATE SET
            views=excluded.views,
            url=CASE WHEN excluded.url != '' THEN excluded.url ELSE erid_ads.url END
        """,
        (day_s, int(chat_id), mid_s, url_s, max(0, int(views or 0)), time.time()),
    )
    return existed is None


def list_erid_ads_for_day(
    conn: sqlite3.Connection, day: str, chat_id: int | None = None
) -> list[dict[str, Any]]:
    if chat_id is None:
        rows = conn.execute(
            """
            SELECT * FROM erid_ads
            WHERE day=?
            ORDER BY chat_id, id
            """,
            ((day or "").strip(),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM erid_ads
            WHERE day=? AND chat_id=?
            ORDER BY id
            """,
            ((day or "").strip(), int(chat_id)),
        ).fetchall()
    return [dict(r) for r in rows]


def prune_erid_ads(conn: sqlite3.Connection, keep_day: str) -> int:
    cur = conn.execute(
        "DELETE FROM erid_ads WHERE day!=?",
        ((keep_day or "").strip(),),
    )
    return int(cur.rowcount or 0)


def get_channel(conn: sqlite3.Connection, chat_id: int) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM channels WHERE chat_id=?", (int(chat_id),)).fetchone())


def get_channel_by_slug(conn: sqlite3.Connection, slug: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM channels WHERE slug=? AND status='active'",
            (slug.strip(),),
        ).fetchone()
    )


def owner_channels(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    uid = int(user_id)
    rows = conn.execute(
        """
        SELECT * FROM channels
        WHERE status='active'
          AND (owner_user_id=? OR added_by_user_id=?)
        ORDER BY title COLLATE NOCASE, chat_id
        """,
        (uid, uid),
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_channel(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    title: str,
    chat_type: str,
    owner_user_id: int,
    added_by_user_id: int | None,
) -> dict[str, Any]:
    now = time.time()
    existing = get_channel(conn, chat_id)
    if existing:
        conn.execute(
            """
            UPDATE channels
            SET title=?, chat_type=?, owner_user_id=?, added_by_user_id=?,
                status='active', updated_at=?
            WHERE chat_id=?
            """,
            (
                (title or existing["title"] or "").strip() or existing["title"],
                chat_type or existing["chat_type"],
                int(owner_user_id),
                int(added_by_user_id) if added_by_user_id is not None else existing["added_by_user_id"],
                now,
                int(chat_id),
            ),
        )
        row = get_channel(conn, chat_id)
        assert row is not None
        return row

    slug = make_slug()
    for _ in range(8):
        try:
            conn.execute(
                """
                INSERT INTO channels(
                    chat_id, title, chat_type, slug, owner_user_id,
                    added_by_user_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    int(chat_id),
                    (title or "").strip() or f"канал {chat_id}",
                    chat_type or "channel",
                    slug,
                    int(owner_user_id),
                    int(added_by_user_id) if added_by_user_id is not None else None,
                    now,
                    now,
                ),
            )
            break
        except sqlite3.IntegrityError:
            slug = make_slug()
    row = get_channel(conn, chat_id)
    assert row is not None
    return row


def set_channel_status(conn: sqlite3.Connection, chat_id: int, status: str) -> None:
    conn.execute(
        "UPDATE channels SET status=?, updated_at=? WHERE chat_id=?",
        (status, time.time(), int(chat_id)),
    )


def set_channel_title(conn: sqlite3.Connection, chat_id: int, title: str) -> None:
    conn.execute(
        "UPDATE channels SET title=?, updated_at=? WHERE chat_id=?",
        ((title or "").strip(), time.time(), int(chat_id)),
    )


def bind_session(conn: sqlite3.Connection, user_id: int, chat_id: int) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT INTO sessions(user_id, chat_id, started_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, started_at=excluded.started_at
        """,
        (int(user_id), int(chat_id), now),
    )


def get_session(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT s.user_id, s.chat_id, s.started_at,
               c.title, c.slug, c.owner_user_id, c.added_by_user_id, c.status,
               c.rate_per_hour, c.max_chars
        FROM sessions s
        JOIN channels c ON c.chat_id = s.chat_id
        WHERE s.user_id=?
        """,
        (int(user_id),),
    ).fetchone()
    return _row(row)


def drop_session(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM sessions WHERE user_id=?", (int(user_id),))


def remember_sender(
    conn: sqlite3.Connection,
    chat_id: int,
    user_id: int,
    label: str,
    username: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO senders(chat_id, user_id, label, username, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            label=excluded.label,
            username=excluded.username,
            updated_at=excluded.updated_at
        """,
        (int(chat_id), int(user_id), (label or "").strip(), (username or "").strip(), time.time()),
    )


def get_sender(conn: sqlite3.Connection, chat_id: int, user_id: int) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM senders WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        ).fetchone()
    )


def channel_settings(channel: dict[str, Any] | None) -> tuple[int, int]:
    if not channel:
        return DEFAULT_RATE_PER_HOUR, DEFAULT_MAX_CHARS
    try:
        rate = int(channel.get("rate_per_hour", DEFAULT_RATE_PER_HOUR))
    except (TypeError, ValueError):
        rate = DEFAULT_RATE_PER_HOUR
    try:
        max_chars = int(channel.get("max_chars", DEFAULT_MAX_CHARS))
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    return rate, max_chars


def set_channel_setting(conn: sqlite3.Connection, chat_id: int, field: str, value: int) -> None:
    if field not in {"rate_per_hour", "max_chars"}:
        raise ValueError(field)
    conn.execute(
        f"UPDATE channels SET {field}=?, updated_at=? WHERE chat_id=?",
        (int(value), time.time(), int(chat_id)),
    )


def is_banned(conn: sqlite3.Connection, chat_id: int, user_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM bans WHERE chat_id=? AND user_id=?",
        (int(chat_id), int(user_id)),
    ).fetchone()
    return row is not None


def ban_user(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    user_id: int,
    label: str,
    username: str = "",
    banned_by: int | None = None,
) -> bool:
    already = is_banned(conn, chat_id, user_id)
    if not already:
        conn.execute(
            """
            INSERT INTO bans(chat_id, user_id, label, username, banned_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(chat_id),
                int(user_id),
                (label or "").strip() or f"id {user_id}",
                (username or "").strip(),
                int(banned_by) if banned_by is not None else None,
                time.time(),
            ),
        )
    sess = get_session(conn, user_id)
    if sess and int(sess["chat_id"]) == int(chat_id):
        drop_session(conn, user_id)
    return not already


def unban_user(conn: sqlite3.Connection, chat_id: int, user_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM bans WHERE chat_id=? AND user_id=?",
        (int(chat_id), int(user_id)),
    )
    return cur.rowcount > 0


def list_bans(conn: sqlite3.Connection, chat_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM bans WHERE chat_id=? ORDER BY created_at DESC",
        (int(chat_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def add_submission(conn: sqlite3.Connection, chat_id: int, user_id: int) -> None:
    conn.execute(
        "INSERT INTO submissions(chat_id, user_id, created_at) VALUES (?, ?, ?)",
        (int(chat_id), int(user_id), time.time()),
    )


def count_submissions_hour(conn: sqlite3.Connection, chat_id: int, user_id: int) -> int:
    since = time.time() - 3600
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM submissions
        WHERE chat_id=? AND user_id=? AND created_at>=?
        """,
        (int(chat_id), int(user_id), since),
    ).fetchone()
    return int(row["n"]) if row else 0


def get_admin_state(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    return _row(
        conn.execute("SELECT * FROM admin_state WHERE user_id=?", (int(user_id),)).fetchone()
    )


def set_admin_state(
    conn: sqlite3.Connection,
    user_id: int,
    action: str,
    chat_id: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO admin_state(user_id, action, chat_id, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            action=excluded.action,
            chat_id=excluded.chat_id,
            updated_at=excluded.updated_at
        """,
        (int(user_id), action, int(chat_id) if chat_id is not None else None, time.time()),
    )


def clear_admin_state(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM admin_state WHERE user_id=?", (int(user_id),))


def upsert_bad_user(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    reason: str = "",
    source: str = "",
    source_chat_id: int | None = None,
    first_name: str = "",
    username: str = "",
) -> None:
    uid = int(user_id)
    if uid <= 0:
        return
    now = time.time()
    conn.execute(
        """
        INSERT INTO bad_users(
            user_id, reason, source, source_chat_id, first_name, username,
            hits, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            reason=CASE
                WHEN excluded.reason != '' THEN excluded.reason
                ELSE bad_users.reason
            END,
            source=CASE
                WHEN excluded.source != '' THEN excluded.source
                ELSE bad_users.source
            END,
            source_chat_id=COALESCE(excluded.source_chat_id, bad_users.source_chat_id),
            first_name=CASE
                WHEN excluded.first_name != '' THEN excluded.first_name
                ELSE bad_users.first_name
            END,
            username=CASE
                WHEN excluded.username != '' THEN excluded.username
                ELSE bad_users.username
            END,
            hits=bad_users.hits + 1,
            updated_at=excluded.updated_at
        """,
        (
            uid,
            (reason or "").strip(),
            (source or "").strip(),
            int(source_chat_id) if source_chat_id is not None else None,
            (first_name or "").strip(),
            (username or "").strip(),
            now,
            now,
        ),
    )


def upsert_bad_users(
    conn: sqlite3.Connection,
    user_ids: list[int],
    *,
    reason: str = "",
    source: str = "",
    source_chat_id: int | None = None,
    profiles: dict[int, dict[str, Any]] | None = None,
) -> int:
    """Массовый upsert. Возвращает число переданных id (>0)."""
    profiles = profiles or {}
    n = 0
    for uid in user_ids:
        uid_i = int(uid)
        if uid_i <= 0:
            continue
        prof = profiles.get(uid_i) or {}
        upsert_bad_user(
            conn,
            uid_i,
            reason=reason,
            source=source,
            source_chat_id=source_chat_id,
            first_name=str(prof.get("first_name") or ""),
            username=str(prof.get("username") or ""),
        )
        n += 1
    return n


def is_bad_user(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM bad_users WHERE user_id=? LIMIT 1",
        (int(user_id),),
    ).fetchone()
    return row is not None


def bad_users_set(conn: sqlite3.Connection, user_ids: list[int] | None = None) -> set[int]:
    if user_ids is not None:
        ids = [int(x) for x in user_ids if int(x) > 0]
        if not ids:
            return set()
        out: set[int] = set()
        # sqlite variable limit — чанками
        for i in range(0, len(ids), 400):
            chunk = ids[i : i + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT user_id FROM bad_users WHERE user_id IN ({placeholders})",
                chunk,
            ).fetchall()
            out.update(int(r["user_id"]) for r in rows)
        return out
    rows = conn.execute("SELECT user_id FROM bad_users").fetchall()
    return {int(r["user_id"]) for r in rows}


def count_bad_users(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM bad_users").fetchone()
    return int(row["n"] if row else 0)
