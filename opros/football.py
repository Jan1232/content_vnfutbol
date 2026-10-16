"""Генерация футбольной пары A vs B (для двух каналов с разным текстом)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.config import get_settings
from opros.channels import get_channel
from src.config import get_openai_client

INSTRUCTIONS = """Ты — редактор футбольных versus-опросов для каналов в MAX.

Задача: придумать РОВНО ОДНУ пару для голосования «A против B».

Правила:
- Только футбол: клубы, игроки, тренеры, сборные, эпохи/поколения в рамках футбола.
- Обе стороны узнаваемы массовой аудитории (не ультра-нишевые).
- Пара должна быть спорной и интересной, не «очевидный разгром».
- Не политика, не оскорбления, без упоминания несовершеннолетних в негативном ключе.
- option_a и option_b — короткие русские названия (как пишут болельщики): «Реал Мадрид», «Барселона», «Месси», «Роналду».
- Не дублируй пары из списка «уже были» (в любом порядке).
- Не добавляй третью сторону, не пиши вопрос целиком — только JSON.

Вывод СТРОГО JSON одной строкой:
{"option_a":"...","option_b":"..."}
Без markdown и без пояснений."""


@dataclass(frozen=True)
class FootballPair:
    option_a: str
    option_b: str

    @property
    def question_or(self) -> str:
        return f"{self.option_a} или {self.option_b}?"

    @property
    def question_who(self) -> str:
        return f"Кто круче: {self.option_a} или {self.option_b}?"


def _parse_pair(raw: str) -> FootballPair | None:
    text = (raw or "").strip()
    if not text:
        return None
    # вытащить JSON-объект
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    a = str(data.get("option_a") or "").strip()
    b = str(data.get("option_b") or "").strip()
    if not a or not b or a.casefold() == b.casefold():
        return None
    return FootballPair(option_a=a, option_b=b)


def _build_input(channel_key: str, recent: list[tuple[str, str]]) -> str:
    ch = get_channel(channel_key)
    lines = [
        f"channel_key={channel_key}",
        f"channel_title={ch.title}",
        f"theme={ch.theme}",
        "",
        "Сгенерируй 1 новую уникальную футбольную пару option_a / option_b.",
    ]
    if recent:
        lines.append("")
        lines.append("Уже были (не повторяй в любом порядке):")
        for a, b in recent[:40]:
            lines.append(f"- {a} vs {b}")
    return "\n".join(lines)


def generate_football_pair(
    channel_key: str,
    recent: list[tuple[str, str]] | None = None,
) -> FootballPair:
    settings = get_settings()
    client = get_openai_client()
    model = (settings.opros_openai_model or "gpt-6-luna").strip()
    recent_list = list(recent or [])
    last_error: Exception | None = None

    for _ in range(2):
        try:
            response = client.responses.create(
                model=model,
                instructions=INSTRUCTIONS,
                input=_build_input(channel_key, recent_list),
                reasoning={"effort": "none"},
                temperature=0.95,
                text={"verbosity": "low"},
            )
        except TypeError:
            response = client.responses.create(
                model=model,
                instructions=INSTRUCTIONS,
                input=_build_input(channel_key, recent_list),
                temperature=0.95,
            )
        except Exception as e:
            last_error = e
            break

        raw = getattr(response, "output_text", "") or ""
        if not raw:
            chunks: list[str] = []
            for item in getattr(response, "output", None) or []:
                for part in getattr(item, "content", None) or []:
                    t = getattr(part, "text", None)
                    if t:
                        chunks.append(str(t))
            raw = "\n".join(chunks)

        pair = _parse_pair(raw)
        if not pair:
            last_error = RuntimeError(f"bad pair json: {raw[:200]!r}")
            continue

        # локальный антидубль по recent
        key = frozenset((pair.option_a.casefold(), pair.option_b.casefold()))
        if any(frozenset((a.casefold(), b.casefold())) == key for a, b in recent_list):
            recent_list = [(pair.option_a, pair.option_b)] + recent_list
            last_error = RuntimeError(f"duplicate pair: {pair.option_a} vs {pair.option_b}")
            continue
        return pair

    raise RuntimeError(f"football generate failed: {last_error}")
