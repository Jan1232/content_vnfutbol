"""Формат поста и отправка в MAX."""

from __future__ import annotations

import html as html_lib
from typing import Any

from app.max_api import MaxClient
from content.parser import SeriesItem, referral_url


def format_genres(genres: tuple[str, ...] | list[str] | None) -> str:
    if not genres:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for name in genres:
        label = str(name or "").strip()
        key = label.casefold()
        if not label or key in seen:
            continue
        seen.add(key)
        parts.append(html_lib.escape(label))
        if len(parts) >= 8:
            break
    if not parts:
        return ""
    return "🎭 Жанр: " + ", ".join(p.casefold() for p in parts)


def seasons_label(seasons: int | None) -> str:
    if not seasons or seasons < 1:
        return ""
    n = int(seasons)
    mod10 = n % 10
    mod100 = n % 100
    if mod10 == 1 and mod100 != 11:
        word = "сезон"
    elif 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
        word = "сезона"
    else:
        word = "сезонов"
    return f"{n} {word}"


def series_title(title: str) -> str:
    name = (title or "").strip()
    low = name.casefold()
    if low.startswith("сериал:"):
        name = name.split(":", 1)[1].strip()
    elif low.startswith("сериал "):
        name = name[7:].strip()
    return f"Сериал: {name}".strip() if name else "Сериал:"


def format_post_text(item: SeriesItem) -> str:
    bits: list[str] = []
    if item.year:
        bits.append(str(item.year))
    seasons = seasons_label(item.seasons)
    if seasons:
        bits.append(seasons)
    meta = " · ".join(bits)
    title = html_lib.escape(series_title(item.title))
    lines = [f"🎥 <b>{title}</b>", ""]
    if meta:
        lines.append(f"📊 {html_lib.escape(meta)}")
    genres = format_genres(item.genres)
    if genres:
        lines.append("")
        lines.append(genres)
    desc = (item.description or "").strip()
    if desc:
        lines.append("")
        lines.append(f"💬 {html_lib.escape(desc)}")
    text = "\n".join(lines).strip()
    if len(text) > 4000:
        text = text[:3997] + "..."
    return text


def watch_button(url: str, label: str) -> dict[str, Any]:
    return {
        "type": "inline_keyboard",
        "payload": {
            "buttons": [
                [
                    {
                        "type": "link",
                        "text": (label or "СМОТРЕТЬ ОНЛАЙН")[:64],
                        "url": url,
                    }
                ]
            ]
        },
    }


def publish_series(
    client: MaxClient,
    item: SeriesItem,
    *,
    chat_id: int,
    button_text: str,
    ref: str,
) -> dict[str, Any]:
    watch_url = referral_url(item.url, ref=ref)
    text = format_post_text(item)
    attachments: list[dict[str, Any]] = []
    if item.poster_url:
        att = client.upload_image_from_url(item.poster_url)
        if att:
            attachments.append(att)
    attachments.append(watch_button(watch_url, button_text))
    resp = client.send_message(
        chat_id=chat_id,
        text=text,
        attachments=attachments,
        notify=True,
        disable_link_preview=True,
        format="html",
    )
    return {"response": resp, "watch_url": watch_url, "text": text}


def extract_message_id(resp: dict[str, Any] | None) -> str:
    if not isinstance(resp, dict):
        return ""
    for key in ("mid", "message_id", "id"):
        if isinstance(resp.get(key), str) and resp[key]:
            return resp[key]
    msg = resp.get("message")
    if isinstance(msg, dict):
        body = msg.get("body") if isinstance(msg.get("body"), dict) else msg
        if isinstance(body, dict):
            for key in ("mid", "message_id", "id"):
                if isinstance(body.get(key), str) and body[key]:
                    return body[key]
        for key in ("mid", "message_id", "id"):
            if isinstance(msg.get(key), str) and msg[key]:
                return msg[key]
    return ""
