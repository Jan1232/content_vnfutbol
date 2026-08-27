"""News feed registry and RSS/API parsers → NewsItem."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import struct_time
from typing import Any
from urllib.parse import urlparse

import feedparser

from app.http_util import http_client
from editorial.catalogs import canonical_team, detect_competition
from editorial.channel_config import EditorialFeed
from editorial.models import NewsItem

DEFAULT_FEEDS: tuple[EditorialFeed, ...] = (
    EditorialFeed("championat_football", "rss", "https://www.championat.com/rss/news/football/"),
    EditorialFeed("sportsru_football", "rss", "https://www.sports.ru/rss/rubric.xml?id=208"),
    EditorialFeed("bbc_football", "rss", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    EditorialFeed("guardian_football", "rss", "https://www.theguardian.com/football/rss"),
    EditorialFeed("espn_soccer", "rss", "https://www.espn.com/espn/rss/soccer/news"),
)

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    t = _HTML_TAG.sub(" ", text or "")
    return _WS.sub(" ", t).strip()


def _dt_from_entry(entry: Any) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if isinstance(parsed, struct_time):
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    for key in ("published", "updated"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(str(raw))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return datetime.now(timezone.utc)


def _guess_lang(text: str) -> str:
    cyr = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    return "ru" if cyr >= 8 else "en"


def _stable_id(source: str, eid: str) -> str:
    return f"{source}:{eid}"


def _extract_entities(title: str, body: str) -> dict[str, Any]:
    from editorial.topic_gate import extract_entities

    return extract_entities(f"{title}\n{body}")


def parse_rss_feed(feed: EditorialFeed) -> list[NewsItem]:
    url = (feed.url or feed.endpoint or "").strip()
    if not url:
        return []
    with http_client() as client:
        r = client.get(url)
        r.raise_for_status()
        content = r.content
    parsed = feedparser.parse(content)
    items: list[NewsItem] = []
    for entry in parsed.entries or []:
        eid = str(entry.get("id") or entry.get("link") or entry.get("title") or "").strip()
        if not eid:
            continue
        title = _strip_html(str(entry.get("title") or ""))
        body = _strip_html(str(entry.get("summary") or entry.get("description") or ""))
        link = str(entry.get("link") or url)
        text = f"{title}\n{body}"
        published = _dt_from_entry(entry)
        entities = _extract_entities(title, body)
        items.append(
            NewsItem(
                external_id=_stable_id(feed.name, eid),
                source=feed.name,
                url=link,
                title=title,
                body=body,
                lang=_guess_lang(text),
                published_at=published,
                entities=entities,
                raw={"id": eid, "link": link},
                competition=str(entities.get("competition") or detect_competition(text)),
            )
        )
    return items


def parse_telegram_meme_feed(feed: EditorialFeed) -> list[NewsItem]:
    """TG-мем-источник: медиа + только lifestyle (трансферы/матчи/составы режем по логам модерации)."""
    from app.config import get_settings
    from parsers.telegram import parse_telegram
    from editorial.topic_gate import MEME_HARD_EVENT_TYPES, classify_meme_event

    handle = (feed.handle or "").strip().lstrip("@")
    if not handle:
        return []
    settings = get_settings()
    if not bool(getattr(settings, "meme_source_enabled", True)):
        return []
    url = f"https://t.me/s/{handle}"
    _title, posts = parse_telegram(url)
    take = {str(x).lower() for x in (feed.take_only or ("video", "meme_image"))}
    out: list[NewsItem] = []

    for post in posts[-40:]:
        media = post.media or []
        has_video = any((m.get("type") or "") == "video" for m in media)
        has_image = any((m.get("type") or "") == "image" and m.get("url") for m in media)
        if "video" in take and has_video:
            media_type = "video"
            post_kind = "video"
        elif "meme_image" in take and has_image and not has_video:
            media_type = "image"
            post_kind = "meme"
        else:
            continue
        title = (post.text or post.title or feed.name or "meme").strip()[:200] or "meme"
        body = (post.text or "").strip()
        # по логам: lifestyle→transfer/match/lineup → reject; в ленту не тащим
        classified = classify_meme_event(f"{title}\n{body}")
        if classified in MEME_HARD_EVENT_TYPES:
            continue
        entities = _extract_entities(title, body)
        entities["meme_source"] = feed.name
        entities["wrap_template"] = bool(getattr(feed, "wrap_template", False))
        entities["meme_text_class"] = classified
        item = NewsItem(
            external_id=_stable_id(feed.name, post.external_id),
            source=feed.name,
            url=post.source_url or url,
            title=title,
            body=body,
            lang="ru",
            published_at=datetime.now(timezone.utc),
            entities=entities,
            raw={
                "media": media,
                "post_kind": post_kind,
                "media_type": media_type,
                "wrap_template": bool(getattr(feed, "wrap_template", False)),
            },
            event_type="lifestyle",
            competition="",
        )
        out.append(item)
    return out


# Точка расширения: позже vk / instagram_export / rss_meme без переписывания fetch_feed.
MEME_SOURCE_PARSERS: dict[str, Any] = {
    "telegram": parse_telegram_meme_feed,
}


def fetch_feed(feed: EditorialFeed) -> list[NewsItem]:
    kind = (feed.kind or "rss").lower()
    parser = MEME_SOURCE_PARSERS.get(kind)
    if parser is not None:
        return parser(feed)
    if kind in {"rss", "atom"}:
        return parse_rss_feed(feed)
    if kind == "api":
        return parse_api_feed(feed)
    if kind in {"yt_bot", "yt-bot", "yt_topics"}:
        return parse_yt_bot_feed(feed)
    print(f"[editorial] unknown feed kind={kind} name={feed.name}", flush=True)
    return []


def parse_yt_bot_feed(feed: EditorialFeed) -> list[NewsItem]:
    """Темы из yt-bot (JSON inbox после score, порог min_score уже применён там)."""
    from pathlib import Path
    import json

    path = Path(
        (feed.endpoint or feed.url or "").strip()
        or "/var/max-repost/data/editorial/inbox/yt_bot_topics.json"
    )
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[editorial] yt_bot feed read fail: {e}", flush=True)
        return []
    rows = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []

    items: list[NewsItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        body = str(row.get("body") or row.get("summary") or "").strip()
        urls = row.get("urls") if isinstance(row.get("urls"), list) else []
        url = str(row.get("url") or (urls[0] if urls else "") or "").strip()
        tid = row.get("id")
        if tid is None:
            continue
        eid = f"yt_bot:topic:{tid}"
        published_raw = row.get("exported_at") or row.get("published_at") or ""
        try:
            published = datetime.fromisoformat(str(published_raw).replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except Exception:
            published = datetime.now(timezone.utc)
        entities = _extract_entities(title, body)
        entities["yt_bot_topic_id"] = int(tid) if str(tid).isdigit() else tid
        entities["yt_bot_score"] = row.get("total_score")
        from editorial.topic_gate import classify_event_rules

        guessed = classify_event_rules(f"{title}\n{body}")
        event_type = guessed if guessed not in {"", "other"} else "other"
        items.append(
            NewsItem(
                external_id=_stable_id(feed.name, eid),
                source=feed.name,
                url=url,
                title=title or f"Тема yt-bot #{tid}",
                body=body,
                lang="ru",
                published_at=published,
                entities=entities,
                raw={"yt_bot": row},
                event_type=event_type,
                competition=str(entities.get("competition") or detect_competition(f"{title}\n{body}")),
            )
        )
    print(f"[editorial] yt_bot feed={feed.name} items={len(items)} path={path}", flush=True)
    return items


def parse_api_feed(feed: EditorialFeed) -> list[NewsItem]:
    """Generic JSON list API: [{id,title,url,body,published_at}]."""
    endpoint = (feed.endpoint or feed.url or "").strip()
    if not endpoint:
        return []
    with http_client() as client:
        r = client.get(endpoint)
        r.raise_for_status()
        data = r.json()
    rows = data if isinstance(data, list) else (data.get("items") or data.get("news") or [])
    items: list[NewsItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        body = str(row.get("body") or row.get("text") or row.get("summary") or "")
        url = str(row.get("url") or row.get("link") or "")
        eid = str(row.get("id") or url or title)
        if not eid:
            eid = hashlib.sha1(f"{title}|{url}".encode()).hexdigest()[:16]
        published_raw = row.get("published_at") or row.get("date") or ""
        try:
            published = datetime.fromisoformat(str(published_raw).replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except Exception:
            published = datetime.now(timezone.utc)
        entities = _extract_entities(title, body)
        items.append(
            NewsItem(
                external_id=_stable_id(feed.name, eid),
                source=feed.name,
                url=url,
                title=title,
                body=body,
                lang=_guess_lang(f"{title} {body}"),
                published_at=published.astimezone(timezone.utc),
                entities=entities,
                raw=row,
                competition=str(entities.get("competition") or detect_competition(f"{title} {body}")),
            )
        )
    return items


def domain_of(url: str) -> str:
    host = (urlparse(url or "").netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host
