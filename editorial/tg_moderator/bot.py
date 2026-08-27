"""Telegram moderation bot: callbacks + text FSM."""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from app.config import get_settings
from editorial.channel_config import get_channel
from editorial.moderation import (
    mark_unacceptable,
    moderation_enabled,
    publish_approved,
    reject_post,
    rerender_after_image,
    save_edited_text,
    save_event_type,
)
from editorial.moderation_feedback import log_moderation
from editorial.moderation_session import (
    clear_input_step,
    get_awaiting_input_session,
    get_session,
    get_session_by_prompt_message,
    photo_pool_from_session,
    upsert_session,
)
from editorial.store import get_news
from editorial.tg_moderator import api
from editorial.tg_moderator.keyboards import (
    category_keyboard,
    unacceptable_keyboard,
    photo_pick_keyboard,
    review_keyboard,
)
from editorial.tg_moderator.notify import (
    allow_photo_button,
    finalize_review_card,
    send_review_card,
)


def _admin_id() -> int:
    return int(get_settings().telegram_admin_id or 0)


def _is_admin(user_id: int | None) -> bool:
    return bool(user_id) and int(user_id) == _admin_id()


def _parse_cb(data: str) -> tuple[str, int, str]:
    parts = (data or "").split(":", 2)
    action = parts[0] if parts else ""
    try:
        news_id = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        news_id = 0
    extra = parts[2] if len(parts) > 2 else ""
    return action, news_id, extra


def _refresh_card(news_id: int, chat_id: int | str) -> None:
    row = get_news(news_id)
    if not row:
        return
    cfg = get_channel(str(row.get("channel_slug") or ""))
    if not cfg:
        return
    send_review_card(row, cfg, admin_id=int(chat_id))


def _card_message_id(cb: dict[str, Any]) -> int:
    try:
        return int((cb.get("message") or {}).get("message_id") or 0)
    except (TypeError, ValueError):
        return 0


def _channel_for_row(row: dict[str, Any] | None):
    if not row:
        return None
    return get_channel(str(row.get("channel_slug") or ""))


