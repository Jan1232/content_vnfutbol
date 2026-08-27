#!/usr/bin/env python3
"""Smoke: editorial ходит в Platform OpenAI API, не в OpenClaw."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.db import init_db
from editorial.openai_client import OpenAIClient, assert_platform_transport, get_client, reset_client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument("--image", action="store_true", help="вызвать generate_image (платно)")
    args = parser.parse_args()

    init_db()
    reset_client()
    settings = get_settings()
    transport = (settings.editorial_llm_transport or "").strip().lower()
    if transport != "openai":
        raise SystemExit(f"FAIL: EDITORIAL_LLM_TRANSPORT={transport}, нужен openai")
    base = (settings.editorial_openai_base_url or settings.openai_base_url or "").strip()
    assert_platform_transport(base)
    if "127.0.0.1:18789" in base or "openclaw" in base.lower():
        raise SystemExit("FAIL: editorial base_url указывает на OpenClaw")

    client = get_client()
    if "api.openai.com" not in client.base_url:
        raise SystemExit(f"FAIL: client.base_url={client.base_url}")

    text_model = settings.editorial_text_model
    fallback = settings.editorial_text_fallback
    print(f"[smoke] transport=openai base={client.base_url} model={text_model} fallback={fallback}")

    reply = client.chat(
        text_model,
        [
            {"role": "system", "content": "Отвечай одной строкой."},
            {"role": "user", "content": "Напиши ровно: editorial-ok"},
        ],
        max_tokens=32,
        fallback=fallback,
        task="smoke",
    )
    print(f"[smoke] chat={reply[:200]!r}")
    if not reply.strip():
        raise SystemExit("FAIL: пустой chat")

    if not args.skip_search:
        hits = client.web_search(
            settings.editorial_search_model,
            "Мбаппе Реал Мадрид трансфер независимые источники",
            max_results=8,
            task="search",
        )
        print(f"[smoke] search hits={len(hits)}")
        for h in hits[:6]:
            print(f"  - {h.get('domain')}: {h.get('title')[:80]}")
        domains = {h.get("domain") for h in hits if h.get("domain")}
        if len(domains) < 3:
            raise SystemExit(f"FAIL: search вернул мало доменов: {domains}")

    if args.image or settings.editorial_image_gen_fallback:
        raw = client.generate_image(
            settings.editorial_image_model,
            "Abstract football stadium lights, no faces, no text",
            size="1024x1536",
            task="image",
        )
        out = ROOT / "data" / "editorial" / "covers" / "smoke_gen.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
        print(f"[smoke] image bytes={len(raw)} path={out}")

    from editorial.usage import daily_usage_summary

    usage = daily_usage_summary()
    print(
        f"[smoke] usage 24h n={usage['n']} in={usage['prompt_tokens']} "
        f"out={usage['completion_tokens']} usd≈{usage['usd']:.4f}"
    )
    if usage["n"] < 1:
        raise SystemExit("FAIL: usage-таблица пуста")
    print("[smoke] OK platform api")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
