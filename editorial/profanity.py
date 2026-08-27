"""Profanity filter for editorial posts (RU + EN roots)."""

from __future__ import annotations

import re
from typing import Iterable

_RU_ROOTS: tuple[str, ...] = (
    "хуй",
    "хуе",
    "хуё",
    "хуи",
    "пизд",
    "ебан",
    "ебал",
    "ебат",
    "ёбан",
    "ёбал",
    "бляд",
    "блят",
    "блять",
    "сука",
    "мудил",
    "мудак",
    "пидор",
    "пидар",
    "ебл",
    "залуп",
    "говно",
    "дерьм",
)

_EN: tuple[str, ...] = (
    "fuck",
    "fucking",
    "shit",
    "bitch",
    "asshole",
    "dick",
    "cunt",
    "motherfucker",
    "nigger",
    "nigga",
)

_PAT = re.compile(
    "(?i)(" + "|".join(re.escape(x) for x in _RU_ROOTS + _EN) + ")"
)

# Длинные фразы — раньше коротких корней.
_EXPLICIT: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)motherfucker"), ""),
    (re.compile(r"(?i)fucking"), ""),
    (re.compile(r"(?i)fuck"), ""),
    (re.compile(r"(?i)asshole"), ""),
    (re.compile(r"(?i)bitch"), ""),
    (re.compile(r"(?i)shit"), "блин"),
    (re.compile(r"(?i)cunt"), ""),
    (re.compile(r"(?i)nigg\w*"), ""),
    (re.compile(r"(?i)ебало"), "лицо"),
    (re.compile(r"(?i)ебло"), "лицо"),
    (re.compile(r"(?i)хуйло"), "фигня"),
    (re.compile(r"(?i)пиздец"), "жесть"),
    (re.compile(r"(?i)блять"), ""),
    (re.compile(r"(?i)бляд\w*"), ""),
    (re.compile(r"(?i)сука"), ""),
    (re.compile(r"(?i)мудак"), "дурак"),
    (re.compile(r"(?i)мудил\w*"), "дурак"),
    (re.compile(r"(?i)пидор\w*"), ""),
    (re.compile(r"(?i)пидар\w*"), ""),
    (re.compile(r"(?i)залуп\w*"), ""),
    (re.compile(r"(?i)говно"), "фигня"),
    (re.compile(r"(?i)дерьм\w*"), "фигня"),
    (re.compile(r"(?i)ебан\w*"), ""),
    (re.compile(r"(?i)ёбан\w*"), ""),
    (re.compile(r"(?i)ебал\w*"), ""),
    (re.compile(r"(?i)ёбал\w*"), ""),
    (re.compile(r"(?i)ебат\w*"), ""),
    (re.compile(r"(?i)ебл\w*"), "лицо"),
    (re.compile(r"(?i)ху[йёяи]\w*"), ""),
    (re.compile(r"(?i)пизд\w*"), "жесть"),
)

_WS = re.compile(r"[ \t]{2,}")
_SPACE_PUNCT = re.compile(r"\s+([,.!?;:])")


def replace_profanity(text: str) -> str:
    """Заменить мат на нейтральные слова; не переписывать смысл."""
    if not text:
        return ""
    out = str(text)
    for rx, repl in _EXPLICIT:
        out = rx.sub(repl, out)
    out = _PAT.sub("", out)
    out = _SPACE_PUNCT.sub(r"\1", out)
    out = _WS.sub(" ", out)
    return out.strip()


def contains_profanity(text: str) -> bool:
    return bool(_PAT.search(text or ""))


def any_profanity(parts: Iterable[str]) -> bool:
    return any(contains_profanity(p) for p in parts if p)


def strip_profanity(text: str) -> str:
    return replace_profanity(text)


def profanity_ok(text: str) -> tuple[bool, str]:
    if contains_profanity(text or ""):
        return False, "profanity"
    return True, "ok"
