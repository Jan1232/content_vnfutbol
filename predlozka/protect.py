"""Превентивные эвристики анти-накрутки поверх MAX members API."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

EKAT = ZoneInfo("Asia/Yekaterinburg")

# Имена, типичные для ферм / авто-аккаунтов
_NAME_RE = re.compile(
    r"(?i)^(user\d+|id\d+|guest\d+|test\d+|\d{4,}|\W{1,3}|.)$"
)


@dataclass
class ProtectTargets:
    """Собранные цели на удаление + метаданные для отчёта."""

    by_reason: dict[str, list[int]] = field(default_factory=dict)
    storm_windows: list[tuple[str, int]] = field(default_factory=list)
    alert_windows: list[tuple[str, int]] = field(default_factory=list)
    fingerprint_hits: list[tuple[int, int, str]] = field(default_factory=list)

    def add(self, reason: str, user_ids: list[int]) -> None:
        ids = [int(uid) for uid in user_ids if int(uid) > 0]
        if not ids:
            return
        bucket = self.by_reason.setdefault(reason, [])
        seen = set(bucket)
        for uid_i in ids:
            if uid_i in seen:
                continue
            bucket.append(uid_i)
            seen.add(uid_i)

    def all_ids(self) -> list[int]:
        out: list[int] = []
        seen: set[int] = set()
        for ids in self.by_reason.values():
            for uid in ids:
                if uid not in seen:
                    out.append(uid)
                    seen.add(uid)
        return out

    def summary(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.by_reason.items()}


def _uid(m: dict[str, Any]) -> int:
    try:
        return int(m.get("user_id") or 0)
    except (TypeError, ValueError):
        return 0


def _is_protected(m: dict[str, Any]) -> bool:
    return bool(m.get("is_admin") or m.get("is_bot") or m.get("is_owner"))


def fingerprint_score(member: dict[str, Any]) -> tuple[int, list[str]]:
    """Мягкий fingerprint аккаунта по полям MAX API. Возвращает (score, flags)."""
    if _is_protected(member):
        return 0, []
    score = 0
    flags: list[str] = []
    if member.get("is_bot"):
        score += 5
        flags.append("is_bot")

    username = member.get("username")
    if "username" in member and not (username or "").strip():
        score += 1
        flags.append("no_username")

    first = str(member.get("first_name") or member.get("name") or "").strip()
    last = str(member.get("last_name") or "").strip()
    full = f"{first} {last}".strip()
    if not first:
        score += 2
        flags.append("empty_name")
    elif _NAME_RE.match(first) or _NAME_RE.match(full):
        score += 2
        flags.append("name_pattern")

    jt = int(member.get("join_time") or 0)
    if "last_activity_time" in member:
        la = member.get("last_activity_time")
        if la is None:
            score += 1
            flags.append("hidden_activity")
        else:
            try:
                la_i = int(la)
            except (TypeError, ValueError):
                la_i = 0
            if jt > 0 and la_i > 0 and abs(la_i - jt) <= 60_000:
                score += 1
                flags.append("activity_eq_join")

    if not member.get("avatar_url") and not member.get("full_avatar_url"):
        # поле может отсутствовать у живых — мягкий вес
        if "avatar_url" in member or "full_avatar_url" in member:
            score += 1
            flags.append("no_avatar")

    return score, flags


def find_circuit_windows(
    members: list[dict[str, Any]],
    *,
    alert_threshold: int = 30,
    storm_threshold: int = 50,
    recent_hours: float = 48.0,
) -> tuple[list[tuple[int, int, list[dict[str, Any]]]], list[tuple[int, int, list[dict[str, Any]]]]]:
    """
    Минуты с высокой скоростью входов.
    Возвращает (alert_windows, storm_windows), каждый элемент: (minute_key, count, members).
    """
    now_ms = int(time.time() * 1000)
    cut = now_ms - int(max(0.1, recent_hours) * 3600 * 1000)
    by_min: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for m in members:
        if _is_protected(m):
            continue
        jt = int(m.get("join_time") or 0)
        if jt <= 0 or jt < cut:
            continue
        by_min[jt // 60_000].append(m)

    alert: list[tuple[int, int, list[dict[str, Any]]]] = []
    storm: list[tuple[int, int, list[dict[str, Any]]]] = []
    for minute, group in sorted(by_min.items()):
        n = len(group)
        if n >= storm_threshold:
            storm.append((minute, n, group))
        elif n >= alert_threshold:
            alert.append((minute, n, group))
    return alert, storm


def expand_minute_members(
    members: list[dict[str, Any]],
    center_minutes: set[int],
    *,
    pad: int = 2,
) -> list[dict[str, Any]]:
    expanded = set()
    for k in center_minutes:
        for d in range(-pad, pad + 1):
            expanded.add(k + d)
    out: list[dict[str, Any]] = []
    for m in members:
        if _is_protected(m):
            continue
        jt = int(m.get("join_time") or 0)
        if jt and (jt // 60_000) in expanded:
            out.append(m)
    return out


def find_dead_subscribers(
    members: list[dict[str, Any]],
    *,
    min_age_hours: float = 24.0,
    max_age_hours: float = 72.0,
    activity_grace_ms: int = 5 * 60 * 1000,
) -> list[dict[str, Any]]:
    """
    Новички, у которых за min_age..max_age часов нет активности в MAX
    после подписки. Скрытый онлайн (нет last_activity_time) — не трогаем.
    """
    now_ms = int(time.time() * 1000)
    min_age = int(max(1.0, min_age_hours) * 3600 * 1000)
    max_age = int(max(min_age_hours, max_age_hours) * 3600 * 1000)
    out: list[dict[str, Any]] = []
    for m in members:
        if _is_protected(m):
            continue
        jt = int(m.get("join_time") or 0)
        if jt <= 0:
            continue
        age = now_ms - jt
        if age < min_age or age > max_age:
            continue
        if "last_activity_time" not in m:
            # поле вообще не пришло — не баним
            continue
        la = m.get("last_activity_time")
        if la is None:
            continue
        try:
            la_i = int(la)
        except (TypeError, ValueError):
            continue
        if la_i <= 0:
            continue
        if la_i <= jt + int(activity_grace_ms):
            out.append(m)
    return out


def find_fingerprint_suspects(
    members: list[dict[str, Any]],
    *,
    recent_hours: float = 48.0,
    min_score: int = 3,
) -> list[tuple[dict[str, Any], int, list[str]]]:
    now_ms = int(time.time() * 1000)
    cut = now_ms - int(max(0.1, recent_hours) * 3600 * 1000)
    hits: list[tuple[dict[str, Any], int, list[str]]] = []
    for m in members:
        if _is_protected(m):
            continue
        jt = int(m.get("join_time") or 0)
        if jt and jt < cut:
            continue
        score, flags = fingerprint_score(m)
        if score >= min_score:
            hits.append((m, score, flags))
    return hits


def collect_protect_targets(
    members: list[dict[str, Any]],
    *,
    bad_ids: set[int],
    burst_user_ids: list[int],
    alert_threshold: int = 30,
    storm_threshold: int = 50,
    recent_hours: float = 48.0,
    dead_min_hours: float = 24.0,
    dead_max_hours: float = 72.0,
    fingerprint_min_score: int = 3,
    enable_dead: bool = True,
    enable_fingerprint: bool = True,
) -> ProtectTargets:
    """Собирает цели: bad_db, burst, storm, dead, fingerprint."""
    targets = ProtectTargets()
    by_uid = {_uid(m): m for m in members if _uid(m)}

    # 1) уже известные боты из нашей базы, если ещё в канале
    present_bad = [uid for uid in bad_ids if uid in by_uid and not _is_protected(by_uid[uid])]
    if present_bad:
        targets.add("bad_db", present_bad)

    # 2) классический burst
    if burst_user_ids:
        targets.add("burst", [int(x) for x in burst_user_ids])

    # 3) circuit breaker
    alert_w, storm_w = find_circuit_windows(
        members,
        alert_threshold=alert_threshold,
        storm_threshold=storm_threshold,
        recent_hours=recent_hours,
    )
    targets.alert_windows = [(fmt_minute(m), n) for m, n, _ in alert_w]
    targets.storm_windows = [(fmt_minute(m), n) for m, n, _ in storm_w]

    if storm_w:
        storm_minutes = {m for m, _, _ in storm_w}
        storm_members = expand_minute_members(members, storm_minutes, pad=2)
        targets.add("storm", [_uid(m) for m in storm_members if _uid(m)])

    # 4) dead-sub honeypot
    if enable_dead:
        dead = find_dead_subscribers(
            members,
            min_age_hours=dead_min_hours,
            max_age_hours=dead_max_hours,
        )
        targets.add("dead_sub", [_uid(m) for m in dead if _uid(m)])

    # 5) soft fingerprint (не дублируем уже помеченных)
    if enable_fingerprint:
        already = set(targets.all_ids())
        for m, score, flags in find_fingerprint_suspects(
            members,
            recent_hours=recent_hours,
            min_score=fingerprint_min_score,
        ):
            uid = _uid(m)
            if not uid or uid in already:
                continue
            targets.add("fingerprint", [uid])
            targets.fingerprint_hits.append((uid, score, ",".join(flags)))

    return targets


def fmt_minute(minute_key: int) -> str:
    return datetime.fromtimestamp(minute_key * 60, EKAT).strftime("%d.%m %H:%M")
