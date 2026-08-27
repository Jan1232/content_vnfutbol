"""Мотивирующая оценка дня: количество к плану, без стыда и «вредных» продуктов."""

from __future__ import annotations

from dataclasses import dataclass, field

from bot.db import MealTotals
from bot.goals import GoalProgress
from bot.nutrition import EnergyNeeds, fmt_num


@dataclass
class DayReview:
    """plan_fit 1–10 = насколько объём съеденного совпал с твоим планом."""

    plan_fit: int
    headline: str
    good: list[str] = field(default_factory=list)
    improve: list[str] = field(default_factory=list)
    focus_tomorrow: str = ""
    snapshot: str = ""


def assess_day(
    *,
    totals: MealTotals,
    progress: GoalProgress,
    needs: EnergyNeeds | None,
    activity_kcal: float = 0.0,
) -> DayReview:
    if totals.count == 0:
        return DayReview(
            plan_fit=5,
            headline="День без записей еды",
            good=["Можно начать с любого приёма — учёт сам по себе уже шаг."],
            improve=["Записать еду голосом, текстом или фото этикетки."],
            focus_tomorrow="Один приём пищи в трекер — и день уже «живой».",
            snapshot="Съедено: пока пусто",
        )

    budget = progress.calorie_budget
    protein_goal = progress.goals.protein_g
    fat_goal = progress.fat_target
    kcal = totals.kcal
    protein = totals.protein
    fat = totals.fat

    good: list[str] = []
    improve: list[str] = []

    # --- ккал ---
    cal_score = 35.0
    if budget and budget > 0:
        ratio = kcal / budget
        if 0.9 <= ratio <= 1.05:
            cal_score = 55
            good.append(
                f"Ккал в бюджете: {fmt_num(kcal, 0)} / {fmt_num(budget, 0)} — объём дня к темпу."
            )
        elif 0.8 <= ratio < 0.9:
            cal_score = 48
            good.append(
                f"Ккал близко к плану снизу ({fmt_num(kcal, 0)} / {fmt_num(budget, 0)})."
            )
            improve.append(
                "Если есть силы и голод — можно чуть добрать, лучше белком."
            )
        elif ratio < 0.8:
            cal_score = 36
            improve.append(
                f"Ккал заметно ниже плана ({fmt_num(kcal, 0)} / {fmt_num(budget, 0)}). "
                "Не «подвиг за голод» — телу нужна энергия."
            )
        elif 1.05 < ratio <= 1.2:
            cal_score = 42
            improve.append(
                f"Чуть выше бюджета (~+{fmt_num(kcal - budget, 0)} ккал). "
                "Один день не ломает неделю."
            )
        else:
            cal_score = 26
            improve.append(
                f"Объём выше плана (~+{fmt_num(kcal - budget, 0)} ккал). "
                "Без отыгрыша голодом — завтра обычный бюджет."
            )
    elif needs and needs.tdee > 0:
        cal_score = 40 if kcal <= needs.tdee * 1.05 else 30
        good.append(f"Записано {fmt_num(kcal, 0)} ккал при расходе ~{fmt_num(needs.tdee, 0)}.")

    # --- белок ---
    prot_score = 18.0
    if protein_goal and protein_goal > 0:
        pr = protein / protein_goal
        if pr >= 0.95:
            prot_score = 35
            good.append(
                f"Белок закрыт: {fmt_num(protein)} / {fmt_num(protein_goal, 0)} г."
            )
        elif pr >= 0.75:
            prot_score = 26
            left = protein_goal - protein
            improve.append(
                f"Белок {fmt_num(protein)} / {fmt_num(protein_goal, 0)} г — "
                f"добрать ~{fmt_num(left)} г (творог, яйца, мясо, йогурт)."
            )
        else:
            prot_score = 14
            improve.append(
                f"Белка мало к цели ({fmt_num(protein)} / {fmt_num(protein_goal, 0)} г) — "
                "это рычаг сытости на дефиците, не штраф."
            )
    else:
        dens = (protein * 4 / kcal * 100) if kcal > 0 else 0
        if dens >= 20:
            prot_score = 28
            good.append(f"Белковая плотность дня ~{fmt_num(dens, 0)}% ккал.")

    # --- жиры (автоцель) ---
    fat_score = 12.0
    if fat_goal and fat_goal > 0:
        fr = fat / fat_goal
        if 0.75 <= fr <= 1.15:
            fat_score = 18
            good.append(
                f"Жиры в ориентире: {fmt_num(fat)} / {fmt_num(fat_goal, 0)} г."
            )
        elif fr < 0.75:
            fat_score = 12
            improve.append(
                f"Жиры ниже ориентира ({fmt_num(fat)} / {fmt_num(fat_goal, 0)} г). "
                "Чуть жирнее соус/орехи/масло — ок, жиры нужны."
            )
        else:
            fat_score = 10
            improve.append(
                f"Жиры выше ориентира ({fmt_num(fat)} / {fmt_num(fat_goal, 0)} г). "
                "Не «плохо» — просто калорийно; порции решают."
            )

    # --- трекинг / активность ---
    track_score = min(10.0, 4 + totals.count * 1.2)
    good.append(f"Учёт ведётся: {totals.count} запис. еды.")
    if activity_kcal > 0:
        good.append(f"Активность учтена: ~{fmt_num(activity_kcal, 0)} ккал к расходу.")

    raw = cal_score + prot_score + fat_score + track_score
    plan_fit = int(max(1, min(10, round(raw / 10))))

    if plan_fit >= 9:
        headline = "День сильно в плане"
    elif plan_fit >= 7:
        headline = "День уверенно рядом с целью"
    elif plan_fit >= 5:
        headline = "Рабочий день — есть за что зацепиться"
    else:
        headline = "День с большим объёмом — данные, не приговор"

    # Фокус на завтра: один конкретный шаг
    if budget and kcal < budget * 0.8:
        focus = f"Попасть ближе к бюджету (~{fmt_num(budget, 0)} ккал), не голодать."
    elif protein_goal and protein < protein_goal * 0.85:
        left = protein_goal - protein
        focus = f"Добрать белок: ещё ~{fmt_num(max(left, 20), 0)} г к привычному дню."
    elif fat_goal and fat > fat_goal * 1.2 and budget:
        focus = "Держать бюджет за счёт порций — жирные продукты можно, количество важнее."
    elif budget and kcal > budget * 1.15:
        focus = "Обычный день по бюджету, без жёсткого отыгрыша."
    else:
        focus = "Повторить ровный день: бюджет + белок без идеальной еды."

    if not good:
        good.append("Ты смотришь на цифры — уже контроль.")
    if not improve:
        improve.append("Держать тот же курс: ровный объём без крайностей.")

    snap_parts = [
        f"🔥 {fmt_num(kcal, 0)} ккал",
        f"Б {fmt_num(protein)}",
        f"Ж {fmt_num(fat)}",
        f"У {fmt_num(totals.carbs)}",
    ]
    if budget:
        snap_parts.append(f"бюджет {fmt_num(budget, 0)}")
    if protein_goal:
        snap_parts.append(f"белок {fmt_num(protein)}/{fmt_num(protein_goal, 0)}")
    if fat_goal:
        snap_parts.append(f"жиры {fmt_num(fat)}/{fmt_num(fat_goal, 0)}")

    return DayReview(
        plan_fit=plan_fit,
        headline=headline,
        good=good[:4],
        improve=improve[:3],
        focus_tomorrow=focus,
        snapshot=" · ".join(snap_parts),
    )


def format_day_review(review: DayReview) -> list[str]:
    bar = "●" * review.plan_fit + "○" * (10 - review.plan_fit)
    lines = [
        f"<b>{review.headline}</b>",
        f"Оценка: <b>{review.plan_fit}/10</b>  {bar}",
    ]
    if review.snapshot:
        lines.append(review.snapshot)
    lines.append("")
    lines.append("<b>Сделано хорошо</b>")
    for item in review.good:
        lines.append(f"✓ {item}")
    lines.append("")
    lines.append("<b>Можно улучшить</b>")
    for item in review.improve:
        lines.append(f"→ {item}")
    lines.append("")
    lines.append(f"<b>Фокус на завтра</b>\n{review.focus_tomorrow}")
    return lines
