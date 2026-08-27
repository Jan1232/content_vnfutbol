"""Человекочитаемые названия event_type для модерации."""

from __future__ import annotations

EVENT_TYPE_LABELS: dict[str, str] = {
    "transfer": "🔄 Трансфер",
    "injury": "🚑 Травма",
    "match_result": "⚽ Результат",
    "official_statement": "📢 Официально",
    "lineup": "📋 Составы на матч",
    "lifestyle": "👤 Lifestyle",
    "rumor": "👀 Слух",
    "other": "📰 Другое",
    "fixture_result": "⚽ Счёт (API)",
}


def event_type_label(event_type: str) -> str:
    key = str(event_type or "").strip()
    return EVENT_TYPE_LABELS.get(key, key or "—")


def moderation_event_types(allowed: list[str] | tuple[str, ...] | None) -> list[str]:
    """Категории для inline-клавиатуры (без служебных)."""
    skip = {"fixture_result", "other"}
    order = (
        "transfer",
        "injury",
        "match_result",
        "official_statement",
        "lineup",
        "lifestyle",
        "rumor",
    )
    allowed_set = set(allowed or ())
    out: list[str] = []
    for et in order:
        if et in allowed_set and et not in skip:
            out.append(et)
    for et in sorted(allowed_set):
        if et not in skip and et not in out:
            out.append(et)
    return out
