"""Content-type blocklist built from «недопустимый» moderation decisions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from editorial.models import NewsItem


# reason_id → какие поля профиля фиксируем для автоблока похожего
BLOCK_SCOPE: dict[str, tuple[str, ...]] = {
    "feed_trash": ("source",),
    "gossip_rumor": ("pick_tag", "event_type"),
    "digest_roundup": ("pick_tag",),
    "meme_low": ("post_kind", "source"),
    "entertainment_noise": ("post_kind", "event_type"),
    "off_topic": ("event_type", "source"),
    "source_event": ("source", "event_type"),
}

UNACCEPTABLE_LABELS: dict[str, str] = {
    "feed_trash": "Мусорный источник / фид",
    "gossip_rumor": "Слухи / gossip",
    "digest_roundup": "Дайджест / сводка",
    "meme_low": "Слабые мемы / кринж",
    "entertainment_noise": "Лишний entertainment",
    "off_topic": "Не по теме канала",
    "source_event": "Источник + тип события",
}


def _blocks_path() -> Path:
    from app.config import ROOT

    settings = get_settings()
    p = Path(getattr(settings, "moderation_blocks_file", None) or ROOT / "data/editorial/feedback/content_blocks.json")
    return p if p.is_absolute() else ROOT / p


def _load_raw() -> dict[str, Any]:
    path = _blocks_path()
    if not path.is_file():
        return {"blocks": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"blocks": []}
    except Exception:
        return {"blocks": []}


def _save_raw(data: dict[str, Any]) -> None:
    path = _blocks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def content_profile(item: NewsItem | dict[str, Any]) -> dict[str, str]:
    if isinstance(item, NewsItem):
        row = {
            "source": item.source,
            "event_type": item.event_type or "",
            "post_kind": str((item.raw or {}).get("post_kind") or "news"),
            "meme_source": "1" if (item.entities or {}).get("meme_source") else "0",
            "entities_json": json.dumps(item.entities or {}, ensure_ascii=False),
        }
    else:
        row = item
    try:
        entities = json.loads(row.get("entities_json") or "{}")
    except Exception:
        entities = {}
    pick = entities.get("pick") if isinstance(entities.get("pick"), dict) else {}
    return {
        "source": str(row.get("source") or ""),
        "event_type": str(row.get("event_type") or ""),
        "post_kind": str(row.get("post_kind") or "news"),
        "pick_tag": str(pick.get("tag") or ""),
        "meme_source": str(int(row.get("meme_source") or 0)),
    }


def profile_subset(full: dict[str, str], reason: str) -> dict[str, str]:
    keys = BLOCK_SCOPE.get(reason) or ("source", "event_type", "pick_tag")
    out: dict[str, str] = {}
    for k in keys:
        v = str(full.get(k) or "").strip()
        if v:
            out[k] = v
    return out


def add_content_block(
    item: dict[str, Any],
    *,
    reason: str,
    news_id: int | None = None,
    note: str = "",
) -> dict[str, Any]:
    full = content_profile(item)
    profile = profile_subset(full, reason)
    block = {
        "id": f"blk_{int(news_id or item.get('id') or 0)}_{reason}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "label": UNACCEPTABLE_LABELS.get(reason, reason),
        "profile": profile,
        "note": (note or "")[:400],
        "news_id": int(news_id or item.get("id") or 0),
    }
    data = _load_raw()
    blocks = list(data.get("blocks") or [])
    # dedupe same profile+reason
    for b in blocks:
        if b.get("reason") == reason and b.get("profile") == profile:
            return b
    blocks.append(block)
    data["blocks"] = blocks[-200:]
    _save_raw(data)
    return block


def is_content_blocked(item: NewsItem | dict[str, Any]) -> tuple[bool, str]:
    prof = content_profile(item)
    for block in _load_raw().get("blocks") or []:
        bp = block.get("profile") if isinstance(block.get("profile"), dict) else {}
        if not bp:
            continue
        if all(str(prof.get(k) or "") == str(v) for k, v in bp.items()):
            label = block.get("label") or block.get("reason") or "blocked"
            return True, str(label)
    return False, ""


def list_blocks() -> list[dict[str, Any]]:
    return list(_load_raw().get("blocks") or [])