def handle_callback(update: dict[str, Any]) -> None:
    cb = update.get("callback_query") or {}
    user = (cb.get("from") or {}).get("id")
    if not _is_admin(user):
        api.answer_callback(str(cb.get("id") or ""), "Нет доступа", show_alert=True)
        return
    data = str(cb.get("data") or "")
    action, news_id, extra = _parse_cb(data)
    chat_id = (cb.get("message") or {}).get("chat", {}).get("id") or _admin_id()
    cb_id = str(cb.get("id") or "")

    if not news_id:
        api.answer_callback(cb_id, "Ошибка id")
        return

    row = get_news(news_id)
    if not row and action not in {"back"}:
        api.answer_callback(cb_id, "Пост не найден", show_alert=True)
        return

    if row and action in {"ok", "no", "bad", "badr", "txt", "photo", "pick", "cat", "catr"}:
        st = str(row.get("status") or "")
        sess = get_session(news_id)
        if st == "published" or (sess and str(sess.get("step") or "") == "done"):
            api.answer_callback(cb_id, "Карточка устарела", show_alert=True)
            return

    if action == "ok":
        msg_id = _card_message_id(cb)
        cfg = _channel_for_row(row)
        res = publish_approved(news_id)
        if res.get("ok"):
            api.answer_callback(cb_id, "Опубликовано")
            if cfg and msg_id:
                fresh = get_news(news_id) or row
                tg = (res.get("res") or {}).get("tg_mirror") or {}
                detail = ""
                if tg and tg.get("ok") is False:
                    detail = f"Telegram: {tg.get('error', 'ошибка')}"
                finalize_review_card(chat_id, msg_id, fresh, cfg, "approved", detail=detail)
        else:
            api.answer_callback(cb_id, str(res.get("msg") or "Ошибка"), show_alert=True)
        return

    if action == "no":
        msg_id = _card_message_id(cb)
        cfg = _channel_for_row(row)
        reject_post(news_id, reason="tg reject")
        api.answer_callback(cb_id, "Отклонено")
        if cfg and msg_id and row:
            finalize_review_card(chat_id, msg_id, row, cfg, "rejected")
        return

    if action == "bad":
        api.answer_callback(cb_id)
        msg_id = _card_message_id(cb)
        if msg_id:
            api.edit_message_reply_markup(chat_id, msg_id, unacceptable_keyboard(news_id))
        return

    if action == "badr":
        msg_id = _card_message_id(cb)
        cfg = _channel_for_row(row)
        mark_unacceptable(news_id, extra or "feed_trash")
        api.answer_callback(cb_id, "Помечено как недопустимый тип")
        if cfg and msg_id and row:
            finalize_review_card(
                chat_id,
                msg_id,
                row,
                cfg,
                "unacceptable",
                detail=_esc_reason(extra),
            )
        return

    if action == "txt":
        clear_input_step(_admin_id(), except_news_id=news_id)
        api.answer_callback(cb_id)
        prompt = api.send_message(
            chat_id,
            f"✏️ Пришлите новый текст для #{news_id} (стикер в начале абзаца).\n"
            f"Ответьте reply на это сообщение.",
        )
        upsert_session(
            news_id,
            admin_id=_admin_id(),
            step="edit_text",
            tg_chat_id=chat_id,
            prompt_message_id=int(prompt.get("message_id") or 0),
        )
        return

    if action == "photo":
        if row and not allow_photo_button(row):
            api.answer_callback(cb_id, "Для video/meme запрос фото недоступен", show_alert=True)
            return
        clear_input_step(_admin_id(), except_news_id=news_id)
        api.answer_callback(cb_id)
        prompt = api.send_message(
            chat_id,
            f"🔍 Введите поисковый запрос для фото (#{news_id}):\n"
            f"Ответьте reply на это сообщение.",
        )
        upsert_session(
            news_id,
            admin_id=_admin_id(),
            step="photo_query",
            tg_chat_id=chat_id,
            prompt_message_id=int(prompt.get("message_id") or 0),
        )
        return

    if action == "pick":
        try:
            idx = int(extra)
        except ValueError:
            api.answer_callback(cb_id, "Неверный номер")
            return
        _apply_photo_pick(news_id, idx, chat_id)
        api.answer_callback(cb_id, f"Фото #{idx + 1}")
        return

    if action == "cat":
        cfg = _channel_for_row(row)
        allowed = list(cfg.event_types or []) if cfg else []
        api.answer_callback(cb_id)
        msg_id = _card_message_id(cb)
        if msg_id:
            api.edit_message_reply_markup(chat_id, msg_id, category_keyboard(news_id, allowed))
        return

    if action == "catr":
        from editorial.event_labels import event_type_label

        api.answer_callback(cb_id, "Обновляю обложку…")
        ok, msg = save_event_type(news_id, extra)
        if not ok:
            api.send_message(chat_id, f"Категория не сохранена: {msg}", parse_mode=None)
            return
        if msg == "same":
            api.send_message(chat_id, "Категория уже такая")
        else:
            api.send_message(chat_id, f"Категория: {event_type_label(extra)}")
        _refresh_card(news_id, chat_id)
        upsert_session(news_id, admin_id=_admin_id(), step="review", tg_chat_id=chat_id)
        return

    if action == "back":
        api.answer_callback(cb_id)
        _refresh_card(news_id, chat_id)
        upsert_session(news_id, admin_id=_admin_id(), step="review", tg_chat_id=chat_id)
        return

    api.answer_callback(cb_id, "?")


def _esc_reason(reason: str) -> str:
    from editorial.content_blocks import UNACCEPTABLE_LABELS

    return UNACCEPTABLE_LABELS.get(reason, reason)


