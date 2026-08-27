from __future__ import annotations

import re
from dataclasses import dataclass

from bot.db import Database, MealTotals
from bot.nutrition import EnergyNeeds, fmt_num
from bot.parsers import _num


@dataclass
class Goals:
    target_weight: float | None = None
    weekly_loss_kg: float = 0.5
    protein_g: float | None = None
    cut_enabled: bool = False


@dataclass
class GoalProgress:
    goals: Goals
    calorie_budget: float | None
    calorie_left: float | None
    protein_left: float | None
    daily_deficit: float
    weight_left: float | None
    weeks_eta: float | None
    fat_target: float | None = None
    fat_left: float | None = None


def auto_fat_target_g(
    *,
    budget: float | None,
    protein_g: float | None,
    weight_kg: float | None,
) -> float | None:
    """
    Автоцель по жирам на день (г), без ручного ввода.
    ~28% бюджета ккал на жиры, в коридоре 0.6–1.0 г/кг веса.
    """
    if budget is None or budget <= 0:
        return None
    from_budget = budget * 0.28 / 9.0
    if weight_kg and weight_kg > 0:
        lo = 0.6 * weight_kg
        hi = 1.0 * weight_kg
        # если белок уже «съел» много бюджета — всё равно не роняем жиры ниже lo
        target = max(lo, min(hi, from_budget))
    else:
        target = max(40.0, from_budget)
    return round(target)


GOAL_WEIGHT_RE = re.compile(
    r"(?i)^\s*цель\s+вес(?:а)?\s*[:=]?\s*(\d+[.,]\d+|\d+)\s*(?:кг)?"
    r"(?:\s*(?:за|по)?\s*(\d+[.,]\d+|\d+)\s*(?:кг)?\s*/?\s*(?:нед|неделю|week)?)?\s*$"
)
GOAL_PROTEIN_RE = re.compile(
    r"(?i)^\s*цель\s+бел(?:ок|ка|ки|ков)\s*[:=]?\s*(\d+[.,]\d+|\d+)\s*(?:г|гр|g)?"
    r"(?:\s*/?\s*кг)?\s*$"
)
GOAL_PROTEIN_PER_KG_RE = re.compile(
    r"(?i)^\s*цель\s+бел(?:ок|ка|ки|ков)\s*[:=]?\s*(\d+[.,]\d+|\d+)\s*г?\s*/\s*кг\s*$"
)

WEEKLY_LOSS_RE = re.compile(
    r"(?i)(?:минус|−|-|сброс(?:ить)?|худеть|терять)?\s*"
    r"(\d+[.,]\d+|\d+)\s*кг\s*(?:в\s*)?(?:неделю|нед\.?|/нед)",
)
TARGET_WEIGHT_FLEX_RE = re.compile(
    r"(?i)(?:до\s+)?вес(?:а|ом)?\s*(?:до|=|:)?\s*(\d+[.,]\d+|\d+)\s*кг"
)
PROTEIN_FLEX_RE = re.compile(
    r"(\d+[.,]\d+|\d+)\s*(?:г|гр|гр\.|g)\.?\s*(?:белк(?:а|ов|о)?|protein)"
    r"|"
    r"бел(?:ок|ка|ки|ков)\s*[:=]?\s*(\d+[.,]\d+|\d+)\s*(?:г|гр|гр\.|g)?\.?",
    re.I,
)


async def load_goals(db: Database) -> Goals:
    tw = await db.get_meta("goal_target_weight")
    wl = await db.get_meta("goal_weekly_loss_kg")
    pr = await db.get_meta("goal_protein_g")
    cut = await db.get_meta("goal_cut_enabled")
    return Goals(
        target_weight=float(tw) if tw else None,
        weekly_loss_kg=float(wl) if wl else 0.5,
        protein_g=float(pr) if pr else None,
        cut_enabled=(cut == "1") or bool(tw),
    )


