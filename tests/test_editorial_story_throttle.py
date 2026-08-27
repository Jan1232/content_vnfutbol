from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from editorial.models import NewsItem
from editorial.story_throttle import (
    RANK_LOW,
    RANK_OFFICIAL,
    can_publish_story,
    channel_day,
    is_official,
    record_story_post,
    story_key,
    subtype_rank,
)


def setUpModule() -> None:
    from app.db import db, init_db

    init_db()
    with db() as conn:
        conn.execute("DELETE FROM editorial_story_log WHERE channel_slug LIKE 'test_%'")


def _transfer_item(title: str, *, player: str = "Батраков", official: bool = False) -> NewsItem:
    body = title
    if official:
        body += " Официально подписал контракт."
    return NewsItem(
        external_id=f"x:{title[:20]}",
        source="test",
        url="https://example.com/x",
        title=title,
        body=body,
        lang="ru",
        published_at=datetime.now(timezone.utc),
        event_type="official_statement" if official else "transfer",
        entities={"players": [player], "teams": ["Локомотив", "Галатасарай"]},
    )


class StoryKeyTests(unittest.TestCase):
    def test_batrakov_same_key(self):
        a = _transfer_item("Батраков летит в Стамбул")
        b = _transfer_item("Батраков проходит медосмотр", player="Алексей Батраков")
        self.assertEqual(story_key(a), story_key(b))
        self.assertIn("transfer", story_key(a))

    def test_official_rank_higher(self):
        rumor = _transfer_item("Слухи о Батракове")
        off = _transfer_item("Батраков ОФИЦИАЛЬНО в Галатасарае", official=True)
        self.assertGreater(subtype_rank(off), subtype_rank(rumor))
        self.assertTrue(is_official(off))


class StoryThrottleTests(unittest.TestCase):
    slug = "test_story"

    def setUp(self) -> None:
        from app.db import db

        with db() as conn:
            conn.execute("DELETE FROM editorial_story_log WHERE channel_slug=?", (self.slug,))

    def test_max_three_per_day(self):
        key = "batrakov|transfer"
        day = channel_day()
        for i in range(3):
            record_story_post(self.slug, key, f"n{i}", RANK_LOW, day=day, post_index=i + 1)
        for j in range(3):
            record_story_post(
                self.slug, "other|transfer", f"o{j}", RANK_LOW, day=day, post_index=10 + j
            )
        ok, reason = can_publish_story(self.slug, key, RANK_LOW, day=day)
        self.assertFalse(ok)
        self.assertIn("limit", reason)

    def test_official_upgrade_to_fourth(self):
        key = "batrakov|transfer"
        day = channel_day()
        for i in range(3):
            record_story_post(self.slug, key, f"n{i}", RANK_LOW, day=day, post_index=i + 1)
        for j in range(3):
            record_story_post(
                self.slug, "other|transfer", f"o{j}", RANK_LOW, day=day, post_index=10 + j
            )
        ok, reason = can_publish_story(self.slug, key, RANK_OFFICIAL, day=day)
        self.assertTrue(ok)
        self.assertIn("official", reason)

    def test_hard_cap_four(self):
        key = "batrakov|transfer"
        day = channel_day()
        for i in range(4):
            rank = RANK_OFFICIAL if i == 3 else RANK_LOW
            record_story_post(self.slug, key, f"n{i}", rank, day=day, post_index=i + 1)
        ok, _ = can_publish_story(self.slug, key, RANK_OFFICIAL, day=day)
        self.assertFalse(ok)

    def test_gap_blocks_back_to_back(self):
        key = "batrakov|transfer"
        day = channel_day()
        record_story_post(self.slug, key, "n1", RANK_LOW, day=day, post_index=5)
        with patch("editorial.story_throttle.current_post_index", return_value=5):
            ok, reason = can_publish_story(self.slug, key, RANK_LOW, day=day)
        self.assertFalse(ok)
        self.assertIn("gap", reason)

    def test_gap_passes_after_enough_posts(self):
        key = "batrakov|transfer"
        other = "other|transfer"
        day = channel_day()
        record_story_post(self.slug, key, "n1", RANK_LOW, day=day, post_index=10)
        for j, sk in enumerate([other, other, other], start=11):
            record_story_post(self.slug, sk, f"o{j}", RANK_LOW, day=day, post_index=j)
        ok, _ = can_publish_story(self.slug, key, RANK_LOW, day=day)
        self.assertTrue(ok)

    def test_gap_passes_after_time(self):
        key = "batrakov|transfer"
        day = channel_day()
        past = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        from app.db import db

        with db() as conn:
            conn.execute(
                """
                INSERT INTO editorial_story_log
                  (channel_slug, story_key, news_id, subtype_rank, day, post_index, posted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (self.slug, key, "n1", RANK_LOW, day, 20, past),
            )
        ok, _ = can_publish_story(
            self.slug, key, RANK_LOW, day=day, now=datetime.now(timezone.utc)
        )
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
