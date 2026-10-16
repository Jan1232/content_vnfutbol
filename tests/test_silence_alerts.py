"""Тишина канала: порог 24 часа и текст алерта."""

from __future__ import annotations

from workers.silence import format_silence_message


def test_format_lists_channel() -> None:
    text = format_silence_message(
        [
            {
                "title": "Тест",
                "link": "https://max.ru/channel_x",
                "platform": "max",
                "last_sent": None,
                "quiet_hours": 26.2,
            }
        ],
        24,
    )
    assert "Тишина больше 24 ч" in text
    assert "Тест" in text
    assert "никогда" in text
    assert "26 ч" in text


if __name__ == "__main__":
    test_format_lists_channel()
    print("OK silence format")
