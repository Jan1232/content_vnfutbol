"""Persistent cache for soccerblog_gate verdicts (meme/news/reject)."""

from __future__ import annotations

import json
from typing import Any

from app.db import db, init_db


def _cache_key(feed_name: str, post_external_id: str) -> str:
    return f"{feed_name}:{post_external_id}"


def get_gate_verdict(feed_name: str, post_external_id: str) -> dict[str, Any] | None:
    init_db()
    key = _cache_key(feed_name, post_external_id)
    with db() as conn:
        row = conn.execute(
            "SELECT verdict_json FROM editorial_gate_cache WHERE cache_key=? LIMIT 1",
            (key,),
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["verdict_json"] or "{}")
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def put_gate_verdict(feed_name: str, post_external_id: str, verdict: dict[str, Any]) -> None:
    init_db()
    key = _cache_key(feed_name, post_external_id)
    payload = {k: v for k, v in verdict.items() if not str(k).startswith("_")}
    with db() as conn:
        conn.execute(
            """
            INSERT INTO editorial_gate_cache (cache_key, feed_name, post_external_id, verdict_json, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(cache_key) DO UPDATE SET
              verdict_json=excluded.verdict_json,
              updated_at=datetime('now')
            """,
            (key, feed_name, post_external_id, json.dumps(payload, ensure_ascii=False)),
        )
