"""Batch-прогон EVAL_FACTS: наш выход рядом с оригиналом канала."""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from src.config import ARCHETYPES
from src.generate.eval_facts import EVAL_FACTS
from src.generate.pipeline import generate_final


def _format_block(
    item: dict,
    post: str,
    status: str,
    attempts: int,
) -> str:
    real = item.get("real_post")
    real_block = real if real else "(эталон не сохранён)"
    lines = [
        "─────────────────────────────────────────────",
        f"#{item['id']} | {item['archetype']} | {item['veracity']} | "
        f"sensation={item['is_sensation']}",
        f"ФАКТ: {item['fact']}",
        f"─── НАШ ВЫХОД (status: {status}, попыток: {attempts}) ───",
        post,
        "─── ОРИГИНАЛ КАНАЛА ───",
        real_block,
        "─────────────────────────────────────────────",
        "",
    ]
    return "\n".join(lines)


def _run(
    *,
    archetype: str | None,
    limit: int | None,
    only_sensation: bool,
    out_path: Path | None,
) -> None:
    items = list(EVAL_FACTS)
    if archetype:
        items = [x for x in items if x["archetype"] == archetype]
    if only_sensation:
        items = [x for x in items if x["is_sensation"]]
    if limit is not None:
        items = items[:limit]

    if not items:
        print("Нет кейсов после фильтров.", file=sys.stderr)
        sys.exit(1)

    out_f = out_path.open("w", encoding="utf-8") if out_path else None

    def emit(text: str) -> None:
        print(text, end="" if text.endswith("\n") else "\n", flush=True)
        if out_f:
            out_f.write(text if text.endswith("\n") else text + "\n")
            out_f.flush()

    status_counts: Counter[str] = Counter()
    attempts_total = 0
    by_arch: dict[str, Counter[str]] = defaultdict(Counter)
    failed_ids: list[int] = []

    emit(f"Batch eval: {len(items)} кейсов\n\n")

    for i, item in enumerate(items, 1):
        emit(f">>> [{i}/{len(items)}] id={item['id']} {item['archetype']}\n")
        try:
            post, status, trace = generate_final(
                fact=item["fact"],
                veracity=item["veracity"],
                archetype=item["archetype"],
                is_sensation=item["is_sensation"],
                max_retries=2,
            )
            attempts = sum(1 for t in trace if t.get("step") == "produce")
        except Exception as exc:  # noqa: BLE001 — batch не должен падать целиком
            post = f"[ERROR] {exc}"
            status = "error"
            attempts = 0

        status_counts[status] += 1
        by_arch[item["archetype"]][status] += 1
        attempts_total += attempts
        if status != "approved":
            failed_ids.append(item["id"])

        emit(_format_block(item, post, status, attempts))

    n = len(items)
    avg_attempts = attempts_total / n if n else 0.0
    summary = [
        "",
        "═════════════════════════════════════════════",
        "СВОДКА",
        "═════════════════════════════════════════════",
        f"Всего: {n}",
        f"approved: {status_counts.get('approved', 0)}",
        f"soft_fail: {status_counts.get('soft_fail', 0)}",
        f"blocked_hard: {status_counts.get('blocked_hard', 0)}",
        f"error: {status_counts.get('error', 0)}",
        f"Среднее попыток: {avg_attempts:.2f}",
        "",
        "По архетипам:",
    ]
    for arch in sorted(by_arch):
        c = by_arch[arch]
        parts = ", ".join(f"{k}={v}" for k, v in sorted(c.items()))
        summary.append(f"  {arch}: {parts}")
    summary.append("")
    summary.append(
        f"id где status != approved: {failed_ids if failed_ids else '—'}"
    )
    summary.append("")
    emit("\n".join(summary))

    if out_f:
        out_f.close()
        print(f"\nЗаписано в {out_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Прогон EVAL_FACTS через Producer→Critic с сравнением тона"
    )
    parser.add_argument(
        "--archetype",
        choices=ARCHETYPES,
        help="Только один архетип",
    )
    parser.add_argument("--limit", type=int, help="Ограничить число кейсов")
    parser.add_argument(
        "--only-sensation",
        action="store_true",
        help="Только is_sensation=true",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Дублировать вывод в файл",
    )
    args = parser.parse_args()
    _run(
        archetype=args.archetype,
        limit=args.limit,
        only_sensation=args.only_sensation,
        out_path=args.out,
    )


if __name__ == "__main__":
    main()
