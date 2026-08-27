"""Пул структурных эмодзи (стикеров) для абзацев editorial-постов."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import ROOT, get_settings

_DEFAULT_STICKERS = (
    "⚽",
    "⚽️",
    "✍️",
    "🏆",
    "🚑",
    "🟥",
    "🟢",
    "✈️",
    "🤩",
    "🏒",
    "🏟️",
    "🔥",
    "💪",
    "👏",
    "✅",
    "❌",
    "🎯",
    "⭐️",
    "⭐",
    "💰",
    "👀",
    "🇷🇺",
    "🇧🇷",
    "🇪🇸",
    "🇮🇹",
    "🇩🇪",
    "🇫🇷",
)

# Базовые блоки Unicode + частые одиночные символы из старого whitelist.
_EMOJI_RE = re.compile(
    r"(?:"
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E0-\U0001F1FF"
    r"\U0001F900-\U0001F9FF\U0001FA70-\U0001FAFF"
    r"⚽✍🏆🚑🟥🟢✈🤩🏒🏟🔥💪👏✅❌🎯⭐💰👀🤯"
    r"]"
    r"[\uFE00-\uFE0F\U0001F3FB-\U0001F3FF\u200D]*"
    r")+",
    re.UNICODE,
)


def pool_path() -> Path:
    settings = get_settings()
    base = Path(getattr(settings, "moderation_feedback_dir", None) or ROOT / "data/editorial/feedback/moderation")
    return base.parent / "sticker_pool.json"


def load_pool() -> list[str]:
    path = pool_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            items = raw.get("stickers") if isinstance(raw, dict) else raw
            if isinstance(items, list):
                out: list[str] = []
                for item in items:
                    s = str(item or "").strip()
                    if s and s not in out:
                        out.append(s[:16])
                if out:
                    return out
        except Exception:
            pass
    return list(_DEFAULT_STICKERS)


def save_pool(stickers: list[str]) -> None:
    path = pool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    uniq: list[str] = []
    for item in stickers:
        s = str(item or "").strip()
        if s and s not in uniq:
            uniq.append(s[:16])
    path.write_text(json.dumps({"stickers": uniq}, ensure_ascii=False, indent=2), encoding="utf-8")


def pool_for_prompt(limit: int = 24) -> list[str]:
    return load_pool()[:limit]


def leading_emoji(text: str) -> str:
    s = (text or "").lstrip()
    if not s:
        return ""
    m = _EMOJI_RE.match(s)
    return m.group(0) if m else ""


def leading_stickers_in_text(text: str) -> list[str]:
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text or ""):
        p = para.strip()
        if not p:
            continue
        lead = leading_emoji(p)
        if lead and lead not in out:
            out.append(lead)
    return out


def paragraph_has_lead_sticker(para: str) -> bool:
    p = (para or "").strip()
    if not p:
        return False
    return bool(leading_emoji(p))


def is_emoji_only_paragraph(para: str) -> bool:
    p = (para or "").strip()
    if not p:
        return False
    rest = _EMOJI_RE.sub("", p)
    rest = re.sub(r"[\s\u200d\ufe0f]", "", rest)
    return not rest and bool(_EMOJI_RE.search(p))


def has_long_emoji_run(text: str) -> bool:
    for m in _EMOJI_RE.finditer(text or ""):
        if len(m.group(0)) >= 3:
            return True
    return False


def register_from_text(text: str) -> list[str]:
    """Добавить эмодзи из правки модератора в общий пул."""
    found = leading_stickers_in_text(text)
    if not found:
        return []
    pool = load_pool()
    added: list[str] = []
    for sticker in found:
        if sticker not in pool:
            pool.append(sticker)
            added.append(sticker)
    if added:
        save_pool(pool)
    return added
