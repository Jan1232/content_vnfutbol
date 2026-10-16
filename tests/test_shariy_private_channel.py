"""Фильтр «частный канал» только для Шария."""

from __future__ import annotations

from app.filter import is_advertisement
from app.rules import reload_rules, resolve_rules

SHARIY = dict(
    chat_id=-77431404215002,
    title="Шарий СВО новости",
    link="https://max.ru/channel_shariy_news",
    source_url="https://t.me/s/ASupersharij",
)

ANATOL = dict(
    chat_id=-79631652207322,
    title="Шарий",
    link="https://max.ru/channel_anatol_shary",
    source_url="https://t.me/s/ASupersharij",
)

FOOTBALL = dict(
    chat_id=-240916179,
    title="ВСЕ НА ФУТБОЛ",
    link="https://vk.com/club240916179",
)

BLOCKED = [
    "Разбор в частном, там без цензуры",
    "Полный разбор в ЧАСТНОМ канале",
    "Смотрите в частный тг",
    "Подробности в частный канал",
    "Это частный тг канал, там продолжение",
    "Это частный канал без купюр",
]


def test_rule_only_for_shariy() -> None:
    reload_rules()
    shariy = resolve_rules(**SHARIY)
    assert shariy.require_media is False
    assert "в частном" in shariy.block_text_contains
    assert "частный канал" in shariy.block_text_contains
    assert "частный тг канал" in shariy.block_text_contains
    assert "в частный" in shariy.block_text_contains

    other = resolve_rules(**FOOTBALL)
    assert other.block_text_contains == ()

    anatol = resolve_rules(**ANATOL)
    assert anatol.require_media is False
    assert anatol.block_text_contains == ()


def test_shariy_blocks_private_channel_phrases() -> None:
    reload_rules()
    phrases = resolve_rules(**SHARIY).block_text_contains
    for text in BLOCKED:
        fr = is_advertisement(text, mode="strip", block_text_contains=phrases)
        assert fr.is_ad and fr.reason.startswith("блок канала:"), (text, fr)


def test_other_channels_keep_same_text() -> None:
    reload_rules()
    phrases = resolve_rules(**FOOTBALL).block_text_contains
    for text in BLOCKED:
        fr = is_advertisement(text, mode="strip", block_text_contains=phrases)
        assert not fr.is_ad, (text, fr)


def test_shariy_regular_news_passes() -> None:
    reload_rules()
    phrases = resolve_rules(**SHARIY).block_text_contains
    text = "БПЛА атаковали порт. Повреждён театр имени Гамзата Цадасы."
    fr = is_advertisement(text, mode="strip", block_text_contains=phrases)
    assert not fr.is_ad, fr


if __name__ == "__main__":
    test_rule_only_for_shariy()
    print("OK shariy rule scoped")
    test_shariy_blocks_private_channel_phrases()
    print("OK blocked phrases")
    test_other_channels_keep_same_text()
    print("OK other channels untouched")
    test_shariy_regular_news_passes()
    print("OK news still passes")
