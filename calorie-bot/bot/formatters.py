from __future__ import annotations

from datetime import date

from bot.config import Settings
from bot.db import Database, MealTotals
from bot.day_review import assess_day, format_day_review
from bot.goals import calc_goal_progress, format_goals_block, load_goals
from bot.nutrition import energy_needs, fmt_num
from bot.ocr import LabelData
from bot.parsers import ParsedMeal
from bot.picooc import PicoocMeasurement


def format_meal_line(meal: ParsedMeal) -> str:
    parts = [
        f"🍽 <b>{meal.name}</b>",
        f"🔥 {fmt_num(meal.kcal)} ккал",
        f"Б {fmt_num(meal.protein)} · Ж {fmt_num(meal.fat)} · У {fmt_num(meal.carbs)}",
    ]
    if meal.amount is not None:
        unit = meal.amount_unit or ""
        parts.append(f"📦 {fmt_num(meal.amount)} {unit}".strip())
    return "\n".join(parts)


def format_label_preview(label: LabelData) -> str:
    per_map = {"100g": "на 100 г", "100ml": "на 100 мл", "portion": "на порцию"}
    per = per_map.get(label.per, label.per)
    lines = [
        f"🏷 <b>{label.name}</b>",
        f"Указано {per}:",
        f"🔥 {fmt_num(label.kcal)} ккал · Б {fmt_num(label.protein)} · Ж {fmt_num(label.fat)} · У {fmt_num(label.carbs)}",
    ]
    if label.package_amount:
        lines.append(
            f"Упаковка: {fmt_num(label.package_amount)} {label.package_unit or ''}".strip()
        )
    return "\n".join(lines)


def format_picooc_measurement(m: PicoocMeasurement, settings: Settings) -> str:
    lines = [
        "⚖️ <b>Picooc: новое взвешивание</b>",
        f"Вес: <b>{fmt_num(m.weight_kg)} кг</b>",
        f"Время: {m.weighed_at.strftime('%d.%m.%Y %H:%M')}",
    ]
    if m.body_fat is not None:
        lines.append(f"Жир: {fmt_num(m.body_fat)}%")
    if m.muscle_pct is not None:
        lines.append(f"Мышцы: {fmt_num(m.muscle_pct)}%")
    if m.water_pct is not None:
        lines.append(f"Вода: {fmt_num(m.water_pct)}%")
    if m.bmi is not None:
        lines.append(f"BMI: {fmt_num(m.bmi)}")
    if m.visceral_fat is not None:
        lines.append(f"Висц. жир: {fmt_num(m.visceral_fat, 0)}")
    if m.bmr is not None:
        lines.append(f"BMR (весы): {fmt_num(m.bmr, 0)} ккал")
    if m.body_age is not None:
        lines.append(f"Возраст тела: {m.body_age}")

    needs = energy_needs(
        settings,
        m.weight_kg,
        m.weighed_at.date(),
        body_fat_pct=m.body_fat,
        picooc_bmr=float(m.bmr) if m.bmr else None,
    )
    lines.append(
        f"Расход дня ~<b>{fmt_num(needs.tdee, 0)}</b> ккал "
        f"(BMR {fmt_num(needs.bmr, 0)} / {needs.bmr_source}, ×{fmt_num(needs.base_factor, 2)})"
    )
    return "\n".join(lines)


async def day_energy(db: Database, settings: Settings, day: date, weight: float):
    factor = await db.get_day_factor(day)
    activity_kcal = await db.day_activity_kcal(day)
    body = await db.latest_body_measurement()
    body_fat = float(body["body_fat"]) if body and body["body_fat"] is not None else None
    picooc_bmr = float(body["bmr"]) if body and body["bmr"] is not None else None
    return energy_needs(
        settings,
        weight,
        day,
        activity_factor=factor,
        activity_kcal=activity_kcal,
        body_fat_pct=body_fat,
        picooc_bmr=picooc_bmr,
    )


