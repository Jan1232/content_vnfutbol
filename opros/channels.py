"""Каналы опросов / versus / футбольных квизов."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings


@dataclass(frozen=True)
class Channel:
    key: str
    title: str
    theme: str
    kind: str  # yesno | football_or | football_who | quiz


CHANNELS: dict[str, Channel] = {
    "18plus": Channel(
        key="18plus",
        title="Опросы 18+",
        kind="yesno",
        theme=(
            "Взрослые / 18+ / провокационные вопросы про секс, отношения, "
            "табу, откровенные привычки. Без прямой порнографии и без "
            "упоминания несовершеннолетних. Тон дерзкий, но анонимный опрос."
        ),
    ),
    "lichnoe": Channel(
        key="lichnoe",
        title="Опросы о личном",
        kind="yesno",
        theme=(
            "Личное: быт, привычки, деньги, работа, дружба, самооценка, "
            "мелкие стыдные моменты и повседневные дилеммы. Без 18+ и без "
            "политики."
        ),
    ),
    "anon": Channel(
        key="anon",
        title="Опросы анонимно",
        kind="yesno",
        theme=(
            "Общие житейские анонимные вопросы: мнения, выборы, странные "
            "привычки, «а вы тоже…?». Лёгкий вайб, массово-узнаваемо. "
            "Без жёсткого 18+."
        ),
    ),
    "football_or": Channel(
        key="football_or",
        title="Футбол опрос",
        kind="football_or",
        theme=(
            "Футбольный versus: две стороны на выбор (клубы, игроки, "
            "тренеры, эпохи). Формат вопроса «A или B?». Популярные и "
            "узнаваемые пары, без политики."
        ),
    ),
    "football_who": Channel(
        key="football_who",
        title="Футбол кто круче?",
        kind="football_who",
        theme=(
            "Футбольный versus «кто круче»: две стороны (клубы, игроки, "
            "тренеры). Формат «Кто круче: A или B?». Спорные, но узнаваемые "
            "пары. Без политики."
        ),
    ),
    "quiz_apl": Channel(
        key="quiz_apl",
        title="Футбольные квизы",
        kind="quiz",
        theme=(
            "Футбольные квизы: угадай игрока по клубной карьере или угадай "
            "клуб по стране/году/титулам/факту. Узнаваемые, но не слишком "
            "очевидные. Без политики."
        ),
    ),
    "quiz_fifa": Channel(
        key="quiz_fifa",
        title="Угадай футболиста",
        kind="quiz",
        theme=(
            "Футбольные квизы: угадай игрока по клубной карьере или угадай "
            "клуб по стране/году/титулам/факту. Узнаваемые, но не слишком "
            "очевидные. Без политики."
        ),
    ),
}

FOOTBALL_KEYS = ("football_or", "football_who")
QUIZ_KEYS = ("quiz_apl", "quiz_fifa")


def channel_keys() -> tuple[str, ...]:
    return tuple(CHANNELS.keys())


def get_channel(key: str) -> Channel:
    ch = CHANNELS.get(key)
    if ch is None:
        raise KeyError(f"unknown opros channel: {key}")
    return ch


def is_football(key: str) -> bool:
    return get_channel(key).kind.startswith("football")


def is_quiz(key: str) -> bool:
    return get_channel(key).kind == "quiz"


def chat_id_for(key: str) -> int:
    settings = get_settings()
    mapping = {
        "18plus": int(settings.opros_chat_18plus),
        "lichnoe": int(settings.opros_chat_lichnoe),
        "anon": int(settings.opros_chat_anon),
        "football_or": int(settings.opros_chat_football_or),
        "football_who": int(settings.opros_chat_football_who),
        "quiz_apl": int(settings.opros_chat_quiz_apl),
        "quiz_fifa": int(settings.opros_chat_quiz_fifa),
    }
    if key not in mapping:
        raise KeyError(f"unknown opros channel: {key}")
    return mapping[key]
