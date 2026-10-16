"""Загрузка ядра правил + опциональных оверрайдов каналов и источников.

Структура:
  rules/core.yaml              — для всех каналов
  rules/channels/<slug>.yaml   — дополняет/меняет ядро под канал или источник

Пример оверрайда (rules/channels/goroskop.yaml):

    chat_id: -77021716331226
    # или: chat_ids / titles / title / link_contains
    require_media: false

Источник (не весь канал) — добавить url_contains. Тогда оверрайд
срабатывает только если совпали канал и URL источника.
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
    # Только посты с файлами-документами (JPG и т.п.), не обычные фото/видео.
    documents_only: bool = False
    # Подмена текста поста при публикации (пустая строка = не трогать).
    fixed_caption: str = ""
    # После поста с картинками сразу отправить те же исходники как файлы.
    also_send_files: bool = False
    # Пропускать посты с матом (в дополнение к фильтру рекламы).
    block_profanity: bool = False
    # Вырезать строки, содержащие эти подстроки (только для совпавшего источника/канала).
    strip_line_contains: tuple[str, ...] = ()
    # Вырезать строку, если в ней только эта подпись (без остального текста).
    strip_exact_lines: tuple[str, ...] = ()
    # Голые домены вроде will.i.am считать ссылкой и вырезать.
    strip_bare_domains: bool = True
    # Блокировать пост целиком, если текст содержит любую из этих подстрок.
    block_text_contains: tuple[str, ...] = ()
    # Только посты с видео (фото/текст отбрасываем).
    require_video: bool = False
    # Публиковать без текста (только медиа).
    drop_caption: bool = False
    # Брать историю источника (не пропускать старые посты при первом обходе).
    archive_drip: bool = False
    # Лимит публикаций в сутки (0 = без лимита), пауза между постами.
    max_per_day: int = 0
    min_gap_sec: int = 0
    max_gap_sec: int = 0
    # Фиксированные слоты "HH:MM" (локально в tz_name). Пусто = обычная капельница.
    publish_times: tuple[str, ...] = ()
    tz_name: str = "Europe/Moscow"


@dataclass(frozen=True)
class _Override:
    chat_ids: frozenset[int]
    titles: frozenset[str]
    link_contains: tuple[str, ...]
    url_contains: tuple[str, ...]
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


def _as_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    return str(val).strip()


def _as_int(val: Any, default: int = 0) -> int:
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _as_publish_times(val: Any, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if not val:
        return default
    raw = [val] if isinstance(val, str) else list(val)
    out: list[str] = []
    for item in raw:
        s = str(item).strip()
        parts = s.split(":")
        if len(parts) < 2:
            continue
        try:
            h = int(parts[0])
            m = int(parts[1])
        except ValueError:
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            out.append(f"{h:02d}:{m:02d}")
    return tuple(out) if out else default


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _as_contains(val: Any) -> tuple[str, ...]:
    if not val:
        return ()
    if isinstance(val, str):
        return (val.casefold(),)
    return tuple(str(x).casefold() for x in val)


def _rules_from_dict(data: dict[str, Any], base: ChannelRules) -> ChannelRules:
    mode = _as_mode(data.get("moderation_mode"), base.moderation_mode)
    require_media = _as_bool(data.get("require_media"), base.require_media)
    documents_only = _as_bool(data.get("documents_only"), base.documents_only)
    fixed_caption = _as_str(data.get("fixed_caption"), base.fixed_caption)
    also_send_files = _as_bool(data.get("also_send_files"), base.also_send_files)
    block_profanity = _as_bool(data.get("block_profanity"), base.block_profanity)
    strip_line_contains = _as_contains(
        data.get("strip_line_contains") if "strip_line_contains" in data else data.get("strip_lines")
    )
    if not strip_line_contains:
        strip_line_contains = base.strip_line_contains
    strip_exact_lines = _as_contains(data.get("strip_exact_lines"))
    if not strip_exact_lines:
        strip_exact_lines = base.strip_exact_lines
    strip_bare_domains = _as_bool(data.get("strip_bare_domains"), base.strip_bare_domains)
    block_text_contains = _as_contains(data.get("block_text_contains"))
    if not block_text_contains:
        block_text_contains = base.block_text_contains
    require_video = _as_bool(data.get("require_video"), base.require_video)
    drop_caption = _as_bool(data.get("drop_caption"), base.drop_caption)
    archive_drip = _as_bool(data.get("archive_drip"), base.archive_drip)
    max_per_day = _as_int(data.get("max_per_day"), base.max_per_day)
    min_gap_sec = _as_int(data.get("min_gap_sec"), base.min_gap_sec)
    max_gap_sec = _as_int(data.get("max_gap_sec"), base.max_gap_sec)
    if max_gap_sec and min_gap_sec and max_gap_sec < min_gap_sec:
        max_gap_sec = min_gap_sec
    publish_times = _as_publish_times(
        data.get("publish_times") if "publish_times" in data else data.get("slots"),
        base.publish_times,
    )
    tz_name = _as_str(data.get("tz") or data.get("tz_name"), base.tz_name) or base.tz_name
    if require_video:
        require_media = True
    return ChannelRules(
        moderation_mode=mode,
        require_media=require_media,
        documents_only=documents_only,
        fixed_caption=fixed_caption,
        also_send_files=also_send_files,
        block_profanity=block_profanity,
        strip_line_contains=strip_line_contains,
        strip_exact_lines=strip_exact_lines,
        strip_bare_domains=strip_bare_domains,
        block_text_contains=block_text_contains,
        require_video=require_video,
        drop_caption=drop_caption,
        archive_drip=archive_drip,
        max_per_day=max(0, max_per_day),
        min_gap_sec=max(0, min_gap_sec),
        max_gap_sec=max(0, max_gap_sec),
        publish_times=publish_times,
        tz_name=tz_name,
    )


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

    link_parts = _as_contains(data.get("link_contains"))
    url_parts = _as_contains(
        data.get("url_contains") or data.get("source_url") or data.get("source_urls")
    )

    if not chat_ids and not titles and not link_parts and not url_parts:
        return None

    return _Override(
        chat_ids=frozenset(chat_ids),
        titles=frozenset(titles),
        link_contains=link_parts,
        url_contains=url_parts,
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


def _channel_match(
    ov: _Override,
    *,
    chat_id: int | None,
    title_cf: str,
    link_cf: str,
) -> bool:
    if chat_id is not None and chat_id in ov.chat_ids:
        return True
    if title_cf and title_cf in ov.titles:
        return True
    if link_cf and any(part in link_cf for part in ov.link_contains):
        return True
    return False


def resolve_rules(
    *,
    chat_id: int | None = None,
    title: str | None = None,
    link: str | None = None,
    source_url: str | None = None,
) -> ChannelRules:
    """Ядро + оверрайд. Сначала правила источника (url_contains), потом канала."""
    core = load_core_rules()
    title_cf = (title or "").strip().casefold()
    link_cf = (link or "").strip().casefold()
    source_cf = (source_url or "").strip().casefold()
    overrides = load_channel_overrides()

    for ov in overrides:
        if not ov.url_contains:
            continue
        if not source_cf or not any(part in source_cf for part in ov.url_contains):
            continue
        has_channel = bool(ov.chat_ids or ov.titles or ov.link_contains)
        if has_channel and not _channel_match(
            ov, chat_id=chat_id, title_cf=title_cf, link_cf=link_cf
        ):
            continue
        return ov.rules

    for ov in overrides:
        if ov.url_contains:
            continue
        if _channel_match(ov, chat_id=chat_id, title_cf=title_cf, link_cf=link_cf):
            return ov.rules
    return core


def sync_rules_to_db(conn: Any) -> None:
    """Проставляет moderation_mode всем каналам и источникам из YAML.

    require_media не трогаем: его задаёт чекбокс в админке.
    """
    rows = conn.execute("SELECT chat_id, title, link FROM channels").fetchall()
    for row in rows:
        chat_id = int(row["chat_id"])
        title = row["title"] or ""
        link = row["link"] or ""
        rules = resolve_rules(chat_id=chat_id, title=title, link=link)
        conn.execute(
            """
            UPDATE channels
            SET moderation_mode=?
            WHERE chat_id=?
            """,
            (rules.moderation_mode, chat_id),
        )
        sources = conn.execute(
            "SELECT id, url FROM sources WHERE chat_id=?",
            (chat_id,),
        ).fetchall()
        for src in sources:
            src_rules = resolve_rules(
                chat_id=chat_id,
                title=title,
                link=link,
                source_url=src["url"] or "",
            )
            conn.execute(
                """
                UPDATE sources
                SET moderation_mode=?
                WHERE id=?
                """,
                (
                    src_rules.moderation_mode,
                    int(src["id"]),
                ),
            )


def reload_rules() -> None:
    """Сброс кэша (после правки YAML без рестарта процесса — для тестов/админки)."""
    load_core_rules.cache_clear()
    load_channel_overrides.cache_clear()
