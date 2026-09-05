"""SQLite для автосбора (SPEC v3)."""

from __future__ import annotations

import sqlite3
import struct
import time
from typing import Any

import os
from pathlib import Path
from typing import Any

from src.config import ROOT

DB_PATH = Path(os.environ.get("INGEST_DB", str(ROOT / "data" / "ingest.db")))


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    _init(conn)
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            msg_id INTEGER NOT NULL,
            text TEXT,
            ts INTEGER,
            is_filtered INTEGER DEFAULT 0,
            filter_reason TEXT,
            UNIQUE(source, msg_id)
        );

        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_msg_id INTEGER,
            fact TEXT NOT NULL,
            archetype TEXT,
            veracity TEXT,
            is_sensation INTEGER,
            attribution TEXT,
            embedding BLOB,
            confirms_count INTEGER DEFAULT 1,
            dedup_of INTEGER,
            created_at INTEGER,
            FOREIGN KEY(raw_msg_id) REFERENCES raw_messages(id)
        );

        CREATE TABLE IF NOT EXISTS generated_live (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            guardrail_flag TEXT,
            status TEXT DEFAULT 'pending',
            created_at INTEGER,
            FOREIGN KEY(fact_id) REFERENCES facts(id)
        );

        CREATE TABLE IF NOT EXISTS calibration_log_live (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_id INTEGER,
            generated_id INTEGER,
            generated TEXT,
            decision TEXT,
            edited_text TEXT,
            source TEXT,
            source_msg_link TEXT,
            model TEXT,
            dedup_layer TEXT,
            created_at INTEGER
        );
        """
    )
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    fact_cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
    for col, typedef in [
        ("event_kind", "TEXT"),
        ("event_teams", "TEXT"),
        ("event_player", "TEXT"),
        ("event_to_club", "TEXT"),
        ("event_score", "TEXT"),
        ("event_minute", "INTEGER"),
        ("event_fingerprint", "TEXT"),
        ("dedup_layer", "TEXT"),
        ("image_query", "TEXT"),
        ("is_garbage", "INTEGER DEFAULT 0"),
        ("is_test", "INTEGER DEFAULT 0"),
    ]:
        if col not in fact_cols:
            conn.execute(f"ALTER TABLE facts ADD COLUMN {col} {typedef}")
    raw_cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_messages)").fetchall()}
    for col, typedef in [
        ("is_test", "INTEGER DEFAULT 0"),
        ("is_garbage", "INTEGER DEFAULT 0"),
    ]:
        if col not in raw_cols:
            conn.execute(f"ALTER TABLE raw_messages ADD COLUMN {col} {typedef}")
    gen_cols = {r[1] for r in conn.execute("PRAGMA table_info(generated_live)").fetchall()}
    for col, typedef in [
        ("media_path", "TEXT"),
        ("media_url", "TEXT"),
        ("media_kind", "TEXT"),
        ("media_strategy", "TEXT"),
        ("image_query", "TEXT"),
        ("media_warning", "TEXT"),
        ("archetype_override", "TEXT"),
        ("news_id", "INTEGER"),
        ("run_tag", "TEXT"),
        ("is_test", "INTEGER DEFAULT 0"),
    ]:
        if col not in gen_cols:
            conn.execute(f"ALTER TABLE generated_live ADD COLUMN {col} {typedef}")
    log_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(calibration_log_live)").fetchall()
    }
    for col, typedef in [
        ("dedup_layer", "TEXT"),
        ("eval_scope", "TEXT"),
        ("old_category", "TEXT"),
        ("new_category", "TEXT"),
        ("news_id", "INTEGER"),
        ("duplicate_of", "INTEGER"),
        ("raw_text", "TEXT"),
        ("fact_snapshot", "TEXT"),
        ("event_json", "TEXT"),
        ("archetype_final", "TEXT"),
        ("auto_image_query", "TEXT"),
        ("manual_image_query", "TEXT"),
        ("is_test", "INTEGER DEFAULT 0"),
        ("is_garbage", "INTEGER DEFAULT 0"),
    ]:
        if col not in log_cols:
            conn.execute(f"ALTER TABLE calibration_log_live ADD COLUMN {col} {typedef}")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS run_24h (
            news_id INTEGER PRIMARY KEY,
            generated_id INTEGER,
            fact_id INTEGER,
            source TEXT,
            msg_id INTEGER,
            raw_text TEXT,
            fact TEXT,
            event_kind TEXT,
            event_teams TEXT,
            event_player TEXT,
            event_to_club TEXT,
            event_score TEXT,
            event_minute INTEGER,
            event_fingerprint TEXT,
            image_query TEXT,
            archetype TEXT,
            old_archetype TEXT,
            decision TEXT,
            edited_text TEXT,
            duplicate_of INTEGER,
            eval_scope TEXT,
            media_strategy TEXT,
            created_at INTEGER,
            decided_at INTEGER
        );
        """
    )
    r24_cols = {r[1] for r in conn.execute("PRAGMA table_info(run_24h)").fetchall()}
    for col, typedef in [
        ("run_tag", "TEXT"),
        ("is_test", "INTEGER DEFAULT 0"),
        ("is_garbage", "INTEGER DEFAULT 0"),
    ]:
        if col not in r24_cols:
            conn.execute(f"ALTER TABLE run_24h ADD COLUMN {col} {typedef}")
    conn.commit()
    _mark_existing_test_rows(conn)


