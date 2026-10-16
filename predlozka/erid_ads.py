"""Рекламные посты ya.cc/?erid: сбор за сегодня и дайджест в ЛС в 22:00 ЕКБ."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from predlozka.config import erid_digest_hm, erid_owner_id, erid_owner_ids, erid_tz
from predlozka.db import (
    get_meta,
    list_active_channels,
    list_erid_ads_for_day,
    prune_erid_ads,
    set_meta,
    upsert_erid_ad,
)
from predlozka.logic import (
    is_dialog_message,
    message_attachments,
    message_body,
    message_mid,
    message_text,
)

log = logging.getLogger("predlozka")

YA_ERID_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?ya\.cc/[^\s<>\"')\]]*[?&]erid=[A-Za-z0-9_\-]+"
)
SENT_META = "erid_digest_sent_day"
SCAN_SLEEP = 0.12
SCAN_PAGES = 8


def calendar_day(now: datetime | None = None, tz: ZoneInfo | None = None) -> str:
    zone = tz or erid_tz()
    dt = now or datetime.now(zone)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    else:
        dt = dt.astimezone(zone)
    return dt.strftime("%Y-%m-%d")


def day_start_ts(day: str, tz: ZoneInfo | None = None) -> float:
    zone = tz or erid_tz()
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone)
    return start.timestamp()


def _normalize_ya_url(raw: str) -> str:
    s = (raw or "").strip().rstrip(").,;]")
    if not s:
        return ""
    if not re.match(r"(?i)^https?://", s):
        s = "https://" + s
    s = re.sub(r"(?i)^https://www\.ya\.cc/", "https://ya.cc/", s)
    return s


def extract_erid_urls(*parts: Any) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def _walk(obj: Any) -> None:
        if isinstance(obj, str):
            for m in YA_ERID_RE.finditer(obj):
                url = _normalize_ya_url(m.group(0))
                key = url.casefold()
                if url and key not in seen:
                    seen.add(key)
                    found.append(url)
            return
        if isinstance(obj, dict):
            for val in obj.values():
                _walk(val)
            return
        if isinstance(obj, (list, tuple)):
            for val in obj:
                _walk(val)

    for part in parts:
        _walk(part)
    return found


def has_ad_footer(text: str) -> bool:
    low = re.sub(r"\s+", " ", (text or "").casefold())
    return bool(re.search(r"#\s*реклама", low)) and "о рекламодателе" in low


def is_erid_ad(
    text: str,
    extras: Any = None,
) -> list[str]:
    urls = extract_erid_urls(text, extras)
    if not urls or not has_ad_footer(text or ""):
        return []
    return urls


def ru_visit_text(n: int) -> str:
    n = max(0, int(n))
    mod10 = n % 10
    mod100 = n % 100
    if mod10 == 1 and mod100 != 11:
        word = "ссылке"
    else:
        word = "ссылкам"
    return (
        f"Перейти по {n} {word} и пробыть на каждой странице не менее 30 секунд"
    )


def views_of_message(message: dict[str, Any] | None) -> int:
    if not isinstance(message, dict):
        return 0
    stat = message.get("stat")
    if not isinstance(stat, dict):
        body = message.get("body")
        stat = body.get("stat") if isinstance(body, dict) else {}
    if not isinstance(stat, dict):
        return 0
    try:
        return max(0, int(stat.get("views") or 0))
    except (TypeError, ValueError):
        return 0


def format_channel_digest(title: str, urls: list[str], views: int = 0) -> str:
    name = (title or "канал").strip() or "канал"
    links = ", ".join(urls)
    n = len(urls)
    reach = f"{max(0, int(views)):,}".replace(",", " ")
    return (
        f'канал "{name}"\n'
        f"Ссылки:\n"
        f"{links}\n"
        f"Текст:\n"
        f"{ru_visit_text(n)}\n"
        f"Охваты: {reach}"
    )


def _message_timestamp(message: dict[str, Any] | None) -> float:
    if not isinstance(message, dict):
        return 0.0
    raw = message.get("timestamp") or 0
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if ts > 1e12:
        return ts / 1000.0
    return ts


def ingest_message(
    conn,
    message: dict[str, Any],
    *,
    chat_id: int | None = None,
    day: str | None = None,
) -> bool:
    if not isinstance(message, dict) or is_dialog_message(message):
        return False
    rec = message.get("recipient") if isinstance(message.get("recipient"), dict) else {}
    cid = chat_id
    if cid is None:
        try:
            cid = int(rec.get("chat_id") or 0)
        except (TypeError, ValueError):
            cid = 0
    if not cid:
        return False
    text = message_text(message)
    body = message_body(message)
    urls = is_erid_ad(
        text,
        [message_attachments(message), body.get("markup"), message],
    )
    if not urls:
        return False
    day_key = day or calendar_day()
    ts = _message_timestamp(message)
    start = day_start_ts(day_key)
    if ts and ts < start:
        return False
    mid = message_mid(message)
    return upsert_erid_ad(
        conn,
        day=day_key,
        chat_id=int(cid),
        mid=mid,
        url=urls[0],
        views=views_of_message(message),
    )


def fetch_today_posts(
    client, chat_id: int, *, since_ts: float, light: bool = False
) -> list[dict[str, Any]]:
    since_ms = int(since_ts * 1000)
    out: list[dict[str, Any]] = []
    cursor_from: int | None = None
    seen: set[str] = set()
    pages = 1 if light else SCAN_PAGES
    count = 40 if light else 100
    for _ in range(pages):
        try:
            batch = client.list_messages(int(chat_id), count=count, from_ms=cursor_from)
        except Exception:
            log.exception("erid list_messages fail chat=%s", chat_id)
            break
        if not batch:
            break
        oldest = None
        progressed = False
        stop = False
        for msg in batch:
            mid = message_mid(msg)
            ts_ms = int((_message_timestamp(msg) or 0) * 1000)
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            progressed = True
            if oldest is None or (ts_ms and ts_ms < oldest):
                oldest = ts_ms or oldest
            if ts_ms and ts_ms < since_ms:
                stop = True
                continue
            out.append(msg)
        if stop or not progressed or oldest is None:
            break
        if cursor_from is not None and oldest >= cursor_from:
            break
        cursor_from = oldest
        time.sleep(SCAN_SLEEP)
    return out


def scan_channel_today(
    client, conn, channel: dict[str, Any], *, day: str, light: bool = False
) -> int:
    chat_id = int(channel["chat_id"])
    added = 0
    for msg in fetch_today_posts(
        client, chat_id, since_ts=day_start_ts(day), light=light
    ):
        if ingest_message(conn, msg, chat_id=chat_id, day=day):
            added += 1
    return added


def scan_all_today(
    client, conn, *, day: str | None = None, light: bool = False
) -> dict[str, int]:
    day_key = day or calendar_day()
    pruned = prune_erid_ads(conn, day_key)
    channels = list_active_channels(conn)
    added = 0
    for ch in channels:
        added += scan_channel_today(client, conn, ch, day=day_key, light=light)
        time.sleep(SCAN_SLEEP)
    return {"channels": len(channels), "added": added, "pruned": pruned}


def ads_by_channel(
    conn, day: str
) -> list[tuple[dict[str, Any], list[str], int]]:
    channels = {int(c["chat_id"]): c for c in list_active_channels(conn)}
    grouped: dict[int, list[str]] = {}
    views: dict[int, int] = {}
    for row in list_erid_ads_for_day(conn, day):
        cid = int(row["chat_id"])
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        grouped.setdefault(cid, [])
        if url not in grouped[cid]:
            grouped[cid].append(url)
        try:
            views[cid] = views.get(cid, 0) + max(0, int(row.get("views") or 0))
        except (TypeError, ValueError):
            pass
    out: list[tuple[dict[str, Any], list[str], int]] = []
    for cid, urls in grouped.items():
        ch = channels.get(cid) or {"chat_id": cid, "title": f"канал {cid}"}
        if urls:
            out.append((ch, urls, views.get(cid, 0)))
    out.sort(key=lambda item: ((item[0].get("title") or "").casefold(), int(item[0]["chat_id"])))
    return out


def _sent_key(day: str, chat_id: int, user_id: int | None = None) -> str:
    if user_id is None:
        return f"erid_sent:{day}:{int(chat_id)}"
    return f"erid_sent:{day}:{int(chat_id)}:{int(user_id)}"


def send_daily_digest(
    client, conn, *, day: str | None = None, sender=None
) -> dict[str, int]:
    """client/sender: бот-отправитель ЛС (контент-менеджер, не предложка)."""
    day_key = day or calendar_day()
    owners = erid_owner_ids()
    stats = {"channels": 0, "sent": 0, "skip": 0, "fail": 0, "owners": len(owners)}
    if not owners:
        log.warning("erid digest: нет PREDLOZKA_ERID_OWNER / DIGEST_OWNER_USER_ID")
        return stats
    dm = sender or client
    items = ads_by_channel(conn, day_key)
    stats["channels"] = len(items)
    for channel, urls, reach in items:
        chat_id = int(channel["chat_id"])
        text = format_channel_digest(str(channel.get("title") or ""), urls, reach)
        for owner in owners:
            if get_meta(conn, _sent_key(day_key, chat_id, owner), "") == "1":
                stats["skip"] += 1
                continue
            if get_meta(conn, _sent_key(day_key, chat_id), "") == "1" and owner == erid_owner_id():
                # старый ключ без user_id: не дублировать первому получателю
                set_meta(conn, _sent_key(day_key, chat_id, owner), "1")
                stats["skip"] += 1
                continue
            try:
                dm.send_message(user_id=int(owner), text=text, format="html", notify=True)
            except Exception:
                stats["fail"] += 1
                log.exception("erid digest DM fail chat=%s user=%s", chat_id, owner)
                continue
            set_meta(conn, _sent_key(day_key, chat_id, owner), "1")
            stats["sent"] += 1
            time.sleep(0.2)
    if stats["fail"] == 0:
        set_meta(conn, SENT_META, day_key)
    log.info("erid digest day=%s owners=%s sender=content %s", day_key, owners, stats)
    return stats


def _local_now(now: datetime | None = None) -> datetime:
    zone = erid_tz()
    dt = now or datetime.now(zone)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def digest_target(now: datetime | None = None) -> datetime:
    dt = _local_now(now)
    hour, minute = erid_digest_hm()
    return dt.replace(hour=hour, minute=minute, second=0, microsecond=0)


def in_digest_window(now: datetime | None = None) -> bool:
    dt = _local_now(now)
    target = digest_target(dt)
    return target <= dt < target + timedelta(minutes=5)


def approaching_digest(now: datetime | None = None) -> bool:
    """Полный обход за 15 минут до 22:00 ЕКБ и во время окна отправки."""
    dt = _local_now(now)
    target = digest_target(dt)
    return target - timedelta(minutes=15) <= dt < target + timedelta(minutes=5)


def should_send_now(conn, now: datetime | None = None) -> bool:
    if not in_digest_window(now):
        return False
    day_key = calendar_day(now)
    return get_meta(conn, SENT_META, "") != day_key
