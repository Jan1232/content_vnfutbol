from __future__ import annotations

import unittest
from unittest.mock import patch

from editorial.light_edit import (
    apply_emoji_rules,
    light_edit,
    normalize_caps,
    strip_branding,
)
from editorial.profanity import soften_profanity


class LightEditCapsTests(unittest.TestCase):
    def test_caps_to_sentence_preserves_abbreviations(self):
        raw = "ПСЖ ВЫИГРАЛ У ЦСКА В МАТЧЕ ЛЧ"
        out = normalize_caps(raw)
        self.assertIn("ПСЖ", out)
        self.assertIn("ЦСКА", out)
        self.assertIn("ЛЧ", out)
        self.assertIn("выиграл", out)
        self.assertIn(" в ", f" {out} ")

    def test_emotional_caps_phrase(self):
        raw = "СКОЛЬКО, СКОЛЬКО?!"
        out = normalize_caps(raw)
        self.assertEqual(out, "Сколько, сколько?!")


class LightEditStripTests(unittest.TestCase):
    def test_removes_links_and_handles(self):
        raw = "Новость про трансфер https://t.me/foo @donor_channel"
        out = strip_branding(raw)
        self.assertNotIn("t.me", out)
        self.assertNotIn("@donor", out)
        self.assertIn("трансфер", out)

    def test_removes_cta_tail(self):
        raw = "Гол в концовке. Подписывайся на наш канал!"
        out = strip_branding(raw)
        self.assertNotIn("Подписывайся", out)
        self.assertIn("Гол", out)


class LightEditProfanityTests(unittest.TestCase):
    def test_soften_profanity(self):
        raw = "Это пиздец какой трансфер"
        out = soften_profanity(raw)
        self.assertNotIn("пиздец", out.lower())
        self.assertIn("трансфер", out)


class LightEditToneTests(unittest.TestCase):
    def test_preserves_donor_humor(self):
        title = ""
        body = (
            "СКОЛЬКО, СКОЛЬКО?!\n\n"
            "К апрелю тренируюсь — мем года от болельщиков."
        )
        with patch("editorial.light_edit.apply_emoji_rules", side_effect=lambda t: t):
            result = light_edit(title, body, profanity_mode="soften")
        self.assertIn("Сколько, сколько?!", result["post_text"])
        self.assertIn("К апрелю тренируюсь", result["post_text"])

    def test_adds_topic_sticker_to_plain_paragraph(self):
        raw = "Клуб подписал нападающего за 20 млн евро."
        out = apply_emoji_rules(raw)
        self.assertTrue(out.startswith("✍") or out.startswith("💰"))


class LightEditPipelineTests(unittest.TestCase):
    def test_full_pipeline_no_llm(self):
        title = ""
        body = (
            "СКОЛЬКО, СКОЛЬКО?!\n\n"
            "Игрок забил гол. Источник: SoccerBlog https://t.me/x @foo"
        )
        with patch("editorial.cycle.rewrite_item") as mock_rewrite:
            with patch("editorial.cycle.light_edit", wraps=light_edit) as mock_light:
                from editorial.cycle import _step_edit

                row = {
                    "id": 1,
                    "title": title,
                    "body": body,
                    "meme_source": 0,
                    "media_type": "photo",
                    "entities_json": "{}",
                }
                with patch("editorial.cycle.get_news", return_value=row):
                    with patch("editorial.cycle.update_news") as mock_upd:
                        with patch("editorial.cycle.enrich_row", return_value=(row, {})):
                            _step_edit(row)
                mock_rewrite.assert_not_called()
                mock_light.assert_called_once()
                kwargs = mock_upd.call_args_list[-1].kwargs
                self.assertIn("Сколько, сколько?!", kwargs.get("post_text", ""))
                self.assertNotIn("t.me", kwargs.get("post_text", ""))


if __name__ == "__main__":
    unittest.main()
