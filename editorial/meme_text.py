"""Текст мемов: оригинал из источника + смягчение мата."""

from __future__ import annotations

from typing import Any

from editorial.profanity import replace_profanity


def is_meme_row(row: dict[str, Any]) -> bool:
    if int(row.get("meme_source") or 0):
        return True
    return str(row.get("post_kind") or "") in {"meme", "video"}


def source_text(row: dict[str, Any]) -> str:
    body = str(row.get("body") or "").strip()
    if body:
        return body
    return str(row.get("title") or "").strip()


def prepare_meme_post(row: dict[str, Any]) -> dict[str, str]:
    raw = source_text(row)
    post_text = replace_profanity(raw)
    return {
        "post_text": post_text,
        "headline": "",
        "caption_line1": "",
        "caption_line2": "",
        "emoji_lead": "",
    }


def apply_meme_post(row: dict[str, Any]) -> dict[str, str]:
    """Готовые поля для update_news — только первоисточник."""
    return prepare_meme_post(row)
