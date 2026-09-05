"""Извлечение сухого факта + event + image_query (Luna)."""

from __future__ import annotations

import json

from src.config import ARCHETYPES, get_openai_client
from src.ingest.aliases import normalize_event
from src.ingest.sources import EXTRACTOR_MODEL

EVENT_KINDS = ["goal", "final_result", "transfer", "transfer_cancel", "quote", "other"]

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_news": {"type": "boolean"},
        "is_garbage": {"type": "boolean"},
        "fact": {"type": "string"},
        "archetype": {
            "type": "string",
            "enum": list(ARCHETYPES),
        },
        "veracity": {
            "type": "string",
            "enum": ["verified", "rumored", "speculation"],
        },
        "is_sensation": {"type": "boolean"},
        "source_attribution": {"type": ["string", "null"]},
        "skip_reason": {"type": ["string", "null"]},
        "image_query": {"type": ["string", "null"]},
        "event": {
            "type": "object",
            "properties": {
                "teams": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "player": {"type": ["string", "null"]},
                "to_club": {"type": ["string", "null"]},
                "score": {"type": ["string", "null"]},
                "minute": {"type": ["integer", "null"]},
                "event_kind": {"type": "string", "enum": EVENT_KINDS},
            },
            "required": [
                "teams",
                "player",
                "to_club",
                "score",
                "minute",
                "event_kind",
            ],
            "additionalProperties": False,
        },
    },
    "required": [
        "is_news",
        "is_garbage",
        "fact",
        "archetype",
        "veracity",
        "is_sensation",
        "source_attribution",
        "skip_reason",
        "image_query",
        "event",
    ],
    "additionalProperties": False,
}

_SYSTEM = """Ты извлекаешь СУХОЙ факт из футбольного Telegram-поста.

Правила:
- КОРОЧЕ ИЛИ РАВНО источнику. Факт НЕ должен быть длиннее исходного поста.
  Если удлиняешь — ты делаешь неправильно.
- НЕ пересказывай («в посте сообщается, что...», «перечислены утверждения о...»,
  «в посте говорится») — это запрещено. Давай сам факт, без обёртки-пересказа.
- НЕ канцелярит. «забежал в штрафную» остаётся «забежал в штрафную», не
  «забежал в штрафную площадь». Сохраняй разговорную футбольную формулировку.
- СПИСКИ сохраняй списком. Если в посте перечень (награды, встречи, составы) —
  выдавай перечнем в fact, НЕ разворачивай в прозу.
- Убирай ТОЛЬКО стиль/эмоции/эмодзи источника. Факты, имена, цифры — переноси
  как есть, ничего не добавляя и не додумывая.
- Пустой/бессмысленный пост (только эмодзи, стикер, реклама, конкурс, ивент без
  футбольной новости) → is_garbage=true, fact="", is_news=false. Не выдумывай факт.

Архетипы: transfer, transfer_cancel, news_opinion, provocation, result, schedule,
lineup, goal_live, quote_hypocrisy, quote_scandal, achievement, humor_list,
injury_list, meme, video.

veracity:
- verified — официально / HERE WE GO / свершившийся факт без маркеров слуха
- rumored — «сообщают», «по информации», Романо/Marca как слух, «близок к»
- speculation — «может», «слухи», гипотезы

is_sensation — громкая новость.
source_attribution — Романо, Marca… или null.
is_news=false если реклама, приветствие, пустой мем без факта, не футбол.
meme — юмористическая картинка/мем; video — пост где главное видео.

image_query — короткий поисковый запрос картинки по СУТИ новости (2–4 слова).
Если есть игрок/клуб — их; если событие без персоны — суть темы
(напр. «Барселона Камп Ноу»). Для meme/schedule можно null.

Поле event — сущности для дедупа (нормализуй имена клубов/игроков).
event_kind: goal | final_result | transfer | transfer_cancel | quote | other

Примеры (учись на них):

ПЛОХО (раздул): raw «Бартра забежал в штрафную, но VAR не увидел нарушения»
  → «Марк Бартра забежал в штрафную площадь; VAR не зафиксировал нарушение...»
ХОРОШО: → fact «Бартра забежал в штрафную, VAR не увидел нарушения», is_garbage=false

ПЛОХО (проза вместо списка): raw со списком наград
  → «В посте перечислены утверждения о лучших игроках...»
ХОРОШО (список сохранён):
  → fact «Игрок сезона в лиге/года в Германии/сезона в Баварии — Олисе; лучший
     игрок ЛЧ — Хвича; лучший бомбардир ЛЧ и ЧМ — Мбаппе; ...»

ПЛОХО (взял мусор): raw «SECRET EVENT: Глава II... приглашения в Москве»
  → извлёк как новость
ХОРОШО: → is_garbage=true, is_news=false, fact=""
"""


def extract_system_prompt() -> str:
    """Системный промпт экстрактора (для тестов и вызовов)."""
    return _SYSTEM


def extract_fact(source_text: str, source_username: str) -> dict:
    client = get_openai_client()
    user = (
        f"Источник канала: @{source_username}\n\n"
        f"Текст поста:\n{source_text}\n\n"
        "Верни структурированное извлечение с event, image_query и is_garbage."
    )
    response = client.responses.create(
        model=EXTRACTOR_MODEL,
        instructions=_SYSTEM,
        input=user,
        reasoning={"effort": "none"},
        temperature=0.1,
        text={
            "format": {
                "type": "json_schema",
                "name": "fact_extract",
                "strict": True,
                "schema": EXTRACT_SCHEMA,
            }
        },
    )
    raw = getattr(response, "output_text", None) or ""
    if not raw:
        for item in response.output:
            if item.type == "message":
                for content in item.content:
                    if content.type == "output_text":
                        raw = content.text
                        break
    data = json.loads(raw)
    data["is_garbage"] = bool(data.get("is_garbage"))
    if data["is_garbage"]:
        data["is_news"] = False
        data["fact"] = (data.get("fact") or "").strip()
    elif not data.get("is_news"):
        data["fact"] = (data.get("fact") or "").strip()
    data["event"] = normalize_event(data.get("event"))
    iq = data.get("image_query")
    data["image_query"] = (iq or "").strip() or None
    return data