def _mark_existing_test_rows(conn: sqlite3.Connection) -> None:
    """Пометить тестовые/заглушечные строки is_test=1 (не удалять)."""
    # источники test_*
    conn.execute(
        """
        UPDATE calibration_log_live SET is_test=1
        WHERE COALESCE(is_test,0)=0 AND (
            source LIKE 'test_%' OR source = 'test' OR source = 'test_source'
        )
        """
    )
    conn.execute(
        """
        UPDATE calibration_log_live SET is_test=1
        WHERE COALESCE(is_test,0)=0 AND (
            raw_text = 'raw goal text'
            OR fact_snapshot = 'raw goal text'
            OR (
                COALESCE(raw_text, '') = ''
                AND fact_snapshot IN ('Мем/видео без текста', 'Видео из источника', 'Мем из источника')
            )
        )
        """
    )
    conn.execute(
        """
        UPDATE raw_messages SET is_test=1
        WHERE COALESCE(is_test,0)=0 AND (
            source LIKE 'test_%' OR source = 'test' OR source = 'test_source'
            OR text = 'raw goal text'
        )
        """
    )
    conn.execute(
        """
        UPDATE run_24h SET is_test=1
        WHERE COALESCE(is_test,0)=0 AND (
            source LIKE 'test_%' OR source = 'test' OR source = 'test_source'
            OR raw_text = 'raw goal text'
            OR (
                COALESCE(raw_text, '') = ''
                AND fact IN ('Мем/видео без текста', 'Видео из источника', 'Мем из источника')
            )
        )
        """
    )
    conn.execute(
        """
        UPDATE facts SET is_test=1
        WHERE COALESCE(is_test,0)=0 AND id IN (
            SELECT f.id FROM facts f
            JOIN raw_messages r ON r.id = f.raw_msg_id
            WHERE r.is_test=1 OR r.source LIKE 'test_%'
               OR f.fact IN ('Мем/видео без текста', 'Видео из источника', 'raw goal text')
        )
        """
    )
    conn.execute(
        """
        UPDATE generated_live SET is_test=1
        WHERE COALESCE(is_test,0)=0 AND (
            fact_id IN (SELECT id FROM facts WHERE is_test=1)
            OR run_tag LIKE 'test%'
        )
        """
    )
    conn.commit()


def pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_embedding(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def insert_raw(
    *,
    source: str,
    msg_id: int,
    text: str,
    ts: int,
    is_filtered: int = 0,
    filter_reason: str | None = None,
    replace: bool = False,
    is_test: bool = False,
    is_garbage: bool = False,
) -> int | None:
    """Возвращает id или None если уже было (UNIQUE), unless replace=True."""
    conn = _connect()
    try:
        if replace:
            conn.execute(
                "DELETE FROM raw_messages WHERE source=? AND msg_id=?",
                (source, msg_id),
            )
        cur = conn.execute(
            """
            INSERT INTO raw_messages (
                source, msg_id, text, ts, is_filtered, filter_reason, is_test, is_garbage
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                msg_id,
                text,
                ts,
                is_filtered,
                filter_reason,
                int(is_test),
                int(is_garbage or (filter_reason or "").startswith("filtered_extract")),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        conn.rollback()
        return None
    finally:
        conn.close()


def find_source_media_file(source: str, msg_id: int | None) -> tuple[str | None, str | None]:
    """Ищет скачанный файл data/media/{source}_{msg_id}*."""
    if not source or not msg_id:
        return None, None
    media_dir = ROOT / "data" / "media"
    if not media_dir.is_dir():
        return None, None
    stem = f"{source}_{msg_id}"
    matches = sorted(media_dir.glob(f"{stem}*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in matches:
        if not p.is_file() or p.stat().st_size < 500:
            continue
        low = p.suffix.lower()
        kind = "video" if low in (".mp4", ".mov", ".mkv", ".webm", ".avi") else "photo"
        return str(p), kind
    return None, None


def get_run24_source(fact_id: int) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute(
        "SELECT source, msg_id, raw_text FROM run_24h WHERE fact_id=? ORDER BY news_id DESC LIMIT 1",
        (fact_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_raw_message(source: str, msg_id: int) -> None:
    """Удаляет сырьё (для повторного прогона 24ч). Связанные facts остаются."""
    conn = _connect()
    conn.execute(
        "DELETE FROM raw_messages WHERE source=? AND msg_id=?",
        (source, msg_id),
    )
    conn.commit()
    conn.close()


def mark_filtered(
    raw_id: int,
    reason: str,
    *,
    is_garbage: bool = False,
) -> None:
    conn = _connect()
    garbage = int(is_garbage or reason.startswith("filtered_extract") or "is_garbage" in reason)
    conn.execute(
        """
        UPDATE raw_messages
        SET is_filtered=1, filter_reason=?, is_garbage=CASE WHEN ? THEN 1 ELSE is_garbage END
        WHERE id=?
        """,
        (reason, garbage, raw_id),
    )
    conn.commit()
    conn.close()


def insert_fact(
    *,
    raw_msg_id: int,
    fact: str,
    archetype: str,
    veracity: str,
    is_sensation: bool,
    attribution: str | None,
    embedding: list[float] | None,
    confirms_count: int = 1,
    dedup_of: int | None = None,
    event: dict | None = None,
    event_fingerprint: str | None = None,
    dedup_layer: str | None = None,
    image_query: str | None = None,
) -> int:
    import json

    ev = event or {}
    teams = ev.get("teams") or []
    conn = _connect()
    cur = conn.execute(
        """
        INSERT INTO facts (
            raw_msg_id, fact, archetype, veracity, is_sensation,
            attribution, embedding, confirms_count, dedup_of, created_at,
            event_kind, event_teams, event_player, event_to_club,
            event_score, event_minute, event_fingerprint, dedup_layer, image_query
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            raw_msg_id,
            fact,
            archetype,
            veracity,
            int(is_sensation),
            attribution,
            pack_embedding(embedding) if embedding else None,
            confirms_count,
            dedup_of,
            int(time.time()),
            ev.get("event_kind"),
            json.dumps(teams, ensure_ascii=False),
            ev.get("player"),
            ev.get("to_club"),
            ev.get("score"),
            ev.get("minute"),
            event_fingerprint,
            dedup_layer,
            image_query,
        ),
    )
    conn.commit()
    fid = int(cur.lastrowid)
    conn.close()
    return fid


def increment_confirms(fact_id: int) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE facts SET confirms_count = confirms_count + 1 WHERE id=?",
        (fact_id,),
    )
    conn.commit()
    conn.close()


