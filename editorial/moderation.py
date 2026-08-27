"""Editorial moderation: dispatch queue, approve, TG notify hooks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.max_api import MaxApiError, MaxClient
from editorial.channel_config import EditorialChannelConfig, get_channel
from editorial.content_blocks import add_content_block, is_content_blocked
from editorial.moderation_feedback import item_snapshot, log_moderation
from editorial.moderation_session import clear_session, upsert_session
from editorial.publisher import publish
from editorial.scheduler import is_priority, mark_normal_published, mark_priority_published, pick_best, slot_ready
from editorial.store import get_news, list_ready, update_news


def moderation_enabled(cfg: EditorialChannelConfig | None = None) -> bool:
    settings = get_settings()
    if not bool(getattr(settings, "editorial_tg_moderation", False)):
        return False
    if not (settings.api_telegram_bot_token or "").strip():
        return False
    if not int(getattr(settings, "telegram_admin_id", 0) or 0):
        return False
    if cfg is not None:
        return bool(cfg.moderate_before_publish)
    return True


def count_awaiting_review(channel_slug: str, *, regular_only: bool = False) -> int:
    from app.db import db

    q = """
            SELECT COUNT(*) AS n FROM editorial_news
            WHERE channel_slug=? AND status='awaiting_review'
            """
    args: list[Any] = [channel_slug]
    if regular_only:
        # fixture_result и мемы — вне cadence-очереди (можно несколько параллельно)
        q += """
            AND event_type != 'fixture_result'
            AND COALESCE(meme_source, 0) = 0
            AND COALESCE(post_kind, '') NOT IN ('meme', 'video')
            """
    with db() as conn:
        row = conn.execute(q, args).fetchone()
    try:
        return int(row["n"] or 0) if row else 0
    except (TypeError, ValueError):
        return 0


def is_out_of_band_item(row: dict[str, Any]) -> bool:
    """Мемы/видео и счёт матчей — не занимают слот обычной очереди модерации."""
    if (row.get("event_type") or "") == "fixture_result":
        return True
    if int(row.get("meme_source") or 0):
        return True
    return str(row.get("post_kind") or "") in {"meme", "video"}


def _ready_pool(channel: EditorialChannelConfig) -> list[dict[str, Any]]:
    pool = list_ready(channel.slug)
    out: list[dict[str, Any]] = []
    for row in pool:
        blocked, _ = is_content_blocked(row)
        if blocked:
            update_news(int(row["id"]), status="filtered", last_error="content block")
            continue
        if not _has_preview_media(row):
            continue
        out.append(row)
    return out


def _has_preview_media(row: dict[str, Any]) -> bool:
    for key in ("cover_path", "media_path"):
        p = Path(str(row.get(key) or ""))
        if p.is_file():
            return True
    return False


def can_dispatch_review(
    channel: EditorialChannelConfig,
    *,
    force: bool = False,
) -> bool:
    depth = max(1, int(getattr(channel.moderation, "queue_depth", 3) or 3))
    if count_awaiting_review(channel.slug, regular_only=True) >= depth:
        return False
    pool = _ready_pool(channel)
    # обычная очередь — без fixture_result и мемов
    pool = [i for i in pool if not is_out_of_band_item(i)]
    if not pool:
        return False
    if force:
        return True
    if any(is_priority(i, channel) for i in pool):
        return True
    from editorial.scheduler import slot_ready

    return slot_ready(channel)


def _send_review_card(channel: EditorialChannelConfig, news_id: int, row: dict[str, Any]) -> dict[str, Any]:
    from editorial.tg_moderator.notify import send_review_card

    admin_id = int(get_settings().telegram_admin_id)
    try:
        msg = send_review_card(row, channel, admin_id=admin_id)
    except Exception as e:
        update_news(news_id, status="ready", last_error=f"tg notify: {e}"[:800])
        raise
    upsert_session(
        news_id,
        admin_id=admin_id,
        step="review",
        tg_chat_id=admin_id,
        tg_message_id=int(msg.get("message_id") or 0),
    )
    return msg


def dispatch_review_immediate(
    channel: EditorialChannelConfig,
    news_id: int,
) -> dict[str, Any]:
    """Результаты матчей: в TG сразу, без cadence-очереди (можно несколько параллельно)."""
    if not moderation_enabled(channel):
        return {"action": "moderation_off", "news_id": news_id}
    row = get_news(int(news_id))
    if not row:
        return {"action": "error", "reason": "missing"}
    st = str(row.get("status") or "")
    if st == "awaiting_review":
        return {"action": "already_review", "news_id": news_id}
    if st == "published":
        return {"action": "already_published", "news_id": news_id}
    update_news(int(news_id), status="awaiting_review", last_error="")
    row = get_news(int(news_id)) or row
    _send_review_card(channel, int(news_id), row)
    log_moderation(
        {
            "action": "sent_to_review_immediate",
            "admin_id": int(get_settings().telegram_admin_id),
            "item": item_snapshot(row),
        }
    )
    return {"action": "dispatched_review_immediate", "news_id": news_id}


def try_dispatch_review(
    channel: EditorialChannelConfig,
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    """Один пост в awaiting_review, пока count < queue_depth и слот/приоритет готов.

    force=True — сразу после reject/unacceptable (без ожидания cadence 40–55 мин).
    Мемы и fixture_result сюда не входят — см. try_dispatch_memes / dispatch_review_immediate.
    """
    if not moderation_enabled(channel):
        return None
    if not can_dispatch_review(channel, force=force):
        return None
    pool = _ready_pool(channel)
    pool = [i for i in pool if not is_out_of_band_item(i)]
    if not pool:
        return None
    picked = pick_best(pool) or pool[0]
    news_id = int(picked["id"])
    update_news(news_id, status="awaiting_review", last_error="")
    row = get_news(news_id) or picked
    _send_review_card(channel, news_id, row)
    log_moderation(
        {
            "action": "sent_to_review",
            "admin_id": int(get_settings().telegram_admin_id),
            "item": item_snapshot(row),
        }
    )
    return {"action": "dispatched_review", "news_id": news_id}


def try_dispatch_memes(
    channel: EditorialChannelConfig,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Готовые мемы/видео → в TG сразу, вне cadence и без блокировки обычной очереди."""
    if not moderation_enabled(channel):
        return []
    pool = [
        i
        for i in _ready_pool(channel)
        if is_out_of_band_item(i) and (i.get("event_type") or "") != "fixture_result"
    ]
    # старые сверху — не копить «хвост»
    pool = list(reversed(pool))[: max(1, int(limit or 10))]
    out: list[dict[str, Any]] = []
    for item in pool:
        try:
            res = dispatch_review_immediate(channel, int(item["id"]))
            res["kind"] = "meme"
            out.append(res)
        except Exception as e:
            out.append(
                {
                    "action": "meme_review_error",
                    "news_id": item.get("id"),
                    "error": str(e)[:200],
                }
            )
            print(f"[moderation] meme dispatch fail #{item.get('id')}: {e}", flush=True)
    return out


