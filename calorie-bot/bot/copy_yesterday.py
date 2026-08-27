from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from bot.db import Database
from bot.parsers import ParsedMeal

SLOT_ALIASES = {
    "завтрак": "breakfast",
    "завтрака": "breakfast",
    "обед": "lunch",
    "обеда": "lunch",
    "ужин": "dinner",
    "ужина": "dinner",
    "перекус": "snack",
    "перекуса": "snack",
    "всё": "all",
    "все": "all",
    "день": "all",
}

COPY_RE = re.compile(
    r"(?i)^\s*как\s+вчера(?:\s+(завтрак|завтрака|обед|обеда|ужин|ужина|перекус|перекуса|всё|все|день))?\s*$"
)


def infer_meal_slot(now: datetime, tz_name: str = "Asia/Yekaterinburg") -> str:
    local = now.astimezone(ZoneInfo(tz_name)) if now.tzinfo else now.replace(tzinfo=ZoneInfo(tz_name))
    h = local.hour
    if 5 <= h < 11:
        return "breakfast"
    if 11 <= h < 16:
        return "lunch"
    if 16 <= h < 22:
        return "dinner"
    return "snack"


def parse_copy_yesterday(text: str) -> str | None:
    m = COPY_RE.match(text.strip())
    if not m:
        return None
    raw = (m.group(1) or "все").lower()
    return SLOT_ALIASES.get(raw, "all")


async def copy_yesterday_meals(
    db: Database,
    *,
    today: date,
    slot: str,
    tz_name: str,
) -> list[ParsedMeal]:
    yesterday = date.fromordinal(today.toordinal() - 1)
    rows = await db.day_meals(yesterday)
    if not rows:
        return []

    copied: list[ParsedMeal] = []
    for row in rows:
        meal_slot = row["meal_slot"] if "meal_slot" in row.keys() else None
        if slot != "all":
            if meal_slot:
                if meal_slot != slot:
                    continue
            else:
                # эвристика по времени создания, если слот не был сохранён
                try:
                    created = datetime.fromisoformat(row["created_at"])
                except Exception:
                    created = datetime.combine(yesterday, time(12, 0))
                if infer_meal_slot(created, tz_name) != slot:
                    continue

        meal = ParsedMeal(
            name=str(row["name"]),
            kcal=float(row["kcal"]),
            protein=float(row["protein"]),
            fat=float(row["fat"]),
            carbs=float(row["carbs"]),
            amount=float(row["amount"]) if row["amount"] is not None else None,
            amount_unit=row["amount_unit"],
        )
        await db.add_meal(
            day=today,
            name=meal.name,
            kcal=meal.kcal,
            protein=meal.protein,
            fat=meal.fat,
            carbs=meal.carbs,
            amount=meal.amount,
            amount_unit=meal.amount_unit,
            source="copy",
            raw=f"как вчера {slot}",
            meal_slot=slot if slot != "all" else (meal_slot or infer_meal_slot(datetime.now(), tz_name)),
        )
        copied.append(meal)
    return copied
