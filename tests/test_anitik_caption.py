"""AniTik: трек остаётся, подпись AniTik вырезается."""

from __future__ import annotations

from app.filter import sanitize_for_publish
from app.rules import reload_rules, resolve_rules

ANITIK = dict(
    chat_id=-79631407823578,
    title="Аниме эдиты",
    link="https://max.ru/channel_animedit",
    source_url="https://t.me/s/Anitik_edits",
)

OTHER = dict(
    chat_id=-240916179,
    title="ВСЕ НА ФУТБОЛ",
    link="https://vk.com/club240916179",
    source_url="https://t.me/s/Anitik_edits",
)

RAW = """🔔Аниме: Наруто

🎧Трек: will.i.am - Big and Chunky
🔗Автор: oni__kage
https://open.spotify.com/track/abc

AniTik"""


def test_anitik_keeps_track_and_drops_signature() -> None:
    reload_rules()
    rules = resolve_rules(**ANITIK)
    assert rules.strip_bare_domains is False
    assert "anitik" in rules.strip_exact_lines
    cleaned = sanitize_for_publish(
        RAW,
        extra_strip_contains=rules.strip_line_contains,
        exact_strip_lines=rules.strip_exact_lines,
        strip_bare_domains=rules.strip_bare_domains,
    )
    assert "will.i.am - Big and Chunky" in cleaned
    assert "🎧Трек:" in cleaned
    assert "open.spotify.com" not in cleaned
    assert "anitik" not in cleaned.casefold()
    assert "oni__kage" in cleaned


def test_other_channel_still_strips_bare_domain() -> None:
    reload_rules()
    rules = resolve_rules(**OTHER)
    assert rules.strip_bare_domains is True
    cleaned = sanitize_for_publish(RAW)
    assert "will.i.am" not in cleaned
    assert "Big and Chunky" in cleaned


if __name__ == "__main__":
    test_anitik_keeps_track_and_drops_signature()
    print("OK anitik track")
    test_other_channel_still_strips_bare_domain()
    print("OK other channels")
