from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from editorial.channel_config import EditorialChannelConfig, EditorialCta
from editorial.fixtures import Match, in_poll_window, is_significant
from editorial.matchday import in_matchday_window, matchday_tick
from editorial.results import results_tick

MSK = ZoneInfo("Europe/Moscow")


def _match(**kwargs) -> Match:
    ko = kwargs.pop("kickoff_utc", datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc))
    home = kwargs.pop("home", "Brentford")
    away = kwargs.pop("away", "Bournemouth")
    defaults = dict(
        provider_id="fd:1",
        competition="PL",
        home=home,
        away=away,
        home_ru=home,
        away_ru=away,
        kickoff_utc=ko,
        status="SCHEDULED",
        score_home=None,
        score_away=None,
        stage=None,
        is_national=False,
    )
    defaults.update(kwargs)
    return Match(**defaults)


class FakeProvider:
    def __init__(self, matches: list[Match]) -> None:
        self.matches = list(matches)
        self.status_calls: list[str] = []

    def matches_on(self, date_msk):
        return list(self.matches)

    def match_status(self, match_id: str) -> Match | None:
        self.status_calls.append(match_id)
        for m in self.matches:
            if m.provider_id == match_id:
                return m
        return None

    def finished_since(self, since_ts):
        return [m for m in self.matches if m.status == "FINISHED"]


class SignificantTests(unittest.TestCase):
    def test_grand_is_significant(self):
        self.assertTrue(is_significant(_match(home="Real Madrid", away="Girona", competition="PD")))

    def test_all_cl_is_significant(self):
        self.assertTrue(is_significant(_match(home="Brest", away="Atalanta", competition="CL")))

    def test_russia_vs_non_top100_is_significant(self):
        self.assertTrue(
            is_significant(
                _match(home="Russia", away="Andorra", competition="NT", is_national=True)
            )
        )

    def test_two_midtable_not_significant(self):
        self.assertFalse(is_significant(_match(home="Brentford", away="Bournemouth", competition="PL")))


class MatchdayResultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        from app.config import get_settings
        from app.db import init_db

        self._settings = get_settings()
        self._old_db = self._settings.db_path
        self._settings.db_path = Path(self._tmp.name)
        init_db()
        self.cfg = EditorialChannelConfig(
            slug="fx_test",
            chat_id=-1,
            dry_run=True,
            cta=EditorialCta(text="Подписаться", url="https://max.ru/channel_vnfutbol"),
        )

    def tearDown(self) -> None:
        self._settings.db_path = self._old_db
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_window_0900(self):
        inside = datetime(2026, 8, 20, 9, 5, tzinfo=MSK)
        outside = datetime(2026, 8, 20, 12, 0, tzinfo=MSK)
        self.assertTrue(in_matchday_window(inside))
        self.assertFalse(in_matchday_window(outside))

    def test_empty_day_no_post(self):
        now = datetime(2026, 8, 20, 9, 5, tzinfo=MSK)
        res = matchday_tick(self.cfg, None, now=now, provider=FakeProvider([]))
        self.assertEqual(res["action"], "skipped_no_matches")
        again = matchday_tick(self.cfg, None, now=now, provider=FakeProvider([]))
        self.assertEqual(again["action"], "already")

    def test_outside_window_wait_unless_force(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=MSK)
        provider = FakeProvider(
            [_match(home="Real Madrid", away="Girona", competition="PD")]
        )
        self.assertEqual(matchday_tick(self.cfg, None, now=now, provider=provider)["action"], "wait")
        with (
            patch("editorial.matchday.render_card", return_value="/tmp/x.png"),
            patch("editorial.matchday.publish", return_value={"action": "simulated", "mid": "simulated"}),
        ):
            forced = matchday_tick(self.cfg, None, now=now, force=True, provider=provider)
        self.assertIn(forced.get("action"), {"simulated", "published"})
        self.assertEqual(forced.get("kind"), "matchday")

    def test_matchday_not_duplicated(self):
        now = datetime(2026, 8, 20, 9, 5, tzinfo=MSK)
        provider = FakeProvider(
            [_match(home="Real Madrid", away="Girona", competition="PD")]
        )
        with (
            patch("editorial.matchday.render_card", return_value="/tmp/x.png"),
            patch("editorial.matchday.publish", return_value={"action": "simulated", "mid": "simulated"}),
        ):
            first = matchday_tick(self.cfg, None, now=now, provider=provider)
            second = matchday_tick(self.cfg, None, now=now, provider=provider)
        self.assertEqual(first.get("kind"), "matchday")
        self.assertEqual(second["action"], "already")

    def test_finished_significant_posted_once(self):
        ko = datetime.now(timezone.utc) - timedelta(hours=2)
        m = _match(
            provider_id="fd:42",
            home="Real Madrid",
            away="Girona",
            competition="PD",
            status="FINISHED",
            score_home=2,
            score_away=1,
            kickoff_utc=ko,
        )
        provider = FakeProvider([m])
        with (
            patch("editorial.results.render_card", return_value="/tmp/x.png"),
            patch("editorial.results.publish", return_value={"action": "simulated", "mid": "simulated"}),
        ):
            first = results_tick(self.cfg, None, now=datetime.now(timezone.utc), provider=provider)
            second = results_tick(self.cfg, None, now=datetime.now(timezone.utc), provider=provider)
        actions = [p.get("action") for p in first.get("posted") or []]
        self.assertIn("simulated", actions)
        self.assertEqual(first.get("posted") and first["posted"][0].get("kind"), "fixture_result")
        self.assertEqual(second.get("skipped"), 1)
        self.assertFalse(second.get("posted"))

    def test_insignificant_not_posted(self):
        ko = datetime.now(timezone.utc) - timedelta(hours=2)
        m = _match(
            provider_id="fd:99",
            home="Brentford",
            away="Bournemouth",
            competition="PL",
            status="FINISHED",
            score_home=1,
            score_away=0,
            kickoff_utc=ko,
        )
        res = results_tick(
            self.cfg, None, now=datetime.now(timezone.utc), provider=FakeProvider([m])
        )
        self.assertEqual(res.get("posted"), [])
        self.assertEqual(res.get("windowed"), 0)

    def test_delayed_score_waits(self):
        ko = datetime.now(timezone.utc) - timedelta(hours=2)
        pending = _match(
            provider_id="fd:7",
            home="Real Madrid",
            away="Girona",
            competition="PD",
            status="FINISHED",
            score_home=None,
            score_away=None,
            kickoff_utc=ko,
        )
        ready = _match(
            provider_id="fd:7",
            home="Real Madrid",
            away="Girona",
            competition="PD",
            status="FINISHED",
            score_home=3,
            score_away=0,
            kickoff_utc=ko,
        )
        now = datetime.now(timezone.utc)
        first = results_tick(self.cfg, None, now=now, provider=FakeProvider([pending]))
        self.assertEqual((first.get("posted") or [{}])[0].get("action"), "wait_score")
        with (
            patch("editorial.results.render_card", return_value="/tmp/x.png"),
            patch("editorial.results.publish", return_value={"action": "simulated", "mid": "simulated"}),
        ):
            second = results_tick(self.cfg, None, now=now, provider=FakeProvider([ready]))
        self.assertEqual((second.get("posted") or [{}])[0].get("action"), "simulated")

    def test_poll_window_pre_kickoff(self):
        ko = datetime.now(timezone.utc) + timedelta(minutes=10)
        m = _match(home="Real Madrid", away="Girona", competition="PD", kickoff_utc=ko)
        self.assertFalse(in_poll_window(m, datetime.now(timezone.utc), pre_min=5, post_min=30))
        self.assertTrue(
            in_poll_window(m, datetime.now(timezone.utc) + timedelta(minutes=6), pre_min=5, post_min=30)
        )


if __name__ == "__main__":
    unittest.main()
