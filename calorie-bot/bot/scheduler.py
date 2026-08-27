from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.config import Settings
from bot.db import Database
from bot.formatters import format_day_summary, format_picooc_measurement
from bot.picooc import PicoocClient, PicoocMeasurement

log = logging.getLogger(__name__)


async def sync_picooc(
    bot: Bot,
    settings: Settings,
    db: Database,
    *,
    notify: bool = True,
    limit_notify: int = 3,
) -> list[PicoocMeasurement]:
    if not settings.picooc_email or not settings.picooc_password:
        return []

    proxy = settings.picooc_proxy or settings.telegram_http_proxy or None
    client = PicoocClient(
        settings.picooc_email,
        settings.picooc_password,
        proxy=proxy,
        role_name=settings.picooc_role_name or "",
    )
    measurements = await client.fetch_measurements()
    new_ones: list[PicoocMeasurement] = []
    for m in measurements:
        added = await db.add_body_measurement(
            external_id=m.external_id,
            weighed_at=m.weighed_at,
            weight_kg=m.weight_kg,
            body_fat=m.body_fat,
            bmi=m.bmi,
            visceral_fat=m.visceral_fat,
            muscle_pct=m.muscle_pct,
            body_age=m.body_age,
            bone_mass=m.bone_mass,
            bmr=m.bmr,
            water_pct=m.water_pct,
            skeletal_muscle=m.skeletal_muscle,
            subcutaneous_fat=m.subcutaneous_fat,
            source="picooc",
            raw=json.dumps(
                {
                    "mac": m.mac,
                    "role": m.role_name,
                },
                ensure_ascii=False,
            ),
        )
        if added:
            new_ones.append(m)

    if new_ones:
        log.info("Picooc sync: %s new measurement(s)", len(new_ones))
        if notify:
            for m in new_ones[-limit_notify:]:
                try:
                    await bot.send_message(
                        settings.allowed_chat_id,
                        format_picooc_measurement(m, settings),
                    )
                except Exception:
                    log.exception("Failed to notify about Picooc measurement")
    else:
        log.info("Picooc sync: no new measurements (%s total)", len(measurements))
    return new_ones


def setup_scheduler(bot: Bot, settings: Settings, db: Database) -> AsyncIOScheduler:
    tz = ZoneInfo(settings.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)

    async def picooc_job() -> None:
        try:
            await sync_picooc(bot, settings, db, notify=True)
        except Exception:
            log.exception("Picooc sync failed")

    async def morning_reminder() -> None:
        day = datetime.now(tz).date()
        try:
            if await db.weight_on_day(day) is not None:
                return
            await bot.send_message(
                settings.allowed_chat_id,
                "☀️ Доброе утро! Вес ещё не записан.\n"
                "Встань на Picooc или напиши <code>Вес … кг</code>",
            )
        except Exception:
            log.exception("Morning reminder failed")

    async def evening_reminder() -> None:
        day = datetime.now(tz).date()
        try:
            if not await db.has_meals_on(day):
                await bot.send_message(
                    settings.allowed_chat_id,
                    "🌙 За сегодня еда ещё не записана.\n"
                    "Можно голосом, текстом («съел …») или <code>как вчера</code>.\n"
                    "Итоги — напиши <code>как день</code>, когда появятся записи.",
                )
                return
            text = await format_day_summary(
                db,
                settings,
                day,
                title=f"🌙 Итоги · {day.strftime('%d.%m.%Y')}",
            )
            await bot.send_message(settings.allowed_chat_id, text)
            log.info("Evening wrap-up sent for %s", day)
        except Exception:
            log.exception("Evening reminder failed")

    # Старый midnight-итог отключаем (дублировал вечерний)
    try:
        scheduler.remove_job("daily_summary")
    except Exception:
        pass

    scheduler.add_job(
        morning_reminder,
        trigger="cron",
        hour=settings.reminder_morning_hour,
        minute=0,
        id="morning_reminder",
        replace_existing=True,
    )
    scheduler.add_job(
        evening_reminder,
        trigger="cron",
        hour=settings.reminder_evening_hour,
        minute=0,
        id="evening_reminder",
        replace_existing=True,
    )

    if settings.picooc_email and settings.picooc_password:
        minutes = max(1, int(settings.picooc_sync_minutes or 5))
        scheduler.add_job(
            picooc_job,
            trigger="interval",
            minutes=minutes,
            id="picooc_sync",
            replace_existing=True,
            next_run_time=datetime.now(tz) + timedelta(seconds=15),
        )
        log.info("Picooc sync enabled every %s min", minutes)

    log.info(
        "Reminders: morning %02d:00, evening %02d:00",
        settings.reminder_morning_hour,
        settings.reminder_evening_hour,
    )
    return scheduler


async def send_summary_now(
    bot: Bot, settings: Settings, db: Database, day: date | None = None
) -> None:
    tz = ZoneInfo(settings.timezone)
    target = day or datetime.now(tz).date()
    text = await format_day_summary(db, settings, target)
    await bot.send_message(settings.allowed_chat_id, text)
