"""Проверка аудитории канала на признаки накрутки (MAX members API)."""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from predlozka.client import PredlozkaClient

EKAT = ZoneInfo("Asia/Yekaterinburg")


@dataclass
class AudienceReport:
    chat_id: int
    title: str
    participants_count: int
    fetched: int
    admins: int
    bots: int
    burst_count: int
    organic_count: int
    burst_windows: list[tuple[str, int]] = field(default_factory=list)
    organic: list[dict[str, Any]] = field(default_factory=list)
    burst_user_ids: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary_text(self) -> str:
        lines = [
            f"🔎 Проверка аудитории: <b>{_esc(self.title)}</b>",
            "",
            f"Всего в API: <b>{self.fetched}</b> (participants_count={self.participants_count})",
            f"Админы/боты: {self.admins}/{self.bots}",
            f"Подозрение на накрутку (burst): <b>{self.burst_count}</b>",
            f"Похожи на живых: <b>{self.organic_count}</b>",
            "",
        ]
        if self.burst_windows:
            lines.append("Пики подписок (≥5/мин):")
            for when, n in self.burst_windows[:8]:
                lines.append(f"• {when} — {n}")
            lines.append("")
        if self.notes:
            for n in self.notes:
                lines.append(n)
            lines.append("")
        if self.organic:
            lines.append("Не из burst (для ориентира):")
            for m in self.organic[:15]:
                lines.append(
                    f"• {_esc(m.get('first_name') or '?')} "
                    f"join={_fmt_ms(m.get('join_time'))}"
                )
        lines.append("")
        lines.append(
            "Критерий burst: минута с ≥5 входами (+/−2 мин вокруг пика). "
            "Админы и боты не трогаются."
        )
        return "\n".join(lines)


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_ms(ms: Any) -> str:
    try:
        v = int(ms or 0)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "—"
    return datetime.fromtimestamp(v / 1000, EKAT).strftime("%d.%m %H:%M")


def fetch_all_members(client: PredlozkaClient, chat_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    marker: int | None = None
    while True:
        params: dict[str, Any] = {"count": 100}
        if marker is not None:
            params["marker"] = marker
        data = client._request("GET", f"/chats/{int(chat_id)}/members", params=params)
        batch = [m for m in (data.get("members") or []) if isinstance(m, dict)]
        out.extend(batch)
        marker = data.get("marker")
        if not marker or not batch:
            break
        time.sleep(0.15)
    return out


def analyze_members(
    chat_id: int,
    title: str,
    participants_count: int,
    members: list[dict[str, Any]],
    *,
    burst_threshold: int = 5,
    recent_hours: float | None = None,
) -> AudienceReport:
    by_min: dict[int, list[dict[str, Any]]] = defaultdict(list)
    admins = 0
    bots = 0
    now_ms = int(time.time() * 1000)
    recent_cut = None
    if recent_hours is not None and recent_hours > 0:
        recent_cut = now_ms - int(float(recent_hours) * 3600 * 1000)

    for m in members:
        if m.get("is_admin"):
            admins += 1
        if m.get("is_bot"):
            bots += 1
        if m.get("is_admin") or m.get("is_bot"):
            continue
        jt = int(m.get("join_time") or 0)
        if jt > 0:
            by_min[jt // 60_000].append(m)

    burst_mins = {k for k, v in by_min.items() if len(v) >= burst_threshold}
    expanded: set[int] = set()
    for k in burst_mins:
        for d in range(-2, 3):
            expanded.add(k + d)

    burst_ids: list[int] = []
    organic: list[dict[str, Any]] = []
    for m in members:
        if m.get("is_admin") or m.get("is_bot"):
            continue
        jt = int(m.get("join_time") or 0)
        minute = jt // 60_000 if jt else None
        uid = int(m.get("user_id") or 0)
        if not uid:
            continue
        in_burst = minute is not None and minute in expanded
        if in_burst:
            if recent_cut is not None and jt and jt < recent_cut:
                # старый burst не трогаем в авто-режиме
                organic.append(m)
            else:
                burst_ids.append(uid)
        else:
            organic.append(m)

    windows = [
        (_fmt_ms(k * 60_000), len(by_min[k]))
        for k in sorted(burst_mins)
        if recent_cut is None or (k * 60_000) >= recent_cut
    ]
    notes: list[str] = []
    if burst_ids:
        share = 100.0 * len(burst_ids) / max(1, len(members) - admins)
        notes.append(
            f"⚠️ ~{share:.0f}% не-админов попали в burst-окно — "
            f"очень похоже на накрутку."
        )
    return AudienceReport(
        chat_id=chat_id,
        title=title,
        participants_count=participants_count,
        fetched=len(members),
        admins=admins,
        bots=bots,
        burst_count=len(burst_ids),
        organic_count=len(organic),
        burst_windows=windows,
        organic=sorted(organic, key=lambda x: int(x.get("join_time") or 0)),
        burst_user_ids=burst_ids,
        notes=notes,
    )


def scan_channel(
    client: PredlozkaClient,
    chat_id: int,
    *,
    burst_threshold: int = 5,
    recent_hours: float | None = None,
) -> AudienceReport:
    chat = client.get_chat(int(chat_id))
    members = fetch_all_members(client, int(chat_id))
    return analyze_members(
        int(chat_id),
        str(chat.get("title") or chat_id),
        int(chat.get("participants_count") or 0),
        members,
        burst_threshold=burst_threshold,
        recent_hours=recent_hours,
    )


def scan_channel_full(
    client: PredlozkaClient,
    chat_id: int,
    *,
    burst_threshold: int = 5,
    recent_hours: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], AudienceReport]:
    """Один проход: chat + members + AudienceReport (без повторного fetch)."""
    chat = client.get_chat(int(chat_id))
    members = fetch_all_members(client, int(chat_id))
    report = analyze_members(
        int(chat_id),
        str(chat.get("title") or chat_id),
        int(chat.get("participants_count") or 0),
        members,
        burst_threshold=burst_threshold,
        recent_hours=recent_hours,
    )
    return chat, members, report


def remove_members(
    client: PredlozkaClient,
    chat_id: int,
    user_ids: list[int],
    *,
    block: bool = True,
    limit: int | None = None,
    sleep_sec: float = 0.35,
) -> dict[str, int]:
    """Удаляет участников. Возвращает ok/fail counts."""
    ids = list(user_ids)
    if limit is not None:
        ids = ids[: max(0, int(limit))]
    ok = 0
    fail = 0
    for uid in ids:
        try:
            client._request(
                "DELETE",
                f"/chats/{int(chat_id)}/members",
                params={"user_id": int(uid), "block": str(bool(block)).lower()},
            )
            ok += 1
        except Exception:
            fail += 1
        time.sleep(sleep_sec)
    return {"ok": ok, "fail": fail, "total": len(ids)}
