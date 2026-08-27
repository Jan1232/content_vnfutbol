"""Rewrite confirmed news into a Russian post with light emoji."""

from __future__ import annotations

import json
import re
from typing import Any

from editorial import llm
from editorial.stickers import (
    has_long_emoji_run,
    is_emoji_only_paragraph,
    paragraph_has_lead_sticker,
    register_from_text,
)

_HEDGE_SENT = re.compile(
    r"(?i)("
    r"официальн\w*\s+(?:объявлен|подтвержден|комментар)\w*\s+пока\s+нет"
    r"|точн\w*\s+(?:формулировк|цитат)\w*"
    r"|формулировк\w*.{0,40}не\s+приводится"
    r"|цитат\w*.{0,30}не\s+приводится"
    r"|подробност\w*\s+не\s+уточняются"
    r"|детал\w*\s+не\s+сообщаются"
    r"|на\s+момент\s+публикации.{0,40}нет"
    r")"
)
_TOKEN = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_ASCII_QUOTES = re.compile(r'"([^"]+)"')
_DASHES = re.compile(r"\s+[-–]\s+")
_MIN_WORDS = 28
_MIN_PARAS = 2


def strip_hedge_tails(text: str) -> str:
    """Вырезает мета-оговорки вроде «официального объявления пока нет»."""
    raw = (text or "").strip()
    if not raw:
        return ""
    out: list[str] = []
    for para in re.split(r"\n+", raw):
        para = para.strip()
        if not para:
            continue
        if is_emoji_only_paragraph(para):
            continue
        bits = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", para) if s.strip()]
        kept = [s for s in bits if not _HEDGE_SENT.search(s)]
        if kept:
            out.append(" ".join(kept))
    return "\n\n".join(out)


def normalize_ru_typo(text: str) -> str:
    """Ёлочки, длинное тире; без вложенных „ “ внутри «»."""
    t = (text or "").strip()
    if not t:
        return ""
    t = t.replace("“", "«").replace("”", "»").replace("„", "«").replace("‟", "«")
    t = t.replace("‹", "«").replace("›", "»").replace("‚", "«")
    t = _ASCII_QUOTES.sub(r"«\1»", t)
    t = re.sub(r"«([^»]*)«([^»]*)»", r"«\1\2»", t)
    t = re.sub(r"«([^»]*)»([^«]*)»", r"«\1\2»", t)
    t = _DASHES.sub(" — ", t)
    t = t.replace("—", " — ")
    paras = []
    for para in t.split("\n"):
        p = re.sub(r"[ \t]+", " ", para).strip()
        p = re.sub(r"«\s+", "«", p)
        p = re.sub(r"\s+»", "»", p)
        p = re.sub(r"»\s*,\s*—", "», —", p)
        paras.append(p)
    return "\n\n".join(p for p in paras if p)


def _word_count(text: str) -> int:
    return len(_TOKEN.findall(text or ""))


def _latin_ratio(text: str) -> float:
    letters = [ch for ch in (text or "") if ch.isalpha()]
    if not letters:
        return 0.0
    latin = sum(1 for ch in letters if ("a" <= ch.lower() <= "z"))
    return latin / len(letters)


def _has_structural_stickers(text: str) -> bool:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if not paras:
        return False
    leads = sum(
        1 for p in paras if paragraph_has_lead_sticker(p) and _word_count(p) > 2
    )
    return leads >= 1


def post_text_ok(
    post_text: str,
    *,
    title: str = "",
    min_paras: int = _MIN_PARAS,
    min_words: int = _MIN_WORDS,
) -> tuple[bool, str]:
    """Минимальная планка качества поста после rewrite."""
    text = (post_text or "").strip()
    if not text:
        return False, "пустой текст"
    if has_long_emoji_run(text):
        return False, "слишком много эмодзи подряд"
    for para in re.split(r"\n\s*\n", text):
        p = para.strip()
        if p and is_emoji_only_paragraph(p):
            return False, "orphan emoji"
    if _latin_ratio(text) >= 0.45:
        return False, "слишком много латиницы"
    words = _word_count(text)
    if words < min_words:
        return False, f"мало слов ({words} из {min_words})"
    paras = [
        p
        for p in re.split(r"\n\s*\n", text)
        if p.strip() and not is_emoji_only_paragraph(p.strip())
    ]
    if len(paras) < min_paras:
        return False, f"мало абзацев ({len(paras)} из {min_paras})"
    if not _has_structural_stickers(text):
        return False, "нет стикеров в абзацах"
    title_l = " ".join(_TOKEN.findall((title or "").lower()))
    body_l = " ".join(_TOKEN.findall(text.lower()))
    if title_l and body_l == title_l:
        return False, "пост = заголовок"
    if words <= 4:
        return False, "почти пустой"
    return True, "ok"


