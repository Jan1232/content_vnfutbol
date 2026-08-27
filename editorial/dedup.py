"""Dedup news items already stored / published."""

from __future__ import annotations

from editorial.models import NewsItem
from editorial.store import get_by_external
from editorial.topic_gate import cluster_id_for


def filter_new(channel_slug: str, items: list[NewsItem]) -> list[NewsItem]:
    fresh: list[NewsItem] = []
    seen: set[str] = set()
    for item in items:
        if item.external_id in seen:
            continue
        seen.add(item.external_id)
        if get_by_external(channel_slug, item.external_id):
            continue
        if not item.cluster_id:
            item.cluster_id = cluster_id_for(item)
        fresh.append(item)
    return fresh
