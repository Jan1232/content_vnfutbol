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


def _tg_post_num(external_id: str) -> int:
    try:
        return int(str(external_id).rsplit("/", 1)[-1].split(":")[-1])
    except ValueError:
        return 0


def _since_num(since_id: str) -> int:
    if not since_id:
        return 0
    return _tg_post_num(since_id)


def bootstrap_telegram_feed_cursor(feed: EditorialFeed) -> str:
    """Последний пост в TG-ленте без вызова soccerblog_gate (bootstrap)."""
    from parsers.telegram import parse_telegram

    handle = (feed.handle or "").strip().lstrip("@")
    if not handle:
        return ""
    _title, posts = parse_telegram(f"https://t.me/s/{handle}")
    if not posts:
        return ""
    latest = max(posts, key=lambda p: _tg_post_num(p.external_id))
    return _stable_id(feed.name, latest.external_id)


_SCORE_PAIR = re.compile(r"\b\d{1,2}\s*[:\-–]\s*\d{1,2}\b")


def _looks_roundup(text: str) -> bool:
    blob = (text or "").lower()
    if len(_SCORE_PAIR.findall(text or "")) >= 2:
        return True
    return any(x in blob for x in ("сводк", "итоги тура", "результаты тура", "roundup"))


def _looks_quote(text: str) -> bool:
    t = text or ""
    return t.count("«") >= 1 and t.count("»") >= 1


