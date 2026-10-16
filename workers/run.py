from __future__ import annotations

import json
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import html as html_lib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.db import (
    active_sources,
    claim_pending_posts,
    expire_stale_unpublished,
    content_fingerprint,
    db,
    init_db,
    insert_post,
    list_active_max_chat_ids,
    list_tracked_source_posts,
    mark_post,
    register_send,
    save_translation,
    set_source_status,
    upsert_channel,
    upsert_tg_channel,
    upsert_vk_channel,
    get_meta,
    set_meta,
)
from app.filter import (
    drop_document_media,
    has_document_media,
    has_downloadable_media,
    has_publishable_media,
    has_video_media,
    is_advertisement,
    keep_document_media,
    keep_video_media,
    sanitize_for_publish,
    scrub_promo_media,
)
from app.rules import resolve_rules
from app.drip import count_source_queue, drip_queue_cap, next_source_publish_at
from app.max_api import MaxClient, MaxApiError, extract_max_message_id
from app.source_sync import (
    deleted_in_visible_window,
    newer_than_cursor,
    publish_hold_sec_for_media,
)
from app.tg_bot_api import TgBotApiError, TgBotClient
from app.vk_api import VkApiError, VkClient
from app.media_store import (
    is_oversized_video,
    local_media_path,
    materialize_media_list,
    persist_media_to_siblings,
)
from app.tg_mtproto import (
    complete_telegram_media,
    fetch_telegram_archive_posts,
    media_needs_tg_download,
    tg_is_flood_blocked,
)
from app.translate import translate_to_russian
from parsers.rss import parse_rss
from parsers.telegram import detect_kind, normalize_telegram_url, parse_telegram, telegram_username_from_url
from parsers.vk import parse_vk
from parsers.x import normalize_x_url, parse_x


def refresh_max_channels_metadata(
    client: MaxClient,
    *,
    chat_ids: list[int] | None = None,
    sleep_sec: float = 0.12,
) -> dict[str, int]:
    """Обновить title/icon/participants_count через GET /chats/{id}."""
    if chat_ids is None:
        with db() as conn:
            chat_ids = list_active_max_chat_ids(conn)
    ok = 0
    fail = 0
    gone = 0
    for chat_id in chat_ids:
        try:
            chat = client.get_chat(int(chat_id))
            # если бота выкинули — API часто 403; ловим ниже
            status = str(chat.get("status") or "active")
            if status not in {"active", ""}:
                with db() as conn:
                    conn.execute(
                        "UPDATE channels SET status=?, updated_at=? WHERE chat_id=? "
                        "AND COALESCE(platform,'max')='max'",
                        (status, time.time(), int(chat_id)),
                    )
                gone += 1
            else:
                with db() as conn:
                    upsert_channel(conn, chat)
                ok += 1
        except MaxApiError as e:
            fail += 1
            # нет доступа / не найден — помечаем removed, чтобы не висел мёртвый
            if e.status in {403, 404}:
                with db() as conn:
                    conn.execute(
                        "UPDATE channels SET status='removed', updated_at=? WHERE chat_id=? "
                        "AND COALESCE(platform,'max')='max'",
                        (time.time(), int(chat_id)),
                    )
                gone += 1
        except Exception:
            fail += 1
            traceback.print_exc()
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    return {"ok": ok, "fail": fail, "gone": gone, "total": len(chat_ids)}


def sync_chats(client: MaxClient, *, refresh_meta: bool = False) -> int:
    """Подтянуть bot_added/removed и опционально обновить метаданные всех MAX-каналов."""
    chats = client.list_chats()
    with db() as conn:
        for chat in chats:
            if chat.get("type") not in {"channel", "chat"}:
                continue
            if chat.get("status") != "active":
                continue
            upsert_channel(conn, chat)
        marker_raw = get_meta(conn, "updates_marker", "")
        marker = int(marker_raw) if marker_raw.isdigit() else None
    try:
        data = client.get_updates(
            marker=marker,
            limit=100,
            timeout=1,
            types=["bot_added", "bot_removed", "bot_stopped"],
        )
    except MaxApiError:
        data = {"updates": [], "marker": marker}

    updates = data.get("updates") or []
    new_marker = data.get("marker")
    added_ids: list[int] = []
    with db() as conn:
        for u in updates:
            ut = u.get("update_type")
            chat_id = u.get("chat_id")
            if ut == "bot_added" and chat_id is not None:
                cid = int(chat_id)
                try:
                    chat = client.get_chat(cid)
                    upsert_channel(conn, chat)
                    added_ids.append(cid)
                except MaxApiError:
                    upsert_channel(
                        conn,
                        {
                            "chat_id": cid,
                            "title": f"chat {cid}",
                            "type": "channel" if u.get("is_channel") else "chat",
                            "status": "active",
                            "participants_count": 0,
                        },
                    )
                    added_ids.append(cid)
            elif ut in {"bot_removed", "bot_stopped"} and chat_id is not None:
                conn.execute(
                    "UPDATE channels SET status=?, updated_at=? WHERE chat_id=?",
                    ("removed", time.time(), int(chat_id)),
                )
        if new_marker is not None:
            set_meta(conn, "updates_marker", str(new_marker))

    # Новые каналы — сразу дотягиваем метаданные ещё раз (иногда icon приходит с задержкой)
    if added_ids and not refresh_meta:
        stats = refresh_max_channels_metadata(client, chat_ids=added_ids, sleep_sec=0.05)
        print(f"[worker] meta refresh new={stats}", flush=True)

    if refresh_meta:
        stats = refresh_max_channels_metadata(client)
        print(f"[worker] meta refresh all={stats}", flush=True)
        return int(stats.get("ok") or 0)

    return len(chats) + len(added_ids)


def telegram_bot_tokens() -> list[str]:
    return get_settings().telegram_bot_tokens()


def _tg_upsert_from_chat(conn, token: str, chat: dict) -> bool:
    chat_type = (chat.get("type") or "").strip()
    if chat_type not in {"channel", "supergroup", "group"}:
        return False
    chat_id = int(chat["id"])
    username = (chat.get("username") or "").strip()
    title = (chat.get("title") or username or f"tg {chat_id}").strip()
    desc = chat.get("description") or chat.get("bio") or ""
    upsert_tg_channel(
        conn,
        chat_id,
        title,
        token,
        chat_type=chat_type,
        description=str(desc)[:500],
        participants_count=int(chat.get("members_count") or 0),
        username=username,
    )
    return True


