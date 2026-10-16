"""Генерация вопроса через OpenAI (стабильный instructions → prompt cache)."""

from __future__ import annotations

import re

from app.config import get_settings
from opros.channels import Channel, get_channel
from src.config import get_openai_client

# Стабильный префикс: НЕ менять между запросами (кэш промпта на стороне API).
INSTRUCTIONS = """Ты — редактор анонимных бинарных опросов для каналов в MAX/Telegram.

Задача: сгенерировать РОВНО ОДИН короткий вопрос, на который отвечают только «Да» или «Нет».

Стиль (как в каналах «Анонимные опросы»):
- Одна фраза-вопрос на русском, живой разговорный тон.
- Личное обращение на «вы» или безлично («У вас…», «Вы когда-нибудь…», «Что для вас важнее…» — но ответ всё равно да/нет; лучше прямые да/нет-вопросы).
- Узнаваемые житейские ситуации, лёгкая провокация, без лекции.
- Без политики, без призывов к насилию, без упоминания несовершеннолетних в сексуальном контексте.
- Без хештегов, без ссылок, без markdown, без кавычек вокруг всего вопроса.
- Без эмодзи в вопросе.
- Без вариантов ответа и без концовки «Да/Нет» — их добавит система отдельно.
- Не пиши преамбулу («Вот вопрос:»), не нумеруй, не объясняй выбор.

Темы каналов (ориентир тона):
1) 18plus — взрослые / 18+ / провокационные вопросы про секс, отношения, табу. Дерзко, анонимно. Без порно-деталей.
2) lichnoe — личное: быт, привычки, деньги, работа, дружба, мелкий стыд. Без жёсткого 18+.
3) anon — общие житейские анонимные вопросы, массово-узнаваемые. Лёгкий вайб.

Вывод: только текст вопроса, одна строка (допустимы 1–2 коротких предложения, если нужно)."""

_STRIP_PREFIX = re.compile(
    r"^(?:вопрос\s*:|вот\s+вопрос\s*:|опрос\s*:)\s*",
    re.IGNORECASE,
)
_FOOTER_MARKERS = (
    "отправь нужную реакцию",
    "◯",
    "да (✅)",
    "нет (🛑",
)


def _clean_question(raw: str) -> str:
    text = (raw or "").strip().strip('"').strip("'").strip("`")
    text = _STRIP_PREFIX.sub("", text).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    kept: list[str] = []
    for ln in lines:
        low = ln.casefold()
        if any(m in low for m in _FOOTER_MARKERS):
            break
        if low in {"да", "нет", "да.", "нет."}:
            break
        kept.append(ln)
    text = " ".join(kept).strip() if kept else text.splitlines()[0].strip() if text else ""
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _build_input(channel: Channel, recent: list[str]) -> str:
    lines = [
        f"channel_key={channel.key}",
        f"channel_title={channel.title}",
        f"theme={channel.theme}",
        "",
        "Сгенерируй 1 новый уникальный вопрос для этого канала.",
    ]
    if recent:
        lines.append("")
        lines.append("Уже были (не повторяй и не перефразируй близко):")
        for q in recent[:30]:
            lines.append(f"- {q}")
    return "\n".join(lines)


def generate_question(channel_key: str, recent: list[str] | None = None) -> str:
    settings = get_settings()
    channel = get_channel(channel_key)
    client = get_openai_client()
    model = (settings.opros_openai_model or "gpt-6-luna").strip()
    recent_list = list(recent or [])

    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            response = client.responses.create(
                model=model,
                instructions=INSTRUCTIONS,
                input=_build_input(channel, recent_list),
                reasoning={"effort": "none"},
                temperature=0.9,
                text={"verbosity": "low"},
            )
        except TypeError:
            # старые SDK / gateway без reasoning|text
            response = client.responses.create(
                model=model,
                instructions=INSTRUCTIONS,
                input=_build_input(channel, recent_list),
                temperature=0.9,
            )
        except Exception as e:
            last_error = e
            break

        question = _clean_question(getattr(response, "output_text", "") or "")
        if not question:
            # fallback: вытащить из output
            chunks: list[str] = []
            for item in getattr(response, "output", None) or []:
                for part in getattr(item, "content", None) or []:
                    t = getattr(part, "text", None)
                    if t:
                        chunks.append(str(t))
            question = _clean_question("\n".join(chunks))

        if not question:
            last_error = RuntimeError("empty question from model")
            continue

        low = question.casefold()
        if any(r.strip().casefold() == low for r in recent_list):
            recent_list = [question] + recent_list
            last_error = RuntimeError(f"duplicate question: {question}")
            continue
        return question

    raise RuntimeError(f"opros generate failed: {last_error}")
