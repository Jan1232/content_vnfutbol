from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from predlozka.db import connect, init_db, list_erid_ads_for_day, prune_erid_ads, upsert_channel
from predlozka.config import erid_digest_hm, parse_user_ids
from predlozka.erid_ads import (
    calendar_day,
    extract_erid_urls,
    format_channel_digest,
    has_ad_footer,
    in_digest_window,
    ingest_message,
    is_erid_ad,
    ru_visit_text,
    should_send_now,
)


AD_TEXT = (
    "Скидка на обучение\n"
    "https://ya.cc/t/5JzgpUfVBGx7Sr/?erid=sKjnGhSeYQwk61JWjM2sLi65frGDyjhgYQh7LaLdoCM2tAs\n"
    "#реклама \n"
    "max.ru\n"
    "О рекламодателе"
)


def test_detects_ya_erid_and_footer() -> None:
    urls = is_erid_ad(AD_TEXT)
    assert len(urls) == 1
    assert "ya.cc/t/5JzgpUfVBGx7Sr" in urls[0]
    assert "erid=" in urls[0]
    assert has_ad_footer(AD_TEXT)
    assert not is_erid_ad("просто новость https://ya.cc/t/abc/?erid=zzz")
    assert not is_erid_ad("#реклама\nmax.ru\nО рекламодателе")


def test_extract_from_button_payload() -> None:
    raw = "https://ya.cc/t/AbC/?erid=xyz123"
    found = extract_erid_urls({"payload": {"buttons": [[{"url": raw}]]}})
    assert found == [raw]


def test_detects_markup_link_without_url_in_text() -> None:
    text = (
        "Футбол в MAX — коротко и по делу\n"
        "Подписаться\n"
        "#реклама 16+\n"
        "max.ru\n"
        "О рекламодателе"
    )
    markup = [
        {"url": "https://ya.cc/t/tQD7UGiUBGofNu/?erid=j1SUnGePsx6LMs4Jd", "type": "link"},
        {"url": "https://ya.cc/t/I13bWpy0BGofPk/?erid=j1SUnGePsx6LMs4Jd", "type": "link"},
    ]
    urls = is_erid_ad(text, markup)
    assert urls[0].startswith("https://ya.cc/t/tQD7UGiUBGofNu")
    other = (
        "Узнать цену\n"
        "#реклама \n"
        "crm.easy-mo.ru\n"
        "О рекламодателе"
    )
    assert is_erid_ad(
        other,
        [{"url": "https://ya.cc/t/5JzgpUfVBGx7Sr/?erid=sKjnGhSeYQwk61JWjM2sLi65frGDyjhgYQh7LaLdoCM2tAs"}],
    )


def test_digest_text_and_plural() -> None:
    msg = format_channel_digest(
        "Шарий",
        ["https://ya.cc/t/a/?erid=1", "https://ya.cc/t/b/?erid=2"],
        views=12345,
    )
    assert 'канал "Шарий"' in msg
    assert "Ссылки:" in msg
    assert "https://ya.cc/t/a/?erid=1, https://ya.cc/t/b/?erid=2" in msg
    assert "Перейти по 2 ссылкам" in msg
    assert "не менее 30 секунд" in msg
    assert "Охваты: 12 345" in msg
    assert ru_visit_text(1).startswith("Перейти по 1 ссылке")
    assert ru_visit_text(21).startswith("Перейти по 21 ссылке")


def test_day_isolation(tmp_path: Path) -> None:
    db_file = tmp_path / "p.db"
    init_db(db_file)
    conn = connect(db_file)
    try:
        upsert_channel(
            conn,
            chat_id=-100,
            title="Тест",
            chat_type="channel",
            owner_user_id=1,
            added_by_user_id=1,
        )
        msg = {
            "recipient": {"chat_type": "channel", "chat_id": -100},
            "body": {
                "text": (
                    "Узнать цену\n#реклама 16+\nmax.ru\nО рекламодателе"
                ),
                "mid": "m-today",
                "markup": [
                    {
                        "url": "https://ya.cc/t/5JzgpUfVBGx7Sr/?erid=sKjnGhSeYQwk61JWjM2sLi65frGDyjhgYQh7LaLdoCM2tAs",
                        "type": "link",
                    }
                ],
            },
            "timestamp": datetime.now(ZoneInfo("Asia/Yekaterinburg")).timestamp() * 1000,
        }
        assert ingest_message(conn, msg, day="2026-10-10")
        assert ingest_message(conn, {**msg, "body": {**msg["body"], "mid": "m2"}}, day="2026-10-10")
        assert len(list_erid_ads_for_day(conn, "2026-10-10", -100)) == 2
        pruned = prune_erid_ads(conn, "2026-10-11")
        assert pruned == 2
        assert list_erid_ads_for_day(conn, "2026-10-10") == []
        assert calendar_day()
    finally:
        conn.close()


def test_parse_owner_ids() -> None:
    assert parse_user_ids("44784432,57360766") == [44784432, 57360766]
    assert parse_user_ids("44784432") == [44784432]


def test_digest_window_at_22_ekb() -> None:
    assert erid_digest_hm() == (22, 0)
    zone = ZoneInfo("Asia/Yekaterinburg")
    assert in_digest_window(datetime(2026, 10, 13, 22, 0, tzinfo=zone))
    assert in_digest_window(datetime(2026, 10, 13, 22, 4, tzinfo=zone))
    assert not in_digest_window(datetime(2026, 10, 13, 22, 20, tzinfo=zone))
    assert not in_digest_window(datetime(2026, 10, 13, 21, 59, tzinfo=zone))


def test_should_not_send_outside_window(tmp_path: Path) -> None:
    db_file = tmp_path / "p.db"
    init_db(db_file)
    conn = connect(db_file)
    try:
        noon = datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("Asia/Yekaterinburg"))
        assert should_send_now(conn, noon) is False
    finally:
        conn.close()


if __name__ == "__main__":
    test_detects_ya_erid_and_footer()
    test_extract_from_button_payload()
    test_digest_text_and_plural()
    test_parse_owner_ids()
    test_digest_window_at_22_ekb()
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as d:
        test_day_isolation(Path(d))
        test_should_not_send_outside_window(Path(d))
    print("OK erid ads")
