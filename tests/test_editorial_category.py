from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from editorial.event_labels import event_type_label, moderation_event_types
from editorial.moderation import save_event_type
from editorial.tg_moderator.keyboards import category_keyboard, review_keyboard
from editorial.topic_gate import classify_event_rules


class EventLabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertIn("Трансфер", event_type_label("transfer"))

    def test_moderation_types_skip_fixture(self):
        types = moderation_event_types(["transfer", "lifestyle", "fixture_result"])
        self.assertIn("transfer", types)
        self.assertNotIn("fixture_result", types)


class CategoryKeyboardTests(unittest.TestCase):
    def test_review_has_category_button(self):
        kb = review_keyboard(42)
        texts = [btn["text"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("📂 Категория", texts)

    def test_category_keyboard_callback(self):
        kb = category_keyboard(42, ["transfer", "lifestyle"])
        data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("catr:42:transfer", data)
        self.assertIn("back:42", data)


class SaveEventTypeTests(unittest.TestCase):
    def test_save_event_type_updates_row(self):
        row = {
            "id": 7,
            "channel_slug": "vnf_editorial",
            "event_type": "lifestyle",
            "entities_json": "{}",
            "image_path": "",
            "media_path": "",
            "media_type": "image",
        }
        cfg = MagicMock()
        cfg.event_types = ["transfer", "injury", "match_result", "lifestyle"]
        cfg.template_for.side_effect = lambda et: "transfer" if et == "transfer" else "default"

        with (
            patch("editorial.moderation.get_news", side_effect=[row, {**row, "event_type": "transfer"}]),
            patch("editorial.moderation.get_channel", return_value=cfg),
            patch("editorial.moderation.update_news") as upd,
            patch("editorial.moderation.refresh_cover_after_category") as refresh,
            patch("editorial.moderation.log_moderation"),
            patch("editorial.moderation.get_settings") as gs,
        ):
            gs.return_value = MagicMock(telegram_admin_id=1)
            ok, msg = save_event_type(7, "transfer")
        self.assertTrue(ok)
        self.assertEqual(msg, "ok")
        refresh.assert_called_once_with(7, cfg)
        upd.assert_any_call(
            7,
            event_type="transfer",
            entities_json=unittest.mock.ANY,
            last_error="",
        )

    def test_refresh_cover_generates_ai_caption(self):
        from editorial.moderation import refresh_cover_after_category

        row = {
            "id": 9,
            "event_type": "transfer",
            "media_type": "image",
            "meme_source": 1,
            "post_kind": "meme",
            "post_text": "Алехандро Бальде принял решение покинуть «Барселону».",
            "title": "Бальде",
            "image_path": "",
            "media_path": "/tmp/balde.jpg",
            "caption_line1": "",
        }
        cfg = MagicMock()
        cfg.template_for.return_value = "transfer"

        with (
            patch("editorial.moderation.get_news", return_value=row),
            patch("editorial.moderation.Path") as P,
            patch("editorial.moderation.update_news") as upd,
            patch("editorial.moderation.get_settings") as gs,
            patch("editorial.caption.generate", return_value={"caption_line1": "Бальде уходит из Барселоны"}),
            patch("editorial.moderation.rerender_after_image", return_value="/tmp/cover.png") as rr,
        ):
            gs.return_value = MagicMock(meme_wrap_template=False)
            P.return_value.is_file.return_value = True
            out = refresh_cover_after_category(9, cfg)
        self.assertEqual(out, "/tmp/cover.png")
        upd.assert_any_call(
            9,
            caption="Бальде уходит из Барселоны",
            caption_line1="Бальде уходит из Барселоны",
            caption_line2="",
            headline="Бальде уходит из Барселоны",
            last_error="",
            post_kind="image",
        )
        rr.assert_called_once()


class SoccerBlogClassifyTests(unittest.TestCase):
    def test_transfer_keywords_from_tg_text(self):
        text = "«Ливерпуль» готовит трансфер нападающего из «ПСЖ»"
        self.assertEqual(classify_event_rules(text), "transfer")

    def test_lineup_plural_and_soccerblog_classifier(self):
        from editorial.topic_gate import classify_soccerblog_event

        self.assertEqual(
            classify_event_rules("Стартовые составы на матч «Ахмата» и «Ростова»"),
            "lineup",
        )
        self.assertEqual(
            classify_soccerblog_event("Когда судья показывает жёлтую на 89-й — классика"),
            "lifestyle",
        )
        self.assertEqual(
            classify_soccerblog_event("ХИРВИГОУ: Горетцка переходит в «Астон Виллу»!"),
            "transfer",
        )

    def test_soccerblog_skips_non_lifestyle(self):
        from editorial.channel_config import EditorialFeed
        from editorial.sources import parse_telegram_meme_feed

        class Post:
            def __init__(self, text, eid):
                self.text = text
                self.title = text[:80]
                self.external_id = eid
                self.source_url = f"https://t.me/x/{eid}"
                self.media = [{"type": "image", "url": "https://example.com/a.jpg"}]

        posts = [
            Post("ХИРВИГОУ: Горетцка переходит в «Астон Виллу»!", "1"),
            Post("Стартовые составы на матч «Ахмата» и «Ростова»", "2"),
            Post("Когда судья показывает жёлтую на 89-й — классика", "3"),
        ]
        feed = EditorialFeed(name="soccerblog_memes", kind="telegram", handle="x")
        with (
            unittest.mock.patch("app.config.get_settings") as gs,
            unittest.mock.patch("parsers.telegram.parse_telegram", return_value=("t", posts)),
            unittest.mock.patch("editorial.gate_cache.get_gate_verdict", return_value=None),
            unittest.mock.patch("editorial.gate_cache.put_gate_verdict"),
            unittest.mock.patch("editorial.tg_donor.is_text_seen", return_value=False),
            unittest.mock.patch("editorial.tg_donor.get_last_seen_id", return_value=0),
            unittest.mock.patch(
                "editorial.soccerblog_gate.soccerblog_gate",
                side_effect=[
                    {"kind": "reject", "confidence": 0.95, "reason": "transfer"},
                    {"kind": "reject", "confidence": 0.95, "reason": "lineup"},
                    {"kind": "meme", "confidence": 0.9, "reason": "lifestyle", "text_lang": "ru"},
                ],
            ),
        ):
            gs.return_value = unittest.mock.MagicMock(meme_source_enabled=True, tg_incremental=False)
            items = parse_telegram_meme_feed(feed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].event_type, "lifestyle")
        self.assertIn("жёлтую", items[0].title)


if __name__ == "__main__":
    unittest.main()
