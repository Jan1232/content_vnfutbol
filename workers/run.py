from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path
import html as html_lib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.db import (
    active_sources,
    claim_pending_posts,
    content_fingerprint,
    db,
    init_db,
    insert_post,
    mark_post,
    register_send,
    save_translation,
    set_source_status,
    upsert_channel,
    get_meta,
    set_meta,
)
from app.filter import (
    has_publishable_media,
    is_advertisement,
    sanitize_for_publish,
    scrub_promo_media,
)
from app.rules import resolve_rules
from app.max_api import MaxClient, MaxApiError
from app.tg_mtproto import media_needs_tg_download, resolve_media_list
from app.translate import translate_to_russian
from parsers.rss import parse_rss
from parsers.telegram import detect_kind, normalize_telegram_url, parse_telegram
from parsers.vk import parse_vk
from parsers.x import normalize_x_url, parse_x


def sync_chats(client: MaxClient) -> int:
    chats = client.list_chats()
    with db() as conn:
        for chat in chats:
            if chat.get("type") not in {"channel", "chat"}:
                continue
            if chat.get("status") != "active":
                continue
            upsert_channel(conn, chat)
        # также ловим bot_added из updates
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
    with db() as conn:
        for u in updates:
            ut = u.get("update_type")
            chat_id = u.get("chat_id")
            if ut == "bot_added" and chat_id is not None:
                try:
                    chat = client.get_chat(int(chat_id))
                    upsert_channel(conn, chat)
                except MaxApiError:
                    upsert_channel(
                        conn,
                        {
                            "chat_id": int(chat_id),
                            "title": f"chat {chat_id}",
                            "type": "channel" if u.get("is_channel") else "chat",
                            "status": "active",
                            "participants_count": 0,
                        },
                    )
            elif ut in {"bot_removed", "bot_stopped"} and chat_id is not None:
                conn.execute(
                    "UPDATE channels SET status=?, updated_at=? WHERE chat_id=?",
                    ("removed", time.time(), int(chat_id)),
                )
        if new_marker is not None:
            set_meta(conn, "updates_marker", str(new_marker))
    return len(chats)


def fetch_source_posts(kind: str, url: str, cursor: str, vk_token: str, *, enrich_media: bool = True):
    if kind == "telegram":
        return parse_telegram(url, since_id=cursor)
    if kind == "vk":
        return parse_vk(url, since_id=cursor, token=vk_token)
    if kind == "rss":
        return parse_rss(url, since_id=cursor)
    if kind == "x":
        return parse_x(url, since_id=cursor, enrich_media=enrich_media)
    raise ValueError(f"Неизвестный тип источника: {kind}")


def poll_sources() -> None:
    settings = get_settings()
    with db() as conn:
        sources = [dict(r) for r in active_sources(conn)]

    for src in sources:
        sid = src["id"]
        try:
            if (src.get("kind") or "") == "editorial":
                continue
            with db() as conn:
                set_source_status(conn, sid, "парсинг", "Читаю ленту…")

            title, posts = fetch_source_posts(
                src["kind"], src["url"], src.get("cursor") or "", settings.vk_access_token
            )

            # Первый запуск: не репостим историю — только ставим watermark
            first_run = not (src.get("cursor") or "")
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

                new_count = 0
                last_id = src.get("cursor") or ""
                rules = resolve_rules(
                    chat_id=int(src["chat_id"]),
                    title=src.get("channel_title") or "",
                    link=src.get("channel_link") or "",
                )
                mode = rules.moderation_mode
                require_media = 1 if rules.require_media else 0
                for p in posts:
                    # Проверяем оригинал (в т.ч. strip): иначе санитайз
                    # вырежет CTA/«здесь👉🏻» до фильтра и пост проскочит.
                    # Вырезка ссылок/подписей — только при публикации.
                    fr = is_advertisement(p.text, p.media, mode=mode)
                    is_ad = fr.is_ad
                    ad_reason = fr.reason
                    if require_media and not is_ad and not has_publishable_media(p.media):
                        is_ad = True
                        ad_reason = "нет медиа"
                    inserted = insert_post(
                        conn,
                        sid,
                        p.external_id,
                        p.text,
                        p.media,
                        p.source_url,
                        is_ad,
                        ad_reason,
                        publish_hold_sec=settings.publish_hold_sec,
                        republish_window_sec=settings.republish_window_sec,
                    )
                    if inserted:
                        new_count += 1
                        # «Media is too big» — оставляем pending: скачаем через Telethon при публикации
                    last_id = p.external_id

                detail = f"Ок. Новых: {new_count}"
                set_source_status(
                    conn,
                    sid,
                    "добавлен",
                    detail,
                    title=title or src.get("title") or "",
                    cursor=last_id if posts else src.get("cursor") or "",
                )
        except Exception as e:
            with db() as conn:
                set_source_status(conn, sid, "ошибка", str(e)[:500])


