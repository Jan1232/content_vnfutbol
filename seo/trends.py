"""Google Trends helper for near-term match hype (tie-break only)."""

from __future__ import annotations

import time
from typing import Iterable

_CACHE: dict[str, tuple[float, float]] = {}
_CACHE_TTL = 3 * 3600


def trends_available() -> bool:
    try:
        from pytrends.request import TrendReq  # noqa: F401

        return True
    except Exception:
        return False


def _cache_get(key: str) -> float | None:
    hit = _CACHE.get(key)
    if not hit:
        return None
    val, exp = hit
    if time.time() > exp:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_set(key: str, val: float) -> None:
    _CACHE[key] = (float(val), time.time() + _CACHE_TTL)


def interest_for_queries(
    queries: list[str],
    *,
    timeframe: str = "now 7-d",
    geo: str = "",
) -> dict[str, float]:
    """Средний интерес Google Trends по запросам (0–100).

    Сравнивать можно только запросы из одного build_payload (одна шкала).
    """
    clean = []
    seen: set[str] = set()
    for q in queries:
        s = " ".join((q or "").split()).strip()
        if not s:
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        clean.append(s)
    if not clean:
        return {}

    cache_key = f"{geo}|{timeframe}|{'||'.join(x.casefold() for x in clean)}"
    cached = _cache_get(cache_key)
    # кэш только для одиночного запроса; для батча считаем заново по ключу батча
    # (храним serialized map отдельно)
    batch_hit = _CACHE.get("batch:" + cache_key)
    if batch_hit and time.time() <= batch_hit[1]:
        # stored as JSON-ish via repr — use separate dict cache
        pass

    try:
        from pytrends.request import TrendReq
    except Exception as e:
        print(f"[seo] trends import fail: {e}", flush=True)
        return {}

    out: dict[str, float] = {}
    # Google Trends: max 5 keywords per request
    for i in range(0, len(clean), 5):
        chunk = clean[i : i + 5]
        try:
            pt = TrendReq(hl="ru-RU", tz=180, retries=2, backoff_factor=0.4)
            pt.build_payload(chunk, timeframe=timeframe, geo=geo or "")
            df = pt.interest_over_time()
            if df is None or df.empty:
                for q in chunk:
                    out[q] = 0.0
            else:
                for q in chunk:
                    if q in df.columns:
                        series = df[q]
                        out[q] = float(series.mean()) if len(series) else 0.0
                    else:
                        out[q] = 0.0
            time.sleep(0.8)
        except Exception as e:
            print(f"[seo] trends fail {chunk}: {e}", flush=True)
            for q in chunk:
                out.setdefault(q, 0.0)
    return out


def match_trends_score(
    home_ru: str,
    away_ru: str,
    *,
    home_en: str = "",
    away_en: str = "",
    geo: str = "",
) -> float:
    """Событийный интерес к паре: max по вариантам запроса."""
    h = " ".join((home_ru or "").split()).strip()
    a = " ".join((away_ru or "").split()).strip()
    if not h or not a:
        return 0.0
    queries = [
        f"{h} {a}",
        f"{a} {h}",
        f"{h} {a} матч",
    ]
    he = " ".join((home_en or "").split()).strip()
    ae = " ".join((away_en or "").split()).strip()
    if he and ae:
        queries.extend([f"{he} {ae}", f"{ae} {he}"])

    # один payload — одна шкала
    scores = interest_for_queries(queries[:5], timeframe="now 7-d", geo=geo)
    if not scores:
        return 0.0
    best = max(scores.values()) if scores else 0.0
    print(
        f"[seo] trends {h}—{a}: {best:.1f} ({scores})",
        flush=True,
    )
    return best


def pick_by_trends(
    items: list[tuple[str, str, str, str]],
    *,
    geo: str = "",
) -> int | None:
    """items: (home_ru, away_ru, home_en, away_en). Вернуть индекс победителя или None."""
    if len(items) < 2:
        return 0 if items else None
    # Сравниваем пары батчами: для честности — общий payload из коротких «home away»
    labels = []
    for h, a, he, ae in items:
        label = f"{h} {a}".strip()
        if he and ae:
            # для сравнения в одном payload лучше короткие уникальные ярлыки
            pass
        labels.append(label)
    # pytrends сравнивает до 5 — если больше, берём попарно vs текущего лидера
    scores = interest_for_queries(labels[:5], timeframe="now 7-d", geo=geo)
    if not scores or all(v <= 0 for v in scores.values()):
        return None
    ranked = sorted(
        ((i, float(scores.get(lab) or 0.0)) for i, lab in enumerate(labels[:5])),
        key=lambda x: x[1],
        reverse=True,
    )
    best_i, best_v = ranked[0]
    second_v = ranked[1][1] if len(ranked) > 1 else 0.0
    # Отсекаем шум: слишком низкий интерес или нет явного отрыва.
    if best_v < 8.0:
        print(
            f"[seo] trends noise best={best_v:.1f} (<8) — skip",
            flush=True,
        )
        return None
    if second_v > 0 and best_v < second_v * 1.35 and (best_v - second_v) < 5.0:
        print(
            f"[seo] trends flat best={best_v:.1f} second={second_v:.1f} — skip",
            flush=True,
        )
        return None
    print(f"[seo] trends tie-break winner idx={best_i} score={best_v:.1f}", flush=True)
    return best_i
