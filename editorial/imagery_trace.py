"""JSONL-след выбора обложки: запрос → кандидаты → quality → vision → pick.

Это не hidden chain-of-thought модели, а полный протокол решения: что искали,
что отбросили локально, что сказала vision (who/reason/score) и что взяли.
Пул разметки строится скриптом scripts/export_imagery_labeling_pool.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT

TRACE_DIR = ROOT / "data" / "editorial" / "logs" / "imagery"


def trace_path(day: datetime | None = None) -> Path:
    stamp = (day or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return TRACE_DIR / f"{stamp}.jsonl"


def new_trace(item: dict[str, Any], *, template: str) -> dict[str, Any]:
    try:
        entities = json.loads(item.get("entities_json") or "{}")
    except Exception:
        entities = {}
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "news_id": item.get("id"),
        "title": (item.get("title") or "")[:240],
        "url": item.get("url") or "",
        "event_type": item.get("event_type") or "",
        "caption": (item.get("caption") or item.get("headline") or "")[:200],
        "entities": {
            "players": list(entities.get("players") or [])[:6],
            "teams": list(entities.get("teams") or [])[:6],
        },
        "template": template,
        "query": "",
        "searches": [],
        "quality_drops": [],
        "quality_ok": [],
        "vision": None,
        "pick": None,
        "outcome": "unknown",
    }


def append_trace(trace: dict[str, Any]) -> Path:
    path = trace_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(trace, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path


def load_trace_for_news(news_id: int | str, *, days_back: int = 7) -> dict[str, Any] | None:
    """Последний trace по news_id (с конца свежих jsonl)."""
    try:
        nid = int(news_id)
    except (TypeError, ValueError):
        return None
    files = sorted(TRACE_DIR.glob("*.jsonl"), reverse=True)
    if days_back > 0:
        files = files[:days_back]
    for file in files:
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                if int(row.get("news_id") or 0) == nid:
                    return row
            except (TypeError, ValueError):
                continue
    return None


def load_traces(path: Path | None = None, *, limit: int = 0) -> list[dict[str, Any]]:
    """Читает один jsonl или все файлы в TRACE_DIR (свежие последние)."""
    files: list[Path]
    if path and path.is_file():
        files = [path]
    else:
        files = sorted(TRACE_DIR.glob("*.jsonl"))
    rows: list[dict[str, Any]] = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows
