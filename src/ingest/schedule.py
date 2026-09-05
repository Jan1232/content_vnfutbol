# -*- coding: utf-8 -*-
"""Расписание: маркеры + рандом-фраза без LLM."""

from __future__ import annotations

import random

from src.ingest.sources import SCHEDULE_MARKERS, SCHEDULE_PHRASES


def is_schedule_post(text: str, source: str) -> bool:
    src = (source or "").lstrip("@")
    if src != "footballhourss":
        return False
    low = (text or "").lower()
    if "расписание" in low:
        return True
    return any(m in low for m in SCHEDULE_MARKERS)


def schedule_phrase() -> str:
    return random.choice(SCHEDULE_PHRASES)
