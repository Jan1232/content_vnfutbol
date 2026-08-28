"""Token usage ledger for editorial Platform API calls."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT, get_settings
from app.db import db, get_meta, set_meta

PRICES_FILE = ROOT / "editorial" / "model_prices.yaml"
_B64_IN_URL = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+")


def sanitize_messages_for_log(messages: list[Any] | None) -> list[Any]:
    """Сериализуемый запрос: без base64-картинок (только плейсхолдеры)."""
    if not messages:
        return []
    out: list[Any] = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        m = {k: v for k, v in msg.items()}
        content = m.get("content")
        if isinstance(content, str):
            m["content"] = content
        elif isinstance(content, list):
            parts: list[Any] = []
            for part in content:
                if not isinstance(part, dict):
                    parts.append(part)
                    continue
                if part.get("type") == "image_url":
                    iu = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
                    detail = iu.get("detail") or "low"
                    url = str(iu.get("url") or "")
                    nbytes = 0
                    if "base64," in url:
                        try:
                            import base64

                            nbytes = len(base64.b64decode(url.split("base64,", 1)[1], validate=False))
                        except Exception:
                            nbytes = max(0, len(url) - 128)
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "detail": detail,
                                "placeholder": f"[jpeg {nbytes} bytes, not stored]",
                            },
                        }
                    )
                else:
                    parts.append(part)
            m["content"] = parts
        out.append(m)
    return out


def _payload_for_log(payload: dict[str, Any] | None, *, messages: list[Any] | None = None) -> str:
    data: dict[str, Any] = {}
    if isinstance(payload, dict):
        data = {k: v for k, v in payload.items() if k != "messages"}
    if messages is not None:
        data["messages"] = sanitize_messages_for_log(messages)
    elif isinstance(payload, dict) and payload.get("messages"):
        data["messages"] = sanitize_messages_for_log(list(payload.get("messages") or []))
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return str(data)[:8000]


def _clip_response(text: str) -> str:
    settings = get_settings()
    limit = int(getattr(settings, "editorial_llm_full_log_max_response_chars", 32_000) or 32_000)
    blob = (text or "").strip()
    if len(blob) <= limit:
        return blob
    return blob[:limit] + f"\n…[truncated {len(blob) - limit} chars]"


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
    benchmark_run_id: str = "",
    ms: int = 0,
    usd: float | None = None,
) -> int | None:
    if usd is None:
        usd = estimate_usd(int(prompt_tokens or 0), int(completion_tokens or 0), model)
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO editorial_llm_usage (
                news_id, task, model, prompt_tokens, completion_tokens,
                cached_tokens, ok, note, benchmark_run_id, ms, usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                str(benchmark_run_id or "")[:64],
                int(ms or 0),
                float(usd or 0),
            ),
        )
        return int(cur.lastrowid) if cur.lastrowid else None


def record_llm_call_log(
    *,
    usage_id: int | None,
    news_id: str | None,
    task: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    ok: bool,
    note: str = "",
    benchmark_run_id: str = "",
    ms: int = 0,
    usd: float | None = None,
    http_status: int = 0,
    images_n: int = 0,
    image_bytes: int = 0,
    request_json: str = "",
    response_text: str = "",
) -> None:
    settings = get_settings()
    if not bool(getattr(settings, "editorial_llm_full_log", True)):
        return
    if usd is None:
        usd = estimate_usd(int(prompt_tokens or 0), int(completion_tokens or 0), model)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO editorial_llm_call_log (
                usage_id, news_id, task, model,
                prompt_tokens, completion_tokens, cached_tokens, usd, ms,
                ok, http_status, images_n, image_bytes,
                request_json, response_text, note, benchmark_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(usage_id) if usage_id else None,
                str(news_id or ""),
                (task or "chat")[:40],
                (model or "")[:80],
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                int(cached_tokens or 0),
                float(usd or 0),
                int(ms or 0),
                1 if ok else 0,
                int(http_status or 0),
                int(images_n or 0),
                int(image_bytes or 0),
                (request_json or "")[:500_000],
                _clip_response(response_text),
                (note or "")[:400],
                str(benchmark_run_id or "")[:64],
            ),
        )


def purge_old_llm_call_logs(*, retention_days: int | None = None) -> int:
    """Удалить полные логи старше N дней. Возвращает число удалённых строк."""
    settings = get_settings()
    days = int(retention_days if retention_days is not None else (settings.editorial_llm_full_log_retention_days or 7))
    if days <= 0:
        return 0
    with db() as conn:
        cur = conn.execute(
            """
            DELETE FROM editorial_llm_call_log
            WHERE ts < datetime('now', ?)
            """,
            (f"-{days} days",),
        )
        return int(cur.rowcount or 0)


def maybe_purge_llm_call_logs() -> int:
    """Раз в сутки — очистка полных логов по retention."""
    settings = get_settings()
    if not bool(getattr(settings, "editorial_llm_full_log", True)):
        return 0
    days = int(settings.editorial_llm_full_log_retention_days or 7)
    with db() as conn:
        last = str(get_meta(conn, "editorial_llm_call_log_purge", "") or "")
        today = conn.execute("SELECT date('now')").fetchone()[0]
        if last == today:
            return 0
        n = purge_old_llm_call_logs(retention_days=days)
        set_meta(conn, "editorial_llm_call_log_purge", str(today))
        if n:
            print(f"[editorial] purged {n} llm_call_log rows older than {days}d", flush=True)
        return n


def call_log_disk_stats() -> dict[str, Any]:
    """Оценка объёма полного лога (для админки)."""
    settings = get_settings()
    with db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(length(request_json)+length(response_text)), 0) AS bytes
            FROM editorial_llm_call_log
            """
        ).fetchone()
    n = int(row["n"] or 0) if row else 0
    nbytes = int(row["bytes"] or 0) if row else 0
    return {
        "rows": n,
        "bytes": nbytes,
        "mb": round(nbytes / (1024 * 1024), 2),
        "retention_days": int(settings.editorial_llm_full_log_retention_days or 7),
        "enabled": bool(settings.editorial_llm_full_log),
    }


