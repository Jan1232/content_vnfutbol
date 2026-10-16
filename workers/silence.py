"""Алерты в ЛС, если в канале публикации нет sent-постов дольше N часов."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from app.config import get_settings
from app.db import db, get_meta, set_meta
from app.tg_bot_api import TgBotApiError, TgBotClient

_ALERT_META_PREFIX = "silence_alert:"
_PLATFORM_RU = {
    "max": "MAX",
    "telegram": "Telegram",
    "vk": "ВКонтакте",
}


def list_silent_channels(
    now: float | None = None,
    *,
    hours: float | None = None,
) -> list[dict]:
    settings = get_settings()
    now = time.time() if now is None else now
    threshold = now - float(hours if hours is not None else settings.silence_alert_hours) * 3600
    with db() as conn:
        rows = list(
            conn.execute(
                """
                SELECT
                    c.chat_id,
                    c.title,
                    c.link,
                    COALESCE(c.platform, 'max') AS platform,
                    MIN(s.created_at) AS sources_since,
                    COUNT(s.id) AS sources_count,
                    MAX(c.last_published_at) AS last_published_at,
                    MAX(
                        CASE
                            WHEN p.publish_status = 'sent'
                            THEN COALESCE(p.sent_at, p.created_at)
                        END
                    ) AS last_sent
                FROM channels c
                JOIN sources s ON s.chat_id = c.chat_id
                LEFT JOIN posts p ON p.source_id = s.id
                WHERE c.status = 'active'
                  AND COALESCE(c.hidden, 0) = 0
                GROUP BY c.chat_id
                """
            )
        )
    silent: list[dict] = []
    for row in rows:
        last = row["last_sent"] or row["last_published_at"]
        since = row["sources_since"]
        if last is None:
            if since is None or float(since) > threshold:
                continue
            quiet_from = float(since)
        else:
            last_f = float(last)
            if last_f > threshold:
                continue
            quiet_from = last_f
        silent.append(
            {
                "chat_id": int(row["chat_id"]),
                "title": row["title"] or str(row["chat_id"]),
                "link": row["link"] or "",
                "platform": (row["platform"] or "max").strip().lower(),
                "sources_count": int(row["sources_count"] or 0),
                "last_sent": float(last) if last is not None else None,
                "quiet_hours": (now - quiet_from) / 3600,
            }
        )
    silent.sort(key=lambda x: -x["quiet_hours"])
    return silent


def _due_alerts(channels: list[dict], now: float, cooldown_sec: float) -> list[dict]:
    due = []
    with db() as conn:
        for ch in channels:
            raw = get_meta(conn, f"{_ALERT_META_PREFIX}{ch['chat_id']}", "")
            try:
                prev = float(raw) if raw else 0.0
            except ValueError:
                prev = 0.0
            if prev and now - prev < cooldown_sec:
                continue
            due.append(ch)
    return due


def _mark_alerted(chat_ids: list[int], now: float) -> None:
    with db() as conn:
        for chat_id in chat_ids:
            set_meta(conn, f"{_ALERT_META_PREFIX}{chat_id}", str(now))


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "никогда"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%d.%m %H:%M")


def format_silence_message(channels: list[dict], hours: float) -> str:
    lines = [
        f"Тишина больше {hours:g} ч — нет публикаций:",
        "",
    ]
    for ch in channels:
        plat = _PLATFORM_RU.get(ch["platform"], ch["platform"])
        title = ch["title"]
        link = ch["link"]
        name = f'<a href="{link}">{title}</a>' if link else title
        qh = ch["quiet_hours"]
        qh_s = f"{qh:.0f} ч" if qh >= 10 else f"{qh:.1f} ч"
        lines.append(f"• {name} ({plat})")
        lines.append(f"  молчит {qh_s}, последний пост: {_fmt_ts(ch['last_sent'])}")
    return "\n".join(lines).strip()


def check_channel_silence(*, force: bool = False) -> int:
    """Проверить каналы и написать в ЛС. Возвращает число каналов в отправке."""
    settings = get_settings()
    chat_id = int(settings.owner_chat_id or 0)
    tokens = settings.telegram_bot_tokens()
    if not chat_id or not tokens:
        print("[silence] skip: нет OWNER_CHAT_ID или TG_BOT_TOKENS", flush=True)
        return 0
    now = time.time()
    hours = float(settings.silence_alert_hours)
    silent = list_silent_channels(now, hours=hours)
    cooldown = hours * 3600
    due = silent if force else _due_alerts(silent, now, cooldown)
    if not due:
        return 0
    text = format_silence_message(due, hours)
    try:
        with TgBotClient(tokens[0]) as bot:
            bot.send_message(chat_id, text, parse_mode="HTML")
    except TgBotApiError as e:
        print(f"[silence] tg fail: {e}", flush=True)
        return 0
    _mark_alerted([int(c["chat_id"]) for c in due], now)
    print(f"[silence] alerted {len(due)} channels", flush=True)
    return len(due)
