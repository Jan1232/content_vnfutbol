from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.http_util import http_client

TG_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,})",
    re.I,
)
# JPG/PNG и т.п., отправленные в TG как document (не сжатое фото)
_IMAGE_DOC_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}
_TG_MSG_HREF_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)/(\d+)",
    re.I,
)


@dataclass
class ParsedPost:
    external_id: str
    text: str
    media: list[dict[str, Any]] = field(default_factory=list)
    source_url: str = ""
    title: str = ""
    published_at: datetime | None = None


def _parse_msg_time(msg) -> datetime | None:
    el = msg.select_one("time[datetime]") or msg.select_one(".tgme_widget_message_date time")
    if el is None:
        return None
    raw = (el.get("datetime") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def telegram_username_from_url(url: str) -> str | None:
    """Публичный @username канала из t.me / t.me/s / @name. Без joinchat и t.me/c/id."""
    raw = (url or "").strip()
    m = TG_RE.search(raw)
    if m:
        username = m.group(1)
    else:
        username = raw.lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{4,}", username or ""):
            return None
    if username.lower() in {"joinchat", "addstickers", "share", "proxy", "socks", "iv", "c"}:
        return None
    return username


def normalize_telegram_url(url: str) -> str | None:
    m = TG_RE.search((url or "").strip())
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
            continue
        href_user = ""
        hm = re.search(
            r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,})",
            href,
            re.I,
        )
        if hm:
            href_user = hm.group(1).casefold()
        label_user = label.lstrip("@＠").strip().casefold()
        # @channel / t.me/channel — не оставляем хэндл после «очистки» ссылки
        if label.startswith("@") or label.startswith("＠"):
            a.replace_with("")
            continue
        if href_user and label_user == href_user:
            a.replace_with("")
            continue
        if label:
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


def _tg_ref_from_href(href: str, fallback: str = "") -> str:
    hm = _TG_MSG_HREF_RE.search(href or "")
    if hm:
        return f"{hm.group(1)}/{hm.group(2)}"
    return fallback


def _bg_image_url(el) -> str:
    if el is None:
        return ""
    style = el.get("style") or ""
    m = re.search(r"url\('([^']+)'\)", style)
    return m.group(1) if m else ""


def _el_is_too_big(el) -> bool:
    classes = el.get("class") or []
    if "not_supported" in classes:
        return True
    lab = el.select_one(".message_media_not_supported_label")
    if not lab:
        return False
    text = lab.get_text(strip=True).lower()
    return "too big" in text or "слишком больш" in text


def collect_message_media(msg, data_post: str) -> list[dict[str, Any]]:
    """Все фото/видео поста, включая альбом (каждое /id?single отдельно)."""
    media: list[dict[str, Any]] = []
    grouped = msg.select(
        ".tgme_widget_message_grouped_layer a.tgme_widget_message_video_player, "
        ".tgme_widget_message_grouped_layer a.tgme_widget_message_photo_wrap"
    )
    players = grouped or msg.select(
        "a.tgme_widget_message_video_player, a.tgme_widget_message_photo_wrap"
    )
    for a in players:
        href = (a.get("href") or "").strip()
        tg_ref = _tg_ref_from_href(href, data_post)
        classes = a.get("class") or []
        is_photo = (
            "tgme_widget_message_photo_wrap" in classes
            or "js-message_photo" in classes
        )
        vid = a.select_one("video.tgme_widget_message_video, video.js-message_video, video")
        vsrc = ((vid.get("src") if vid else "") or "").strip()
        thumb = _bg_image_url(a.select_one(".tgme_widget_message_video_thumb")) or _bg_image_url(a)
        if is_photo and not vsrc:
            media.append(
                {
                    "type": "image",
                    "url": thumb,
                    "tg_ref": tg_ref,
                    "too_big": not bool(thumb),
                }
            )
            continue
        dur_el = a.select_one(
            ".message_video_duration, .tgme_widget_message_video_duration, "
            "time.message_video_duration"
        )
        media.append(
            {
                "type": "video",
                "url": vsrc,
                "tg_ref": tg_ref,
                "too_big": bool(_el_is_too_big(a) and not vsrc),
                "thumb": thumb,
                "duration": dur_el.get_text(strip=True) if dur_el else "",
            }
        )

    if not any((m.get("type") or "") in {"image", "video"} for m in media):
        for photo in msg.select(".tgme_widget_message_photo_wrap"):
            url = _bg_image_url(photo)
            if url:
                media.append({"type": "image", "url": url, "tg_ref": data_post})
        for vid in msg.select("video.tgme_widget_message_video, video.js-message_video"):
            vsrc = (vid.get("src") or "").strip()
            if vsrc:
                media.append({"type": "video", "url": vsrc, "tg_ref": data_post})
        too_big_label = msg.select_one(".message_media_not_supported_label")
        too_big_text = (too_big_label.get_text(strip=True) if too_big_label else "").lower()
        if too_big_label and (
            "too big" in too_big_text or "слишком больш" in too_big_text
        ):
            media.append(
                {
                    "type": "video",
                    "url": "",
                    "tg_ref": data_post,
                    "too_big": True,
                }
            )

    for doc in msg.select("a.tgme_widget_message_document_wrap"):
        href = (doc.get("href") or "").strip()
        title_el = doc.select_one(".tgme_widget_message_document_title")
        filename = (title_el.get_text(strip=True) if title_el else "") or ""
        ext = PurePosixPath(filename).suffix.lower()
        if filename and ext and ext not in _IMAGE_DOC_EXT:
            continue
        tg_ref = _tg_ref_from_href(href, data_post)
        if not tg_ref:
            continue
        media.append(
            {
                "type": "image",
                "url": "",
                "tg_ref": tg_ref,
                "too_big": True,
                "from_document": True,
                "filename": filename or f"{tg_ref.rsplit('/', 1)[-1]}.jpg",
            }
        )
    return media


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

            media = collect_message_media(msg, data_post)

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
                    published_at=_parse_msg_time(msg),
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
