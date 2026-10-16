"""Пост, который не ушёл за сутки, удаляется из очереди."""

from __future__ import annotations

import sqlite3
import time

from app.db import expire_stale_unpublished


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE channels (chat_id INTEGER PRIMARY KEY);
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL
        );
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            external_id TEXT,
            text TEXT DEFAULT '',
            publish_status TEXT,
            publish_at REAL,
            created_at REAL
        );
        INSERT INTO channels(chat_id) VALUES (1);
        INSERT INTO sources(id, chat_id) VALUES (1, 1);
        """
    )
    return conn


def test_drops_old_pending_keeps_scheduled_and_fresh() -> None:
    conn = _conn()
    now = time.time()
    day = 24 * 3600
    conn.execute(
        "INSERT INTO posts(id, source_id, external_id, publish_status, publish_at, created_at) VALUES (1,1,'old','pending',NULL,?)",
        (now - 2 * day,),
    )
    conn.execute(
        "INSERT INTO posts(id, source_id, external_id, publish_status, publish_at, created_at) VALUES (2,1,'fresh','pending',NULL,?)",
        (now - 3600,),
    )
    conn.execute(
        "INSERT INTO posts(id, source_id, external_id, publish_status, publish_at, created_at) VALUES (3,1,'slot','pending',?,?)",
        (now + 3 * 3600, now - 2 * day),
    )
    conn.execute(
        "INSERT INTO posts(id, source_id, external_id, publish_status, publish_at, created_at) VALUES (4,1,'queued-old','queued',?,?)",
        (now - 30 * 3600, now - 30 * 3600),
    )
    n = expire_stale_unpublished(conn, now=now)
    assert n == 2
    left = {r["external_id"] for r in conn.execute("SELECT external_id FROM posts")}
    assert left == {"fresh", "slot"}
    conn.close()


if __name__ == "__main__":
    test_drops_old_pending_keeps_scheduled_and_fresh()
    print("OK queue ttl")