async def save_target_weight(db: Database, weight: float, weekly_loss: float | None = None) -> Goals:
    await db.set_meta("goal_target_weight", str(weight))
    await db.set_meta("goal_cut_enabled", "1")
    if weekly_loss is not None:
        await db.set_meta("goal_weekly_loss_kg", str(weekly_loss))
    return await load_goals(db)


async def save_weekly_loss(db: Database, weekly_loss: float) -> Goals:
    await db.set_meta("goal_weekly_loss_kg", str(weekly_loss))
    await db.set_meta("goal_cut_enabled", "1")
    return await load_goals(db)


async def save_protein_goal(db: Database, protein_g: float) -> Goals:
    await db.set_meta("goal_protein_g", str(protein_g))
    return await load_goals(db)


async def apply_goal_updates(db: Database, updates: dict) -> Goals:
    if "target_weight" in updates:
        await db.set_meta("goal_target_weight", str(updates["target_weight"]))
        await db.set_meta("goal_cut_enabled", "1")
    if "weekly_loss_kg" in updates:
        await db.set_meta("goal_weekly_loss_kg", str(updates["weekly_loss_kg"]))
        await db.set_meta("goal_cut_enabled", "1")
    if "protein_g" in updates:
        await db.set_meta("goal_protein_g", str(updates["protein_g"]))
    return await load_goals(db)


def parse_goal_command(text: str, *, weight_kg: float | None = None) -> tuple[str, dict] | None:
    """Возвращает ('weight'|'protein'|'bundle', kwargs) или None."""
    raw = text.strip()

    m = GOAL_WEIGHT_RE.match(raw)
    if m:
        target = _num(m.group(1))
        weekly = _num(m.group(2)) if m.group(2) else None
        if target is None or target < 40 or target > 300:
            return None
        if weekly is not None and (weekly < 0.1 or weekly > 1.5):
            weekly = 0.5
        return "weight", {"target_weight": target, "weekly_loss_kg": weekly}

    m = GOAL_PROTEIN_PER_KG_RE.match(raw)
    if m and weight_kg:
        per_kg = _num(m.group(1))
        if per_kg is None:
            return None
        return "protein", {"protein_g": round(per_kg * weight_kg)}

    m = GOAL_PROTEIN_RE.match(raw)
    if m:
        prot = _num(m.group(1))
        if prot is None:
            return None
        if weight_kg and prot <= 5:
            return "protein", {"protein_g": round(prot * weight_kg)}
        return "protein", {"protein_g": prot}

    if not re.search(r"(?i)\bцель\b", raw):
        return None

    updates: dict = {}
    wm = WEEKLY_LOSS_RE.search(raw)
    weekly_val = None
    if wm:
        weekly_val = _num(wm.group(1))
        if weekly_val is not None and 0.1 <= weekly_val <= 1.5:
            updates["weekly_loss_kg"] = weekly_val

    tm = TARGET_WEIGHT_FLEX_RE.search(raw)
    if tm:
        target = _num(tm.group(1))
        if target is not None and 40 <= target <= 300:
            # не принимать «1 кг в неделю» за целевой вес
            if weekly_val is None or abs(target - weekly_val) > 1e-6:
                updates["target_weight"] = target

    pm = PROTEIN_FLEX_RE.search(raw)
    if pm:
        prot = _num(pm.group(1) or pm.group(2))
        if prot is not None:
            if weight_kg and prot <= 5:
                updates["protein_g"] = round(prot * weight_kg)
            elif 20 <= prot <= 400:
                updates["protein_g"] = prot

    if not updates:
        return None
    return "bundle", updates


