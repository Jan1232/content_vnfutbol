from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from bot.config import Settings
from bot.parsers import _num


@dataclass
class ParsedActivity:
    name: str
    kcal: float
    duration_min: float | None = None


@dataclass
class FactorOverride:
    factor: float
    label: str


FACTOR_PRESETS = {
    "сидячий": 1.2,
    "сидячая": 1.2,
    "дома": 1.2,
    "лёгкий": 1.375,
    "легкий": 1.375,
    "лёгкая": 1.375,
    "легкая": 1.375,
    "прогулки": 1.375,
    "средний": 1.55,
    "средняя": 1.55,
    "активный": 1.55,
    "активная": 1.55,
    "высокий": 1.725,
    "высокая": 1.725,
    "очень активный": 1.725,
    "спорт": 1.725,
}

FACTOR_RE = re.compile(
    r"(?i)^\s*(?:кф|коэффициент|activity\s*factor|активность)\s*[:=]?\s*(\d+[.,]\d+|\d+)\s*$"
)

ACTIVITY_HINT_RE = re.compile(
    r"(?i)(?:"
    r"\bактивност|"
    r"\bтрениров|"
    r"\bзал\b|\bфитнес\b|\bкачалк|\bкардио\b|\bкроссфит\b|"
    r"\bбег\b|\bпробежк|\bходьб|\bпрогулк|\bшаг(?:и|ов)?\b|\bsteps\b|"
    r"\bвелосипед\b|\bвело\b|\bплаван|\bбассейн\b|\bйог|\bрастяжк|"
    r"\bфутбол\b|\bтеннис\b|\bбаскетбол\b|\bволейбол\b|"
    r"\bтанц|\bлыж|\bконьк|\bсамокат\b|"
    r"\bминут(?:ы|у|а)?\b|\bмин\b|"
    r"\bчасов\b|\bчаса\b|\bчас\b|"
    r"\bкм\b"
    r")"
)


def looks_like_activity(text: str) -> bool:
    t = text.strip().lower()
    if t.startswith("цель") or re.search(r"(?i)\bцель\b", t):
        return False
    if re.search(r"(?i)\b(белк|ккал|калори)\b", t) and not re.search(
        r"(?i)\b(трениров|зал|бег|шаг|прогулк)\b", t
    ):
        return False
    return bool(ACTIVITY_HINT_RE.search(text))


# MET ≈ ккал/кг/час; грубые ориентиры
MET_TABLE = [
    (re.compile(r"(?i)бег|пробежк"), 9.0),
    (re.compile(r"(?i)интервал|hiit|кроссфит"), 10.0),
    (re.compile(r"(?i)зал|качалк|силовая|жим|тяг"), 6.0),
    (re.compile(r"(?i)плаван|бассейн"), 7.0),
    (re.compile(r"(?i)велосипед|вело"), 7.5),
    (re.compile(r"(?i)футбол|теннис|баскетбол|волейбол"), 7.0),
    (re.compile(r"(?i)танц"), 5.5),
    (re.compile(r"(?i)йог|растяжк"), 3.0),
    (re.compile(r"(?i)прогулк|ходьб"), 3.5),
    (re.compile(r"(?i)шаг"), 3.3),
]


def parse_factor_override(text: str) -> FactorOverride | None:
    t = text.strip().lower()
    m = FACTOR_RE.match(t)
    if m:
        val = _num(m.group(1))
        if val is not None and 1.0 <= val <= 2.5:
            return FactorOverride(factor=val, label=f"кф {val}")
    # «сегодня средний» / «день сидячий»
    for key, factor in sorted(FACTOR_PRESETS.items(), key=lambda x: -len(x[0])):
        if re.search(rf"(?i)\b{re.escape(key)}\b", t) and (
            "день" in t or "кф" in t or "актив" in t or t.strip() == key
            or t.startswith("сегодня")
        ):
            return FactorOverride(factor=factor, label=key)
    if t.startswith("сегодня ") and len(t) < 40:
        for key, factor in FACTOR_PRESETS.items():
            if key in t:
                return FactorOverride(factor=factor, label=key)
    return None


def _duration_hours(text: str) -> float | None:
    m = re.search(r"(?i)(\d+[.,]\d+|\d+)\s*(?:час(?:а|ов)?|ч)\b", text)
    if m:
        return _num(m.group(1))
    m = re.search(r"(?i)(\d+[.,]\d+|\d+)\s*(?:минут(?:ы|у)?|мин)\b", text)
    if m:
        mins = _num(m.group(1))
        return (mins / 60.0) if mins is not None else None
    return None