def sync_tg_chats() -> int:
    """Подтянуть TG-каналы, куда добавлены боты из TG_BOT_TOKENS."""
    tokens = telegram_bot_tokens()
    if not tokens:
        return 0
    added = 0
    for token in tokens:
        with TgBotClient(token) as bot:
            marker_key = f"tg_updates_offset:{token.split(':', 1)[0]}"
            with db() as conn:
                raw = get_meta(conn, marker_key, "")
            offset = int(raw) if str(raw).isdigit() else None
            try:
                updates = bot.get_updates(
                    offset=offset,
                    timeout=1,
                    allowed_updates=["my_chat_member", "channel_post", "message"],
                )
            except TgBotApiError as e:
                print(f"[worker] tg sync fail: {e}", flush=True)
                continue

            seen: dict[int, dict] = {}
            max_id = offset
            removed: list[int] = []
            for u in updates:
                uid = int(u.get("update_id") or 0)
                if max_id is None or uid >= max_id:
                    max_id = uid + 1
                mcm = u.get("my_chat_member")
                if mcm and isinstance(mcm.get("chat"), dict):
                    new_st = ((mcm.get("new_chat_member") or {}).get("status") or "")
                    chat = mcm["chat"]
                    if new_st in {"kicked", "left"}:
                        removed.append(int(chat["id"]))
                    else:
                        seen[int(chat["id"])] = chat
                for key in ("channel_post", "message"):
                    payload = u.get(key) or {}
                    chat = payload.get("chat") if isinstance(payload, dict) else None
                    if isinstance(chat, dict) and chat.get("id") is not None:
                        seen[int(chat["id"])] = chat

            with db() as conn:
                for chat in seen.values():
                    try:
                        extra = bot.get_chat(chat["id"])
                        if isinstance(extra, dict):
                            chat = {**chat, **extra}
                    except TgBotApiError:
                        pass
                    if _tg_upsert_from_chat(conn, token, chat):
                        added += 1
                for cid in removed:
                    conn.execute(
                        """
                        UPDATE channels
                        SET status='removed', updated_at=?
                        WHERE chat_id=? AND COALESCE(platform, 'max')='telegram'
                        """,
                        (time.time(), cid),
                    )
                if max_id is not None:
                    set_meta(conn, marker_key, str(max_id))
    return added


def add_vk_group_by_token(token: str, user_token: str = "") -> dict:
    """Добавить сообщество VK по ключу из Управление → Работа с API."""
    raw = (token or "").strip()
    if not raw:
        raise ValueError("Вставьте ключ API сообщества")
    info = VkClient(raw).identify_community()
    user_token = (user_token or "").strip()
    if user_token:
        VkClient(user_token).can_upload_wall_photo(abs(int(info["owner_id"])))
    with db() as conn:
        existing = conn.execute(
            "SELECT platform FROM channels WHERE chat_id=?",
            (int(info["owner_id"]),),
        ).fetchone()
        if existing and (existing["platform"] or "max") not in {"vk", ""}:
            raise ValueError(
                f"chat_id {info['owner_id']} уже занят платформой {existing['platform']}"
            )
        upsert_vk_channel(
            conn,
            owner_id=int(info["owner_id"]),
            title=info["title"],
            token=raw,
            screen_name=info.get("screen_name") or "",
            description=info.get("description") or "",
            participants_count=int(info.get("members_count") or 0),
            icon_url=info.get("photo") or "",
            upload_token=user_token,
        )
    return info


def add_tg_channel_by_ref(ref: str, token: str | None = None) -> dict:
    """Добавить TG-канал по @username / t.me / chat_id. Бот уже должен быть админом."""
    tokens = telegram_bot_tokens()
    if token:
        tokens = [token] + [t for t in tokens if t != token]
    if not tokens:
        raise ValueError("Нет TG_BOT_TOKENS в .env")

    raw = (ref or "").strip()
    if not raw:
        raise ValueError("Укажите @username, ссылку t.me/… или chat_id")
    m = re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)", raw, re.I)
    if m:
        chat_ref: int | str = f"@{m.group(1)}"
    elif raw.startswith("@"):
        chat_ref = raw
    else:
        try:
            chat_ref = int(raw)
        except ValueError:
            chat_ref = f"@{raw.lstrip('@')}"

    last_err = ""
    for tok in tokens:
        with TgBotClient(tok) as bot:
            try:
                chat = bot.get_chat(chat_ref)
            except TgBotApiError as e:
                last_err = str(e)
                continue
            with db() as conn:
                if not _tg_upsert_from_chat(conn, tok, chat):
                    raise ValueError("Это не канал и не группа")
            return {"chat_id": int(chat["id"]), "title": chat.get("title") or ""}
    raise ValueError(last_err or "Бот не видит этот чат. Добавьте его админом в канал.")


_TG_MEMBER_OK = {"creator", "administrator", "member", "restricted"}
_TG_CONN_CACHE_KEY = "tg_bot_connections_json"
_TG_STATUS_RU = {
    "creator": "владелец",
    "administrator": "админ",
    "member": "участник",
    "restricted": "с ограничениями",
}


def _tg_conn_candidates() -> list[dict]:
    """Уникальные чаты: назначения Telegram + username из источников."""
    by_key: dict[str, dict] = {}
    with db() as conn:
        dests = list(
            conn.execute(
                """
                SELECT chat_id, title, type, link, participants_count, status
                FROM channels
                WHERE COALESCE(platform, 'max') = 'telegram'
                  AND status = 'active'
                  AND COALESCE(hidden, 0) = 0
                """
            )
        )
        sources = list(
            conn.execute(
                "SELECT DISTINCT url, title FROM sources WHERE kind='telegram'"
            )
        )

    for row in dests:
        chat_id = int(row["chat_id"])
        username = telegram_username_from_url(row["link"] or "") or ""
        key = f"id:{chat_id}"
        by_key[key] = {
            "ref": chat_id,
            "username": username,
            "dest_chat_id": chat_id,
            "dest_title": row["title"] or "",
            "dest_type": row["type"] or "channel",
            "dest_link": row["link"] or "",
            "dest_members": int(row["participants_count"] or 0),
            "dest_status": row["status"] or "",
            "source_urls": [],
        }
        if username:
            by_key[f"user:{username.lower()}"] = by_key[key]

    for row in sources:
        username = telegram_username_from_url(row["url"] or "")
        if not username:
            continue
        ukey = f"user:{username.lower()}"
        item = by_key.get(ukey)
        if item is None:
            item = {
                "ref": f"@{username}",
                "username": username,
                "dest_chat_id": None,
                "dest_title": "",
                "dest_type": "channel",
                "dest_link": f"https://t.me/{username}",
                "dest_members": 0,
                "dest_status": "",
                "source_urls": [],
            }
            by_key[ukey] = item
        title = (row["title"] or "").strip()
        url = row["url"] or ""
        if url and url not in item["source_urls"]:
            item["source_urls"].append(url)
        if title and not item.get("source_title"):
            item["source_title"] = title

    # один кандидат на чат: предпочитаем chat_id, иначе @username
    seen: set[int] = set()
    out: list[dict] = []
    for item in by_key.values():
        marker = id(item)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def _probe_tg_membership(token: str, bot_id: int, bot_username: str, cand: dict) -> dict | None:
    ref = cand["ref"]
    with TgBotClient(token) as bot:
        try:
            member = bot.get_chat_member(ref, bot_id)
        except TgBotApiError:
            return None
        status = (member.get("status") or "").strip()
        if status not in _TG_MEMBER_OK:
            return None
        try:
            chat = bot.get_chat(ref)
        except TgBotApiError:
            chat = {}
    chat_id = int(chat.get("id") or cand.get("dest_chat_id") or 0)
    if not chat_id:
        return None
    username = (chat.get("username") or cand.get("username") or "").strip()
    title = (chat.get("title") or cand.get("dest_title") or cand.get("source_title") or username or str(chat_id)).strip()
    chat_type = (chat.get("type") or cand.get("dest_type") or "channel").strip()
    link = f"https://t.me/{username}" if username else (cand.get("dest_link") or "")
    return {
        "chat_id": chat_id,
        "title": title,
        "type": chat_type,
        "username": username,
        "link": link,
        "status": status,
        "status_ru": _TG_STATUS_RU.get(status, status),
        "members": int(chat.get("members_count") or cand.get("dest_members") or 0),
        "bot_username": bot_username,
        "is_destination": cand.get("dest_chat_id") is not None,
        "is_source": bool(cand.get("source_urls")),
        "source_count": len(cand.get("source_urls") or []),
    }


