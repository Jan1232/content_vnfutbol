"""Источник editsfootballers: только видео, без текста, архив 2/день."""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

from app.drip import compute_next_publish_at
from app.filter import has_video_media, keep_video_media
from app.rules import reload_rules, resolve_rules

DEST = dict(
    chat_id=-71534604699075,
    title="ФУТБОЛЬНЫЕ ЭДИТЫ,НАРЕЗКИ🔥",
    link="https://max.ru/join/QQHa3mXhKHm_1myDjOSa9QdlR7az2SIddeEpRy-WvLU",
)


def test_editsfootballers_rules_scoped_to_source() -> None:
    reload_rules()
    src = resolve_rules(**DEST, source_url="https://t.me/s/editsfootballers")
    assert src.require_video is True
    assert src.drop_caption is True
    assert src.archive_drip is True
    assert src.max_per_day == 2
    assert src.min_gap_sec == 21600
    assert src.max_gap_sec == 28800

    other = resolve_rules(**DEST, source_url="https://t.me/s/futbolnyeediti")
    assert other.require_video is False
    assert other.drop_caption is False
    assert other.archive_drip is False
    assert other.max_per_day == 0


def test_keep_video_only() -> None:
    media = [
        {"type": "image", "url": "a.jpg"},
        {"type": "video", "url": "", "tg_ref": "editsfootballers/1"},
        {"type": "tg_meta", "buttons": []},
    ]
    kept = keep_video_media(media)
    assert has_video_media(kept)
    assert all((m.get("type") in {"video", "tg_meta"}) for m in kept)
    assert not has_video_media([{"type": "image", "url": "a.jpg"}])


def test_drip_two_per_day_and_gap() -> None:
    rng = random.Random(1)
    zone = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 9, 10, 10, 0, tzinfo=zone).timestamp()
    first = compute_next_publish_at(
        [],
        now=now,
        min_gap_sec=21600,
        max_gap_sec=28800,
        max_per_day=2,
        rng=rng,
    )
    assert first == now
    second = compute_next_publish_at(
        [first],
        now=now,
        min_gap_sec=21600,
        max_gap_sec=28800,
        max_per_day=2,
        rng=rng,
    )
    gap = second - first
    assert 21600 <= gap <= 28800
    third = compute_next_publish_at(
        [first, second],
        now=second,
        min_gap_sec=21600,
        max_gap_sec=28800,
        max_per_day=2,
        rng=rng,
    )
    day2 = datetime.fromtimestamp(third, zone).date()
    assert day2 > datetime.fromtimestamp(first, zone).date()
    assert third - second >= 21600


if __name__ == "__main__":
    test_editsfootballers_rules_scoped_to_source()
    test_keep_video_only()
    test_drip_two_per_day_and_gap()
    print("OK editsfootballers")