def accept_edited_text(text: str, *, title: str = "") -> tuple[bool, str, str]:
    """Нормализация + валидация текста из TG-модерации."""
    cleaned = normalize_ru_typo((text or "").strip())
    ok, why = post_text_ok(cleaned, title=title, min_paras=1, min_words=8)
    if not ok:
        return False, why, cleaned
    register_from_text(cleaned)
    return True, "ok", cleaned


def rewrite(item: dict[str, Any], facts: str = "", *, max_attempts: int = 2) -> dict[str, Any]:
    title = str(item.get("title") or "")
    last_err = "нет ответа"
    post_text = ""
    headline = ""
    emoji_lead = "⚽️"
    stickers: list[str] = []
    for attempt in range(max(1, max_attempts)):
        hint = ""
        if attempt:
            hint = (
                f"\nПрошлая попытка плохая ({last_err}). "
                "Напиши ПЛОТНЕЕ по-русски: минимум 2 абзаца, кто/что/зачем, "
                "цифры и имена. Цитату — если есть в источнике. Без английских слов. "
                "Каждый абзац начни тематическим эмодзи (⚽🔴✍️💰🚑🔥🏆👀)."
            )
        data = llm.rewrite(item, facts=facts + hint)
        post_text = normalize_ru_typo(strip_hedge_tails(str(data.get("post_text") or "").strip()))
        headline = normalize_ru_typo(str(data.get("headline") or title).strip())
        emoji_lead = str(data.get("emoji_lead") or "⚽️").strip() or "⚽️"
        stickers = _stickers(data.get("stickers"))
        if not post_text:
            last_err = "пустой post_text"
            continue
        paras = [p.strip() for p in re.split(r"\n\s*\n", post_text) if p.strip()]
        paras = [p for p in paras if not is_emoji_only_paragraph(p)]
        if len(paras) > 6:
            paras = paras[:5]
        post_text = "\n\n".join(paras)
        ok, why = post_text_ok(post_text, title=title)
        if ok:
            register_from_text(post_text)
            return {
                "post_text": post_text,
                "headline": headline[:120],
                "emoji_lead": emoji_lead[:8],
                "stickers": stickers,
            }
        last_err = why
    raise RuntimeError(f"редактура не прошла проверку: {last_err}")


def _stickers(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item or "").strip()
        if s and s not in out:
            out.append(s[:8])
        if len(out) >= 3:
            break
    return out


def facts_from_item(item: dict[str, Any]) -> str:
    bits = [
        f"event_type={item.get('event_type')}",
        f"competition={item.get('competition')}",
        f"factcheck={item.get('factcheck_status')} conf={item.get('factcheck_conf')}",
        f"reason={item.get('factcheck_reason')}",
    ]
    try:
        entities = json.loads(item.get("entities_json") or "{}")
        bits.append(f"entities={json.dumps(entities, ensure_ascii=False)}")
        enrich = entities.get("match_enrich") if isinstance(entities.get("match_enrich"), dict) else {}
        if enrich.get("score"):
            bits.append(f"match_score={enrich.get('score')}")
    except Exception:
        pass
    from editorial.match_enrich import parse_score_from_text

    score = parse_score_from_text(f"{item.get('title') or ''}\n{item.get('body') or ''}")
    if score and not any(line.startswith("match_score=") for line in bits):
        bits.append(f"match_score={score[0]}:{score[1]}")
    return "\n".join(bits)
