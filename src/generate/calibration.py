"""SQLite-лог решений владельца (сырьё калибровки, петля разомкнута)."""

from __future__ import annotations

import sqlite3
import time

from src.config import FAN_MODEL, ROOT

DB_PATH = ROOT / "data" / "calibration.db"

# Активный прогон бота: 1 = calibration_log, 2 = calibration_log_run2
ACTIVE_RUN = 2


def _table(run: int | None = None) -> str:
    r = ACTIVE_RUN if run is None else run
    return "calibration_log" if r == 1 else "calibration_log_run2"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    for table in ("calibration_log", "calibration_log_run2"):
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                eval_id INTEGER,
                fact TEXT NOT NULL,
                archetype TEXT,
                veracity TEXT,
                is_sensation INTEGER,
                generated TEXT,
                guardrail_flag TEXT,
                decision TEXT,
                edited_text TEXT,
                model TEXT,
                created_at INTEGER
            )
            """
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS moderator_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()
    return conn


def decided_eval_ids(run: int | None = None) -> set[int]:
    table = _table(run)
    conn = _connect()
    rows = conn.execute(
        f"SELECT eval_id FROM {table} WHERE eval_id IS NOT NULL AND decision IS NOT NULL"
    ).fetchall()
    conn.close()
    return {int(r["eval_id"]) for r in rows}


def log_decision(
    *,
    eval_id: int,
    fact: str,
    archetype: str,
    veracity: str,
    is_sensation: bool,
    generated: str,
    guardrail_flag: str | None,
    decision: str,
    edited_text: str | None,
    run: int | None = None,
) -> None:
    table = _table(run)
    conn = _connect()
    conn.execute(
        f"""
        INSERT INTO {table} (
            eval_id, fact, archetype, veracity, is_sensation,
            generated, guardrail_flag, decision, edited_text, model, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            eval_id,
            fact,
            archetype,
            veracity,
            int(is_sensation),
            generated,
            guardrail_flag,
            decision,
            edited_text,
            FAN_MODEL,
            int(time.time()),
        ),
    )
    conn.commit()
    conn.close()


def summary(run: int | None = None) -> dict[str, int]:
    table = _table(run)
    conn = _connect()
    rows = conn.execute(
        f"SELECT decision, COUNT(*) AS n FROM {table} GROUP BY decision"
    ).fetchall()
    conn.close()
    out = {"accepted": 0, "rejected": 0, "edited": 0, "total": 0}
    for r in rows:
        key = r["decision"] or "unknown"
        out[key] = r["n"]
        out["total"] += r["n"]
    return out


def get_state(key: str) -> str | None:
    conn = _connect()
    row = conn.execute(
        "SELECT value FROM moderator_state WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return None if row is None else row["value"]


def set_state(key: str, value: str | None) -> None:
    conn = _connect()
    if value is None:
        conn.execute("DELETE FROM moderator_state WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO moderator_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.commit()
    conn.close()