def publish_approved(news_id: int) -> dict[str, Any]:
    row = get_news(int(news_id))
    if not row:
        return {"ok": False, "msg": "missing"}
    st = str(row.get("status") or "")
    # deferred после прошлого story_gate — модератор может форсировать
    if st not in {"awaiting_review", "ready", "deferred"}:
        return {"ok": False, "msg": f"status={st}"}
    if st == "deferred":
        update_news(int(news_id), status="awaiting_review", last_error="")
        row = get_news(int(news_id)) or row
    cfg = get_channel(str(row.get("channel_slug") or ""))
    if not cfg:
        return {"ok": False, "msg": "no_channel"}
    client = MaxClient()
    try:
        res = publish(client, cfg, row, force_live=True)
    except MaxApiError as e:
        body = str(e.body or e)[:800]
        update_news(int(news_id), status="awaiting_review", last_error=body)
        return {"ok": False, "msg": body[:200]}
    except Exception as e:
        err = str(e)[:800]
        update_news(int(news_id), status="awaiting_review", last_error=err)
        return {"ok": False, "msg": err[:200]}

    action = str(res.get("action") or "")
    if action == "deferred":
        reason = str(res.get("reason") or "story_gate")
        update_news(int(news_id), status="awaiting_review", last_error=reason[:800])
        return {"ok": False, "msg": f"Отложено story-gate: {reason}"[:200]}
    if action in {"published", "simulated"}:
        if (row.get("event_type") or "") == "fixture_result":
            mark_priority_published(cfg)
            ext = str(row.get("external_id") or "")
            if ext.startswith("fixture:"):
                from editorial.store import mark_result_posted

                mark_result_posted(ext.split(":", 1)[1], cfg.slug, str(res.get("mid") or ""))
        elif is_priority(row, cfg):
            mark_priority_published(cfg)
        else:
            mark_normal_published(cfg)
        clear_session(int(news_id))
        log_moderation(
            {
                "action": "approved",
                "admin_id": int(get_settings().telegram_admin_id),
                "item": item_snapshot(row),
                "publish": res,
            }
        )
        try_dispatch_review(cfg)
        try:
            try_dispatch_memes(cfg)
        except Exception as e:
            print(f"[moderation] meme dispatch after approve fail: {e}", flush=True)
        return {"ok": True, "msg": action, "res": res}
    return {"ok": False, "msg": action or "unknown", "res": res}


