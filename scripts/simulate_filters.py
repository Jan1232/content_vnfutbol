#!/usr/bin/env python3
"""Симуляция фильтров на последних N постах каждого источника (без публикации в MAX)."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.filter import is_advertisement, sanitize_for_publish  # noqa: E402
from parsers import telegram as tgmod  # noqa: E402
from parsers.telegram import parse_telegram  # noqa: E402

# Эвристика для отчёта: «похоже на рекламу, но фильтр пропустил»
SUSPICIOUS_RE = re.compile(
    r"(?iu)"
    r"(розыгрыш|разыгрыва|конкурс|участв|приз(?:ы|ов)?|"
    r"реклам|промокод|скидк|залетай|масштабн|автограф|"
    r"минут до итог|фрибет|букмекер|казино|giveaway|"
    r"🐐\s*x\s*🐐|erid|спонсор)"
)


def _normalize_keep_before(url: str) -> str | None:
    m = tgmod.TG_RE.search(url.strip())
    if not m:
        return None
    username = m.group(1)
    if username.lower() in {"joinchat", "addstickers", "share", "proxy", "socks", "iv"}:
        return None
    out = f"https://t.me/s/{username}"
    before = parse_qs(urlparse(url).query).get("before", [None])[0]
    if before:
        out += f"?before={before}"
    return out


def fetch_telegram_posts(url: str, limit: int):
    """Последние `limit` постов с пагинацией ?before= (новые первыми в результате)."""
    base = tgmod.normalize_telegram_url(url)
    if not base:
        raise ValueError(f"bad telegram url: {url}")
    username = base.rsplit("/", 1)[-1]

    collected = []
    seen: set[str] = set()
    before: int | None = None
    orig_norm = tgmod.normalize_telegram_url
    tgmod.normalize_telegram_url = _normalize_keep_before
    try:
        while len(collected) < limit:
            page_url = f"https://t.me/s/{username}"
            if before is not None:
                page_url += f"?before={before}"
            _title, posts = parse_telegram(page_url)
            if not posts:
                break
            # parse отдаёт старые→новые; для пагинации берём min id
            page_nums = []
            added = 0
            for p in reversed(posts):  # новые сначала
                if p.external_id in seen:
                    continue
                seen.add(p.external_id)
                collected.append(p)
                added += 1
                try:
                    page_nums.append(int(p.external_id.rsplit("/", 1)[-1]))
                except ValueError:
                    pass
                if len(collected) >= limit:
                    break
            if not added or not page_nums:
                break
            before = min(page_nums)
            if before <= 1:
                break
    finally:
        tgmod.normalize_telegram_url = orig_norm

    return collected[:limit]


def fetch_x_posts(url: str, limit: int):
    from parsers.x import parse_x

    _title, posts = parse_x(url, since_id="", enrich_media=False)
    posts = list(reversed(posts))  # новые сначала, если старые→новые
    return posts[:limit]


def load_sources(db_path: Path, source_ids: list[int] | None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, title, kind, url FROM sources ORDER BY id"
    ).fetchall()
    conn.close()
    sources = [dict(r) for r in rows]
    if source_ids:
        want = set(source_ids)
        sources = [s for s in sources if s["id"] in want]
    return sources


def simulate(text: str, media: list) -> dict:
    raw = is_advertisement(text, media)
    sanitized = sanitize_for_publish(text or "")
    after = is_advertisement(sanitized, media)
    would = (not raw.is_ad) and (not after.is_ad)
    return {
        "raw": raw,
        "sanitized": sanitized,
        "after": after,
        "would_publish": would,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--source-id", type=int, action="append")
    ap.add_argument("--db", default=str(ROOT / "data" / "app.db"))
    args = ap.parse_args()

    sources = load_sources(Path(args.db), args.source_id)
    print(f"=== Filter simulation (limit={args.limit}, read-only, NO publish) ===")
    print(f"Sources: {len(sources)}\n")

    total = 0
    would_total = 0
    blocked_total = 0
    leaks: list[tuple] = []
    blocked_samples: list[tuple] = []

    for src in sources:
        sid, title, kind, url = src["id"], src["title"], src["kind"], src["url"]
        print(f"--- [{sid}] {title} ({kind}) ---")
        try:
            if kind == "telegram":
                posts = fetch_telegram_posts(url, args.limit)
            elif kind == "x":
                posts = fetch_x_posts(url, args.limit)
            else:
                print(f"  SKIP unsupported kind={kind}\n")
                continue
        except Exception as exc:
            print(f"  ERROR fetch: {exc}\n")
            continue

        blocked = 0
        would = 0
        reasons: dict[str, int] = {}
        src_leaks = []

        for p in posts:
            text = getattr(p, "text", None) or (p.get("text") if isinstance(p, dict) else "") or ""
            media = getattr(p, "media", None) or (p.get("media") if isinstance(p, dict) else []) or []
            eid = getattr(p, "external_id", None) or (p.get("external_id") if isinstance(p, dict) else "?")
            res = simulate(text, media)
            if res["would_publish"]:
                would += 1
                if SUSPICIOUS_RE.search(text):
                    preview = text[:160].replace("\n", " ")
                    src_leaks.append((eid, preview))
                    leaks.append((sid, title, eid, preview))
            else:
                blocked += 1
                reason = res["raw"].reason if res["raw"].is_ad else res["after"].reason
                reasons[reason or "?"] = reasons.get(reason or "?", 0) + 1
                if len(blocked_samples) < 40:
                    preview = text[:120].replace("\n", " ")
                    blocked_samples.append((sid, title, eid, reason, preview))

        total += len(posts)
        would_total += would
        blocked_total += blocked
        print(f"  fetched: {len(posts)}")
        print(f"  blocked: {blocked}")
        print(f"  would_publish: {would}")
        if reasons:
            top = sorted(reasons.items(), key=lambda x: -x[1])[:8]
            print("  block reasons: " + ", ".join(f"{k}={v}" for k, v in top))
        if src_leaks:
            print(f"  SUSPICIOUS leaks (heuristic): {len(src_leaks)}")
            for eid, preview in src_leaks[:8]:
                print(f"    - {eid}: {preview}")
        print()

    print("=== SUMMARY ===")
    print(f"posts checked: {total}")
    print(f"blocked: {blocked_total}")
    print(f"would_publish: {would_total}")
    print(f"suspicious_leaks: {len(leaks)}")
    if leaks:
        print("\nSuspicious posts that WOULD pass filters:")
        for sid, title, eid, preview in leaks:
            print(f"  [{sid}] {title} {eid}: {preview}")
    else:
        print("No heuristic-suspicious posts among would_publish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
