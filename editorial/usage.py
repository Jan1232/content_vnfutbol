"""Token usage ledger for editorial Platform API calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT
from app.db import db

PRICES_FILE = ROOT / "editorial" / "model_prices.yaml"


def record_llm_usage(
    *,
    news_id: str | None,
    task: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    ok: bool,
    note: str = "",
    cached_tokens: int = 0,
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO editorial_llm_usage (
                news_id, task, model, prompt_tokens, completion_tokens, cached_tokens, ok, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(news_id or ""),
                (task or "chat")[:40],
                (model or "")[:80],
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                int(cached_tokens or 0),
                1 if ok else 0,
                (note or "")[:400],
            ),
        )


def load_prices() -> dict[str, dict[str, float]]:
    path = Path(PRICES_FILE)
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for name, row in models.items():
        if not isinstance(row, dict):
            continue
        out[str(name)] = {
            "input_per_m": float(row.get("input_per_m") or 0),
            "output_per_m": float(row.get("output_per_m") or 0),
        }
    return out


def estimate_usd(prompt_tokens: int, completion_tokens: int, model: str) -> float:
    prices = load_prices()
    row = prices.get(model) or {}
    return (
        (prompt_tokens / 1_000_000.0) * row.get("input_per_m", 0.0)
        + (completion_tokens / 1_000_000.0) * row.get("output_per_m", 0.0)
    )


def daily_usage_summary() -> dict[str, Any]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT model, task,
                   COUNT(*) AS n,
                   SUM(ok) AS ok_n,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(COALESCE(cached_tokens, 0)) AS cached_tokens
            FROM editorial_llm_usage
            WHERE ts >= datetime('now', '-1 day')
            GROUP BY model, task
            ORDER BY prompt_tokens DESC
            """
        ).fetchall()
    items: list[dict[str, Any]] = []
    prompt = 0
    completion = 0
    cached = 0
    usd = 0.0
    n = 0
    for row in rows:
        d = dict(row)
        d["prompt_tokens"] = int(d.get("prompt_tokens") or 0)
        d["completion_tokens"] = int(d.get("completion_tokens") or 0)
        d["cached_tokens"] = int(d.get("cached_tokens") or 0)
        d["n"] = int(d.get("n") or 0)
        d["ok_n"] = int(d.get("ok_n") or 0)
        d["usd"] = estimate_usd(d["prompt_tokens"], d["completion_tokens"], str(d.get("model") or ""))
        items.append(d)
        prompt += d["prompt_tokens"]
        completion += d["completion_tokens"]
        cached += d["cached_tokens"]
        usd += d["usd"]
        n += d["n"]
    return {
        "rows": items,
        "n": n,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "usd": usd,
    }
