"""Thin Telegram Bot API client (sync httpx)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.http_util import http_client


class TelegramApiError(RuntimeError):
    pass


def _token() -> str:
    tok = (get_settings().api_telegram_bot_token or "").strip()
    if not tok:
        raise TelegramApiError("API_TELEGRAM_BOT_TOKEN не задан")
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
        elif isinstance(v, list) and k in {"allowed_updates", "media"}:
            data[k] = json.dumps(v, ensure_ascii=False)
            json_payload[k] = v
        elif v is not None:
            data[k] = v
            json_payload[k] = v
    with http_client(timeout=90.0) as client:
        if files:
            r = client.post(url, data=data, files=files)
        else:
            r = client.post(url, json=json_payload)
        body = r.json()
    if not body.get("ok"):
        raise TelegramApiError(str(body.get("description") or body))
    return body.get("result")


def get_updates(offset: int = 0, timeout: int = 25) -> list[dict[str, Any]]:
    res = _call(
        "getUpdates",
        offset=offset,
        timeout=timeout,
        allowed_updates=["message", "callback_query"],
    )
    return res if isinstance(res, list) else []


def send_message(
    chat_id: int | str,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = "HTML",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4096],
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = _call("sendMessage", **payload)
    return res if isinstance(res, dict) else {}


def edit_message_reply_markup(
    chat_id: int | str, message_id: int, reply_markup: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    res = _call("editMessageReplyMarkup", **payload)
    return res if isinstance(res, dict) else {}


def edit_message_caption(
    chat_id: int | str,
    message_id: int,
    caption: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str = "HTML",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption[:1024],
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    res = _call("editMessageCaption", **payload)
    return res if isinstance(res, dict) else {}


def edit_message_text(
    chat_id: int | str,
    message_id: int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str = "HTML",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4096],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    res = _call("editMessageText", **payload)
    return res if isinstance(res, dict) else {}


def remove_inline_keyboard(chat_id: int | str, message_id: int) -> None:
    edit_message_reply_markup(chat_id, message_id, {"inline_keyboard": []})


def answer_callback(callback_query_id: str, text: str = "", *, show_alert: bool = False) -> None:
    _call(
        "answerCallbackQuery",
        callback_query_id=callback_query_id,
        text=text[:200],
        show_alert=show_alert,
    )


def send_photo(
    chat_id: int | str,
    photo_path: Path,
    *,
    caption: str = "",
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "photo": photo_path,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = _call("sendPhoto", **payload)
    return res if isinstance(res, dict) else {}


def send_video(
    chat_id: int | str,
    video_path: Path,
    *,
    caption: str = "",
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "video": video_path,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = _call("sendVideo", **payload)
    return res if isinstance(res, dict) else {}
