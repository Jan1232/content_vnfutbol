"""Round-7: soccerblog gate, story_relation hybrid, vision savings."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from editorial.channel_config import EditorialChannelConfig, EditorialFeed
from editorial.llm import story_relation
from editorial.soccerblog_gate import (
    _normalize_verdict,
    _text_fallback_verdict,
    should_auto_publish,
    soccerblog_gate,
)


class SoccerblogGateTests(unittest.TestCase):
    def test_normalize_reject(self):
        v = _normalize_verdict(
            {"kind": "reject", "confidence": 0.9, "reason": "transfer", "text_lang": "ru"}
        )
        self.assertEqual(v["kind"], "reject")

    def test_text_fallback_reject_transfer(self):
        v = _text_fallback_verdict("ХИРВИГОУ: аренда с опцией выкупа")
        self.assertEqual(v["kind"], "reject")

    def test_should_auto_publish(self):
        row = {
            "entities_json": '{"soccerblog_gate":{"kind":"meme","confidence":0.85}}'
        }
        with patch("editorial.soccerblog_gate.get_settings") as gs:
            gs.return_value = MagicMock(
                soccerblog_gate_enabled=True,
                soccerblog_auto_publish=True,
                soccerblog_auto_confidence=0.8,
            )
            self.assertTrue(should_auto_publish(row))

    def test_auto_publish_disabled_by_default(self):
        row = {
            "entities_json": '{"soccerblog_gate":{"kind":"meme","confidence":0.95}}'
        }
        with patch("editorial.soccerblog_gate.get_settings") as gs:
            gs.return_value = MagicMock(
                soccerblog_gate_enabled=True,
                soccerblog_auto_publish=False,
                soccerblog_auto_confidence=0.8,
            )
            self.assertFalse(should_auto_publish(row))

    def test_gate_vision_meme(self):
        with (
            patch("editorial.soccerblog_gate.get_settings") as gs,
            patch("editorial.soccerblog_gate.media_preview_from_post", return_value=b"jpeg"),
            patch("editorial.soccerblog_gate.get_client") as gc,
        ):
            gs.return_value = MagicMock(
                soccerblog_gate_enabled=True,
                soccerblog_gate_model="",
                editorial_vision_model="gpt-4o-mini",
            )
            gc.return_value.vision.return_value = {
                "kind": "meme",
                "confidence": 0.92,
                "reason": "коллаж с подписью",
                "is_media_meme": True,
                "text_lang": "ru",
            }
            v = soccerblog_gate("мем текст", [{"type": "image", "url": "http://x/i.jpg"}])
        self.assertEqual(v["kind"], "meme")
        self.assertGreaterEqual(v["confidence"], 0.9)

    def test_parse_feed_reject_skipped(self):
        from editorial.sources import parse_telegram_meme_feed

        feed = EditorialFeed(
            name="soccerblog_memes",
            kind="telegram",
            handle="thesoccerblogteam",
            take_only=("video", "meme_image"),
        )
        post = SimpleNamespace(
            text="Состав на матч",
            title="",
            external_id="tg1",
            source_url="https://t.me/x/1",
            media=[{"type": "image", "url": "https://example.com/i.jpg"}],
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
                "editorial.soccerblog_gate.soccerblog_gate",
                return_value={"kind": "reject", "confidence": 0.95, "reason": "lineup"},
            ),
        ):
            items = parse_telegram_meme_feed(feed)
        self.assertEqual(items, [])

    def test_parse_feed_news_template_path(self):
        from editorial.sources import parse_telegram_meme_feed

        feed = EditorialFeed(
            name="soccerblog_memes",
            kind="telegram",
            handle="thesoccerblogteam",
            take_only=("video", "meme_image"),
        )
        post = SimpleNamespace(
            text="Артета дал интервью после матча",
            title="",
            external_id="tg2",
            source_url="https://t.me/x/2",
            media=[{"type": "image", "url": "https://example.com/i.jpg"}],
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
                    "confidence": 0.75,
                    "reason": "интервью",
                    "text_lang": "ru",
                    "gate_version": 2,
                },
            ),
        ):
            items = parse_telegram_meme_feed(feed)
        self.assertEqual(len(items), 1)
        self.assertNotIn("meme_source", items[0].entities or {})
        self.assertEqual(items[0].entities.get("tg_post_type"), "news")


class StoryRelationHybridTests(unittest.TestCase):
    def test_luna_high_conf_skips_terra(self):
        with (
            patch("editorial.llm.chat_json") as cj,
            patch("editorial.llm.get_settings") as gs,
        ):
            gs.return_value = MagicMock(story_relation_hybrid=True, reasoning_escalate=0.7)
            cj.return_value = {
                "relation": "duplicate",
                "confidence": 0.9,
                "reason": "same story",
            }
            out = story_relation("t", "b", ["prior"])
        self.assertEqual(cj.call_count, 1)
        self.assertEqual(cj.call_args.kwargs.get("tag"), "story_relation_luna")
        self.assertEqual(out["relation"], "duplicate")
        self.assertEqual(out.get("_model_tier"), "luna")

    def test_low_conf_escalates_terra(self):
        with (
            patch("editorial.llm.chat_json") as cj,
            patch("editorial.llm.get_settings") as gs,
        ):
            gs.return_value = MagicMock(story_relation_hybrid=True, reasoning_escalate=0.7)
            cj.side_effect = [
                {"relation": "development", "confidence": 0.4, "reason": "unclear"},
                {"relation": "development", "confidence": 0.85, "reason": "new fact"},
            ]
            out = story_relation("t", "b", ["prior"])
        self.assertEqual(cj.call_count, 2)
        self.assertEqual(out.get("_model_tier"), "terra")


class VisionSingleCandidateTests(unittest.TestCase):
    def test_single_candidate_skips_vision_for_og(self):
        from pathlib import Path
        from editorial.imagery import ImageCandidate, score_relevance

        cand = ImageCandidate(path=Path("/tmp/x.jpg"), url="u", via="article", width=800, height=600)
        with patch("editorial.openai_client.get_client") as gc:
            ranked = score_relevance([cand], {"title": "test", "entities_json": "{}"})
        gc.assert_not_called()
        self.assertEqual(len(ranked), 1)
        self.assertTrue(ranked[0].relevant)

    def test_script_pick_prefers_article(self):
        from pathlib import Path
        from editorial.imagery import ImageCandidate, _script_pick_candidate

        a = ImageCandidate(path=Path("/tmp/a.jpg"), url="a", via="article", width=600, height=600)
        y = ImageCandidate(path=Path("/tmp/y.jpg"), url="y", via="yandex", width=2000, height=2000)
        picked = _script_pick_candidate([a, y])
        self.assertEqual(picked.via, "article")

    def test_should_skip_vision_for_og(self):
        from pathlib import Path
        from editorial.imagery import ImageCandidate, _should_call_vision

        cand = ImageCandidate(path=Path("/tmp/x.jpg"), url="u", via="article", width=800, height=600)
        with patch("editorial.imagery.get_settings") as gs:
            gs.return_value = type("S", (), {"vision_skip_for_og": True})()
            self.assertFalse(_should_call_vision(cand, {"title": "t", "entities_json": "{}"}))


if __name__ == "__main__":
    unittest.main()
