from __future__ import annotations

import unittest

from editorial.meme_text import prepare_meme_post
from editorial.profanity import replace_profanity


class MemeTextTests(unittest.TestCase):
    def test_keeps_source_text_only(self):
        row = {
            "meme_source": 1,
            "post_kind": "meme",
            "body": "КАК СЕЗОН НАЧНЁШЬ, ТАК ЕГО И ПРОВЕДЁШЬ? 😂",
            "title": "SoccerBlog",
        }
        out = prepare_meme_post(row)
        self.assertEqual(out["post_text"], row["body"])
        self.assertEqual(out["headline"], "")
        self.assertNotIn("Похоже", out["post_text"])

    def test_keeps_source_text(self):
        row = {
            "meme_source": 1,
            "post_kind": "meme",
            "body": "Гениальный Магuайр во время гола",
            "title": "SoccerBlog",
        }
        out = prepare_meme_post(row)
        self.assertIn("Магuайр", out["post_text"])

    def test_replace_profanity_ebalo(self):
        self.assertIn("лицо", replace_profanity("Моё ебало, когда эксперты ошиблись"))

    def test_replace_profanity_huylo(self):
        cleaned = replace_profanity("Кстати, а зачем это хуйло было куплено?")
        self.assertNotIn("хуй", cleaned.lower())
        self.assertIn("фигня", cleaned.lower())


if __name__ == "__main__":
    unittest.main()
