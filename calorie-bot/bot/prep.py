"""Заготовки из ингредиентов: фото упаковок → N порций → потом «съел порцию»."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bot.nutrition import fmt_num
from bot.ocr import LabelData, scale_label_to_meal
from bot.parsers import ParsedMeal, _num


@dataclass
class PrepDraft:
    servings: float
    name_hint: str | None = None


def parse_prep_servings(text: str | None) -> PrepDraft | None:
    """
    «на 4 порции», «4 порции», «приготовил на 4», «ингредиенты на 4 блюда»…
    """
    if not text:
        return None
    t = text.strip().lower().replace("ё", "е")
    if not re.search(
        r"(?i)порц|блюд|заготов|ингредиент|приготов|сварил|собрал|на\s+\d+",
        t,
    ):
        # голое «4 порции» ещё поймаем ниже
        if not re.search(r"(?i)\d+\s*порц", t):
            return None

    patterns = [
        r"(?i)(?:приготовил(?:а)?|сварил(?:а)?|собрал(?:а)?|сделал(?:а)?)\s+на\s+(\d+[.,]\d+|\d+)\s*(?:порц\w*|блюд\w*|человек\w*|персон\w*)?",
        r"(?i)(?:заготовк\w*|ингредиент\w*|продукты)\s+(?:на|для)\s+(\d+[.,]\d+|\d+)\s*(?:порц\w*|блюд\w*)?",
        r"(?i)(?:на|для)\s+(\d+[.,]\d+|\d+)(?:\s*(?:порц\w*|блюд\w*|человек\w*|персон\w*))?",
        r"(?i)(\d+[.,]\d+|\d+)\s*(?:порц\w*|блюд\w*)",
        r"(?i)порц\w*\s*[:=]?\s*(\d+[.,]\d+|\d+)",
    ]
    servings = None
    for p in patterns:
        m = re.search(p, t)
        if m:
            servings = _num(m.group(1))
            if servings and 1 <= servings <= 30:
                break
            servings = None

    # явный интент без числа — вернём servings=None через отдельный флаг нельзя;
    # вызывающий спросит, если is_prep_intent и servings is None
    if servings is None:
        if is_prep_intent(t):
            return PrepDraft(servings=0)  # 0 = нужно уточнить
        return None

    name_hint = None
    hm = re.search(
        r"(?i)(?:заготовк[аи]|блюдо|рецепт)\s*[:\-–]\s*(.+?)(?:\s+на\s+\d|\s+\d+\s*порц|$)",
        text.strip(),
    )
    if hm:
        hint = hm.group(1).strip(" :-–")
        if (
            3 < len(hint) < 80
            and not re.search(r"(?i)^(?:ингредиент|на\s+\d)", hint)
        ):
            name_hint = hint

    return PrepDraft(servings=float(servings), name_hint=name_hint)


def is_prep_intent(text: str | None) -> bool:
    if not text:
        return False
    t = text.lower().replace("ё", "е")
    return bool(
        re.search(
            r"(?i)("
            r"ингредиент|заготовк|приготов|сварил|собрал\s+на|"
            r"на\s+\d+\s*порц|на\s+\d+\s*блюд|\d+\s*порц|"
            r"не\s+ел|не\s+съел|сырь[её]|упаковк"
            r")",
            t,
        )
    )


def parse_eat_servings(text: str) -> float | None:
    """Сколько порций из заготовки съел. None — не про это."""
    t = text.strip().lower().replace("ё", "е")
    t = re.sub(r"[!?.…]+$", "", t).strip()

    if t in {
        "порцию",
        "порция",
        "одну",
        "одну порцию",
        "съел порцию",
        "съела порцию",
        "1 порцию",
        "1 порция",
        "1",
        "съел одну",
        "съел 1",
        "взял порцию",
    }:
        return 1.0

    if t in {"половину порции", "полпорции", "0.5", "½"}:
        return 0.5

    m = re.search(
        r"(?i)(?:съел(?:а)?|взял(?:а)?|ел(?:а)?)\s+"
        r"(?:ещ[её]\s+)?"
        r"(\d+[.,]\d+|\d+|одн[уа]|две|три|половин[уа])\s*"
        r"(?:порц\w*)?",
        t,
    )
    if m:
        raw = m.group(1).lower()
        words = {"одну": 1, "одна": 1, "две": 2, "три": 3, "половину": 0.5, "половина": 0.5}
        if raw in words:
            return float(words[raw])
        n = _num(raw)
        if n is not None and 0 < n <= 20:
            return n

    m = re.search(r"(?i)(\d+[.,]\d+|\d+)\s*порц", t)
    if m:
        n = _num(m.group(1))
        if n is not None and 0 < n <= 20:
            return n

    if re.fullmatch(r"(?i)съел(?:а)?(\s+это)?", t):
        return 1.0

    return None


def looks_like_eat_from_prep(text: str) -> bool:
    t = text.lower().replace("ё", "е")
    if parse_eat_servings(text) is not None:
        return True
    return bool(
        re.search(
            r"(?i)(?:съел|порци).*(?:заготов|из\s+заготов|куриц|макарон|паст)|"
            r"(?:из\s+заготовки)|порци\w*\s+(?:куриц|макарон)",
            t,
        )
    )


def ingredient_meal_from_label(label: LabelData) -> ParsedMeal:
    """Упаковка ингредиента целиком → вклад в заготовку."""
    return scale_label_to_meal(label, use_full_package=True)


def build_prep_name(ingredients: list[ParsedMeal], hint: str | None = None) -> str:
    if hint:
        return hint[:100]
    parts = []
    for ing in ingredients[:4]:
        short = re.split(r"[,\(\n]", ing.name)[0].strip()
        if len(short) > 40:
            short = short[:37] + "…"
        parts.append(short)
    if not parts:
        return "Заготовка"
    return " + ".join(parts)[:100]


def format_prep_created(
    *,
    name: str,
    servings: float,
    total: ParsedMeal,
    per: ParsedMeal,
    ingredients: list[ParsedMeal],
) -> str:
    lines = [
        f"🍲 <b>Заготовка: {name}</b>",
        f"Порций: <b>{fmt_num(servings, 0)}</b>",
        "",
        "Ингредиенты (целиком в кастрюлю, не в дневник):",
    ]
    for i, ing in enumerate(ingredients, 1):
        amt = ""
        if ing.amount is not None:
            amt = f" · {fmt_num(ing.amount)} {ing.amount_unit or ''}".rstrip()
        lines.append(
            f"{i}. {ing.name} — {fmt_num(ing.kcal, 0)} ккал "
            f"(Б{fmt_num(ing.protein)}/Ж{fmt_num(ing.fat)}/У{fmt_num(ing.carbs)}){amt}"
        )
    lines.extend(
        [
            "",
            f"Всего: {fmt_num(total.kcal, 0)} ккал · "
            f"Б{fmt_num(total.protein)}/Ж{fmt_num(total.fat)}/У{fmt_num(total.carbs)}",
            f"На 1 порцию: <b>{fmt_num(per.kcal, 0)} ккал</b> · "
            f"Б{fmt_num(per.protein)}/Ж{fmt_num(per.fat)}/У{fmt_num(per.carbs)}",
            "",
            "Когда съешь — напиши <code>съел порцию</code> "
            "или ответь на это сообщение: <code>1</code> / <code>2 порции</code>.",
        ]
    )
    return "\n".join(lines)
