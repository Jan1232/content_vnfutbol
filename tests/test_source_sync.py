from __future__ import annotations

import sqlite3
import time

from app.db import list_tracked_source_posts, mark_post
from app.max_api import extract_max_message_id
from app.tg_mtproto import (
    note_tg_flood,
    telegram_media_needs_album_expand,
    tg_is_flood_blocked,
)
from app.source_sync import (
    deleted_in_visible_window,
    newer_than_cursor,
    publish_hold_sec_for_media,
    source_seq,
)


def test_source_seq_and_newer() -> None:
    assert source_seq("tg:championsleague365/20843") == 20843
    assert source_seq("vk:-123_99") == 99
    assert newer_than_cursor("tg:c/10", "tg:c/9")
    assert not newer_than_cursor("tg:c/9", "tg:c/9")
    assert not newer_than_cursor("tg:c/8", "tg:c/9")


def test_deleted_in_window_not_scrolled_off() -> None:
    visible = [f"tg:c/{n}" for n in (12, 13, 14, 16, 17)]
    # 15 пропал внутри окна; 8 ушёл вниз за край страницы
    missing = deleted_in_visible_window(visible, ["tg:c/15", "tg:c/8", "tg:c/17"])
    assert missing == ["tg:c/15"]


def test_no_delete_without_gap() -> None:
    visible = [f"tg:c/{n}" for n in range(10, 20)]
    assert deleted_in_visible_window(visible, ["tg:c/15"]) == []


def test_text_posts_have_no_hold() -> None:
    assert publish_hold_sec_for_media([], 180) == 0
    assert publish_hold_sec_for_media([{"type": "link", "url": "https://x"}], 180) == 0
    assert publish_hold_sec_for_media([{"type": "image", "url": "https://x/a.jpg"}], 180) == 180
    assert publish_hold_sec_for_media([{"type": "video"}], 180) == 0
    assert publish_hold_sec_for_media([{"type": "video", "url": "https://x/a.mp4"}], 180) == 180


def test_extract_max_mid() -> None:
    assert extract_max_message_id({"message": {"body": {"mid": "mid-1"}}}) == "mid-1"
    assert extract_max_message_id({"mid": "abc"}) == "abc"
    assert extract_max_message_id(None) == ""


def test_tracked_posts_drop_after_window() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE channels (
            chat_id INTEGER PRIMARY KEY,
            platform TEXT DEFAULT 'max',
            bot_token TEXT DEFAULT ''
        );
        CREATE TABLE sources (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL);
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            external_id TEXT,
            is_ad INTEGER DEFAULT 0,
            publish_status TEXT,
            dest_message_id TEXT DEFAULT '',
            sent_at REAL,
            created_at REAL
        );
        INSERT INTO channels(chat_id) VALUES (1);
        INSERT INTO sources(id, chat_id) VALUES (1, 1);
        """
    )
    now = time.time()
    conn.execute(
        "INSERT INTO posts(id,source_id,external_id,publish_status,dest_message_id,sent_at,created_at) VALUES (1,1,'tg:c/1','sent','mid-old',?,?)",
        (now - 5 * 3600, now - 5 * 3600),
    )
    conn.execute(
        "INSERT INTO posts(id,source_id,external_id,publish_status,dest_message_id,sent_at,created_at) VALUES (2,1,'tg:c/2','sent','mid-new',?,?)",
        (now - 600, now - 600),
    )
    conn.execute(
        "INSERT INTO posts(id,source_id,external_id,publish_status,created_at) VALUES (3,1,'tg:c/3','pending',?)",
        (now - 120,),
    )
    rows = list_tracked_source_posts(conn, 1, window_sec=14400, now=now)
    ids = {r["external_id"] for r in rows}
    assert ids == {"tg:c/2", "tg:c/3"}
    conn.close()


def test_mark_post_stores_mid() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE channels (chat_id INTEGER PRIMARY KEY, last_published_at REAL);
        CREATE TABLE sources (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL);
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            publish_status TEXT,
            publish_error TEXT DEFAULT '',
            sent_at REAL,
            dest_message_id TEXT DEFAULT ''
        );
        INSERT INTO channels(chat_id) VALUES (7);
        INSERT INTO sources(id, chat_id) VALUES (1, 7);
        INSERT INTO posts(id, source_id, publish_status) VALUES (9, 1, 'queued');
        """
    )
    mark_post(conn, 9, "sent", dest_message_id="mid-xyz")
    row = conn.execute("SELECT dest_message_id, publish_status FROM posts WHERE id=9").fetchone()
    assert row["dest_message_id"] == "mid-xyz"
    assert row["publish_status"] == "sent"
    conn.close()


def test_empty_album_does_not_need_telethon() -> None:
    assert telegram_media_needs_album_expand("tg:c/1", []) is False


def test_floodwait_blocks_mtproto() -> None:
    import app.tg_mtproto as m

    m._flood_until = 0.0
    note_tg_flood("A wait of 30421 seconds is required (caused by ResolveUsernameRequest)")
    assert tg_is_flood_blocked()
    m._flood_until = 0.0
    assert not tg_is_flood_blocked()


if __name__ == "__main__":
    test_source_seq_and_newer()
    test_deleted_in_window_not_scrolled_off()
    test_no_delete_without_gap()
    test_text_posts_have_no_hold()
    test_extract_max_mid()
    test_tracked_posts_drop_after_window()
    test_mark_post_stores_mid()
    test_empty_album_does_not_need_telethon()
    test_floodwait_blocks_mtproto()
    print("OK source sync")
