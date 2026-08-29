"""Load per-channel editorial configs (mirrors seo/channel_config.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT, get_settings


@dataclass(frozen=True)
class EditorialFeed:
    name: str
    kind: str = "rss"
    url: str = ""
    endpoint: str = ""
    handle: str = ""
    take_only: tuple[str, ...] = ()
    rewrite_text: bool = False
    profanity_gate: str = ""
    profanity_mode: str = ""
    preserve_quotes: bool = False
    max_per_day: int = 0  # 0 = fallback на глобальный meme_source_max_per_day
    wrap_template: bool = False


@dataclass(frozen=True)
class ModerationConfig:
    """Очередь TG-модерации."""

    queue_depth: int = 3
    auto_publish_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class CadenceConfig:
    min_gap_min: int = 40
    max_gap_min: int = 55
    priority_bypass: bool = True
    item_ttl_sec: int = 10800


@dataclass(frozen=True)
class EditorialBrand:
    name: str = ""
    logo: str = ""
    accent_color: str = "#E11D2A"
    cover_handle: str = "@channel_vnfutbol"


@dataclass(frozen=True)
class EditorialCta:
    text: str = ""
    url: str = ""


@dataclass(frozen=True)
class MatchdayConfig:
    enabled: bool = True
    group_order: tuple[str, ...] = ("CL", "EL", "NT", "PL", "PD", "SA", "BL", "FL1", "RPL")
    grands: bool = True
    all_cl_el: bool = True
    national_top100: bool = True
    national_always: tuple[str, ...] = ("Russia",)


@dataclass(frozen=True)
class ResultsConfig:
    enabled: bool = True
    significant_only: bool = True


@dataclass(frozen=True)
class TelegramMirrorConfig:
    enabled: bool = False
    channel: str = ""
    preview_handle: str = "@vnfutbol"


@dataclass(frozen=True)
class EditorialChannelConfig:
    slug: str
    chat_id: int
    enabled: bool = True
    feeds: tuple[EditorialFeed, ...] = ()
    competitions: tuple[str, ...] = ()
    event_types: tuple[str, ...] = (
        "transfer",
        "injury",
        "match_result",
        "official_statement",
        "lifestyle",
        "meme",
    )
    allow_rumors: bool = False
    factcheck_min_sources: int = 2
    image_rights_ack: bool = False
    cadence: CadenceConfig = field(default_factory=CadenceConfig)
    moderation: ModerationConfig = field(default_factory=ModerationConfig)
    always_priority_teams: tuple[str, ...] = ("Russia",)
    template_map: tuple[tuple[str, str], ...] = ()
    brand: EditorialBrand = field(default_factory=EditorialBrand)
    cta: EditorialCta = field(default_factory=EditorialCta)
    poll_interval_sec: int = 60
    # true = не слать в MAX, складывать готовую карточку в ленту источника в админке
    dry_run: bool = True
    # true = готовый пост ждёт ручного approve в админке перед отправкой в MAX
    moderate_before_publish: bool = False
    matchday: MatchdayConfig = field(default_factory=MatchdayConfig)
    results: ResultsConfig = field(default_factory=ResultsConfig)
    telegram_mirror: TelegramMirrorConfig = field(default_factory=TelegramMirrorConfig)

    def template_for(self, event_type: str) -> str:
        mapping = dict(self.template_map)
        return mapping.get(event_type) or mapping.get("other") or "default"


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


def _as_tuple(val: Any, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if val is None:
        return default
    if isinstance(val, str):
        return (val,)
    return tuple(str(x) for x in val)


def _parse_feeds(raw: Any) -> tuple[EditorialFeed, ...]:
    if not raw:
        return ()
    out: list[EditorialFeed] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "rss").strip().lower()
        name = str(item.get("name") or item.get("url") or item.get("endpoint") or kind)
        is_meme_tg = kind == "telegram"
        rewrite_default = True if is_meme_tg else False
        profanity_default = "soften" if is_meme_tg else ""
        out.append(
            EditorialFeed(
                name=name,
                kind=kind,
                url=str(item.get("url") or ""),
                endpoint=str(item.get("endpoint") or ""),
                handle=str(item.get("handle") or ""),
                take_only=tuple(str(x) for x in (item.get("take_only") or ())),
                rewrite_text=_as_bool(item.get("rewrite_text"), rewrite_default),
                profanity_gate=str(item.get("profanity_gate") or profanity_default),
                profanity_mode=str(item.get("profanity_mode") or item.get("profanity_gate") or profanity_default),
                preserve_quotes=_as_bool(item.get("preserve_quotes"), False),
                max_per_day=int(item.get("max_per_day") or 0),
                wrap_template=_as_bool(item.get("wrap_template"), False),
            )
        )
    return tuple(out)


def _parse_moderation(raw: Any, settings: Any) -> ModerationConfig:
    data = raw if isinstance(raw, dict) else {}
    depth = data.get("queue_depth")
    if depth is None:
        depth = getattr(settings, "moderation_queue_depth", 3)
    return ModerationConfig(
        queue_depth=max(1, int(depth or 3)),
        auto_publish_types=_as_tuple(data.get("auto_publish_types"), ()),
    )


def _parse_cadence(raw: Any, settings: Any) -> CadenceConfig:
    data = raw if isinstance(raw, dict) else {}
    return CadenceConfig(
        min_gap_min=int(data.get("min_gap_min") or settings.editorial_min_gap_min or 40),
        max_gap_min=int(data.get("max_gap_min") or settings.editorial_max_gap_min or 55),
        priority_bypass=_as_bool(data.get("priority_bypass"), True),
        item_ttl_sec=int(data.get("item_ttl_sec") or settings.editorial_item_ttl_sec or 10800),
    )


def _parse(data: dict[str, Any]) -> EditorialChannelConfig | None:
    if not data or "chat_id" not in data:
        return None
    settings = get_settings()
    brand_raw = data.get("brand") if isinstance(data.get("brand"), dict) else {}
    cta_raw = data.get("cta") if isinstance(data.get("cta"), dict) else {}
    tmap = data.get("template_map") if isinstance(data.get("template_map"), dict) else {}
    md_raw = data.get("matchday") if isinstance(data.get("matchday"), dict) else {}
    inc = md_raw.get("include") if isinstance(md_raw.get("include"), dict) else {}
    res_raw = data.get("results") if isinstance(data.get("results"), dict) else {}
    tg_raw = data.get("telegram_mirror") if isinstance(data.get("telegram_mirror"), dict) else {}
    always = _as_tuple(data.get("always_priority_teams"), ("Russia",))
    tg_channel = str(tg_raw.get("channel") or settings.telegram_content_channel or "")
    tg_enabled = _as_bool(tg_raw.get("enabled"), False) and bool(
        (settings.telegram_content_bot_token or "").strip() and tg_channel
    )
    return EditorialChannelConfig(
        slug=str(data.get("slug") or data["chat_id"]),
        chat_id=int(data["chat_id"]),
        enabled=_as_bool(data.get("enabled"), True),
        feeds=_parse_feeds(data.get("feeds")),
        competitions=tuple(c.upper() for c in _as_tuple(data.get("competitions"), ())),
        event_types=_as_tuple(
            data.get("event_types"),
            ("transfer", "injury", "match_result", "official_statement", "lifestyle", "meme"),
        ),
        allow_rumors=_as_bool(data.get("allow_rumors"), False),
        factcheck_min_sources=int(
            data.get("factcheck_min_sources") or settings.factcheck_min_sources or 2
        ),
        image_rights_ack=_as_bool(data.get("image_rights_ack"), False),
        cadence=_parse_cadence(data.get("cadence"), settings),
        moderation=_parse_moderation(data.get("moderation"), settings),
        always_priority_teams=always,
        template_map=tuple((str(k), str(v)) for k, v in tmap.items()),
        brand=EditorialBrand(
            name=str(brand_raw.get("name") or ""),
            logo=str(brand_raw.get("logo") or ""),
            accent_color=str(brand_raw.get("accent_color") or "#E11D2A"),
            cover_handle=str(brand_raw.get("cover_handle") or "@channel_vnfutbol"),
        ),
        cta=EditorialCta(
            text=str(cta_raw.get("text") or ""),
            url=str(cta_raw.get("url") or ""),
        ),
        poll_interval_sec=int(
            data.get("poll_interval_sec") or settings.editorial_poll_interval_sec or 60
        ),
        dry_run=_as_bool(data.get("dry_run"), True),
        moderate_before_publish=_as_bool(data.get("moderate_before_publish"), False),
        matchday=MatchdayConfig(
            enabled=_as_bool(md_raw.get("enabled"), True),
            group_order=_as_tuple(md_raw.get("group_order"), ("CL", "EL", "NT", "PL", "PD", "SA", "BL", "FL1", "RPL")),
            grands=_as_bool(inc.get("grands"), True),
            all_cl_el=_as_bool(inc.get("all_cl_el"), True),
            national_top100=_as_bool(inc.get("national_top100"), True),
            national_always=_as_tuple(inc.get("national_always"), always),
        ),
        results=ResultsConfig(
            enabled=_as_bool(res_raw.get("enabled"), True),
            significant_only=_as_bool(res_raw.get("significant_only"), True),
        ),
        telegram_mirror=TelegramMirrorConfig(
            enabled=tg_enabled,
            channel=tg_channel,
            preview_handle=str(tg_raw.get("preview_handle") or "@vnfutbol"),
        ),
    )


def brand_render_context(
    channel: EditorialChannelConfig,
    *,
    for_telegram: bool = False,
) -> dict[str, str]:
    handle = channel.brand.cover_handle
    if for_telegram and channel.telegram_mirror.preview_handle:
        handle = channel.telegram_mirror.preview_handle
    return {
        "name": channel.brand.name,
        "logo": channel.brand.logo,
        "accent_color": channel.brand.accent_color,
        "cover_handle": handle,
    }


def channels_dir() -> Path:
    settings = get_settings()
    directory = Path(settings.editorial_channels_dir)
    if not directory.is_dir():
        directory = ROOT / "editorial" / "channels"
    return directory


@lru_cache
def load_editorial_channels(*, include_disabled: bool = False) -> tuple[EditorialChannelConfig, ...]:
    directory = channels_dir()
    out: list[EditorialChannelConfig] = []
    if not directory.is_dir():
        return ()
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            continue
        cfg = _parse(raw)
        if not cfg:
            continue
        if cfg.enabled or include_disabled:
            out.append(cfg)
    return tuple(out)


def reload_editorial_channels() -> None:
    load_editorial_channels.cache_clear()


def get_channel(slug: str) -> EditorialChannelConfig | None:
    reload_editorial_channels()
    for cfg in load_editorial_channels(include_disabled=True):
        if cfg.slug == slug:
            return cfg
    return None