def refresh_tg_bot_connections() -> dict:
    """Опросить Bot API: какие чаты видят боты из TG_BOT_TOKENS."""
    payload: dict = {"ts": time.time(), "bots": [], "chats": [], "error": ""}
    tokens = telegram_bot_tokens()
    if not tokens:
        payload["error"] = "TG_BOT_TOKENS пуст"
        _store_tg_bot_connections(payload)
        return payload

    candidates = _tg_conn_candidates()
    chats_by_id: dict[int, dict] = {}

    for token in tokens:
        with TgBotClient(token) as bot:
            try:
                me = bot.get_me()
            except TgBotApiError as e:
                payload["error"] = str(e)[:300]
                continue
        bot_id = int(me.get("id") or 0)
        bot_username = (me.get("username") or "").strip()
        payload["bots"].append(
            {
                "id": bot_id,
                "username": bot_username,
                "name": (me.get("first_name") or "").strip(),
            }
        )
        if not candidates:
            continue
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [
                pool.submit(_probe_tg_membership, token, bot_id, bot_username, cand)
                for cand in candidates
            ]
            for fut in as_completed(futs):
                try:
                    info = fut.result()
                except Exception:
                    info = None
                if not info:
                    continue
                prev = chats_by_id.get(info["chat_id"])
                if prev is None:
                    chats_by_id[info["chat_id"]] = info
                else:
                    prev["is_destination"] = prev["is_destination"] or info["is_destination"]
                    prev["is_source"] = prev["is_source"] or info["is_source"]
                    prev["source_count"] = max(prev["source_count"], info["source_count"])

    # назначения из БД, даже если getChatMember не ответил
    for cand in candidates:
        dest_id = cand.get("dest_chat_id")
        if not dest_id:
            continue
        dest_id = int(dest_id)
        if dest_id in chats_by_id:
            chats_by_id[dest_id]["is_destination"] = True
            continue
        username = cand.get("username") or ""
        chats_by_id[dest_id] = {
            "chat_id": int(dest_id),
            "title": cand.get("dest_title") or username or str(dest_id),
            "type": cand.get("dest_type") or "channel",
            "username": username,
            "link": cand.get("dest_link") or "",
            "status": "administrator" if (cand.get("dest_status") or "") == "active" else (cand.get("dest_status") or ""),
            "status_ru": "в базе",
            "members": int(cand.get("dest_members") or 0),
            "bot_username": (payload["bots"][0]["username"] if payload["bots"] else ""),
            "is_destination": True,
            "is_source": bool(cand.get("source_urls")),
            "source_count": len(cand.get("source_urls") or []),
        }

    payload["chats"] = sorted(
        chats_by_id.values(),
        key=lambda c: ((c.get("title") or "").casefold(), c.get("chat_id") or 0),
    )
    _store_tg_bot_connections(payload)
    return payload


def _store_tg_bot_connections(payload: dict) -> None:
    with db() as conn:
        set_meta(conn, _TG_CONN_CACHE_KEY, json.dumps(payload, ensure_ascii=False))


def get_tg_bot_connections(*, refresh: bool = False) -> dict:
    """Список чатов бота. refresh=True — заново опросить Telegram (кнопка «Обновить список»)."""
    if not refresh:
        with db() as conn:
            raw = get_meta(conn, _TG_CONN_CACHE_KEY, "")
        if raw:
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
    return refresh_tg_bot_connections()


def fetch_source_posts(
    kind: str,
    url: str,
    cursor: str,
    vk_token: str,
    *,
    enrich_media: bool = True,
    full_page: bool = False,
):
    since = "" if full_page else cursor
    if kind == "telegram":
        # Все TG-источники: полный альбом из t.me/s + добор через Telethon.
        title, posts = parse_telegram(url, since_id=since)
        if enrich_media:
            from app.tg_mtproto import enrich_telegram_posts_media

            enrich_telegram_posts_media(posts)
        return title, posts
    if kind == "vk":
        return parse_vk(url, since_id=since, token=vk_token)
    if kind == "rss":
        return parse_rss(url, since_id=cursor)
    if kind == "x":
        return parse_x(url, since_id=cursor, enrich_media=enrich_media)
    raise ValueError(f"Неизвестный тип источника: {kind}")


def reconcile_source_deletions(
    client: MaxClient | None,
    source_id: int,
    visible_ids: list[str],
    new_external_ids: list[str],
    window_sec: int,
) -> int:
    """Если в источнике вышел новый пост и старый стёрт — убрать зеркало в MAX."""
    if not visible_ids or not new_external_ids:
        return 0
    with db() as conn:
        tracked = [dict(r) for r in list_tracked_source_posts(conn, source_id, window_sec)]
    missing = set(
        deleted_in_visible_window(visible_ids, [r["external_id"] for r in tracked])
    )
    if not missing:
        return 0
    n = 0
    for row in tracked:
        if row["external_id"] not in missing:
            continue
        status = row.get("publish_status") or ""
        if status in {"pending", "queued"}:
            with db() as conn:
                mark_post(conn, int(row["id"]), "skipped", "удалён в источнике")
            print(
                f"[worker] source deleted skip pending id={row['id']} {row['external_id']}",
                flush=True,
            )
            n += 1
            continue
        if status != "sent":
            continue
        mid = (row.get("dest_message_id") or "").strip()
        platform = (row.get("platform") or "max").strip().lower()
        if platform != "max" or not mid or client is None:
            print(
                f"[worker] source deleted no max mid id={row['id']} {row['external_id']}",
                flush=True,
            )
            continue
        token = (row.get("bot_token") or "").strip()
        try:
            if token:
                with MaxClient(token=token) as api:
                    api.delete_message(mid)
            else:
                client.delete_message(mid)
            with db() as conn:
                mark_post(conn, int(row["id"]), "skipped", "удалён в источнике")
            print(
                f"[worker] source deleted max mid={mid} id={row['id']} {row['external_id']}",
                flush=True,
            )
            n += 1
        except Exception as e:
            print(
                f"[worker] source delete fail id={row['id']} {row['external_id']}: {e}",
                flush=True,
            )
    return n


