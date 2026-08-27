"""Скачивание медиа из публичных TG-каналов через Telethon (для «Media is too big»)."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import get_settings

# Официальные ключи Telegram Desktop (публичные в исходниках tdesktop).
# Можно переопределить через TG_API_ID / TG_API_HASH.
_DEFAULT_API_ID = 2040
_DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"

_TG_REF_RE = re.compile(r"^(?:tg:)?([A-Za-z0-9_]{4,})/(\d+)$")


def _api_credentials() -> tuple[int, str]:
    s = get_settings()
    api_id = int(s.tg_api_id or _DEFAULT_API_ID)
    api_hash = (s.tg_api_hash or _DEFAULT_API_HASH).strip()
    return api_id, api_hash


def session_path() -> Path:
    s = get_settings()
    raw = (s.tg_session_path or "").strip()
    if raw:
        return Path(raw)
    return s.data_dir / "tg_user.session"


_auth_cache: tuple[float, bool] | None = None


def is_tg_ready() -> bool:
    """Есть ли авторизованная user-сессия Telethon (не просто файл .session)."""
    global _auth_cache
    p = session_path()
    if not (
        p.exists()
        or Path(str(p) + ".session").exists()
        or Path(f"{p}.session").exists()
    ):
        return False
    now = __import__("time").time()
    if _auth_cache and now - _auth_cache[0] < 60.0:
        return _auth_cache[1]
    try:
        ok = bool(asyncio.run(_check_authorized()))
    except Exception as e:
        print(f"[tg] auth check fail: {e}", flush=True)
        ok = False
    _auth_cache = (now, ok)
    return ok


async def _check_authorized() -> bool:
    from telethon import TelegramClient

    api_id, api_hash = _api_credentials()
    path = session_path()
    session = str(path).removesuffix(".session")
    client = TelegramClient(session, api_id, api_hash, proxy=_proxy_dict())
    await client.connect()
    try:
        return bool(await client.is_user_authorized())
    finally:
        await client.disconnect()


def invalidate_tg_auth_cache() -> None:
    global _auth_cache
    _auth_cache = None


def _proxy_dict() -> dict[str, Any] | None:
    s = get_settings()
    raw = (s.scraper_http_proxy or s.groq_http_proxy or "").strip()
    if not raw:
        return None
    u = urlparse(raw)
    if not u.hostname or not u.port:
        return None
    # Telethon: (proxy_type, addr, port, rdns, username, password)
    # или dict для python-socks
    return {
        "proxy_type": "http",
        "addr": u.hostname,
        "port": u.port,
        "username": u.username,
        "password": u.password,
        "rdns": True,
    }


def parse_tg_ref(ref: str) -> tuple[str, int] | None:
    m = _TG_REF_RE.match((ref or "").strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _cache_dir() -> Path:
    d = get_settings().data_dir / "tg_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _download_async(username: str, msg_id: int) -> Path | None:
    from telethon import TelegramClient
    from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

    api_id, api_hash = _api_credentials()
    path = session_path()
    # Telethon добавляет .session сам, если передать путь без суффикса
    session = str(path).removesuffix(".session")
    proxy = _proxy_dict()

    client = TelegramClient(session, api_id, api_hash, proxy=proxy)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("[tg] session not authorized — run scripts/tg-login.py", flush=True)
            return None

        entity = await client.get_entity(username)
        msg = await client.get_messages(entity, ids=msg_id)
        if not msg or not msg.media:
            print(f"[tg] no media for {username}/{msg_id}", flush=True)
            return None

        cache = _cache_dir()
        out_base = cache / f"{username}_{msg_id}"
        # уже скачано?
        for existing in cache.glob(f"{username}_{msg_id}.*"):
            if existing.is_file() and existing.stat().st_size > 0:
                return existing

        if isinstance(msg.media, (MessageMediaDocument, MessageMediaPhoto)) or msg.video or msg.document:
            path_out = await client.download_media(msg, file=str(out_base))
            if path_out:
                p = Path(path_out)
                if p.exists() and p.stat().st_size > 0:
                    print(
                        f"[tg] downloaded {username}/{msg_id} -> {p} ({p.stat().st_size} bytes)",
                        flush=True,
                    )
                    return p
        print(f"[tg] download empty for {username}/{msg_id}", flush=True)
        return None
    finally:
        await client.disconnect()


def download_tg_media(tg_ref: str) -> Path | None:
    """Скачать медиа по ссылке вида Neymar_jru/7215. None если нет сессии/ошибка."""
    parsed = parse_tg_ref(tg_ref)
    if not parsed:
        return None
    username, msg_id = parsed
    if not is_tg_ready():
        print(f"[tg] skip download {tg_ref}: session not authorized — run scripts/tg-login.py", flush=True)
        return None
    try:
        return asyncio.run(_download_async(username, msg_id))
    except Exception as e:
        print(f"[tg] download fail {tg_ref}: {e}", flush=True)
        return None


def resolve_media_item(item: dict[str, Any]) -> dict[str, Any]:
    """Если video/image без url / too_big — скачать через MTProto и проставить file:// url."""
    mtype = (item.get("type") or "").lower()
    if mtype not in {"video", "image"}:
        return item
    url = (item.get("url") or "").strip()
    if url and not item.get("too_big"):
        return item
    tg_ref = (item.get("tg_ref") or "").strip()
    if not tg_ref:
        return item
    path = download_tg_media(tg_ref)
    if not path:
        return item
    out = dict(item)
    out["url"] = path.resolve().as_uri()  # file:///...
    out["too_big"] = False
    out["local_path"] = str(path.resolve())
    return out


def resolve_media_list(media: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Скачать все too_big / без url элементы с tg_ref."""
    out: list[dict[str, Any]] = []
    for item in media or []:
        if isinstance(item, dict):
            out.append(resolve_media_item(item))
        else:
            out.append(item)
    return out


def media_needs_tg_download(media: list[dict[str, Any]] | None) -> bool:
    for m in media or []:
        if not isinstance(m, dict):
            continue
        if (m.get("type") or "").lower() not in {"image", "video"}:
            continue
        if m.get("too_big") or (m.get("tg_ref") and not (m.get("url") or "").strip()):
            return True
    return False
