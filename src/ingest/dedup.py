# -*- coding: utf-8 -*-
"""Дедуп: слой 1 — отпечаток события, слой 2 — эмбеддинг + teams."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from src.ingest.aliases import normalize_event
from src.ingest.db import recent_facts_for_dedup
from src.ingest.embed import cosine
from src.ingest.sources import DEDUP_THRESHOLD, DEDUP_WINDOW_HOURS

log = logging.getLogger("ingest.dedup")

GOAL_MINUTE_TOLERANCE = 5


def fingerprint_key(event: dict) -> str | None:
    """Индексный ключ (без минуты для goal — минута сравнивается отдельно)."""
    e = normalize_event(event)
    kind = e["event_kind"]
    teams = "|".join(e["teams"])
    if kind == "goal":
        if not e["teams"] or not e["player"] or not e["score"]:
            return None
        return f"goal|{teams}|{e['player']}|{e['score']}"
    if kind == "final_result":
        if not e["teams"] or not e["score"]:
            return None
        return f"final|{teams}|{e['score']}"
    if kind == "transfer":
        if not e["player"] or not e["to_club"]:
            return None
        return f"transfer|{e['player']}|{e['to_club']}"
    if kind == "transfer_cancel":
        if not e["player"] or not e["to_club"]:
            return None
        return f"cancel|{e['player']}|{e['to_club']}"
    if kind == "quote":
        if not e["player"]:
            return None
        return f"quote|{e['player']}"
    return None


def fingerprint_hash(event: dict) -> str | None:
    key = fingerprint_key(event)
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _teams_overlap(a: list[str], b: list[str]) -> bool:
    return bool(set(a) & set(b))


def _events_match_layer1(new: dict, old: dict) -> bool:
    """Правила совпадения отпечатка по типу события."""
    n = normalize_event(new)
    o = normalize_event(old)
    if n["event_kind"] != o["event_kind"]:
        return False
    kind = n["event_kind"]
    if kind == "goal":
        if set(n["teams"]) != set(o["teams"]) or len(n["teams"]) < 2:
            return False
        if not (n["player"] and o["player"] and n["player"] == o["player"]):
            return False
        if not (n["score"] and o["score"] and n["score"] == o["score"]):
            return False
        if n["minute"] is None or o["minute"] is None:
            return False
        return abs(int(n["minute"]) - int(o["minute"])) <= GOAL_MINUTE_TOLERANCE
    if kind == "final_result":
        return (
            set(n["teams"]) == set(o["teams"])
            and bool(n["score"])
            and n["score"] == o["score"]
        )
    if kind == "transfer":
        return (
            bool(n["player"])
            and n["player"] == o["player"]
            and bool(n["to_club"])
            and n["to_club"] == o["to_club"]
        )
    if kind == "transfer_cancel":
        return (
            bool(n["player"])
            and n["player"] == o["player"]
            and bool(n["to_club"])
            and n["to_club"] == o["to_club"]
        )
    if kind == "quote":
        # автор совпал — суть проверит слой 2 / caller с эмбеддингом
        return bool(n["player"]) and n["player"] == o["player"]
    return False


def find_duplicate(
    *,
    event: dict,
    fact_text: str,
    embedding: list[float],
    window_hours: int = DEDUP_WINDOW_HOURS,
    threshold: float = DEDUP_THRESHOLD,
) -> tuple[int, str, float] | None:
    """Возвращает (fact_id, layer, score) или None.

    layer: 'fingerprint' | 'embedding'
    """
    e = normalize_event(event)
    recent = recent_facts_for_dedup(window_hours)

    # Слой 1 — отпечаток
    if e["event_kind"] != "other":
        for row in recent:
            old_event = {
                "teams": json.loads(row["event_teams"] or "[]"),
                "player": row.get("event_player"),
                "to_club": row.get("event_to_club"),
                "score": row.get("event_score"),
                "minute": row.get("event_minute"),
                "event_kind": row.get("event_kind") or "other",
            }
            if not _events_match_layer1(e, old_event):
                continue
            if e["event_kind"] == "quote":
                # автор совпал — нужна близость сути
                vec = row.get("embedding")
                if not vec:
                    continue
                score = cosine(embedding, vec)
                if score < threshold:
                    continue
                log.info("dedup layer=fingerprint(quote+emb) fact=%s score=%.3f", row["id"], score)
                return int(row["id"]), "fingerprint", score
            log.info("dedup layer=fingerprint fact=%s kind=%s", row["id"], e["event_kind"])
            return int(row["id"]), "fingerprint", 1.0

    # Слой 2 — эмбеддинг + teams (страховка кривого extract)
    best: tuple[int, float] | None = None
    for row in recent:
        vec = row.get("embedding")
        if not vec:
            continue
        score = cosine(embedding, vec)
        if score < threshold:
            continue
        old_teams = json.loads(row["event_teams"] or "[]")
        if e["teams"] and old_teams and not _teams_overlap(e["teams"], old_teams):
            continue
        old_kind = row.get("event_kind") or "other"
        # не схлопывать разные типы события одного матча
        if e["event_kind"] != "other" and old_kind != "other" and e["event_kind"] != old_kind:
            continue
        # голы с разницей минут > tolerance — разные события
        if e["event_kind"] == "goal" and old_kind == "goal":
            om = row.get("event_minute")
            if e["minute"] is not None and om is not None:
                if abs(int(e["minute"]) - int(om)) > GOAL_MINUTE_TOLERANCE:
                    continue
        if best is None or score > best[1]:
            best = (int(row["id"]), score)
    if best:
        log.info("dedup layer=embedding fact=%s score=%.3f", best[0], best[1])
        return best[0], "embedding", best[1]
    return None


def soft_zh_overlap(event: dict, window_hours: int = DEDUP_WINDOW_HOURS) -> int | None:
    """Если та же новость уже есть от zhfootballll — id факта (не блок)."""
    e = normalize_event(event)
    if e["event_kind"] == "other":
        return None
    for row in recent_facts_for_dedup(window_hours):
        if row.get("source") != "zhfootballll":
            continue
        old_event = {
            "teams": json.loads(row["event_teams"] or "[]"),
            "player": row.get("event_player"),
            "to_club": row.get("event_to_club"),
            "score": row.get("event_score"),
            "minute": row.get("event_minute"),
            "event_kind": row.get("event_kind") or "other",
        }
        if _events_match_layer1(e, old_event):
            return int(row["id"])
    return None
