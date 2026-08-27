from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.activity import looks_like_activity, parse_activity, parse_factor_override
from bot.config import Settings
from bot.copy_yesterday import copy_yesterday_meals, infer_meal_slot, parse_copy_yesterday
from bot.db import Database
from bot.formatters import (
    HELP_TEXT,
    day_energy,
    format_day_summary,
    format_label_preview,
    format_meal_line,
)
from bot.goals import (
    apply_goal_updates,
    calc_goal_progress,
    format_goals_block,
    load_goals,
    parse_goal_command,
    save_protein_goal,
    save_target_weight,
)
from bot.nutrition import fmt_num
from bot.ocr import (
    LabelData,
    analyze_album,
    label_has_macros,
    label_ready_to_save,
    labels_complementary,
    likely_same_product,
    merge_labels,
    needs_package_weight,
    sanitize_label,
    scale_label_to_meal,
)
from bot.prep import (
    build_prep_name,
    format_prep_created,
    ingredient_meal_from_label,
    looks_like_eat_from_prep,
    parse_eat_servings,
    parse_prep_servings,
)
from bot.parsers import (
    ParsedMeal,
    extract_activity_name_from_bot_message,
    extract_meal_name_from_bot_message,
    is_delete_request,
    is_repeat_request,
    looks_like_food,
    parse_meal,
    parse_meal_regex,
    parse_portion_answer,
    parse_weight,
)
from bot.scheduler import sync_picooc
from bot.voice import transcribe_telegram_voice

log = logging.getLogger("calorie-bot.handlers")

SLOT_LABELS = {
    "breakfast": "завтрак",
    "lunch": "обед",
    "dinner": "ужин",
    "snack": "перекус",
    "all": "весь день",
}

# Альбомы Telegram: media_group_id → буфер фото
_album_buffers: dict[str, dict] = {}
# Отмена текущего распознавания: scan_id → True
_scan_cancelled: set[str] = set()
ALBUM_WAIT_SEC = 1.8
# Окно склейки двух отдельных фото одного продукта
MERGE_WINDOW_SEC = 120


def _kb_cancel_meal(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Отменить",
                    callback_data=f"cancel_meal:{meal_id}",
                )
            ]
        ]
    )


def _kb_cancel_prep(prep_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Отменить заготовку",
                    callback_data=f"cancel_prep:{prep_id}",
                )
            ]
        ]
    )


def _kb_cancel_scan(scan_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data=f"cancel_scan:{scan_id}",
                )
            ]
        ]
    )


def _kb_cancel_pending() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data="cancel_pending",
                )
            ]
        ]
    )


def _meal_as_prep_label(meal: ParsedMeal) -> LabelData:
    """Текстовый ингредиент → этикетка «порция целиком» для заготовки."""
    return LabelData(
        name=meal.name,
        kcal=float(meal.kcal or 0),
        protein=float(meal.protein or 0),
        fat=float(meal.fat or 0),
        carbs=float(meal.carbs or 0),
        per="portion",
        package_amount=meal.amount,
        package_unit=meal.amount_unit or ("г" if meal.amount else None),
    )


# Типовые КБЖУ на 100 г — если пользователь дописал «макароны 450 г» без этикетки
_STAPLE_PER_100G: list[tuple[re.Pattern[str], str, float, float, float, float]] = [
    (re.compile(r"(?i)спагетт|макарон|паста|barilla|pasta"), "Макароны", 350, 12, 1.5, 72),
    (re.compile(r"(?i)рис\b"), "Рис", 340, 7, 1, 78),
    (re.compile(r"(?i)гречк"), "Гречка", 330, 12.6, 3.3, 62),
    (re.compile(r"(?i)овсян|геркулес"), "Овсянка", 370, 13, 6.5, 65),
]


def _estimate_staple_for_prep(text: str) -> ParsedMeal | None:
    t = text.strip()
    m = re.search(
        r"(\d+[.,]\d+|\d+)\s*(г|гр|грамм[аыу]?|кг)?",
        t.lower().replace("ё", "е"),
    )
    if not m:
        return None
    amount = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "г").lower()
    if unit in {"гр", "грамм", "грамма", "граммы", "грамму"}:
        unit = "г"
    if unit == "кг":
        amount *= 1000
        unit = "г"
    if amount <= 0 or amount > 5000:
        return None
    for pat, name, kcal100, p100, f100, c100 in _STAPLE_PER_100G:
        if pat.search(t):
            factor = amount / 100.0
            return ParsedMeal(
                name=name,
                kcal=round(kcal100 * factor, 1),
                protein=round(p100 * factor, 1),
                fat=round(f100 * factor, 1),
                carbs=round(c100 * factor, 1),
                amount=amount,
                amount_unit=unit,
            )
    return None


