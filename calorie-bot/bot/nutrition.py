from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from bot.config import Settings


@dataclass
class EnergyNeeds:
    age: int
    weight_kg: float
    height_cm: float
    bmr: float
    base_factor: float
    activity_kcal: float
    tdee: float
    effective_factor: float
    bmr_source: str = "mifflin"


def calc_age(birthdate: date, on: date | None = None) -> int:
    today = on or date.today()
    years = today.year - birthdate.year
    if (today.month, today.day) < (birthdate.month, birthdate.day):
        years -= 1
    return years


def mifflin_st_jeor(weight_kg: float, height_cm: float, age: int, sex: str) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    if sex.lower() in {"female", "f", "ж", "жен"}:
        return base - 161
    return base + 5


def katch_mcardle(weight_kg: float, body_fat_pct: float) -> float:
    """BMR по lean body mass. body_fat_pct — процент жира 0–100."""
    lbm = weight_kg * (1 - body_fat_pct / 100.0)
    return 370 + 21.6 * lbm


def energy_needs(
    settings: Settings,
    weight_kg: float,
    on: date | None = None,
    *,
    activity_factor: float | None = None,
    activity_kcal: float = 0.0,
    body_fat_pct: float | None = None,
    picooc_bmr: float | None = None,
) -> EnergyNeeds:
    day = on or date.today()
    age = calc_age(settings.user_birthdate, day)
    method = (settings.bmr_method or "mifflin").lower()

    if method == "picooc" and picooc_bmr and picooc_bmr > 0:
        bmr = float(picooc_bmr)
        source = "picooc"
    elif method == "katch" and body_fat_pct is not None and 3 <= body_fat_pct <= 60:
        bmr = katch_mcardle(weight_kg, body_fat_pct)
        source = "katch"
    elif body_fat_pct is not None and 3 <= body_fat_pct <= 60 and method in {"auto", "katch"}:
        bmr = katch_mcardle(weight_kg, body_fat_pct)
        source = "katch"
    elif picooc_bmr and picooc_bmr > 0 and method == "auto":
        bmr = float(picooc_bmr)
        source = "picooc"
    else:
        bmr = mifflin_st_jeor(weight_kg, settings.user_height_cm, age, settings.user_sex)
        source = "mifflin"

    factor = activity_factor if activity_factor is not None else settings.activity_factor
    tdee = bmr * factor + max(0.0, activity_kcal)
    return EnergyNeeds(
        age=age,
        weight_kg=weight_kg,
        height_cm=settings.user_height_cm,
        bmr=bmr,
        base_factor=factor,
        activity_kcal=max(0.0, activity_kcal),
        tdee=tdee,
        effective_factor=(tdee / bmr) if bmr else factor,
        bmr_source=source,
    )


def fmt_num(value: float, digits: int = 1) -> str:
    if digits <= 0:
        return str(int(round(value)))
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
