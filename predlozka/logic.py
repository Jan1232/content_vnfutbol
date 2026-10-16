from __future__ import annotations

import html
import re
import secrets
from typing import Any
from urllib.parse import quote

SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
CMD_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@[\w.]+)?(?:\s+(.*))?$", re.DOTALL)


def make_slug() -> str:
    return secrets.token_hex(8)


def is_valid_slug(value: str | None) -> bool:
    return bool(value and SLUG_RE.fullmatch(value.strip()))


def deep_link(username: str, slug: str) -> str:
    name = (username or "").strip().lstrip("@")
    if not name:
        raise ValueError("username бота пуст")
    return f"https://max.ru/{name}?start={quote(slug, safe='')}"


def parse_command(text: str | None) -> tuple[str, str] | None:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None
    first, _, rest = raw.partition("\n")
    m = CMD_RE.match(first.strip())
    if not m:
        return None
    name = m.group(1).lower()
    arg = (m.group(2) or "").strip()
    if rest:
        arg = f"{arg}\n{rest}".strip() if arg else rest.strip()
    return name, arg


def user_id_of(obj: dict[str, Any] | None) -> int | None:
    if not isinstance(obj, dict):
        return None
    raw = obj.get("user_id") or obj.get("id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def user_label(user: dict[str, Any] | None) -> str:
    if not isinstance(user, dict):
        return "пользователь"
    first = str(user.get("first_name") or user.get("name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    name = " ".join(p for p in (first, last) if p) or "пользователь"
    username = str(user.get("username") or "").strip().lstrip("@")
    if username:
        return f"{name} (@{username})"
    uid = user_id_of(user)
    if uid:
        return f"{name} (id {uid})"
    return name


def message_body(message: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    body = message.get("body")
    if isinstance(body, dict):
        return body
    body = message.get("message_body")
    return body if isinstance(body, dict) else {}


def message_text(message: dict[str, Any] | None) -> str:
    return str(message_body(message).get("text") or "").strip()


def message_mid(message: dict[str, Any] | None) -> str:
    body = message_body(message)
    for key in ("mid", "message_id", "id"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    if isinstance(message, dict):
        for key in ("mid", "message_id", "id"):
            val = message.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def message_attachments(message: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = message_body(message).get("attachments") or []
    return [a for a in raw if isinstance(a, dict)]


def recipient_chat_type(message: dict[str, Any] | None) -> str:
    if not isinstance(message, dict):
        return ""
    rec = message.get("recipient")
    if not isinstance(rec, dict):
        return ""
    return str(rec.get("chat_type") or rec.get("type") or "").strip().lower()


def is_dialog_message(message: dict[str, Any] | None) -> bool:
    return recipient_chat_type(message) in {"", "dialog", "dialogue"}


def sender_of(message: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    sender = message.get("sender")
    return sender if isinstance(sender, dict) else {}


def format_owner_link_text(title: str, url: str) -> str:
    safe_title = html.escape((title or "канал").strip() or "канал")
    safe_url = html.escape(url)
    return (
        f"Ссылка предложки для канала «{safe_title}»:\n"
        f"<code>{safe_url}</code>\n\n"
        "Отправьте её авторам. Обычная ссылка на бота не принимается — "
        "работает только эта."
    )


def format_added_text(title: str, url: str) -> str:
    safe_title = html.escape((title or "канал").strip() or "канал")
    safe_url = html.escape(url)
    return (
        f"Бот добавлен в канал «{safe_title}».\n\n"
        f"Уникальная ссылка предложки:\n<code>{safe_url}</code>\n\n"
        "Повторно получить ссылку: /url"
    )


def format_submit_welcome(title: str) -> str:
    safe_title = html.escape((title or "канал").strip() or "канал")
    return (
        f"Можно отправить текст, фото, видео или файл для канала «{safe_title}».\n"
        "Сообщение уйдёт владельцу канала."
    )


def display_name(user: dict[str, Any] | None) -> str:
    if not isinstance(user, dict):
        return "пользователь"
    first = str(user.get("first_name") or user.get("name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    return " ".join(p for p in (first, last) if p) or "пользователь"


def sender_web_link(user: dict[str, Any] | None) -> str:
    if not isinstance(user, dict):
        return ""
    username = str(user.get("username") or "").strip().lstrip("@")
    if username:
        return f"https://max.ru/{username}"
    return ""


def sender_html_mention(user: dict[str, Any] | None) -> str:
    name = html.escape(display_name(user))
    uid = user_id_of(user)
    if uid:
        return f'<a href="max://user/{uid}">{name}</a>'
    return name


def format_suggestion_header(title: str, sender: dict[str, Any] | None) -> str:
    safe_title = html.escape((title or "канал").strip() or "канал")
    lines = [
        f"📩 Предложка для «{safe_title}»",
        f"От: {sender_html_mention(sender)}",
    ]
    url = sender_web_link(sender)
    if url:
        lines.append(f"Профиль: {html.escape(url)}")
    uid = user_id_of(sender)
    if uid:
        lines.append(f"id: <code>{uid}</code>")
    return "\n".join(lines)


def combine_header_and_text(header: str, text: str, limit: int = 4000) -> str:
    body = (text or "").strip()
    if body:
        out = f"{header}\n\n{body}"
    else:
        out = header
    if len(out) > limit:
        return out[: limit - 1] + "…"
    return out


def attachment_url(att: dict[str, Any]) -> str:
    payload = att.get("payload")
    if isinstance(payload, dict):
        for key in ("url", "file_url", "photo_url"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    for key in ("url", "file_url"):
        val = att.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def inline_keyboard(rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def callback_btn(text: str, payload: str) -> dict[str, Any]:
    return {"type": "callback", "text": (text or "…")[:64], "payload": payload[:1024]}


def link_btn(text: str, url: str) -> dict[str, Any]:
    return {"type": "link", "text": (text or "Ссылка")[:64], "url": url}


def callback_keyboard(channels: list[dict[str, Any]], prefix: str = "url") -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = []
    for ch in channels:
        title = str(ch.get("title") or f"канал {ch.get('chat_id')}").strip() or "канал"
        rows.append([callback_btn(title, f"{prefix}:{int(ch['chat_id'])}")])
    return inline_keyboard(rows)


def suggestion_keyboard(chat_id: int, sender: dict[str, Any] | None) -> list[dict[str, Any]]:
    uid = user_id_of(sender)
    row: list[dict[str, Any]] = []
    if uid:
        row.append(callback_btn("Забанить", f"ban:{int(chat_id)}:{uid}"))
    url = sender_web_link(sender)
    if url:
        row.append(link_btn("Профиль", url))
    return inline_keyboard([row]) if row else []


def parse_callback_payload(payload: str | None) -> tuple[str, list[str]] | None:
    raw = (payload or "").strip()
    if not raw or ":" not in raw:
        return None
    kind, _, rest = raw.partition(":")
    if not kind or not rest:
        return None
    return kind, rest.split(":")


def parse_url_callback(payload: str | None) -> int | None:
    parsed = parse_callback_payload(payload)
    if not parsed or parsed[0] != "url":
        return None
    try:
        return int(parsed[1][0])
    except (TypeError, ValueError, IndexError):
        return None


def parse_int_setting(text: str | None) -> int | None:
    raw = (text or "").strip().replace(" ", "")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value


def length_error(text: str, max_chars: int) -> str | None:
    if max_chars <= 0:
        return None
    n = len(text or "")
    if n > max_chars:
        return f"Слишком длинный текст: {n} из {max_chars} символов."
    return None


def rate_error(count: int, limit: int) -> str | None:
    if limit <= 0:
        return None
    if count >= limit:
        return f"Лимит: не больше {limit} сообщений в час от одного отправителя. Попробуйте позже."
    return None


def format_settings_text(title: str, rate_per_hour: int, max_chars: int) -> str:
    safe_title = html.escape((title or "канал").strip() or "канал")
    rate = "без лимита" if rate_per_hour <= 0 else f"{rate_per_hour} в час"
    length = "без лимита" if max_chars <= 0 else f"{max_chars} символов"
    return (
        f"Настройки антифрода для «{safe_title}»\n\n"
        f"• Сообщений от одного человека: <b>{html.escape(rate)}</b>\n"
        f"• Макс. длина текста: <b>{html.escape(length)}</b>\n\n"
        "Нажмите кнопку и отправьте своё число. 0 — без ограничения."
    )


def settings_keyboard(chat_id: int) -> list[dict[str, Any]]:
    cid = int(chat_id)
    return inline_keyboard(
        [
            [callback_btn("Сообщений в час", f"setrate:{cid}")],
            [callback_btn("Длина текста", f"setlen:{cid}")],
        ]
    )


def format_ban_list_text(title: str, bans: list[dict[str, Any]]) -> str:
    safe_title = html.escape((title or "канал").strip() or "канал")
    if not bans:
        return f"В канале «{safe_title}» никто не забанен."
    return (
        f"Забаненные в «{safe_title}» ({len(bans)}).\n"
        "Нажмите кнопку, чтобы разбанить."
    )


def ban_list_keyboard(chat_id: int, bans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = []
    for item in bans[:20]:
        label = str(item.get("label") or f"id {item.get('user_id')}").strip() or "пользователь"
        rows.append([callback_btn(f"Разбан: {label}", f"unban:{int(chat_id)}:{int(item['user_id'])}")])
    return inline_keyboard(rows) if rows else []
