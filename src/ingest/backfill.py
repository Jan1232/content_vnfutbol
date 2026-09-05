"""Одноразовый бэкфилл истории. НЕ запускать параллельно с ingest.run
(общий flock на tg_user.lock). Предпочтительно: python -m src.ingest.run --warm N
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from src.config import _load_env
from src.ingest.pipeline import process_message
from src.ingest.singleton import acquire_telethon_lock
from src.ingest.sources import SOURCE_USERNAMES

log = logging.getLogger("ingest.backfill")


def _telethon_proxy():
    from urllib.parse import urlparse

    raw = (
        os.environ.get("SCRAPER_HTTP_PROXY", "").strip()
        or os.environ.get("OPENAI_HTTP_PROXY", "").strip()
    )
    if not raw:
        return None
    u = urlparse(raw)
    if not u.hostname or not u.port:
        return None
    return {
        "proxy_type": "http",
        "addr": u.hostname,
        "port": u.port,
        "username": u.username,
        "password": u.password,
        "rdns": True,
    }


async def backfill(*, limit: int, generate_per_source: int, only: list[str] | None = None) -> None:
    from telethon import TelegramClient

    _load_env()
    session = str(acquire_telethon_lock())
    api_id = int(os.environ.get("TG_API_ID") or 2040)
    api_hash = (os.environ.get("TG_API_HASH") or "b18441a1ff607e10a989891a5462e627").strip()
    client = TelegramClient(session, api_id, api_hash, proxy=_telethon_proxy())
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telethon session not authorized")

    sources = only or SOURCE_USERNAMES
    totals: dict[str, dict[str, int]] = {}
    for uname in sources:
        totals[uname] = {
            "filtered": 0,
            "dedup": 0,
            "fact_only": 0,
            "queued": 0,
            "skip": 0,
            "err": 0,
            "ok": 0,
        }
        gen_left = generate_per_source
        try:
            entity = await client.get_entity(uname)
        except Exception:
            log.exception("resolve @%s", uname)
            continue

        messages = await client.get_messages(entity, limit=limit)
        for msg in reversed(list(messages)):
            text = (msg.message or "").strip()
            is_forward = bool(msg.fwd_from)
            has_media_only = bool(msg.media) and not text
            do_gen = gen_left > 0
            try:
                result = await asyncio.to_thread(
                    process_message,
                    source=uname,
                    msg_id=int(msg.id),
                    text=text,
                    ts=int(msg.date.timestamp()) if msg.date else None,
                    is_forward=is_forward,
                    has_media_only=has_media_only,
                    skip_generate=not do_gen,
                )
            except Exception:
                log.exception("process @%s/%s", uname, msg.id)
                totals[uname]["err"] += 1
                continue

            st = result.get("status") or "?"
            if st == "duplicate_raw":
                totals[uname]["skip"] += 1
            elif st == "filtered":
                totals[uname]["filtered"] += 1
            elif st == "dedup":
                totals[uname]["dedup"] += 1
            elif st == "fact_only":
                totals[uname]["fact_only"] += 1
            elif st == "queued":
                totals[uname]["queued"] += 1
                gen_left -= 1
            elif st in ("embed_error", "extract_error", "generate_error"):
                totals[uname]["err"] += 1
            else:
                totals[uname]["ok"] += 1
            log.info("@%s/%s -> %s", uname, msg.id, st)

        print(f"@{uname}: {totals[uname]}")

    await client.disconnect()
    print("DONE", totals)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40, help="сообщений на канал")
    ap.add_argument("--generate", type=int, default=0, help="сколько постов в ЛС на канал")
    ap.add_argument("--only", nargs="*", default=None, help="только эти username")
    args = ap.parse_args()
    asyncio.run(
        backfill(
            limit=args.limit,
            generate_per_source=args.generate,
            only=args.only,
        )
    )


if __name__ == "__main__":
    main()
