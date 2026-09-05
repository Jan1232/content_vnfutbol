"""Telethon listener + live bot (SPEC v3). Ровно один процесс."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from src.config import _load_env
from src.generate.live_bot import build_bot, queue_watcher, _owner_id
from src.ingest.pipeline import process_message
from src.ingest.singleton import acquire_telethon_lock
from src.ingest.sources import SOURCE_USERNAMES

log = logging.getLogger("ingest.run")


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


async def _download_media(msg, source: str) -> tuple[str | None, str | None]:  # type: ignore[no-untyped-def]
    """Скачивает фото/видео сообщения. Возвращает (path, kind)."""
    if not msg.media:
        return None, None
    from src.config import ROOT

    media_dir = ROOT / "data" / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    stem = media_dir / f"{source}_{msg.id}"
    try:
        path = await msg.download_media(file=str(stem))
    except Exception:
        log.exception("download media @%s/%s", source, msg.id)
        return None, None
    if not path:
        return None, None
    low = str(path).lower()
    kind = "video" if low.endswith((".mp4", ".mov", ".mkv", ".webm", ".avi")) else "photo"
    return str(path), kind


async def _process_tg_message(msg, source: str) -> None:  # type: ignore[no-untyped-def]
    text = (msg.message or "").strip()
    is_forward = bool(msg.fwd_from)
    has_media_only = bool(msg.media) and not text
    media_path, media_kind = await _download_media(msg, source)
    result = await asyncio.to_thread(
        process_message,
        source=source,
        msg_id=int(msg.id),
        text=text,
        ts=int(msg.date.timestamp()) if msg.date else None,
        is_forward=is_forward,
        has_media_only=has_media_only,
        skip_generate=False,
        source_media_path=media_path,
        media_kind=media_kind,
    )
    log.info("processed @%s/%s -> %s", source, msg.id, result.get("status"))


async def _warm_history(client, entities, *, limit: int) -> None:  # type: ignore[no-untyped-def]
    """Опциональный догон истории тем же клиентом (без второго процесса)."""
    if limit <= 0:
        return
    log.info("warm history limit=%s", limit)
    for ent, uname in zip(entities, SOURCE_USERNAMES[: len(entities)]):
        try:
            messages = await client.get_messages(ent, limit=limit)
        except Exception:
            log.exception("warm @%s failed", uname)
            continue
        for msg in reversed(list(messages)):
            try:
                text = (msg.message or "").strip()
                result = await asyncio.to_thread(
                    process_message,
                    source=uname,
                    msg_id=int(msg.id),
                    text=text,
                    ts=int(msg.date.timestamp()) if msg.date else None,
                    is_forward=bool(msg.fwd_from),
                    has_media_only=bool(msg.media) and not text,
                    skip_generate=True,  # не спамить ЛС при прогреве
                )
                log.info("warm @%s/%s -> %s", uname, msg.id, result.get("status"))
            except Exception:
                log.exception("warm process @%s/%s", uname, msg.id)


async def _run_telethon(*, warm_limit: int) -> None:
    from telethon import TelegramClient, events

    _load_env()
    session = str(acquire_telethon_lock())
    api_id = int(os.environ.get("TG_API_ID") or 2040)
    api_hash = (os.environ.get("TG_API_HASH") or "b18441a1ff607e10a989891a5462e627").strip()
    proxy = _telethon_proxy()

    client = TelegramClient(session, api_id, api_hash, proxy=proxy)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telethon session not authorized — run scripts/tg-login.py")

    me = await client.get_me()
    log.info("telethon ok as %s (@%s), sources=%s", me.first_name, me.username, SOURCE_USERNAMES)

    entities = []
    resolved_names: list[str] = []
    for uname in SOURCE_USERNAMES:
        try:
            ent = await client.get_entity(uname)
            entities.append(ent)
            resolved_names.append(uname)
            log.info("listening @%s", uname)
        except Exception:
            log.exception("cannot resolve @%s", uname)

    await _warm_history(client, entities, limit=warm_limit)

    name_by_id = {getattr(e, "id", None): n for e, n in zip(entities, resolved_names)}

    @client.on(events.NewMessage(chats=entities or SOURCE_USERNAMES))
    async def handler(event):  # type: ignore[no-untyped-def]
        try:
            msg = event.message
            chat = await event.get_chat()
            source = (
                getattr(chat, "username", None)
                or name_by_id.get(getattr(chat, "id", None))
                or str(getattr(chat, "id", "?"))
            )
            await _process_tg_message(msg, source)
        except Exception:
            log.exception("handler error")

    log.info("realtime listening (single process)")
    await client.run_until_disconnected()


async def main(*, warm_limit: int = 0) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _load_env()
    bot, dp = build_bot()
    log.info("starting live ingest+bot (single process), owner=%s", _owner_id())

    async def bot_task() -> None:
        asyncio.create_task(queue_watcher(bot))
        await dp.start_polling(bot)

    await asyncio.gather(_run_telethon(warm_limit=warm_limit), bot_task())


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Единый процесс: Telethon + бот ЛС")
    ap.add_argument(
        "--warm",
        type=int,
        default=0,
        help="при старте догнать N последних постов на канал (без генерации в ЛС)",
    )
    args = ap.parse_args()
    asyncio.run(main(warm_limit=args.warm))