def record_benchmark_stage(
    *,
    run_id: str,
    news_id: str | int,
    stage: str,
    model: str,
    p_in: int,
    p_out: int,
    cached: int = 0,
    ms: int = 0,
) -> None:
    usd = estimate_usd(int(p_in or 0), int(p_out or 0), model)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO editorial_cost_benchmark (
                run_id, news_id, stage, model, p_in, p_out, cached, usd, ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(run_id)[:64],
                str(news_id or ""),
                str(stage or "")[:40],
                str(model or "")[:80],
                int(p_in or 0),
                int(p_out or 0),
                int(cached or 0),
                float(usd),
                int(ms or 0),
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


def _usage_summary_for_period(days: int) -> dict[str, Any]:
    """Агрегат расхода по task/model за N дней."""
    if days <= 1:
        where = "ts >= datetime('now', '-1 day')"
        label = "24ч"
    else:
        where = f"ts >= datetime('now', '-{int(days)} days')"
        label = f"{int(days)}д"
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT task, model,
                   COUNT(*) AS n,
                   SUM(ok) AS ok_n,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(COALESCE(cached_tokens, 0)) AS cached_tokens
            FROM editorial_llm_usage
            WHERE {where}
            GROUP BY task, model
            ORDER BY (SUM(prompt_tokens) + SUM(completion_tokens)) DESC
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
        d["total_tokens"] = d["prompt_tokens"] + d["completion_tokens"]
        d["n"] = int(d.get("n") or 0)
        d["ok_n"] = int(d.get("ok_n") or 0)
        d["usd"] = estimate_usd(d["prompt_tokens"], d["completion_tokens"], str(d.get("model") or ""))
        if d["n"]:
            d["avg_prompt"] = int(d["prompt_tokens"] / d["n"])
        else:
            d["avg_prompt"] = 0
        items.append(d)
        prompt += d["prompt_tokens"]
        completion += d["completion_tokens"]
        cached += d["cached_tokens"]
        usd += d["usd"]
        n += d["n"]
    return {
        "label": label,
        "rows": items,
        "n": n,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "total_tokens": prompt + completion,
        "usd": usd,
    }


def daily_usage_summary() -> dict[str, Any]:
    return _usage_summary_for_period(1)


def usage_dashboard() -> dict[str, Any]:
    """24ч + 7д для /editorial."""
    h24 = _usage_summary_for_period(1)
    d7 = _usage_summary_for_period(7)
    return {
        "h24": h24,
        "d7": d7,
        "last_benchmark": get_last_benchmark_summary(),
        "call_log": call_log_disk_stats(),
    }


def get_last_benchmark_summary() -> dict[str, Any] | None:
    bench_dir = ROOT / "data" / "editorial" / "benchmark"
    if not bench_dir.is_dir():
        return None
    files = sorted(bench_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    import json

    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
        data["file"] = files[0].name
        return data
    except Exception:
        return None
