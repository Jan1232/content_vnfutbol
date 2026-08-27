"""Mirror editorial posts to a Telegram channel after MAX publish."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import get_settings
from editorial.channel_config import EditorialChannelConfig, brand_render_context
from editorial.render import render_mirror_cover
from editorial.tg_content import api


def mirror_enabled(channel: EditorialChannelConfig) -> bool:
    settings = get_settings()
    if not (settings.telegram_content_bot_token or "").strip():
        return False
    if not channel.telegram_mirror.enabled:
        return False
    return bool(_target_chat_id(channel))


def _target_chat_id(channel: EditorialChannelConfig) -> str:
    settings = get_settings()
    return (
        (channel.telegram_mirror.channel or "").strip()
        or (settings.telegram_content_channel or "").strip()
    )


def _cover_for_telegram(channel: EditorialChannelConfig, item: dict[str, Any]) -> Path:
    tg_brand = brand_render_context(channel, for_telegram=True)
    mirrored = render_mirror_cover(channel, item, channel_brand=tg_brand)
    if mirrored and Path(mirrored).is_file():
        return Path(mirrored)
    cover = Path(item.get("cover_path") or item.get("media_path") or "")
    return cover


def publish_mirror(channel: EditorialChannelConfig, item: dict[str, Any]) -> dict[str, Any]:
    """Отправить тот же контент (фото/видео + текст) в TG-канал."""
    chat_id = _target_chat_id(channel)
    if not chat_id:
        return {"ok": False, "error": "telegram channel not configured"}
    text = (item.get("post_text") or "").strip()
    media_type = str(item.get("media_type") or "")
    post_kind = str(item.get("post_kind") or "")

    try:
        if media_type == "video" or post_kind == "video":
            vpath = Path(item.get("media_path") or "")
            if not vpath.is_file():
                return {"ok": False, "error": "нет video файла"}
            msg = api.send_video(chat_id, vpath, caption=text)
        else:
            cover = _cover_for_telegram(channel, item)
            if not cover.is_file():
                return {"ok": False, "error": "нет cover/media"}
            msg = api.send_photo(chat_id, cover, caption=text)
    except api.TelegramContentApiError as e:
        return {"ok": False, "error": str(e)[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:400]}

    return {
        "ok": True,
        "message_id": int(msg.get("message_id") or 0),
        "chat_id": chat_id,
    }
