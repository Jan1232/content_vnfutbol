"""Tests for moderation auto-reject (daytime hour, night quiet)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from editorial.channel_config import EditorialChannelConfig
from editorial.moderation import (
    _is_quiet_hour,
    attentive_elapsed_seconds,
    expire_stale_moderation_reviews,
)


class QuietHoursTests(unittest.TestCase):
    def test_night_window_ekb(self):
        tz = ZoneInfo("Asia/Yekaterinburg")
        self.assertTrue(
            _is_quiet_hour(datetime(2026, 8, 27, 23, 0, tzinfo=tz), quiet_start=22, quiet_end=8)
        )
        self.assertTrue(
            _is_quiet_hour(datetime(2026, 8, 28, 3, 0, tzinfo=tz), quiet_start=22, quiet_end=8)
        )
        self.assertFalse(
            _is_quiet_hour(datetime(2026, 8, 28, 10, 0, tzinfo=tz), quiet_start=22, quiet_end=8)
        )

    def test_attentive_skips_night(self):
        # 21:30 → 09:30 next day Ekb: night 22–08 quiet; attentive ≈ 0.5h + 1.5h
        start = datetime(2026, 8, 27, 16, 30, tzinfo=timezone.utc)  # 21:30 Ekb
        end = datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc)  # 09:30 Ekb
        elapsed = attentive_elapsed_seconds(start, end)
        self.assertGreater(elapsed, 1.8 * 3600)
        self.assertLess(elapsed, 2.2 * 3600)


class AutoRejectTests(unittest.TestCase):
    def _settings(self) -> MagicMock:
        return MagicMock(
            moderation_auto_reject_min=60,
            moderation_auto_reject_tz="Asia/Yekaterinburg",
            moderation_quiet_start_hour=22,
            moderation_quiet_end_hour=8,
            telegram_admin_id=1,
        )

    def test_skips_at_night(self):
        cfg = EditorialChannelConfig(slug="vnf_editorial", chat_id=-1, moderate_before_publish=True)
        night = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)  # 01:00 Ekb
        with (
            patch("editorial.moderation.moderation_enabled", return_value=True),
            patch("editorial.moderation.get_settings", return_value=self._settings()),
            patch("editorial.moderation.datetime") as dt_mod,
        ):
            dt_mod.now.return_value = night
            out = expire_stale_moderation_reviews(cfg)
        self.assertEqual(out[0]["action"], "auto_reject_skipped_night")

    def test_rejects_after_daytime_hour(self):
        cfg = EditorialChannelConfig(slug="vnf_editorial", chat_id=-1, moderate_before_publish=True)
        now = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)  # 15:00 Ekb
        row = {
            "id": 99,
            "status": "awaiting_review",
            "awaiting_review_at": (now - timedelta(hours=2)).isoformat(),
            "title": "x",
            "channel_slug": "vnf_editorial",
        }
        with (
            patch("editorial.moderation.moderation_enabled", return_value=True),
            patch("editorial.moderation.get_settings", return_value=self._settings()),
            patch("editorial.moderation.datetime") as dt_mod,
            patch("editorial.moderation.list_by_status", return_value=[row]),
            patch("editorial.moderation.get_session", return_value=None),
            patch("editorial.moderation.reject_post") as rej,
            patch("editorial.moderation.attentive_elapsed_seconds", return_value=3600),
            patch("editorial.moderation._is_quiet_hour", return_value=False),
        ):
            dt_mod.now.return_value = now
            out = expire_stale_moderation_reviews(cfg)
        rej.assert_called_once()
        self.assertEqual(out[0]["action"], "auto_rejected")
        self.assertEqual(out[0]["news_id"], 99)


if __name__ == "__main__":
    unittest.main()
