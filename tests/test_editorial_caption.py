from __future__ import annotations

import unittest
from unittest.mock import patch

from editorial.caption import generate, similarity
from editorial.cover_text import (
    MAX_LINES,
    clip_to_cover,
    fits_cover,
    line_count,
    wrap_lines,
)


class CaptionSimilarityTests(unittest.TestCase):
    def test_identical_is_high(self):
        text = "Барселона заплатит 76,5 млн евро за Родри"
        self.assertGreater(similarity(text, text), 0.6)

    def test_paraphrase_can_pass(self):
        post = (
            "Барселона закрыла трансфер полузащитника Родри. "
            "Сумма сделки — 76,5 млн евро, контракт до 2030 года."
        )
        caption = "Родри переходит в Барселону за €76,5 млн"
        self.assertLessEqual(similarity(caption, post), 0.85)
        # другой лексический каркас — не дословный дубль
        self.assertNotEqual(caption.lower(), post.lower())

    def test_normalize_quote_and_dash(self):
        from editorial.caption import _normalize_ru_typo

        self.assertEqual(
            _normalize_ru_typo('"Ребята расстроены" - Галактионов'),
            "«Ребята расстроены» — Галактионов",
        )
        self.assertEqual(
            _normalize_ru_typo("«Цитата»,— тренер"),
            "«Цитата», — тренер",
        )


class CoverTextBudgetTests(unittest.TestCase):
    def test_short_phrase_keeps_all_words(self):
        text = "Локомотив уступил Ростову в Кубке"
        self.assertEqual(clip_to_cover(text), text)
        self.assertLessEqual(line_count(text), MAX_LINES)

    def test_long_phrase_fits_four_lines(self):
        text = (
            "Спартак подписал контракт с новым нападающим до 2030 года "
            "после долгого трансфера из Европы и сразу заявил его в Лигу чемпионов"
        )
        clipped = clip_to_cover(text)
        self.assertEqual(
            clipped,
            "Спартак подписал контракт с новым нападающим до 2030 года",
        )
        self.assertTrue(fits_cover(clipped))
        self.assertLessEqual(len(wrap_lines(clipped)), MAX_LINES)

    def test_generate_merges_legacy_line2_and_clips(self):
        payload = {
            "caption_line1": "Спартак подписал контракт с новым нападающим до 2030 года",
            "caption_line2": "после долгого трансфера из Европы и сразу заявил его",
        }
        with patch("editorial.caption.llm.caption", return_value=payload):
            out = generate({"title": "Спартак новость", "entities_json": "{}"}, "другой пост про зиму")
        self.assertIsNone(out["caption_line2"])
        self.assertTrue(out["caption_line1"])
        self.assertTrue(fits_cover(out["caption_line1"]))
        self.assertNotIn("заявил его", out["caption_line1"])

    def test_generate_falls_back_when_llm_json_breaks(self):
        with patch("editorial.caption.llm.caption", side_effect=ValueError("нет JSON-объекта")):
            out = generate(
                {"title": "Родри перешёл в Барселону", "entities_json": "{}"},
                "длинный другой пост про зиму и снег",
            )
        self.assertTrue(out["caption_line1"])
        self.assertIn("Родри", out["caption_line1"])


if __name__ == "__main__":
    unittest.main()
