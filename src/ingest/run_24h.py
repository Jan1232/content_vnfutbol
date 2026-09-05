"""SPEC v3.2/v3.3: разовый прогон истории за 24ч → очередь → бот ЛС.

Usage:
  # остановить realtime ingest.run, затем:
  python -m src.ingest.run_24h
  python -m src.ingest.run_24h --hours 24 --bot-only   # только бот, очередь уже есть
  python -m src.ingest.run_24h --hours 24 --collect-only --run-tag run_24h_v33
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from src.config import ROOT, _load_env
from src.generate.live_bot import build_bot, queue_watcher, _owner_id
from src.ingest import db
from src.ingest.pipeline import process_message
from src.ingest.singleton import acquire_telethon_lock
from src.ingest.sources import SOURCE_USERNAMES

log = logging.getLogger("ingest.run_24h")

DEFAULT_RUN_TAG = "run_24h_v33"


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


def _session_path() -> str:
    raw = os.environ.get("TG_SESSION_PATH", "").strip()
    if raw:
        from pathlib import Path

        return str(Path(raw).removesuffix(".session"))
    return str((ROOT / "data" / "tg_user").resolve())


async def _download_media(msg, source: str):  # type: ignore[no-untyped-def]
    if not msg.media:
        return None, None
    media_dir = ROOT / "data" / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    stem = media_dir / f"{source}_{msg.id}"
    try:
        path = await msg.download_media(file=str(stem))
    except Exception:
        log.exception("download @%s/%s", source, msg.id)
        return None, None
    if not path:
        return None, None
    low = str(path).lower()
    kind = "video" if low.endswith((".mp4", ".mov", ".mkv", ".webm", ".avi")) else "photo"
    return str(path), kind


async def collect_and_process(
    *,
    hours: int,
    limit: int = 0,
    run_tag: str = DEFAULT_RUN_TAG,
    replace_raw: bool = False,
) -> dict[str, int]:
    from telethon import TelegramClient

    _load_env()
    session = str(acquire_telethon_lock())
    api_id = int(os.environ.get("TG_API_ID") or 2040)
    api_hash = (os.environ.get("TG_API_HASH") or "b18441a1ff607e10a989891a5462e627").strip()
    client = TelegramClient(session, api_id, api_hash, proxy=_telethon_proxy())
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telethon session not authorized")

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    # собираем всё, потом сортируем хронологически
    bag: list[tuple[datetime, str, object]] = []
    for uname in SOURCE_USERNAMES:
        try:
            entity = await client.get_entity(uname)
        except Exception:
            log.exception("resolve @%s", uname)
            continue
        n = 0
        async for msg in client.iter_messages(entity, offset_date=None):
            if not msg.date:
                continue
            md = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
            if md < since:
                break
            bag.append((md, uname, msg))
            n += 1
        log.info("collected @%s last %sh: %s msgs", uname, hours, n)

    bag.sort(key=lambda x: x[0])  # хронологический порядок
    if limit and len(bag) > limit:
        log.info("trim bag %s → %s", len(bag), limit)
        bag = bag[:limit]
    stats: dict = {
        "queued": 0,
        "filtered": 0,
        "filtered_extract": 0,
        "dedup": 0,
        "skip": 0,
        "err": 0,
        "other": 0,
        "by_source_queued": {},
    }
    for md, uname, msg in bag:
        text = (msg.message or "").strip()
        media_path, media_kind = await _download_media(msg, uname)
        try:
            result = await asyncio.to_thread(
                process_message,
                source=uname,
                msg_id=int(msg.id),
                text=text,
                ts=int(md.timestamp()),
                is_forward=bool(msg.fwd_from),
                has_media_only=bool(msg.media) and not text,
                skip_generate=False,
                source_media_path=media_path,
                media_kind=media_kind,
                run_tag=run_tag,
                replace_raw=replace_raw,
            )
        except Exception:
            log.exception("process @%s/%s", uname, msg.id)
            stats["err"] += 1
            continue
        st = result.get("status") or "?"
        if st == "queued":
            stats["queued"] += 1
            by = stats["by_source_queued"]
            by[uname] = by.get(uname, 0) + 1
        elif st == "filtered_extract":
            stats["filtered_extract"] += 1
        elif st == "filtered":
            stats["filtered"] += 1
        elif st == "dedup":
            stats["dedup"] += 1
        elif st == "duplicate_raw":
            stats["skip"] += 1
        else:
            stats["other"] += 1
        log.info(
            "@%s/%s %s -> %s news=%s",
            uname,
            msg.id,
            md.isoformat(),
            st,
            result.get("news_id"),
        )

    await client.disconnect()
    print("STATS", stats)
    print(f"run_tag={run_tag} by source:", db.run_24h_counts_by_source(run_tag=run_tag))
    return stats


async def run_bot(*, run_tag: str | None = None) -> None:
    bot, dp = build_bot()
    log.info("run_24h bot polling, owner=%s run_tag=%s", _owner_id(), run_tag)
    oid = _owner_id()
    if oid:
        try:
            from src.generate.live_bot import _get_pending, _send_card, send_next

            counts = db.run_24h_counts_by_source(run_tag=run_tag)
            lines = [
                f"Прогон готов (tag={run_tag or 'all'}). Очередь по одному.",
                "По источникам в run_24h:",
            ]
            for s, n in counts:
                lines.append(f"  @{s}: {n}")
            conn = db._connect()
            pend = conn.execute(
                "SELECT COUNT(*) AS n FROM generated_live WHERE status='pending'"
            ).fetchone()["n"]
            conn.close()
            lines.append(f"В очереди pending: {pend}")
            lines.append("Кнопки: ✅❌✏️🔄 + 🔁 повтор (разметка дублей).")
            await bot.send_message(oid, "\n".join(lines))

            # после рестарта: либо дослать залипший слот, либо взять следующий
            p = _get_pending()
            if p:
                await bot.send_message(oid, "↩️ Возвращаю текущий пост (слот не закрыт):")
                await _send_card(bot, oid, p)
            else:
                await send_next(bot, oid)
        except Exception:
            log.exception("notify owner")
    asyncio.create_task(queue_watcher(bot))
    await dp.start_polling(bot)


async def main(
    *,
    hours: int,
    bot_only: bool,
    collect_only: bool,
    limit: int = 0,
    run_tag: str = DEFAULT_RUN_TAG,
    replace_raw: bool = False,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _load_env()
    if not bot_only:
        await collect_and_process(
            hours=hours,
            limit=limit,
            run_tag=run_tag,
            replace_raw=replace_raw,
        )
    if collect_only:
        return
    await run_bot(run_tag=run_tag)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--bot-only", action="store_true", help="только бот, без сбора")
    ap.add_argument("--collect-only", action="store_true", help="только сбор, без бота")
    ap.add_argument("--limit", type=int, default=0, help="макс. сообщений всего (0=без лимита)")
    ap.add_argument(
        "--run-tag",
        type=str,
        default=DEFAULT_RUN_TAG,
        help="метка сессии прогона",
    )
    ap.add_argument(
        "--replace-raw",
        action="store_true",
        help="перезаписать уже виденные raw (по умолчанию skip already-seen)",
    )
    args = ap.parse_args()
    asyncio.run(
        main(
            hours=args.hours,
            bot_only=args.bot_only,
            collect_only=args.collect_only,
            limit=args.limit,
            run_tag=args.run_tag,
            replace_raw=args.replace_raw,
        )
    )
