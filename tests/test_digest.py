from app.digest import (
    ChannelDigest,
    Err24Row,
    er_percent,
    err24_percent,
    format_digest,
    format_err24_messages,
    is_city_podslushano_channel,
    median_number,
    reach_sample_24h,
    views_of_posts_in_window,
)


def test_format_includes_tops() -> None:
    rows = [
        ChannelDigest(-1, "Альфа", "https://max.ru/a", 100, 500, 3, 12, 0, 12, 700, 900),
        ChannelDigest(-2, "Бета", "", 80, 200, 2, 5, 0, 5, 250, 300),
        ChannelDigest(-3, "Гамма", "", 50, 100, 1, 1, 0, 1, 120, 150),
        ChannelDigest(-4, "Дельта", "", 40, 10, 1, 0, 9, -9, 10, 10),
        ChannelDigest(-5, "Эхо", "", 30, 0, 0, 0, 4, -4, 0, 0),
        ChannelDigest(-6, "Дзета", "", 20, 0, 0, 0, 2, -2, 0, 0),
    ]
    text = format_digest(
        rows, day_label="05.10.2026 17:06", deltas_ready=True, views_ready=True
    )
    assert "Сводка сети" in text
    assert "24ч" in text and "810" in text
    assert "48ч" in text and "1 080" in text
    assert "72ч" in text and "1 360" in text
    assert "Альфа" in text and "500" in text
    assert "Дельта" in text and "−9" in text


def test_maxstars_views_are_sum_of_post_counters_in_window() -> None:
    now = 1_000_000.0
    posts = [
        {"mid": "new", "views": 100, "post_ts": now - 3600},
        {"mid": "old", "views": 9999, "post_ts": now - 40 * 3600},
        {"mid": "mid", "views": 50, "post_ts": now - 10 * 3600},
    ]
    views24, n24 = views_of_posts_in_window(posts, now - 24 * 3600, now)
    views48, _ = views_of_posts_in_window(posts, now - 48 * 3600, now)
    assert n24 == 2
    assert views24 == 150
    assert views48 == 150 + 9999
    assert abs(er_percent(3596, 10735) - 33.50) < 0.01


def test_err24_is_median_reach_over_subscribers() -> None:
    now = 2_000_000.0
    posts = [
        {"mid": "a", "views": 11000, "post_ts": now - 30 * 3600},
        {"mid": "b", "views": 9000, "post_ts": now - 40 * 3600},
        {"mid": "c", "views": 20000, "post_ts": now - 50 * 3600},
        {"mid": "fresh", "views": 999999, "post_ts": now - 3600},
    ]
    sample = reach_sample_24h(posts, now_ts=now, sample=20)
    assert 999999 not in sample
    reach = median_number(sample)
    assert reach == 11000
    assert abs(err24_percent(11000, 50000) - 22.0) < 0.01
    rows = [
        Err24Row(-1, "Альфа", "https://max.ru/a", 50000, 11000, 3, 22.0),
    ]
    text = format_err24_messages(rows, day_label="13.10.2026 13:00")[0]
    assert "ERR24" in text
    assert "22%" in text
    assert "11 000" in text
    assert "50 000" in text


def test_city_podslushano_excluded_from_err24() -> None:
    assert is_city_podslushano_channel("Ярославль подслушано")
    assert is_city_podslushano_channel("Ростов-на-Дону подслушано")
    assert is_city_podslushano_channel("  Набережные Челны  Подслушано  ")
    assert not is_city_podslushano_channel("Шарий")
    assert not is_city_podslushano_channel("ФУТБОЛ СЕГОДНЯ | МАТЧ ФУТБОЛ")
    assert not is_city_podslushano_channel("подслушано внутри названия футбол")


if __name__ == "__main__":
    test_format_includes_tops()
    test_maxstars_views_are_sum_of_post_counters_in_window()
    test_err24_is_median_reach_over_subscribers()
    test_city_podslushano_excluded_from_err24()
    print("ok")
