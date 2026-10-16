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
_flood_until = 0.0
_FLOOD_SEC_RE = re.compile(r"wait of (\d+) seconds", re.I)


def note_tg_flood(exc: BaseException | str) -> None:
    """Запомнить FloodWait, чтобы не долбить ResolveUsername на каждый пост."""
    global _flood_until
    sec = 0
    attr = getattr(exc, "seconds", None)
    if isinstance(attr, (int, float)) and attr > 0:
        sec = int(attr)
    m = _FLOOD_SEC_RE.search(str(exc))
    if m:
        sec = max(sec, int(m.group(1)))
    if sec <= 0:
        return
    until = __import__("time").time() + sec
    if until > _flood_until:
        _flood_until = until
        print(f"[tg] floodwait: pause {sec}s, skip MTProto", flush=True)


def tg_is_flood_blocked() -> bool:
    return __import__("time").time() < _flood_until


def is_tg_ready() -> bool:
    """Есть ли авторизованная user-сессия Telethon (не просто файл .session)."""
    global _auth_cache
    if tg_is_flood_blocked():
        return False
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
        note_tg_flood(e)
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
        if (m.get("type") or "").lower() == "video" and m.get("too_big"):
            continue
        if m.get("too_big") or (m.get("tg_ref") and not (m.get("url") or "").strip()):
            return True
    return False


def _media_count(media: list[dict[str, Any]] | None) -> int:
    n = 0
    for m in media or []:
        if isinstance(m, dict) and (m.get("type") or "").lower() in {"image", "video"}:
            n += 1
    return n


def telegram_media_needs_album_expand(external_id: str, media: list[dict[str, Any]] | None) -> bool:
    """Нужно ли добрать альбом: пусто или старый парсер схлопнул группу в 1–2 файла."""
    ext = (external_id or "").strip()
    if not ext.startswith("tg:"):
        return False
    items = [
        m
        for m in (media or [])
        if isinstance(m, dict) and (m.get("type") or "").lower() in {"image", "video"}
    ]
    if len(items) == 0:
        # Текст без медиа: не ходим в Telethon (FloodWait копит пачку).
        return False
    if len(items) >= 10:
        return False
    post_ref = ext.replace("tg:", "", 1)
    refs = {(m.get("tg_ref") or "").split("?")[0] for m in items if m.get("tg_ref")}
    if not refs:
        return True
    if len(items) <= 2 and refs <= {post_ref}:
        return True
    return False


