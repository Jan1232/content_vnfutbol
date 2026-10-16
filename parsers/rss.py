from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import feedparser

from app.http_util import http_client
from parsers.telegram import ParsedPost


def parse_rss(url: str, since_id: str = "") -> tuple[str, list[ParsedPost]]:
    url = url.strip()
    with http_client() as client:
        r = client.get(url)
        r.raise_for_status()
        content = r.content

    feed = feedparser.parse(content)
    title = feed.feed.get("title") or urlparse(url).netloc or "RSS"

    posts: list[ParsedPost] = []
    entries = list(reversed(feed.entries))  # старые → новые
    seen_since = not since_id

    for entry in entries:
        eid = entry.get("id") or entry.get("link") or entry.get("title")
        if not eid:
            continue
        external_id = f"rss:{eid}"
        if since_id:
            if external_id == since_id:
                seen_since = True
                continue
            if not seen_since:
                continue

        text_parts = []
        if entry.get("title"):
            text_parts.append(entry.get("title"))
        summary = entry.get("summary") or entry.get("description") or ""
        # strip rough html
        if summary:
            import re
            summary = re.sub(r"<[^>]+>", " ", summary)
            summary = re.sub(r"\s+", " ", summary).strip()
            text_parts.append(summary)
        # RSS почти всегда содержит ссылку на статью — пользователь просил фильтровать ссылки.
        # Чтобы RSS имел смысл, ссылку НЕ вклеиваем в текст; оригинал храним в source_url.
        text = "\n\n".join([p for p in text_parts if p])

        media: list[dict[str, Any]] = []
        for link in entry.get("links") or []:
            href = link.get("href")
            if not href:
                continue
            ltype = str(link.get("type", "")).lower()
            if ltype.startswith("image/"):
                media.append({"type": "image", "url": href})
            elif ltype.startswith("video/") or href.lower().endswith((".mp4", ".webm", ".mov")):
                media.append({"type": "video", "url": href})
        for m in entry.get("media_content") or []:
            murl = m.get("url")
            if not murl:
                continue
            mtype = str(m.get("type") or m.get("medium") or "").lower()
            if mtype.startswith("video") or mtype == "video" or murl.lower().endswith((".mp4", ".webm", ".mov")):
                media.append({"type": "video", "url": murl})
            else:
                media.append({"type": "image", "url": murl})
        for m in entry.get("media_thumbnail") or []:
            if m.get("url"):
                media.append({"type": "image", "url": m["url"]})
        # картинки/видео из HTML summary
        summary_html = entry.get("summary") or entry.get("description") or ""
        if summary_html and "<" in summary_html:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(summary_html, "lxml")
            for img in soup.find_all("img"):
                src = img.get("src")
                if src:
                    media.append({"type": "image", "url": src})
            for src_el in soup.find_all("source"):
                src = src_el.get("src")
                if src and any(src.lower().endswith(ext) for ext in (".mp4", ".webm", ".mov")):
                    media.append({"type": "video", "url": src})

        posts.append(
            ParsedPost(
                external_id=external_id,
                text=text,
                media=media,
                source_url=entry.get("link") or "",
                title=title,
            )
        )

    # если since не найден — не заливаем всю ленту, берём 1 последний
    if since_id and not seen_since and posts:
        posts = posts[-1:]

    return title, posts
