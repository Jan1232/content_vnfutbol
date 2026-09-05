# -*- coding: utf-8 -*-
"""Медиа-роутер: архетип → стратегия (SPEC v3.1)."""

from __future__ import annotations

from dataclasses import dataclass

from src.ingest.sources import MEDIA_CLEAN_SOURCES, MEDIA_WATERMARK_SOURCES

# Архетипы → ветка по умолчанию
YANDEX_ARCHETYPES = frozenset(
    {
        "transfer",
        "transfer_cancel",
        "news_opinion",
        "provocation",
        "quote_scandal",
        "quote_hypocrisy",
        "achievement",
        "humor_list",
        "injury_list",
    }
)


@dataclass
class MediaPlan:
    strategy: str  # yandex | source | as_is | none
    allow_zh_image: bool = False
    reason: str = ""


def resolve_media_plan(
    *,
    archetype: str,
    source: str,
    has_source_media: bool,
    media_kind: str | None,  # photo | video | None
) -> MediaPlan:
    """Выбирает медиа-ветку. Готовые картинки НИКОГДА из ЖФ."""
    src = (source or "").lstrip("@")
    is_zh = src in MEDIA_WATERMARK_SOURCES
    clean = src in MEDIA_CLEAN_SOURCES
    kind = media_kind or ("photo" if has_source_media else None)
    is_video = kind == "video"

    if archetype == "video":
        if has_source_media and is_video:
            return MediaPlan("as_is", allow_zh_image=True, reason="video any source")
        if has_source_media:
            return MediaPlan("as_is", allow_zh_image=True, reason="video-ish media")
        return MediaPlan("none", reason="video without media")

    if archetype == "meme":
        # авто-мем предпочтительно с clean; при ручной смене категории —
        # берём медиа из любого источника КРОМЕ ЖФ (вотермарки)
        if has_source_media and not is_zh:
            return MediaPlan("as_is", reason="meme from non-ZH source")
        if clean and has_source_media:
            return MediaPlan("as_is", reason="meme clean source")
        return MediaPlan("none", reason="meme needs source media (non-ZH)")

    if archetype == "schedule":
        if src == "footballhourss" and has_source_media and not is_video:
            return MediaPlan("source", reason="schedule from footballhourss")
        if src == "footballhourss" and has_source_media:
            return MediaPlan("source", reason="schedule media fh")
        return MediaPlan("none", reason="schedule needs fh media")

    if archetype == "lineup":
        if has_source_media and not is_zh:
            return MediaPlan("source", reason="lineup non-ZH source")
        return MediaPlan("yandex", reason="lineup fallback yandex")

    if archetype == "result":
        if clean and has_source_media:
            return MediaPlan("source", reason="result from clean source")
        return MediaPlan("yandex", reason="result fallback yandex")

    if archetype == "goal_live":
        return MediaPlan("none", reason="goal_live text-only (v3.2)")

    if archetype in YANDEX_ARCHETYPES or archetype.startswith("quote_"):
        return MediaPlan("yandex", reason="search by image_query")

    # unknown → yandex if news-like
    return MediaPlan("yandex", reason="default yandex")


def media_source_allowed(
    *,
    strategy: str,
    source: str,
    media_kind: str | None,
) -> bool:
    """Картинки не из ЖФ. Видео — с любого. as_is/source фото — не ЖФ."""
    src = (source or "").lstrip("@")
    if strategy not in ("source", "as_is"):
        return True
    if media_kind == "video":
        return True
    if src in MEDIA_WATERMARK_SOURCES:
        return False
    return True
