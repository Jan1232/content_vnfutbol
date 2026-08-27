"""Publish rendered editorial posts via MaxClient — or into the admin feed (dry_run)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.db import (
    db,
    ensure_editorial_source,
    insert_simulated_editorial_post,
)
from app.max_api import MaxApiError, MaxClient
from editorial.channel_config import EditorialChannelConfig
from editorial.tg_content.publisher import mirror_enabled
from editorial.models import utcnow_iso
from editorial.pick import pick_tag_of
from editorial.store import cluster_published, update_news


def story_gate(channel_slug: str, item: dict[str, Any]) -> tuple[bool, str, str, int]:
    """Throttling / profanity gate before publish. Fallback — пропуск."""
    try:
        from editorial.story_throttle import story_gate as _gate

        return _gate(channel_slug, item)
    except Exception:
        return True, "", "", 0


def _extract_mid(resp: dict[str, Any] | None) -> str:
    if not isinstance(resp, dict):
        return ""
    for key in ("mid", "message_id", "id"):
        if isinstance(resp.get(key), str) and resp[key]:
            return resp[key]
    msg = resp.get("message")
    if isinstance(msg, dict):
        body = msg.get("body") if isinstance(msg.get("body"), dict) else msg
        if isinstance(body, dict):
            for key in ("mid", "message_id", "id"):
                if isinstance(body.get(key), str) and body[key]:
                    return body[key]
        for key in ("mid", "message_id", "id"):
            if isinstance(msg.get(key), str) and msg[key]:
                return msg[key]
    return ""


def _publish_simulated(
    channel: EditorialChannelConfig,
    item: dict[str, Any],
    *,
    published_at: str | None = None,
) -> dict[str, Any]:
    cover = Path(item.get("cover_path") or item.get("media_path") or "")
    if not cover.is_file():
        raise RuntimeError("нет медиа для simulated")
    news_id = int(item["id"])
    title = (channel.brand.name or channel.slug).strip() or "Editorial"
    stamp = published_at or utcnow_iso()
    try:
        from datetime import datetime, timezone

        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when_ts = when.timestamp()
    except Exception:
        when_ts = None
    with db() as conn:
        source_id = ensure_editorial_source(
            conn,
            int(channel.chat_id),
            channel.slug,
            title=f"{title} · editorial",
        )
        insert_simulated_editorial_post(
            conn,
            source_id=source_id,
            news_id=news_id,
            text=(item.get("post_text") or "")[:4000],
            cover_path=str(cover),
            source_url=str(item.get("url") or ""),
            when=when_ts,
        )
    update_news(
        news_id,
        status="published",
        mid="simulated",
        chat_id=str(channel.chat_id),
        published_at=stamp,
        last_error="dry_run: лента в админке, в MAX не отправлено",
    )
    try:
        from editorial.story_throttle import record_story_post, story_gate

        _ok, _r, sk, sr = story_gate(channel.slug, item)
        record_story_post(channel.slug, sk, news_id, sr)
    except Exception:
        pass
    return {"action": "simulated", "news_id": news_id, "mid": "simulated"}


def publish(
    client: MaxClient | None,
    channel: EditorialChannelConfig,
    item: dict[str, Any],
    *,
    published_at: str | None = None,
    force_live: bool = False,
) -> dict[str, Any]:
    cluster_id = item.get("cluster_id") or ""
    score_key = item.get("score_key") or ""
    chat_id = str(channel.chat_id)
    if cluster_id and cluster_published(cluster_id, chat_id, score_key=score_key):
        if pick_tag_of(item) != "addition":
            update_news(int(item["id"]), status="skipped", last_error="уже публиковали cluster")
            return {"action": "skipped", "reason": "cluster_duplicate"}

    if channel.dry_run:
        return _publish_simulated(channel, item, published_at=published_at)

    # Ручное одобрение модератора — без story-throttle (человек уже решил публиковать)
    if not force_live:
        ok, reason, story_key_val, subtype = story_gate(channel.slug, item)
        if not ok:
            update_news(
                int(item["id"]),
                status="deferred",
                last_error=(reason or "story_gate")[:800],
            )
            return {"action": "deferred", "reason": reason, "story_key": story_key_val}
    else:
        story_key_val, subtype = "", 0
        try:
            _ok, _r, story_key_val, subtype = story_gate(channel.slug, item)
            if not _ok:
                print(
                    f"[editorial] force_live skip story_gate #{item.get('id')}: {_r}",
                    flush=True,
                )
        except Exception as e:
            print(f"[editorial] force_live story_gate err: {e}", flush=True)

    if client is None:
        raise RuntimeError("MaxClient не передан для боевой публикации")

    media_type = str(item.get("media_type") or "")
    attachments: list[dict[str, Any]] = []
    if media_type == "video":
        vpath = Path(item.get("media_path") or "")
        if not vpath.is_file():
            raise RuntimeError("нет video файла")
        att = client.upload_video_from_path(vpath)
        if not att:
            raise RuntimeError("не удалось загрузить video в MAX")
        attachments.append(att)
    else:
        cover = Path(item.get("cover_path") or item.get("media_path") or "")
        if not cover.is_file():
            raise RuntimeError("нет cover PNG")
        att = client.upload_image_bytes(
            cover.read_bytes(), filename="cover.png", ctype="image/png"
        )
        if not att:
            raise RuntimeError("не удалось загрузить cover в MAX")
        attachments.append(att)

    if channel.cta.url and channel.cta.text:
        attachments.append(
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [
                        [
                            {
                                "type": "link",
                                "text": channel.cta.text[:64],
                                "url": channel.cta.url,
                            }
                        ]
                    ]
                },
            }
        )

    try:
        resp = client.send_message(
            chat_id=channel.chat_id,
            text=(item.get("post_text") or "")[:4000] or None,
            attachments=attachments,
            notify=True,
            disable_link_preview=True,
        )
    except MaxApiError as e:
        body = str(e.body or e)
        if "attachment.not.ready" in body:
            update_news(int(item["id"]), status="ready", last_error=body[:800])
            return {"action": "retry_ready", "error": body[:300]}
        raise

    mid = _extract_mid(resp)
    tg_mirror: dict[str, Any] = {}
    if mirror_enabled(channel):
        try:
            from editorial.tg_content.publisher import publish_mirror

            tg_mirror = publish_mirror(channel, item)
        except Exception as e:
            tg_mirror = {"ok": False, "error": str(e)[:400]}
            print(f"[editorial] tg mirror fail: {e}", flush=True)

    last_error = ""
    if tg_mirror and not tg_mirror.get("ok"):
        last_error = f"tg_mirror: {tg_mirror.get('error', 'fail')}"[:800]

    update_news(
        int(item["id"]),
        status="published",
        mid=mid,
        chat_id=chat_id,
        published_at=utcnow_iso(),
        last_error=last_error,
    )
    try:
        from editorial.story_throttle import record_story_post

        record_story_post(channel.slug, story_key_val, int(item["id"]), subtype)
    except Exception as e:
        print(f"[editorial] story log fail: {e}", flush=True)
    return {
        "action": "published",
        "mid": mid,
        "news_id": item["id"],
        "tg_mirror": tg_mirror,
    }
