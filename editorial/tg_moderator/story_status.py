"""Сводка очереди и статуса editorial для команды /story."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from editorial.channel_config import get_channel, load_editorial_channels, reload_editorial_channels
from editorial.scheduler import slot_ready
from editorial.store import get_channel_state, list_by_status, status_counts


def _esc(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _title_of(row: dict[str, Any], *, n: int = 70) -> str:
    t = (row.get("title") or row.get("headline") or row.get("post_text") or "").strip()
    t = " ".join(t.split())
    if len(t) > n:
        t = t[: n - 1] + "…"
    return t or "—"


def _phase_for(channel_slug: str, counts: dict[str, int], state: dict[str, Any]) -> str:
    awaiting = counts.get("awaiting_review", 0)
    ready = counts.get("ready", 0)
    imaging = counts.get("imaging", 0)
    verifying = counts.get("verifying", 0)
    held = counts.get("held", 0)
    deferred = counts.get("deferred", 0)
    editing = counts.get("editing", 0) + counts.get("confirmed", 0) + counts.get("topic", 0)

    cfg = get_channel(channel_slug)
    slot_ok = False
    if cfg:
        try:
            slot_ok = slot_ready(cfg)
        except Exception:
            slot_ok = False

    nxt = str(state.get("next_slot_at") or "").strip() or "—"

    if awaiting > 0:
        return f"⏳ Ждём твоей модерации в боте ({awaiting} карт.)"
    if imaging or verifying or editing:
        parts = []
        if imaging:
            parts.append(f"фото {imaging}")
        if verifying:
            parts.append(f"фактчек {verifying}")
        if editing:
            parts.append(f"обработка {editing}")
        return "🔧 Идёт подбор/обработка: " + ", ".join(parts)
    if ready > 0 and not slot_ok:
        return f"📦 Посты готовы ({ready}), ждём слот публикации до {nxt}"
    if ready > 0 and slot_ok:
        return f"🚀 Слот открыт, готово к выгрузке в ревью/MAX ({ready})"
    if held > 0:
        return f"🛑 Застряли в held ({held}) — смотри /editorial"
    if deferred > 0:
        return f"⏸ Отложены story-throttle ({deferred})"
    return "😴 Очередь пуста — ждём новые источники"


def build_story_report(*, limit: int = 12) -> str:
    reload_editorial_channels()
    channels = load_editorial_channels(include_disabled=False)
    if not channels:
        return "Нет активных editorial-каналов."

    blocks: list[str] = ["<b>/story</b> — очередь и статус\n"]
    for cfg in channels:
        counts_rows = status_counts(cfg.slug)
        counts = {str(r.get("status") or ""): int(r.get("n") or 0) for r in counts_rows}
        state = get_channel_state(cfg.slug)
        phase = _phase_for(cfg.slug, counts, state)
        brand = (cfg.brand.name or cfg.slug).strip()

        awaiting = list_by_status(cfg.slug, ("awaiting_review",), limit=limit)
        ready = list_by_status(cfg.slug, ("ready",), limit=limit)
        pipeline = list_by_status(
            cfg.slug,
            ("imaging", "verifying", "editing", "confirmed", "topic"),
            limit=8,
        )

        lines = [
            f"<b>{_esc(brand)}</b> <code>{_esc(cfg.slug)}</code>",
            phase,
            f"слот: <code>{_esc(state.get('next_slot_at') or '—')}</code> · "
            f"last: <code>{_esc(state.get('last_published_at') or '—')}</code>",
            (
                f"счётчики: review={counts.get('awaiting_review', 0)} "
                f"ready={counts.get('ready', 0)} "
                f"imaging={counts.get('imaging', 0)} "
                f"held={counts.get('held', 0)} "
                f"deferred={counts.get('deferred', 0)}"
            ),
        ]

        if awaiting:
            lines.append("\n<b>На модерации (ждут тебя):</b>")
            for row in awaiting:
                et = row.get("event_type") or "?"
                lines.append(
                    f"• #{row.get('id')} [{_esc(et)}] {_esc(_title_of(row))}"
                )
        else:
            lines.append("\nНа модерации: пусто")

        if ready:
            lines.append("\n<b>Ready (отобраны, ждут слот/выгрузку):</b>")
            for row in ready:
                et = row.get("event_type") or "?"
                kind = row.get("post_kind") or "news"
                lines.append(
                    f"• #{row.get('id')} [{_esc(et)}/{_esc(kind)}] {_esc(_title_of(row))}"
                )

        if pipeline:
            lines.append("\n<b>В обработке:</b>")
            for row in pipeline:
                lines.append(
                    f"• #{row.get('id')} {_esc(row.get('status') or '?')}: {_esc(_title_of(row, n=55))}"
                )

        blocks.append("\n".join(lines))

    blocks.append(f"\n<code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</code>")
    text = "\n\n".join(blocks)
    return text[:3900]
