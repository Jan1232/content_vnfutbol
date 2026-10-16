from __future__ import annotations

from pathlib import Path

from predlozka import db as pdb
from predlozka.logic import (
    combine_header_and_text,
    deep_link,
    format_suggestion_header,
    is_dialog_message,
    is_valid_slug,
    length_error,
    make_slug,
    message_text,
    parse_callback_payload,
    parse_command,
    parse_int_setting,
    parse_url_callback,
    rate_error,
    sender_html_mention,
    sender_web_link,
    user_label,
)


def test_slug_and_link() -> None:
    slug = make_slug()
    assert is_valid_slug(slug)
    assert not is_valid_slug("")
    assert not is_valid_slug("ab")
    url = deep_link("id662354021091_4_bot", slug)
    assert url == f"https://max.ru/id662354021091_4_bot?start={slug}"


def test_parse_command() -> None:
    assert parse_command("/url") == ("url", "")
    assert parse_command("/url@id662354021091_4_bot") == ("url", "")
    assert parse_command("/start abc_12") == ("start", "abc_12")
    assert parse_command("привет") is None


def test_callback_and_dialog() -> None:
    assert parse_url_callback("url:-123") == -123
    assert parse_url_callback("nope") is None
    msg = {
        "recipient": {"chat_type": "dialog", "chat_id": 1},
        "body": {"text": "hi", "mid": "m1"},
    }
    assert is_dialog_message(msg)
    assert message_text(msg) == "hi"
    assert not is_dialog_message({"recipient": {"chat_type": "channel"}})


def test_header_and_label() -> None:
    sender = {"first_name": "Иван", "last_name": "Петров", "username": "ivan", "user_id": 42}
    label = user_label(sender)
    assert "Иван" in label and "@ivan" in label
    header = format_suggestion_header("Мой канал", sender)
    assert "Мой канал" in header
    assert "max://user/42" in header
    assert "https://max.ru/ivan" in header
    assert sender_html_mention(sender).startswith("<a href=")
    assert sender_web_link(sender) == "https://max.ru/ivan"
    combined = combine_header_and_text("H", "текст")
    assert combined.startswith("H")
    assert "текст" in combined


def test_antifraud_helpers() -> None:
    assert parse_int_setting("15") == 15
    assert parse_int_setting("нет") is None
    assert length_error("abc", 2) is not None
    assert length_error("abc", 10) is None
    assert length_error("abc", 0) is None
    assert rate_error(10, 10) is not None
    assert rate_error(3, 10) is None
    assert rate_error(99, 0) is None
    parsed = parse_callback_payload("ban:-100:55")
    assert parsed == ("ban", ["-100", "55"])
    assert parse_url_callback("url:-123") == -123


def test_channel_and_session(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    pdb.init_db(path)
    with pdb.db(path) as conn:
        ch = pdb.upsert_channel(
            conn,
            chat_id=-100,
            title="Новости",
            chat_type="channel",
            owner_user_id=7,
            added_by_user_id=7,
        )
        assert is_valid_slug(ch["slug"])
        again = pdb.upsert_channel(
            conn,
            chat_id=-100,
            title="Новости 2",
            chat_type="channel",
            owner_user_id=7,
            added_by_user_id=7,
        )
        assert again["slug"] == ch["slug"]
        other = pdb.upsert_channel(
            conn,
            chat_id=-200,
            title="Второй",
            chat_type="channel",
            owner_user_id=7,
            added_by_user_id=8,
        )
        owned = pdb.owner_channels(conn, 7)
        assert {c["chat_id"] for c in owned} == {-100, -200}
        added = pdb.owner_channels(conn, 8)
        assert [c["chat_id"] for c in added] == [-200]
        pdb.bind_session(conn, 55, -100)
        sess = pdb.get_session(conn, 55)
        assert sess is not None
        assert sess["chat_id"] == -100
        assert sess["title"] == "Новости 2"
        found = pdb.get_channel_by_slug(conn, other["slug"])
        assert found is not None and found["chat_id"] == -200
        pdb.set_channel_status(conn, -100, "removed")
        assert pdb.get_channel_by_slug(conn, again["slug"]) is None


def test_settings_and_bans(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    pdb.init_db(path)
    with pdb.db(path) as conn:
        pdb.upsert_channel(
            conn,
            chat_id=-100,
            title="Новости",
            chat_type="channel",
            owner_user_id=7,
            added_by_user_id=7,
        )
        ch = pdb.get_channel(conn, -100)
        rate, max_chars = pdb.channel_settings(ch)
        assert rate == 10
        assert max_chars == 2000
        pdb.set_channel_setting(conn, -100, "rate_per_hour", 3)
        pdb.set_channel_setting(conn, -100, "max_chars", 50)
        ch = pdb.get_channel(conn, -100)
        assert pdb.channel_settings(ch) == (3, 50)
        assert pdb.ban_user(conn, chat_id=-100, user_id=55, label="Иван", username="ivan", banned_by=7)
        assert pdb.is_banned(conn, -100, 55)
        assert not pdb.ban_user(conn, chat_id=-100, user_id=55, label="Иван")
        bans = pdb.list_bans(conn, -100)
        assert len(bans) == 1 and bans[0]["user_id"] == 55
        pdb.bind_session(conn, 55, -100)
        pdb.ban_user(conn, chat_id=-100, user_id=55, label="Иван")
        assert pdb.get_session(conn, 55) is None
        pdb.add_submission(conn, -100, 9)
        pdb.add_submission(conn, -100, 9)
        assert pdb.count_submissions_hour(conn, -100, 9) == 2
        assert pdb.unban_user(conn, -100, 55)
        assert not pdb.is_banned(conn, -100, 55)
        pdb.set_admin_state(conn, 7, "wait_rate", -100)
        st = pdb.get_admin_state(conn, 7)
        assert st is not None and st["action"] == "wait_rate"
        pdb.clear_admin_state(conn, 7)
        assert pdb.get_admin_state(conn, 7) is None


if __name__ == "__main__":
    from tempfile import TemporaryDirectory

    test_slug_and_link()
    test_parse_command()
    test_callback_and_dialog()
    test_header_and_label()
    test_antifraud_helpers()
    with TemporaryDirectory() as tmp:
        test_channel_and_session(Path(tmp))
        test_settings_and_bans(Path(tmp))
    print("OK predlozka")
