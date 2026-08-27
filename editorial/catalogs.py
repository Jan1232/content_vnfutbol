"""Catalogs: clubs, players, FIFA top-100, team-name aliases."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT, get_settings

_WS = re.compile(r"\s+")


def norm_name(value: str) -> str:
    t = (value or "").strip().lower()
    t = t.replace("ё", "е")
    t = t.replace("é", "e").replace("è", "e").replace("ê", "e")
    t = t.replace("á", "a").replace("à", "a").replace("ã", "a")
    t = t.replace("ó", "o").replace("ö", "o").replace("ø", "o")
    t = t.replace("ü", "u").replace("ú", "u")
    t = t.replace("ñ", "n").replace("ç", "c").replace("ş", "s")
    t = t.replace("ı", "i").replace("ï", "i")
    t = t.replace("-", " ").replace("'", " ").replace("’", " ")
    t = t.replace(".", " ")
    t = _WS.sub(" ", t).strip()
    for suffix in (" fc", " cf", " fk", " sk", " ac", " sc", " afc", " cfc"):
        if t.endswith(suffix) and len(t) > len(suffix) + 2:
            t = t[: -len(suffix)].strip()
    return t


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or None


@lru_cache
def load_team_aliases() -> dict[str, str]:
    """normalized alias → canonical English name."""
    path = ROOT / "seo" / "team_names_ru.yaml"
    raw = _read_yaml(path) or {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for en, ru in raw.items():
        canonical = str(en).strip()
        if not canonical:
            continue
        out[norm_name(canonical)] = canonical
        if ru:
            out[norm_name(str(ru))] = canonical
    return out


@lru_cache
def load_clubs() -> dict[str, list[str]]:
    """league code → list of canonical club names."""
    settings = get_settings()
    path = Path(settings.clubs_file)
    if not path.is_file():
        path = ROOT / "editorial" / "clubs.yaml"
    raw = _read_yaml(path) or {}
    leagues = raw.get("leagues") if isinstance(raw, dict) else {}
    out: dict[str, list[str]] = {}
    if not isinstance(leagues, dict):
        return out
    for league, clubs in leagues.items():
        names: list[str] = []
        if isinstance(clubs, list):
            for item in clubs:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict) and item.get("name"):
                    names.append(str(item["name"]))
        out[str(league)] = names
    return out


@lru_cache
def grand_clubs() -> set[str]:
    names: set[str] = set()
    for clubs in load_clubs().values():
        for name in clubs:
            names.add(norm_name(name))
            aliases = load_team_aliases()
            # also the canonical itself
            names.add(norm_name(aliases.get(norm_name(name), name)))
    return names


@lru_cache
def load_players() -> dict[str, str]:
    """normalized alias → canonical player name."""
    path = ROOT / "editorial" / "players_ru.yaml"
    raw = _read_yaml(path) or {}
    out: dict[str, str] = {}
    players = raw.get("players") if isinstance(raw, dict) else raw
    if not isinstance(players, (dict, list)):
        return out
    if isinstance(players, list):
        for item in players:
            if isinstance(item, str):
                out[norm_name(item)] = item
            elif isinstance(item, dict):
                name = str(item.get("name") or item.get("en") or "").strip()
                if not name:
                    continue
                out[norm_name(name)] = name
                for alias in item.get("aliases") or []:
                    out[norm_name(str(alias))] = name
                if item.get("ru"):
                    out[norm_name(str(item["ru"]))] = name
        return out
    for key, val in players.items():
        name = str(key).strip()
        out[norm_name(name)] = name
        if isinstance(val, str):
            out[norm_name(val)] = name
        elif isinstance(val, (list, tuple)):
            for alias in val:
                out[norm_name(str(alias))] = name
        elif isinstance(val, dict):
            if val.get("ru"):
                out[norm_name(str(val["ru"]))] = name
            for alias in val.get("aliases") or []:
                out[norm_name(str(alias))] = name
    return out


@lru_cache
def load_fifa_top100_names() -> set[str]:
    settings = get_settings()
    path = Path(settings.fifa_top100_file)
    if not path.is_file():
        path = ROOT / "editorial" / "fifa_top100.yaml"
    raw = _read_yaml(path) or {}
    teams = raw.get("teams") if isinstance(raw, dict) else raw
    names: set[str] = set()
    if isinstance(teams, list):
        for item in teams:
            if isinstance(item, str):
                names.add(norm_name(item))
            elif isinstance(item, dict):
                for key in ("team", "en", "name", "team_ru", "ru"):
                    if item.get(key):
                        names.add(norm_name(str(item[key])))
    aliases = load_team_aliases()
    extra: set[str] = set(names)
    for alias, canonical in aliases.items():
        if norm_name(canonical) in names or alias in names:
            extra.add(alias)
            extra.add(norm_name(canonical))
    return extra


FOOTBALL_ORGS = {
    "fifa",
    "uefa",
    "conmebol",
    "afc",
    "caf",
    "concacaf",
    "premier league",
    "премьер лига",
    "апл",
    "ла лига",
    "laliga",
    "la liga",
    "серия а",
    "serie a",
    "бундеслига",
    "bundesliga",
    "лига 1",
    "ligue 1",
    "рпл",
    "лига чемпионов",
    "champions league",
    "лига европы",
    "europa league",
    "чемпионат мира",
    "world cup",
    "евро",
    "euro 2024",
    "euro 2028",
    "кубок мира",
}

COMPETITION_HINTS: dict[str, str] = {
    "champions league": "CL",
    "лига чемпионов": "CL",
    "ucl": "CL",
    "europa league": "EL",
    "лига европы": "EL",
    "conference league": "ECL",
    "premier league": "PL",
    "премьер-лига": "PL",
    "апл": "PL",
    "la liga": "PD",
    "laliga": "PD",
    "ла лига": "PD",
    "serie a": "SA",
    "серия а": "SA",
    "bundesliga": "BL",
    "бундеслига": "BL",
    "ligue 1": "FL1",
    "лига 1": "FL1",
    "рпл": "RPL",
    "российская премьер": "RPL",
    "world cup": "WC",
    "чемпионат мира": "WC",
    "евро": "EC",
    "euro": "EC",
    "nations league": "UNL",
    "лига наций": "UNL",
}


def canonical_team(name: str) -> str:
    aliases = load_team_aliases()
    key = norm_name(name)
    return aliases.get(key) or name.strip()


def is_grand(team: str) -> bool:
    return norm_name(canonical_team(team)) in grand_clubs() or norm_name(team) in grand_clubs()


@lru_cache
def load_team_ru() -> dict[str, str]:
    """normalized english/alias → русское имя из seo/team_names_ru.yaml."""
    path = ROOT / "seo" / "team_names_ru.yaml"
    raw = _read_yaml(path) or {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for en, ru in raw.items():
        label = str(ru or "").strip()
        if not label:
            continue
        out[norm_name(str(en))] = label
    return out


def team_display_ru(name: str) -> str:
    canonical = canonical_team(name)
    ru = load_team_ru().get(norm_name(canonical)) or load_team_ru().get(norm_name(name))
    if ru:
        return ru[:1].upper() + ru[1:]
    return canonical or (name or "").strip()


def detect_competition(text: str) -> str:
    blob = norm_name(text)
    for hint, code in COMPETITION_HINTS.items():
        if hint in blob:
            return code
    return ""


def reload_catalogs() -> None:
    load_team_aliases.cache_clear()
    load_clubs.cache_clear()
    grand_clubs.cache_clear()
    load_players.cache_clear()
    load_fifa_top100_names.cache_clear()
    load_team_ru.cache_clear()
