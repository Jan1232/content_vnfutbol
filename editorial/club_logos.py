"""Маппинг названий клубов → файлы гербов для шаблона результата."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import ROOT, get_settings

_LOGO_DIR = ROOT / "editorial" / "templates" / "assets" / "logos"
_MISSING_LOG = ROOT / "data" / "editorial" / "missing_logos.jsonl"

_STRIP = re.compile(
    r"(?i)\b(?:fc|fk|sc|ac|cf|cd|sd|ss|as|sk|bk|afc|bfc|cfc|s\.?k\.?)\b|[\.\,\'\"]"
)


def logos_json_path() -> Path:
    settings = get_settings()
    raw = getattr(settings, "club_logos", None)
    path = Path(raw) if raw else ROOT / "editorial" / "club_logos.json"
    return path if path.is_absolute() else ROOT / path


def logo_dir() -> Path:
    return _LOGO_DIR


@lru_cache
def _catalog() -> dict[str, dict[str, Any]]:
    path = logos_json_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    clubs = data.get("clubs") if isinstance(data, dict) else data
    if not isinstance(clubs, dict):
        return {}
    return clubs


def reload_catalog() -> None:
    _catalog.cache_clear()


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", (name or "").strip())
    s = _STRIP.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s.replace("ё", "е")


def _alias_index() -> dict[str, str]:
    out: dict[str, str] = {}
    for slug, row in _catalog().items():
        names = [slug.replace("_", " ")]
        if isinstance(row, dict):
            names.extend(str(x) for x in (row.get("names") or []) if str(x).strip())
        for n in names:
            key = normalize_name(n)
            if key:
                out[key] = slug
    return out


def find_slug(name: str) -> str | None:
    raw = (name or "").strip()
    if not raw:
        return None
    idx = _alias_index()
    key = normalize_name(raw)
    if key in idx:
        return idx[key]
    for alias, slug in idx.items():
        if len(alias) >= 4 and (alias in key or key in alias):
            return slug
    return None


def logo_file_for_slug(slug: str) -> Path | None:
    row = _catalog().get(slug) or {}
    fname = str(row.get("file") or f"{slug}.png")
    path = logo_dir() / fname
    return path if path.is_file() else None


def resolve_team_logo(team_name: str, *, allow_fallback: bool = False) -> dict[str, Any]:
    slug = find_slug(team_name)
    if not slug:
        log_missing_logo(team_name)
        if allow_fallback:
            return {"slug": "", "team": team_name, "path": "", "missing": True}
        return {"slug": "", "team": team_name, "path": "", "missing": True}
    path = logo_file_for_slug(slug)
    if not path:
        log_missing_logo(team_name, slug=slug)
        if allow_fallback:
            return {"slug": slug, "team": team_name, "path": "", "missing": True}
        return {"slug": slug, "team": team_name, "path": "", "missing": True}
    return {"slug": slug, "team": team_name, "path": str(path), "missing": False}


def resolve_pair(home: str, away: str) -> tuple[dict[str, Any], dict[str, Any]]:
    settings = get_settings()
    fb = bool(getattr(settings, "result_logo_fallback", False))
    return resolve_team_logo(home, allow_fallback=fb), resolve_team_logo(away, allow_fallback=fb)


def log_missing_logo(name: str, *, slug: str = "") -> None:
    _MISSING_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"team": name, "slug": slug}
    try:
        with _MISSING_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
