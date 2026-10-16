from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'channel',
    status TEXT NOT NULL DEFAULT 'active',
    icon_url TEXT,
    participants_count INTEGER NOT NULL DEFAULT 0,
    link TEXT,
    description TEXT,
    watermark_text TEXT NOT NULL DEFAULT '',
    footer_link TEXT NOT NULL DEFAULT '',
    footer_link_text TEXT NOT NULL DEFAULT '',
    footer_as_button INTEGER NOT NULL DEFAULT 0,
    instant_publish INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- telegram | vk | rss
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'обход',  -- обход | парсинг | добавлен | ошибка
    status_detail TEXT NOT NULL DEFAULT '',
    cursor TEXT NOT NULL DEFAULT '',       -- last seen post id / watermark
    translate INTEGER NOT NULL DEFAULT 0, -- автоперевод через Groq
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(chat_id, kind, url),
    FOREIGN KEY(chat_id) REFERENCES channels(chat_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    external_id TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    text_original TEXT NOT NULL DEFAULT '',
    translated INTEGER NOT NULL DEFAULT 0,
    media_json TEXT NOT NULL DEFAULT '[]',
    source_url TEXT NOT NULL DEFAULT '',
    is_ad INTEGER NOT NULL DEFAULT 0,
    ad_reason TEXT NOT NULL DEFAULT '',
    publish_status TEXT NOT NULL DEFAULT 'pending', -- pending|skipped|queued|scheduled|sent|error
    publish_error TEXT NOT NULL DEFAULT '',
    publish_at REAL,
    created_at REAL NOT NULL,
    sent_at REAL,
    dest_message_id TEXT NOT NULL DEFAULT '',
    UNIQUE(source_id, external_id),
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(settings.db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
        _ensure_column(conn, "sources", "translate", "INTEGER NOT NULL DEFAULT 0")
        # strip = ядро: усиленные промо/CTA + вырезка URL; strict = ещё и блок по ссылкам
        _ensure_column(
            conn, "sources", "moderation_mode", "TEXT NOT NULL DEFAULT 'strip'"
        )
        _ensure_column(conn, "posts", "text_original", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "posts", "translated", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "posts", "content_hash", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "channels", "watermark_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "channels", "footer_link", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "channels", "footer_link_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "channels", "footer_as_button", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "channels", "instant_publish", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "channels", "last_published_at", "REAL")
        _ensure_column(conn, "channels", "hidden", "INTEGER NOT NULL DEFAULT 0")
        # Telegram / VK: platform + bot_token (TG-бот или ключ сообщества VK)
        _ensure_column(conn, "channels", "platform", "TEXT NOT NULL DEFAULT 'max'")
        _ensure_column(conn, "channels", "bot_token", "TEXT NOT NULL DEFAULT ''")
        # пользовательский ключ админа группы — photos/video (ключ сообщества даёт ошибку 27)
        _ensure_column(conn, "channels", "upload_token", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "posts", "publish_at", "REAL")
        # Модерация на уровне MAX-канала (наследуется всеми его источниками)
        _ensure_column(
            conn, "channels", "moderation_mode", "TEXT NOT NULL DEFAULT 'strip'"
        )
        _ensure_column(
            conn, "channels", "require_media", "INTEGER NOT NULL DEFAULT 1"
        )
        _ensure_column(
            conn, "sources", "require_media", "INTEGER NOT NULL DEFAULT 1"
        )
        _ensure_column(
            conn, "channels", "require_text", "INTEGER NOT NULL DEFAULT 1"
        )
        _ensure_column(
            conn, "sources", "require_text", "INTEGER NOT NULL DEFAULT 1"
        )
        _ensure_column(conn, "posts", "dest_message_id", "TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE channels SET require_text=1 WHERE require_text IS NULL")
        conn.execute("UPDATE sources SET require_text=1 WHERE require_text IS NULL")

        # Ядро + оверрайды из rules/*.yaml (без хардкода по каналам)
        from app.rules import sync_rules_to_db

        sync_rules_to_db(conn)

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_count_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                participants_count INTEGER NOT NULL DEFAULT 0,
                captured_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_count_snap_chat_time
            ON channel_count_snapshots(chat_id, captured_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS post_view_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                mid TEXT NOT NULL,
                views INTEGER NOT NULL DEFAULT 0,
                post_ts REAL NOT NULL DEFAULT 0,
                captured_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_post_view_snap
            ON post_view_snapshots(chat_id, captured_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS send_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                external_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                post_id INTEGER,
                created_at REAL NOT NULL,
                UNIQUE(chat_id, external_id),
                UNIQUE(chat_id, content_hash)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seo_active_matches (
                chat_id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL DEFAULT '',
                match_id TEXT NOT NULL DEFAULT '',
                competition TEXT NOT NULL DEFAULT '',
                home_team TEXT NOT NULL DEFAULT '',
                away_team TEXT NOT NULL DEFAULT '',
                home_team_ru TEXT NOT NULL DEFAULT '',
                away_team_ru TEXT NOT NULL DEFAULT '',
                kickoff_at REAL,
                status TEXT NOT NULL DEFAULT '',
                channel_title TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL DEFAULT '',
                post_text TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )


def record_participant_snapshot(
    conn: sqlite3.Connection,
    chat_id: int,
    participants_count: int,
    *,
    now: float | None = None,
    min_interval_sec: float = 3300,
) -> bool:
    """Пишет снимок числа подписчиков, не чаще чем раз в ~час на канал."""
    ts = time.time() if now is None else float(now)
    last = conn.execute(
        """
        SELECT captured_at FROM channel_count_snapshots
        WHERE chat_id=? ORDER BY captured_at DESC LIMIT 1
        """,
        (int(chat_id),),
    ).fetchone()
    if last and ts - float(last["captured_at"] or 0) < float(min_interval_sec):
        return False
    conn.execute(
        """
        INSERT INTO channel_count_snapshots(chat_id, participants_count, captured_at)
        VALUES (?, ?, ?)
        """,
        (int(chat_id), int(participants_count or 0), ts),
    )
    return True


def upsert_channel(conn: sqlite3.Connection, chat: dict[str, Any]) -> None:
    from app.rules import resolve_rules

    icon = chat.get("icon") or {}
    icon_url = icon.get("url") if isinstance(icon, dict) else None
    if isinstance(icon_url, str):
        icon_url = icon_url.strip() or None
    now = time.time()
    chat_id = int(chat["chat_id"])
    title = chat.get("title") or ""
    link = chat.get("link") or ""
    rules = resolve_rules(chat_id=chat_id, title=title, link=link)
    # watermark / footer / hidden не перезаписываем при sync с API
    # пустой icon из API не затирает уже сохранённый
    conn.execute(
        """
        INSERT INTO channels (
            chat_id, title, type, status, icon_url, participants_count,
            link, description, watermark_text, moderation_mode, require_media, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=CASE
                WHEN excluded.title != '' THEN excluded.title
                ELSE channels.title
            END,
            type=excluded.type,
            status=CASE WHEN channels.hidden=1 THEN channels.status ELSE excluded.status END,
            icon_url=CASE
                WHEN excluded.icon_url IS NOT NULL AND excluded.icon_url != ''
                THEN excluded.icon_url
                ELSE channels.icon_url
            END,
            participants_count=excluded.participants_count,
            link=COALESCE(excluded.link, channels.link),
            description=COALESCE(excluded.description, channels.description),
            moderation_mode=excluded.moderation_mode,
            updated_at=excluded.updated_at
        WHERE COALESCE(channels.platform, 'max') = 'max'
        """,
        (
            chat_id,
            title,
            chat.get("type") or "channel",
            chat.get("status") or "active",
            icon_url,
            int(chat.get("participants_count") or 0),
            chat.get("link"),
            chat.get("description"),
            rules.moderation_mode,
            1,
            now,
        ),
    )
    record_participant_snapshot(
        conn, chat_id, int(chat.get("participants_count") or 0), now=now
    )


def upsert_tg_channel(
    conn: sqlite3.Connection,
    chat_id: int,
    title: str,
    bot_token: str,
    *,
    chat_type: str = "channel",
    description: str = "",
    participants_count: int = 0,
    username: str = "",
) -> None:
    """Создать/обновить канал с platform='telegram'."""
    from app.rules import resolve_rules

    now = time.time()
    link = f"https://t.me/{username}" if username else ""
    rules = resolve_rules(chat_id=chat_id, title=title, link=link)
    conn.execute(
        """
        INSERT INTO channels (
            chat_id, title, type, status, icon_url, participants_count,
            link, description, watermark_text, moderation_mode, require_media,
            platform, bot_token, updated_at
        )
        VALUES (?, ?, ?, 'active', '', ?, ?, ?, '', ?, ?, 'telegram', ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            type=excluded.type,
            status='active',
            participants_count=excluded.participants_count,
            link=excluded.link,
            description=excluded.description,
            moderation_mode=excluded.moderation_mode,
            platform='telegram',
            bot_token=excluded.bot_token,
            updated_at=excluded.updated_at
        """,
        (
            chat_id,
            title,
            chat_type,
            participants_count,
            link,
            description,
            rules.moderation_mode,
            1,
            bot_token,
            now,
        ),
    )


def upsert_vk_channel(
    conn: sqlite3.Connection,
    owner_id: int,
    title: str,
    token: str,
    *,
    screen_name: str = "",
    description: str = "",
    participants_count: int = 0,
    icon_url: str = "",
    upload_token: str = "",
) -> None:
    """Создать/обновить назначение platform='vk'. chat_id = owner_id (−group_id)."""
    from app.rules import resolve_rules

    now = time.time()
    chat_id = int(owner_id)
    screen = (screen_name or "").strip().lstrip("@")
    link = f"https://vk.com/{screen}" if screen else f"https://vk.com/club{abs(chat_id)}"
    rules = resolve_rules(chat_id=chat_id, title=title, link=link)
    conn.execute(
        """
        INSERT INTO channels (
            chat_id, title, type, status, icon_url, participants_count,
            link, description, watermark_text, moderation_mode, require_media,
            platform, bot_token, upload_token, hidden, updated_at
        )
        VALUES (?, ?, 'group', 'active', ?, ?, ?, ?, '', ?, ?, 'vk', ?, ?, 0, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            type='group',
            status='active',
            icon_url=CASE
                WHEN excluded.icon_url != '' THEN excluded.icon_url
                ELSE channels.icon_url
            END,
            participants_count=excluded.participants_count,
            link=excluded.link,
            description=excluded.description,
            moderation_mode=excluded.moderation_mode,
            platform='vk',
            bot_token=excluded.bot_token,
            upload_token=CASE
                WHEN excluded.upload_token != '' THEN excluded.upload_token
                ELSE channels.upload_token
            END,
            hidden=0,
            updated_at=excluded.updated_at
        """,
        (
            chat_id,
            title,
            icon_url or "",
            int(participants_count or 0),
            link,
            description or "",
            rules.moderation_mode,
            1,
            token,
            (upload_token or "").strip(),
            now,
        ),
    )


def hide_channel(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute(
        "UPDATE channels SET hidden=1, status='hidden', updated_at=? WHERE chat_id=?",
        (time.time(), chat_id),
    )


def set_channel_upload_token(conn: sqlite3.Connection, chat_id: int, upload_token: str) -> None:
    conn.execute(
        "UPDATE channels SET upload_token=?, updated_at=? WHERE chat_id=?",
        ((upload_token or "").strip(), time.time(), chat_id),
    )


def requeue_skipped_no_media(
    conn: sqlite3.Connection,
    chat_id: int,
    *,
    max_age_sec: float = 6 * 3600,
) -> int:
    """Вернуть в очередь недавние посты, отброшенные из‑за «только медиа»."""
    since = time.time() - max(0.0, float(max_age_sec))
    cur = conn.execute(
        """
        UPDATE posts
        SET publish_status='pending',
            is_ad=0,
            ad_reason='',
            publish_error='',
            publish_at=?
        WHERE source_id IN (SELECT id FROM sources WHERE chat_id=?)
          AND publish_status='skipped'
          AND created_at >= ?
          AND (
            ad_reason='нет медиа'
            OR publish_error='нет медиа'
          )
        """,
        (time.time(), int(chat_id), since),
    )
    return int(cur.rowcount or 0)


def set_channel_require_media(
    conn: sqlite3.Connection,
    chat_id: int,
    require_media: bool,
    *,
    requeue: bool = True,
) -> int:
    """Чекбокс админки: только посты с медиа (True) или все после модерации (False)."""
    flag = 1 if require_media else 0
    now = time.time()
    prev = conn.execute(
        "SELECT require_media FROM channels WHERE chat_id=?",
        (int(chat_id),),
    ).fetchone()
    was = int(prev["require_media"] or 0) if prev else 1
    conn.execute(
        "UPDATE channels SET require_media=?, updated_at=? WHERE chat_id=?",
        (flag, now, int(chat_id)),
    )
    conn.execute(
        "UPDATE sources SET require_media=?, updated_at=? WHERE chat_id=?",
        (flag, now, int(chat_id)),
    )
    # Повторное «Сохранить» при уже снятой галочке не должно поднимать архив.
    if not requeue or flag or was == 0:
        return 0
    return requeue_skipped_no_media(conn, chat_id)


def set_channel_require_text(
    conn: sqlite3.Connection, chat_id: int, require_text: bool
) -> None:
    flag = 1 if require_text else 0
    now = time.time()
    conn.execute(
        "UPDATE channels SET require_text=?, updated_at=? WHERE chat_id=?",
        (flag, now, int(chat_id)),
    )
    conn.execute(
        "UPDATE sources SET require_text=?, updated_at=? WHERE chat_id=?",
        (flag, now, int(chat_id)),
    )


def set_all_vk_upload_tokens(conn: sqlite3.Connection, upload_token: str) -> int:
    cur = conn.execute(
        """
        UPDATE channels
        SET upload_token=?, updated_at=?
        WHERE COALESCE(platform, 'max') = 'vk' AND status = 'active'
        """,
        ((upload_token or "").strip(), time.time()),
    )
    return int(cur.rowcount or 0)


def requeue_vk_media_errors(conn: sqlite3.Connection, chat_id: int) -> int:
    """Вернуть в очередь посты, упавшие из‑за VK 27 / загрузки медиа."""
    rows = list(
        conn.execute(
            """
            SELECT p.id, p.external_id
            FROM posts p
            JOIN sources s ON s.id = p.source_id
            WHERE s.chat_id=?
              AND p.publish_status='error'
              AND (
                p.publish_error LIKE '27:%'
                OR p.publish_error LIKE '%group auth%'
                OR p.publish_error LIKE '%загруз%медиа%'
                OR p.publish_error LIKE '%ключ сообщества не умеет%'
                OR p.publish_error LIKE '%access_token has expired%'
                OR p.publish_error LIKE '%authorization failed%'
              )
            """,
            (int(chat_id),),
        )
    )
    ids = [int(r["id"]) for r in rows]
    if not ids:
        return 0
    conn.execute(
        "DELETE FROM send_log WHERE post_id IN ({})".format(",".join("?" * len(ids))),
        ids,
    )
    for r in rows:
        conn.execute(
            "DELETE FROM send_log WHERE chat_id=? AND external_id=?",
            (int(chat_id), r["external_id"]),
        )
    conn.execute(
        """
        UPDATE posts
        SET publish_status='pending', publish_error='', publish_at=NULL
        WHERE id IN ({})
        """.format(",".join("?" * len(ids))),
        ids,
    )
    return len(ids)


def set_channel_watermark(conn: sqlite3.Connection, chat_id: int, watermark_text: str) -> None:
    conn.execute(
        "UPDATE channels SET watermark_text=?, updated_at=? WHERE chat_id=?",
        ((watermark_text or "").strip(), time.time(), chat_id),
    )


def set_channel_footer_link(
    conn: sqlite3.Connection,
    chat_id: int,
    footer_link: str,
    footer_link_text: str = "",
    footer_as_button: bool | None = None,
) -> None:
    if footer_as_button is None:
        conn.execute(
            "UPDATE channels SET footer_link=?, footer_link_text=?, updated_at=? WHERE chat_id=?",
            (
                (footer_link or "").strip(),
                (footer_link_text or "").strip(),
                time.time(),
                chat_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE channels
            SET footer_link=?, footer_link_text=?, footer_as_button=?, updated_at=?
            WHERE chat_id=?
            """,
            (
                (footer_link or "").strip(),
                (footer_link_text or "").strip(),
                1 if footer_as_button else 0,
                time.time(),
                chat_id,
            ),
        )


def list_channels(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM channels
            WHERE status='active' AND COALESCE(hidden, 0)=0
            ORDER BY participants_count DESC, title COLLATE NOCASE
            """
        )
    )


def list_active_max_chat_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """
        SELECT chat_id FROM channels
        WHERE status='active'
          AND COALESCE(hidden, 0)=0
          AND COALESCE(platform, 'max')='max'
        ORDER BY participants_count DESC, title COLLATE NOCASE
        """
    ).fetchall()
    return [int(r["chat_id"]) for r in rows]


def get_channel(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM channels WHERE chat_id=?", (chat_id,)).fetchone()


def list_sources(conn: sqlite3.Connection, chat_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM sources WHERE chat_id=? ORDER BY id DESC",
            (chat_id,),
        )
    )


def get_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


def add_source(
    conn: sqlite3.Connection,
    chat_id: int,
    kind: str,
    url: str,
    translate: bool = False,
) -> int:
    now = time.time()
    ch = conn.execute(
        "SELECT moderation_mode, require_media, require_text, title, link FROM channels WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    from app.rules import resolve_rules

    if ch is not None:
        rules = resolve_rules(
            chat_id=chat_id,
            title=ch["title"] if "title" in ch.keys() else "",
            link=ch["link"] if "link" in ch.keys() else "",
            source_url=url,
        )
    else:
        rules = resolve_rules(chat_id=chat_id)
    mode = rules.moderation_mode
    if ch is not None and "require_media" in ch.keys() and ch["require_media"] is not None:
        require_media = 1 if int(ch["require_media"]) else 0
    else:
        require_media = 1
    if ch is not None and "require_text" in ch.keys() and ch["require_text"] is not None:
        require_text = 1 if int(ch["require_text"]) else 0
    else:
        require_text = 1
    cur = conn.execute(
        """
        INSERT INTO sources (
            chat_id, kind, url, title, status, status_detail,
            translate, moderation_mode, require_media, require_text, created_at, updated_at
        )
        VALUES (?, ?, ?, '', 'обход', 'Проверка источника…', ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            kind,
            url,
            1 if translate else 0,
            mode,
            require_media,
            require_text,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM sources WHERE id=?", (source_id,))


def set_source_status(
    conn: sqlite3.Connection,
    source_id: int,
    status: str,
    detail: str = "",
    title: str | None = None,
    cursor: str | None = None,
) -> None:
    fields = ["status=?", "status_detail=?", "updated_at=?"]
    vals: list[Any] = [status, detail, time.time()]
    if title is not None:
        fields.append("title=?")
        vals.append(title)
    if cursor is not None:
        fields.append("cursor=?")
        vals.append(cursor)
    vals.append(source_id)
    conn.execute(f"UPDATE sources SET {', '.join(fields)} WHERE id=?", vals)


def normalize_post_text(text: str) -> str:
    """Текст для сравнения репостов/правок: без ссылок и лишних пробелов."""
    import re

    t = (text or "").lower()
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"t\.me/\S+", " ", t)
    t = re.sub(r"[@#]\w+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def texts_similar(a: str, b: str, threshold: float = 0.86) -> bool:
    from difflib import SequenceMatcher

    na, nb = normalize_post_text(a), normalize_post_text(b)
    if not na and not nb:
        return True
    if not na or not nb:
        return False
    if na == nb:
        return True
    # короткие подписи к медиа — сравниваем мягче по вхождению
    if len(na) < 40 or len(nb) < 40:
        shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
        if shorter and shorter in longer:
            return True
    return SequenceMatcher(None, na, nb).ratio() >= threshold


def insert_post(
    conn: sqlite3.Connection,
    source_id: int,
    external_id: str,
    text: str,
    media: list[dict[str, Any]],
    source_url: str,
    is_ad: bool,
    ad_reason: str,
    publish_hold_sec: int = 0,
    republish_window_sec: int = 600,
    publish_at: float | None = None,
) -> bool:
    """Returns True if inserted (new)."""
    now = time.time()
    media_json = json.dumps(media, ensure_ascii=False)
    # Hold перед публикацией — источник может удалить/переопубликовать правку
    if is_ad:
        scheduled_at = None
    elif publish_at is not None:
        scheduled_at = float(publish_at)
    else:
        hold = max(0, int(publish_hold_sec or 0))
        scheduled_at = now + hold
    try:
        conn.execute(
            """
            INSERT INTO posts (
                source_id, external_id, text, media_json, source_url,
                is_ad, ad_reason, publish_status, publish_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                external_id,
                text,
                media_json,
                source_url,
                1 if is_ad else 0,
                ad_reason,
                "skipped" if is_ad else "pending",
                scheduled_at,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        return False

    if not is_ad:
        superseded = supersede_similar_pending(
            conn,
            source_id=source_id,
            keep_external_id=external_id,
            text=text,
            window_sec=republish_window_sec,
        )
        if superseded:
            print(
                f"[db] supersede {superseded} older pending source={source_id} "
                f"keep={external_id}",
                flush=True,
            )
    return True


def supersede_similar_pending(
    conn: sqlite3.Connection,
    source_id: int,
    keep_external_id: str,
    text: str,
    window_sec: int = 600,
) -> int:
    """
    Отменяет более старые pending того же источника с похожим текстом.
    Нужно когда канал удаляет пост и публикует заново с правками (новый external_id).
    """
    if not normalize_post_text(text):
        return 0
    since = time.time() - max(60, int(window_sec or 600))
    rows = list(
        conn.execute(
            """
            SELECT id, external_id, text
            FROM posts
            WHERE source_id=?
              AND publish_status='pending'
              AND is_ad=0
              AND external_id!=?
              AND created_at>=?
            ORDER BY id ASC
            """,
            (source_id, keep_external_id, since),
        )
    )
    skipped = 0
    for row in rows:
        if not texts_similar(text, row["text"] or ""):
            continue
        cur = conn.execute(
            """
            UPDATE posts
            SET publish_status='skipped',
                publish_error=?,
                is_ad=1,
                ad_reason=?
            WHERE id=? AND publish_status='pending'
            """,
            (
                "перепубликация у источника (похожий пост новее)",
                "перепубликация",
                row["id"],
            ),
        )
        if cur.rowcount:
            skipped += 1
    return skipped


def pending_posts(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    now = time.time()
    return list(
        conn.execute(
            """
            SELECT p.*, s.chat_id, s.translate AS need_translate,
                   s.url AS source_feed_url,
                   COALESCE(c.moderation_mode, 'strip') AS moderation_mode,
                   COALESCE(c.require_media, 1) AS require_media,
                   COALESCE(c.require_text, 1) AS require_text,
                   c.title AS channel_title,
                   c.link AS channel_link,
                   COALESCE(c.platform, 'max') AS platform,
                   COALESCE(c.bot_token, '') AS bot_token,
                   COALESCE(c.upload_token, '') AS upload_token
            FROM posts p
            JOIN sources s ON s.id = p.source_id
            JOIN channels c ON c.chat_id = s.chat_id
            WHERE p.is_ad=0 AND p.publish_status='pending'
              AND (p.publish_at IS NULL OR p.publish_at <= ?)
            ORDER BY p.id ASC
            LIMIT ?
            """,
            (now, limit),
        )
    )


def claim_pending_posts(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """Атомарно забирает pending → queued."""
    rows = pending_posts(conn, limit=limit)
    claimed: list[dict[str, Any]] = []
    for row in rows:
        cur = conn.execute(
            """
            UPDATE posts SET publish_status='queued'
            WHERE id=? AND publish_status='pending'
            """,
            (row["id"],),
        )
        if cur.rowcount:
            claimed.append(dict(row))
    return claimed


def list_recent_posts(conn: sqlite3.Connection, chat_id: int, limit: int = 25) -> list[sqlite3.Row]:
    """История по каналу (отправленные / пропуски / ошибки)."""
    return list(
        conn.execute(
            """
            SELECT p.id, p.external_id, p.text, p.publish_status, p.publish_error,
                   p.publish_at, p.created_at, p.sent_at, p.is_ad, p.ad_reason,
                   p.media_json, s.url AS source_url, s.kind AS source_kind
            FROM posts p
            JOIN sources s ON s.id = p.source_id
            WHERE s.chat_id=?
              AND p.publish_status NOT IN ('pending', 'queued', 'simulated')
            ORDER BY p.id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
    )


def content_fingerprint(chat_id: int, text: str, media_json: str) -> str:
    import hashlib

    raw = f"{chat_id}|{(text or '').strip()}|{(media_json or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def register_send(
    conn: sqlite3.Connection,
    chat_id: int,
    external_id: str,
    content_hash: str,
    post_id: int | None = None,
) -> bool:
    """True если отправка разрешена (новый), False если дубль."""
    try:
        conn.execute(
            """
            INSERT INTO send_log (chat_id, external_id, content_hash, post_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, external_id, content_hash, post_id, time.time()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def expire_stale_unpublished(
    conn: sqlite3.Connection,
    *,
    ttl_sec: float = 24 * 3600,
    now: float | None = None,
) -> int:
    """Удаляет из очереди посты, которые так и не ушли за ttl (по умолчанию сутки).

    Запланированные на будущее (publish_at > now) не трогаем.
    """
    ts = time.time() if now is None else float(now)
    cutoff = ts - max(60.0, float(ttl_sec))
    cur = conn.execute(
        """
        DELETE FROM posts
        WHERE publish_status IN ('pending', 'queued')
          AND COALESCE(publish_at, created_at) <= ?
        """,
        (cutoff,),
    )
    return int(cur.rowcount or 0)


def mark_post(
    conn: sqlite3.Connection,
    post_id: int,
    status: str,
    error: str = "",
    dest_message_id: str | None = None,
) -> None:
    now = time.time()
    if status == "sent":
        mid = (dest_message_id or "").strip()
        if mid:
            conn.execute(
                """
                UPDATE posts
                SET publish_status=?, publish_error=?, sent_at=?, dest_message_id=?
                WHERE id=?
                """,
                (status, error, now, mid, post_id),
            )
        else:
            conn.execute(
                """
                UPDATE posts SET publish_status=?, publish_error=?, sent_at=? WHERE id=?
                """,
                (status, error, now, post_id),
            )
        row = conn.execute(
            """
            SELECT s.chat_id FROM posts p JOIN sources s ON s.id=p.source_id WHERE p.id=?
            """,
            (post_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE channels SET last_published_at=? WHERE chat_id=?",
                (now, int(row["chat_id"])),
            )
    else:
        conn.execute(
            "UPDATE posts SET publish_status=?, publish_error=? WHERE id=?",
            (status, error, post_id),
        )


def list_tracked_source_posts(
    conn: sqlite3.Connection,
    source_id: int,
    window_sec: int = 14400,
    now: float | None = None,
) -> list[sqlite3.Row]:
    """Недавние pending/queued/sent по источнику (окно зеркального удаления)."""
    ts = time.time() if now is None else float(now)
    since = ts - max(60, int(window_sec or 14400))
    return list(
        conn.execute(
            """
            SELECT p.id, p.external_id, p.publish_status,
                   COALESCE(p.dest_message_id, '') AS dest_message_id,
                   p.sent_at, p.created_at,
                   s.chat_id,
                   COALESCE(c.platform, 'max') AS platform,
                   COALESCE(c.bot_token, '') AS bot_token
            FROM posts p
            JOIN sources s ON s.id = p.source_id
            JOIN channels c ON c.chat_id = s.chat_id
            WHERE p.source_id=?
              AND p.is_ad=0
              AND p.publish_status IN ('pending', 'queued', 'sent')
              AND (
                (p.publish_status='sent' AND p.sent_at IS NOT NULL AND p.sent_at >= ?)
                OR (
                    p.publish_status IN ('pending', 'queued')
                    AND p.created_at >= ?
                )
              )
            """,
            (source_id, since, since),
        )
    )


def save_translation(conn: sqlite3.Connection, post_id: int, original: str, translated_text: str) -> None:
    conn.execute(
        """
        UPDATE posts
        SET text_original=?, text=?, translated=1
        WHERE id=?
        """,
        (original, translated_text, post_id),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def active_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT s.*,
                   c.title AS channel_title,
                   c.link AS channel_link,
                   COALESCE(c.moderation_mode, 'strip') AS channel_moderation_mode,
                   COALESCE(c.require_media, 1) AS channel_require_media,
                   COALESCE(c.require_text, 1) AS channel_require_text
            FROM sources s
            JOIN channels c ON c.chat_id = s.chat_id
            WHERE s.status IN ('обход', 'парсинг', 'добавлен', 'ошибка')
              AND COALESCE(c.hidden, 0)=0
              AND c.status='active'
            ORDER BY s.id
            """
        )
    )


def get_seo_active(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM seo_active_matches WHERE chat_id=?",
        (int(chat_id),),
    ).fetchone()


def list_seo_active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM seo_active_matches ORDER BY updated_at DESC")
    )


def upsert_seo_active(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    slug: str,
    match_id: str,
    competition: str,
    home_team: str,
    away_team: str,
    home_team_ru: str,
    away_team_ru: str,
    kickoff_at: float | None,
    status: str,
    channel_title: str,
    message_id: str = "",
    post_text: str = "",
    last_error: str = "",
) -> None:
    now = time.time()
    existing = get_seo_active(conn, chat_id)
    created = float(existing["created_at"]) if existing else now
    conn.execute(
        """
        INSERT INTO seo_active_matches (
            chat_id, slug, match_id, competition,
            home_team, away_team, home_team_ru, away_team_ru,
            kickoff_at, status, channel_title, message_id, post_text,
            last_error, updated_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            slug=excluded.slug,
            match_id=excluded.match_id,
            competition=excluded.competition,
            home_team=excluded.home_team,
            away_team=excluded.away_team,
            home_team_ru=excluded.home_team_ru,
            away_team_ru=excluded.away_team_ru,
            kickoff_at=excluded.kickoff_at,
            status=excluded.status,
            channel_title=excluded.channel_title,
            message_id=excluded.message_id,
            post_text=excluded.post_text,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (
            int(chat_id),
            slug or "",
            match_id or "",
            competition or "",
            home_team or "",
            away_team or "",
            home_team_ru or "",
            away_team_ru or "",
            kickoff_at,
            status or "",
            channel_title or "",
            message_id or "",
            post_text or "",
            (last_error or "")[:800],
            now,
            created,
        ),
    )


def set_seo_error(conn: sqlite3.Connection, chat_id: int, error: str) -> None:
    now = time.time()
    row = get_seo_active(conn, chat_id)
    if row:
        conn.execute(
            "UPDATE seo_active_matches SET last_error=?, updated_at=? WHERE chat_id=?",
            ((error or "")[:800], now, int(chat_id)),
        )
    else:
        conn.execute(
            """
            INSERT INTO seo_active_matches (
                chat_id, slug, match_id, competition, home_team, away_team,
                home_team_ru, away_team_ru, kickoff_at, status, channel_title,
                message_id, post_text, last_error, updated_at, created_at
            ) VALUES (?, '', '', '', '', '', '', '', NULL, '', '', '', '', ?, ?, ?)
            """,
            (int(chat_id), (error or "")[:800], now, now),
        )


def clear_seo_active(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute("DELETE FROM seo_active_matches WHERE chat_id=?", (int(chat_id),))

