"""Детерминированный фильтр мусора (реклама/ставки/не-новость)."""

from __future__ import annotations

import re

from src.ingest.sources import AD_LINK_MARKERS, AD_MARKERS, FOOTBALL_HINTS

_MORNING = re.compile(
    r"^(добр(ое|ый|ой)\s+(утро|день|вечер)|спокойной\s+ночи|всем\s+привет)[!?.\s]*$",
    re.IGNORECASE,
)

# «бонус» в ставках/рекламе, но не «€5 млн бонусы» в трансфере
_BET_BONUS = re.compile(
    r"(бонус\s+(новым|за\s+регистр|от\s+бук|игрокам)|получи\s+бонус|бонус\s+до\s+\d)",
    re.IGNORECASE,
)

# Конкурсы / закрытые ивенты / розыгрыши без футбольной новости
EVENT_PROMO_MARKERS = [
    "secret event",
    "закрытое мероприятие",
    "закрытое мероприят",
    "приглашени",
    "получат приглашения",
    "получите приглашен",
    "рейтинг",
    "открыли доступ к рейтингу",
    "розыгрыш",
    "giveaway",
    "конкурс",
    "голосован",
    "проголосуй",
    "проголосуйте",
    "опрос:",
    "опрос ",
    "кто победит в опросе",
    "закрытый ивент",
    "закрытый event",
    "private event",
    "мероприятие в москве",
    "мероприятие в спб",
    "вход только по приглашен",
    "ждут вас на мероприятии",
]


def check_garbage(
    text: str | None,
    *,
    is_forward: bool = False,
    has_media_only: bool = False,
) -> str | None:
    """Возвращает filter_reason или None если сообщение ок."""
    raw = (text or "").strip()
    if not raw:
        if has_media_only:
            return "media_without_text"
        return "empty"

    if _MORNING.match(raw):
        return "greeting_only"

    low = raw.lower()

    for m in AD_MARKERS:
        if m.lower() in low:
            return f"ad_marker:{m}"

    if _BET_BONUS.search(raw):
        return "ad_marker:бонус"

    for m in EVENT_PROMO_MARKERS:
        if m.lower() in low:
            # «рейтинг» может быть в футболе (рейтинг УЕФА) — требуем promo-контекст
            if m.lower() == "рейтинг":
                promo_ctx = (
                    "приглаш",
                    "мероприят",
                    "secret",
                    "конкурс",
                    "розыгрыш",
                    "голосов",
                    "участник",
                    "доступ к рейтинг",
                )
                if not any(c in low for c in promo_ctx):
                    continue
            return f"event_promo:{m}"

    # самореклама / ссылки t.me в рекламном контексте
    if any(x in low for x in AD_LINK_MARKERS):
        if any(
            w in low
            for w in (
                "подпис",
                "переход",
                "жми",
                "бонус",
                "промо",
                "канал",
                "ставк",
                "прогноз",
                "букмекер",
                "мероприят",
                "приглаш",
                "конкурс",
                "розыгрыш",
            )
        ):
            return "ad_link"

    if is_forward and any(
        w in low
        for w in (
            "ставк",
            "кэф",
            "коэфф",
            "промокод",
            "бонус",
            "букмекер",
            "мероприят",
            "приглаш",
            "конкурс",
            "розыгрыш",
        )
    ):
        return "forward_ad"

    # нет футбольной сути на коротком тексте
    if len(raw) < 40 and not any(h in low for h in FOOTBALL_HINTS):
        return "no_football_signal"

    return None
