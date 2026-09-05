# -*- coding: utf-8 -*-
"""Нормализация имён клубов/игроков для event-дедупа."""

from __future__ import annotations

import re
import unicodedata

# Алиас (нижний регистр) → каноническое имя
CLUB_ALIASES: dict[str, str] = {
    "барса": "Барселона",
    "барселона": "Барселона",
    "барселоны": "Барселона",
    "барселону": "Барселона",
    "fcb": "Барселона",
    "barca": "Барселона",
    "barcelona": "Барселона",
    "реал": "Реал Мадрид",
    "реал мадрид": "Реал Мадрид",
    "мадрид": "Реал Мадрид",
    "real": "Реал Мадрид",
    "real madrid": "Реал Мадрид",
    "малага": "Малага",
    "malaga": "Малага",
    "мью": "Манчестер Юнайтед",
    "мю": "Манчестер Юнайтед",
    "манчестер юнайтед": "Манчестер Юнайтед",
    "юнайтед": "Манчестер Юнайтед",
    "man utd": "Манчестер Юнайтед",
    "manchester united": "Манчестер Юнайтед",
    "сити": "Манчестер Сити",
    "ман сити": "Манчестер Сити",
    "манчестер сити": "Манчестер Сити",
    "manchester city": "Манчестер Сити",
    "man city": "Манчестер Сити",
    "арсенал": "Арсенал",
    "arsenal": "Арсенал",
    "челси": "Челси",
    "chelsea": "Челси",
    "ливерпуль": "Ливерпуль",
    "liverpool": "Ливерпуль",
    "тоттенхэм": "Тоттенхэм",
    "тоттенхем": "Тоттенхэм",
    "шпоры": "Тоттенхэм",
    "tottenham": "Тоттенхэм",
    "псж": "ПСЖ",
    "париж": "ПСЖ",
    "psg": "ПСЖ",
    "бавария": "Бавария",
    "bayern": "Бавария",
    "ювентус": "Ювентус",
    "juventus": "Ювентус",
    "милан": "Милан",
    "milan": "Милан",
    "интер": "Интер",
    "inter": "Интер",
    "атлетико": "Атлетико",
    "атлетико мадрид": "Атлетико",
    "зенит": "Зенит",
    "спартак": "Спартак",
    "цска": "ЦСКА",
    "локомотив": "Локомотив",
    "динамо": "Динамо",
    "трабзонспор": "Трабзонспор",
    "trabzonspor": "Трабзонспор",
    "монако": "Монако",
    "monaco": "Монако",
}

PLAYER_ALIASES: dict[str, str] = {
    "беллингем": "Беллингем",
    "джейд беллингем": "Беллингем",
    "джуд беллингем": "Беллингем",
    "bellingham": "Беллингем",
    "ришарлисон": "Ришарлисон",
    "richarlison": "Ришарлисон",
    "мбаппе": "Мбаппе",
    "mbappe": "Мбаппе",
    "месси": "Месси",
    "messi": "Месси",
    "головин": "Головин",
    "ямаль": "Ямаль",
    "ламин ямаль": "Ямаль",
}


def _strip_quotes(s: str) -> str:
    return s.strip().strip("«»\"'`")


def _fold(s: str) -> str:
    s = _strip_quotes(s).lower().replace("ё", "е")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_club(name: str | None) -> str | None:
    if not name:
        return None
    key = _fold(name)
    if key in CLUB_ALIASES:
        return CLUB_ALIASES[key]
    # убрать префикс «ФК »
    key2 = re.sub(r"^(фк|fc)\s+", "", key)
    if key2 in CLUB_ALIASES:
        return CLUB_ALIASES[key2]
    # Title-case fallback
    return _strip_quotes(name).title() if name.isascii() else _strip_quotes(name)


def normalize_player(name: str | None) -> str | None:
    if not name:
        return None
    key = _fold(name)
    if key in PLAYER_ALIASES:
        return PLAYER_ALIASES[key]
    # фамилия как ключ
    parts = key.split()
    if parts and parts[-1] in PLAYER_ALIASES:
        return PLAYER_ALIASES[parts[-1]]
    return _strip_quotes(name)


def normalize_score(score: str | None) -> str | None:
    if not score:
        return None
    s = score.strip().replace("–", ":").replace("-", ":").replace(" ", "")
    m = re.match(r"^(\d+):(\d+)$", s)
    if not m:
        return score.strip()
    return f"{int(m.group(1))}:{int(m.group(2))}"


def normalize_teams(teams: list | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in teams or []:
        n = normalize_club(t if isinstance(t, str) else None)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return sorted(out)


def normalize_event(event: dict | None) -> dict:
    """Нормализует сущности события на месте (копия)."""
    if not event:
        return {
            "teams": [],
            "player": None,
            "to_club": None,
            "score": None,
            "minute": None,
            "event_kind": "other",
        }
    kind = event.get("event_kind") or "other"
    minute = event.get("minute")
    if minute is not None:
        try:
            minute = int(minute)
        except (TypeError, ValueError):
            minute = None
    return {
        "teams": normalize_teams(event.get("teams")),
        "player": normalize_player(event.get("player")),
        "to_club": normalize_club(event.get("to_club")),
        "score": normalize_score(event.get("score") if isinstance(event.get("score"), str) else (
            str(event["score"]) if event.get("score") is not None else None
        )),
        "minute": minute,
        "event_kind": kind,
    }
