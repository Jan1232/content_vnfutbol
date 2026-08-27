from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.http_util import http_client

TG_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,})",
    re.I,
)


@dataclass
class ParsedPost:
    external_id: str
    text: str
    media: list[dict[str, Any]] = field(default_factory=list)
    source_url: str = ""
    title: str = ""


def normalize_telegram_url(url: str) -> str | None:
    m = TG_RE.search(url.strip())
    if not m:
        return None
    username = m.group(1)
    if username.lower() in {"joinchat", "addstickers", "share", "proxy", "socks", "iv"}:
        return None
    return f"https://t.me/s/{username}"


def _message_text_preserve_air(text_el) -> str:
    """Текст поста с «воздухом»: <br><br> → пустая строка между абзацами.

    Ссылки <a href>: оставляем видимое название (имя игры и т.п.), URL отбрасываем.
    """
    if text_el is None:
        return ""
    # decode_contents: только внутренности, br сохраняем через парсер
    root = BeautifulSoup(f"<div>{text_el.decode_contents()}</div>", "lxml").div
    if root is None:
        return ""
    for br in list(root.find_all("br")):
        br.replace_with("\n")
    # <a href="store...">Cat Chaos</a> → «Cat Chaos» (без URL)
    for a in list(root.find_all("a")):
        label = a.get_text(" ", strip=True)
        href = (a.get("href") or "").strip()
        # если «текст ссылки» сам URL — не оставляем мусор
        if label and href and label.rstrip("/") == href.rstrip("/"):
            a.replace_with("")
        elif label:
            a.replace_with(label)
        else:
            a.replace_with("")
    raw = root.get_text()
    lines = [ln.replace("\xa0", " ").rstrip() for ln in raw.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out: list[str] = []
    empty_run = 0
    for ln in lines:
        if not ln.strip():
            empty_run += 1
            if empty_run == 1:
                out.append("")
            continue
        empty_run = 0
        out.append(re.sub(r"[ \t]{2,}", " ", ln.strip()))
    return "\n".join(out)


def _parse_message_meta(msg) -> dict[str, Any] | None:
    """Метаданные TG: пересылка, inline-кнопки, опрос."""
    forwarded = msg.select_one(".tgme_widget_message_forwarded_from_name")
    buttons: list[dict[str, str]] = []
    for btn in msg.select("a.tgme_widget_message_inline_button"):
        label = btn.get_text(strip=True)
        if not label:
            continue
        buttons.append({"text": label, "url": btn.get("href") or ""})

    is_poll = bool(
        msg.select_one(
            ".tgme_widget_message_poll, .tgme_widget_message_poll_options, .tgme_widget_message_poll_question"
        )
    )

    forwarded_from = forwarded.get_text(strip=True) if forwarded else ""
    if not forwarded_from and not buttons and not is_poll:
        return None

    return {
        "type": "tg_meta",
        "forwarded_from": forwarded_from,
        "buttons": buttons,
        "is_poll": is_poll,
    }


def _og_description(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for sel in (
        'meta[property="og:description"]',
        'meta[name="twitter:description"]',
        'meta[name="description"]',
    ):
        el = soup.select_one(sel)
        if not el:
            continue
        content = (el.get("content") or "").strip()
        if content:
            return content
    return ""


def _enrich_empty_post_text(client: httpx.Client, data_post: str, text: str) -> str:
    """В t.me/s реклама часто без текста; на странице поста og:description есть."""
    if (text or "").strip():
        return text
    try:
        r = client.get(f"https://t.me/{data_post}", follow_redirects=True)
        if r.status_code >= 400:
            return text
        desc = _og_description(r.text)
        return desc or text
    except Exception:
        return text


def parse_telegram(url: str, since_id: str = "") -> tuple[str, list[ParsedPost]]:
    """Публичный превью t.me/s/<channel>. Возвращает (title, posts) старые→новые."""
    norm = normalize_telegram_url(url)
    if not norm:
        raise ValueError("Некорректная ссылка Telegram. Нужен публичный канал: https://t.me/channel")

    username = norm.rsplit("/", 1)[-1]
    with http_client() as client:
        r = client.get(norm)
        r.raise_for_status()
        html = r.text

        soup = BeautifulSoup(html, "lxml")
        title_el = soup.select_one(".tgme_channel_info_header_title, .tgme_page_title")
        title = title_el.get_text(strip=True) if title_el else username

        posts: list[ParsedPost] = []
        for msg in soup.select(".tgme_widget_message"):
            data_post = msg.get("data-post") or ""
            if not data_post or "/" not in data_post:
                continue
            external_id = f"tg:{data_post}"

            # Основной текст поста, не цитату reply (у reply часто обрезок с «…»).
            text_el = msg.select_one(
                ".tgme_widget_message_text.js-message_text"
            ) or msg.select_one(".tgme_widget_message_text")
            text = _message_text_preserve_air(text_el)

            media: list[dict[str, Any]] = []
            for photo in msg.select(".tgme_widget_message_photo_wrap"):
                style = photo.get("style") or ""
                m = re.search(r"url\('([^']+)'\)", style)
                if m:
                    media.append({"type": "image", "url": m.group(1)})
            for img in msg.select(".tgme_widget_message_photo img, a.tgme_widget_message_photo_wrap"):
                src = img.get("src")
                if src:
                    media.append({"type": "image", "url": src})

            # видео/гиф с прямым mp4 в превью
            for vid in msg.select("video.tgme_widget_message_video, video.js-message_video"):
                vsrc = vid.get("src")
                if vsrc:
                    media.append({"type": "video", "url": vsrc})

            # «Media is too big» / not_supported — mp4 в HTML нет, качаем через Telethon
            has_video_url = any(
                (m.get("type") or "") == "video" and m.get("url") for m in media
            )
            player = msg.select_one(
                "a.tgme_widget_message_video_player.not_supported, "
                "a.tgme_widget_message_video_player"
            )
            too_big_label = msg.select_one(".message_media_not_supported_label")
            too_big_text = (too_big_label.get_text(strip=True) if too_big_label else "").lower()
            is_too_big = bool(
                too_big_label
                and ("too big" in too_big_text or "слишком больш" in too_big_text)
            ) or bool(
                player and "not_supported" in (player.get("class") or []) and not has_video_url
            )
            if is_too_big and not has_video_url:
                thumb_url = ""
                thumb_el = msg.select_one(".tgme_widget_message_video_thumb")
                if thumb_el:
                    style = thumb_el.get("style") or ""
                    tm = re.search(r"url\('([^']+)'\)", style)
                    if tm:
                        thumb_url = tm.group(1)
                dur_el = msg.select_one(
                    ".message_video_duration, .tgme_widget_message_video_duration"
                )
                media.append(
                    {
                        "type": "video",
                        "url": "",
                        "tg_ref": data_post,
                        "too_big": True,
                        "thumb": thumb_url,
                        "duration": dur_el.get_text(strip=True) if dur_el else "",
                    }
                )

            # dedupe media urls (too_big без url — по tg_ref)
            seen = set()
            uniq = []
            for m in media:
                u = m.get("url") or ""
                key = u or f"tg_ref:{(m.get('tg_ref') or '')}"
                if key and key not in seen:
                    seen.add(key)
                    uniq.append(m)

            meta = _parse_message_meta(msg)
            if meta:
                uniq.append(meta)

            # пустые посты (часто TG Ads) — добираем текст из og:description
            if not text.strip() and not any(
                (m.get("type") or "") in {"image", "video"} for m in uniq
            ):
                text = _enrich_empty_post_text(client, data_post, text)

            posts.append(
                ParsedPost(
                    external_id=external_id,
                    text=text,
                    media=uniq,
                    source_url=f"https://t.me/{data_post}",
                    title=title,
                )
            )

    def _num(p: ParsedPost) -> int:
        try:
            return int(p.external_id.rsplit("/", 1)[-1])
        except ValueError:
            return 0

    # стабильно: по номеру поста по возрастанию
    posts.sort(key=_num)

    if since_id:
        try:
            since_num = int(str(since_id).rsplit("/", 1)[-1].split(":")[-1])
        except ValueError:
            since_num = 0
        # только строго более новые, чем watermark
        posts = [p for p in posts if _num(p) > since_num]

    return title, posts


def detect_kind(url: str) -> str:
    u = url.strip().lower()
    host = urlparse(u).netloc.removeprefix("www.")
    if "t.me" in host or "telegram.me" in host or u.startswith("@"):
        return "telegram"
    if "vk.com" in host or "vk.ru" in host:
        return "vk"
    if host in {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"}:
        return "x"
    if u.endswith(".xml") or "rss" in u or "feed" in u:
        return "rss"
    # heuristic: try telegram @handle
    if re.fullmatch(r"@?[A-Za-z0-9_]{4,}", url.strip()):
        return "telegram"
    return "rss"
