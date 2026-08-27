"""Telegram Bot API client for the content channel bot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.http_util import http_client


class TelegramContentApiError(RuntimeError):
    pass


def _token() -> str:
    tok = (get_settings().telegram_content_bot_token or "").strip()
    if not tok:
        raise TelegramContentApiError("TELEGRAM_CONTENT_BOT_TOKEN не задан")
    return tok


def _call(method: str, **payload: Any) -> Any:
    url = f"https://api.telegram.org/bot{_token()}/{method}"
    files: dict[str, Any] = {}
    data: dict[str, Any] = {}
    json_payload: dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, Path):
            files[k] = (v.name, v.read_bytes())
        elif isinstance(v, dict) and k == "reply_markup":
            data[k] = json.dumps(v, ensure_ascii=False)
            json_payload[k] = v
        elif v is not None:
            data[k] = v
            json_payload[k] = v
    with http_client(timeout=120.0) as client:
        if files:
            r = client.post(url, data=data, files=files)
        else:
            r = client.post(url, json=json_payload)
        body = r.json()
    if not body.get("ok"):
        raise TelegramContentApiError(str(body.get("description") or body))
    return body.get("result")


def send_photo(
    chat_id: int | str,
    photo_path: Path,
    *,
    caption: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "photo": photo_path,
        "caption": caption[:1024],
        "disable_notification": False,
    }
    res = _call("sendPhoto", **payload)
    return res if isinstance(res, dict) else {}


def send_video(
    chat_id: int | str,
    video_path: Path,
    *,
    caption: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "video": video_path,
        "caption": caption[:1024],
        "supports_streaming": True,
        "disable_notification": False,
    }
    res = _call("sendVideo", **payload)
    return res if isinstance(res, dict) else {}
