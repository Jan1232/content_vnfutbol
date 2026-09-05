"""Live-бот v3.2: медиа + ✅/❌/✏️/🔄 категория + 🔁 дубль + 🖼 ручной поиск картинки."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    FSInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.config import ARCHETYPES, FAN_MODEL, ROOT, _load_env
from src.ingest import db
from src.ingest.pipeline import regenerate_for_category, replace_media_manual_query
from src.ingest.sources import CATEGORY_LABELS, MEDIA_STRATEGY_LABELS

log = logging.getLogger("live_bot")
router = Router()
PENDING_KEY = "live_pending"


class Wait(StatesGroup):
    edit_text = State()
    duplicate_id = State()
    image_query = State()


def _owner_id() -> int:
    _load_env()
    raw = os.environ.get("OWNER_CHAT_ID", "").strip()
    return int(raw) if raw else 0


def _is_owner(uid: int) -> bool:
    oid = _owner_id()
    return bool(oid) and uid == oid


def _bot_token() -> str:
    _load_env()
    token = os.environ.get("BOT_TOKEN_OBUCHENIE", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN_OBUCHENIE не задан")
    return token


def _get_pending() -> dict[str, Any] | None:
    raw = db._connect()
    raw.execute(
        "CREATE TABLE IF NOT EXISTS live_bot_state (key TEXT PRIMARY KEY, value TEXT)"
    )
    row = raw.execute(
        "SELECT value FROM live_bot_state WHERE key=?", (PENDING_KEY,)
    ).fetchone()
    raw.commit()
    raw.close()
    if not row or not row["value"]:
        return None
    return json.loads(row["value"])


def _set_pending(payload: dict[str, Any] | None) -> None:
    conn = db._connect()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS live_bot_state (key TEXT PRIMARY KEY, value TEXT)"
    )
    if payload is None:
        conn.execute("DELETE FROM live_bot_state WHERE key=?", (PENDING_KEY,))
    else:
        conn.execute(
            "INSERT INTO live_bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (PENDING_KEY, json.dumps(payload, ensure_ascii=False)),
        )
    conn.commit()
    conn.close()


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data="live:accept"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data="live:reject"),
            ],
            [
                InlineKeyboardButton(text="✏️ Свой вариант", callback_data="live:edit"),
                InlineKeyboardButton(
                    text="🔄 Сменить категорию", callback_data="live:recat"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔁 Повтор новости", callback_data="live:dup"
                ),
                InlineKeyboardButton(
                    text="🖼 Поиск картинки", callback_data="live:img"
                ),
            ],
        ]
    )


def _category_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for arch in ARCHETYPES:
        label = CATEGORY_LABELS.get(arch, arch)
        row.append(
            InlineKeyboardButton(text=label, callback_data=f"live:setcat:{arch}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="« Назад", callback_data="live:recat_cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _effective_arch(item: dict[str, Any]) -> str:
    return item.get("archetype_override") or item.get("archetype") or "?"


def _card(item: dict[str, Any]) -> str:
    post = html.escape(item["generated"])
    fact = html.escape(item.get("fact") or "")
    source = item.get("source") or "?"
    link = f"https://t.me/{source}/{item['msg_id']}" if item.get("msg_id") else ""
    arch = _effective_arch(item)
    cat = CATEGORY_LABELS.get(arch, arch)
    strat = item.get("media_strategy") or "none"
    if item.get("media_warning") or strat == "missing":
        media_lbl = MEDIA_STRATEGY_LABELS.get("missing", "⚠ картинка не найдена")
    else:
        media_lbl = MEDIA_STRATEGY_LABELS.get(strat, strat)
    ver = html.escape(item.get("veracity") or "")
    news_id = item.get("news_id") or item.get("generated_id") or "?"
    parts = [
        f"🆔 #{news_id}",
        post,
        "",
        "───",
        f"📁 категория: {html.escape(cat)} → {html.escape(media_lbl)}",
        f"📎 источник: @{html.escape(str(source))} | {ver}",
        f"📄 факт: {fact}",
    ]
    if item.get("media_warning"):
        parts.append(f"<b>{html.escape(item['media_warning'])}</b>")
    if item.get("image_query"):
        parts.append(f"🔎 image_query: {html.escape(item['image_query'])}")
    if link:
        parts.append(f'🔗 <a href="{html.escape(link)}">исходный пост</a>')
    if item.get("guardrail_flag"):
        parts.append(f"⚠ <b>{html.escape(item['guardrail_flag'])}</b>")
    return "\n".join(parts)


def _usable_media_path(path: str | None, kind: str | None) -> bool:
    if not path or not kind:
        return False
    p = Path(path)
    if not p.is_file():
        return False
    # отсечь старые заглушки yastatic (~2–3 КБ png)
    if kind == "photo" and p.stat().st_size < 4000:
        return False
    return True


async def _send_card(bot: Bot, chat_id: int, payload: dict[str, Any]) -> None:
    # если стратегия yandex/source, но файла нет — форсируем пометку
    path = payload.get("media_path")
    kind = payload.get("media_kind")
    if payload.get("media_strategy") in ("yandex", "source", "as_is") and not _usable_media_path(
        path, kind
    ):
        payload = dict(payload)
        payload["media_strategy"] = "missing"
        payload["media_warning"] = payload.get("media_warning") or "⚠ картинка не найдена"
        payload["media_path"] = None
        log.warning(
            "send_card dropped bad media news=%s path=%s",
            payload.get("news_id"),
            path,
        )
    caption = _card(payload)
    path = payload.get("media_path")
    kind = payload.get("media_kind")
    markup = _keyboard()
    if _usable_media_path(path, kind) and kind == "photo":
        cap = caption if len(caption) <= 1000 else caption[:990] + "…"
        await bot.send_photo(
            chat_id,
            FSInputFile(path),
            caption=cap,
            reply_markup=markup,
        )
    elif _usable_media_path(path, kind) and kind == "video":
        cap = caption if len(caption) <= 1000 else caption[:990] + "…"
        await bot.send_video(
            chat_id,
            FSInputFile(path),
            caption=cap,
            reply_markup=markup,
        )
    else:
        await bot.send_message(
            chat_id,
            caption,
            reply_markup=markup,
            disable_web_page_preview=True,
        )


def _payload_from_item(item: dict[str, Any]) -> dict[str, Any]:
    arch = item.get("archetype_override") or item.get("archetype")
    import json as _json

    event = {
        "event_kind": item.get("event_kind"),
        "teams": item.get("event_teams"),
        "player": item.get("event_player"),
        "to_club": item.get("event_to_club"),
        "score": item.get("event_score"),
        "minute": item.get("event_minute"),
        "fingerprint": item.get("event_fingerprint"),
    }
    return {
        "generated_id": item["generated_id"],
        "news_id": item.get("news_id") or item["generated_id"],
        "fact_id": item["fact_id"],
        "generated": item["generated"],
        "fact": item["fact"],
        "archetype": item["archetype"],
        "archetype_override": item.get("archetype_override"),
        "veracity": item["veracity"],
        "attribution": item.get("attribution"),
        "guardrail_flag": item.get("guardrail_flag"),
        "source": item.get("source"),
        "msg_id": item.get("msg_id"),
        "source_text": item.get("source_text"),
        "media_path": item.get("media_path"),
        "media_url": item.get("media_url"),
        "media_kind": item.get("media_kind"),
        "media_strategy": item.get("media_strategy"),
        "image_query": item.get("image_query") or item.get("fact_image_query"),
        # авто-запрос фиксируем один раз при первом показе, не перезаписываем
        # при ручном 🖼 (там image_query = manual)
        "auto_image_query": item.get("auto_image_query")
        or item.get("image_query")
        or item.get("fact_image_query"),
        "media_warning": item.get("media_warning"),
        "event_json": _json.dumps(event, ensure_ascii=False)
        if not isinstance(item.get("event_json"), str)
        else item.get("event_json"),
        "effective_arch": arch,
    }


async def send_next(bot: Bot, chat_id: int, state: FSMContext | None = None) -> bool:
    if _get_pending():
        return False
    item = db.next_pending_generated()
    if not item:
        return False
    db.set_generated_status(item["generated_id"], "sent")
    payload = _payload_from_item(item)
    _set_pending(payload)
    await _send_card(bot, chat_id, payload)
    if state:
        await state.clear()
    return True


def _record(
    decision: str,
    edited_text: str | None = None,
    *,
    eval_scope: str = "skip_media",
    old_category: str | None = None,
    new_category: str | None = None,
    duplicate_of: int | None = None,
) -> None:
    p = _get_pending()
    if not p:
        raise RuntimeError("нет текущего поста")
    source = p.get("source")
    msg_id = p.get("msg_id")
    link = f"https://t.me/{source}/{msg_id}" if source and msg_id else None
    news_id = int(p.get("news_id") or p["generated_id"])
    arch = _effective_arch(p)
    db.log_live_decision(
        fact_id=int(p["fact_id"]),
        generated_id=int(p["generated_id"]),
        generated=p["generated"],
        decision=decision,
        edited_text=edited_text,
        source=source,
        source_msg_link=link,
        model=FAN_MODEL,
        eval_scope=eval_scope,
        old_category=old_category,
        new_category=new_category,
        news_id=news_id,
        duplicate_of=duplicate_of,
        raw_text=p.get("source_text"),
        fact_snapshot=p.get("fact"),
        event_json=p.get("event_json"),
        archetype_final=new_category or arch,
    )
    db.set_generated_status(int(p["generated_id"]), "decided")
    _set_pending(None)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(message.chat.id):
        if not _owner_id():
            await message.answer(f"chat_id=<code>{message.chat.id}</code>")
        return
    s = db.live_summary()
    conn = db._connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM generated_live WHERE status='pending'"
    ).fetchone()
    conn.close()
    pending_n = row["n"] if row else 0
    await message.answer(
        "Live v3.2: прогон / realtime → ЛС.\n"
        f"В логе: {s['total']} (✅{s.get('accepted',0)} ❌{s.get('rejected',0)} ✏️{s.get('edited',0)})\n"
        f"В очереди: {pending_n}\n"
        "🆔 #N в первой строке. 🔁 — разметить пропущенный дубль."
    )
    p = _get_pending()
    if p:
        await _send_card(bot, message.chat.id, p)
        return
    sent = await send_next(bot, message.chat.id, state)
    if not sent:
        await message.answer("Очередь пуста — жду новости из каналов.")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not _is_owner(message.chat.id):
        return
    s = db.live_summary()
    counts = db.raw_counts_by_source()
    lines = [
        f"live log: {s['total']} ✅{s.get('accepted',0)} ❌{s.get('rejected',0)} ✏️{s.get('edited',0)}"
    ]
    lines.append("raw_messages:")
    for src, n in counts:
        lines.append(f"  @{src}: {n}")
    r24 = db.run_24h_counts_by_source()
    if r24:
        lines.append("run_24h:")
        for src, n in r24:
            lines.append(f"  @{src}: {n}")
    await message.answer("\n".join(lines))


@router.message(Command("next"))
async def cmd_next(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(message.chat.id):
        return
    if _get_pending():
        await message.answer("Сначала реши текущий пост.")
        return
    ok = await send_next(bot, message.chat.id, state)
    if not ok:
        await message.answer("Очередь пуста.")


@router.callback_query(F.data == "live:accept")
async def on_accept(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(cb.from_user.id):
        await cb.answer()
        return
    if not _get_pending():
        await cb.answer("Уже записано", show_alert=True)
        return
    _record("accepted", eval_scope="skip_media")
    await cb.answer("Принято (skip_media)")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not await send_next(bot, cb.message.chat.id, state):
        await cb.message.answer("Очередь пуста.")


@router.callback_query(F.data == "live:reject")
async def on_reject(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(cb.from_user.id):
        await cb.answer()
        return
    if not _get_pending():
        await cb.answer("Уже записано", show_alert=True)
        return
    _record("rejected", eval_scope="skip_media")
    await cb.answer("Отклонено")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not await send_next(bot, cb.message.chat.id, state):
        await cb.message.answer("Очередь пуста.")


@router.callback_query(F.data == "live:edit")
async def on_edit(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(cb.from_user.id):
        await cb.answer()
        return
    if not _get_pending():
        await cb.answer("Уже записано", show_alert=True)
        return
    await state.set_state(Wait.edit_text)
    await cb.answer()
    await cb.message.answer("Пришли текст своего варианта одним сообщением.")


@router.message(Wait.edit_text, F.text)
async def on_edit_text(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(message.chat.id):
        return
    text = (message.text or "").strip()
    if not text or not _get_pending():
        await message.answer("Нужен текст / нет текущего поста.")
        return
    _record("edited", edited_text=text, eval_scope="voice")
    await message.answer("Свой вариант записан (eval_scope=voice).")
    if not await send_next(bot, message.chat.id, state):
        await message.answer("Очередь пуста.")


@router.message(Wait.edit_text)
async def on_edit_other(message: Message) -> None:
    if _is_owner(message.chat.id):
        await message.answer("Жду текст своего варианта.")


@router.callback_query(F.data == "live:dup")
async def on_dup(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(cb.from_user.id):
        await cb.answer()
        return
    if not _get_pending():
        await cb.answer("Нет текущего поста", show_alert=True)
        return
    await state.set_state(Wait.duplicate_id)
    await cb.answer()
    cur = _get_pending() or {}
    await cb.message.answer(
        f"🔁 Текущий пост 🆔 #{cur.get('news_id')}.\n"
        "Введи id новости, которую это повторяет (число, напр. 37):"
    )


@router.message(Wait.duplicate_id, F.text)
async def on_dup_id(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(message.chat.id):
        return
    raw = (message.text or "").strip().lstrip("#")
    if not raw.isdigit():
        await message.answer("Нужен числовой id. Попробуй ещё раз.")
        return
    oid = int(raw)
    if not db.news_id_exists(oid):
        await message.answer(f"Нет такого id #{oid}. Введи существующий.")
        return
    p = _get_pending()
    if not p:
        await message.answer("Нет текущего поста.")
        await state.clear()
        return
    if int(p.get("news_id") or 0) == oid:
        await message.answer("Нельзя указать тот же id. Введи id оригинала.")
        return
    _record("duplicate", eval_scope="skip_media", duplicate_of=oid)
    await state.clear()
    await message.answer(f"Записано: duplicate_of=#{oid}")
    if not await send_next(bot, message.chat.id, state):
        await message.answer("Очередь пуста.")


@router.message(Wait.duplicate_id)
async def on_dup_other(message: Message) -> None:
    if _is_owner(message.chat.id):
        await message.answer("Жду числовой id оригинала.")


@router.callback_query(F.data == "live:recat")
async def on_recat(cb: CallbackQuery) -> None:
    if not _is_owner(cb.from_user.id):
        await cb.answer()
        return
    if not _get_pending():
        await cb.answer("Нет текущего поста", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(
        "Выбери новую категорию — пост пересоберётся:",
        reply_markup=_category_keyboard(),
    )


@router.callback_query(F.data == "live:recat_cancel")
async def on_recat_cancel(cb: CallbackQuery) -> None:
    if not _is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer("Отмена")
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("live:setcat:"))
async def on_setcat(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(cb.from_user.id):
        await cb.answer()
        return
    p = _get_pending()
    if not p:
        await cb.answer("Нет текущего поста", show_alert=True)
        return
    new_arch = cb.data.split(":", 2)[2]
    if new_arch not in ARCHETYPES:
        await cb.answer("Неизвестная категория", show_alert=True)
        return
    old_arch = _effective_arch(p)
    await cb.answer("Пересобираю…")
    await cb.message.answer(
        f"🔄 {CATEGORY_LABELS.get(old_arch, old_arch)} → "
        f"{CATEGORY_LABELS.get(new_arch, new_arch)}…\n"
        "Это тот же пост — очередь не двигается, пока не нажмёшь ✅/❌/✏️."
    )

    # НЕ вызываем _record/_set_pending(None): иначе queue_watcher
    # подсунет следующий пост из очереди. Смена категории = замена
    # текущего слота, финальное решение — только ✅/❌/✏️/🔁.
    try:
        regen = await asyncio.to_thread(
            regenerate_for_category,
            fact_id=int(p["fact_id"]),
            new_archetype=new_arch,
        )
    except Exception:
        log.exception("regenerate failed")
        await cb.message.answer("Ошибка перегенерации — смотри лог. Текущий пост на месте.")
        return

    # старую gen-версию помечаем superseded, не decided (решение ещё впереди)
    try:
        db.set_generated_status(int(p["generated_id"]), "superseded")
    except Exception:
        pass

    # побочный лог смены категории (не закрывает слот)
    source = p.get("source")
    msg_id = p.get("msg_id")
    link = f"https://t.me/{source}/{msg_id}" if source and msg_id else None
    db.log_live_decision(
        fact_id=int(p["fact_id"]),
        generated_id=int(regen["generated_id"]),
        generated=regen["post"],
        decision="category_changed",
        edited_text=None,
        source=source,
        source_msg_link=link,
        model=FAN_MODEL,
        eval_scope="category",
        old_category=old_arch,
        new_category=new_arch,
        news_id=int(regen.get("news_id") or regen["generated_id"]),
        raw_text=p.get("source_text"),
        fact_snapshot=p.get("fact"),
        event_json=p.get("event_json"),
        archetype_final=new_arch,
    )

    item = {
        "generated_id": regen["generated_id"],
        "news_id": regen.get("news_id") or regen["generated_id"],
        "fact_id": p["fact_id"],
        "generated": regen["post"],
        "fact": p["fact"],
        "archetype": new_arch,
        "archetype_override": new_arch,
        "veracity": p.get("veracity"),
        "attribution": p.get("attribution"),
        "guardrail_flag": None,
        "source": p.get("source"),
        "msg_id": p.get("msg_id"),
        "source_text": p.get("source_text"),
        "event_json": p.get("event_json"),
        "auto_image_query": p.get("auto_image_query"),
        **{
            k: regen["media"].get(k)
            for k in (
                "media_path",
                "media_url",
                "media_kind",
                "media_strategy",
                "image_query",
                "media_warning",
            )
        },
    }
    db.set_generated_status(int(regen["generated_id"]), "sent")
    payload = _payload_from_item(item)
    # pending НЕ обнуляли — сразу атомарно ставим новую версию того же слота
    _set_pending(payload)
    await _send_card(bot, cb.message.chat.id, payload)
    if state:
        await state.clear()


@router.callback_query(F.data == "live:img")
async def on_img(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(cb.from_user.id):
        await cb.answer()
        return
    if not _get_pending():
        await cb.answer("Нет текущего поста", show_alert=True)
        return
    await state.set_state(Wait.image_query)
    await cb.answer()
    await cb.message.answer("Введи свой запрос для поиска картинки")


@router.message(Wait.image_query, F.text)
async def on_img_query(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(message.chat.id):
        return
    query = (message.text or "").strip()
    p = _get_pending()
    if not query or not p:
        await message.answer("Нужен текст запроса / нет текущего поста.")
        return

    # Как смена категории: не трогаем pending/очередь до ✅/❌/✏️/🔁
    await message.answer(
        f"🖼 Ищу картинку: «{html.escape(query)}»…\n"
        "Текст поста не меняется, очередь на месте."
    )
    visible_news_id = int(p.get("news_id") or p["generated_id"])
    auto_q = p.get("auto_image_query") or p.get("image_query")
    arch = _effective_arch(p)

    try:
        regen = await asyncio.to_thread(
            replace_media_manual_query,
            fact_id=int(p["fact_id"]),
            post_text=p["generated"],
            manual_query=query,
            archetype=arch if arch != "?" else None,
            visible_news_id=visible_news_id,
        )
    except Exception:
        log.exception("manual image search failed")
        await message.answer("Ошибка поиска — смотри лог. Текущий пост на месте.")
        return

    try:
        db.set_generated_status(int(p["generated_id"]), "superseded")
    except Exception:
        pass

    source = p.get("source")
    msg_id = p.get("msg_id")
    link = f"https://t.me/{source}/{msg_id}" if source and msg_id else None
    db.log_live_decision(
        fact_id=int(p["fact_id"]),
        generated_id=int(regen["generated_id"]),
        generated=p["generated"],
        decision="image_search_manual",
        edited_text=None,
        source=source,
        source_msg_link=link,
        model=FAN_MODEL,
        eval_scope="image",
        news_id=visible_news_id,
        raw_text=p.get("source_text"),
        fact_snapshot=p.get("fact"),
        event_json=p.get("event_json"),
        archetype_final=arch if arch != "?" else None,
        auto_image_query=auto_q,
        manual_image_query=query,
    )

    item = {
        "generated_id": regen["generated_id"],
        "news_id": visible_news_id,
        "fact_id": p["fact_id"],
        "generated": p["generated"],
        "fact": p["fact"],
        "archetype": p.get("archetype"),
        "archetype_override": p.get("archetype_override"),
        "veracity": p.get("veracity"),
        "attribution": p.get("attribution"),
        "guardrail_flag": p.get("guardrail_flag"),
        "source": p.get("source"),
        "msg_id": p.get("msg_id"),
        "source_text": p.get("source_text"),
        "event_json": p.get("event_json"),
        "auto_image_query": auto_q,
        **{
            k: regen["media"].get(k)
            for k in (
                "media_path",
                "media_url",
                "media_kind",
                "media_strategy",
                "image_query",
                "media_warning",
            )
        },
    }
    db.set_generated_status(int(regen["generated_id"]), "sent")
    payload = _payload_from_item(item)
    _set_pending(payload)
    await state.clear()
    await _send_card(bot, message.chat.id, payload)


@router.message(Wait.image_query)
async def on_img_other(message: Message) -> None:
    if _is_owner(message.chat.id):
        await message.answer("Жду текстовый запрос для поиска картинки.")


async def queue_watcher(bot: Bot) -> None:
    """Шлёт следующий пост ТОЛЬКО если нет активного решения."""
    while True:
        try:
            oid = _owner_id()
            # строгий гейт: пока есть pending — ничего из очереди
            if oid and not _get_pending():
                await send_next(bot, oid)
        except Exception:
            log.exception("queue_watcher")
        await asyncio.sleep(5)


def build_bot() -> tuple[Bot, Dispatcher]:
    _load_env()
    proxy = (
        os.environ.get("TELEGRAM_PROXY", "").strip()
        or os.environ.get("SCRAPER_HTTP_PROXY", "").strip()
        or os.environ.get("OPENAI_HTTP_PROXY", "").strip()
        or None
    )
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(
        token=_bot_token(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp


async def run_bot_only() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    bot, dp = build_bot()
    asyncio.create_task(queue_watcher(bot))
    log.info("live bot polling, owner=%s db=%s", _owner_id(), ROOT / "data" / "ingest.db")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run_bot_only())
