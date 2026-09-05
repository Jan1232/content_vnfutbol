"""Выгрузки для владельца (критерии 1,2,4,7)."""

from __future__ import annotations

import argparse
import json

from src.ingest import db


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=20, help="пар источник→факт")
    ap.add_argument("--filtered", type=int, default=20)
    ap.add_argument("--source", type=str, default="", help="сэмпл raw по каналу")
    args = ap.parse_args()

    print("=== raw_messages COUNT BY SOURCE ===")
    for src, n in db.raw_counts_by_source():
        print(f"  @{src}: {n}")

    print("\n=== FILTERED SAMPLE ===")
    for r in db.filtered_sample(args.filtered):
        text = (r["text"] or "").replace("\n", " ")[:120]
        print(f"[{r['source']}/{r['msg_id']}] {r['filter_reason']}: {text}")

    print("\n=== FACT PAIRS (source → fact) ===")
    for r in db.fact_pairs_sample(args.pairs):
        src = (r["source_text"] or "").replace("\n", " ")[:160]
        print("---")
        print(f"@{r['source']}: {src}")
        print(f"→ [{r['archetype']}|{r['veracity']}|attr={r['attribution']}] {r['fact']}")

    if args.source:
        print(f"\n=== RAW @{args.source} ===")
        for r in db.raw_sample(args.source, 30):
            flag = "FILTERED" if r["is_filtered"] else "ok"
            text = (r["text"] or "").replace("\n", " ")[:140]
            print(f"[{flag}|{r['filter_reason']}] {text}")

    print("\n=== LIVE SUMMARY (is_test=false) ===")
    print(json.dumps(db.live_summary(include_test=False), ensure_ascii=False))
    print("LIVE SUMMARY all:", json.dumps(db.live_summary(include_test=True), ensure_ascii=False))
    rows = db.analysis_log_rows(include_test=False)
    print(f"analysis rows is_test=false: {len(rows)}")


if __name__ == "__main__":
    main()