def _apply_photo_pick(news_id: int, idx: int, chat_id: int | str) -> None:
    session = get_session(news_id)
    pool = photo_pool_from_session(session or {})
    if idx < 0 or idx >= len(pool):
        api.send_message(chat_id, "Нет такого номера в пуле")
        return
    row = get_news(news_id)
    if not row:
        return
    cfg = get_channel(str(row.get("channel_slug") or ""))
    if not cfg:
        return
    entry = pool[idx]
    path = str(entry.get("cropped") or entry.get("path") or "")
    if not path or not Path(path).is_file():
        from editorial.imagery import ImageCandidate, apply_photo_choice

        cand = ImageCandidate(
            path=Path(str(entry.get("path"))),
            url=str(entry.get("url") or ""),
            via=str(entry.get("via") or "manual"),
            width=int(entry.get("width") or 0),
            height=int(entry.get("height") or 0),
            relevance=float(entry.get("score") or 0),
        )
        template = cfg.template_for(row.get("event_type") or "other")
        cropped = apply_photo_choice(row, cand, template_name=template)
        if not cropped:
            api.send_message(chat_id, "Не удалось обработать фото")
            return
        path = cropped
        entry["cropped"] = cropped
    rerender_after_image(news_id, path, cfg)
    fresh = get_news(news_id) or row
    log_moderation(
        {
            "action": "photo_chosen",
            "admin_id": _admin_id(),
            "news_id": news_id,
            "pick_idx": idx,
            "pool_entry": entry,
            "session_query": (session or {}).get("photo_query") or "",
        }
    )
    upsert_session(news_id, admin_id=_admin_id(), step="review", tg_chat_id=chat_id)
    send_review_card(fresh, cfg, admin_id=int(chat_id))


def _send_pool_preview(chat_id: int | str, path: Path | str, caption: str) -> bool:
    from editorial.tg_moderator import api
    from editorial.tg_moderator.media import prepare_tg_preview

    src = Path(path)
    if not src.is_file():
        return False
    candidates = [src]
    try:
        prepared = prepare_tg_preview(src)
        if prepared != src:
            candidates.append(prepared)
    except Exception as e:
        print(f"[tg-moderator] preview prep fail {src}: {e}", flush=True)

    last_err = ""
    for photo in candidates:
        try:
            api.send_photo(chat_id, photo, caption=caption)
            return True
        except api.TelegramApiError as e:
            last_err = str(e)
    if last_err:
        print(f"[tg-moderator] preview send fail {src}: {last_err}", flush=True)
    return False


def _build_photo_pool(news_id: int, query: str, chat_id: int | str) -> None:
    row = get_news(news_id)
    if not row:
        return
    cfg = get_channel(str(row.get("channel_slug") or ""))
    if not cfg:
        return
    from editorial.imagery import apply_photo_choice, build_photo_pool

    api.send_message(chat_id, f"🔍 Ищу фото для #{news_id}…")
    template = cfg.template_for(row.get("event_type") or "other")
    limit = int(get_settings().moderation_photo_pool_size or 6)
    try:
        ranked, trace = build_photo_pool(row, query, template_name=template, limit=limit)
    except Exception as e:
        print(f"[tg-moderator] photo pool fail: {e}", flush=True)
        api.send_message(chat_id, f"Ошибка поиска фото: {str(e)[:200]}", parse_mode=None)
        upsert_session(news_id, admin_id=_admin_id(), step="photo_query", tg_chat_id=chat_id)
        return
    if not ranked:
        api.send_message(chat_id, "По запросу нет годных фото. Попробуйте другой.")
        upsert_session(news_id, admin_id=_admin_id(), step="photo_query", tg_chat_id=chat_id)
        return

    pool_json: list[dict[str, Any]] = []
    sent = 0
    for cand in ranked:
        cropped = apply_photo_choice(row, cand, template_name=template)
        preview_path = Path(cropped or cand.path)
        entry = {
            "idx": sent,
            "path": str(cand.path),
            "cropped": cropped or "",
            "url": cand.url,
            "via": cand.via,
            "width": cand.width,
            "height": cand.height,
            "score": cand.relevance,
            "reason": cand.reason,
            "extras": cand.extras,
        }
        cap = f"#{sent + 1} · {cand.relevance:.2f} · {cand.reason[:60]}"
        if _send_pool_preview(chat_id, preview_path, cap):
            pool_json.append(entry)
            sent += 1

    if not pool_json:
        api.send_message(
            chat_id,
            "Фото нашлись, но Telegram не принял превью. Попробуйте другой запрос.",
        )
        upsert_session(news_id, admin_id=_admin_id(), step="photo_query", tg_chat_id=chat_id)
        return
    upsert_session(
        news_id,
        admin_id=_admin_id(),
        step="photo_pick",
        tg_chat_id=chat_id,
        photo_query=query,
        photo_pool=pool_json,
    )
    log_moderation(
        {
            "action": "photo_pool",
            "admin_id": _admin_id(),
            "news_id": news_id,
            "query": query,
            "trace_query": trace.get("query"),
            "candidates": pool_json,
        }
    )
    api.send_message(
        chat_id,
        f"Выберите фото для #{news_id} (запрос: {query}):",
        reply_markup=photo_pick_keyboard(news_id, len(pool_json)),
    )