def recent_facts_with_embeddings(window_hours: int) -> list[dict[str, Any]]:
    """Обратная совместимость — только id+embedding."""
    return [
        {"id": r["id"], "fact": r["fact"], "archetype": r["archetype"],
         "veracity": r["veracity"], "embedding": r["embedding"],
         "confirms_count": r["confirms_count"]}
        for r in recent_facts_for_dedup(window_hours)
    ]


def recent_facts_for_dedup(window_hours: int) -> list[dict[str, Any]]:
    since = int(time.time()) - window_hours * 3600
    conn = _connect()
    rows = conn.execute(
        """
        SELECT f.id, f.fact, f.archetype, f.veracity, f.embedding, f.confirms_count,
               f.event_kind, f.event_teams, f.event_player, f.event_to_club,
               f.event_score, f.event_minute, f.event_fingerprint,
               r.source
        FROM facts f
        LEFT JOIN raw_messages r ON r.id = f.raw_msg_id
        WHERE f.created_at >= ? AND f.dedup_of IS NULL
        ORDER BY f.id DESC
        """,
        (since,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "fact": r["fact"],
                "archetype": r["archetype"],
                "veracity": r["veracity"],
                "embedding": unpack_embedding(r["embedding"]),
                "confirms_count": r["confirms_count"],
                "event_kind": r["event_kind"],
                "event_teams": r["event_teams"],
                "event_player": r["event_player"],
                "event_to_club": r["event_to_club"],
                "event_score": r["event_score"],
                "event_minute": r["event_minute"],
                "event_fingerprint": r["event_fingerprint"],
                "source": r["source"],
            }
        )
    return out


def insert_generated(
    *,
    fact_id: int,
    text: str,
    guardrail_flag: str | None,
    media_path: str | None = None,
    media_url: str | None = None,
    media_kind: str | None = None,
    media_strategy: str | None = None,
    image_query: str | None = None,
    media_warning: str | None = None,
    archetype_override: str | None = None,
    run_tag: str | None = None,
    is_test: bool = False,
) -> int:
    conn = _connect()
    cur = conn.execute(
        """
        INSERT INTO generated_live (
            fact_id, text, guardrail_flag, status, created_at,
            media_path, media_url, media_kind, media_strategy,
            image_query, media_warning, archetype_override, run_tag, is_test
        ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fact_id,
            text,
            guardrail_flag,
            int(time.time()),
            media_path,
            media_url,
            media_kind,
            media_strategy,
            image_query,
            media_warning,
            archetype_override,
            run_tag,
            int(is_test),
        ),
    )
    gid = int(cur.lastrowid)
    # news_id = видимый id = id строки (автоинкремент)
    conn.execute(
        "UPDATE generated_live SET news_id=? WHERE id=?",
        (gid, gid),
    )
    conn.commit()
    conn.close()
    return gid


def register_run_24h(
    *,
    news_id: int,
    generated_id: int,
    fact_id: int,
    source: str | None,
    msg_id: int | None,
    raw_text: str | None,
    fact: str,
    event: dict | None,
    image_query: str | None,
    archetype: str,
    media_strategy: str | None,
    run_tag: str | None = None,
    is_test: bool = False,
    is_garbage: bool = False,
) -> None:
    import json

    ev = event or {}
    conn = _connect()
    conn.execute(
        """
        INSERT OR REPLACE INTO run_24h (
            news_id, generated_id, fact_id, source, msg_id, raw_text, fact,
            event_kind, event_teams, event_player, event_to_club, event_score,
            event_minute, event_fingerprint, image_query, archetype,
            media_strategy, created_at, run_tag, is_test, is_garbage
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            news_id,
            generated_id,
            fact_id,
            source,
            msg_id,
            raw_text,
            fact,
            ev.get("event_kind"),
            json.dumps(ev.get("teams") or [], ensure_ascii=False),
            ev.get("player"),
            ev.get("to_club"),
            ev.get("score"),
            ev.get("minute"),
            None,
            image_query,
            archetype,
            media_strategy,
            int(time.time()),
            run_tag,
            int(is_test),
            int(is_garbage),
        ),
    )
    conn.commit()
    conn.close()


