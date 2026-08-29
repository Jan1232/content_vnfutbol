"""HOTFIX-2: donor gate as_is / template / reject."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from editorial.channel_config import EditorialFeed
from editorial.gate_cache import get_gate_verdict, put_gate_verdict
from editorial.soccerblog_gate import (
    detect_ad,
    donor_gate,
    effective_gate_kind,
)
from editorial.sources import _classify_tg_post, parse_telegram_meme_feed


class DonorGateKindTests(unittest.TestCase):
    def test_effective_kind_legacy(self):
        self.assertEqual(effective_gate_kind({"kind": "meme"}), "as_is")
        self.assertEqual(effective_gate_kind({"kind": "news"}), "template")

    def test_ad_detect_betting(self):
        ok, _ = detect_ad("СЕГОДНЯ СТАВЛЮ НА МАТЧ: БАВАРИЯ - ШТУТГАРТ")
        self.assertTrue(ok)

    def test_ad_reject_strict(self):
        with patch("editorial.soccerblog_gate.get_settings") as gs:
            gs.return_value = MagicMock(
                soccerblog_gate_enabled=True,
                ad_reject_strict=True,
                soccerblog_gate_model="",
                editorial_vision_model="gpt-5.6-luna",
                editorial_reasoning_effort="low",
            )
            v = donor_gate("ставлю на матч букмекер промокод", [])
        self.assertEqual(v["kind"], "reject")
        self.assertTrue(v.get("is_ad"))

    def test_gate_error_not_meme(self):
        with (
            patch("editorial.soccerblog_gate.get_settings") as gs,
            patch("editorial.soccerblog_gate.media_preview_from_post", return_value=None),
            patch("editorial.soccerblog_gate.get_client") as gc,
        ):
            gs.return_value = MagicMock(
                soccerblog_gate_enabled=True,
                ad_reject_strict=True,
                donor_gate_default="template",
                soccerblog_gate_model="",
                editorial_vision_model="gpt-5.6-luna",
                editorial_reasoning_effort="low",
            )
            gc.return_value.chat.side_effect = RuntimeError("empty content")
            v = donor_gate("трансфер Мбаппе", [])
        self.assertEqual(v["kind"], "reject")
        self.assertTrue(v.get("gate_failed"))

    def test_template_from_model(self):
        with (
            patch("editorial.soccerblog_gate.get_settings") as gs,
            patch("editorial.soccerblog_gate.media_preview_from_post", return_value=None),
            patch("editorial.soccerblog_gate.get_client") as gc,
        ):
            gs.return_value = MagicMock(
                soccerblog_gate_enabled=True,
                ad_reject_strict=True,
                donor_gate_default="template",
                soccerblog_gate_model="",
                editorial_vision_model="gpt-5.6-luna",
                editorial_reasoning_effort="low",
            )
            gc.return_value.chat.return_value = (
                '{"kind":"template","reason":"трансфер","confidence":0.9,'
                '"is_video":false,"is_ad":false,"text_lang":"ru"}'
            )
            v = donor_gate("Мбайе перешёл в Астон Виллу за €55 млн", [])
        self.assertEqual(v["kind"], "template")

    def test_video_forces_as_is(self):
        with (
            patch("editorial.soccerblog_gate.get_settings") as gs,
            patch("editorial.soccerblog_gate.media_preview_from_post", return_value=b"jpeg"),
            patch("editorial.soccerblog_gate.get_client") as gc,
        ):
            gs.return_value = MagicMock(
                soccerblog_gate_enabled=True,
                ad_reject_strict=True,
                donor_gate_default="template",
                soccerblog_gate_model="",
                editorial_vision_model="gpt-5.6-luna",
                editorial_reasoning_effort="low",
            )
            gc.return_value.vision.return_value = {
                "kind": "template",
                "confidence": 0.8,
                "reason": "новость",
                "is_video": False,
                "is_ad": False,
                "text_lang": "ru",
            }
            v = donor_gate("гол", [{"type": "video", "url": "http://x/v.mp4"}], media_type="video")
        self.assertEqual(v["kind"], "as_is")
        self.assertTrue(v.get("is_video"))


class ClassifyTgPostTests(unittest.TestCase):
    def test_transfer_template(self):
        out = _classify_tg_post(
            "Мбайе перешёл в Виллу",
            take={"meme_image", "news"},
            has_video=False,
            has_image=True,
            verdict={"kind": "template", "confidence": 0.9},
        )
        self.assertEqual(out, ("news", "image"))

    def test_lineup_template(self):
        out = _classify_tg_post(
            "СОСТАВ БАВАРИИ на матч",
            take={"meme_image", "news"},
            has_video=False,
            has_image=True,
            verdict={"kind": "template", "confidence": 0.85},
        )
        self.assertEqual(out, ("news", "image"))

    def test_score_graphic_template(self):
        out = _classify_tg_post(
            "Бавария разносит Штутгарт 5:1",
            take={"meme_image", "news"},
            has_video=False,
            has_image=True,
            verdict={"kind": "template", "confidence": 0.8, "reason": "счёт на графике"},
        )
        self.assertEqual(out, ("news", "image"))

    def test_video_as_is(self):
        out = _classify_tg_post(
            "гол",
            take={"video", "meme_image"},
            has_video=True,
            has_image=False,
            verdict={"kind": "template"},
        )
        self.assertEqual(out, ("video", "video"))

    def test_joke_as_is(self):
        out = _classify_tg_post(
            "ахах мем",
            take={"meme_image", "news"},
            has_video=False,
            has_image=True,
            verdict={"kind": "as_is", "confidence": 0.9},
        )
        self.assertEqual(out, ("meme", "image"))

    def test_ad_reject_skipped(self):
        out = _classify_tg_post(
            "ставлю на матч",
            take={"meme_image", "news"},
            has_video=False,
            has_image=True,
            verdict={"kind": "reject", "is_ad": True},
        )
        self.assertIsNone(out)


class ParseFeedTemplateTests(unittest.TestCase):
    def test_news_gate_ingests_template_path(self):
        feed = EditorialFeed(
            name="futbol1",
            kind="telegram",
            handle="futbol1",
            take_only=("video", "meme_image", "news"),
        )
        post = SimpleNamespace(
            text="СОСТАВ БАВАРИИ на матч против Штутгарта",
            title="",
            external_id="tg2",
            source_url="https://t.me/x/2",
            media=[{"type": "image", "url": "https://example.com/i.jpg"}],
            published_at=None,
        )
        with (
            patch("app.config.get_settings", return_value=MagicMock(meme_source_enabled=True, tg_incremental=False)),
            patch("parsers.telegram.parse_telegram", return_value=("ch", [post])),
            patch("editorial.sources._extract_entities", return_value={}),
            patch("editorial.gate_cache.get_gate_verdict", return_value=None),
            patch("editorial.gate_cache.put_gate_verdict"),
            patch("editorial.tg_donor.is_text_seen", return_value=False),
            patch("editorial.tg_donor.get_last_seen_id", return_value=0),
            patch(
                "editorial.soccerblog_gate.donor_gate",
                return_value={
                    "kind": "template",
                    "confidence": 0.85,
                    "reason": "состав",
                    "gate_version": 2,
                    "text_lang": "ru",
                },
            ),
        ):
            items = parse_telegram_meme_feed(feed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].entities.get("tg_post_type"), "news")
        self.assertNotIn("meme_source", items[0].entities or {})


class GateCacheVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.db import init_db

        init_db()
        from app.db import db

        with db() as conn:
            conn.execute("DELETE FROM editorial_gate_cache WHERE feed_name='test_feed2'")

    def test_old_meme_cache_ignored(self):
        put_gate_verdict("test_feed2", "tg:ch/1", {"kind": "meme", "confidence": 0.9})
        self.assertIsNone(get_gate_verdict("test_feed2", "tg:ch/1"))

    def test_v2_template_cached(self):
        put_gate_verdict(
            "test_feed2",
            "tg:ch/2",
            {"kind": "template", "confidence": 0.9, "gate_version": 2},
        )
        v = get_gate_verdict("test_feed2", "tg:ch/2")
        self.assertEqual(v["kind"], "template")


if __name__ == "__main__":
    unittest.main()