async def format_day_summary(
    db: Database,
    settings: Settings,
    day: date,
    *,
    title: str | None = None,
    detailed: bool = False,
) -> str:
    """Краткие итоги: оценка, хорошо / улучшить / фокус. detailed=True — полный разбор."""
    totals = await db.day_totals(day)
    weight = await db.weight_on_or_before(day)
    head = title or f"🌙 Итоги · {day.strftime('%d.%m.%Y')}"

    if weight is None:
        return "\n".join(
            [
                head,
                "",
                "Вес ещё не задан — без него бюджет и жиры считаю примерно.",
                "Напиши <code>Вес … кг</code> или встань на Picooc.",
                "",
                *_totals_block(totals),
            ]
        )

    needs = await day_energy(db, settings, day, weight)
    goals = await load_goals(db)
    progress = calc_goal_progress(
        goals, needs=needs, totals=totals, current_weight=weight
    )
    review = assess_day(
        totals=totals,
        progress=progress,
        needs=needs,
        activity_kcal=needs.activity_kcal,
    )

    lines = [head, ""]
    lines.extend(format_day_review(review))

    if detailed:
        body = await db.latest_body_measurement()
        meals = await db.day_meals(day)
        activities = await db.day_activities(day)
        balance = needs.tdee - totals.kcal
        sign = "+" if balance >= 0 else ""
        lines.append("")
        lines.append("———")
        lines.append(
            f"⚖️ {fmt_num(weight)} кг · BMR {fmt_num(needs.bmr, 0)} · "
            f"расход ~{fmt_num(needs.tdee, 0)}"
        )
        if body and body["body_fat"] is not None:
            lines.append(
                f"Жир {fmt_num(body['body_fat'])}%"
                + (
                    f" · мышцы {fmt_num(body['muscle_pct'])}%"
                    if body["muscle_pct"] is not None
                    else ""
                )
            )
        lines.append("")
        lines.extend(format_goals_block(progress))
        lines.append("")
        lines.extend(_totals_block(totals))
        lines.append(f"📈 К расходу: {sign}{fmt_num(balance, 0)} ккал")
        if activities:
            lines.append("")
            lines.append("Активности:")
            for i, row in enumerate(activities, 1):
                dur = f", {fmt_num(row['duration_min'], 0)} мин" if row["duration_min"] else ""
                lines.append(f"{i}. {row['name']} — ~{fmt_num(row['kcal'], 0)} ккал{dur}")
        if meals:
            lines.append("")
            lines.append("Еда:")
            for i, row in enumerate(meals, 1):
                lines.append(
                    f"{i}. {row['name']} — {fmt_num(row['kcal'])} ккал "
                    f"(Б{fmt_num(row['protein'])}/Ж{fmt_num(row['fat'])}/У{fmt_num(row['carbs'])})"
                )
    return "\n".join(lines)


def _totals_block(totals: MealTotals) -> list[str]:
    return [
        f"Съедено ({totals.count}):",
        f"🔥 {fmt_num(totals.kcal)} ккал",
        f"🥩 Белки: {fmt_num(totals.protein)} г",
        f"🧈 Жиры: {fmt_num(totals.fat)} г",
        f"🍞 Углеводы: {fmt_num(totals.carbs)} г",
    ]


HELP_TEXT = """<b>Бот КБЖУ</b>

• Еда: КБЖУ текстом, фото этикетки, голосом или «съел два яйца и овсянку»
• Фото: по умолчанию записываю <b>всю упаковку</b>. Другую порцию — подписью к фото (<code>100 г</code>, <code>половину</code>)
• Два фото одного продукта (КБЖУ + масса) — скинь альбомом или подряд
• Удалить: ответь на запись <code>удали</code> или /undo
• Повторить блюдо: ответь на запись <code>повтори</code> или <code>сегодня тоже самое</code>
• Заготовка: фото ингредиентов с подписью <code>на 4 порции</code> / <code>приготовил на 4</code> — не пишет в съеденное. Потом <code>съел порцию</code> или ответ <code>1</code> на сообщение заготовки
• Оценка дня: /day или <code>как день</code> — кратко: оценка, хорошо, улучшить, фокус
• Полный разбор: <code>день подробно</code>
• Жиры: ориентир каждый день сам (от бюджета и веса), вручную задавать не нужно
• Вес: <code>Вес 139.2 кг</code> или Picooc
• Активности: <code>зал 1 час</code>, <code>10к шагов</code>
• Копировать: <code>как вчера</code> / <code>как вчера завтрак</code>
• Цели:
<code>цель вес 120</code>
<code>цель вес 120 за 0.5 кг/нед</code>
<code>цель белок 180</code> или <code>цель белок 1.8 г/кг</code>

Команды: /day · /goals · /sync · /undo · /help

Утром напомню про вес, вечером в 21:00 — краткие итоги дня."""
