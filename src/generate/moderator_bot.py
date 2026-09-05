"""Telegram-бот калибровки: Terra → 1 пост → ✅/❌/✏️ → лог. Не публикация."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.config import FAN_MODEL, ROOT, _load_env
from src.generate.calibration import (
    ACTIVE_RUN,
    decided_eval_ids,
    get_state,
    log_decision,
    set_state,
    summary,
)
from src.generate.eval_facts_run2 import EVAL_FACTS_RUN2
from src.generate.fan import generate_single

log = logging.getLogger("moderator_bot")
router = Router()

FACTS = EVAL_FACTS_RUN2
PENDING_KEY = f"pending_run{ACTIVE_RUN}"


class Wait(StatesGroup):
    edit_text = State()


def _owner_id() -> int:
    _load_env()
    raw = os.environ.get("OWNER_CHAT_ID", "").strip()
    if not raw:
        return 0
    return int(raw)


def _is_owner(chat_id: int) -> bool:
    oid = _owner_id()
    return bool(oid) and chat_id == oid


def _bot_token() -> str:
    _load_env()
    token = os.environ.get("BOT_TOKEN_OBUCHENIE", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN_OBUCHENIE не задан в .env")
    return token


def _pending() -> dict[str, Any] | None:
    raw = get_state(PENDING_KEY)
    if not raw:
        return None
    return json.loads(raw)


def _save_pending(payload: dict[str, Any] | None) -> None:
    set_state(
        PENDING_KEY,
        None if payload is None else json.dumps(payload, ensure_ascii=False),
    )


def _queue() -> list[dict]:
    done = decided_eval_ids()
    return [f for f in FACTS if f["id"] not in done]


def _progress_label() -> str:
    done = len(decided_eval_ids())
    return f"{done + 1}/{len(FACTS)}" if done < len(FACTS) else f"{done}/{len(FACTS)}"


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data="cal:accept"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data="cal:reject"),
                InlineKeyboardButton(text="✏️ Свой вариант", callback_data="cal:edit"),
            ]
        ]
    )


def _card_html(item: dict, post: str, flags: list[str]) -> str:
    fact = html.escape(item["fact"])
    body = html.escape(post)
    meta = (
        f"#{item['id']} · {html.escape(item['archetype'])} · "
        f"{html.escape(item['veracity'])} · sensation={item['is_sensation']}"
    )
    parts = [
        f"<b>{html.escape(_progress_label())}</b>  <code>{html.escape(FAN_MODEL)}</code>",
        "",
        body,
        "",
        f"<i>{html.escape(meta)}</i>",
        f"<i>факт: {fact}</i>",
    ]
    if flags:
        note = " · ".join(flags)
        parts.append(f"⚠ <b>{html.escape(note)}</b>")
    return "\n".join(parts)


async def _generate(item: dict) -> tuple[str, list[str]]:
    result = await asyncio.to_thread(
        generate_single,
        item["fact"],
        item["veracity"],
        item["archetype"],
        item["is_sensation"],
        note=item.get("note"),
    )
    return result["post"], result["flags"]


async def _send_card(bot: Bot, chat_id: int, item: dict, post: str, flags: list[str]) -> None:
    await bot.send_message(
        chat_id,
        _card_html(item, post, flags),
        reply_markup=_keyboard(),
        disable_web_page_preview=True,
    )


async def _advance(bot: Bot, chat_id: int, state: FSMContext) -> None:
    await state.clear()
    left = _queue()
    if not left:
        _save_pending(None)
        s = summary()
        run1 = summary(run=1)
        await bot.send_message(
            chat_id,
            "Готово, run{run}: {n} решений записано.\n"
            "✅ принято: {a}\n❌ отклонено: {r}\n✏️ свой вариант: {e}\n\n"
            "Для сравнения run1: ✅{a1} ❌{r1} ✏️{e1} "
            "(доля правок {p1:.0%} → сейчас {p2:.0%})".format(
                run=ACTIVE_RUN,
                n=s["total"],
                a=s.get("accepted", 0),
                r=s.get("rejected", 0),
                e=s.get("edited", 0),
                a1=run1.get("accepted", 0),
                r1=run1.get("rejected", 0),
                e1=run1.get("edited", 0),
                p1=(run1.get("edited", 0) / run1["total"]) if run1["total"] else 0,
                p2=(s.get("edited", 0) / s["total"]) if s["total"] else 0,
            ),
        )
        return

    item = left[0]
    wait = await bot.send_message(
        chat_id, f"⏳ Генерирую пост Terra (run{ACTIVE_RUN}, калибровка)…"
    )
    try:
        post, flags = await _generate(item)
    except Exception as exc:  # noqa: BLE001
        log.exception("generate failed")
        await wait.edit_text(f"Ошибка генерации: {html.escape(str(exc))}\nНажми /next")
        return
    await wait.delete()
    _save_pending(
        {
            "eval_id": item["id"],
            "post": post,
            "flags": flags,
        }
    )
    await _send_card(bot, chat_id, item, post, flags)


def _item_by_id(eval_id: int) -> dict:
    for f in FACTS:
        if f["id"] == eval_id:
            return f
    raise KeyError(eval_id)


def _record(decision: str, edited_text: str | None = None) -> None:
    pending = _pending()
    if not pending:
        raise RuntimeError("нет текущего поста")
    item = _item_by_id(int(pending["eval_id"]))
    flags = pending.get("flags") or []
    log_decision(
        eval_id=item["id"],
        fact=item["fact"],
        archetype=item["archetype"],
        veracity=item["veracity"],
        is_sensation=item["is_sensation"],
        generated=pending["post"],
        guardrail_flag=" | ".join(flags) if flags else None,
        decision=decision,
        edited_text=edited_text,
    )
    _save_pending(None)


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    await message.answer(f"chat_id = <code>{message.chat.id}</code>")


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _owner_id():
        await message.answer(
            "OWNER_CHAT_ID не задан в .env.\n"
            f"Твой chat_id: <code>{message.chat.id}</code>\n"
            "Пропиши его и перезапусти бота."
        )
        return
    if not _is_owner(message.chat.id):
        return

    pending = _pending()
    if pending:
        item = _item_by_id(int(pending["eval_id"]))
        await _send_card(bot, message.chat.id, item, pending["post"], pending.get("flags") or [])
        await message.answer("Продолжаем с текущего поста — решение ещё не записано.")
        return

    left = _queue()
    await message.answer(
        f"Run{ACTIVE_RUN} — калибровка Terra на новых фактах.\n"
        f"Прогресс: {len(FACTS) - len(left)}/{len(FACTS)} в логе "
        f"<code>calibration_log_run{ACTIVE_RUN}</code>.\n"
        "По одному. ✅ принять / ❌ отклонить / ✏️ свой вариант.\n"
        "Тренировка, в канал не уходит."
    )
    await _advance(bot, message.chat.id, state)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not _is_owner(message.chat.id):
        return
    s = summary()
    left = len(_queue())
    await message.answer(
        f"Run{ACTIVE_RUN} лог: {s['total']}/{len(FACTS)}\n"
        f"✅ {s.get('accepted', 0)} · ❌ {s.get('rejected', 0)} · ✏️ {s.get('edited', 0)}\n"
        f"Осталось: {left}"
    )


@router.message(Command("next"))
async def cmd_next(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(message.chat.id):
        return
    if _pending():
        await message.answer("Сначала реши текущий пост.")
        return
    await _advance(bot, message.chat.id, state)


@router.callback_query(F.data == "cal:accept")
async def on_accept(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(cb.from_user.id):
        await cb.answer()
        return
    if not _pending():
        await cb.answer("Уже записано", show_alert=True)
        return
    _record("accepted")
    await cb.answer("Принято")
    await cb.message.edit_reply_markup(reply_markup=None)
    await _advance(bot, cb.message.chat.id, state)


@router.callback_query(F.data == "cal:reject")
async def on_reject(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(cb.from_user.id):
        await cb.answer()
        return
    if not _pending():
        await cb.answer("Уже записано", show_alert=True)
        return
    _record("rejected")
    await cb.answer("Отклонено")
    await cb.message.edit_reply_markup(reply_markup=None)
    await _advance(bot, cb.message.chat.id, state)


@router.callback_query(F.data == "cal:edit")
async def on_edit(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(cb.from_user.id):
        await cb.answer()
        return
    if not _pending():
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
    if not text:
        await message.answer("Нужен текст поста.")
        return
    if not _pending():
        await state.clear()
        await message.answer("Текущего поста уже нет.")
        return
    _record("edited", edited_text=text)
    await message.answer("Свой вариант записан.")
    await _advance(bot, message.chat.id, state)


@router.message(Wait.edit_text)
async def on_edit_not_text(message: Message) -> None:
    if not _is_owner(message.chat.id):
        return
    await message.answer("Жду текст своего варианта (не кнопку и не файл).")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _load_env()
    token = _bot_token()
    proxy = (
        os.environ.get("TELEGRAM_PROXY", "").strip()
        or os.environ.get("SCRAPER_HTTP_PROXY", "").strip()
        or os.environ.get("OPENAI_HTTP_PROXY", "").strip()
        or None
    )
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    owner = _owner_id()
    log.info(
        "moderator bot polling, run=%s facts=%s owner_chat_id=%s db=%s table=%s",
        ACTIVE_RUN,
        len(FACTS),
        owner or "(не задан)",
        ROOT / "data" / "calibration.db",
        f"calibration_log_run{ACTIVE_RUN}" if ACTIVE_RUN != 1 else "calibration_log",
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
