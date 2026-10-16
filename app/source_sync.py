from __future__ import annotations

from typing import Any


def publish_hold_sec_for_media(media: list[dict[str, Any]] | None, default_hold: int) -> int:
    """Текстовые посты без фото/видео — сразу, медиа ждут publish_hold_sec."""
    from app.filter import has_downloadable_media

    if has_downloadable_media(media):
        return max(0, int(default_hold or 0))
    return 0


def source_seq(external_id: str) -> int:
    """Номер поста в источнике: tg:chan/20843, vk:-123_456."""
    raw = (external_id or "").strip()
    if not raw:
        return 0
    tail = raw.rsplit("/", 1)[-1]
    if "_" in tail:
        tail = tail.rsplit("_", 1)[-1]
    if ":" in tail:
        tail = tail.rsplit(":", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return 0


def newer_than_cursor(external_id: str, cursor: str) -> bool:
    n = source_seq(external_id)
    c = source_seq(cursor)
    if n and c:
        return n > c
    if not cursor:
        return bool(external_id)
    return bool(external_id) and external_id != cursor


def deleted_in_visible_window(
    visible_ids: list[str],
    candidate_ids: list[str],
) -> list[str]:
    """
    Пост пропал с видимой страницы ленты, но его номер новее самого старого
    видимого — значит его удалили, а не просто прокрутили вниз.
    """
    vis = set(visible_ids)
    nums = [source_seq(x) for x in visible_ids]
    nums = [n for n in nums if n]
    if not nums:
        return []
    oldest = min(nums)
    out: list[str] = []
    seen: set[str] = set()
    for eid in candidate_ids:
        if not eid or eid in vis or eid in seen:
            continue
        n = source_seq(eid)
        if n and n > oldest:
            seen.add(eid)
            out.append(eid)
    return out
