from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from bot.config import Settings


@dataclass
class ParsedMeal:
    name: str
    kcal: float
    protein: float
    fat: float
    carbs: float
    amount: float | None = None
    amount_unit: str | None = None
    meal_slot: str | None = None


WEIGHT_RE = re.compile(
    r"(?i)(?:вес|weight|кг|kg)\s*[:=]?\s*(\d+[.,]\d+|\d+)\s*(?:кг|kg)?|"
    r"^(\d+[.,]\d+|\d+)\s*(?:кг|kg)\s*$"
)

KCAL_RE = re.compile(r"(?i)(?:калори[яи]|ккал|kcal)\s*[:=]?\s*(\d+[.,]\d+|\d+)")
PROTEIN_RE = re.compile(r"(?i)(?:белк[иа]|protein[s]?)\s*[:=]?\s*(\d+[.,]\d+|\d+)")
FAT_RE = re.compile(r"(?i)(?:жир[ыа]|fat[s]?)\s*[:=]?\s*(\d+[.,]\d+|\d+)")
CARBS_RE = re.compile(r"(?i)(?:углевод[ыа]|carb[so]?)\s*[:=]?\s*(\d+[.,]\d+|\d+)")
PORTION_RE = re.compile(
    r"(?i)(?:порци[яиюе]|вес(?:ом)?|объ[её]м)[^0-9]{0,20}~?\s*(\d+[.,]\d+|\d+)\s*(г|гр|g|мл|ml|л|l)?"
)
NAME_RE = re.compile(r"[«\"]([^»\"]+)[»\"]")

PORTION_ANSWER_RE = re.compile(
    r"^\s*(?:вс[её]|вся|целиком|упаковк[ауи]|бутылк[ауи]|1\s*(?:шт|уп))?\s*"
    r"(?:(\d+[.,]\d+|\d+)\s*(г|гр|g|мл|ml|л|l|шт)?|"
    r"(\d+[.,]\d+|\d+)\s*[xх×]\s*)?\s*$|"
    r"^\s*(\d+[.,]\d+|\d+)\s*(г|гр|g|мл|ml|л|l|шт)?\s*$",
    re.I,
)


def _num(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).replace(",", ".").replace(" ", ""))


def parse_weight(text: str) -> float | None:
    text = text.strip()
    m = WEIGHT_RE.search(text)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    kg = _num(raw)
    if kg is None or kg < 30 or kg > 400:
        return None
    # сообщение только про вес (короткое), либо явно «вес …»
    if re.search(r"(?i)\bвес\b|\bkg\b|\bкг\b", text) and not KCAL_RE.search(text):
        return kg
    if len(text) <= 20 and not KCAL_RE.search(text):
        return kg
    return None


def parse_meal_regex(text: str) -> ParsedMeal | None:
    kcal = _num(m.group(1)) if (m := KCAL_RE.search(text)) else None
    protein = _num(m.group(1)) if (m := PROTEIN_RE.search(text)) else None
    fat = _num(m.group(1)) if (m := FAT_RE.search(text)) else None
    carbs = _num(m.group(1)) if (m := CARBS_RE.search(text)) else None

    if kcal is None and all(v is None for v in (protein, fat, carbs)):
        return None
    if kcal is None:
        # грубая оценка, если указали только БЖУ
        kcal = (protein or 0) * 4 + (fat or 0) * 9 + (carbs or 0) * 4

    name = None
    if nm := NAME_RE.search(text):
        name = nm.group(1).strip()
    else:
        first = text.strip().splitlines()[0]
        first = re.sub(r"(?i)калорийность|пищевая ценность.*", "", first).strip(" .:;-")
        name = first[:80] if first else "Приём пищи"

    amount = None
    unit = None
    if pm := PORTION_RE.search(text):
        amount = _num(pm.group(1))
        unit = (pm.group(2) or "г").lower()
        if unit in {"гр", "g"}:
            unit = "г"
        if unit in {"ml"}:
            unit = "мл"
        if unit == "l":
            unit = "л"

    return ParsedMeal(
        name=name or "Приём пищи",
        kcal=float(kcal or 0),
        protein=float(protein or 0),
        fat=float(fat or 0),
        carbs=float(carbs or 0),
        amount=amount,
        amount_unit=unit,
    )


FOOD_HINT_RE = re.compile(
    r"(?i)\b("
    r"съел|съела|поел|поела|завтрак|обед|ужин|перекус|"
    r"яйц|овсян|творог|курин|рис|гречк|хлеб|банан|яблок|"
    r"кофе|чай|протеин|каш[аеи]|суп|салат|мясо|рыб|сыр|"
    r"бургер|пицц|паста|макарон|картош|картофел"
    r")\b"
)


def looks_like_food(text: str) -> bool:
    if KCAL_RE.search(text) or PROTEIN_RE.search(text):
        return True
    return bool(FOOD_HINT_RE.search(text))


