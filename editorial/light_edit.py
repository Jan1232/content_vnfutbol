"""Детерминированная косметика текста донора без LLM (замена rewrite)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT, get_settings
from editorial.cover_text import clip_to_cover
from editorial.editor import normalize_ru_typo
from editorial.profanity import apply_profanity, effective_profanity_mode
from editorial.stickers import (
    _EMOJI_RE,
    has_long_emoji_run,
    is_emoji_only_paragraph,
    leading_emoji,
    leading_stickers_in_text,
    paragraph_has_lead_sticker,
)

_ABBREVS = frozenset(
    {
        "ПСЖ",
        "ЦСКА",
        "МЮ",
        "VAR",
        "АПЛ",
        "ЛЧ",
        "ЛЕ",
        "РПЛ",
        "ФИФА",
        "УЕФА",
        "НХЛ",
        "НБА",
        "НФЛ",
        "МЛС",
        "КХЛ",
        "РФС",
        "РФПЛ",
        "ФК",
        "ГК",
        "ОЗ",
        "ПЗ",
        "НП",
        "ТОП",
        "VIP",
        "CEO",
        "FC",
        "AC",
        "AS",
        "SC",
        "CF",
        "CD",
        "SD",
        "SS",
        "US",
        "UK",
        "EU",
        "IT",
        "TV",
        "HD",
        "UCL",
        "UEL",
    }
)

_CAPS_WORD = re.compile(r"(?<![A-ZА-ЯЁ])[A-ZА-ЯЁ]{2,}(?![a-zа-яё])")
_URL = re.compile(
    r"(?i)(?:https?://|t\.me/|www\.)[^\s<>\"']+"
)
_AT_HANDLE = re.compile(r"(?<!\w)@[a-zA-Z0-9_]{3,32}\b")
_BROKEN_EMOJI = re.compile(
    r"[\uFFFD\uFE0E\u200B\u200C\u2060\uFEFF"
    r"]+"
    r"|(?:\uFE0F(?![\U0001F300-\U0001FAFF\U00002600-\U000027BF]))"
)
_MULTI_NL = re.compile(r"\n{3,}")
_WS = re.compile(r"[ \t]{2,}")

_TOPIC_STICKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(?:гол|гола|голы|матч|счёт|счет|таблиц)\b"), "⚽"),
    (re.compile(r"(?i)\b(?:трансфер|подписал|перешёл|перешел|аренд)\w*\b"), "✍️"),
    (re.compile(r"(?i)(?:€|\$|млн|млрд|миллион|сумм)\w*"), "💰"),
    (re.compile(r"(?i)\b(?:травм|операци|вылетел\w*\s+на)\w*\b"), "🚑"),
    (re.compile(r"(?i)\b(?:сенсаци|шок|скандал)\w*\b"), "🔥"),
    (re.compile(r"(?i)\b(?:трофей|чемпион|кубок|золот\w*\s+мяч)\w*\b"), "🏆"),
    (re.compile(r"(?i)\b(?:слух|инсайд|по\s+данным|сообщает)\w*\b"), "👀"),
    (re.compile(r"(?i)(?:😂|🤣|лол|ахах|ирони|стёб|стеб|мем)"), "😏"),
)


def _strip_path() -> Path:
    settings = get_settings()
    raw = getattr(settings, "light_edit_strip", None)
    path = Path(raw) if raw else ROOT / "editorial" / "light_edit_strip.yaml"
    return path if path.is_absolute() else ROOT / path


@lru_cache
def _strip_rules() -> tuple[list[re.Pattern[str]], list[str]]:
    path = _strip_path()
    if not path.is_file():
        return [], []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return [], []
    pats: list[re.Pattern[str]] = []
    for row in data.get("patterns") or []:
        try:
            pats.append(re.compile(str(row)))
        except re.error:
            continue
    strings = [str(s).strip() for s in (data.get("strings") or []) if str(s).strip()]
    return pats, strings


def _resolve_profanity_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m in {"soften", "strict"}:
        return m
    settings = get_settings()
    le = (getattr(settings, "light_edit_profanity", None) or "").strip().lower()
    if le in {"soften", "strict"}:
        return le
    return effective_profanity_mode()


def _source_text(title: str, body: str) -> str:
    t = (title or "").strip()
    b = (body or "").strip()
    if b:
        if t and t not in b[: max(len(t) + 20, 40)]:
            return f"{t}\n\n{b}"
        return b
    return t


def _fix_caps_word(word: str, *, sentence_start: bool) -> str:
    letters = [ch for ch in word if ch.isalpha()]
    if len(letters) < 2:
        return word
    core = "".join(letters)
    if core.upper() in _ABBREVS:
        return word
    if core != core.upper():
        return word
    if sentence_start:
        lower = core[0] + core[1:].lower()
    else:
        lower = core.lower()
    out: list[str] = []
    li = 0
    for ch in word:
        if ch.isalpha():
            out.append(lower[li])
            li += 1
        else:
            out.append(ch)
    return "".join(out)


def normalize_caps(text: str) -> str:
    if not text:
        return ""

    lines: list[str] = []
    for line in text.split("\n"):
        letters = [ch for ch in line if ch.isalpha()]
        if letters:
            upper = sum(1 for ch in letters if ch.isupper())
            if upper / len(letters) >= 0.75:
                parts: list[str] = []
                pos = 0
                sentence_start = True
                for m in _CAPS_WORD.finditer(line):
                    parts.append(line[pos : m.start()])
                    parts.append(_fix_caps_word(m.group(0), sentence_start=sentence_start))
                    sentence_start = False
                    pos = m.end()
                parts.append(line[pos:])
                line = "".join(parts)
                line = re.sub(
                    r"(?<!\w)([А-ЯЁ])(?!\w)",
                    lambda m: m.group(1).lower(),
                    line,
                )
        lines.append(line)
    return "\n".join(lines)


def strip_branding(text: str) -> str:
    if not text:
        return ""
    out = _URL.sub("", text)
    out = _AT_HANDLE.sub("", out)
    pats, strings = _strip_rules()
    for rx in pats:
        out = rx.sub("", out)
    for s in strings:
        out = re.sub(re.escape(s), "", out, flags=re.IGNORECASE)
    return out


def clean_broken_emoji(text: str) -> str:
    if not text:
        return ""
    return _BROKEN_EMOJI.sub("", text)


def _topic_sticker(para: str) -> str:
    for rx, sticker in _TOPIC_STICKERS:
        if rx.search(para):
            return sticker
    return "⚽"


def _normalize_pool_emoji(text: str) -> str:
    """Удалить только битые fallback; валидные юникод-эмодзи донора не трогаем."""
    return clean_broken_emoji(text)


def _trim_emoji_runs(text: str) -> str:
    if not text:
        return ""

    def trim_run(m: re.Match[str]) -> str:
        run = m.group(0)
        if len(run) <= 2:
            return run
        return run[:2]

    return _EMOJI_RE.sub(trim_run, text)


def _add_paragraph_stickers(text: str) -> str:
    paras: list[str] = []
    for para in re.split(r"\n\s*\n", text or ""):
        p = para.strip()
        if not p:
            continue
        if is_emoji_only_paragraph(p):
            continue
        if not paragraph_has_lead_sticker(p):
            sticker = _topic_sticker(p)
            p = f"{sticker} {p}"
        paras.append(p)
    return "\n\n".join(paras)


def _remove_orphan_paragraphs(text: str) -> str:
    paras: list[str] = []
    for para in re.split(r"\n\s*\n", text or ""):
        p = para.strip()
        if not p or is_emoji_only_paragraph(p):
            continue
        paras.append(p)
    return "\n\n".join(paras)


def apply_emoji_rules(text: str) -> str:
    out = clean_broken_emoji(text)
    out = _normalize_pool_emoji(out)
    out = _trim_emoji_runs(out)
    out = _remove_orphan_paragraphs(out)
    out = _add_paragraph_stickers(out)
    if has_long_emoji_run(out):
        out = _trim_emoji_runs(out)
    return out


def _extract_headline(title: str, post_text: str) -> str:
    source = (title or "").strip()
    if not source:
        for para in re.split(r"\n\s*\n", post_text or ""):
            p = para.strip()
            if p and not is_emoji_only_paragraph(p):
                source = _EMOJI_RE.sub("", p).strip()
                break
    else:
        source = _EMOJI_RE.sub("", source).strip()
    source = re.sub(r"^[«\"'\s]+", "", source)
    source = re.sub(r"\s+", " ", source).strip()
    if not source:
        return ""
    return clip_to_cover(source)[:120]


def _normalize_whitespace(text: str) -> str:
    out = (text or "").strip()
    out = _MULTI_NL.sub("\n\n", out)
    lines = [_WS.sub(" ", ln).strip() for ln in out.split("\n")]
    return "\n".join(lines).strip()


def light_edit(
    title: str,
    body: str,
    *,
    profanity_mode: str = "",
) -> dict[str, Any]:
    """Косметика донора: капс, брендинг, мат, типографика, эмодзи. Без LLM."""
    raw = _source_text(title, body)
    if not raw.strip():
        return {"headline": "", "post_text": "", "stickers": [], "emoji_lead": "⚽️"}

    mode = _resolve_profanity_mode(profanity_mode)
    text = normalize_caps(raw)
    text = strip_branding(text)
    text = apply_profanity(text, mode=mode)
    text = normalize_ru_typo(text)
    text = _normalize_whitespace(text)
    text = apply_emoji_rules(text)
    text = _normalize_whitespace(text)

    headline = _extract_headline(title, text)
    stickers = leading_stickers_in_text(text)
    emoji_lead = leading_emoji(text) or "⚽️"

    return {
        "headline": headline,
        "post_text": text,
        "stickers": stickers[:3],
        "emoji_lead": emoji_lead[:8],
    }
