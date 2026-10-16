"""Load per-channel SEO rules (like rules/channels for repost)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT, get_settings


@dataclass(frozen=True)
class SeoChannelConfig:
    slug: str
    chat_id: int
    link: str = ""
    # football-data codes and/or espn:<path>
    competitions: tuple[str, ...] = ("EL",)
    providers: tuple[str, ...] = ("auto",)  # auto | football-data | espn
    horizon_days: int = 45
    post_match_grace_min: int = 120
    target_community: str = "https://max.ru/channel_vnfutbol"
    title_suffix: str = "матч смотреть онлайн прямая трансляция бесплатно"
    idle_title: str = ""
    pin_post: bool = True
    notify_title_change: bool = False
    competition_label: str = ""
    competition_label_qual: str = ""
    # per-code overrides, e.g. {"WC": "Чемпионат мира FIFA"}
    competition_labels: tuple[tuple[str, str], ...] = ()
    competition_labels_qual: tuple[tuple[str, str], ...] = ()
    # hype | national_priority
    pick_mode: str = "hype"
    # турниры «первой очереди» для national_priority
    major_competitions: tuple[str, ...] = ()
    # [[Russia, Россия], [Spain, Испания], ...]
    priority_teams: tuple[tuple[str, ...], ...] = ()
    # окно приоритета сборных / хайпа от ближайшего матча (дней)
    priority_window_days: int = 4
    hype_window_days: int = 4
    enabled: bool = True

    def label_for(self, competition_code: str, *, qualifying: bool = False) -> str:
        code = (competition_code or "").upper()
        mapping = dict(
            self.competition_labels_qual if qualifying else self.competition_labels
        )
        if code in mapping and mapping[code]:
            return mapping[code]
        if qualifying and self.competition_label_qual:
            return self.competition_label_qual
        return self.competition_label or code


def _as_bool(val: Any, default: bool = True) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in {"1", "true", "yes", "on", "да"}:
        return True
    if s in {"0", "false", "no", "off", "нет"}:
        return False
    return default


def _as_tuple(val: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if val is None:
        return default
    if isinstance(val, str):
        return (val,)
    return tuple(str(x) for x in val)


def _as_str_map(val: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(val, dict):
        return ()
    out: list[tuple[str, str]] = []
    for k, v in val.items():
        if k is None or v is None:
            continue
        out.append((str(k).strip().upper(), str(v).strip()))
    return tuple(out)


def _as_priority_teams(val: Any) -> tuple[tuple[str, ...], ...]:
    if not val:
        return ()
    out: list[tuple[str, ...]] = []
    if isinstance(val, dict):
        # {Russia: [Россия, RF], ...} — порядок не гарантирован, лучше list
        for k, v in val.items():
            aliases = [str(k)]
            if isinstance(v, (list, tuple)):
                aliases.extend(str(x) for x in v)
            elif v:
                aliases.append(str(v))
            out.append(tuple(aliases))
        return tuple(out)
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str):
                out.append((item,))
            elif isinstance(item, (list, tuple)):
                out.append(tuple(str(x) for x in item if x))
        return tuple(out)
    return ()


def _parse(data: dict[str, Any]) -> SeoChannelConfig | None:
    if not data or "chat_id" not in data:
        return None
    comps = _as_tuple(data.get("competitions"), ("EL",))
    providers = _as_tuple(data.get("providers"), ("auto",))
    majors = _as_tuple(data.get("major_competitions"), ())
    pick_mode = str(data.get("pick_mode") or "hype").strip().lower()
    if pick_mode not in {"hype", "national_priority"}:
        pick_mode = "hype"
    return SeoChannelConfig(
        slug=str(data.get("slug") or data["chat_id"]),
        chat_id=int(data["chat_id"]),
        link=str(data.get("link") or ""),
        competitions=tuple(
            c.upper() if not str(c).startswith("espn:") else str(c) for c in comps
        ),
        providers=tuple(p.lower() for p in providers),
        horizon_days=int(data.get("horizon_days") or 45),
        post_match_grace_min=int(data.get("post_match_grace_min") or 120),
        target_community=str(
            data.get("target_community") or "https://max.ru/channel_vnfutbol"
        ),
        title_suffix=str(
            data.get("title_suffix")
            or "матч смотреть онлайн прямая трансляция бесплатно"
        ),
        idle_title=str(data.get("idle_title") or ""),
        pin_post=_as_bool(data.get("pin_post"), True),
        notify_title_change=_as_bool(data.get("notify_title_change"), False),
        competition_label=str(data.get("competition_label") or ""),
        competition_label_qual=str(data.get("competition_label_qual") or ""),
        competition_labels=_as_str_map(data.get("competition_labels")),
        competition_labels_qual=_as_str_map(data.get("competition_labels_qual")),
        pick_mode=pick_mode,
        major_competitions=tuple(c.upper() for c in majors),
        priority_teams=_as_priority_teams(data.get("priority_teams")),
        priority_window_days=int(data.get("priority_window_days") or 4),
        hype_window_days=int(
            data.get("hype_window_days")
            or data.get("priority_window_days")
            or 4
        ),
        enabled=_as_bool(data.get("enabled"), True),
    )


def channels_dir() -> Path:
    settings = get_settings()
    directory = Path(settings.seo_channels_dir)
    if not directory.is_dir():
        directory = ROOT / "seo" / "channels"
    return directory


@lru_cache
def load_seo_channels() -> tuple[SeoChannelConfig, ...]:
    directory = channels_dir()
    out: list[SeoChannelConfig] = []
    if not directory.is_dir():
        return ()
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            continue
        cfg = _parse(raw)
        if cfg and cfg.enabled:
            out.append(cfg)
    return tuple(out)


def reload_seo_channels() -> None:
    load_seo_channels.cache_clear()
