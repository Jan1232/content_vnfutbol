from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import feedparser
from bs4 import BeautifulSoup

from app.http_util import http_client
from parsers.telegram import ParsedPost

NITTER_RSS_HOSTS = (
    "https://nitter.net",
)

VX_APIS = (
    "https://api.vxtwitter.com/{user}/status/{tid}",
    "https://api.fxtwitter.com/{user}/status/{tid}",
)


def extract_x_username(url: str) -> str | None:
    """https://x.com/premierleague → premierleague"""
    u = (url or "").strip()
    if not u:
        return None
    if u.startswith("@"):
        return u[1:].split("/")[0].strip() or None
    if not u.startswith("http"):
        u = "https://" + u
    p = urlparse(u)
    host = (p.netloc or "").lower().removeprefix("www.")
    if host not in {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"}:
        # already a nitter rss?
        if "/rss" in p.path and host:
            parts = [x for x in p.path.split("/") if x and x != "rss"]
            return parts[0] if parts else None
        return None
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        return None
    skip = {"home", "explore", "search", "i", "intent", "share", "settings"}
    if parts[0].lower() in skip:
        return None
    return parts[0]


def normalize_x_url(url: str) -> str:
    user = extract_x_username(url)
    if not user:
        raise ValueError(
            "Нужна ссылка на профиль X/Twitter, например https://x.com/premierleague"
        )
    return f"https://x.com/{user}"


def _strip_html_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    # убрать блоки «Video»-ссылки с превью
    for a in soup.find_all("a"):
        t = (a.get_text(" ", strip=True) or "").strip()
        tl = t.lower()
        href = (a.get("href") or "").lower()
        if tl == "video" or ("video" in tl and a.find("img")):
            a.decompose()
            continue
        # клубные / user-ссылки nitter/x — не тащим в пост
        if "nitter." in href or "x.com/" in href or "twitter.com/" in href:
            if t.startswith("@") or re.fullmatch(r"@?[A-Za-z0-9_]{2,}", t):
                a.decompose()
            else:
                a.replace_with(t)
            continue
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # хвост из одних @handles
    lines = [ln for ln in text.splitlines() if not re.fullmatch(r"@?[A-Za-z0-9_]{3,}", ln.strip())]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _media_from_vx(user: str, tid: str) -> list[dict[str, Any]]:
    media: list[dict[str, Any]] = []
    with http_client(timeout=30.0) as client:
        for tpl in VX_APIS:
            url = tpl.format(user=user, tid=tid)
            try:
                r = client.get(url)
                if r.status_code >= 400:
                    continue
                data = r.json()
            except Exception:
                continue

            # vxtwitter flat
            if isinstance(data, dict) and data.get("media_extended"):
                for m in data["media_extended"]:
                    mtype = (m.get("type") or "").lower()
                    murl = m.get("url") or ""
                    if not murl:
                        continue
                    if mtype == "video":
                        media.append({"type": "video", "url": murl})
                    elif mtype in {"image", "photo", "gif"}:
                        # gif часто отдаётся как mp4 — если .mp4, шлём как video
                        if murl.lower().split("?", 1)[0].endswith(".mp4"):
                            media.append({"type": "video", "url": murl})
                        else:
                            media.append({"type": "image", "url": murl})
                if media:
                    return media

            # fxtwitter nested
            tweet = data.get("tweet") if isinstance(data, dict) else None
            if isinstance(tweet, dict):
                block = tweet.get("media") or {}
                for m in block.get("all") or []:
                    mtype = (m.get("type") or "").lower()
                    murl = m.get("url") or m.get("video_url") or ""
                    if mtype == "video" or m.get("video_url"):
                        v = m.get("video_url") or murl
                        if v:
                            media.append({"type": "video", "url": v})
                    elif mtype in {"photo", "image", "gif"} and murl:
                        media.append({"type": "image", "url": murl})
                if media:
                    return media
    return media


def _media_from_summary_html(summary: str, nitter_host: str) -> list[dict[str, Any]]:
    """Фолбэк: картинки из HTML RSS (видео только как превью → image)."""
    media: list[dict[str, Any]] = []
    soup = BeautifulSoup(summary or "", "lxml")
    seen: set[str] = set()
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src:
            continue
        if src.startswith("/"):
            src = nitter_host.rstrip("/") + src
        if src in seen:
            continue
        seen.add(src)
        media.append({"type": "image", "url": src})
    return media


def parse_x(
    url: str, since_id: str = "", *, enrich_media: bool = True
) -> tuple[str, list[ParsedPost]]:
    """Лента профиля X: Nitter RSS + медиа через vxtwitter (image/video)."""
    user = extract_x_username(url)
    if not user:
        raise ValueError("Некорректная ссылка X/Twitter")

    last_err: Exception | None = None
    feed = None
    nitter_host = NITTER_RSS_HOSTS[0]
    for host in NITTER_RSS_HOSTS:
        feed_url = f"{host.rstrip('/')}/{user}/rss"
        try:
            with http_client(timeout=45.0) as client:
                r = client.get(feed_url)
                r.raise_for_status()
                content = r.content
            parsed = feedparser.parse(content)
            if not parsed.entries and "RSS reader not yet whitelist" in (parsed.feed.get("title") or ""):
                raise RuntimeError(f"Nitter RSS whitelist: {host}")
            if not parsed.entries and not parsed.feed.get("title"):
                raise RuntimeError(f"Пустой RSS: {feed_url}")
            feed = parsed
            nitter_host = host
            break
        except Exception as e:
            last_err = e
            continue
    if feed is None:
        raise RuntimeError(f"Не удалось получить ленту X @{user}: {last_err}")

    title = feed.feed.get("title") or f"@{user}"
    posts: list[ParsedPost] = []
    entries = list(reversed(feed.entries))  # старые → новые
    seen_since = not since_id

    for entry in entries:
        raw_id = str(entry.get("id") or "").strip()
        # nitter guid = numeric tweet id
        tid = re.search(r"(\d{8,})", raw_id) or re.search(
            r"status/(\d+)", entry.get("link") or ""
        )
        if not tid:
            continue
        tweet_id = tid.group(1)
        external_id = f"x:{user}/{tweet_id}"

        if since_id:
            if external_id == since_id:
                seen_since = True
                continue
            if not seen_since:
                continue

        summary = entry.get("summary") or entry.get("description") or ""
        text = _strip_html_text(summary)
        if not text and entry.get("title"):
            text = str(entry.get("title")).strip()

        uniq: list[dict[str, Any]] = []
        if enrich_media:
            media = _media_from_vx(user, tweet_id)
            if not media:
                media = _media_from_summary_html(summary, nitter_host)
            seen_u: set[str] = set()
            for m in media:
                u = (m.get("url") or "").strip()
                if not u or u in seen_u:
                    continue
                seen_u.add(u)
                uniq.append(m)
            # из X берём только посты с видео
            if not any((m.get("type") or "").lower() == "video" for m in uniq):
                continue
            # вложение — только видео (без превью-картинок рядом)
            uniq = [m for m in uniq if (m.get("type") or "").lower() == "video"]

        posts.append(
            ParsedPost(
                external_id=external_id,
                text=text,
                media=uniq,
                source_url=f"https://x.com/{user}/status/{tweet_id}",
                title=title,
            )
        )

    if since_id and not seen_since and posts:
        posts = posts[-1:]

    return title, posts