def build_router(settings: Settings, db: Database) -> Router:
    router = Router(name="calorie")
    tz = ZoneInfo(settings.timezone)

    def today() -> date:
        return datetime.now(tz).date()

    def allowed(message: Message) -> bool:
        ok = bool(message.chat and message.chat.id == settings.allowed_chat_id)
        if message.chat and not ok:
            log.info(
                "Ignore chat_id=%s (allowed=%s) type=%s",
                message.chat.id,
                settings.allowed_chat_id,
                message.chat.type,
            )
        return ok

    async def progress_extra(day: date) -> str:
        totals = await db.day_totals(day)
        weight = await db.latest_weight()
        goals = await load_goals(db)
        if weight is None:
            return f"\n\nСегодня: {fmt_num(totals.kcal)} ккал · Б {fmt_num(totals.protein)} г"
        needs = await day_energy(db, settings, day, weight)
        progress = calc_goal_progress(
            goals, needs=needs, totals=totals, current_weight=weight
        )
        lines = [""] + format_goals_block(progress)
        return "\n".join(lines)

    def _meal_from_label(
        label: LabelData, caption: str | None = None
    ):
        """По умолчанию — вся упаковка; подпись может задать другую порцию."""
        if caption:
            amount, unit, full = parse_portion_answer(caption)
            if full:
                return scale_label_to_meal(label, use_full_package=True)
            if amount is not None:
                return scale_label_to_meal(
                    label, amount=amount, amount_unit=unit, use_full_package=False
                )
        return scale_label_to_meal(label, use_full_package=True)

    async def _ask_package_or_wait(message: Message, label: LabelData) -> None:
        payload = label.to_dict()
        payload["_waiting_more"] = True
        payload["_ts"] = time.time()
        await db.set_pending(message.chat.id, payload)
        await message.reply(
            f"{format_label_preview(label)}\n\n"
            "КБЖУ есть, но не вижу массу упаковки.\n"
            "Скинь второе фото (нетто) или напиши граммы/мл "
            "(например <code>230 г</code>).\n"
            "По умолчанию записываю <b>всю упаковку</b>.",
            reply_markup=_kb_cancel_pending(),
        )

    async def _finalize_label(
        message: Message,
        label: LabelData,
        *,
        caption: str | None = None,
        day: date | None = None,
    ) -> None:
        day = day or today()
        caption = (caption or "").strip() or None

        clean = sanitize_label(label)
        if clean is None:
            await message.reply(
                "Не уверен в цифрах с этикетки (похоже на ошибку распознавания).\n"
                "Пришли фото пищевой ценности поближе или КБЖУ текстом, например:\n"
                "<code>наггетсы 280 ккал Б14 Ж16 У20 на 100г, съел 192г</code>"
            )
            return
        label = clean

        if caption:
            amount, unit, full = parse_portion_answer(caption)
            if full or amount is not None:
                if needs_package_weight(label) and amount is not None and unit in {"г", "мл", "л"}:
                    # Подпись задаёт съеденное = массу для пересчёта с 100г
                    meal = scale_label_to_meal(
                        label, amount=amount, amount_unit=unit, use_full_package=False
                    )
                else:
                    meal = _meal_from_label(label, caption)
                await db.clear_pending(message.chat.id)
                await _save_meal(
                    message, db, meal, source="photo", raw=caption, day=day
                )
                return

        if label_ready_to_save(label):
            meal = _meal_from_label(label, caption)
            await db.clear_pending(message.chat.id)
            note = ""
            if label.package_amount:
                note = (
                    f"\n📦 Вся упаковка: {fmt_num(label.package_amount)} "
                    f"{label.package_unit or ''}".strip()
                )
            await _save_meal(message, db, meal, source="photo", day=day, extra_note=note)
            return

        if label_has_macros(label) and needs_package_weight(label):
            await _ask_package_or_wait(message, label)
            return

        await message.reply(
            "Не смог разобрать этикетку. Пришли фото поближе, второе фото с массой "
            "или текст с КБЖУ."
        )

    async def _process_photo_images(
        message: Message,
        images: list[bytes],
        *,
        caption: str | None = None,
        status: Message | None = None,
        scan_id: str | None = None,
    ) -> None:
        if scan_id and scan_id in _scan_cancelled:
            _scan_cancelled.discard(scan_id)
            if status:
                try:
                    await status.edit_text("✖️ Распознавание отменено.")
                except Exception:
                    pass
            return

        try:
            products = await analyze_album(images, settings)
        except Exception:
            log.exception("Photo OCR failed")
            err = "Ошибка при разборе фото. Попробуй ещё раз или пришли КБЖУ текстом."
            if status:
                await status.edit_text(err)
            else:
                await message.reply(err)
            return

        if scan_id and scan_id in _scan_cancelled:
            _scan_cancelled.discard(scan_id)
            if status:
                try:
                    await status.edit_text("✖️ Распознавание отменено.")
                except Exception:
                    pass
            return

        if not products:
            text = (
                "Не смог разобрать этикетку. Пришли фото поближе, второе фото "
                "с массой или текст с КБЖУ."
            )
            if status:
                await status.edit_text(text)
            else:
                await message.reply(text)
            return

        # Одно фото: можно склеить с ожидающим pending
        if len(images) == 1 and len(products) == 1:
            label = products[0]
            pending = await db.get_pending(message.chat.id)
            if pending and pending.get("_waiting_more"):
                ts = float(pending.get("_ts") or 0)
                if time.time() - ts <= MERGE_WINDOW_SEC:
                    prev = LabelData.from_dict(pending)
                    if likely_same_product(prev, label):
                        label = merge_labels(prev, label)
                        await db.clear_pending(message.chat.id)
                        products = [label]
                    elif label_ready_to_save(label):
                        await db.clear_pending(message.chat.id)
                        if label_ready_to_save(prev):
                            if status:
                                try:
                                    await status.delete()
                                except Exception:
                                    pass
                                status = None
                            await _finalize_label(message, prev, caption=None)
                    elif labels_complementary(prev, label):
                        label = merge_labels(prev, label)
                        await db.clear_pending(message.chat.id)
                        products = [label]
                else:
                    await db.clear_pending(message.chat.id)

        if scan_id and scan_id in _scan_cancelled:
            _scan_cancelled.discard(scan_id)
            if status:
                try:
                    await status.edit_text("✖️ Распознавание отменено.")
                except Exception:
                    pass
            return

        if status:
            try:
                await status.delete()
            except Exception:
                pass

        # Заготовка из ингредиентов (подпись: «на 4 порции» и т.п.)
        prep_draft = parse_prep_servings(caption) if caption else None
        if prep_draft is not None:
            await _handle_prep_from_products(
                message,
                products,
                servings=prep_draft.servings,
                name_hint=prep_draft.name_hint,
                raw_caption=caption,
                photos_count=len(images),
            )
            return

        # Обычная еда с этикетки
        cap = caption if len(products) == 1 else None
        incomplete: list[LabelData] = []
        for label in products:
            if label_ready_to_save(label) or (
                cap and parse_portion_answer(cap)[0] is not None
            ) or (cap and parse_portion_answer(cap)[2]):
                await _finalize_label(message, label, caption=cap)
            elif label_has_macros(label) and needs_package_weight(label):
                incomplete.append(label)
            else:
                await message.reply(
                    f"Не разобрал: <b>{label.name}</b>. "
                    "Пришли фото поближе или КБЖУ текстом."
                )

        if not incomplete:
            return
        if len(incomplete) == 1:
            await _ask_package_or_wait(message, incomplete[0])
            return

        lines = [
            f"Распознал <b>{len(incomplete)}</b> продукта без массы упаковки:"
        ]
        for i, lab in enumerate(incomplete, 1):
            unit = "г" if lab.per == "100g" else "мл"
            lines.append(
                f"{i}. {lab.name} — {fmt_num(lab.kcal)} ккал/100{unit}"
            )
        lines.append(
            "Скинь фото с нетто по очереди или напиши массу для первого "
            f"(<b>{incomplete[0].name}</b>), например <code>250 г</code>."
        )
        payload = incomplete[0].to_dict()
        payload["_waiting_more"] = True
        payload["_ts"] = time.time()
        await db.set_pending(message.chat.id, payload)
        await message.reply("\n".join(lines))

    async def _handle_prep_from_products(
        message: Message,
        products: list,
        *,
        servings: float,
        name_hint: str | None,
        raw_caption: str | None,
        photos_count: int | None = None,
    ) -> None:
        ready: list = []
        need_weight: list = []
        for label in products:
            clean = sanitize_label(label) if label else None
            if clean is None or not label_has_macros(clean):
                continue
            if needs_package_weight(clean) and not clean.package_amount:
                need_weight.append(clean)
            else:
                ready.append(clean)

        if not ready and not need_weight:
            await message.reply(
                "Не разобрал ингредиенты. Пришли этикетки с КБЖУ поближе "
                "(текст можно боком — попробую повернуть)."
            )
            return

        all_labels = ready + need_weight
        missing_photos = bool(photos_count and photos_count > len(all_labels))

        # Не хватает кадров — НЕ сохраняем сразу, ждём дописку текстом
        if missing_photos:
            await db.set_pending(
                message.chat.id,
                {
                    "_prep_wait_missing": True,
                    "_ts": time.time(),
                    "labels": [x.to_dict() for x in all_labels],
                    "servings": servings if servings and servings >= 1 else None,
                    "name_hint": name_hint,
                    "raw": raw_caption,
                    "photos_count": photos_count,
                    "found_count": len(all_labels),
                },
            )
            names = ", ".join(f"<b>{x.name}</b>" for x in all_labels) or "—"
            await message.reply(
                f"С {photos_count} фото разобрал только <b>{len(all_labels)}</b>: "
                f"{names}.\n"
                "Допиши пропущенное, например: <code>макароны 450 г</code>\n"
                "Или <code>только это</code> — сохранить без него.",
                reply_markup=_kb_cancel_pending(),
            )
            return

        if need_weight:
            await db.set_pending(
                message.chat.id,
                {
                    "_prep_wait_weight": True,
                    "_ts": time.time(),
                    "labels": [x.to_dict() for x in all_labels],
                    "wait_name": need_weight[0].name,
                    "servings": servings if servings and servings >= 1 else None,
                    "name_hint": name_hint,
                    "raw": raw_caption,
                },
            )
            names = ", ".join(f"<b>{x.name}</b>" for x in all_labels)
            await message.reply(
                f"Вижу ингредиенты: {names}.\n"
                f"Сколько грамм <b>{need_weight[0].name}</b> ушло в кастрюлю?\n"
                "Например: <code>200 г</code>.",
                reply_markup=_kb_cancel_pending(),
            )
            return

        if not servings or servings < 1:
            await db.set_pending(
                message.chat.id,
                {
                    "_prep_wait_servings": True,
                    "_ts": time.time(),
                    "labels": [x.to_dict() for x in ready],
                    "name_hint": name_hint,
                    "raw": raw_caption,
                },
            )
            await message.reply(
                f"Вижу <b>{len(ready)}</b> ингредиент(ов) как заготовку.\n"
                "На сколько порций? Например: <code>4 порции</code> или <code>на 4</code>.",
                reply_markup=_kb_cancel_pending(),
            )
            return

        await _save_prep(
            message,
            ready,
            servings=servings,
            name_hint=name_hint,
            raw_caption=raw_caption,
        )

    async def _save_prep(
        message: Message,
        labels: list,
        *,
        servings: float,
        name_hint: str | None = None,
        raw_caption: str | None = None,
    ) -> None:
        ingredients = [ingredient_meal_from_label(lab) for lab in labels]
        total_kcal = sum(i.kcal for i in ingredients)
        total_p = sum(i.protein for i in ingredients)
        total_f = sum(i.fat for i in ingredients)
        total_c = sum(i.carbs for i in ingredients)
        if total_kcal <= 0:
            await message.reply("Не смог посчитать КБЖУ ингредиентов.")
            return

        name = build_prep_name(ingredients, name_hint)
        per = ParsedMeal(
            name=name,
            kcal=round(total_kcal / servings, 1),
            protein=round(total_p / servings, 1),
            fat=round(total_f / servings, 1),
            carbs=round(total_c / servings, 1),
        )
        total = ParsedMeal(
            name=name,
            kcal=round(total_kcal, 1),
            protein=round(total_p, 1),
            fat=round(total_f, 1),
            carbs=round(total_c, 1),
        )
        prep_id = await db.add_meal_prep(
            name=name,
            servings=servings,
            kcal_total=total.kcal,
            protein_total=total.protein,
            fat_total=total.fat,
            carbs_total=total.carbs,
            ingredients=[
                {
                    "name": i.name,
                    "kcal": i.kcal,
                    "protein": i.protein,
                    "fat": i.fat,
                    "carbs": i.carbs,
                    "amount": i.amount,
                    "amount_unit": i.amount_unit,
                }
                for i in ingredients
            ],
        )
        await db.clear_pending(message.chat.id)
        text = format_prep_created(
            name=name,
            servings=servings,
            total=total,
            per=per,
            ingredients=ingredients,
        )
        sent = await message.reply(
            text,
            reply_markup=_kb_cancel_prep(prep_id),
        )
        try:
            await db.set_prep_tg_message(prep_id, sent.message_id)
        except Exception:
            log.exception("Failed to store prep tg_message_id")

    async def _eat_prep_servings(
        message: Message,
        prep_row,
        servings: float,
        *,
        day: date,
        raw: str | None = None,
    ) -> None:
        total_s = float(prep_row["servings_total"])
        left = float(prep_row["servings_left"])
        if left <= 0.01:
            await message.reply("В этой заготовке порций уже не осталось.")
            return
        take = min(servings, left)
        factor = take / total_s
        meal = ParsedMeal(
            name=f"{prep_row['name']} ({fmt_num(take)} порц.)",
            kcal=round(float(prep_row["kcal_total"]) * factor, 1),
            protein=round(float(prep_row["protein_total"]) * factor, 1),
            fat=round(float(prep_row["fat_total"]) * factor, 1),
            carbs=round(float(prep_row["carbs_total"]) * factor, 1),
        )
        await db.consume_prep_servings(int(prep_row["id"]), take)
        new_left = left - take
        note = f"\n🍲 Осталось порций в заготовке: <b>{fmt_num(max(new_left, 0))}</b>"
        await _save_meal(
            message,
            db,
            meal,
            source="prep",
            raw=raw,
            day=day,
            extra_note=note,
        )

    async def _download_photo(bot: Bot, message: Message) -> bytes:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        buf = await bot.download_file(file.file_path)
        return buf.read()

    @router.message(Command("start", "help"))
    async def cmd_help(message: Message) -> None:
        if not allowed(message):
            return
        await message.reply(HELP_TEXT)

    @router.message(Command("day", "today", "итог"))
    async def cmd_day(message: Message) -> None:
        if not allowed(message):
            return
        text = await format_day_summary(db, settings, today())
        await message.reply(text)

    @router.message(Command("goals", "цели"))
    async def cmd_goals(message: Message) -> None:
        if not allowed(message):
            return
        day = today()
        weight = await db.latest_weight()
        totals = await db.day_totals(day)
        goals = await load_goals(db)
        needs = await day_energy(db, settings, day, weight) if weight else None
        progress = calc_goal_progress(
            goals, needs=needs, totals=totals, current_weight=weight
        )
        await message.reply("\n".join(format_goals_block(progress)))

    @router.message(Command("sync", "picooc"))
    async def cmd_sync(message: Message, bot: Bot) -> None:
        if not allowed(message):
            return
        if not settings.picooc_email or not settings.picooc_password:
            await message.reply("Picooc ещё не настроен (нет логина/пароля в .env).")
            return
        await message.reply("🔄 Синкаю Picooc…")
        try:
            new_ones = await sync_picooc(bot, settings, db, notify=True)
        except Exception as e:
            log.exception("Manual Picooc sync failed")
            await message.reply(f"Ошибка синка Picooc: <code>{e}</code>")
            return
        if not new_ones:
            body = await db.latest_body_measurement()
            if body:
                await message.reply(
                    f"Новых взвешиваний нет.\n"
                    f"Последнее: <b>{fmt_num(body['weight_kg'])} кг</b> "
                    f"({body['weighed_at']})"
                )
            else:
                await message.reply("Новых взвешиваний нет, история пока пустая.")
        else:
            await message.reply(f"Готово: новых измерений — <b>{len(new_ones)}</b>.")

    @router.message(Command("undo"))
    async def cmd_undo(message: Message) -> None:
        if not allowed(message):
            return
        entry = await db.undo_last_entry(today())
        if not entry:
            await message.reply("Нечего удалять — за сегодня записей нет.")
            return
        kind, name = entry
        label = "еду" if kind == "meal" else "активность"
        await message.reply(f"Удалил {label}: <b>{name}</b>")

    @router.message(Command("cancel", "отмена"))
    async def cmd_cancel(message: Message) -> None:
        if not allowed(message):
            return
        pending = await db.get_pending(message.chat.id)
        await db.clear_pending(message.chat.id)
        if pending:
            await message.reply("Ок, фото отменил.")
        else:
            await message.reply("Нечего отменять.")

    @router.message(F.voice | F.audio | F.video_note)
    async def on_voice(message: Message, bot: Bot) -> None:
        if not allowed(message):
            return
        media = message.voice or message.audio or message.video_note
        if not media:
            return
        await message.reply("🎤 Слушаю…")
        text = await transcribe_telegram_voice(bot, media.file_id, settings)
        if not text:
            await message.reply("Не разобрал голос. Попробуй ещё раз или текстом.")
            return
        await message.reply(f"Распознал: <i>{text}</i>")
        await _handle_free_text(message, text)

    @router.message(F.photo)
    async def on_photo(message: Message, bot: Bot) -> None:
        if not allowed(message):
            return
        log.info(
            "Photo from chat=%s media_group=%s",
            message.chat.id,
            message.media_group_id,
        )

        # Альбом: собираем все фото, потом один раз разбираем
        if message.media_group_id:
            gid = message.media_group_id
            buf = _album_buffers.setdefault(
                gid,
                {
                    "messages": [],
                    "caption": None,
                    "chat_id": message.chat.id,
                    "bot": bot,
                    "task": None,
                    "status": None,
                },
            )
            buf["messages"].append(message)
            if message.caption and not buf["caption"]:
                buf["caption"] = message.caption

            async def _flush_album(group_id: str) -> None:
                await asyncio.sleep(ALBUM_WAIT_SEC)
                data = _album_buffers.pop(group_id, None)
                if not data or not data["messages"]:
                    return
                if group_id in _scan_cancelled:
                    _scan_cancelled.discard(group_id)
                    return
                msgs: list[Message] = data["messages"]
                first = msgs[0]
                status = await first.reply(
                    f"🔎 Читаю этикетки ({len(msgs)} фото)…",
                    reply_markup=_kb_cancel_scan(group_id),
                )
                images: list[bytes] = []
                for m in msgs:
                    if group_id in _scan_cancelled:
                        break
                    try:
                        images.append(await _download_photo(data["bot"], m))
                    except Exception:
                        log.exception("Album photo download failed")
                if group_id in _scan_cancelled:
                    _scan_cancelled.discard(group_id)
                    try:
                        await status.edit_text("✖️ Распознавание отменено.")
                    except Exception:
                        pass
                    return
                if not images:
                    await status.edit_text("Не удалось скачать фото.")
                    return
                await _process_photo_images(
                    first,
                    images,
                    caption=data.get("caption"),
                    status=status,
                    scan_id=group_id,
                )

            old_task = buf.get("task")
            if old_task and not old_task.done():
                old_task.cancel()
            buf["task"] = asyncio.create_task(_flush_album(gid))
            return

        scan_id = f"m{message.message_id}"
        status = await message.reply(
            "🔎 Читаю этикетку…",
            reply_markup=_kb_cancel_scan(scan_id),
        )
        try:
            image_bytes = await _download_photo(bot, message)
        except Exception:
            log.exception("Photo download failed")
            await status.edit_text("Ошибка при загрузке фото.")
            return
        if scan_id in _scan_cancelled:
            _scan_cancelled.discard(scan_id)
            try:
                await status.edit_text("✖️ Распознавание отменено.")
            except Exception:
                pass
            return
        await _process_photo_images(
            message,
            [image_bytes],
            caption=message.caption,
            status=status,
            scan_id=scan_id,
        )

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        if not allowed(message):
            return
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return
        log.info("Text from chat=%s: %s", message.chat.id, text[:80])
        await _handle_free_text(message, text)

    async def _handle_free_text(message: Message, text: str) -> None:
        day = today()

        # Ответ на запись бота: «удали это»
        if is_delete_request(text):
            deleted_name = await _try_delete_from_reply(message, day)
            if deleted_name:
                await message.reply(
                    f"🗑 Удалил: <b>{deleted_name}</b>" + await progress_extra(day)
                )
                return
            if message.reply_to_message:
                await message.reply(
                    "Не нашёл запись по этому сообщению.\n"
                    "Ответь «удали» на сообщение с едой, заготовкой "
                    "или активностью — или используй /undo / кнопку «Отменить»."
                )
                return
            entry = await db.undo_last_entry(day)
            if entry:
                kind, name = entry
                label = "еду" if kind == "meal" else "активность"
                await message.reply(
                    f"🗑 Удалил {label}: <b>{name}</b>" + await progress_extra(day)
                )
            else:
                await message.reply("Нечего удалять — за сегодня записей нет.")
            return

        # Ответ на запись еды: «повтори» / «сегодня тоже самое»
        if is_repeat_request(text):
            meal = await _try_repeat_from_reply(message)
            if meal is not None:
                await _save_meal(
                    message, db, meal, source="repeat", raw=text, day=day
                )
                return
            if message.reply_to_message:
                await message.reply(
                    "Не нашёл блюдо по этому сообщению.\n"
                    "Ответь <code>повтори</code> на сообщение бота с едой "
                    "(🍽 …)."
                )
                return
            await message.reply(
                "Чтобы повторить блюдо — ответь <code>повтори</code> "
                "на его запись.\n"
                "Или напиши <code>как вчера</code>."
            )
            return

        # Порция из заготовки: ответ на 🍲 или «съел порцию»
        eat_n = parse_eat_servings(text)
        if eat_n is not None or looks_like_eat_from_prep(text):
            prep_row = None
            replied = message.reply_to_message
            if replied:
                prep_row = await db.find_prep_by_tg_message(replied.message_id)
            if prep_row is None and looks_like_eat_from_prep(text):
                prep_row = await db.find_active_prep_by_name(text)
            # без ответа на сообщение — только явные фразы про порцию/съел
            if prep_row is None and eat_n is not None and re.search(
                r"(?i)порц|съел|заготов", text
            ):
                prep_row = await db.latest_active_prep()
            if prep_row is not None:
                n = eat_n if eat_n is not None else 1.0
                await _eat_prep_servings(
                    message, prep_row, n, day=day, raw=text
                )
                return
            if (
                eat_n is not None
                and replied
                and (
                    "🍲" in (replied.html_text or replied.text or "")
                    or "Заготовка" in (replied.text or "")
                )
            ):
                await message.reply("Не нашёл эту заготовку. Создай новую с фото.")
                return

        low = text.lower().replace("ё", "е").strip()
        if low in {
            "как день",
            "оценка",
            "оценка дня",
            "как сегодня",
            "ревью",
            "как я сегодня",
            "итоги",
            "итог",
        } or re.fullmatch(r"как\s+(день|сегодня|поел|ел)", low):
            await message.reply(await format_day_summary(db, settings, day))
            return
        if low in {"день подробно", "итог подробно", "полный день", "как день подробно"}:
            await message.reply(
                await format_day_summary(db, settings, day, detailed=True)
            )
            return

        pending = await db.get_pending(message.chat.id)
        if pending:
            # Ждём пропущенный ингредиент текстом (фото не прочиталось)
            if pending.get("_prep_wait_missing"):
                low_m = text.lower().replace("ё", "е").strip()
                if low_m in {
                    "только это",
                    "только то что есть",
                    "дальше",
                    "ок",
                    "готово",
                    "хватит",
                    "без него",
                    "пропусти",
                }:
                    labels = [
                        LabelData.from_dict(x)
                        for x in (pending.get("labels") or [])
                    ]
                    servings = pending.get("servings")
                    if not servings or float(servings) < 1:
                        await db.set_pending(
                            message.chat.id,
                            {
                                "_prep_wait_servings": True,
                                "_ts": time.time(),
                                "labels": [x.to_dict() for x in labels],
                                "name_hint": pending.get("name_hint"),
                                "raw": pending.get("raw") or text,
                            },
                        )
                        await message.reply(
                            "Ок, без пропущенного. На сколько порций?",
                            reply_markup=_kb_cancel_pending(),
                        )
                        return
                    await _save_prep(
                        message,
                        labels,
                        servings=float(servings),
                        name_hint=pending.get("name_hint"),
                        raw_caption=pending.get("raw") or text,
                    )
                    return

                # Только граммы без названия — уточняем
                grams_only = re.fullmatch(
                    r"\s*(\d+[.,]?\d*)\s*(г|гр|грамм[аыу]?|мл|л)?\s*",
                    low_m,
                )
                if grams_only and not looks_like_food(text):
                    await message.reply(
                        "Напиши продукт и массу, например: "
                        "<code>макароны 450 г</code> или "
                        "<code>спагетти 450г</code>.\n"
                        "Или <code>только это</code> — сохранить без него.",
                        reply_markup=_kb_cancel_pending(),
                    )
                    return

                meal = parse_meal_regex(text)
                if meal is None or meal.kcal <= 0:
                    meal = await parse_meal(text, settings)
                if meal is None or meal.kcal <= 0:
                    # граммы + название без КБЖУ → типовые макароны/рис и т.п.
                    meal = _estimate_staple_for_prep(text)
                if meal is None or meal.kcal <= 0:
                    await message.reply(
                        "Не понял ингредиент. Пример: <code>макароны 450 г</code>\n"
                        "Или <code>только это</code>.",
                        reply_markup=_kb_cancel_pending(),
                    )
                    return

                labels = [
                    LabelData.from_dict(x) for x in (pending.get("labels") or [])
                ]
                labels.append(_meal_as_prep_label(meal))
                # Может ещё ждать другие пропуски — если фото было 2, хватит одного дописанного
                photos_count = int(pending.get("photos_count") or 0)
                if photos_count and len(labels) < photos_count:
                    await db.set_pending(
                        message.chat.id,
                        {
                            **pending,
                            "labels": [x.to_dict() for x in labels],
                            "found_count": len(labels),
                            "_ts": time.time(),
                        },
                    )
                    await message.reply(
                        f"Добавил: <b>{meal.name}</b> "
                        f"({fmt_num(meal.kcal, 0)} ккал). "
                        f"Ещё чего-то не хватает? Напиши или <code>только это</code>.",
                        reply_markup=_kb_cancel_pending(),
                    )
                    return

                servings = pending.get("servings")
                if not servings or float(servings) < 1:
                    await db.set_pending(
                        message.chat.id,
                        {
                            "_prep_wait_servings": True,
                            "_ts": time.time(),
                            "labels": [x.to_dict() for x in labels],
                            "name_hint": pending.get("name_hint"),
                            "raw": pending.get("raw") or text,
                        },
                    )
                    await message.reply(
                        f"Добавил <b>{meal.name}</b>. На сколько порций?",
                        reply_markup=_kb_cancel_pending(),
                    )
                    return
                await _save_prep(
                    message,
                    labels,
                    servings=float(servings),
                    name_hint=pending.get("name_hint"),
                    raw_caption=pending.get("raw") or text,
                )
                return

            # Ждём граммы ингредиента без нетто (макароны и т.п.)
            if pending.get("_prep_wait_weight"):
                amount, unit, _full = parse_portion_answer(text)
                if amount is None or unit not in {"г", "мл", "л", None}:
                    m = re.search(
                        r"(\d+[.,]\d+|\d+)\s*(г|гр|грамм|мл|л)?",
                        text.lower().replace("ё", "е"),
                    )
                    if m:
                        try:
                            amount = float(m.group(1).replace(",", "."))
                        except ValueError:
                            amount = None
                        unit = m.group(2) or "г"
                        if unit in {"гр", "грамм"}:
                            unit = "г"
                if amount is None or amount <= 0 or amount > 5000:
                    wait = pending.get("wait_name") or "продукта"
                    await message.reply(
                        f"Напиши массу <b>{wait}</b> в граммах, "
                        "например <code>200 г</code>.",
                        reply_markup=_kb_cancel_pending(),
                    )
                    return
                if unit in {None, "гр", "грамм"}:
                    unit = "г"
                labels = [
                    LabelData.from_dict(x) for x in (pending.get("labels") or [])
                ]
                wait_name = (pending.get("wait_name") or "").lower()
                filled = False
                still_need: list = []
                for lab in labels:
                    if (
                        not filled
                        and needs_package_weight(lab)
                        and not lab.package_amount
                        and (
                            not wait_name
                            or wait_name[:20] in (lab.name or "").lower()
                            or (lab.name or "").lower()[:20] in wait_name
                        )
                    ):
                        lab.package_amount = amount
                        lab.package_unit = unit if unit in {"г", "мл", "л"} else "г"
                        filled = True
                    elif needs_package_weight(lab) and not lab.package_amount:
                        still_need.append(lab)
                if not filled:
                    for lab in labels:
                        if needs_package_weight(lab) and not lab.package_amount:
                            lab.package_amount = amount
                            lab.package_unit = unit if unit in {"г", "мл", "л"} else "г"
                            filled = True
                            break
                    still_need = [
                        lab
                        for lab in labels
                        if needs_package_weight(lab) and not lab.package_amount
                    ]
                if still_need:
                    await db.set_pending(
                        message.chat.id,
                        {
                            **pending,
                            "_prep_wait_weight": True,
                            "labels": [x.to_dict() for x in labels],
                            "wait_name": still_need[0].name,
                            "_ts": time.time(),
                        },
                    )
                    await message.reply(
                        f"Ок, записал {fmt_num(amount)} {unit}.\n"
                        f"Сколько грамм <b>{still_need[0].name}</b>?\n"
                        "Например: <code>150 г</code>.",
                        reply_markup=_kb_cancel_pending(),
                    )
                    return
                servings = pending.get("servings")
                if not servings or float(servings) < 1:
                    await db.set_pending(
                        message.chat.id,
                        {
                            "_prep_wait_servings": True,
                            "_ts": time.time(),
                            "labels": [x.to_dict() for x in labels],
                            "name_hint": pending.get("name_hint"),
                            "raw": pending.get("raw") or text,
                        },
                    )
                    await message.reply(
                        f"Ок, {fmt_num(amount)} {unit}. На сколько порций?\n"
                        "Например: <code>4 порции</code>.",
                        reply_markup=_kb_cancel_pending(),
                    )
                    return
                await _save_prep(
                    message,
                    labels,
                    servings=float(servings),
                    name_hint=pending.get("name_hint"),
                    raw_caption=pending.get("raw") or text,
                )
                return

            # Ждём число порций для заготовки
            if pending.get("_prep_wait_servings"):
                draft = parse_prep_servings(text)
                n = draft.servings if draft and draft.servings >= 1 else None
                if n is None:
                    m = re.search(r"(\d+[.,]\d+|\d+)", text)
                    if m:
                        try:
                            n = float(m.group(1).replace(",", "."))
                        except ValueError:
                            n = None
                if n is None or n < 1 or n > 30:
                    await message.reply(
                        "Напиши число порций, например <code>4</code> или <code>на 4 порции</code>."
                    )
                    return
                labels = [
                    LabelData.from_dict(x) for x in (pending.get("labels") or [])
                ]
                await _save_prep(
                    message,
                    labels,
                    servings=n,
                    name_hint=pending.get("name_hint"),
                    raw_caption=pending.get("raw") or text,
                )
                return

            amount, unit, full = parse_portion_answer(text)
            low = text.lower().replace("ё", "е")
            if full or amount is not None or low in {"все", "вся", "целиком", "полностью"}:
                if low in {"все", "вся", "целиком", "полностью"} or "полностью" in low:
                    full = True
                label = LabelData.from_dict(pending)
                # Если ждут массу упаковки и прислали граммы — это и есть упаковка/порция
                if (
                    pending.get("_waiting_more")
                    and amount is not None
                    and unit in {"г", "мл", "л"}
                    and needs_package_weight(label)
                ):
                    label.package_amount = amount
                    label.package_unit = unit
                    meal = scale_label_to_meal(label, use_full_package=True)
                else:
                    meal = scale_label_to_meal(
                        label, amount=amount, amount_unit=unit, use_full_package=full
                    )
                await db.clear_pending(message.chat.id)
                await _save_meal(message, db, meal, source="photo", raw=text, day=day)
                return
            meal_probe = parse_meal_regex(text)
            if (
                parse_weight(text) is None
                and meal_probe is None
                and parse_factor_override(text) is None
                and parse_goal_command(text) is None
                and parse_copy_yesterday(text) is None
                and not looks_like_activity(text)
                and not looks_like_food(text)
            ):
                await message.reply(
                    "Жду массу упаковки или второе фото.\n"
                    "Напиши <code>230 г</code> / <code>190 мл</code> — запишу всю упаковку.\n"
                    "Или порцию: <code>100 г</code>, <code>половину</code>.\n"
                    "/cancel — отменить."
                )
                return
            await db.clear_pending(message.chat.id)

        weight = parse_weight(text)
        if weight is not None:
            await db.add_weight(weight, day)
            needs = await day_energy(db, settings, day, weight)
            extra = await progress_extra(day)
            await message.reply(
                f"⚖️ Вес обновлён: <b>{fmt_num(weight)} кг</b>\n"
                f"BMR {fmt_num(needs.bmr, 0)} ккал · "
                f"расход дня ~<b>{fmt_num(needs.tdee, 0)}</b> ккал"
                + extra
            )
            return

        weight_now = await db.latest_weight()
        goal_cmd = parse_goal_command(text, weight_kg=weight_now)
        if goal_cmd is not None:
            kind, kwargs = goal_cmd
            if kind == "weight":
                goals = await save_target_weight(
                    db, kwargs["target_weight"], kwargs.get("weekly_loss_kg")
                )
                await message.reply(
                    f"✅ Цель по весу: <b>{fmt_num(goals.target_weight)} кг</b>\n"
                    f"Темп: −{fmt_num(goals.weekly_loss_kg)} кг/нед"
                    + await progress_extra(day)
                )
            elif kind == "protein":
                goals = await save_protein_goal(db, kwargs["protein_g"])
                await message.reply(
                    f"✅ Цель по белку: <b>{fmt_num(goals.protein_g, 0)} г/день</b>"
                    + await progress_extra(day)
                )
            else:
                goals = await apply_goal_updates(db, kwargs)
                bits = ["✅ Цели обновлены:"]
                if "weekly_loss_kg" in kwargs:
                    bits.append(f"темп −{fmt_num(goals.weekly_loss_kg)} кг/нед")
                if "protein_g" in kwargs:
                    bits.append(f"белок {fmt_num(goals.protein_g, 0)} г")
                if "target_weight" in kwargs:
                    bits.append(f"вес {fmt_num(goals.target_weight)} кг")
                await message.reply("\n".join(bits) + await progress_extra(day))
            return

        copy_slot = parse_copy_yesterday(text)
        if copy_slot is not None:
            copied = await copy_yesterday_meals(
                db, today=day, slot=copy_slot, tz_name=settings.timezone
            )
            if not copied:
                await message.reply(
                    f"Вчера не нашлось записей для «{SLOT_LABELS.get(copy_slot, copy_slot)}»."
                )
                return
            names = ", ".join(m.name for m in copied[:5])
            more = "" if len(copied) <= 5 else f" и ещё {len(copied) - 5}"
            await message.reply(
                f"📋 Скопировал с вчера ({SLOT_LABELS.get(copy_slot, copy_slot)}): "
                f"<b>{len(copied)}</b>\n{names}{more}"
                + await progress_extra(day)
            )
            return

        factor = parse_factor_override(text)
        if factor is not None:
            await db.set_day_factor(day, factor.factor)
            if weight_now is None:
                await message.reply(
                    f"⚙️ Кф дня: <b>×{fmt_num(factor.factor, 2)}</b> ({factor.label})\n"
                    "Вес ещё не задан — расход посчитаю после веса."
                )
                return
            needs = await day_energy(db, settings, day, weight_now)
            await message.reply(
                f"⚙️ Кф дня: <b>×{fmt_num(factor.factor, 2)}</b> ({factor.label})\n"
                f"Расход дня ~<b>{fmt_num(needs.tdee, 0)}</b> ккал "
                f"(эфф. ×{fmt_num(needs.effective_factor, 2)})"
                + await progress_extra(day)
            )
            return

        meal = parse_meal_regex(text)
        if meal and meal.kcal > 0:
            await _save_meal(message, db, meal, source="text", raw=text, day=day)
            return

        if looks_like_activity(text):
            if weight_now is None:
                await message.reply(
                    "Сначала укажи вес (<code>Вес 139.3 кг</code>) — "
                    "от него считаю ккал активности."
                )
                return
            activity = await parse_activity(text, weight_now, settings)
            if activity is not None:
                activity_id = await db.add_activity(
                    day=day,
                    name=activity.name,
                    kcal=activity.kcal,
                    duration_min=activity.duration_min,
                    raw=text,
                )
                needs = await day_energy(db, settings, day, weight_now)
                dur = ""
                if activity.duration_min:
                    dur = f" · {fmt_num(activity.duration_min, 0)} мин"
                steps_note = ""
                if activity.name.lower().startswith("шаги"):
                    steps_note = "\n(учёл как тысячи шагов, ккал от твоего веса)"
                sent = await message.reply(
                    f"🏃 <b>{activity.name}</b>\n"
                    f"~{fmt_num(activity.kcal, 0)} ккал{dur}{steps_note}\n"
                    f"✅ Добавил к расходу дня\n"
                    f"Расход дня ~<b>{fmt_num(needs.tdee, 0)}</b> ккал"
                    + await progress_extra(day)
                )
                try:
                    await db.set_activity_tg_message(activity_id, sent.message_id)
                except Exception:
                    log.exception(
                        "Failed to store tg_message_id for activity %s", activity_id
                    )
                return

        if looks_like_food(text) or len(text.split()) >= 3:
            meal = await parse_meal(text, settings)
            if meal is not None and meal.kcal > 0:
                await _save_meal(message, db, meal, source="voice" if message.voice else "text", raw=text, day=day)

    async def _try_delete_from_reply(
        message: Message, day: date
    ) -> str | None:
        replied = message.reply_to_message
        if not replied:
            return None
        body = replied.html_text or replied.text or replied.caption

        prep_row = await db.find_prep_by_tg_message(replied.message_id)
        if prep_row is None and body:
            plain = re.sub(r"<[^>]+>", "", body)
            m = re.search(r"Заготовка:\s*(.+)", plain)
            if m:
                hint = m.group(1).strip().split("\n")[0].strip()
                if hint:
                    prep_row = await db.find_active_prep_by_name(hint)
        if prep_row is not None:
            return await db.cancel_prep(int(prep_row["id"]))

        meal_id = await db.find_meal_by_tg_message(replied.message_id)
        if meal_id is None:
            name = extract_meal_name_from_bot_message(body)
            if name:
                meal_id = await db.find_meal_by_name(day, name)
                if meal_id is None:
                    row = await db.find_meal_by_name_recent(name)
                    meal_id = int(row["id"]) if row else None
        if meal_id is not None:
            return await db.delete_meal_by_id(meal_id)

        act_id = await db.find_activity_by_tg_message(replied.message_id)
        if act_id is None:
            name = extract_activity_name_from_bot_message(body)
            if name:
                act_id = await db.find_activity_by_name(day, name)
        if act_id is not None:
            return await db.delete_activity_by_id(act_id)
        return None

    async def _try_repeat_from_reply(message: Message) -> ParsedMeal | None:
        replied = message.reply_to_message
        if not replied:
            return None
        body = replied.html_text or replied.text or replied.caption
        row = None
        meal_id = await db.find_meal_by_tg_message(replied.message_id)
        if meal_id is not None:
            row = await db.get_meal_by_id(meal_id)
        if row is None:
            name = extract_meal_name_from_bot_message(body)
            if name:
                row = await db.find_meal_by_name_recent(name)
        if row is None:
            return None
        return ParsedMeal(
            name=str(row["name"]),
            kcal=float(row["kcal"] or 0),
            protein=float(row["protein"] or 0),
            fat=float(row["fat"] or 0),
            carbs=float(row["carbs"] or 0),
            amount=float(row["amount"]) if row["amount"] is not None else None,
            amount_unit=row["amount_unit"],
            meal_slot=None,
        )

    async def _save_meal(
        message: Message,
        database: Database,
        meal,
        *,
        source: str,
        day: date,
        raw: str | None = None,
        extra_note: str = "",
    ) -> None:
        slot = meal.meal_slot or infer_meal_slot(datetime.now(tz), settings.timezone)
        meal_id = await database.add_meal(
            day=day,
            name=meal.name,
            kcal=meal.kcal,
            protein=meal.protein,
            fat=meal.fat,
            carbs=meal.carbs,
            amount=meal.amount,
            amount_unit=meal.amount_unit,
            source=source,
            raw=raw,
            meal_slot=slot,
        )
        extra = await progress_extra(day)
        sent = await message.reply(
            format_meal_line(meal)
            + extra_note
            + f"\n✅ Записал ({SLOT_LABELS.get(slot, slot)})"
            + extra,
            reply_markup=_kb_cancel_meal(meal_id),
        )
        try:
            await database.set_meal_tg_message(meal_id, sent.message_id)
        except Exception:
            log.exception("Failed to store tg_message_id for meal %s", meal_id)

    @router.callback_query(F.data.startswith("cancel_"))
    async def on_cancel_callback(query: CallbackQuery) -> None:
        if not query.message or not query.from_user:
            await query.answer()
            return
        if query.message.chat.id != settings.allowed_chat_id:
            await query.answer("Недоступно", show_alert=True)
            return

        data = query.data or ""
        try:
            if data == "cancel_pending":
                await db.clear_pending(query.message.chat.id)
                await query.message.edit_reply_markup(reply_markup=None)
                await query.message.reply("Ок, отменил.")
                await query.answer("Отменено")
                return

            if data.startswith("cancel_scan:"):
                scan_id = data.split(":", 1)[1]
                _scan_cancelled.add(scan_id)
                await db.clear_pending(query.message.chat.id)
                try:
                    await query.message.edit_text("✖️ Распознавание отменено.")
                except Exception:
                    await query.message.reply("✖️ Распознавание отменено.")
                await query.answer("Отменено")
                return

            if data.startswith("cancel_meal:"):
                meal_id = int(data.split(":")[1])
                name = await db.delete_meal_by_id(meal_id)
                if not name:
                    await query.answer("Уже удалено", show_alert=True)
                    return
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                await query.message.reply(
                    f"🗑 Отменил запись: <b>{name}</b>"
                    + await progress_extra(today())
                )
                await query.answer("Удалено")
                return

            if data.startswith("cancel_prep:"):
                prep_id = int(data.split(":")[1])
                name = await db.cancel_prep(prep_id)
                if not name:
                    await query.answer("Уже отменено", show_alert=True)
                    return
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                await query.message.reply(f"🗑 Заготовка отменена: <b>{name}</b>")
                await query.answer("Отменено")
                return

            await query.answer()
        except Exception:
            log.exception("Cancel callback failed: %s", data)
            await query.answer("Ошибка", show_alert=True)

    return router
