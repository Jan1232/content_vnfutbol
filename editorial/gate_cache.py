"""Persistent cache for donor gate verdicts (as_is/template/reject)."""

from __future__ import annotations

import json
from typing import Any

from app.db import db, init_db

from editorial.soccerblog_gate import GATE_VERSION

CACHE_KIND_VERSION = GATE_VERSION


def _cache_key(feed_name: str, post_external_id: str) -> str:
    return f"{feed_name}:{post_external_id}"


def _is_poisoned_verdict(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return True
    if data.get("gate_failed"):
        return True
    reason = str(data.get("reason") or "").strip()
    if "gate error" in reason.lower():
        return True
    ver = int(data.get("gate_version") or 0)
    if ver < CACHE_KIND_VERSION:
        return True
    kind = str(data.get("kind") or "").lower()
    if kind in {"meme", "news"}:
        return True
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return kind == "as_is" and conf == 0.0 and not reason


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
    if not isinstance(data, dict) or _is_poisoned_verdict(data):
        return None
    return data


def put_gate_verdict(feed_name: str, post_external_id: str, verdict: dict[str, Any]) -> None:
    if _is_poisoned_verdict(verdict):
        return
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
