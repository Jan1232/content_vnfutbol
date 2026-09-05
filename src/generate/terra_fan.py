"""Прогон Terra-веера: 10 фактов × 5 вариантов. Без критика."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.generate.eval_facts import EVAL_FACTS
from src.generate.fan import generate_fan

# Спека §5 — акцент на искре / контрасте
DEFAULT_IDS = (27, 30, 20, 33, 41, 11, 3, 49, 24, 15)


def _items_by_ids(ids: tuple[int, ...]) -> list[dict]:
    by_id = {x["id"]: x for x in EVAL_FACTS}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"id не найдены в EVAL_FACTS: {missing}")
    return [by_id[i] for i in ids]


def _format_block(item: dict, variants: list[dict]) -> str:
    real = item.get("real_post") or "(эталон не сохранён)"
    lines = [
        "════════════════════════════════════════════",
        f"ФАКТ #{item['id']} | {item['archetype']} | {item['veracity']} | "
        f"sensation={item['is_sensation']}",
        item["fact"],
        "──── ОРИГИНАЛ КАНАЛА (для ориентира) ────",
        real,
        "──── TERRA, 5 вариантов ────",
    ]
    for v in variants:
        flag_note = ""
        if v["flags"]:
            flag_note = "  ⚠ " + "; ".join(v["flags"])
        lines.append(f"[{v['index']}] ({v['direction']}){flag_note}")
        lines.append(v["post"])
        lines.append("")
    lines.append("════════════════════════════════════════════")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Terra fan: N фактов × 5 вариантов")
    parser.add_argument(
        "--ids",
        type=str,
        default=",".join(str(i) for i in DEFAULT_IDS),
        help="Список id через запятую (из eval_facts)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("terra_run_1.txt"),
        help="Файл вывода",
    )
    args = parser.parse_args()
    ids = tuple(int(x.strip()) for x in args.ids.split(",") if x.strip())
    items = _items_by_ids(ids)

    out_f = args.out.open("w", encoding="utf-8")

    def emit(text: str) -> None:
        print(text, end="" if text.endswith("\n") else "\n", flush=True)
        out_f.write(text if text.endswith("\n") else text + "\n")
        out_f.flush()

    emit(f"Terra fan run: {len(items)} фактов × 5 = {len(items) * 5} текстов\n\n")

    flag_hits = 0
    for i, item in enumerate(items, 1):
        emit(f">>> [{i}/{len(items)}] id={item['id']} {item['archetype']}\n")
        try:
            variants = generate_fan(
                fact=item["fact"],
                veracity=item["veracity"],
                archetype=item["archetype"],
                is_sensation=item["is_sensation"],
                n=5,
            )
        except Exception as exc:  # noqa: BLE001
            emit(f"[ERROR] id={item['id']}: {exc}\n\n")
            continue

        for v in variants:
            if v["flags"]:
                flag_hits += 1
        emit(_format_block(item, variants))

    emit(f"\nГотово. Вариантов с пометками guardrail: {flag_hits}\n")
    out_f.close()
    print(f"Записано в {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