def _steps_kcal(text: str, weight_kg: float) -> ParsedActivity | None:
    """«10к шагов» / «10000 шагов» / «steps 10k» → ккал с учётом веса."""
    steps: float | None = None

    m = re.search(
        r"(?i)(\d+[.,]\d+|\d+)\s*(к|k|тыс\.?)?\s*шаг",
        text,
    )
    if m:
        raw = _num(m.group(1))
        suffix = (m.group(2) or "").lower()
        if raw is not None:
            steps = raw * (1000.0 if suffix in {"к", "k", "тыс", "тыс."} else 1.0)
    if steps is None:
        m = re.search(
            r"(?i)шаг(?:и|ов)?\s*[:=]?\s*(\d+[.,]\d+|\d+)\s*(к|k|тыс\.?)?",
            text,
        )
        if m:
            raw = _num(m.group(1))
            suffix = (m.group(2) or "").lower()
            if raw is not None:
                steps = raw * (1000.0 if suffix in {"к", "k", "тыс", "тыс."} else 1.0)
    if steps is None:
        m = re.search(r"(?i)(\d+[.,]\d+|\d+)\s*k\s*steps", text)
        if m:
            raw = _num(m.group(1))
            if raw is not None:
                steps = raw * 1000.0
    if steps is None:
        m = re.search(r"(?i)(\d+[.,]\d+|\d+)\s*steps", text)
        if m:
            steps = _num(m.group(1))

    if steps is None or steps < 500:
        return None
    # ориентир: ~0.5 ккал на кг массы на 1000 шагов
    kcal = steps * weight_kg * 0.0005
    return ParsedActivity(
        name=f"Шаги ({fmt_steps(steps)})",
        kcal=round(kcal),
        duration_min=None,
    )


def fmt_steps(steps: float) -> str:
    n = int(round(steps))
    return f"{n:,}".replace(",", " ")


def estimate_activity_local(text: str, weight_kg: float) -> ParsedActivity | None:
    steps = _steps_kcal(text, weight_kg)
    if steps:
        return steps

    if not ACTIVITY_HINT_RE.search(text):
        return None

    hours = _duration_hours(text)
    met = 5.0
    name = "Активность"
    for pattern, value in MET_TABLE:
        if pattern.search(text):
            met = value
            name = pattern.pattern.split("|")[0].replace("(?i)", "").strip()
            break

    # если длительность не указана — считаем 45 мин по умолчанию для «зал/тренировка»
    if hours is None:
        if re.search(r"(?i)зал|трениров|качалк|кардио|бег|плаван", text):
            hours = 0.75
        elif re.search(r"(?i)прогулк|ходьб", text):
            hours = 0.5
        else:
            hours = 0.5

    # MET * kg * hours
    kcal = met * weight_kg * hours
    # чуть снижаем: MET часто завышен для любителей
    kcal *= 0.9

    pretty = text.strip()
    if len(pretty) > 60:
        pretty = pretty[:57] + "…"
    return ParsedActivity(
        name=pretty or name,
        kcal=round(kcal),
        duration_min=round(hours * 60),
    )


async def estimate_activity_llm(
    text: str,
    weight_kg: float,
    settings: Settings,
) -> ParsedActivity | None:
    if not settings.groq_api_key:
        return None
    prompt = (
        f"Оцени энергозатраты активности для человека {weight_kg:.1f} кг.\n"
        "Верни ТОЛЬКО JSON:\n"
        '{"is_activity":true/false,"name":"...","kcal":123,"duration_min":45}\n'
        "Правила:\n"
        "- Если это еда/вес/не активность — is_activity=false.\n"
        "- «10к шагов» / «10k steps» = 10000 шагов (к/k = тысяча), не 10 минут ходьбы.\n"
        "- Учитывай вес человека в оценке ккал.\n"
        f"Текст: {text}"
    )
    try:
        async with httpx.AsyncClient(proxy=settings.groq_http_proxy or None, timeout=40) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.groq_model,
                    "messages": [
                        {"role": "system", "content": "Ты оцениваешь ккал активности. Только JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                },
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None

    content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(data, dict) or not data.get("is_activity"):
        return None
    kcal = _num(data.get("kcal"))
    if kcal is None or kcal <= 0:
        return None
    return ParsedActivity(
        name=str(data.get("name") or text)[:120],
        kcal=round(float(kcal)),
        duration_min=_num(data.get("duration_min")),
    )


async def parse_activity(
    text: str,
    weight_kg: float,
    settings: Settings,
) -> ParsedActivity | None:
    if not looks_like_activity(text):
        return None

    # Шаги считаем локально: «10к» = 10 000, LLM часто путает
    steps = _steps_kcal(text, weight_kg)
    if steps:
        return steps

    local = estimate_activity_local(text, weight_kg)
    # явный префикс или сильные маркеры — можно без LLM
    if local and (
        re.match(r"(?i)^\s*активност", text)
        or re.search(r"(?i)\b(зал|трениров|пробежк|бег|прогулк|ходьб)\b", text)
    ):
        llm = await estimate_activity_llm(text, weight_kg, settings)
        return llm or local

    llm = await estimate_activity_llm(text, weight_kg, settings)
    if llm:
        return llm
    return local