def _classify_tg_post(
    text: str,
    *,
    take: set[str],
    has_video: bool,
    has_image: bool,
    verdict: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """Возвращает (post_kind, media_type) или None — пропустить."""
    from editorial.soccerblog_gate import effective_gate_kind

    if verdict and verdict.get("gate_failed"):
        mt = "video" if has_video else ("image" if has_image else "")
        return "news", mt

    gk = effective_gate_kind(verdict)
    if gk == "reject":
        return None

    if has_video and "video" in take:
        return "video", "video"

    if has_image and not has_video:
        if gk == "as_is" and "meme_image" in take:
            return "meme", "image"
        if gk == "template" and "meme_image" in take:
            return "news", "image"

    body = (text or "").strip()
    if len(body) < 12:
        return None
    if _looks_roundup(body) and "roundup" in take:
        return "roundup", "image" if has_image else ""
    if _looks_quote(body) and "quote" in take:
        return "quote", "image" if has_image else ""
    if gk == "template" and "news" in take:
        return "news", "image" if has_image else ""
    if gk == "as_is" and "meme_image" in take:
        return "meme", "image" if has_image else ""
    if gk == "template" and "meme_image" in take:
        return "news", "image" if has_image else ""
    return None


def parse_telegram_feed(
    feed: EditorialFeed,
    *,
    since_id: str = "",
    cursor_out: list[str] | None = None,
    replay: bool = False,
) -> list[NewsItem]:
    """TG-донор: инкрементальный fetch, gate только на новых постах."""
    from app.config import get_settings
    from parsers.telegram import parse_telegram
    from editorial.gate_cache import get_gate_verdict, put_gate_verdict
    from editorial.soccerblog_gate import donor_gate
    from editorial.topic_gate import classify_event_rules
    from editorial.tg_donor import (
        get_last_seen_id,
        is_text_seen,
        mark_text_seen,
        set_last_seen_id,
        text_hash,
    )

    handle = (feed.handle or "").strip().lstrip("@")
    if not handle:
        return []
    settings = get_settings()
    if not bool(getattr(settings, "meme_source_enabled", True)):
        return []
    url = f"https://t.me/s/{handle}"
    _title, posts = parse_telegram(url)
    take = {str(x).lower() for x in (feed.take_only or ("video", "meme_image", "news"))}
    out: list[NewsItem] = []
    incremental = bool(getattr(settings, "tg_incremental", True)) and not replay
    since_num = _since_num(since_id)
    if incremental:
        since_num = max(since_num, get_last_seen_id(handle))
    latest_num = 0
    latest_ext = ""
    max_processed = since_num

    for post in posts:
        post_num = _tg_post_num(post.external_id)
        if post_num > latest_num:
            latest_num = post_num
            latest_ext = post.external_id

    if cursor_out is not None and latest_ext and not replay:
        cursor_out.append(_stable_id(feed.name, latest_ext))

    for post in posts:
        post_num = _tg_post_num(post.external_id)
        if incremental and since_num > 0 and post_num <= since_num:
            continue
        if incremental:
            max_processed = max(max_processed, post_num)
        title = (post.text or post.title or feed.name or "post").strip()[:200] or "post"
        body = (post.text or "").strip()
        digest = text_hash(f"{title}\n{body}")
        if incremental and is_text_seen(handle, digest):
            continue
        media = post.media or []
        has_video = any((m.get("type") or "") == "video" for m in media)
        has_image = any((m.get("type") or "") == "image" and m.get("url") for m in media)
        needs_gate = has_video or has_image
        verdict: dict[str, Any] | None = None
        if needs_gate:
            verdict = get_gate_verdict(feed.name, post.external_id)
            if verdict is None:
                media_type_hint = "video" if has_video else "image"
                verdict = donor_gate(f"{title}\n{body}", media, media_type=media_type_hint)
                put_gate_verdict(feed.name, post.external_id, verdict)
        elif body:
            verdict = {"kind": "template", "confidence": 0.5, "reason": "text-only", "gate_version": 2}
        classified = _classify_tg_post(
            body,
            take=take,
            has_video=has_video,
            has_image=has_image,
            verdict=verdict,
        )
        if incremental:
            mark_text_seen(handle, digest, post_id=post_num)
        if not classified:
            continue
        post_kind, media_type = classified
        published = post.published_at or datetime.now(timezone.utc)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        entities = _extract_entities(title, body)
        entities["tg_donor"] = feed.name
        entities["story_key"] = entities.get("story_key") or ""
        if verdict:
            entities["donor_gate"] = verdict
            entities["soccerblog_gate"] = verdict  # legacy key для notify/moderation
        if post_kind in {"meme", "video"}:
            entities["meme_source"] = feed.name
        if feed.rewrite_text:
            entities["rewrite_text"] = True
        if feed.preserve_quotes:
            entities["preserve_quotes"] = True
        prof_mode = (feed.profanity_mode or feed.profanity_gate or "").strip()
        if prof_mode:
            entities["profanity_mode"] = prof_mode
        entities["tg_post_type"] = post_kind
        if post_kind == "meme":
            event_type = "lifestyle"
            meme_flag = True
        elif post_kind == "roundup":
            event_type = "match_result"
            meme_flag = False
        elif post_kind == "quote":
            event_type = classify_event_rules(f"{title}\n{body}") or "official_statement"
            meme_flag = False
        else:
            event_type = classify_event_rules(f"{title}\n{body}") or "other"
            if event_type in {"lifestyle", "meme"} and post_kind == "news":
                event_type = "other"
            meme_flag = False
        if verdict and str(verdict.get("post_subtype") or "").strip().lower() == "match_result":
            event_type = "match_result"
            entities["post_subtype"] = "match_result"
        from editorial.story_throttle import story_key as _story_key

        entities["story_key"] = _story_key(
            NewsItem(
                external_id=_stable_id(feed.name, post.external_id),
                source=feed.name,
                url=post.source_url or url,
                title=title,
                body=body,
                lang="ru",
                published_at=published,
                entities=entities,
                event_type=event_type,
            )
        )
        item = NewsItem(
            external_id=_stable_id(feed.name, post.external_id),
            source=feed.name,
            url=post.source_url or url,
            title=title,
            body=body,
            lang=str((verdict or {}).get("text_lang") or "ru")[:8] or "ru",
            published_at=published,
            entities=entities,
            raw={
                "media": media,
                "post_kind": post_kind,
                "media_type": media_type,
                "wrap_template": bool(getattr(feed, "wrap_template", False)),
                "soccerblog_kind": str((verdict or {}).get("kind") or post_kind),
                "tg_post_type": post_kind,
            },
            event_type=event_type,
            competition=str(entities.get("competition") or detect_competition(f"{title}\n{body}")),
        )
        if meme_flag:
            entities["wrap_template"] = bool(getattr(feed, "wrap_template", False))
            entities["meme_text_class"] = "lifestyle"
        out.append(item)

    if incremental and max_processed > since_num:
        set_last_seen_id(handle, max_processed)
    return out


def parse_telegram_meme_feed(
    feed: EditorialFeed,
    *,
    since_id: str = "",
    cursor_out: list[str] | None = None,
) -> list[NewsItem]:
    """Обратная совместимость: делегирует в parse_telegram_feed."""
    return parse_telegram_feed(feed, since_id=since_id, cursor_out=cursor_out)


# Точка расширения: позже vk / instagram_export / rss_meme без переписывания fetch_feed.
MEME_SOURCE_PARSERS: dict[str, Any] = {
    "telegram": parse_telegram_feed,
}


def fetch_feed(
    feed: EditorialFeed,
    *,
    tg_since_id: str = "",
    tg_cursor_out: list[str] | None = None,
    replay: bool = False,
) -> list[NewsItem]:
    kind = (feed.kind or "rss").lower()
    parser = MEME_SOURCE_PARSERS.get(kind)
    if parser is not None:
        if kind == "telegram":
            return parser(feed, since_id=tg_since_id, cursor_out=tg_cursor_out, replay=replay)
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
