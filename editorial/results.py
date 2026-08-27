"""Результаты значимых матчей после FINISHED — вне каденса, один раз."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from editorial.channel_config import EditorialChannelConfig
from editorial.fixtures import (
    FINISHED_STATUSES,
    Match,
    competition_label_ru,
    get_provider,
    in_poll_window,
    significant_matches,
)
from editorial.models import utcnow_iso
from editorial.publisher import publish
from editorial.render import render_card
from editorial.store import (
    get_by_external,
    get_news,
    insert_news,
    mark_result_posted,
    result_already_posted,
    update_news,
    upsert_fixture,
)

MSK = ZoneInfo("Europe/Moscow")


def _brand(channel: EditorialChannelConfig) -> dict[str, str]:
    return {
        "name": channel.brand.name,
        "logo": channel.brand.logo,
        "accent_color": channel.brand.accent_color,
    }


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _caption(match: Match) -> str:
    sh = match.score_home if match.score_home is not None else "–"
    sa = match.score_away if match.score_away is not None else "–"
    label = competition_label_ru(match.competition)
    return f"⚽️ ФИНАЛ. {match.home_ru} {sh}:{sa} {match.away_ru} — {label}"


def _publish_result(
    channel: EditorialChannelConfig,
    client: Any,
    match: Match,
    *,
    published_at: str | None = None,
) -> dict[str, Any]:
    if result_already_posted(match.provider_id, channel.slug):
        return {"action": "already", "provider_id": match.provider_id}
    if match.score_home is None or match.score_away is None:
        return {"action": "wait_score", "provider_id": match.provider_id}

    ext_id = f"fixture:{match.provider_id}"
    existing = get_by_external(channel.slug, ext_id)
    if existing and existing.get("status") == "published":
        mark_result_posted(match.provider_id, channel.slug, str(existing.get("mid") or ""))
        return {"action": "already", "provider_id": match.provider_id, "news_id": existing["id"]}
    if existing and existing.get("status") == "awaiting_review":
        return {
            "action": "pending_review",
            "provider_id": match.provider_id,
            "news_id": existing["id"],
        }
    if existing and existing.get("status") == "rejected":
        return {"action": "rejected", "provider_id": match.provider_id, "news_id": existing["id"]}

    news_id = existing["id"] if existing else insert_news(
        {
            "channel_slug": channel.slug,
            "external_id": ext_id,
            "cluster_id": f"fixture:{match.provider_id}",
            "source": "fixtures",
            "url": "",
            "event_type": "fixture_result",
            "competition": match.competition,
            "is_national": 1 if match.is_national else 0,
            "is_priority": 1,
            "teams_json": f'["{match.home}","{match.away}"]',
            "title": f"{match.home_ru} {match.score_home}:{match.score_away} {match.away_ru}",
            "body": "",
            "lang": "ru",
            "source_published_at": utcnow_iso(),
            "entities_json": "{}",
            "status": "rendering",
        }
    )
    if not news_id:
        return {"action": "error", "reason": "insert_failed"}

    cover = render_card(
        "result",
        {
            "home": match.home_ru,
            "away": match.away_ru,
            "score_home": match.score_home,
            "score_away": match.score_away,
            "competition": competition_label_ru(match.competition),
            "stage": match.stage or "",
        },
        news_id=news_id,
        channel_brand=_brand(channel),
    )
    text = _caption(match)
    settings = get_settings()
    if getattr(settings, "results_llm_caption", False):
        try:
            from editorial.openai_client import get_client

            text = get_client().chat(
                settings.editorial_text_model,
                [
                    {
                        "role": "user",
                        "content": (
                            "Одна короткая строка на русском про финальный счёт, без выдумки. "
                            f"{match.home_ru} {match.score_home}:{match.score_away} {match.away_ru}, "
                            f"{competition_label_ru(match.competition)}"
                        ),
                    }
                ],
                max_tokens=80,
                fallback=settings.editorial_text_fallback,
                task="result-caption",
            ).strip() or text
        except Exception as e:
            print(f"[results] llm caption skip: {e}", flush=True)

    update_news(
        int(news_id),
        status="ready",
        cover_path=cover,
        post_text=text,
        caption=text,
        caption_line1=f"{match.score_home}:{match.score_away}",
        headline=f"{match.home_ru} {match.score_home}:{match.score_away} {match.away_ru}",
        is_priority=1,
        last_error="",
    )
    item = get_news(int(news_id))
    if not item:
        return {"action": "error", "reason": "missing"}

    from editorial.moderation import dispatch_review_immediate, moderation_enabled

    if moderation_enabled(channel):
        res = dispatch_review_immediate(channel, int(news_id))
        res["kind"] = "fixture_result"
        res["provider_id"] = match.provider_id
        return res

    res = publish(client, channel, item, published_at=published_at)
    mark_result_posted(match.provider_id, channel.slug, str(res.get("mid") or ""))
    res["kind"] = "fixture_result"
    res["provider_id"] = match.provider_id
    return res


def results_tick(
    channel: EditorialChannelConfig,
    client: Any,
    *,
    now: datetime | None = None,
    force: bool = False,
    provider: Any | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if not channel.results.enabled or not getattr(settings, "results_enabled", True):
        return {"action": "disabled"}
    cur = _aware(now)
    today = cur.astimezone(MSK).date()
    src = provider or get_provider()
    raw = src.matches_on(today)
    for m in raw:
        upsert_fixture(m)

    sig = significant_matches(
        raw,
        always_priority=channel.always_priority_teams,
        grands=channel.matchday.grands,
        all_cl_el=channel.matchday.all_cl_el,
        national_top100=channel.matchday.national_top100,
    )
    if channel.results.significant_only is False:
        sig = list(raw)

    pre = int(getattr(settings, "results_poll_window_pre_min", 5) or 5)
    post = int(getattr(settings, "results_poll_window_post_min", 30) or 30)
    posted: list[dict[str, Any]] = []
    skipped = 0
    windowed = 0
    for m in sig:
        already = result_already_posted(m.provider_id, channel.slug)
        if already:
            skipped += 1
            continue
        if not force and not in_poll_window(m, cur, pre_min=pre, post_min=post, posted=False):
            continue
        windowed += 1
        fresh = m
        if m.status not in FINISHED_STATUSES:
            got = src.match_status(m.provider_id)
            if got:
                fresh = got
                upsert_fixture(fresh)
        if fresh.status not in FINISHED_STATUSES:
            continue
        posted.append(_publish_result(channel, client, fresh))
    return {
        "action": "ok",
        "windowed": windowed,
        "posted": posted,
        "skipped": skipped,
        "live": getattr(settings, "fixtures_live", False),
    }
