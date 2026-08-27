"""Football-only topic gate: rule pre-filter, then LLM."""

from __future__ import annotations

import re
from typing import Any

from editorial.catalogs import (
    COMPETITION_HINTS,
    FOOTBALL_ORGS,
    canonical_team,
    load_fifa_top100_names,
    load_players,
    load_team_aliases,
    norm_name,
)
from editorial.models import NewsItem

_TOKEN_SPLIT = re.compile(r"[^\wа-яё]+", re.IGNORECASE)

LIFESTYLE_KEYS = (
    "татуиров",
    "tattoo",
    "прическ",
    "стрижк",
    "свадьб",
    "свадебн",
    "женился",
    "женилась",
    "поженились",
    "родила",
    "беремен",
    "брачный договор",
)


EVENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("lifestyle", LIFESTYLE_KEYS),
    (
        "transfer",
        (
            "transfer",
            "трансфер",
            "signed",
            "подписал",
            "подписан",
            "fee",
            "отступн",
            "loan",
            "аренда",
            "аренду",
            "released",
            "расторг",
            "контракт",
            "переход",
            "перейд",
            "покинет",
            "покидает",
            "хирвигоу",
            "here we go",
            "опцие",
            "опцией выкуп",
            "обязательным выкуп",
            "миллионов евро",
            "миллионов фунт",
            "ведёт переговоры",
            "ведет переговоры",
            "предложение по",
        ),
    ),
    (
        "injury",
        (
            "injur",
            "травм",
            "hamstring",
            "acl",
            "fracture",
            "перелом",
            "out for",
            "выбыл",
            "диагностир",
            "diagnos",
            "порванные крест",
            "увезли на коляске",
        ),
    ),
    (
        "match_result",
        (
            "full-time",
            "full time",
            "ft:",
            "побед",
            "проиграл",
            "ничья",
            "забил",
            "гол ",
            " score",
            "счёт",
            "счет",
            " — ",
            " - ",
            "won ",
            "beats ",
            "beat ",
            "обладатель",
            "суперкубк",
            "дубль оформля",
        ),
    ),
    ("lineup", (
        "line-up",
        "lineup",
        "starting xi",
        "стартовый состав",
        "стартовые состав",
        "стартовые xi",
        "состав на матч",
        "составы на матч",
        "составы на встречу",
        "стартовые составы",
    )),
    (
        "official_statement",
        ("official statement", "официальн", "club statement", "заявлен клуба", "press release"),
    ),
    ("rumor", ("rumor", "rumour", "according to sources", "слухи", "источники сообщают", " reportedly")),
]

# Жёсткие новости — из мем-фида не берём (по логам модерации SoccerBlog)
MEME_HARD_EVENT_TYPES = frozenset(
    {"transfer", "injury", "match_result", "lineup", "official_statement"}
)

OFF_TOPIC_HINTS = (
    "баскетбол",
    "basketball",
    "нба",
    "nba",
    "хоккей",
    "hockey",
    "nhl",
    "теннис",
    "tennis",
    "формула-1",
    "formula 1",
    "mma",
    "ufc",
    "боксёр",
    "boxing",
    "киберспорт",
    "esports",
    "cs2",
    "dota",
    "госдум",
    "выборы",
    "политик",
    "криминал",
    "убийств",
    "ставки на спорт",
    "каппер",
    "букмекер",
)


def classify_event_rules(text: str) -> str:
    blob = (text or "").lower()
    for event_type, keys in EVENT_RULES:
        if any(k in blob for k in keys):
            return event_type
    return "other"


def classify_meme_event(text: str) -> str:
    """Классификация текста мем-поста: hard-news vs lifestyle."""
    blob = (text or "").lower()
    for event_type, keys in EVENT_RULES:
        if event_type == "lifestyle":
            # lifestyle-ключи не режут мем-фид; они слишком узкие (тату/свадьба)
            continue
        if event_type == "match_result":
            keys = tuple(
                k for k in keys if k.strip() not in {"—", "-", "—", "–"} and k not in {" — ", " - ", " – "}
            )
        if any(k in blob for k in keys):
            return event_type
    return "lifestyle"


def classify_soccerblog_event(text: str) -> str:
    """Alias: SoccerBlog — только lifestyle после фильтра hard-news."""
    return classify_meme_event(text)