def complete_telegram_media(
    external_id: str, media: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Для любого назначения: если альбом обрезан — подставить полный набор tg_ref."""
    current = list(media or [])
    if not telegram_media_needs_album_expand(external_id, current):
        return current
    album = expand_telegram_album((external_id or "").replace("tg:", "", 1))
    if _media_count(album) <= _media_count(current):
        return current
    meta = [
        m
        for m in current
        if isinstance(m, dict) and (m.get("type") or "") == "tg_meta"
    ]
    return album + meta


def _item_from_tg_message(username: str, msg: Any) -> dict[str, Any] | None:
    if not msg:
        return None
    ref = f"{username}/{int(msg.id)}"
    mime = ""
    file = getattr(msg, "file", None)
    if file is not None:
        mime = (getattr(file, "mime_type", None) or "").lower()
    if getattr(msg, "video", None) or mime.startswith("video/"):
        return {"type": "video", "url": "", "tg_ref": ref, "too_big": True}
    if getattr(msg, "photo", None) or getattr(msg, "document", None) or mime.startswith("image/"):
        return {"type": "image", "url": "", "tg_ref": ref, "too_big": True}
    return None


async def _album_items_async(client: Any, username: str, msg_id: int) -> list[dict[str, Any]]:
    entity = await client.get_entity(username)
    msg = await client.get_messages(entity, ids=int(msg_id))
    if not msg:
        return []
    grouped_id = getattr(msg, "grouped_id", None)
    msgs = [msg]
    if grouped_id:
        lo = max(1, int(msg.id) - 12)
        hi = int(msg.id) + 12
        bunch = await client.get_messages(entity, ids=list(range(lo, hi + 1)))
        msgs = [m for m in bunch if m and getattr(m, "grouped_id", None) == grouped_id]
        msgs.sort(key=lambda m: int(m.id))
    out: list[dict[str, Any]] = []
    for m in msgs:
        item = _item_from_tg_message(username, m)
        if item:
            out.append(item)
    return out


def expand_telegram_album(tg_ref: str) -> list[dict[str, Any]]:
    """Все элементы альбома по одному id (через grouped_id)."""
    parsed = parse_tg_ref(tg_ref)
    if not parsed or not is_tg_ready():
        return []
    username, msg_id = parsed
    try:
        return asyncio.run(_expand_one_async(username, msg_id))
    except Exception as e:
        note_tg_flood(e)
        print(f"[tg] album expand fail {tg_ref}: {e}", flush=True)
        return []


async def _expand_one_async(username: str, msg_id: int) -> list[dict[str, Any]]:
    from telethon import TelegramClient

    api_id, api_hash = _api_credentials()
    session = str(session_path()).removesuffix(".session")
    client = TelegramClient(session, api_id, api_hash, proxy=_proxy_dict())
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return []
        return await _album_items_async(client, username, msg_id)
    finally:
        await client.disconnect()


def enrich_telegram_posts_media(posts: list[Any]) -> None:
    """Добирает обрезанные альбомы через MTProto, не скачивая файлы."""
    if not posts or not is_tg_ready():
        return
    try:
        asyncio.run(_enrich_posts_async(posts))
    except Exception as e:
        print(f"[tg] album enrich fail: {e}", flush=True)


async def _enrich_posts_async(posts: list[Any]) -> None:
    from telethon import TelegramClient

    api_id, api_hash = _api_credentials()
    session = str(session_path()).removesuffix(".session")
    client = TelegramClient(session, api_id, api_hash, proxy=_proxy_dict())
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return
        cache: dict[str, Any] = {}
        for post in posts:
            ext = getattr(post, "external_id", "") or ""
            parsed = parse_tg_ref(ext.replace("tg:", "", 1))
            if not parsed:
                continue
            username, msg_id = parsed
            entity = cache.get(username)
            if entity is False:
                continue
            if entity is None:
                try:
                    entity = await client.get_entity(username)
                except Exception as e:
                    note_tg_flood(e)
                    print(f"[tg] entity fail {username}: {e}", flush=True)
                    cache[username] = False
                    continue
                cache[username] = entity
            album = await _album_items_async(client, username, msg_id)
            current = list(getattr(post, "media", None) or [])
            if _media_count(album) <= _media_count(current):
                continue
            meta = [m for m in current if isinstance(m, dict) and (m.get("type") or "") == "tg_meta"]
            post.media = album + meta
            print(
                f"[tg] album {username}/{msg_id}: html={_media_count(current)} mtproto={_media_count(album)}",
                flush=True,
            )
    finally:
        await client.disconnect()


def _cursor_msg_id(cursor: str) -> int:
    raw = (cursor or "").strip()
    if not raw:
        return 0
    try:
        return int(str(raw).rsplit("/", 1)[-1].split(":")[-1])
    except ValueError:
        return 0


def fetch_telegram_archive_posts(
    url: str,
    after_id: str = "",
    *,
    max_posts: int = 8,
    limit_messages: int = 120,
) -> tuple[str, list[Any], str]:
    """Старые посты канала по возрастанию id (после cursor). Только то, что успели просканировать.

    Возвращает (title, posts, cursor) — cursor = последний просмотренный msg id.
    """
    from parsers.telegram import ParsedPost, telegram_username_from_url

    username = telegram_username_from_url(url)
    if not username:
        raise ValueError("Нужен публичный Telegram-канал")
    if not is_tg_ready():
        raise RuntimeError("Нет сессии Telethon — архив старых постов недоступен")
    after = _cursor_msg_id(after_id)
    title, posts, last_id = asyncio.run(
        _archive_posts_async(
            username,
            after_id=after,
            max_posts=max(1, int(max_posts or 1)),
            limit_messages=max(20, int(limit_messages or 120)),
        )
    )
    cursor = f"tg:{username}/{last_id}" if last_id else (after_id or "")
    return title, posts, cursor


async def _archive_posts_async(
    username: str,
    *,
    after_id: int,
    max_posts: int,
    limit_messages: int,
) -> tuple[str, list[Any], int]:
    from telethon import TelegramClient

    from parsers.telegram import ParsedPost

    api_id, api_hash = _api_credentials()
    session = str(session_path()).removesuffix(".session")
    client = TelegramClient(session, api_id, api_hash, proxy=_proxy_dict())
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telethon не авторизован")
        entity = await client.get_entity(username)
        title = (getattr(entity, "title", None) or username).strip()
        posts: list[Any] = []
        last_id = int(after_id or 0)
        seen_groups: set[int] = set()
        async for msg in client.iter_messages(
            entity,
            min_id=int(after_id or 0),
            reverse=True,
            limit=limit_messages,
        ):
            if not msg:
                continue
            last_id = max(last_id, int(msg.id))
            gid = getattr(msg, "grouped_id", None)
            if gid:
                gid_i = int(gid)
                if gid_i in seen_groups:
                    continue
                seen_groups.add(gid_i)
                album = await _album_items_async(client, username, int(msg.id))
            else:
                item = _item_from_tg_message(username, msg)
                album = [item] if item else []
            videos = [m for m in album if m and (m.get("type") or "") == "video"]
            if not videos:
                continue
            text = (getattr(msg, "message", None) or "") or ""
            posts.append(
                ParsedPost(
                    external_id=f"tg:{username}/{int(msg.id)}",
                    text=text,
                    media=videos,
                    source_url=f"https://t.me/{username}/{int(msg.id)}",
                    title=title,
                )
            )
            if len(posts) >= max_posts:
                break
        return title, posts, last_id
    finally:
        await client.disconnect()