def news_id_exists(news_id: int) -> bool:
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM generated_live WHERE news_id=? OR id=?",
        (news_id, news_id),
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT 1 FROM run_24h WHERE news_id=?", (news_id,)
        ).fetchone()
    conn.close()
    return row is not None


def update_run_24h_decision(
    *,
    news_id: int,
    decision: str,
    edited_text: str | None = None,
    duplicate_of: int | None = None,
    eval_scope: str | None = None,
    old_archetype: str | None = None,
    archetype: str | None = None,
) -> None:
    conn = _connect()
    conn.execute(
        """
        UPDATE run_24h SET
            decision=?,
            edited_text=COALESCE(?, edited_text),
            duplicate_of=COALESCE(?, duplicate_of),
            eval_scope=COALESCE(?, eval_scope),
            old_archetype=COALESCE(?, old_archetype),
            archetype=COALESCE(?, archetype),
            decided_at=?
        WHERE news_id=?
        """,
        (
            decision,
            edited_text,
            duplicate_of,
            eval_scope,
            old_archetype,
            archetype,
            int(time.time()),
            news_id,
        ),
    )
    conn.commit()
    conn.close()


def next_pending_generated() -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute(
        """
        SELECT g.id AS generated_id, g.news_id, g.fact_id, g.text AS generated, g.guardrail_flag,
               g.media_path, g.media_url, g.media_kind, g.media_strategy,
               g.image_query, g.media_warning, g.archetype_override, g.run_tag,
               f.fact, f.archetype, f.veracity, f.is_sensation, f.attribution,
               f.image_query AS fact_image_query,
               f.event_kind, f.event_teams, f.event_player, f.event_to_club,
               f.event_score, f.event_minute, f.event_fingerprint,
               r.source, r.msg_id, r.text AS source_text
        FROM generated_live g
        JOIN facts f ON f.id = g.fact_id
        LEFT JOIN raw_messages r ON r.id = f.raw_msg_id
        WHERE g.status = 'pending'
        ORDER BY g.id ASC
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_fact_bundle(fact_id: int) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute(
        """
        SELECT f.*, r.source, r.msg_id, r.text AS source_text
        FROM facts f
        LEFT JOIN raw_messages r ON r.id = f.raw_msg_id
        WHERE f.id=?
        """,
        (fact_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_fact_archetype(fact_id: int, archetype: str) -> None:
    conn = _connect()
    conn.execute("UPDATE facts SET archetype=? WHERE id=?", (archetype, fact_id))
    conn.commit()
    conn.close()


def set_generated_status(generated_id: int, status: str) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE generated_live SET status=? WHERE id=?",
        (status, generated_id),
    )
    conn.commit()
    conn.close()


def set_generated_news_id(generated_id: int, news_id: int) -> None:
    """Видимый #N карточки (не менять id строки)."""
    conn = _connect()
    conn.execute(
        "UPDATE generated_live SET news_id=? WHERE id=?",
        (news_id, generated_id),
    )
    conn.commit()
    conn.close()


def log_live_decision(
    *,
    fact_id: int,
    generated_id: int,
    generated: str,
    decision: str,
    edited_text: str | None,
    source: str | None,
    source_msg_link: str | None,
    model: str,
    eval_scope: str | None = "skip_media",
    old_category: str | None = None,
    new_category: str | None = None,
    news_id: int | None = None,
    duplicate_of: int | None = None,
    raw_text: str | None = None,
    fact_snapshot: str | None = None,
    event_json: str | None = None,
    archetype_final: str | None = None,
    auto_image_query: str | None = None,
    manual_image_query: str | None = None,
    is_test: bool | None = None,
    is_garbage: bool = False,
) -> None:
    if is_test is None:
        src = (source or "").lower()
        is_test = (
            src.startswith("test_")
            or src in {"test", "test_source"}
            or (raw_text or "") == "raw goal text"
            or (
                not (raw_text or "").strip()
                and (fact_snapshot or "")
                in ("Мем/видео без текста", "Видео из источника", "Мем из источника")
            )
        )
    conn = _connect()
    conn.execute(
        """
        INSERT INTO calibration_log_live (
            fact_id, generated_id, generated, decision, edited_text,
            source, source_msg_link, model, created_at,
            eval_scope, old_category, new_category,
            news_id, duplicate_of, raw_text, fact_snapshot, event_json, archetype_final,
            auto_image_query, manual_image_query, is_test, is_garbage
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fact_id,
            generated_id,
            generated,
            decision,
            edited_text,
            source,
            source_msg_link,
            model,
            int(time.time()),
            eval_scope,
            old_category,
            new_category,
            news_id,
            duplicate_of,
            raw_text,
            fact_snapshot,
            event_json,
            archetype_final,
            auto_image_query,
            manual_image_query,
            int(bool(is_test)),
            int(bool(is_garbage)),
        ),
    )
    conn.commit()
    conn.close()
    if news_id is not None:
        update_run_24h_decision(
            news_id=news_id,
            decision=decision,
            edited_text=edited_text,
            duplicate_of=duplicate_of,
            eval_scope=eval_scope,
            old_archetype=old_category,
            archetype=archetype_final or new_category,
        )


def live_summary(*, include_test: bool = False) -> dict[str, int]:
    conn = _connect()
    if include_test:
        rows = conn.execute(
            "SELECT decision, COUNT(*) AS n FROM calibration_log_live GROUP BY decision"
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT decision, COUNT(*) AS n FROM calibration_log_live
            WHERE COALESCE(is_test, 0)=0
            GROUP BY decision
            """
        ).fetchall()
    conn.close()
    out = {"accepted": 0, "rejected": 0, "edited": 0, "total": 0}
    for r in rows:
        out[r["decision"] or "unknown"] = r["n"]
        out["total"] += r["n"]
    return out


def analysis_log_rows(*, include_test: bool = False) -> list[dict[str, Any]]:
    """Строки calibration_log_live для анализа (по умолчанию без is_test)."""
    conn = _connect()
    if include_test:
        rows = conn.execute(
            "SELECT * FROM calibration_log_live ORDER BY id ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM calibration_log_live
            WHERE COALESCE(is_test, 0)=0
            ORDER BY id ASC
            """
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def run_24h_counts_by_source(*, run_tag: str | None = None) -> list[tuple[str, int]]:
    conn = _connect()
    if run_tag:
        rows = conn.execute(
            """
            SELECT source, COUNT(*) AS n FROM run_24h
            WHERE run_tag=? AND COALESCE(is_test,0)=0
            GROUP BY source ORDER BY n DESC
            """,
            (run_tag,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT source, COUNT(*) AS n FROM run_24h
            WHERE COALESCE(is_test,0)=0
            GROUP BY source ORDER BY n DESC
            """
        ).fetchall()
    conn.close()
    return [(r["source"] or "?", r["n"]) for r in rows]


def raw_counts_by_source() -> list[tuple[str, int]]:
    conn = _connect()
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM raw_messages GROUP BY source ORDER BY n DESC"
    ).fetchall()
    conn.close()
    return [(r["source"], r["n"]) for r in rows]


def filtered_sample(limit: int = 30) -> list[dict[str, Any]]:
    conn = _connect()
    rows = conn.execute(
        """
        SELECT source, msg_id, text, filter_reason, ts
        FROM raw_messages WHERE is_filtered=1
        ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def raw_sample(source: str, limit: int = 30) -> list[dict[str, Any]]:
    conn = _connect()
    rows = conn.execute(
        """
        SELECT source, msg_id, text, is_filtered, filter_reason, ts
        FROM raw_messages WHERE source=?
        ORDER BY id DESC LIMIT ?
        """,
        (source, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fact_pairs_sample(limit: int = 30) -> list[dict[str, Any]]:
    conn = _connect()
    rows = conn.execute(
        """
        SELECT r.text AS source_text, r.source, f.fact, f.archetype, f.veracity, f.attribution
        FROM facts f
        JOIN raw_messages r ON r.id = f.raw_msg_id
        WHERE f.dedup_of IS NULL
        ORDER BY f.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
