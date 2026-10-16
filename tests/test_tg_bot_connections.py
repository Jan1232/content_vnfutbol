"""Разбор username TG и отбор чатов, куда бот реально добавлен."""

from __future__ import annotations

from parsers.telegram import telegram_username_from_url
from workers.run import _TG_MEMBER_OK


def test_username_from_public_links() -> None:
    assert telegram_username_from_url("https://t.me/s/thesoccerblogteam") == "thesoccerblogteam"
    assert telegram_username_from_url("https://t.me/kino_filmach") == "kino_filmach"
    assert telegram_username_from_url("@hent_animart") == "hent_animart"
    assert telegram_username_from_url("hent_animart") == "hent_animart"


def test_username_skips_private_and_service() -> None:
    assert telegram_username_from_url("https://t.me/c/1234567890/10") is None
    assert telegram_username_from_url("https://t.me/joinchat/AAAA") is None
    assert telegram_username_from_url("https://example.com/foo") is None
    assert telegram_username_from_url("") is None


def test_member_statuses() -> None:
    assert "administrator" in _TG_MEMBER_OK
    assert "kicked" not in _TG_MEMBER_OK
    assert "left" not in _TG_MEMBER_OK


if __name__ == "__main__":
    test_username_from_public_links()
    test_username_skips_private_and_service()
    test_member_statuses()
    print("OK tg bot connections helpers")
