"""Tests for soccerblog gate cache and TG since_id cursor."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.db import db, init_db
from editorial.channel_config import EditorialFeed
from editorial.gate_cache import get_gate_verdict, put_gate_verdict
from editorial.sources import parse_telegram_meme_feed


class GateCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        with db() as conn:
            conn.execute("DELETE FROM editorial_gate_cache WHERE feed_name='test_feed'")
            conn.execute("DELETE FROM tg_donor_cursor WHERE handle IN ('ch', 'test_feed')")
            conn.execute("DELETE FROM tg_donor_text_seen WHERE handle IN ('ch', 'test_feed')")

    def test_put_get_roundtrip(self):
        put_gate_verdict("test_feed", "tg:ch/1", {"kind": "reject", "confidence": 0.9})
        v = get_gate_verdict("test_feed", "tg:ch/1")
        self.assertEqual(v["kind"], "reject")

    def test_since_id_skips_old_posts(self):
        feed = EditorialFeed(
            name="test_feed",
            kind="telegram",
            handle="ch",
            take_only=("meme_image",),
        )
        posts = [
            SimpleNamespace(
                text="старый",
                title="",
                external_id="tg:ch/100",
                source_url="https://t.me/ch/100",
                media=[{"type": "image", "url": "https://example.com/a.jpg"}],
            ),
            SimpleNamespace(
                text="новый",
                title="",
                external_id="tg:ch/101",
                source_url="https://t.me/ch/101",
                media=[{"type": "image", "url": "https://example.com/b.jpg"}],
            ),
        ]
        gate_calls: list[str] = []

        def _gate(text, media, *, media_type):
            gate_calls.append(text)
            return {"kind": "meme", "confidence": 0.9, "reason": "ok", "text_lang": "ru"}

        with (
            patch("app.config.get_settings", return_value=MagicMock(meme_source_enabled=True, tg_incremental=False)),
            patch("parsers.telegram.parse_telegram", return_value=("ch", posts)),
            patch("editorial.sources._extract_entities", return_value={}),
            patch("editorial.soccerblog_gate.soccerblog_gate", side_effect=_gate),
            patch("editorial.gate_cache.get_gate_verdict", return_value=None),
            patch("editorial.gate_cache.put_gate_verdict"),
        ):
            items = parse_telegram_meme_feed(
                feed,
                since_id="test_feed:tg:ch/100",
            )
        self.assertEqual(len(items), 1)
        self.assertEqual(len(gate_calls), 1)
        self.assertIn("новый", gate_calls[0])

    def test_cache_avoids_second_gate_call(self):
        feed = EditorialFeed(
            name="test_feed",
            kind="telegram",
            handle="ch",
            take_only=("meme_image",),
        )
        post = SimpleNamespace(
            text="мем",
            title="",
            external_id="tg:ch/200",
            source_url="https://t.me/ch/200",
            media=[{"type": "image", "url": "https://example.com/c.jpg"}],
        )
        with (
            patch("app.config.get_settings", return_value=MagicMock(meme_source_enabled=True, tg_incremental=False)),
            patch("parsers.telegram.parse_telegram", return_value=("ch", [post])),
            patch("editorial.sources._extract_entities", return_value={}),
            patch("editorial.soccerblog_gate.soccerblog_gate") as gate,
        ):
            gate.return_value = {"kind": "reject", "confidence": 0.95, "reason": "x"}
            parse_telegram_meme_feed(feed)
            parse_telegram_meme_feed(feed)
        self.assertEqual(gate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
