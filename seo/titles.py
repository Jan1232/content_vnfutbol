"""SEO channel title builder (Wordstat-style)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import ROOT

_DEFAULT_SUFFIX = "матч смотреть онлайн прямая трансляция бесплатно"
_MAX_TITLE = 200


@lru_cache
def _team_map() -> dict[str, str]:
    path = ROOT / "seo" / "team_names_ru.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if k and v:
                out[str(k).strip().casefold()] = str(v).strip().lower()
    return out


def reload_team_names() -> None:
    _team_map.cache_clear()


def _strip_fc_suffix(name: str) -> str:
    t = (name or "").strip()
    t = re.sub(
        r"(?iu)\b(fc|cf|fk|sk|ac|as|sc|rcd?|afc|ssc|bsc|gnk|pae|sfp)\b\.?",
        "",
        t,
    )
    t = re.sub(r"\s{2,}", " ", t).strip(" .-")
    return t


def team_name_ru(official: str) -> str:
    raw = (official or "").strip()
    if not raw:
        return ""
    m = _team_map()
    key = raw.casefold()
    if key in m:
        return m[key]
    # try without FC/CF
    stripped = _strip_fc_suffix(raw)
    if stripped.casefold() in m:
        return m[stripped.casefold()]
    # partial contains
    for eng, ru in m.items():
        if eng and (eng in key or key in eng):
            return ru
    # fallback: lowercase transliteration-ish keep as latin lower
    return stripped.casefold()


def team_display_ru(official: str) -> str:
    """Имя клуба/сборной для поста: с заглавной (ПСЖ → ПСЖ, бенфика → Бенфика)."""
    ru = team_name_ru(official).strip()
    if not ru:
        return ""
    # Короткие аббревиатуры (латиница или известные кириллические)
    acronyms = {"псж", "цска", "сша", "рф", "оаэ", "кнр"}
    if ru in acronyms:
        return ru.upper()
    if " " not in ru and len(ru) <= 4 and ru.isascii():
        return ru.upper()
    return " ".join(p[:1].upper() + p[1:] if p else "" for p in ru.split())


def build_seo_title(
    home_official: str,
    away_official: str,
    *,
    suffix: str = _DEFAULT_SUFFIX,
    max_len: int = _MAX_TITLE,
) -> str:
    home = team_display_ru(home_official)
    away = team_display_ru(away_official)
    suffix = (suffix or _DEFAULT_SUFFIX).strip().lower()
    core = f"{home} — {away}".strip(" —")
    title = f"{core} {suffix}".strip()
    title = re.sub(r"\s{2,}", " ", title)
    if len(title) <= max_len:
        return title
    # keep SEO suffix, trim team names
    budget = max_len - len(suffix) - 1
    if budget < 8:
        return title[:max_len].rstrip()
    teams = f"{home} — {away}".strip()
    if len(teams) > budget:
        teams = teams[:budget].rstrip(" —")
    return f"{teams} {suffix}".strip()[:max_len]
