"""Цикл Producer → Critic → rewrite. Точка входа CLI."""

from __future__ import annotations

import argparse
import json
import sys

from src.config import ARCHETYPES, VERACITY_LEVELS
from src.generate import critic, producer


def generate_final(
    fact: str,
    veracity: str,
    archetype: str,
    is_sensation: bool,
    max_retries: int = 2,
) -> tuple[str, str, list[dict]]:
    trace: list[dict] = []
    trace.append(
        {
            "step": "input",
            "fact": fact,
            "veracity": veracity,
            "archetype": archetype,
            "is_sensation": is_sensation,
        }
    )

    post = producer.generate_post(fact, veracity, archetype, is_sensation)
    trace.append({"step": "produce", "attempt": 0, "post": post})

    last_review: dict | None = None

    for attempt in range(max_retries + 1):
        review_result = critic.review(
            post, fact, veracity, is_sensation, archetype
        )
        last_review = review_result
        trace.append({"step": "review", "attempt": attempt, "review": review_result})

        if review_result["verdict"] == "pass":
            return post, "approved", trace

        if attempt >= max_retries:
            break

        feedback = (
            f"{post}\n\n"
            f"Замечания: {review_result['issues']} | {review_result['how_to_improve']}"
        )
        post = producer.generate_post(
            fact, veracity, archetype, is_sensation, feedback=feedback
        )
        trace.append({"step": "produce", "attempt": attempt + 1, "post": post})

    assert last_review is not None
    if last_review.get("hard_violations"):
        return post, "blocked_hard", trace
    return post, "soft_fail", trace


def _print_trace(trace: list[dict]) -> None:
    print("\n" + "=" * 60)
    print("TRACE")
    print("=" * 60)
    for entry in trace:
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        print("-" * 40)


def _interactive_input() -> tuple[str, str, str, bool]:
    print("Интерактивный режим генерации поста\n")
    fact = input("Факт: ").strip()
    if not fact:
        print("Факт не может быть пустым.", file=sys.stderr)
        sys.exit(1)

    print(f"Достоверность ({', '.join(VERACITY_LEVELS)}): ", end="")
    veracity = input().strip() or "verified"
    if veracity not in VERACITY_LEVELS:
        print(f"Неверная достоверность. Допустимо: {VERACITY_LEVELS}", file=sys.stderr)
        sys.exit(1)

    print(f"Архетип ({', '.join(ARCHETYPES)}): ", end="")
    archetype = input().strip() or "transfer"
    if archetype not in ARCHETYPES:
        print(f"Неверный архетип. Допустимо: {ARCHETYPES}", file=sys.stderr)
        sys.exit(1)

    sensation_raw = input("Сенсация? (y/n): ").strip().lower()
    is_sensation = sensation_raw in ("y", "yes", "да", "1", "true")

    return fact, veracity, archetype, is_sensation


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Генерация Telegram-поста: Producer → Critic → rewrite"
    )
    parser.add_argument("--fact", help="Сухой факт")
    parser.add_argument(
        "--veracity",
        choices=VERACITY_LEVELS,
        default="verified",
        help="Метка достоверности",
    )
    parser.add_argument(
        "--archetype",
        choices=ARCHETYPES,
        default="transfer",
        help="Архетип поста",
    )
    parser.add_argument(
        "--sensation",
        action="store_true",
        help="Сенсация (плашка-заголовок)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Макс. число rewrite-проходов",
    )
    args = parser.parse_args()

    if args.fact:
        fact, veracity, archetype, is_sensation = (
            args.fact,
            args.veracity,
            args.archetype,
            args.sensation,
        )
    else:
        fact, veracity, archetype, is_sensation = _interactive_input()

    post, status, trace = generate_final(
        fact=fact,
        veracity=veracity,
        archetype=archetype,
        is_sensation=is_sensation,
        max_retries=args.max_retries,
    )

    _print_trace(trace)

    print("\n" + "=" * 60)
    print(f"СТАТУС: {status}")
    print("=" * 60)
    if status == "blocked_hard":
        print("⚠️  HARD-нарушение — пост НЕ для публикации без ручной правки.\n")
    elif status == "soft_fail":
        print("⚠️  Мягкий фейл — пост на ручную проверку.\n")

    print(post)


if __name__ == "__main__":
    main()