def extract_entities(text: str) -> dict[str, Any]:
    blob = norm_name(text)
    raw = text or ""
    teams: list[str] = []
    players: list[str] = []
    seen_t: set[str] = set()
    seen_p: set[str] = set()

    aliases = load_team_aliases()
    # longer aliases first so "manchester united" beats "united"
    for alias, canonical in sorted(aliases.items(), key=lambda kv: len(kv[0]), reverse=True):
        if len(alias) < 4:
            continue
        if alias in blob and canonical not in seen_t:
            seen_t.add(canonical)
            teams.append(canonical)

    for alias, canonical in sorted(load_players().items(), key=lambda kv: len(kv[0]), reverse=True):
        if len(alias) < 4:
            continue
        if alias in blob and canonical not in seen_p:
            seen_p.add(canonical)
            players.append(canonical)

    orgs = [name for name in FOOTBALL_ORGS if name in blob]
    competition = ""
    for hint, code in COMPETITION_HINTS.items():
        if hint in blob:
            competition = code
            break

    fifa = load_fifa_top100_names()
    is_national = False
    national_hits = [t for t in teams if norm_name(t) in fifa or norm_name(canonical_team(t)) in fifa]
    if national_hits and len(teams) <= 3:
        is_national = True
    # Russia is not in FIFA table but is a national team
    if any(norm_name(t) in {"russia", "россия", "рф"} for t in teams) or "сборная россии" in blob:
        is_national = True
        if not any(norm_name(canonical_team(t)) == "russia" or norm_name(t) == "russia" for t in teams):
            teams.append("Russia")

    return {
        "teams": teams[:8],
        "players": players[:8],
        "orgs": orgs[:6],
        "competition": competition,
        "is_national": is_national,
        "event_type": classify_event_rules(raw),
    }


def has_football_entity(entities: dict[str, Any], extra_teams: tuple[str, ...] = ()) -> bool:
    if entities.get("teams") or entities.get("players") or entities.get("orgs"):
        return True
    extra = {norm_name(t) for t in extra_teams}
    for team in entities.get("teams") or []:
        if norm_name(team) in extra:
            return True
    return False


def cluster_id_for(item: NewsItem | dict[str, Any]) -> str:
    if isinstance(item, NewsItem):
        event_type = item.event_type or (item.entities or {}).get("event_type") or "other"
        entities = item.entities or {}
        published = item.published_at.strftime("%Y-%m-%d") if item.published_at else ""
    else:
        event_type = item.get("event_type") or "other"
        try:
            import json

            entities = json.loads(item.get("entities_json") or "{}")
        except Exception:
            entities = {}
        published = str(item.get("source_published_at") or "")[:10]

    players = [norm_name(canonical_team(p)) for p in (entities.get("players") or [])]
    teams = [norm_name(canonical_team(t)) for t in (entities.get("teams") or [])]
    teams = sorted({t for t in teams if t})
    players = sorted({p for p in players if p})

    if event_type in {"injury", "transfer"} and players:
        key = f"{event_type}:{players[0]}"
    elif event_type == "match_result" and len(teams) >= 2:
        key = f"match:{teams[0]}:{teams[1]}:{published}"
    elif teams:
        key = f"{event_type}:{':'.join(teams[:2])}:{published}"
    elif players:
        key = f"{event_type}:{players[0]}"
    else:
        title = (item.title if isinstance(item, NewsItem) else item.get("title") or "")[:80]
        key = f"{event_type}:{norm_name(title)}"
    return key[:180]


def rule_prefilter(item: NewsItem, extra_teams: tuple[str, ...] = ()) -> tuple[bool, str]:
    text = f"{item.title}\n{item.body}"
    blob = text.lower()
    if any(h in blob for h in OFF_TOPIC_HINTS):
        # still allow if a football entity is clearly present (e.g. "Messi watches NBA")
        if not has_football_entity(item.entities, extra_teams):
            return False, "off-topic keyword without football entity"
    if has_football_entity(item.entities, extra_teams):
        return True, "entity match"
    return False, "no football entity"


def check(item: NewsItem, extra_teams: tuple[str, ...] = (), *, use_llm: bool = True) -> tuple[bool, str, dict[str, Any]]:
    """Return (is_football, reason, llm_payload)."""
    ok, reason = rule_prefilter(item, extra_teams)
    if not ok:
        return False, reason, {}
    if not use_llm:
        return True, reason, {"is_football": True, "subtype": "other", "reason": reason}
    from editorial import llm

    try:
        payload = llm.topic_check(item.title, item.body, item.entities)
    except Exception as e:
        # fail closed only if pre-filter was weak; pre-filter passed → allow with note
        return True, f"llm topic fail, keep by prefilter: {e}", {}
    if payload.get("is_football") is False:
        return False, str(payload.get("reason") or "llm off_topic"), payload
    return True, str(payload.get("reason") or "llm football"), payload
