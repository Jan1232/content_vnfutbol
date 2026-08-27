"""Inline keyboards for editorial moderation bot."""

from __future__ import annotations

from editorial.content_blocks import UNACCEPTABLE_LABELS
from editorial.event_labels import EVENT_TYPE_LABELS, moderation_event_types


def _cb(action: str, news_id: int, extra: str = "") -> str:
    raw = f"{action}:{news_id}"
    if extra:
        raw = f"{raw}:{extra}"
    return raw[:64]


def review_keyboard(news_id: int, *, allow_photo: bool = True) -> dict:
    rows = [
        [
            {"text": "✅ Одобрить", "callback_data": _cb("ok", news_id)},
            {"text": "❌ Отклонить", "callback_data": _cb("no", news_id)},
        ],
        [
            {"text": "🚫 Недопустимый", "callback_data": _cb("bad", news_id)},
            {"text": "✏️ Текст", "callback_data": _cb("txt", news_id)},
        ],
        [{"text": "📂 Категория", "callback_data": _cb("cat", news_id)}],
    ]
    if allow_photo:
        rows.append([{"text": "🔍 Запрос фото", "callback_data": _cb("photo", news_id)}])
    return {"inline_keyboard": rows}


def category_keyboard(news_id: int, allowed: list[str] | tuple[str, ...] | None) -> dict:
    rows: list[list[dict]] = []
    row: list[dict] = []
    for et in moderation_event_types(allowed):
        label = EVENT_TYPE_LABELS.get(et, et)
        row.append({"text": label, "callback_data": _cb("catr", news_id, et)})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "↩️ Назад", "callback_data": _cb("back", news_id)}])
    return {"inline_keyboard": rows}


def unacceptable_keyboard(news_id: int) -> dict:
    rows = []
    for reason, label in UNACCEPTABLE_LABELS.items():
        rows.append([{"text": label, "callback_data": _cb("badr", news_id, reason)}])
    rows.append([{"text": "↩️ Назад", "callback_data": _cb("back", news_id)}])
    return {"inline_keyboard": rows}


def photo_pick_keyboard(news_id: int, n: int) -> dict:
    rows = []
    row: list[dict] = []
    for i in range(n):
        row.append({"text": str(i + 1), "callback_data": _cb("pick", news_id, str(i))})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            {"text": "↩️ Другой запрос", "callback_data": _cb("photo", news_id)},
            {"text": "↩️ К посту", "callback_data": _cb("back", news_id)},
        ]
    )
    return {"inline_keyboard": rows}