def reject_post(news_id: int, *, reason: str = "manual reject") -> None:
    row = get_news(int(news_id))
    update_news(int(news_id), status="rejected", last_error=reason[:800])
    clear_session(int(news_id))
    if row:
        log_moderation(
            {
                "action": "rejected",
                "reason": reason,
                "admin_id": int(get_settings().telegram_admin_id),
                "item": item_snapshot(row),
            }
        )
        cfg = get_channel(str(row.get("channel_slug") or ""))
        if cfg:
            try:
                try_dispatch_review(cfg, force=True)
            except Exception as e:
                print(f"[moderation] dispatch after reject fail: {e}", flush=True)
            try:
                try_dispatch_memes(cfg)
            except Exception as e:
                print(f"[moderation] meme dispatch after reject fail: {e}", flush=True)


def mark_unacceptable(news_id: int, reason: str, *, note: str = "") -> dict[str, Any]:
    row = get_news(int(news_id))
    if not row:
        return {"ok": False, "msg": "missing"}
    block = add_content_block(row, reason=reason, news_id=news_id, note=note)
    update_news(
        int(news_id),
        status="rejected",
        last_error=f"unacceptable:{reason}"[:800],
    )
    clear_session(int(news_id))
    log_moderation(
        {
            "action": "unacceptable",
            "reason": reason,
            "block": block,
            "admin_id": int(get_settings().telegram_admin_id),
            "item": item_snapshot(row),
        }
    )
    cfg = get_channel(str(row.get("channel_slug") or ""))
    if cfg:
        try:
            try_dispatch_review(cfg, force=True)
        except Exception as e:
            print(f"[moderation] dispatch after unacceptable fail: {e}", flush=True)
        try:
            try_dispatch_memes(cfg)
        except Exception as e:
            print(f"[moderation] meme dispatch after unacceptable fail: {e}", flush=True)
    return {"ok": True, "block": block}


def save_event_type(news_id: int, event_type: str) -> tuple[bool, str]:
    row = get_news(int(news_id))
    if not row:
        return False, "missing"
    cfg = get_channel(str(row.get("channel_slug") or ""))
    if not cfg:
        return False, "no_channel"
    new_type = str(event_type or "").strip()
    allowed = set(cfg.event_types or ())
    if new_type not in allowed:
        return False, "категория недоступна для канала"
    old_type = str(row.get("event_type") or "")
    if old_type == new_type:
        return True, "same"

    entities: dict[str, Any] = {}
    try:
        entities = json.loads(row.get("entities_json") or "{}")
    except Exception:
        entities = {}
    entities["moderation_event_type"] = {
        "from": old_type,
        "to": new_type,
        "admin_id": int(get_settings().telegram_admin_id),
    }
    update_news(
        int(news_id),
        event_type=new_type,
        entities_json=json.dumps(entities, ensure_ascii=False),
        last_error="",
    )

    if str(row.get("media_type") or "") != "video" and new_type != "fixture_result":
        try:
            refresh_cover_after_category(int(news_id), cfg)
        except Exception as e:
            print(f"[moderation] cover after category fail: {e}", flush=True)
            update_news(int(news_id), last_error=f"cover: {e}"[:800])

    fresh = get_news(int(news_id)) or row
    log_moderation(
        {
            "action": "event_type_changed",
            "admin_id": int(get_settings().telegram_admin_id),
            "item": item_snapshot(fresh),
            "from": old_type,
            "to": new_type,
        }
    )
    return True, "ok"


_COVER_TEXT_TEMPLATES = frozenset({"default", "transfer", "breaking", "result"})


