"""Загрузка ядра правил + опциональных оверрайдов каналов.

Структура:
  rules/core.yaml              — для всех каналов
  rules/channels/<slug>.yaml   — дополняет/меняет ядро под канал

Пример оверрайда (rules/channels/goroskop.yaml):

    chat_id: -77021716331226
    # или: chat_ids / titles / title / link_contains
    require_media: false
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT

RULES_DIR = ROOT / "rules"
CORE_PATH = RULES_DIR / "core.yaml"
CHANNELS_DIR = RULES_DIR / "channels"


@dataclass(frozen=True)
class ChannelRules:
    moderation_mode: str = "strip"
    require_media: bool = True


@dataclass(frozen=True)
class _Override:
    chat_ids: frozenset[int]
    titles: frozenset[str]
    link_contains: tuple[str, ...]
    rules: ChannelRules


def _as_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in {"1", "true", "yes", "on", "да"}:
        return True
    if s in {"0", "false", "no", "off", "нет"}:
        return False
    return default


def _as_mode(val: Any, default: str = "strip") -> str:
    mode = str(val or default).strip().lower()
    return mode if mode in {"strip", "strict"} else default


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _rules_from_dict(data: dict[str, Any], base: ChannelRules) -> ChannelRules:
    mode = _as_mode(data.get("moderation_mode"), base.moderation_mode)
    require_media = _as_bool(data.get("require_media"), base.require_media)
    return ChannelRules(moderation_mode=mode, require_media=require_media)


@lru_cache
def load_core_rules() -> ChannelRules:
    data = _load_yaml(CORE_PATH)
    return _rules_from_dict(data, ChannelRules())


def _parse_override(path: Path, core: ChannelRules) -> _Override | None:
    data = _load_yaml(path)
    if not data:
        return None

    chat_ids: set[int] = set()
    if "chat_id" in data and data["chat_id"] is not None:
        chat_ids.add(int(data["chat_id"]))
    for x in data.get("chat_ids") or []:
        chat_ids.add(int(x))

    titles: set[str] = set()
    if data.get("title"):
        titles.add(str(data["title"]).strip().casefold())
    for t in data.get("titles") or []:
        titles.add(str(t).strip().casefold())

    link_parts: list[str] = []
    if data.get("link_contains"):
        raw = data["link_contains"]
        if isinstance(raw, str):
            link_parts.append(raw.casefold())
        else:
            link_parts.extend(str(x).casefold() for x in raw)

    if not chat_ids and not titles and not link_parts:
        return None

    return _Override(
        chat_ids=frozenset(chat_ids),
        titles=frozenset(titles),
        link_contains=tuple(link_parts),
        rules=_rules_from_dict(data, core),
    )


@lru_cache
def load_channel_overrides() -> tuple[_Override, ...]:
    core = load_core_rules()
    if not CHANNELS_DIR.is_dir():
        return ()
    out: list[_Override] = []
    for path in sorted(CHANNELS_DIR.glob("*.yaml")):
        ov = _parse_override(path, core)
        if ov:
            out.append(ov)
    return tuple(out)


def resolve_rules(
    *,
    chat_id: int | None = None,
    title: str | None = None,
    link: str | None = None,
) -> ChannelRules:
    """Ядро + первый совпавший оверрайд канала (если есть)."""
    core = load_core_rules()
    title_cf = (title or "").strip().casefold()
    link_cf = (link or "").strip().casefold()

    for ov in load_channel_overrides():
        if chat_id is not None and chat_id in ov.chat_ids:
            return ov.rules
        if title_cf and title_cf in ov.titles:
            return ov.rules
        if link_cf and any(part in link_cf for part in ov.link_contains):
            return ov.rules
    return core


def sync_rules_to_db(conn: Any) -> None:
    """Проставляет moderation_mode / require_media всем каналам и источникам из YAML."""
    rows = conn.execute("SELECT chat_id, title, link FROM channels").fetchall()
    for row in rows:
        chat_id = int(row["chat_id"])
        rules = resolve_rules(
            chat_id=chat_id,
            title=row["title"] or "",
            link=row["link"] or "",
        )
        conn.execute(
            """
            UPDATE channels
            SET moderation_mode=?, require_media=?
            WHERE chat_id=?
            """,
            (rules.moderation_mode, 1 if rules.require_media else 0, chat_id),
        )
        conn.execute(
            """
            UPDATE sources
            SET moderation_mode=?, require_media=?
            WHERE chat_id=?
            """,
            (rules.moderation_mode, 1 if rules.require_media else 0, chat_id),
        )


def reload_rules() -> None:
    """Сброс кэша (после правки YAML без рестарта процесса — для тестов/админки)."""
    load_core_rules.cache_clear()
    load_channel_overrides.cache_clear()