async def parse_meal_llm(text: str, settings: Settings) -> ParsedMeal | None:
    if not settings.groq_api_key:
        return None
    prompt = (
        "Оцени КБЖУ съеденного. Если в тексте нет еды — верни null.\n"
        "Если точных цифр нет — оцени типичные порции для взрослого мужчины.\n"
        "Ответь ТОЛЬКО JSON:\n"
        '{"name":"...","kcal":0,"protein":0,"fat":0,"carbs":0,"amount":null,"amount_unit":null,'
        '"meal_slot":"breakfast|lunch|dinner|snack|null"}\n'
        f"Текст: {text}"
    )
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.groq_model,
        "messages": [
            {
                "role": "system",
                "content": "Ты нутрициолог-парсер. Оцениваешь КБЖУ по описанию еды. Только JSON.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(
            proxy=settings.groq_http_proxy or None,
            timeout=45,
        ) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None

    content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if content.lower() == "null":
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("kcal") is None and not any(data.get(k) for k in ("protein", "fat", "carbs")):
        return None
    slot = data.get("meal_slot")
    if slot not in {"breakfast", "lunch", "dinner", "snack"}:
        slot = None
    return ParsedMeal(
        name=str(data.get("name") or "Приём пищи")[:120],
        kcal=float(data.get("kcal") or 0),
        protein=float(data.get("protein") or 0),
        fat=float(data.get("fat") or 0),
        carbs=float(data.get("carbs") or 0),
        amount=_num(data.get("amount")),
        amount_unit=data.get("amount_unit"),
        meal_slot=slot,
    )


async def parse_meal(text: str, settings: Settings) -> ParsedMeal | None:
    meal = parse_meal_regex(text)
    if meal and meal.kcal > 0:
        return meal
    if looks_like_food(text) or len(text.split()) >= 3:
        llm = await parse_meal_llm(text, settings)
        if llm and llm.kcal > 0:
            return llm
    return meal


def is_delete_request(text: str) -> bool:
    """«удали это», «убери», «не то» и т.п."""
    t = text.strip().lower().replace("ё", "е")
    t = re.sub(r"[!?.…]+$", "", t).strip()
    if t in {
        "удали",
        "удалить",
        "убери",
        "убрать",
        "отмени",
        "отмена",
        "сотри",
        "не то",
        "ошибка",
        "удали это",
        "убери это",
        "отмени это",
        "удали блюдо",
        "убери блюдо",
    }:
        return True
    return bool(
        re.fullmatch(
            r"(удали|удалить|убери|убрать|отмени|сотри)(\s+(это|блюдо|запись|еду))?",
            t,
        )
    )


def is_repeat_request(text: str) -> bool:
    """Ответ на запись: повторить то же блюдо сегодня."""
    t = text.strip().lower().replace("ё", "е")
    t = re.sub(r"[!?.…]+$", "", t).strip()
    if t in {
        "повтори",
        "повторить",
        "повтор",
        "еще раз",
        "ещё раз",
        "снова",
        "тоже",
        "тоже самое",
        "то же самое",
        "сегодня тоже",
        "сегодня тоже самое",
        "сегодня то же самое",
        "скопируй",
        "копируй",
        "как это",
        "+1",
    }:
        return True
    return bool(
        re.search(
            r"(?i)(повтор|"
            r"то\s*же\s+сам|"
            r"сегодня\s+то\s*же|"
            r"съел\s+то\s*же|"
            r"то\s*же\s+съел|"
            r"еще\s+раз|"
            r"ещё\s+раз)",
            t,
        )
    )


def extract_meal_name_from_bot_message(text: str | None) -> str | None:
    """Достаёт название из сообщения «🍽 Название …»."""
    if not text:
        return None
    plain = re.sub(r"<[^>]+>", "", text)
    m = re.search(r"🍽\s*(.+)", plain)
    if not m:
        return None
    name = m.group(1).strip().split("\n")[0].strip()
    return name or None


def extract_activity_name_from_bot_message(text: str | None) -> str | None:
    """Достаёт название из «🏃 Название …»."""
    if not text:
        return None
    plain = re.sub(r"<[^>]+>", "", text)
    m = re.search(r"🏃\s*(.+)", plain)
    if not m:
        return None
    name = m.group(1).strip().split("\n")[0].strip()
    return name or None


def parse_portion_answer(text: str) -> tuple[float | None, str | None, bool]:
    """Возвращает (amount, unit, use_full_package)."""
    t = text.strip().lower().replace("ё", "е")
    if re.search(
        r"(?i)\b(все|всё|целиком|полностью|всю\s+упаковку|упаковка|бутылка)\b",
        t,
    ) or t in {"1", "1 шт", "1уп", "1 уп"}:
        return None, None, True
    if re.search(r"(?i)\b(половин[ауе]|1/2|½)\b", t):
        return 0.5, "шт", False
    if re.search(r"(?i)\b(треть|1/3)\b", t):
        return 1 / 3, "шт", False
    if re.search(r"(?i)\b(четверть|1/4)\b", t):
        return 0.25, "шт", False

    m = re.search(
        r"(?i)(\d+[.,]\d+|\d+)\s*(г|гр|g|мл|ml|л|l|шт)\b",
        text.strip(),
    )
    if not m:
        m = re.match(r"(?i)^\s*(\d+[.,]\d+|\d+)\s*(г|гр|g|мл|ml|л|l|шт)?\s*$", text.strip())
    if not m:
        return None, None, False
    amount = _num(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in {"гр", "g"}:
        unit = "г"
    if unit == "ml":
        unit = "мл"
    if unit == "l":
        unit = "л"
    if not unit:
        unit = "г"
    return amount, unit, False


COMMANDS = {
    "итог",
    "итоги",
    "день",
    "статус",
    "оценка",
    "ревью",
    "помощь",
    "help",
    "start",
    "отмена",
    "undo",
    "назад",
}
