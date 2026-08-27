"""JSONL log of human moderation decisions for later tuning."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT, get_settings


def _feedback_dir() -> Path:
    settings = get_settings()
    d = Path(getattr(settings, "moderation_feedback_dir", None) or ROOT / "data/editorial/feedback/moderation")
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_moderation(event: dict[str, Any]) -> Path:
    row = dict(event)
    row.setdefault("ts", datetime.now(timezone.utc).isoformat())
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = _feedback_dir() / f"{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def item_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    try:
        entities = json.loads(item.get("entities_json") or "{}")
    except Exception:
        entities = {}
    pick = entities.get("pick") if isinstance(entities.get("pick"), dict) else {}
    try:
        imagery = json.loads(item.get("imagery_meta_json") or "{}")
    except Exception:
        imagery = {}
    return {
        "news_id": item.get("id"),
        "channel_slug": item.get("channel_slug"),
        "source": item.get("source"),
        "url": item.get("url"),
        "event_type": item.get("event_type"),
        "post_kind": item.get("post_kind"),
        "pick_tag": pick.get("tag"),
        "title": (item.get("title") or "")[:240],
        "post_text": (item.get("post_text") or "")[:2000],
        "imagery_query": imagery.get("query") or "",
        "imagery_pick": imagery.get("pick") or {},
    }
