from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.drip import compute_next_publish_at, parse_publish_times
from app.rules import resolve_rules

TZ = ZoneInfo("Asia/Yekaterinburg")
SLOTS = ("09:00", "12:30", "16:30", "22:30")


def _ts(y, mo, d, h, mi) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


def test_parse_times() -> None:
    assert parse_publish_times(SLOTS) == [(9, 0), (12, 30), (16, 30), (22, 30)]


def test_next_slot_skips_used_and_past() -> None:
    now = _ts(2026, 10, 5, 4, 5)
    first = compute_next_publish_at(
        [], now=now, min_gap_sec=0, max_gap_sec=0, max_per_day=4,
        tz_name="Asia/Yekaterinburg", publish_times=SLOTS,
    )
    assert datetime.fromtimestamp(first, TZ).strftime("%H:%M") == "09:00"
    second = compute_next_publish_at(
        [first], now=now, min_gap_sec=0, max_gap_sec=0, max_per_day=4,
        tz_name="Asia/Yekaterinburg", publish_times=SLOTS,
    )
    assert datetime.fromtimestamp(second, TZ).strftime("%H:%M") == "12:30"
    # утренний слив в 04:02 слот не занимает
    burst = _ts(2026, 10, 5, 4, 2)
    again = compute_next_publish_at(
        [burst], now=now, min_gap_sec=0, max_gap_sec=0, max_per_day=4,
        tz_name="Asia/Yekaterinburg", publish_times=SLOTS,
    )
    assert datetime.fromtimestamp(again, TZ).strftime("%H:%M") == "09:00"


def test_rolls_to_next_day() -> None:
    now = _ts(2026, 10, 5, 23, 0)
    used = [_ts(2026, 10, 5, 22, 30)]
    nxt = compute_next_publish_at(
        used, now=now, min_gap_sec=0, max_gap_sec=0, max_per_day=4,
        tz_name="Asia/Yekaterinburg", publish_times=SLOTS,
    )
    dt = datetime.fromtimestamp(nxt, TZ)
    assert dt.strftime("%Y-%m-%d %H:%M") == "2026-10-06 09:00"


def test_edits_yaml_slots() -> None:
    rules = resolve_rules(
        chat_id=-71534604699075,
        title="ФУТБОЛЬНЫЕ ЭДИТЫ,НАРЕЗКИ🔥",
        source_url="https://t.me/s/editsfootballers",
    )
    assert rules.publish_times == SLOTS
    assert rules.max_per_day == 4
    assert rules.archive_drip is True


if __name__ == "__main__":
    from app.rules import load_channel_overrides, load_core_rules

    load_core_rules.cache_clear()
    load_channel_overrides.cache_clear()
    test_parse_times()
    test_next_slot_skips_used_and_past()
    test_rolls_to_next_day()
    test_edits_yaml_slots()
    print("ok")
