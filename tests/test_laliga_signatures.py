"""Хвост «LA LIGA • Наши Каналы»: правило источника + глобальный sanitize."""

from __future__ import annotations

from app.filter import sanitize_for_publish
from app.rules import reload_rules, resolve_rules

RAW = (
    "🚨🇪🇸 Педри теперь забил БОЛЬШЕ голов, чем Кроос, в Ла Лиге\n"
    "\n"
    "🏆 LA LIGA • Наши Каналы"
)


def test_rule_only_for_laliga_source() -> None:
    reload_rules()
    src = resolve_rules(
        chat_id=-72882480536147,
        title="Ла Лига - Испанский Футбол",
        link="https://max.ru/channel_Laliga_2026",
        source_url="https://t.me/s/LaLiga_telegram",
    )
    assert "наши каналы" in src.strip_line_contains

    other = resolve_rules(
        chat_id=-72882480536147,
        title="Ла Лига - Испанский Футбол",
        link="https://max.ru/channel_Laliga_2026",
        source_url="https://t.me/s/thesoccerblogteam",
    )
    assert "наши каналы" not in other.strip_line_contains


def test_sanitize_strips_laliga_channels_footer_globally() -> None:
    cleaned = sanitize_for_publish(RAW)
    assert "Наши Каналы" not in cleaned
    assert "Педри" in cleaned

    also = sanitize_for_publish(RAW, extra_strip_contains=("наши каналы",))
    assert "Наши Каналы" not in also
    assert "Педри" in also


if __name__ == "__main__":
    test_rule_only_for_laliga_source()
    print("OK rule scoped to LaLiga_telegram")
    test_sanitize_strips_laliga_channels_footer_globally()
    print("OK sanitize global our-channels strip")
