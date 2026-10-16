from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from content.parser import parse_catalog_item, referral_url
from content.publish import format_genres, format_post_text, seasons_label
from content.schedule import is_quiet, next_publish_at, next_window_start


def test_referral_appends_query() -> None:
    url = referral_url("https://tvoe.live/p/dezhurstvo", ref="abc")
    assert url == "https://tvoe.live/p/dezhurstvo?r=abc"


def test_referral_replaces_existing() -> None:
    url = referral_url("https://tvoe.live/p/x?foo=1&r=old", ref="new")
    assert "r=new" in url
    assert "foo=1" in url
    assert "r=old" not in url


def test_seasons_label() -> None:
    assert seasons_label(1) == "1 сезон"
    assert seasons_label(2) == "2 сезона"
    assert seasons_label(5) == "5 сезонов"
    assert seasons_label(21) == "21 сезон"
    assert seasons_label(None) == ""


def test_parse_catalog_item() -> None:
    item = parse_catalog_item(
        {
            "_id": "66b5e2303c0ca13912e9b57d",
            "name": "Дежурство",
            "shortDesc": "Детектив на лодке.",
            "dateReleased": "2021-01-14",
            "seasonsCount": 3,
            "poster": {"src": "/images/poster.jpg"},
            "url": "/p/dezhurstvo",
            "genreNames": ["Триллеры", "Драмы", "Криминал"],
        }
    )
    assert item is not None
    assert item.title == "Дежурство"
    assert item.year == 2021
    assert item.seasons == 3
    assert item.url == "https://tvoe.live/p/dezhurstvo"
    assert item.poster_url.endswith("/images/poster.jpg")
    text = format_post_text(item)
    assert "🎥 <b>Сериал: Дежурство</b>" in text
    assert "📊 2021 · 3 сезона" in text
    assert text.index("Сериал: Дежурство") < text.index("📊")
    assert item.genres == ("Триллеры", "Драмы", "Криминал")
    assert "🎭 Жанр: триллеры, драмы, криминал" in text
    assert "💬 Детектив на лодке." in text


def test_genre_block() -> None:
    assert format_genres(("Комедии", "Драмы")) == "🎭 Жанр: комедии, драмы"


def test_quiet_hours_msk() -> None:
    msk = ZoneInfo("Europe/Moscow")
    assert is_quiet(datetime(2026, 9, 6, 22, 0, tzinfo=msk))
    assert is_quiet(datetime(2026, 9, 6, 3, 15, tzinfo=msk))
    assert not is_quiet(datetime(2026, 9, 6, 8, 0, tzinfo=msk))
    assert not is_quiet(datetime(2026, 9, 6, 21, 59, tzinfo=msk))
    nxt = next_window_start(datetime(2026, 9, 6, 23, 10, tzinfo=msk))
    assert nxt.hour == 8
    assert nxt.day == 7


def test_interval_skips_night() -> None:
    msk = ZoneInfo("Europe/Moscow")
    after = datetime(2026, 9, 6, 21, 0, tzinfo=msk)
    nxt = next_publish_at(after, interval_sec=3 * 3600)
    assert nxt.hour == 8
    assert nxt.day == 7


if __name__ == "__main__":
    test_referral_appends_query()
    test_referral_replaces_existing()
    test_seasons_label()
    test_parse_catalog_item()
    test_genre_block()
    test_quiet_hours_msk()
    test_interval_skips_night()
    print("ok")
