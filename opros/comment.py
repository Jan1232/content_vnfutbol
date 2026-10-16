"""Комментарии от бота «Предложка» под постами квизов."""

from __future__ import annotations

from typing import Any

from predlozka.client import PredlozkaClient
from predlozka.config import bot_token


def format_answer_comment(answer: str) -> str:
    a = (answer or "").strip()
    return f"✅ Ответ: {a}" if a else "✅ Ответ"


def post_quiz_answer_comment(
    *,
    chat_id: int,
    post_message_id: str,
    answer: str,
) -> dict[str, Any]:
    """Пишет ответ в ветку комментариев к посту (не отдельным постом в ленте)."""
    mid = (post_message_id or "").strip()
    if not mid:
        raise RuntimeError("empty post message_id")
    text = format_answer_comment(answer)
    # chat_id не нужен API comments, но оставляем в сигнатуре для логов/вызывающего кода
    _ = chat_id
    with PredlozkaClient(token=bot_token()) as client:
        resp = client._request(
            "POST",
            f"/messages/{mid}/comments",
            json={"text": text[:4000]},
        )
    return {"response": resp, "text": text}


def extract_comment_mid(resp: dict[str, Any] | None) -> str:
    if not isinstance(resp, dict):
        return ""
    msg = resp.get("message")
    if isinstance(msg, dict):
        body = msg.get("body") if isinstance(msg.get("body"), dict) else msg
        if isinstance(body, dict):
            mid = body.get("mid") or body.get("message_id") or body.get("id")
            if isinstance(mid, str) and mid:
                return mid
        mid = msg.get("mid") or msg.get("message_id") or msg.get("id")
        if isinstance(mid, str) and mid:
            return mid
    for key in ("mid", "message_id", "id"):
        if isinstance(resp.get(key), str) and resp[key]:
            return resp[key]
    return ""
