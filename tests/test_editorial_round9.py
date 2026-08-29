"""Editorial round-9: TG donors, incremental cache, soften profanity."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.db import db, init_db
from editorial.channel_config import EditorialFeed
from editorial.cross_donor import cross_donor_duplicate
from editorial.models import NewsItem
from editorial.profanity import apply_profanity, effective_profanity_mode, profanity_ok, soften_profanity
from editorial.tg_donor import get_last_seen_id, set_last_seen_id, text_hash


class IncrementalCursorTests(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        with db() as conn:
            conn.execute("DELETE FROM tg_donor_cursor WHERE handle='testch'")
            conn.execute("DELETE FROM tg_donor_text_seen WHERE handle='testch'")

    def test_cursor_roundtrip(self):
        set_last_seen_id("testch", 42)
        self.assertEqual(get_last_seen_id("testch"), 42)

    def test_incremental_skips_old_posts(self):
        from editorial.sources import parse_telegram_feed

        feed = EditorialFeed(
            name="f1",
            kind="telegram",
            handle="testch",
            take_only=("news",),
        )
        posts = [
            SimpleNamespace(
                text="старый пост",
                title="",
                external_id="tg:testch/10",
                source_url="https://t.me/testch/10",
                media=[],
            ),
            SimpleNamespace(
                text="новый пост про матч команды",
                title="",
                external_id="tg:testch/11",
                source_url="https://t.me/testch/11",
                media=[],
            ),
        ]
        set_last_seen_id("testch", 10)
        gate_calls: list[str] = []

        def _gate(text, media, *, media_type):
            gate_calls.append(text)
            return {"kind": "news", "confidence": 0.9, "reason": "ok", "text_lang": "ru"}

        with (
            patch("app.config.get_settings") as gs,
            patch("parsers.telegram.parse_telegram", return_value=("ch", posts)),
            patch("editorial.sources._extract_entities", return_value={}),
            patch("editorial.soccerblog_gate.soccerblog_gate", side_effect=_gate),
        ):
            gs.return_value = MagicMock(meme_source_enabled=True, tg_incremental=True)
            items = parse_telegram_feed(feed)
        self.assertEqual(len(items), 1)
        self.assertIn("новый", items[0].title)
        self.assertEqual(gate_calls, [])


class SoftProfanityTests(unittest.TestCase):
    def test_soften_replaces_obscene(self):
        out = soften_profanity("Это пиздец какая жесть")
        self.assertNotIn("пизд", out.lower())
        self.assertTrue(profanity_ok(out, mode="soften")[0])

    def test_effective_mode_from_settings(self):
        with patch("editorial.profanity.get_settings") as gs:
            gs.return_value = MagicMock(profanity_mode="soften", profanity_filter="strict")
            self.assertEqual(effective_profanity_mode(), "soften")


class CrossDonorTests(unittest.TestCase):
    def setUp(self) -> None:
        init_db()

    def test_duplicate_from_other_source(self):
        item = NewsItem(
            external_id="b:1",
            source="futbol1",
            url="u",
            title="Гол",
            body="Счёт 2:1",
            lang="ru",
            published_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            entities={"story_key": "team|match_result"},
            event_type="match_result",
        )
        with (
            patch("editorial.cross_donor.db") as mock_db,
            patch("editorial.cross_donor.get_settings") as gs,
            patch("editorial.cross_donor.story_key", return_value="team|match_result"),
        ):
            gs.return_value = MagicMock(cross_donor_window_min=180)
            conn = MagicMock()
            mock_db.return_value.__enter__.return_value = conn
            conn.execute.return_value.fetchone.return_value = {"source": "soccerblog", "id": 1}
            dup, reason = cross_donor_duplicate("vnf_editorial", item)
        self.assertTrue(dup)
        self.assertIn("cross-donor", reason)


class RoundupTemplateTests(unittest.TestCase):
    def test_roundup_template_exists(self):
        from pathlib import Path

        p = Path(__file__).resolve().parents[1] / "editorial" / "templates" / "roundup.html.j2"
        self.assertTrue(p.is_file())


if __name__ == "__main__":
    unittest.main()
