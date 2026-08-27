"""Утренняя сетка «Матчи дня» — 09:00 МСК, вне каденса."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from editorial.channel_config import EditorialChannelConfig
from editorial.fixtures import (
    Match,
    competition_label_ru,
    get_provider,
    significant_matches,
    sort_matchday,
)
from editorial.models import utcnow_iso
from editorial.publisher import publish
from editorial.render import render_card
from editorial.store import (
    get_by_external,
    get_channel_state,
    get_news,
    insert_news,
    update_news,
    upsert_channel_state,
    upsert_fixture,
)

MSK = ZoneInfo("Europe/Moscow")


def _now_msk(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(MSK)
    if now.tzinfo is None:
        return now.replace(tzinfo=MSK)
    return now.astimezone(MSK)


def in_matchday_window(now: datetime | None = None) -> bool:
    settings = get_settings()
    cur = _now_msk(now)
    hour = int(getattr(settings, "matchday_hour_msk", 9) or 9)
    grace = int(getattr(settings, "matchday_grace_min", 15) or 15)
    if cur.hour != hour:
        return False
    return cur.minute < grace


def _brand(channel: EditorialChannelConfig) -> dict[str, str]:
    return {
        "name": channel.brand.name,
        "logo": channel.brand.logo,
        "accent_color": channel.brand.accent_color,
    }


def _more_suffix(n: int) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return "ей"
    last = n % 10
    if last == 1:
        return ""
    if 2 <= last <= 4:
        return "а"
    return "ей"


def build_groups(matches: list[Match], *, max_rows: int) -> tuple[list[dict[str, Any]], int]:
    extra = max(0, len(matches) - max_rows)
    shown = matches[:max_rows]
    groups: list[dict[str, Any]] = []
    current = ""
    bucket: dict[str, Any] | None = None
    for m in shown:
        if m.competition != current:
            bucket = {"code": m.competition, "label": competition_label_ru(m.competition), "rows": []}
            groups.append(bucket)
            current = m.competition
        assert bucket is not None
        bucket["rows"].append(
            {
                "time": m.kickoff_msk.strftime("%H:%M"),
                "home": m.home_ru,
                "away": m.away_ru,
            }
        )
    return groups, extra


def matchday_tick(
    channel: EditorialChannelConfig,
    client: Any,
    *,
    now: datetime | None = None,
    force: bool = False,
    provider: Any | None = None,
    published_at: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if not channel.matchday.enabled:
        return {"action": "disabled"}
    cur = _now_msk(now)
    today = cur.date().isoformat()
    state = get_channel_state(channel.slug)
    if not force and not in_matchday_window(cur):
        return {"action": "wait", "date": today}
    if (state.get("matchday_last_date") or "") == today:
        return {"action": "already", "date": today}

    src = provider or get_provider()
    raw = src.matches_on(cur.date())
    for m in raw:
        upsert_fixture(m)
    picked = sort_matchday(
        significant_matches(
            raw,
            always_priority=channel.matchday.national_always or channel.always_priority_teams,
            grands=channel.matchday.grands,
            all_cl_el=channel.matchday.all_cl_el,
            national_top100=channel.matchday.national_top100,
        ),
        channel.matchday.group_order,
    )
    if not picked:
        upsert_channel_state(channel.slug, matchday_last_date=today)
        return {"action": "skipped_no_matches", "date": today}

    max_rows = int(getattr(settings, "matchday_max_rows", 12) or 12)
    groups, extra = build_groups(picked, max_rows=max_rows)
    date_label = cur.strftime("%d.%m.%Y")
    ext_id = f"matchday:{today}"
    existing = get_by_external(channel.slug, ext_id)
    news_id = existing["id"] if existing else insert_news(
        {
            "channel_slug": channel.slug,
            "external_id": ext_id,
            "cluster_id": f"matchday:{channel.slug}:{today}",
            "source": "fixtures",
            "url": "",
            "event_type": "matchday",
            "competition": "",
            "is_national": 0,
            "is_priority": 1,
            "teams_json": "[]",
            "title": f"Матчи дня · {date_label}",
            "body": "",
            "lang": "ru",
            "source_published_at": utcnow_iso(),
            "entities_json": "{}",
            "status": "rendering",
        }
    )
    if not news_id:
        return {"action": "error", "reason": "insert_failed"}
    if existing and existing.get("status") == "published":
        upsert_channel_state(channel.slug, matchday_last_date=today)
        return {"action": "already", "date": today, "news_id": news_id}

    cover = render_card(
        "matchday",
        {
            "date_label": date_label,
            "groups": groups,
            "more": extra,
            "more_suffix": _more_suffix(extra) if extra else "",
        },
        news_id=news_id,
        channel_brand=_brand(channel),
    )
    caption = "⚽️ Главные матчи сегодня. Время московское."
    if channel.cta.url:
        extra_cta = (channel.cta.text or "Подписаться").strip()
        caption = f"{caption}\n{extra_cta}: {channel.cta.url}"
    update_news(
        int(news_id),
        status="ready",
        cover_path=cover,
        post_text=caption,
        caption=caption,
        caption_line1="Матчи дня",
        headline=f"Матчи дня · {date_label}",
        is_priority=1,
        last_error="",
    )
    item = get_news(int(news_id))
    if not item:
        return {"action": "error", "reason": "missing"}
    res = publish(client, channel, item, published_at=published_at)
    upsert_channel_state(channel.slug, matchday_last_date=today)
    res["kind"] = "matchday"
    res["rows"] = len(picked)
    return res