def publish_pending(client: MaxClient) -> int:
    import json

    sent = 0
    with db() as conn:
        rows = claim_pending_posts(conn, limit=10)

    for row in rows:
        text = (row.get("text") or "").strip()
        media_json = row.get("media_json") or "[]"
        media = json.loads(media_json)
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
        )
        mode = rules.moderation_mode
        require_media = 1 if rules.require_media else 0
        text = sanitize_for_publish(text, mode=mode)
        if mode != "strict":
            media = scrub_promo_media(media)
            media_json = json.dumps(media, ensure_ascii=False)
        if text != (row.get("text") or "").strip() or mode != "strict":
            with db() as conn:
                conn.execute(
                    "UPDATE posts SET text=?, media_json=? WHERE id=?",
                    (text, media_json, row["id"]),
                )

        if require_media and not has_publishable_media(media):
            with db() as conn:
                mark_post(conn, row["id"], "skipped", "нет медиа")
                conn.execute(
                    "UPDATE posts SET is_ad=1, ad_reason=? WHERE id=?",
                    ("нет медиа", row["id"]),
                )
            print(f"[worker] skip no-media post={row['id']}", flush=True)
            continue

        # повторная проверка рекламы после перевода/санитайза
        fr = is_advertisement(text, media, mode=mode)
        if fr.is_ad:
            with db() as conn:
                mark_post(conn, row["id"], "skipped", f"реклама: {fr.reason}")
                conn.execute(
                    "UPDATE posts SET is_ad=1, ad_reason=? WHERE id=?",
                    (fr.reason, row["id"]),
                )
            print(f"[worker] skip ad post={row['id']} reason={fr.reason}", flush=True)
            continue

        chat_id = int(row["chat_id"])
        external_id = row.get("external_id") or f"post:{row['id']}"
        fp = content_fingerprint(chat_id, text, media_json)

        body_text = (text or "").strip()

        # TG «Media is too big» / нет CDN-url — качаем через Telethon перед загрузкой в MAX
        if media_needs_tg_download(media):
            print(f"[worker] tg-download post={row['id']} …", flush=True)
            media = resolve_media_list(media)
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
                and (
                    m.get("too_big")
                    or (m.get("tg_ref") and not (m.get("url") or "").strip())
                )
            ]
            if missing_media:
                refs = ", ".join(
                    (m.get("tg_ref") or m.get("type") or "?") for m in missing_media[:3]
                )
                # Не skip навсегда: повторим после логина / сети
                retry_at = time.time() + 120.0
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE posts
                        SET publish_status='pending', publish_error=?, publish_at=?
                        WHERE id=?
                        """,
                        (
                            f"ожидание TG-скачки (Media is too big): {refs}"[:800],
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
                       COALESCE(footer_as_button, 0) AS footer_as_button
                FROM channels WHERE chat_id=?
                """,
                (chat_id,),
            ).fetchone()
            watermark = (ch["watermark_text"] if ch else "") or ""
            footer_link = ((ch["footer_link"] if ch else "") or "").strip()
            footer_link_text = ((ch["footer_link_text"] if ch else "") or "").strip()
            footer_as_button = bool(ch and int(ch["footer_as_button"] or 0))

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

        attachments = []
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
            client.send_message(
                chat_id=chat_id,
                text=text or None,
                attachments=attachments or None,
                disable_link_preview=True,
                format=msg_format,
            )
            with db() as conn:
                mark_post(conn, row["id"], "sent")
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
    print(f"[worker] start poll={settings.poll_interval_sec}s", flush=True)
    last_sync = 0.0
    last_poll = 0.0
    last_pub = 0.0
    while True:
        try:
            with MaxClient() as client:
                me = client.me()
                print(f"[worker] bot=@{me.get('username')} id={me.get('user_id')}", flush=True)
                while True:
                    now = time.time()
                    try:
                        if now - last_sync >= settings.sync_chats_interval_sec:
                            n = sync_chats(client)
                            print(f"[worker] sync chats={n}", flush=True)
                            last_sync = now
                        if now - last_poll >= settings.poll_interval_sec:
                            poll_sources()
                            print("[worker] sources polled", flush=True)
                            last_poll = now
                        if now - last_pub >= settings.publish_interval_sec:
                            n = publish_pending(client)
                            if n:
                                print(f"[worker] published={n}", flush=True)
                            last_pub = now
                    except Exception:
                        traceback.print_exc()
                    time.sleep(2)
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main_loop()
