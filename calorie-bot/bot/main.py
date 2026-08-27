from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeDefault

# allow `python -m bot.main` and `python bot/main.py`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import get_settings
from bot.db import Database
from bot.handlers import build_router
from bot.scheduler import setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("calorie-bot")


async def main() -> None:
    settings = get_settings()
    db = Database(settings.database_path)
    await db.connect()

    session = None
    if settings.telegram_http_proxy:
        session = AiohttpSession(proxy=settings.telegram_http_proxy)

    bot = Bot(
        token=settings.telegram_bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    from aiogram import BaseMiddleware, Dispatcher
    from aiogram.types import TelegramObject
    from typing import Any, Awaitable, Callable

    class LogUpdatesMiddleware(BaseMiddleware):
        async def __call__(
            self,
            handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: dict[str, Any],
        ) -> Any:
            # Update middleware видит целый Update; message-handlers — Message.
            cq = getattr(event, "callback_query", None)
            msg = getattr(event, "message", None) or getattr(event, "edited_message", None)
            if cq is not None:
                chat = getattr(getattr(cq, "message", None), "chat", None)
                log.info(
                    "Update: type=CallbackQuery chat=%s data=%s",
                    getattr(chat, "id", None),
                    getattr(cq, "data", None),
                )
            else:
                src = msg or event
                chat = getattr(src, "chat", None)
                log.info(
                    "Update: type=%s chat=%s has_text=%s has_photo=%s",
                    type(src).__name__,
                    getattr(chat, "id", None),
                    bool(getattr(src, "text", None)),
                    bool(getattr(src, "photo", None)),
                )
            return await handler(event, data)

    dp = Dispatcher()
    dp.update.middleware(LogUpdatesMiddleware())
    dp.include_router(build_router(settings, db))

    commands = [
        BotCommand(command="day", description="Итог за сегодня"),
        BotCommand(command="goals", description="Цели и остаток"),
        BotCommand(command="sync", description="Синк Picooc"),
        BotCommand(command="undo", description="Удалить последнюю запись"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="cancel", description="Отменить распознавание фото"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())

    scheduler = setup_scheduler(bot, settings, db)
    scheduler.start()
    log.info(
        "Bot started. chat_id=%s tz=%s db=%s",
        settings.allowed_chat_id,
        settings.timezone,
        settings.database_path,
    )

    try:
        me = await bot.get_me()
        log.info(
            "Logged in as @%s (%s), can_read_all_group_messages=%s",
            me.username,
            me.id,
            me.can_read_all_group_messages,
        )
        await dp.start_polling(
            bot,
            allowed_updates=["message", "edited_message", "callback_query"],
        )
    finally:
        scheduler.shutdown(wait=False)
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