def poll_sources(client: MaxClient | None = None) -> None:
    settings = get_settings()
    with db() as conn:
        sources = [dict(r) for r in active_sources(conn)]

    for src in sources:
        sid = src["id"]
        try:
            rules = resolve_rules(
                chat_id=int(src["chat_id"]),
                title=src.get("channel_title") or "",
                link=src.get("channel_link") or "",
                source_url=src.get("url") or "",
            )
            with db() as conn:
                set_source_status(conn, sid, "парсинг", "Читаю ленту…")
                queue_n = count_source_queue(conn, sid)

            cursor = src.get("cursor") or ""
            archive_cursor = cursor
            title = src.get("title") or ""
            posts: list = []
            visible_ids: list[str] = []

            if rules.archive_drip and src["kind"] == "telegram":
                cap = drip_queue_cap(rules.max_per_day)
                slots = max(0, cap - queue_n)
                if slots <= 0:
                    with db() as conn:
                        set_source_status(
                            conn,
                            sid,
                            "добавлен",
                            f"Архив: в очереди {queue_n}, ждём слоты",
                            title=title,
                            cursor=cursor,
                        )
                    continue
                title, posts, archive_cursor = fetch_telegram_archive_posts(
                    src["url"],
                    after_id=cursor,
                    max_posts=slots,
                    limit_messages=120,
                )
            else:
                use_page = src["kind"] in {"telegram", "vk"}
                title, page_posts = fetch_source_posts(
                    src["kind"],
                    src["url"],
                    cursor,
                    settings.vk_access_token,
                    enrich_media=not use_page,
                    full_page=use_page,
                )
                if use_page:
                    visible_ids = [p.external_id for p in page_posts]
                    posts = [
                        p
                        for p in page_posts
                        if newer_than_cursor(p.external_id, cursor)
                    ]
                    if src["kind"] == "telegram" and posts:
                        from app.tg_mtproto import enrich_telegram_posts_media

                        enrich_telegram_posts_media(posts)
                else:
                    posts = page_posts

            # Первый запуск обычного источника: не репостим историю
            first_run = (not cursor) and not rules.archive_drip
            with db() as conn:
                if first_run:
                    if posts:
                        set_source_status(
                            conn,
                            sid,
                            "добавлен",
                            f"Источник готов, ждём новые посты ({len(posts)} уже в ленте пропущено)",
                            title=title,
                            cursor=posts[-1].external_id,
                        )
                    else:
                        set_source_status(
                            conn,
                            sid,
                            "добавлен",
                            "Источник готов, лента пуста — ждём новые посты",
                            title=title,
                        )
                    continue

                if visible_ids:
                    reconcile_source_deletions(
                        client,
                        sid,
                        visible_ids,
                        [p.external_id for p in posts],
                        int(settings.source_delete_track_sec or 14400),
                    )

                new_count = 0
                last_id = archive_cursor if rules.archive_drip else cursor
                mode = rules.moderation_mode
                require_media = int(
                    src.get("channel_require_media")
                    if src.get("channel_require_media") is not None
                    else (src.get("require_media") if src.get("require_media") is not None else 1)
                )
                require_text = int(
                    src.get("channel_require_text")
                    if src.get("channel_require_text") is not None
                    else (src.get("require_text") if src.get("require_text") is not None else 1)
                )
                for p in posts:
                    media = p.media
                    if rules.documents_only:
                        media = keep_document_media(media)
                    else:
                        media = drop_document_media(media)
                    if rules.require_video:
                        media = keep_video_media(media)
                    text = "" if rules.drop_caption else p.text
                    if rules.drop_caption:
                        is_ad = False
                        ad_reason = ""
                    else:
                        fr = is_advertisement(
                            p.text,
                            media,
                            mode=mode,
                            block_profanity=rules.block_profanity,
                            block_text_contains=rules.block_text_contains,
                        )
                        is_ad = fr.is_ad
                        ad_reason = fr.reason
                    if (
                        not is_ad
                        and rules.documents_only
                        and not has_document_media(media)
                    ):
                        is_ad = True
                        ad_reason = "не файл (нужны исходники)"
                    if not is_ad and rules.require_video and not has_video_media(media):
                        is_ad = True
                        ad_reason = "нет видео"
                    elif require_media and not is_ad and not has_downloadable_media(media):
                        is_ad = True
                        ad_reason = "нет медиа"
                    if (
                        require_text
                        and not is_ad
                        and not rules.drop_caption
                        and not rules.fixed_caption
                        and not (text or "").strip()
                    ):
                        is_ad = True
                        ad_reason = "нет текста"
                    scheduled_at = None
                    hold_sec = publish_hold_sec_for_media(media, settings.publish_hold_sec)
                    if not is_ad and (
                        rules.publish_times
                        or rules.max_per_day
                        or rules.min_gap_sec
                        or rules.max_gap_sec
                    ):
                        scheduled_at = next_source_publish_at(
                            conn,
                            sid,
                            min_gap_sec=rules.min_gap_sec,
                            max_gap_sec=rules.max_gap_sec,
                            max_per_day=rules.max_per_day,
                            tz_name=rules.tz_name,
                            publish_times=rules.publish_times,
                            chat_id=int(src["chat_id"]),
                        )
                        if not rules.publish_times:
                            scheduled_at = max(
                                scheduled_at,
                                time.time() + hold_sec,
                            )
                    inserted = insert_post(
                        conn,
                        sid,
                        p.external_id,
                        text,
                        media,
                        p.source_url,
                        is_ad,
                        ad_reason,
                        publish_hold_sec=hold_sec,
                        republish_window_sec=settings.republish_window_sec,
                        publish_at=scheduled_at,
                    )
                    if inserted:
                        new_count += 1
                    if not rules.archive_drip:
                        last_id = p.external_id

                if rules.archive_drip:
                    last_id = archive_cursor or last_id
                    detail = f"Архив. В очередь: {new_count}"
                else:
                    detail = f"Ок. Новых: {new_count}"
                set_source_status(
                    conn,
                    sid,
                    "добавлен",
                    detail,
                    title=title or src.get("title") or "",
                    cursor=last_id if (posts or rules.archive_drip) else src.get("cursor") or "",
                )
            if client is not None and new_count:
                try:
                    publish_pending(client)
                except Exception:
                    traceback.print_exc()
        except Exception as e:
            with db() as conn:
                set_source_status(conn, sid, "ошибка", str(e)[:500])


