"""Фактический guardrail v2 — только правда/риск, НЕ стиль."""

from __future__ import annotations

import re

from src.generate.tone_reference import FORBIDDEN_SIGNATURES

RUMOR_MARKERS = (
    "источник",
    "сообщают",
    "пишет",
    "инсайдер",
    "по информации",
    "романо",
    "моретто",
    "если верить",
    "marca",
    "слухи",
    "слух",
    "по данным",
)

_SELF_HARM_PATTERNS = (
    re.compile(r"отреж\w*\s+себе", re.IGNORECASE),
    re.compile(r"отреза\w*\s+себе", re.IGNORECASE),
    re.compile(r"пореж\w*\s+себе", re.IGNORECASE),
    re.compile(r"повеш\w*\s+ся", re.IGNORECASE),
    re.compile(r"самоубий", re.IGNORECASE),
)

# Имена источников, которым нельзя приписывать выдумку
_SOURCE_ALT = r"(?:marca|\bas\b|романо|моретто|фабрицио(?:\s+романо)?|the athletic|sky sports|bild)"

_ATTR_CLAIM_RE = re.compile(
    rf"(?:{_SOURCE_ALT}(?:\s+и\s+{_SOURCE_ALT})*)\s+"
    rf"(?:сообщает|сообщают|пишет|пишут|заявил|заявляет|подтвердил(?:а|и)?)"
    rf"\s*[:—–-]?\s*(?P<claim>.+?)(?:\n|$)",
    re.IGNORECASE,
)

_ATTR_CLAIM_RE_2 = re.compile(
    rf"(?:сообщает|пишет|по словам|по данным|по информации)\s+"
    rf"{_SOURCE_ALT}\s*[:—–,]?\s*(?P<claim>.+?)(?:\n|$)",
    re.IGNORECASE,
)

_STOP = {
    "это", "как", "что", "для", "уже", "его", "её", "ее", "они", "она",
    "был", "была", "были", "есть", "при", "над", "под", "без", "или",
    "но", "да", "нет", "все", "всё", "еще", "ещё", "там", "тут", "этот",
    "эта", "эти", "тот", "после", "перед", "между", "также", "если",
}

_FIGURATIVE = (
    "ключ", "разделал", "священн", "экономик", "ограб", "будто",
    "кормит", "напомн", "квест", "квантов", "шедевр судей",
    "явно решил", "где лежат",
)


def has_self_harm_literal(text: str) -> bool:
    return any(p.search(text) for p in _SELF_HARM_PATTERNS)


def _content_words(text: str) -> set[str]:
    words = re.findall(r"[а-яёa-z]{3,}", text.lower())
    return {w for w in words if w not in _STOP}


def _looks_figurative(claim: str) -> bool:
    low = claim.lower()
    return any(tok in low for tok in _FIGURATIVE)


def find_source_inventions(post: str, fact: str) -> list[str]:
    """Приписка источнику: после атрибуции идёт образ, которого нет во факте."""
    fact_words = _content_words(fact)
    hits: list[str] = []
    for rx in (_ATTR_CLAIM_RE, _ATTR_CLAIM_RE_2):
        for m in rx.finditer(post):
            claim = (m.group("claim") or "").strip()
            if not claim:
                continue
            claim_words = _content_words(claim)
            if not claim_words:
                continue
            extra = claim_words - fact_words
            overlap = len(claim_words & fact_words) / max(len(claim_words), 1)
            invented = len(extra) >= 4 and overlap < 0.45
            if invented or (extra and _looks_figurative(claim) and overlap < 0.7):
                hits.append(claim[:180])
    return hits


def check_guardrail(post: str, veracity: str, fact: str = "") -> list[str]:
    """Пометки (не «убивают» стиль). Пустой список = чисто."""
    flags: list[str] = []
    post_lower = post.lower()

    if veracity != "verified":
        if "here we go" in post_lower:
            flags.append("HERE WE GO на слухе/спекуляции")
        if not any(m in post_lower for m in RUMOR_MARKERS):
            flags.append("нет маркера источника при veracity != verified")

    for sig in FORBIDDEN_SIGNATURES:
        if sig.lower() in post_lower:
            flags.append(f"подпись/ссылка на канал («{sig}»)")
            break

    if has_self_harm_literal(post):
        flags.append("дословное самоповреждение")

    if fact and find_source_inventions(post, fact):
        flags.append("возможна приписка источнику")

    return flags
