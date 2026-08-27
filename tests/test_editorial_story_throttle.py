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
    make_story_summary,
    record_story_post,
    story_gate,
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


def _incident_item(
    title: str,
    body: str = "",
    *,
    event_type: str = "other",
    day: str | None = None,
) -> NewsItem:
    pub = datetime.now(timezone.utc)
    if day:
        pub = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return NewsItem(
        external_id=f"inc:{title[:30]}",
        source="test",
        url="https://example.com/inc",
        title=title,
        body=body or title,
        lang="ru",
        published_at=pub,
        event_type=event_type,
        entities={"teams": ["Spartak", "CSKA"], "players": []},
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

    def test_incident_chain_same_key(self):
        day = "2026-08-20"
        fight = _incident_item(
            "Драка после матча Спартак — ЦСКА",
            "После финального свистка началась потасовка",
            event_type="other",
            day=day,
        )
        sanctions = _incident_item(
            "Федерация дисквалифицировала игрока на 3 матча",
            "Санкции за драку в матче Спартак — ЦСКА",
            event_type="official_statement",
            day=day,
        )
        injury = _incident_item(
            "Игрок получил травму в драке",
            "Растяжение после стычки Спартак — ЦСКА",
            event_type="injury",
            day=day,
        )
        keys = {story_key(fight), story_key(sanctions), story_key(injury)}
        self.assertEqual(len(keys), 1)
        self.assertTrue(list(keys)[0].endswith("|incident"))
        self.assertIn("|incident", story_key(fight))


class IncidentStoryGateTests(unittest.TestCase):
    slug = "test_incident"

    def setUp(self) -> None:
        from app.db import db

        with db() as conn:
            conn.execute("DELETE FROM editorial_story_log WHERE channel_slug=?", (self.slug,))

    def _fill_gap(self, after_key: str) -> None:
        day = channel_day()
        idx = 100
        for j in range(3):
            record_story_post(
                self.slug, f"gapfiller{j}|other", f"g{j}", RANK_LOW, day=day, post_index=idx + j
            )

    def test_fight_series_llm_duplicate_and_development(self):
        day = "2026-08-20"
        fight = _incident_item(
            "Драка после матча Спартак — ЦСКА",
            "Массовая потасовка",
            day=day,
        )
        key = story_key(fight)
        self.assertTrue(key.endswith("|incident"))

        # 1) первый — без LLM
        with patch("editorial.story_throttle.throttle_config") as tc:
            from editorial.story_throttle import StoryThrottleConfig

            tc.return_value = StoryThrottleConfig(llm_relation_enabled=True, min_gap_posts=0, min_gap_min=0)
            ok, reason, k, _ = story_gate(self.slug, fight, day=day)
        self.assertTrue(ok)
        self.assertEqual(k, key)
        record_story_post(
            self.slug, key, "1", RANK_LOW, day=day, post_index=1, summary=make_story_summary(fight)
        )

        retell = _incident_item(
            "Стычка Спартака и ЦСКА — подробности драки",
            "Пересказ той же потасовки другими словами",
            day=day,
        )
        retell2 = _incident_item(
            "Ещё раз о драке Спартак — ЦСКА",
            "Другой источник пересказывает драку",
            day=day,
        )
        sanctions = _incident_item(
            "Федерация дисквалифицировала игрока на 3 матча",
            "Санкции РФС за драку",
            event_type="official_statement",
            day=day,
        )
        injury = _incident_item(
            "Игрок получил травму в драке",
            "Диагностировано растяжение после стычки",
            event_type="injury",
            day=day,
        )
        opinion = _incident_item(
            "Эксперт прокомментировал драку",
            "Мнение без новых фактов о потасовке Спартак ЦСКА",
            day=day,
        )

        def _fake_relation(title, body, priors):
            blob = f"{title}\n{body}".lower()
            if "дисквалиф" in blob or "санкц" in blob:
                return {"relation": "development", "new_facts": ["санкции"], "confidence": 0.9, "reason": "санкции"}
            if "травм" in blob or "растяжен" in blob:
                return {"relation": "development", "new_facts": ["травма"], "confidence": 0.9, "reason": "травма"}
            if "эксперт" in blob or "прокомментир" in blob:
                return {"relation": "duplicate", "new_facts": [], "confidence": 0.8, "reason": "мнение"}
            return {"relation": "duplicate", "new_facts": [], "confidence": 0.85, "reason": "пересказ"}

        with (
            patch("editorial.llm.story_relation", side_effect=_fake_relation),
            patch("editorial.story_throttle.throttle_config") as tc,
        ):
            from editorial.story_throttle import StoryThrottleConfig

            # gap выключен для проверки relation; отдельно проверим gap
            tc.return_value = StoryThrottleConfig(
                llm_relation_enabled=True,
                min_gap_posts=0,
                min_gap_min=0,
                incident_window_days=3,
                hard_cap=4,
                max_per_day=3,
            )
            ok2, r2, _, _ = story_gate(self.slug, retell, day=day)
            self.assertFalse(ok2)
            self.assertIn("повтор", r2)

            ok3, r3, _, _ = story_gate(self.slug, retell2, day=day)
            self.assertFalse(ok3)

            ok4, r4, _, _ = story_gate(self.slug, sanctions, day=day)
            self.assertTrue(ok4)
            self.assertEqual(r4, "development")

            record_story_post(
                self.slug,
                key,
                "4",
                RANK_OFFICIAL,
                day=day,
                post_index=2,
                summary=make_story_summary(sanctions),
            )

            ok5, r5, _, _ = story_gate(self.slug, injury, day=day)
            self.assertTrue(ok5)
            self.assertEqual(r5, "development")

            ok6, r6, _, _ = story_gate(self.slug, opinion, day=day)
            self.assertFalse(ok6)
            self.assertIn("повтор", r6)

    def test_development_respects_gap(self):
        day = channel_day()
        fight = _incident_item("Драка после матча Спартак — ЦСКА", day=day)
        key = story_key(fight)
        record_story_post(
            self.slug, key, "1", RANK_LOW, day=day, post_index=50, summary="драка"
        )
        sanctions = _incident_item(
            "Федерация дисквалифицировала игрока",
            "Санкции за драку",
            event_type="official_statement",
            day=day,
        )
        with (
            patch(
                "editorial.llm.story_relation",
                return_value={"relation": "development", "new_facts": ["бан"], "confidence": 1, "reason": "x"},
            ),
            patch("editorial.story_throttle.throttle_config") as tc,
        ):
            from editorial.story_throttle import StoryThrottleConfig

            tc.return_value = StoryThrottleConfig(
                llm_relation_enabled=True,
                min_gap_posts=3,
                min_gap_min=9999,
                incident_window_days=3,
            )
            ok, reason, _, _ = story_gate(self.slug, sanctions, day=day)
        self.assertFalse(ok)
        self.assertIn("gap", reason)

    def test_llm_disabled_falls_back_to_rank(self):
        day = channel_day()
        fight = _incident_item("Драка после матча Спартак — ЦСКА", day=day)
        key = story_key(fight)
        record_story_post(self.slug, key, "1", RANK_LOW, day=day, post_index=1, summary="драка")
        for j in range(3):
            record_story_post(
                self.slug, f"other{j}|x", f"o{j}", RANK_LOW, day=day, post_index=10 + j
            )
        retell = _incident_item("Пересказ драки Спартак ЦСКА", day=day)
        sanctions = _incident_item(
            "Дисквалификация и санкции федерации за драку",
            event_type="official_statement",
            day=day,
        )
        with patch("editorial.story_throttle.throttle_config") as tc:
            from editorial.story_throttle import StoryThrottleConfig

            tc.return_value = StoryThrottleConfig(
                llm_relation_enabled=False,
                min_gap_posts=0,
                min_gap_min=0,
                incident_window_days=3,
            )
            ok_d, r_d, _, _ = story_gate(self.slug, retell, day=day)
            ok_u, r_u, _, _ = story_gate(self.slug, sanctions, day=day)
        self.assertFalse(ok_d)
        self.assertIn("fallback", r_d)
        self.assertTrue(ok_u)


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
