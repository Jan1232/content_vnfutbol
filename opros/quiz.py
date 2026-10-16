"""Футбольные квизы: игрок по карьере / клуб по фактам."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from app.config import get_settings
from opros.channels import get_channel
from src.config import get_openai_client

QuizKind = Literal["player", "club"]

INSTRUCTIONS = """Ты — автор футбольных квизов для каналов в MAX.

Генерируешь РОВНО ОДИН квиз одного из двух типов — тип указан во входе (player|club).

Общие правила:
- Ответ должен быть узнаваем массовой аудитории, но не банальный на 100%.
- Не повторяй ответы из списка «уже были».
- Не пиши ответ в тексте загадки.
- Без политики, без оскорблений.
- Вывод СТРОГО JSON одной строкой, без markdown.

Тип player — угадай игрока по клубной карьере:
{
  "kind":"player",
  "answer":"Имя Фамилия",
  "career":[
    {"seasons":"2002/2003","club":"Барселона"},
    {"seasons":"2003/2012","club":"Реал Мадрид"}
  ]
}
Правила player:
- 3–6 строк карьеры (основные клубы; можно юниор/аренду если это ключ).
- seasons в формате YYYY/YYYY или YYYY/YYYY–YYYY/YYYY; для одного года — YYYY.
- club — коротко по-русски («Реал Мадрид», «Интер Милан», «ПСЖ»).
- Не подсказывай прозвище/прозвище-ответ в названиях клубов.
- Игрок реальный.

Тип club — угадай футбольный клуб:
{
  "kind":"club",
  "answer":"Название клуба",
  "country":"Португалия",
  "founded":1889,
  "ucl":"нет",
  "fact":"Короткий факт без имён игроков"
}
Правила club:
- country — страна клуба по-русски.
- founded — год основания числом.
- ucl — победы в Лиге чемпионов / Кубке чемпионов: «нет» или «x1»/«x2»/«x3»… (сколько раз клуб выигрывал).
- fact — 1 предложение: стадион, город, прозвище клуба, цвет формы, уникальная история клуба.
  ЗАПРЕЩЕНО упоминать любых игроков (топ/легенды/нынешних) по имени.
- Клуб реальный."""

QUIZ_FOOTER = (
    "❤️ - угадал\n"
    "🤔 - не знаю\n"
    "\n"
    "👇 Ответ в комментарии под постом!"
)


@dataclass(frozen=True)
class QuizItem:
    kind: QuizKind
    answer: str
    body: str  # текст загадки без футера

    @property
    def question(self) -> str:
        # краткий заголовок для логов/БД
        first = (self.body.splitlines() or [""])[0].strip()
        return first or self.answer

    @property
    def answer_key(self) -> str:
        return f"quiz:{(self.answer or '').strip().casefold()}"


def format_quiz_text(item: QuizItem) -> str:
    return f"{item.body.strip()}\n\n{QUIZ_FOOTER}".strip()


def _parse_player(data: dict[str, Any]) -> QuizItem | None:
    answer = str(data.get("answer") or "").strip()
    career = data.get("career")
    if not answer or not isinstance(career, list) or len(career) < 2:
        return None
    lines = ["Угадай игрока по клубной карьере:", ""]
    for row in career[:8]:
        if not isinstance(row, dict):
            continue
        seasons = str(row.get("seasons") or "").strip()
        club = str(row.get("club") or "").strip()
        if not seasons or not club:
            continue
        lines.append(f"📅 {seasons} — {club}")
    if len(lines) < 4:
        return None
    return QuizItem(kind="player", answer=answer, body="\n".join(lines))


def _parse_club(data: dict[str, Any]) -> QuizItem | None:
    answer = str(data.get("answer") or "").strip()
    country = str(data.get("country") or "").strip()
    founded = data.get("founded")
    ucl = str(data.get("ucl") or "").strip()
    fact = str(data.get("fact") or "").strip()
    if not answer or not country or not founded or not ucl or not fact:
        return None
    try:
        year = int(founded)
    except (TypeError, ValueError):
        return None
    # нормализуем ucl
    ucl_norm = ucl.casefold().replace(" ", "")
    if ucl_norm in {"0", "нет", "no", "none", "-"}:
        ucl_label = "нет"
    elif re.fullmatch(r"x?\d+", ucl_norm):
        n = int(ucl_norm.lstrip("x"))
        ucl_label = "нет" if n <= 0 else f"x{n}"
    else:
        ucl_label = ucl
    body = (
        "Угадай футбольный клуб:\n"
        "\n"
        f"🌍 Страна: {country}\n"
        f"📅 Год основания: {year}\n"
        f"🏆 Победитель Лиги Чемпионов: {ucl_label}\n"
        f"📌 {fact}"
    )
    return QuizItem(kind="club", answer=answer, body=body)


def _parse_quiz(raw: str, expect: QuizKind) -> QuizItem | None:
    text = (raw or "").strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or expect).strip().lower()
    if kind == "player":
        item = _parse_player(data)
    elif kind == "club":
        item = _parse_club(data)
    else:
        return None
    if item is None or item.kind != expect:
        return None
    return item


def _build_input(channel_key: str, kind: QuizKind, recent_answers: list[str]) -> str:
    ch = get_channel(channel_key)
    lines = [
        f"channel_key={channel_key}",
        f"channel_title={ch.title}",
        f"quiz_kind={kind}",
        f"theme={ch.theme}",
        "",
        f"Сгенерируй 1 квиз типа {kind}.",
    ]
    if recent_answers:
        lines.append("")
        lines.append("Уже были ответы (не повторяй):")
        for a in recent_answers[:40]:
            lines.append(f"- {a}")
    return "\n".join(lines)


def generate_quiz(
    channel_key: str,
    kind: QuizKind,
    recent_answers: list[str] | None = None,
) -> QuizItem:
    settings = get_settings()
    client = get_openai_client()
    model = (settings.opros_openai_model or "gpt-6-luna").strip()
    recent = list(recent_answers or [])
    last_error: Exception | None = None

    for _ in range(2):
        try:
            response = client.responses.create(
                model=model,
                instructions=INSTRUCTIONS,
                input=_build_input(channel_key, kind, recent),
                reasoning={"effort": "none"},
                temperature=0.9,
                text={"verbosity": "low"},
            )
        except TypeError:
            response = client.responses.create(
                model=model,
                instructions=INSTRUCTIONS,
                input=_build_input(channel_key, kind, recent),
                temperature=0.9,
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

        quiz = _parse_quiz(raw, kind)
        if not quiz:
            last_error = RuntimeError(f"bad quiz json: {raw[:240]!r}")
            continue
        if any(quiz.answer.casefold() == a.strip().casefold() for a in recent):
            recent = [quiz.answer] + recent
            last_error = RuntimeError(f"duplicate answer: {quiz.answer}")
            continue
        return quiz

    raise RuntimeError(f"quiz generate failed: {last_error}")
