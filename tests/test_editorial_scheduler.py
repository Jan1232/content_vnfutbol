from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from editorial.channel_config import CadenceConfig, EditorialChannelConfig
from editorial.scheduler import is_priority, pick_best, random_gap_minutes


def setUpModule() -> None:
    from app.db import init_db

    init_db()


def _cfg(**kwargs) -> EditorialChannelConfig:
    return EditorialChannelConfig(
        slug="test",
        chat_id=-1,
        cadence=CadenceConfig(min_gap_min=40, max_gap_min=55, priority_bypass=True),
        always_priority_teams=("Russia",),
        **kwargs,
    )


def _item(**kwargs) -> dict:
    base = {
        "id": 1,
        "event_type": "match_result",
        "competition": "PL",
        "is_national": 0,
        "teams_json": '["Brighton", "Bournemouth"]',
        "source_published_at": "2026-08-19 12:00:00",
    }
    base.update(kwargs)
    return base


class SchedulerTests(unittest.TestCase):
    def test_random_gap_in_range(self):
        cfg = _cfg()
        seen = {random_gap_minutes(cfg) for _ in range(80)}
        self.assertTrue(all(40 <= x <= 55 for x in seen))
        self.assertGreater(len(seen), 3)

    def test_normal_match_not_priority(self):
        cfg = _cfg()
        self.assertFalse(is_priority(_item(), cfg))

    def test_grand_score_is_priority(self):
        cfg = _cfg()
        self.assertTrue(
            is_priority(_item(teams_json='["Real Madrid", "Girona"]'), cfg)
        )

    def test_cl_is_priority(self):
        cfg = _cfg()
        self.assertTrue(is_priority(_item(competition="CL", teams_json='["Brest", "Atalanta"]'), cfg))

    def test_russia_priority_even_vs_non_top100(self):
        cfg = _cfg()
        item = _item(
            is_national=1,
            competition="NT",
            teams_json='["Russia", "Andorra"]',
        )
        self.assertTrue(is_priority(item, cfg))

    def test_top100_nationals_priority(self):
        cfg = _cfg()
        item = _item(
            is_national=1,
            competition="WC",
            teams_json='["Spain", "France"]',
        )
        self.assertTrue(is_priority(item, cfg))

    def test_rumor_not_priority(self):
        cfg = _cfg()
        self.assertFalse(
            is_priority(_item(event_type="rumor", teams_json='["Real Madrid", "Barcelona"]'), cfg)
        )

    def test_priority_does_not_move_slot(self):
        cfg = _cfg()
        frozen = datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc)
        state = {"next_slot_at": "2026-08-19 12:00:00", "last_published_at": None}
        calls: list[dict] = []

        def _upsert(slug, **kwargs):
            calls.append(kwargs)

        with (
            patch("editorial.scheduler.get_channel_state", return_value=state),
            patch("editorial.scheduler.upsert_channel_state", side_effect=_upsert),
            patch("editorial.scheduler.utcnow", return_value=frozen),
        ):
            from editorial.scheduler import mark_priority_published, mark_normal_published

            mark_priority_published(cfg)
            self.assertEqual(calls[-1]["next_slot_at"], "2026-08-19 12:00:00")
            mark_normal_published(cfg)
            nxt = datetime.strptime(calls[-1]["next_slot_at"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            delta = (nxt - frozen).total_seconds() / 60
            self.assertGreaterEqual(delta, 40)
            self.assertLessEqual(delta, 55)

    def test_slot_blocks_normal(self):
        cfg = _cfg()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        with patch(
            "editorial.scheduler.get_channel_state",
            return_value={"next_slot_at": future},
        ):
            from editorial.scheduler import slot_ready

            self.assertFalse(slot_ready(cfg))

    def test_pick_best_prefers_grand(self):
        a = _item(id=1, teams_json='["Brighton", "Bournemouth"]')
        b = _item(id=2, teams_json='["Liverpool", "Everton"]')
        picked = pick_best([a, b])
        self.assertEqual(picked["id"], 2)

    def test_slot_idle_does_not_move_timer_via_publish_ready(self):
        """Пустой ready при наступившем слоте → slot_idle, таймер не двигаем."""
        from editorial.cycle import publish_ready

        cfg = EditorialChannelConfig(
            slug="test_idle",
            chat_id=-1,
            moderate_before_publish=False,
        )
        client = MagicMock()
        with (
            patch("editorial.cycle.expire_stale"),
            patch("editorial.cycle.ensure_slot_initialized"),
            patch("editorial.moderation.moderation_enabled", return_value=False),
            patch("editorial.cycle.list_ready", return_value=[]),
            patch("editorial.cycle.slot_ready", return_value=True),
            patch("editorial.cycle.mark_normal_published") as mark,
            patch("editorial.store.status_counts", return_value=[]),
            patch("editorial.store.top_stuck_errors", return_value=[]),
        ):
            out = publish_ready(client, cfg)
        self.assertTrue(any(r.get("action") == "slot_idle" for r in out))
        mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