def calc_goal_progress(
    goals: Goals,
    *,
    needs: EnergyNeeds | None,
    totals: MealTotals,
    current_weight: float | None,
) -> GoalProgress:
    weight_goal_active = goals.cut_enabled or goals.target_weight is not None
    daily_deficit = (
        max(0.0, goals.weekly_loss_kg) * 7700 / 7 if weight_goal_active else 0.0
    )

    budget = None
    calorie_left = None
    if needs is not None:
        if weight_goal_active:
            budget = max(1200.0, needs.tdee - daily_deficit)
            calorie_left = budget - totals.kcal
        else:
            budget = needs.tdee
            calorie_left = needs.tdee - totals.kcal

    protein_left = None
    if goals.protein_g is not None:
        protein_left = goals.protein_g - totals.protein

    fat_target = auto_fat_target_g(
        budget=budget,
        protein_g=goals.protein_g,
        weight_kg=current_weight,
    )
    fat_left = (fat_target - totals.fat) if fat_target is not None else None

    weight_left = None
    weeks_eta = None
    if goals.target_weight is not None and current_weight is not None:
        weight_left = current_weight - goals.target_weight
        if goals.weekly_loss_kg > 0 and weight_left > 0:
            weeks_eta = weight_left / goals.weekly_loss_kg

    return GoalProgress(
        goals=goals,
        calorie_budget=budget,
        calorie_left=calorie_left,
        protein_left=protein_left,
        daily_deficit=daily_deficit,
        weight_left=weight_left,
        weeks_eta=weeks_eta,
        fat_target=fat_target,
        fat_left=fat_left,
    )


def format_goals_block(progress: GoalProgress) -> list[str]:
    g = progress.goals
    lines = ["🎯 <b>Цели</b>"]
    if g.cut_enabled or g.target_weight is not None:
        if g.target_weight is not None:
            lines.append(
                f"⚖️ Вес → <b>{fmt_num(g.target_weight)} кг</b> "
                f"(−{fmt_num(g.weekly_loss_kg)} кг/нед, дефицит ~{fmt_num(progress.daily_deficit, 0)} ккал/день)"
            )
        else:
            lines.append(
                f"⚖️ Темп: −<b>{fmt_num(g.weekly_loss_kg)}</b> кг/нед "
                f"(дефицит ~{fmt_num(progress.daily_deficit, 0)} ккал/день)"
            )
        if progress.weight_left is not None:
            if progress.weight_left <= 0:
                lines.append("До цели по весу: уже достигнута или ниже 🎉")
            else:
                eta = ""
                if progress.weeks_eta is not None:
                    eta = f", ~{fmt_num(progress.weeks_eta, 1)} нед."
                lines.append(f"Осталось сбросить: <b>{fmt_num(progress.weight_left)} кг</b>{eta}")
        if progress.calorie_budget is not None and progress.calorie_left is not None:
            sign = "+" if progress.calorie_left >= 0 else ""
            lines.append(
                f"🔥 Ккал сегодня: бюджет <b>{fmt_num(progress.calorie_budget, 0)}</b> · "
                f"осталось <b>{sign}{fmt_num(progress.calorie_left, 0)}</b>"
            )
    if g.protein_g is not None and progress.protein_left is not None:
        sign = "+" if progress.protein_left >= 0 else ""
        eaten = g.protein_g - progress.protein_left
        lines.append(
            f"🥩 Белок: <b>{fmt_num(eaten)}/{fmt_num(g.protein_g, 0)} г</b> · "
            f"осталось <b>{sign}{fmt_num(progress.protein_left)}</b>"
        )
    if progress.fat_target is not None and progress.fat_left is not None:
        sign = "+" if progress.fat_left >= 0 else ""
        eaten_f = progress.fat_target - progress.fat_left
        lines.append(
            f"🧈 Жиры: <b>{fmt_num(eaten_f)}/{fmt_num(progress.fat_target, 0)} г</b> · "
            f"осталось <b>{sign}{fmt_num(progress.fat_left)}</b> "
            f"<i>(авто на сегодня)</i>"
        )
    if not g.cut_enabled and g.target_weight is None and g.protein_g is None:
        lines.append("Пока не заданы. Примеры:")
        lines.append("<code>цель вес 120</code>")
        lines.append("<code>цель минус 1 кг в неделю и 180гр белка</code>")
        lines.append("<code>цель белок 180</code>")
    return lines
