#!/usr/bin/env python3
"""Собрать пул разметки фото из JSONL-следов — аналог pool_14d для новостей.

В каждом item: keep_photo=null. Разметка: true = выбранный кадр ок в прод,
false = не то фото (в note: кто должен быть / почему дроп).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from editorial.imagery_trace import TRACE_DIR, load_traces, trace_path


def _item_from_trace(trace: dict, idx: int) -> dict:
    vision = trace.get("vision") or {}
    cands = list(vision.get("candidates") or [])
    pick = trace.get("pick") or {}
    return {
        "id": f"img-{idx:04d}",
        "news_id": trace.get("news_id"),
        "title": trace.get("title") or "",
        "url": trace.get("url") or "",
        "event_type": trace.get("event_type") or "",
        "query": trace.get("query") or "",
        "outcome": trace.get("outcome") or "",
        "entities": trace.get("entities") or {},
        "picked": {
            "url": pick.get("url") or "",
            "via": pick.get("via") or "",
            "score": pick.get("score"),
            "reason": pick.get("reason") or "",
            "who": pick.get("who") or "",
            "path": pick.get("path") or "",
        },
        "vision_candidates": cands,
        "quality_drops_n": len(trace.get("quality_drops") or []),
        "keep_photo": None,
        "better_idx": None,
        "note": "",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="", help="YYYY-MM-DD, иначе все jsonl")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--out",
        default=str(ROOT / "data" / "editorial" / "labeling" / "imagery_pool.json"),
    )
    args = parser.parse_args()
    src = trace_path() if not args.date else TRACE_DIR / f"{args.date}.jsonl"
    traces = load_traces(src if args.date else None, limit=args.limit)
    items = [_item_from_trace(t, i + 1) for i, t in enumerate(traces)]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "imagery_labeling",
        "n": len(items),
        "source": str(src if args.date else TRACE_DIR),
        "instruction": (
            "keep_photo: true если выбранный кадр (picked) можно в канал; "
            "false если нет. better_idx — индекс vision_candidates, который "
            "взял бы ты. note — коротко: не тот игрок / чужой клуб / коллаж / ок."
        ),
        "items": items,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[imagery-pool] n={len(items)} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