def _load_bytes_from_url(url: str, watermark: str = "") -> bytes | None:
    from urllib.parse import unquote, urlparse

    from app.watermark import apply_text_watermark

    raw = (url or "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("file://"):
            content = Path(unquote(urlparse(raw).path)).read_bytes()
        elif raw.startswith("/") and Path(raw).is_file():
            content = Path(raw).read_bytes()
        else:
            from app.http_util import http_client

            with http_client(timeout=90.0) as c:
                r = c.get(raw)
                r.raise_for_status()
                content = r.content
    except Exception as e:
        print(f"[worker] media download fail: {e}", flush=True)
        return None
    if (watermark or "").strip():
        try:
            content = apply_text_watermark(content, watermark)
        except Exception as e:
            print(f"[worker] watermark fail: {e}", flush=True)
    return content


def _publish_telegram(
    *,
    chat_id: int,
    media: list,
    body_text: str,
    watermark: str,
    footer_link: str,
    footer_link_text: str,
    footer_as_button: bool,
    also_send_files: bool,
    bot_token: str,
) -> None:
    token = (bot_token or "").strip()
    if not token:
        tokens = telegram_bot_tokens()
        token = tokens[0] if tokens else ""
    if not token:
        raise TgBotApiError("Нет токена TG-бота для канала")

    items: list[dict] = []
    file_items: list[tuple[bytes, str]] = []
    for m in media[:10]:
        if not isinstance(m, dict):
            continue
        mtype = (m.get("type") or "").lower()
        murl = m.get("url") or ""
        if mtype not in {"image", "video"} or not murl:
            continue
        wm = watermark if mtype == "image" else ""
        content = _load_bytes_from_url(murl, wm)
        if not content:
            raise TgBotApiError("не удалось загрузить медиа")
        fname = (m.get("filename") or "").strip() or (
            "photo.jpg" if mtype == "image" else "video.mp4"
        )
        items.append(
            {
                "type": "photo" if mtype == "image" else "video",
                "bytes": content,
                "filename": fname,
            }
        )
        if also_send_files and m.get("from_document"):
            raw = _load_bytes_from_url(m.get("local_path") or murl, "")
            if raw:
                file_items.append((raw, fname))

    caption = (body_text or "").strip()
    parse_mode = None
    reply_markup = None
    if footer_link:
        label = (footer_link_text or footer_link).strip() or footer_link
        if footer_as_button and len(items) <= 1:
            reply_markup = {
                "inline_keyboard": [[{"text": label[:64], "url": footer_link}]]
            }
        else:
            href = html_lib.escape(footer_link, quote=True)
            link_html = f'<a href="{href}">{html_lib.escape(label)}</a>'
            caption = f"{html_lib.escape(caption)}\n\n{link_html}" if caption else link_html
            parse_mode = "HTML"

    if not items and not caption:
        raise TgBotApiError("пустой пост")

    with TgBotClient(token) as tg:
        if not items:
            tg.send_message(
                chat_id,
                caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        elif len(items) == 1:
            it = items[0]
            if it["type"] == "photo":
                tg.send_photo(
                    chat_id,
                    it["bytes"],
                    caption=caption or None,
                    parse_mode=parse_mode,
                    filename=it["filename"],
                    reply_markup=reply_markup,
                )
            else:
                tg.send_video(
                    chat_id,
                    it["bytes"],
                    caption=caption or None,
                    parse_mode=parse_mode,
                    filename=it["filename"],
                    reply_markup=reply_markup,
                )
        else:
            tg.send_media_group(
                chat_id,
                items,
                caption=caption or None,
                parse_mode=parse_mode,
            )
        for raw, fname in file_items:
            time.sleep(0.6)
            tg.send_document(chat_id, raw, filename=fname)


def _publish_vk(
    *,
    owner_id: int,
    media: list,
    body_text: str,
    watermark: str,
    footer_link: str,
    footer_link_text: str,
    token: str,
    upload_token: str = "",
) -> None:
    vk = VkClient(token)
    media_token = (upload_token or "").strip() or (get_settings().vk_access_token or "").strip()
    uploader = VkClient(media_token) if media_token and media_token != token else vk
    group_id = abs(int(owner_id))
    attachments: list[str] = []
    last_media_err = ""
    for m in media[:10]:
        if not isinstance(m, dict):
            continue
        mtype = (m.get("type") or "").lower()
        murl = m.get("url") or ""
        if mtype not in {"image", "video"} or not murl:
            continue
        wm = watermark if mtype == "image" else ""
        content = _load_bytes_from_url(murl, wm)
        if not content:
            last_media_err = "не удалось скачать медиа"
            continue
        fname = (m.get("filename") or "").strip() or (
            "photo.jpg" if mtype == "image" else "video.mp4"
        )
        try:
            if mtype == "image":
                attachments.append(uploader.upload_wall_photo(group_id, content, fname))
            else:
                attachments.append(uploader.upload_wall_video(group_id, content, fname))
        except VkApiError as e:
            last_media_err = str(e)
            print(f"[worker] vk media fail: {e}", flush=True)
            continue

    caption = (body_text or "").strip()
    if footer_link:
        label = (footer_link_text or footer_link).strip() or footer_link
        caption = f"{caption}\n\n{label} {footer_link}".strip() if caption else f"{label} {footer_link}"

    had_media = any(
        isinstance(m, dict)
        and (m.get("type") or "").lower() in {"image", "video"}
        and m.get("url")
        for m in media[:10]
    )
    if had_media and not attachments:
        raise VkApiError(
            last_media_err
            or "не удалось загрузить медиа в VK (ключу сообщества часто нужны фото/видео отдельно)"
        )
    if not caption and not attachments:
        raise VkApiError(last_media_err or "пустой пост")
    vk.wall_post(int(owner_id), message=caption, attachments=attachments or None)


def publish_pending(client: MaxClient) -> int:
    import json

    sent = 0
    with db() as conn:
        rows = claim_pending_posts(conn, limit=10)

    for row in rows:
        text = (row.get("text") or "").strip()
        media_json = row.get("media_json") or "[]"
        media = json.loads(media_json)
        ext_id = row.get("external_id") or ""
        if (ext_id or "").startswith("tg:"):
            filled = complete_telegram_media(ext_id, media)
            if filled is not media and len(filled) != len(media):
                media = filled
                media_json = json.dumps(media, ensure_ascii=False)
                with db() as conn:
                    conn.execute(
                        "UPDATE posts SET media_json=? WHERE id=?",
                        (media_json, row["id"]),
                    )
                print(
                    f"[worker] album complete post={row['id']} media={sum(1 for m in media if (m.get('type') or '') in {'image','video'})}",
                    flush=True,
                )
        # перевод перед публикацией, если источник с флагом translate
        if int(row.get("need_translate") or 0) and text and not int(row.get("translated") or 0):
            try:
                original = text
                text = translate_to_russian(text)
                with db() as conn:
                    save_translation(conn, row["id"], original, text)
            except Exception as e:
                with db() as conn:
                    mark_post(conn, row["id"], "pending", f"перевод: {e}"[:800])
                time.sleep(2.0)
                continue

        # Единые правила перед публикацией (ядро + оверрайд канала)
        rules = resolve_rules(
            chat_id=int(row["chat_id"]),
            title=row.get("channel_title") or "",
            link=row.get("channel_link") or "",
            source_url=row.get("source_feed_url") or row.get("source_url") or "",
        )
        mode = rules.moderation_mode
        require_media = int(
            row.get("require_media") if row.get("require_media") is not None else 1
        )
        require_text = int(
            row.get("require_text") if row.get("require_text") is not None else 1
        )
        # До санитайза: он вырезает #реклама, erid и «О рекламодателе».
        if not rules.drop_caption:
            fr0 = is_advertisement(
                text,
                media,
                mode=mode,
                block_profanity=rules.block_profanity,
                block_text_contains=rules.block_text_contains,
            )
            if fr0.is_ad:
                with db() as conn:
                    mark_post(conn, row["id"], "skipped", f"реклама: {fr0.reason}")
                    conn.execute(
                        "UPDATE posts SET is_ad=1, ad_reason=? WHERE id=?",
                        (fr0.reason, row["id"]),
                    )
                print(f"[worker] skip ad post={row['id']} reason={fr0.reason}", flush=True)
                continue
        if rules.documents_only:
            media = keep_document_media(media)
            media_json = json.dumps(media, ensure_ascii=False)
        else:
            # Не публикуем TG-документы вне канала обоев
            media = drop_document_media(media)
            media_json = json.dumps(media, ensure_ascii=False)
        text = sanitize_for_publish(
            text,
            mode=mode,
            extra_strip_contains=rules.strip_line_contains,
            exact_strip_lines=rules.strip_exact_lines,
            strip_bare_domains=rules.strip_bare_domains,
        )
        if rules.drop_caption:
            text = ""
        elif rules.fixed_caption:
            text = rules.fixed_caption
        if rules.require_video:
            media = keep_video_media(media)
            media_json = json.dumps(media, ensure_ascii=False)
        if mode != "strict":
            media = scrub_promo_media(media)
            media_json = json.dumps(media, ensure_ascii=False)
        if text != (row.get("text") or "").strip() or mode != "strict" or rules.documents_only:
            with db() as conn:
                conn.execute(
                    "UPDATE posts SET text=?, media_json=? WHERE id=?",
                    (text, media_json, row["id"]),
                )

        if rules.documents_only and not has_document_media(media):
            with db() as conn:
                mark_post(conn, row["id"], "skipped", "не файл (нужны исходники)")
                conn.execute(
                    "UPDATE posts SET is_ad=1, ad_reason=? WHERE id=?",
                    ("не файл (нужны исходники)", row["id"]),
                )
            print(f"[worker] skip non-document post={row['id']}", flush=True)
            continue

        if rules.require_video and not has_video_media(media):
            with db() as conn:
                mark_post(conn, row["id"], "skipped", "нет видео")
                conn.execute(
                    "UPDATE posts SET is_ad=1, ad_reason=? WHERE id=?",
                    ("нет видео", row["id"]),
                )
            print(f"[worker] skip no-video post={row['id']}", flush=True)
            continue

        if require_media and not has_downloadable_media(media):
            with db() as conn:
                mark_post(conn, row["id"], "skipped", "нет медиа")
                conn.execute(
                    "UPDATE posts SET is_ad=1, ad_reason=? WHERE id=?",
                    ("нет медиа", row["id"]),
                )
            print(f"[worker] skip no-media post={row['id']}", flush=True)
            continue
        if not require_media and not has_downloadable_media(media):
            media = [
                m
                for m in media
                if not isinstance(m, dict)
                or (m.get("type") or "").lower() not in {"image", "video"}
            ]
            media_json = json.dumps(media, ensure_ascii=False)

        if (
            require_text
            and not rules.drop_caption
            and not (text or "").strip()
        ):
            with db() as conn:
                mark_post(conn, row["id"], "skipped", "нет текста")
                conn.execute(
                    "UPDATE posts SET is_ad=1, ad_reason=? WHERE id=?",
                    ("нет текста", row["id"]),
                )
            print(f"[worker] skip no-text post={row['id']}", flush=True)
            continue

        # повторная проверка рекламы после перевода/санитайза
        if rules.drop_caption:
            fr_is_ad = False
            fr_reason = ""
        else:
            fr = is_advertisement(
                text,
                media,
                mode=mode,
                block_profanity=rules.block_profanity,
                block_text_contains=rules.block_text_contains,
            )
            fr_is_ad = fr.is_ad
            fr_reason = fr.reason
        if fr_is_ad:
            with db() as conn:
                mark_post(conn, row["id"], "skipped", f"реклама: {fr_reason}")
                conn.execute(
                    "UPDATE posts SET is_ad=1, ad_reason=? WHERE id=?",
                    (fr_reason, row["id"]),
                )
            print(f"[worker] skip ad post={row['id']} reason={fr_reason}", flush=True)
            continue

        chat_id = int(row["chat_id"])
        external_id = row.get("external_id") or f"post:{row['id']}"
        fp = content_fingerprint(chat_id, text, media_json)

        body_text = (text or "").strip()

        media = materialize_media_list(media)
        media_json = json.dumps(media, ensure_ascii=False)
        with db() as conn:
            conn.execute(
                "UPDATE posts SET media_json=? WHERE id=?",
                (media_json, row["id"]),
            )
            persist_media_to_siblings(
                conn,
                external_id=row.get("external_id") or "",
                media=media,
                except_id=int(row["id"]),
            )

        oversized_files = []
        max_video_bytes = 250 * 1024 * 1024
        for m in media:
            if not isinstance(m, dict) or (m.get("type") or "").lower() != "video":
                continue
            path = local_media_path(m)
            if path and path.stat().st_size > max_video_bytes:
                oversized_files.append(m)
        if oversized_files:
            media = [m for m in media if m not in oversized_files]
            media_json = json.dumps(media, ensure_ascii=False)
            if not has_publishable_media(media):
                with db() as conn:
                    mark_post(conn, row["id"], "skipped", "видео слишком большое")
                print(f"[worker] skip post={row['id']} video over MAX limit", flush=True)
                continue

        missing_video = [
            m
            for m in media
            if isinstance(m, dict)
            and (m.get("type") or "").lower() == "video"
            and local_media_path(m) is None
            and (
                is_oversized_video(m)
                or not ((m.get("url") or "").startswith("http"))
            )
        ]
        if missing_video:
            refs = ", ".join(
                (m.get("tg_ref") or m.get("type") or "?") for m in missing_video[:3]
            )
            if (text or "").strip() and not require_media:
                media = [m for m in media if m not in missing_video]
                media_json = json.dumps(media, ensure_ascii=False)
                print(
                    f"[worker] drop undownloadable video post={row['id']} {refs}",
                    flush=True,
                )
            else:
                with db() as conn:
                    mark_post(
                        conn,
                        row["id"],
                        "skipped",
                        f"видео слишком большое / не скачалось: {refs}"[:800],
                    )
                print(f"[worker] skip post={row['id']} video missing {refs}", flush=True)
                continue

        if media_needs_tg_download(media):
            print(f"[worker] tg-download post={row['id']} …", flush=True)
            media = materialize_media_list(media)
            media_json = json.dumps(media, ensure_ascii=False)
            with db() as conn:
                conn.execute(
                    "UPDATE posts SET media_json=? WHERE id=?",
                    (media_json, row["id"]),
                )
            missing_media = [
                m
                for m in media
                if (m.get("type") or "").lower() in {"image", "video"}
                and local_media_path(m) is None
                and (
                    m.get("too_big")
                    or (m.get("tg_ref") and not (m.get("url") or "").strip())
                )
            ]
            if missing_media:
                refs = ", ".join(
                    (m.get("tg_ref") or m.get("type") or "?") for m in missing_media[:3]
                )
                too_big_only = all(bool(m.get("too_big")) for m in missing_media)
                prev = row.get("publish_error") or ""
                attempt = 1
                m_att = re.search(r"попытка\s+(\d+)", prev)
                if m_att:
                    attempt = int(m_att.group(1)) + 1
                can_text = bool((text or "").strip()) and not require_media
                flood = tg_is_flood_blocked()
                if can_text and (flood or too_big_only or attempt >= 3):
                    media = [m for m in media if m not in missing_media]
                    media_json = json.dumps(media, ensure_ascii=False)
                    print(
                        f"[worker] send text without tg media post={row['id']} {refs}",
                        flush=True,
                    )
                elif too_big_only and attempt >= 3:
                    with db() as conn:
                        mark_post(
                            conn,
                            row["id"],
                            "skipped",
                            f"видео слишком большое / не скачалось: {refs}"[:800],
                        )
                    print(
                        f"[worker] skip post={row['id']} too-big after {attempt} tries {refs}",
                        flush=True,
                    )
                    continue
                else:
                    retry_at = time.time() + 120.0
                    with db() as conn:
                        conn.execute(
                            """
                            UPDATE posts
                            SET publish_status='pending', publish_error=?, publish_at=?
                            WHERE id=?
                            """,
                            (
                                f"ожидание TG-скачки (попытка {attempt}): {refs}"[:800],
                                retry_at,
                                row["id"],
                            ),
                        )
                    print(
                        f"[worker] defer post={row['id']} tg media {refs} (retry in 120s)",
                        flush=True,
                    )
                    continue

        watermark = ""
        footer_link = ""
        footer_link_text = ""
        dest_platform = (row.get("platform") or "max").strip().lower()
        dest_token = (row.get("bot_token") or "").strip()
        dest_upload_token = (row.get("upload_token") or "").strip()
        with db() as conn:
            # повторная проверка статуса
            st = conn.execute(
                "SELECT publish_status FROM posts WHERE id=?", (row["id"],)
            ).fetchone()
            if not st or st["publish_status"] not in {"queued", "pending"}:
                continue
            if not register_send(conn, chat_id, external_id, fp, post_id=row["id"]):
                mark_post(conn, row["id"], "skipped", "дубль (уже публиковали)")
                print(f"[worker] skip duplicate {external_id}", flush=True)
                continue
            ch = conn.execute(
                """
                SELECT watermark_text, footer_link, footer_link_text,
                       COALESCE(footer_as_button, 0) AS footer_as_button,
                       COALESCE(platform, 'max') AS platform,
                       COALESCE(bot_token, '') AS bot_token,
                       COALESCE(upload_token, '') AS upload_token
                FROM channels WHERE chat_id=?
                """,
                (chat_id,),
            ).fetchone()
            watermark = (ch["watermark_text"] if ch else "") or ""
            footer_link = ((ch["footer_link"] if ch else "") or "").strip()
            footer_link_text = ((ch["footer_link_text"] if ch else "") or "").strip()
            footer_as_button = bool(ch and int(ch["footer_as_button"] or 0))
            if ch:
                dest_platform = (ch["platform"] or dest_platform or "max").strip().lower()
                dest_token = (ch["bot_token"] or dest_token or "").strip()
                dest_upload_token = (ch["upload_token"] or dest_upload_token or "").strip()

        # своя ссылка канала — только к реальному контенту (не к пустому посту)
        publishable_media = [
            m
            for m in media
            if (m.get("type") or "").lower() in {"image", "video"} and m.get("url")
        ]
        if not body_text and not publishable_media:
            with db() as conn:
                mark_post(conn, row["id"], "skipped", "пустой пост")
            print(f"[worker] skip empty post={row['id']}", flush=True)
            continue

        if dest_platform == "vk":
            if not dest_token:
                with db() as conn:
                    conn.execute(
                        "DELETE FROM send_log WHERE chat_id=? AND external_id=?",
                        (chat_id, external_id),
                    )
                    mark_post(conn, row["id"], "error", "нет ключа VK у группы")
                print(f"[worker] skip vk post={row['id']}: no token", flush=True)
                continue
            try:
                _publish_vk(
                    owner_id=chat_id,
                    media=media,
                    body_text=body_text,
                    watermark=watermark,
                    footer_link=footer_link,
                    footer_link_text=footer_link_text,
                    token=dest_token,
                    upload_token=dest_upload_token,
                )
            except VkApiError as e:
                with db() as conn:
                    conn.execute(
                        "DELETE FROM send_log WHERE chat_id=? AND external_id=?",
                        (chat_id, external_id),
                    )
                    mark_post(conn, row["id"], "error", str(e)[:800])
                print(f"[worker] vk publish fail post={row['id']}: {e}", flush=True)
                time.sleep(2.0)
                continue
            except Exception as e:
                with db() as conn:
                    conn.execute(
                        "DELETE FROM send_log WHERE chat_id=? AND external_id=?",
                        (chat_id, external_id),
                    )
                    mark_post(conn, row["id"], "error", str(e)[:800])
                print(f"[worker] vk publish fail post={row['id']}: {e}", flush=True)
                time.sleep(2.0)
                continue
            with db() as conn:
                mark_post(conn, row["id"], "sent")
            sent += 1
            time.sleep(1.0)
            continue

        if dest_platform == "telegram":
            try:
                _publish_telegram(
                    chat_id=chat_id,
                    media=media,
                    body_text=body_text,
                    watermark=watermark,
                    footer_link=footer_link,
                    footer_link_text=footer_link_text,
                    footer_as_button=footer_as_button,
                    also_send_files=bool(rules.also_send_files),
                    bot_token=dest_token,
                )
            except TgBotApiError as e:
                body = str(e.body or e)
                with db() as conn:
                    conn.execute(
                        "DELETE FROM send_log WHERE chat_id=? AND external_id=?",
                        (chat_id, external_id),
                    )
                    mark_post(conn, row["id"], "error", body[:800])
                print(f"[worker] tg publish fail post={row['id']}: {body[:200]}", flush=True)
                time.sleep(2.0)
                continue
            except Exception as e:
                with db() as conn:
                    conn.execute(
                        "DELETE FROM send_log WHERE chat_id=? AND external_id=?",
                        (chat_id, external_id),
                    )
                    mark_post(conn, row["id"], "error", str(e)[:800])
                print(f"[worker] tg publish fail post={row['id']}: {e}", flush=True)
                time.sleep(2.0)
                continue
            with db() as conn:
                mark_post(conn, row["id"], "sent")
            sent += 1
            time.sleep(1.0)
            continue

        attachments = []
        file_attachments = []
        upload_failed_required = False
        for m in media[:10]:
            mtype = (m.get("type") or "").lower()
            murl = m.get("url")
            if not murl:
                continue
            if mtype == "image":
                att = client.upload_image_from_url(murl, watermark_text=watermark)
                if att:
                    attachments.append(att)
                    time.sleep(0.4)
                else:
                    upload_failed_required = True
                    break
                if rules.also_send_files and m.get("from_document"):
                    local = (m.get("local_path") or "").strip()
                    fname = (m.get("filename") or "").strip() or None
                    fatt = None
                    if local:
                        fatt = client.upload_file_from_path(local, filename=fname)
                    else:
                        fatt = client.upload_file_from_url(murl, filename=fname)
                    if fatt:
                        file_attachments.append(fatt)
                        time.sleep(0.4)
                    else:
                        upload_failed_required = True
                        break
            elif mtype == "video":
                # вотермарку на видео не ставим
                att = client.upload_video_from_url(murl)
                if att:
                    attachments.append(att)
                    time.sleep(1.0)
                else:
                    upload_failed_required = True
                    break

        if upload_failed_required:
            if body_text and not require_media:
                attachments = []
                print(
                    f"[worker] send text after media upload fail post={row['id']}",
                    flush=True,
                )
            else:
                with db() as conn:
                    conn.execute(
                        "DELETE FROM send_log WHERE chat_id=? AND external_id=?",
                        (chat_id, external_id),
                    )
                    mark_post(conn, row["id"], "skipped", "не удалось загрузить медиа")
                print(f"[worker] skip post={row['id']} media upload failed", flush=True)
                continue

        if not body_text and not attachments:
            with db() as conn:
                mark_post(conn, row["id"], "skipped", "пустой пост")
            print(f"[worker] skip empty after upload post={row['id']}", flush=True)
            continue

        # footer: по умолчанию текстовая ссылка; кнопка — только если footer_as_button=1
        msg_format = None
        text = body_text
        if footer_link:
            label = (footer_link_text or footer_link).strip() or footer_link
            if footer_as_button:
                attachments.append(
                    {
                        "type": "inline_keyboard",
                        "payload": {
                            "buttons": [
                                [
                                    {
                                        "type": "link",
                                        "text": label[:64],
                                        "url": footer_link,
                                    }
                                ]
                            ]
                        },
                    }
                )
            else:
                href = html_lib.escape(footer_link, quote=True)
                link_html = f'<a href="{href}">{html_lib.escape(label)}</a>'
                if body_text:
                    text = f"{html_lib.escape(body_text)}\n\n{link_html}"
                else:
                    text = link_html
                msg_format = "html"

        try:
            send_resp = client.send_message(
                chat_id=chat_id,
                text=text or None,
                attachments=attachments or None,
                disable_link_preview=True,
                format=msg_format,
            )
            dest_mid = extract_max_message_id(send_resp)
            # Обои: сразу следом исходники файлами (без текста/футера).
            # MAX: в одном сообщении допускается только 1 file-вложение.
            files_note = ""
            if rules.also_send_files and file_attachments:
                failed: list[str] = []
                for fi, fatt in enumerate(file_attachments):
                    file_ok = False
                    last_file_err = ""
                    for attempt in range(5):
                        try:
                            time.sleep(1.0 + attempt * 1.5)
                            client.send_message(
                                chat_id=chat_id,
                                text=None,
                                attachments=[fatt],
                                disable_link_preview=True,
                            )
                            file_ok = True
                            break
                        except MaxApiError as e:
                            last_file_err = str(e.body or e)
                            if "attachment.not.ready" in last_file_err and attempt < 4:
                                print(
                                    f"[worker] file[{fi}] not ready post={row['id']} try={attempt + 1}",
                                    flush=True,
                                )
                                continue
                            break
                    if not file_ok:
                        failed.append(f"#{fi}:{last_file_err}")
                        print(
                            f"[worker] post={row['id']} file[{fi}] failed: {last_file_err[:200]}",
                            flush=True,
                        )
                if failed:
                    files_note = f"картинки ок, файлы: {'; '.join(failed)}"[:800]
            with db() as conn:
                mark_post(conn, row["id"], "sent", files_note, dest_message_id=dest_mid)
            sent += 1
            time.sleep(1.0)
        except MaxApiError as e:
            body = str(e.body or e)
            status = "pending" if "attachment.not.ready" in body else "error"
            with db() as conn:
                # снимаем бронь дубля только если реально не ушло и это временная ошибка
                if status == "pending":
                    conn.execute(
                        "DELETE FROM send_log WHERE chat_id=? AND external_id=?",
                        (chat_id, external_id),
                    )
                mark_post(conn, row["id"], status, body[:800])
            time.sleep(2.0)
    return sent


def normalize_source_url(kind: str, url: str) -> str:
    url = url.strip()
    if kind == "telegram":
        if url.startswith("@"):
            url = f"https://t.me/{url[1:]}"
        norm = normalize_telegram_url(url)
        if not norm:
            raise ValueError("Нужна ссылка на публичный Telegram-канал")
        return norm
    if kind == "x":
        return normalize_x_url(url)
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url


def bootstrap_source(source_id: int) -> None:
    """Быстрый обход после добавления из админки."""
    settings = get_settings()
    with db() as conn:
        from app.db import get_source

        src = get_source(conn, source_id)
        if not src:
            return
        src = dict(src)
        set_source_status(conn, source_id, "обход", "Проверка источника…")

    try:
        rules = resolve_rules(
            chat_id=int(src.get("chat_id") or 0),
            title="",
            link="",
            source_url=src.get("url") or "",
        )
        if rules.archive_drip:
            with db() as conn:
                ch = conn.execute(
                    "SELECT title, link FROM channels WHERE chat_id=?",
                    (int(src["chat_id"]),),
                ).fetchone()
            rules = resolve_rules(
                chat_id=int(src["chat_id"]),
                title=(ch["title"] if ch else "") or "",
                link=(ch["link"] if ch else "") or "",
                source_url=src.get("url") or "",
            )
            title, posts = fetch_source_posts(
                src["kind"], src["url"], "", settings.vk_access_token, enrich_media=False
            )
            with db() as conn:
                set_source_status(
                    conn,
                    source_id,
                    "добавлен",
                    "Архив: берём старые видео по 2 в день, без текста",
                    title=title,
                    cursor="",
                )
            return
        title, posts = fetch_source_posts(
            src["kind"], src["url"], "", settings.vk_access_token, enrich_media=False
        )
        with db() as conn:
            cursor = posts[-1].external_id if posts else ""
            set_source_status(
                conn,
                source_id,
                "добавлен",
                "Источник добавлен, история не публикуется — только новые посты",
                title=title,
                cursor=cursor,
            )
    except Exception as e:
        with db() as conn:
            set_source_status(conn, source_id, "ошибка", str(e)[:500])


def main_loop() -> None:
    init_db()
    settings = get_settings()
    # После аварийного завершения claimed-посты могут навсегда остаться queued.
    # Свежие возвращаем в pending; совсем старые не должны блокировать актуальную
    # очередь попытками скачать уже протухшие CDN-медиа.
    with db() as conn:
        stale_before = time.time() - 6 * 3600
        stale_queued = conn.execute(
            """
            UPDATE posts
            SET publish_status='skipped',
                publish_error='устаревшая queued-задача после сбоя воркера'
            WHERE publish_status='queued' AND created_at < ?
            """,
            (stale_before,),
        ).rowcount
        recovered = conn.execute(
            """
            UPDATE posts SET publish_status='pending'
            WHERE publish_status='queued' AND created_at >= ?
            """,
            (stale_before,),
        ).rowcount
    if stale_queued:
        print(f"[worker] skipped stale queued={stale_queued}", flush=True)
    if recovered:
        print(f"[worker] recovered queued={recovered}", flush=True)
    print(f"[worker] start poll={settings.poll_interval_sec}s", flush=True)
    last_sync = 0.0
    last_meta = 0.0
    last_poll = 0.0
    last_pub = 0.0
    last_silence = 0.0
    last_vk_token = 0.0
    while True:
        try:
            with MaxClient() as client:
                me = client.me()
                print(f"[worker] bot=@{me.get('username')} id={me.get('user_id')}", flush=True)
                while True:
                    now = time.time()
                    if now - last_vk_token >= settings.vk_token_refresh_sec:
                        # Ставим timestamp до внешнего вызова: ошибка не должна
                        # зациклить задачу и заблокировать poll/publish.
                        last_vk_token = now
                        try:
                            from workers.vk_token_refresh import refresh_vk_user_token

                            if refresh_vk_user_token():
                                print("[worker] vk upload token refreshed", flush=True)
                        except Exception:
                            traceback.print_exc()
                    if now - last_sync >= settings.sync_chats_interval_sec:
                        last_sync = now
                        try:
                            do_meta = (now - last_meta) >= settings.sync_meta_interval_sec
                            n = sync_chats(client, refresh_meta=do_meta)
                            if do_meta:
                                last_meta = now
                            tn = sync_tg_chats()
                            print(
                                f"[worker] sync chats={n} tg={tn} meta={do_meta}",
                                flush=True,
                            )
                        except Exception:
                            traceback.print_exc()
                    if now - last_pub >= settings.publish_interval_sec:
                        last_pub = now
                        try:
                            with db() as conn:
                                expired = expire_stale_unpublished(conn)
                            if expired:
                                print(f"[worker] expired unpublished={expired}", flush=True)
                            n = publish_pending(client)
                            if n:
                                print(f"[worker] published={n}", flush=True)
                        except Exception:
                            traceback.print_exc()
                    if now - last_poll >= settings.poll_interval_sec:
                        last_poll = now
                        try:
                            poll_sources(client)
                            print("[worker] sources polled", flush=True)
                        except Exception:
                            traceback.print_exc()
                    if now - last_silence >= settings.silence_check_interval_sec:
                        last_silence = now
                        try:
                            from workers.silence import check_channel_silence

                            n = check_channel_silence()
                            if n:
                                print(f"[worker] silence alerts={n}", flush=True)
                        except Exception:
                            traceback.print_exc()
                    time.sleep(2)
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main_loop()
