"""Caption on the cover image — must not clone the post text."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from editorial import llm
from editorial.cover_text import PROMPT_MAX_WORDS, clip_to_cover

_TOKEN = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_ASCII_QUOTES = re.compile(r'"([^"]+)"')
_DASHES = re.compile(r"\s+[-–]\s+")


def _tokens(text: str) -> list[str]:
    return [t.lower().replace("ё", "е") for t in _TOKEN.findall(text or "")]


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    # token Jaccard + sequence on joined tokens
    sa, sb = set(ta), set(tb)
    jacc = len(sa & sb) / max(1, len(sa | sb))
    seq = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    return max(jacc, seq)


def _normalize_ru_typo(text: str) -> str:
    """Ёлочки, длинное тире — даже если модель дала английские кавычки."""
    t = (text or "").strip()
    if not t:
        return ""
    t = t.replace("“", "«").replace("”", "»").replace("„", "«").replace("‟", "«")
    t = t.replace("‹", "«").replace("›", "»")
    t = _ASCII_QUOTES.sub(r"«\1»", t)
    t = _DASHES.sub(" — ", t)
    t = t.replace("—", " — ")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"«\s+", "«", t)
    t = re.sub(r"\s+»", "»", t)
    t = re.sub(r"»\s*,\s*—", "», —", t)
    return t


def _join_model_caption(data: dict[str, Any]) -> str:
    line1 = str(data.get("caption_line1") or "").strip()
    line2 = str(data.get("caption_line2") or "").strip()
    if line1 and line2:
        return f"{line1} {line2}"
    return line1 or line2


def _finalize(text: str) -> str:
    return clip_to_cover(_normalize_ru_typo(text))


def generate(item: dict[str, Any], post_text: str, *, max_attempts: int = 2) -> dict[str, str | None]:
    try:
        import json

        entities = json.loads(item.get("entities_json") or "{}")
    except Exception:
        entities = {}

    last = ""
    for _ in range(max(1, max_attempts)):
        try:
            data = llm.caption(post_text, entities)
            text = _finalize(_join_model_caption(data))
        except Exception as e:
            print(f"[editorial] caption llm fail: {e}", flush=True)
            continue
        last = text
        if text and similarity(text, post_text) <= 0.6:
            return {"caption_line1": text, "caption_line2": None}
    headline = str(item.get("headline") or item.get("title") or "")
    short = _finalize(headline)
    if similarity(short, post_text) > 0.6:
        words = _tokens(short)[: max(4, PROMPT_MAX_WORDS // 2)]
        short = _finalize(" ".join(words))
    return {"caption_line1": short or last, "caption_line2": None}
