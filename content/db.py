"""Каталог сериалов и очередь публикаций — отдельно от app.db."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    year INTEGER,
    seasons INTEGER,
    description TEXT NOT NULL DEFAULT '',
    poster_url TEXT NOT NULL DEFAULT '',
    genres TEXT NOT NULL DEFAULT '[]',
    url TEXT NOT NULL,
    published_at REAL,
    message_id TEXT NOT NULL DEFAULT '',
    first_seen_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _db_path() -> Path:
    settings = get_settings()
    path = Path(settings.content_db_path)
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
        _ensure_column(conn, "series", "genres", "TEXT NOT NULL DEFAULT '[]'")


def dump_genres(genres: Any) -> str:
    if not genres:
        return "[]"
    if isinstance(genres, str):
        raw = genres.strip()
        if raw.startswith("["):
            return raw
        parts = [x.strip() for x in raw.split(",") if x.strip()]
        return json.dumps(parts, ensure_ascii=False)
    return json.dumps([str(x).strip() for x in genres if str(x).strip()], ensure_ascii=False)


def load_genres(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    text = str(raw).strip()
    if not text:
        return ()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return tuple(x.strip() for x in text.split(",") if x.strip())
    if isinstance(data, list):
        return tuple(str(x).strip() for x in data if str(x).strip())
    return ()


def upsert_series(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT INTO series (
            id, slug, title, year, seasons, description, poster_url, genres, url,
            first_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            slug=excluded.slug,
            title=excluded.title,
            year=excluded.year,
            seasons=excluded.seasons,
            description=CASE
                WHEN length(excluded.description) > length(series.description)
                THEN excluded.description
                ELSE series.description
            END,
            poster_url=excluded.poster_url,
            genres=CASE
                WHEN length(excluded.genres) > length(COALESCE(series.genres, ''))
                THEN excluded.genres
                ELSE series.genres
            END,
            url=excluded.url,
            updated_at=excluded.updated_at
        """,
        (
            item["id"],
            item.get("slug") or "",
            item["title"],
            item.get("year"),
            item.get("seasons"),
            item.get("description") or "",
            item.get("poster_url") or "",
            dump_genres(item.get("genres")),
            item["url"],
            now,
            now,
        ),
    )


def count_series(conn: sqlite3.Connection) -> tuple[int, int]:
    total = int(conn.execute("SELECT COUNT(*) FROM series").fetchone()[0])
    unpublished = int(
        conn.execute("SELECT COUNT(*) FROM series WHERE published_at IS NULL").fetchone()[0]
    )
    return total, unpublished


def pick_unpublished(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM series
        WHERE published_at IS NULL
        ORDER BY RANDOM()
        LIMIT 1
        """
    ).fetchone()


def mark_published(
    conn: sqlite3.Connection,
    series_id: str,
    message_id: str = "",
    description: str | None = None,
    genres: Any = None,
) -> None:
    now = time.time()
    fields = ["published_at=?", "message_id=?", "updated_at=?"]
    values: list[Any] = [now, message_id or "", now]
    if description:
        fields.append("description=?")
        values.append(description)
    if genres is not None:
        fields.append("genres=?")
        values.append(dump_genres(genres))
    values.extend([series_id])
    conn.execute(
        f"UPDATE series SET {', '.join(fields)} WHERE id=? AND published_at IS NULL",
        values,
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
