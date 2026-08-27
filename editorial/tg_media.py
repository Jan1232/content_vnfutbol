"""Download Telegram media for editorial meme/video posts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.config import ROOT, get_settings
from app.http_util import http_client


def _media_dir() -> Path:
    d = ROOT / "data" / "editorial" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _first_media(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        entities = json.loads(row.get("entities_json") or "{}")
    except Exception:
        entities = {}
    raw = entities.get("raw") if isinstance(entities.get("raw"), dict) else {}
    media = raw.get("media") or []
    if not media:
        body_raw = row.get("body") or ""
        if isinstance(body_raw, str) and body_raw.startswith("{"):
            try:
                parsed = json.loads(body_raw)
                media = parsed.get("media") or []
            except Exception:
                pass
    media_type = str(row.get("media_type") or "")
    for m in media:
        if not isinstance(m, dict):
            continue
        if media_type == "video" and (m.get("type") or "") == "video":
            return m
        if media_type == "image" and (m.get("type") or "") == "image" and m.get("url"):
            return m
    for m in media:
        if isinstance(m, dict) and m.get("url"):
            return m
    return None


def download_item_media(row: dict[str, Any]) -> str:
    """Скачивает медиа поста в data/editorial/media. Возвращает путь."""
    existing = str(row.get("media_path") or "").strip()
    if existing and Path(existing).is_file():
        return existing
    media = _first_media(row)
    if not media:
        raise RuntimeError("нет медиа в raw")
    url = str(media.get("url") or "").strip()
    if not url:
        raise RuntimeError("нет URL медиа (too_big — нужен Telethon)")
    settings = get_settings()
    max_bytes = int(getattr(settings, "video_max_mb", 250) or 250) * 1024 * 1024
    ext = ".mp4" if str(row.get("media_type") or "") == "video" else ".jpg"
    name = hashlib.sha1(f"{row.get('external_id')}|{url}".encode()).hexdigest()[:20]
    dest = _media_dir() / f"{name}{ext}"
    if dest.is_file():
        return str(dest)
    with http_client() as client:
        r = client.get(url, follow_redirects=True)
        r.raise_for_status()
        content = r.content
    if len(content) > max_bytes:
        raise RuntimeError(f"медиа больше {max_bytes // (1024*1024)}MB")
    dest.write_bytes(content)
    return str(dest)
