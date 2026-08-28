"""Send editorial preview cards to Telegram admin."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from editorial.channel_config import EditorialChannelConfig
from editorial.imagery import imagery_meta_of
from editorial.meme_text import is_meme_row
from editorial.tg_content.publisher import mirror_enabled
from editorial.tg_moderator import api
from editorial.event_labels import event_type_label
from editorial.tg_moderator.keyboards import review_keyboard


def _esc(text: str) -> str:
    return html.escape(text or "")


def format_caption(item: dict[str, Any], channel: EditorialChannelConfig) -> str:
    meta = imagery_meta_of(item)
    query = str(meta.get("query") or "").strip() or "—"
    pick = meta.get("pick") if isinstance(meta.get("pick"), dict) else {}
    pick_bits: list[str] = []
    if pick.get("via"):
        pick_bits.append(f"via={pick.get('via')}")
    if pick.get("score") is not None:
        try:
            pick_bits.append(f"score={float(pick.get('score')):.2f}")
        except (TypeError, ValueError):
            pick_bits.append(f"score={pick.get('score')}")
    if pick.get("who"):
        pick_bits.append(str(pick.get("who"))[:100])
    if pick.get("reason"):
        pick_bits.append(str(pick.get("reason"))[:120])
    extras = pick.get("extras") if isinstance(pick.get("extras"), dict) else {}
    if not extras and isinstance(meta.get("vision"), dict):
        rows = (meta.get("vision") or {}).get("rows") or []
        if not rows:
            rows = (meta.get("vision") or {}).get("candidates") or []
        if rows and isinstance(rows[0], dict):
            extras = rows[0]
    club = extras.get("club_on_photo") if isinstance(extras, dict) else ""
    if not club and pick.get("club_on_photo"):
        club = str(pick.get("club_on_photo"))
    if club and club not in {"none", "unknown", "—"}:
        pick_bits.append(f"club={club}")
    pick_line = " · ".join(pick_bits) if pick_bits else "—"
    headline = (item.get("caption_line1") or item.get("headline") or "").strip()
    meme = is_meme_row(item)
    from editorial.soccerblog_gate import gate_verdict_of_row

    gate = gate_verdict_of_row(item)
    if gate:
        lines.append(
            f"<b>LLM gate:</b> {_esc(str(gate.get('kind') or '—'))} "
            f"conf={gate.get('confidence', '—')} — {_esc(str(gate.get('reason') or '')[:200])}"
        )

    et = str(item.get("event_type") or "—")
    if et == "fixture_result":
        lines = [
            f"<b>#{item.get('id')} · {channel.slug} · ⚽ РЕЗУЛЬТАТ МАТЧА</b>",
            f"{_esc(item.get('headline') or item.get('title') or '—')}",
            "",
            "<i>Карточка счёта (шаблон result), без поиска фото.</i>",
            "",
            _esc(item.get("post_text") or "")[:3500],
        ]
        return "\n".join(lines)

    lines = [
        f"<b>#{item.get('id')} · {channel.slug}</b>",
        f"{_esc(event_type_label(et))} · {_esc(item.get('source') or '—')}",
    ]
    if meme:
        lines.extend(["", "<i>Мем / GIF — текст как в источнике, без редактуры.</i>"])
    elif headline:
        lines.extend(["", f"<b>Обложка:</b> {_esc(headline)}"])
    if not meme:
        lines.extend(
            [
                "",
                f"<b>Запрос к фото:</b> {_esc(query)}",
                f"<b>Выбор:</b> {_esc(pick_line)}",
            ]
        )
        photo_url = str(pick.get("url") or "").strip()
        if photo_url:
            lines.append(f"<i>URL фото:</i> {_esc(photo_url[:240])}")
    lines.extend(["", _esc(item.get("post_text") or "")[:3500]])
    url = str(item.get("url") or "")
    if url:
        lines.append("")
        lines.append(f'<a href="{_esc(url)}">источник новости</a>')
    return "\n".join(lines)


def _media_path(item: dict[str, Any]) -> Path | None:
    for key in ("cover_path", "media_path"):
        p = Path(str(item.get(key) or ""))
        if p.is_file():
            return p
    return None


def allow_photo_button(item: dict[str, Any]) -> bool:
    if (item.get("event_type") or "") == "fixture_result":
        return False
    if str(item.get("media_type") or "") == "video":
        return False
    if str(item.get("post_kind") or "") in {"video", "meme"}:
        return False
    return True


def _clip_caption(caption: str, *, limit: int = 1024) -> str:
    """Безопасно уложить HTML-подпись в лимит Telegram (не рвать тег)."""
    text = caption or ""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    last_open = cut.rfind("<")
    last_close = cut.rfind(">")
    if last_open > last_close:
        cut = cut[:last_open].rstrip()
    return cut + "…"


def send_review_card(
    item: dict[str, Any],
    channel: EditorialChannelConfig,
    *,
    admin_id: int,
) -> dict[str, Any]:
    base_caption = format_caption(item, channel)
    kb = review_keyboard(int(item["id"]), allow_photo=allow_photo_button(item))
    media = _media_path(item)
    if not media:
        return api.send_message(admin_id, _clip_caption(base_caption, limit=4096), reply_markup=kb)
    caption = _clip_caption(base_caption)
    if str(item.get("media_type") or "") == "video" or media.suffix.lower() in {".mp4", ".mov"}:
        return api.send_video(admin_id, media, caption=caption, reply_markup=kb)
    return api.send_photo(admin_id, media, caption=caption, reply_markup=kb)


_STATUS_BANNERS = {
    "approved": "✅ <b>ОПУБЛИКОВАНО в MAX</b>",
    "rejected": "❌ <b>ОТКЛОНЕНО</b>",
    "unacceptable": "🚫 <b>НЕДОПУСТИМЫЙ</b>",
}


def _status_banner(status: str, channel: EditorialChannelConfig | None) -> str:
    if status == "approved" and channel and mirror_enabled(channel):
        return "✅ <b>ОПУБЛИКОВАНО в MAX и Telegram</b>"
    return _STATUS_BANNERS.get(status, "")


def format_caption_with_status(
    item: dict[str, Any],
    channel: EditorialChannelConfig,
    status: str,
    *,
    detail: str = "",
) -> str:
    banner = _status_banner(status, channel)
    if detail:
        banner = f"{banner}\n<i>{_esc(detail)}</i>"
    base = format_caption(item, channel)
    return f"{banner}\n\n{base}" if banner else base


def finalize_review_card(
    chat_id: int | str,
    message_id: int,
    item: dict[str, Any],
    channel: EditorialChannelConfig,
    status: str,
    *,
    detail: str = "",
) -> None:
    """Пометить карточку решением и убрать inline-кнопки."""
    if not message_id:
        return
    caption = format_caption_with_status(item, channel, status, detail=detail)
    empty_kb = {"inline_keyboard": []}
    msg = item or {}
    has_media = bool(_media_path(msg))
    try:
        if has_media:
            api.edit_message_caption(
                chat_id,
                int(message_id),
                _clip_caption(caption),
                reply_markup=empty_kb,
            )
        else:
            api.edit_message_text(
                chat_id,
                int(message_id),
                caption[:4096],
                reply_markup=empty_kb,
            )
    except api.TelegramApiError:
        try:
            api.remove_inline_keyboard(chat_id, int(message_id))
        except api.TelegramApiError:
            pass