def handle_message(update: dict[str, Any]) -> None:
    msg = update.get("message") or {}
    user_id = (msg.get("from") or {}).get("id")
    if not _is_admin(user_id):
        return
    text = str(msg.get("text") or "").strip()
    if not text or text.startswith("/"):
        if text in {"/start", "/help"}:
            api.send_message(
                user_id,
                "Модерация editorial: ждите карточки готовых постов. Кнопки — под превью.",
            )
        return
    chat_id = msg.get("chat", {}).get("id") or user_id
    reply = msg.get("reply_to_message") or {}
    reply_mid = 0
    try:
        reply_mid = int(reply.get("message_id") or 0)
    except (TypeError, ValueError):
        reply_mid = 0

    session = None
    if reply_mid:
        session = get_session_by_prompt_message(_admin_id(), reply_mid)
    if not session:
        session = get_awaiting_input_session(_admin_id())
    if not session:
        api.send_message(
            chat_id,
            "Нажмите кнопку под нужным постом ещё раз (✏️/🔍), затем ответьте reply на запрос бота.",
        )
        return

    news_id = int(session.get("news_id") or 0)
    step = str(session.get("step") or "")

    if step == "edit_text":
        ok, why = save_edited_text(news_id, text)
        if not ok:
            api.send_message(chat_id, f"Текст не принят: {why}", parse_mode=None)
            return
        upsert_session(
            news_id,
            admin_id=_admin_id(),
            step="review",
            tg_chat_id=chat_id,
            prompt_message_id=0,
        )
        api.send_message(chat_id, f"Текст обновлён для #{news_id}")
        _refresh_card(news_id, chat_id)
        return

    if step == "photo_query":
        _build_photo_pool(news_id, text, chat_id)
        return

    api.send_message(
        chat_id,
        "Нажмите кнопку под нужным постом ещё раз, затем введите текст reply на запрос.",
    )


def process_update(update: dict[str, Any]) -> None:
    if update.get("callback_query"):
        handle_callback(update)
    elif update.get("message"):
        handle_message(update)


def run_poll_loop() -> None:
    offset = 0
    print("[tg-moderator] polling…", flush=True)
    while True:
        try:
            updates = api.get_updates(offset=offset, timeout=25)
            for upd in updates:
                offset = max(offset, int(upd.get("update_id") or 0) + 1)
                try:
                    process_update(upd)
                except Exception as e:
                    print(f"[tg-moderator] update fail: {e}", flush=True)
                    traceback.print_exc()
        except Exception as e:
            print(f"[tg-moderator] poll fail: {e}", flush=True)
            traceback.print_exc()
