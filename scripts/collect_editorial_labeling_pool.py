"""Сбор пула футбольных новостей за N дней для разметки take_to_prod."""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import struct_time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import feedparser

from app.config import ROOT
from app.http_util import http_client
from editorial.catalogs import detect_competition
from editorial.models import NewsItem
from editorial.pick import score_pool
from editorial.sources import _guess_lang, _strip_html
from editorial.topic_gate import classify_event_rules, cluster_id_for, extract_entities

WINDOW_DAYS = 14
SPORTSRU_MAX_PAGES = 40
CHAMPIONAT_MAX_PAGES = 250
OUT_DIR = ROOT / "data" / "editorial" / "labeling"

CHAMPIONAT_RSS = "https://www.championat.com/rss/news/football/"
SPORTSRU_LIST = "https://www.sports.ru/football/news/"
SPORTSRU_RSS = "https://www.sports.ru/rss/rubric.xml?id=208"
EN_FEEDS = (
    ("bbc_football", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("guardian_football", "https://www.theguardian.com/football/rss"),
    ("espn_soccer", "https://www.espn.com/espn/rss/soccer/news"),
)

MONTHS_RU = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SHORT_NEWS = re.compile(
    r'<div class="short-news">\s*<b>(?P<date>[^<]+)</b>(?P<body>.*?)(?=<div class="short-news">|\Z)',
    re.DOTALL,
)
_ITEM = re.compile(
    r'<span class="time">(?P<time>\d{1,2}:\d{2})</span>'
    r'(?P<meta>.*?)<a class="short-text" href="(?P<href>[^"]+)" title="(?P<lead>[^"]*)">(?P<title>.*?)</a>',
    re.DOTALL,
)


def _clean(text: str) -> str:
    t = _HTML_TAG.sub(" ", text or "")
    return _WS.sub(" ", t).replace("&quot;", '"').replace("&amp;", "&").replace("&#39;", "'").strip()


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


def event_type_guess(title: str, body: str) -> str:
    return classify_event_rules(f"{title}\n{body}")


def in_window(dt: datetime, start: datetime, end: datetime) -> bool:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return start <= dt <= end


def parse_ru_date(raw: str, time_s: str, now: datetime) -> datetime | None:
    s = (raw or "").strip().lower().replace("\xa0", " ")
    hh, mm = 0, 0
    if time_s and ":" in time_s:
        try:
            hh, mm = [int(x) for x in time_s.split(":")[:2]]
        except ValueError:
            pass
    if s.startswith("сегодня"):
        d = now.astimezone(timezone(timedelta(hours=3))).date()
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone(timedelta(hours=3))).astimezone(timezone.utc)
    if s.startswith("вчера"):
        d = (now.astimezone(timezone(timedelta(hours=3))) - timedelta(days=1)).date()
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone(timedelta(hours=3))).astimezone(timezone.utc)
    m = re.match(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", s)
    if not m:
        return None
    day = int(m.group(1))
    month = MONTHS_RU.get(m.group(2))
    if not month:
        return None
    year = int(m.group(3)) if m.group(3) else now.year
    try:
        return datetime(year, month, day, hh, mm, tzinfo=timezone(timedelta(hours=3))).astimezone(timezone.utc)
    except ValueError:
        return None


def next_cursor(parsed: Any) -> str | None:
    for link in parsed.feed.get("links") or []:
        if link.get("rel") != "next":
            continue
        href = str(link.get("href") or "")
        cur = (parse_qs(urlparse(href).query).get("cursor") or [None])[0]
        if cur:
            return cur
    return None


def collect_championat(client, start: datetime, end: datetime) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor: str | None = None
    stale = 0
    for page in range(1, CHAMPIONAT_MAX_PAGES + 1):
        url = CHAMPIONAT_RSS if not cursor else f"{CHAMPIONAT_RSS}?cursor={cursor}"
        r = client.get(url)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        entries = list(parsed.entries or [])
        if not entries:
            break
        page_in = 0
        oldest = None
        for entry in entries:
            link = str(entry.get("link") or "").strip()
            if not link or link in seen:
                continue
            published = _dt_from_entry(entry)
            oldest = published if oldest is None else min(oldest, published)
            if published > end:
                continue
            if published < start:
                continue
            seen.add(link)
            title = _strip_html(str(entry.get("title") or ""))
            body = _strip_html(str(entry.get("summary") or entry.get("description") or ""))
            items.append(
                {
                    "source": "championat_football",
                    "url": link,
                    "published_at": published,
                    "title": title,
                    "body": body,
                    "lang": _guess_lang(f"{title}\n{body}"),
                }
            )
            page_in += 1
        nxt = next_cursor(parsed)
        print(
            f"[championat] page={page} n={len(entries)} kept={page_in} oldest={oldest} next={bool(nxt)}",
            flush=True,
        )
        if oldest is not None and oldest < start:
            stale += 1
            if stale >= 2:
                break
        else:
            stale = 0
        if not nxt or nxt == cursor:
            break
        cursor = nxt
        time.sleep(0.15)
    return items


def collect_rss(client, source: str, url: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    r = client.get(url)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    out: list[dict[str, Any]] = []
    for entry in parsed.entries or []:
        link = str(entry.get("link") or "").strip()
        if not link:
            continue
        published = _dt_from_entry(entry)
        if not in_window(published, start, end):
            continue
        title = _strip_html(str(entry.get("title") or ""))
        body = _strip_html(str(entry.get("summary") or entry.get("description") or ""))
        out.append(
            {
                "source": source,
                "url": link,
                "published_at": published,
                "title": title,
                "body": body,
                "lang": _guess_lang(f"{title}\n{body}"),
            }
        )
    print(f"[{source}] rss kept={len(out)}/{len(parsed.entries or [])}", flush=True)
    return out


def collect_sportsru_html(client, start: datetime, end: datetime, now: datetime) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    stale_empty = 0
    for page in range(1, SPORTSRU_MAX_PAGES + 1):
        url = SPORTSRU_LIST if page == 1 else f"{SPORTSRU_LIST}?page={page}"
        html = client.get(url, headers={"Accept-Language": "ru"}).text
        blocks = list(_SHORT_NEWS.finditer(html))
        kept = 0
        oldest: datetime | None = None
        for block in blocks:
            date_raw = block.group("date")
            chunk = block.group("body")
            if "Пользовательская новость" in chunk:
                continue
            for m in _ITEM.finditer(chunk):
                href = m.group("href")
                if href.endswith("#comments"):
                    continue
                url_full = urljoin("https://www.sports.ru", href)
                if url_full in seen:
                    continue
                published = parse_ru_date(date_raw, m.group("time"), now)
                if published is None:
                    continue
                oldest = published if oldest is None else min(oldest, published)
                if not in_window(published, start, end):
                    continue
                seen.add(url_full)
                title = _clean(m.group("title"))
                body = _clean(m.group("lead"))
                items.append(
                    {
                        "source": "sportsru_football",
                        "url": url_full,
                        "published_at": published,
                        "title": title,
                        "body": body,
                        "lang": "ru",
                    }
                )
                kept += 1
        print(
            f"[sportsru] page={page} blocks={len(blocks)} kept={kept} oldest={oldest}",
            flush=True,
        )
        if kept == 0:
            stale_empty += 1
            if stale_empty >= 2 or (oldest is not None and oldest < start):
                break
        else:
            stale_empty = 0
        if oldest is not None and oldest < start and kept == 0:
            break
        time.sleep(0.15)
    return items


def _to_news_item(raw: dict[str, Any]) -> NewsItem:
    title = raw["title"]
    body = raw.get("body") or ""
    text = f"{title}\n{body}"
    entities = extract_entities(text)
    event_type = classify_event_rules(text)
    competition = detect_competition(text) or str(entities.get("competition") or "")
    item = NewsItem(
        external_id=raw["url"],
        source=raw["source"],
        url=raw["url"],
        title=title,
        body=body,
        lang=raw.get("lang") or "",
        published_at=raw["published_at"],
        entities=entities,
        event_type=event_type,
        competition=competition,
        is_national=bool(entities.get("is_national")),
    )
    item.cluster_id = cluster_id_for(item)
    return item


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=WINDOW_DAYS)
    args = parser.parse_args()
    days = int(args.days)

    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now
    raw: list[dict[str, Any]] = []
    with http_client() as client:
        raw.extend(collect_championat(client, start, end))
        raw.extend(collect_rss(client, "sportsru_football", SPORTSRU_RSS, start, end))
        raw.extend(collect_sportsru_html(client, start, end, now))
        for name, url in EN_FEEDS:
            try:
                raw.extend(collect_rss(client, name, url, start, end))
            except Exception as e:
                print(f"[{name}] fail: {e}", flush=True)

    uniq: list[dict[str, Any]] = []
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    for item in sorted(raw, key=lambda x: x["published_at"], reverse=True):
        url = re.sub(r"[?#].*$", "", item["url"]).rstrip("/")
        if url in seen_url:
            continue
        title_key = f"{item['title'].strip().lower()}|{item['published_at'].date().isoformat()}"
        if title_key in seen_title:
            continue
        seen_url.add(url)
        seen_title.add(title_key)
        uniq.append(item)

    news_items = [_to_news_item(x) for x in uniq]
    # канал идёт от старых к новым: дедуп кластера и cap human-factor
    chrono_idx = sorted(range(len(news_items)), key=lambda i: news_items[i].published_at)
    chrono_items = [news_items[i] for i in chrono_idx]
    chrono_verdicts = score_pool(chrono_items)
    verdict_by_pos = {chrono_idx[i]: chrono_verdicts[i] for i in range(len(chrono_idx))}

    rows = []
    for i, item in enumerate(news_items, start=1):
        v = verdict_by_pos[i - 1]
        rows.append(
            {
                "id": f"n{i:04d}",
                "source": item.source,
                "url": item.url,
                "published_at": item.published_at.isoformat(),
                "title": item.title,
                "body": item.body,
                "lang": item.lang,
                "event_type_guess": item.event_type,
                "model_take": v.take,
                "model_tag": v.tag,
                "model_note": v.reason,
                "model_by": v.by,
                "take_to_prod": None,
                "note": None,
            }
        )

    by_src: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_model: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    for row in rows:
        by_src[row["source"]] = by_src.get(row["source"], 0) + 1
        by_type[row["event_type_guess"]] = by_type.get(row["event_type_guess"], 0) + 1
        key = "true" if row["model_take"] else "false"
        by_model[key] = by_model.get(key, 0) + 1
        by_tag[row["model_tag"]] = by_tag.get(row["model_tag"], 0) + 1

    true_n = by_model.get("true", 0)
    payload = {
        "meta": {
            "window_from": start.isoformat(),
            "window_to": end.isoformat(),
            "collected_at": now.isoformat(),
            "days": days,
            "count": len(rows),
            "by_source": by_src,
            "by_event_type_guess": by_type,
            "model": {
                "round": 1,
                "policy": "editorial pick_offline after pool_10d labels",
                "true_count": true_n,
                "false_count": by_model.get("false", 0),
                "true_pct": round(100 * true_n / len(rows), 1) if rows else 0,
                "by_tag": by_tag,
            },
            "instruction": (
                "Это второй круг после обучения на вашей разметке pool_10d. "
                "Поля model_take / model_tag / model_note — прогноз модели (не трогай). "
                "Поставь take_to_prod: true/false как взял бы в прод сам. "
                "note необязателен. Можно вернуть этот JSON или список {id, take_to_prod, note}."
            ),
        },
        "items": rows,
    }
    out_path = OUT_DIR / f"pool_{days}d.json"
    labels_path = OUT_DIR / f"pool_{days}d_labels.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    compact = {
        "meta": payload["meta"],
        "items": [
            {
                "id": x["id"],
                "published_at": x["published_at"],
                "source": x["source"],
                "title": x["title"],
                "event_type_guess": x["event_type_guess"],
                "model_take": x["model_take"],
                "model_tag": x["model_tag"],
                "model_note": x["model_note"],
                "take_to_prod": x["take_to_prod"],
                "note": x["note"],
            }
            for x in rows
        ],
    }
    labels_path.write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"wrote {out_path} count={len(rows)} model_true={true_n} "
        f"sources={by_src} types={by_type} tags={by_tag}",
        flush=True,
    )
    print(f"wrote {labels_path}", flush=True)


if __name__ == "__main__":
    main()