def refresh_cover_after_category(
    news_id: int,
    channel: EditorialChannelConfig,
) -> str | None:
    """После смены категории: ИИ-надпись на обложку (если шаблон её рисует) + рендер."""
    row = get_news(int(news_id))
    if not row:
        return None
    if str(row.get("media_type") or "") == "video":
        return None

    et = str(row.get("event_type") or "other")
    template = channel.template_for(et)
    settings = get_settings()
    meme_raw = (
        bool(int(row.get("meme_source") or 0))
        and et == "lifestyle"
        and not bool(getattr(settings, "meme_wrap_template", False))
    )
    media = str(row.get("media_path") or "")
    image = str(row.get("image_path") or "") or media
    if meme_raw and media and Path(media).is_file():
        update_news(
            int(news_id),
            image_path=media,
            cover_path=media,
            caption="",
            caption_line1="",
            caption_line2="",
            headline="",
            post_kind="meme",
            status="awaiting_review",
            last_error="",
        )
        return media

    if not image or not Path(image).is_file():
        return None

    caption_line1 = str(row.get("caption_line1") or "").strip()
    caption_line2 = str(row.get("caption_line2") or "").strip() or None
    if template in _COVER_TEXT_TEMPLATES:
        from editorial.caption import generate as generate_caption

        post_text = str(row.get("post_text") or row.get("title") or "")
        cap = generate_caption(row, post_text)
        caption_line1 = str(cap.get("caption_line1") or "").strip()
        caption_line2 = None
        patch: dict[str, Any] = {
            "caption": caption_line1,
            "caption_line1": caption_line1,
            "caption_line2": "",
            "headline": caption_line1,
            "last_error": "",
        }
        # Обложка через шаблон — не сырой мем; текст поста при meme_source без LLM
        if str(row.get("post_kind") or "") == "meme":
            patch["post_kind"] = "image"
        update_news(int(news_id), **patch)

    return rerender_after_image(int(news_id), image, channel)


def save_edited_text(news_id: int, text: str) -> tuple[bool, str]:
    from editorial.editor import accept_edited_text

    row = get_news(int(news_id))
    if not row:
        return False, "missing"
    if (row.get("event_type") or "") == "fixture_result":
        cleaned = (text or "").strip()
        if len(cleaned) < 8:
            return False, "мало текста"
        update_news(int(news_id), post_text=cleaned, caption=cleaned, last_error="")
    elif int(row.get("meme_source") or 0) or str(row.get("post_kind") or "") in {"meme", "video"}:
        from editorial.profanity import replace_profanity

        cleaned = replace_profanity((text or "").strip())
        if len(cleaned) < 2:
            return False, "мало текста"
        update_news(int(news_id), post_text=cleaned, last_error="")
        text = cleaned
    else:
        ok, why, cleaned = accept_edited_text(text, title=str(row.get("title") or ""))
        if not ok:
            return False, why
        update_news(int(news_id), post_text=cleaned, last_error="")
        text = cleaned
    fresh = get_news(int(news_id)) or row
    log_moderation(
        {
            "action": "text_edited",
            "admin_id": int(get_settings().telegram_admin_id),
            "item": item_snapshot(fresh),
            "text_before": (row.get("post_text") or "")[:2000],
            "text_after": text.strip()[:2000],
        }
    )
    return True, "ok"


def rerender_after_image(news_id: int, image_path: str, channel: EditorialChannelConfig) -> str | None:
    from editorial.imagery import ensure_template_crop
    from editorial.channel_config import brand_render_context
    from editorial.render import BADGE_FOR_EVENT, render_post

    row = get_news(int(news_id))
    if not row:
        return None
    template = channel.template_for(row.get("event_type") or "other")
    image_path = ensure_template_crop(image_path, template_name=template)
    badge = BADGE_FOR_EVENT.get(row.get("event_type") or "", "НОВОСТЬ")
    cover = render_post(
        template,
        image_path,
        row.get("caption_line1") or "",
        row.get("caption_line2") or None,
        badge,
        brand_render_context(channel),
        news_id=news_id,
    )
    update_news(
        int(news_id),
        image_path=image_path,
        cover_path=cover,
        status="awaiting_review",
        last_error="",
    )
    meta = {}
    try:
        meta = json.loads(row.get("imagery_meta_json") or "{}")
    except Exception:
        meta = {}
    meta["manual_pick"] = {"path": image_path, "cover": cover}
    update_news(int(news_id), imagery_meta_json=json.dumps(meta, ensure_ascii=False))
    return cover
