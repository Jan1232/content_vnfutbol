from __future__ import annotations

import unittest
from unittest.mock import patch

from editorial.editor import (
    normalize_ru_typo,
    post_text_ok,
    rewrite,
    strip_hedge_tails,
)


class HedgeTailTests(unittest.TestCase):
    def test_strips_no_official_announcement(self):
        src = (
            "🇧🇷 Малком близок к расторжению контракта с «Аль-Хилялем», сообщает ESPN.\n\n"
            "Следующим клубом бывшего нападающего «Барселоны» и «Зенита» может стать «Аль-Джазира». "
            "Официального объявления пока нет."
        )
        out = strip_hedge_tails(src)
        self.assertNotIn("Официального объявления пока нет", out)
        self.assertIn("Аль-Джазира", out)
        self.assertIn("ESPN", out)

    def test_strips_quote_not_given(self):
        src = (
            "🔵🔴 «Барселона» приобрела у «Манчестер Сити» обладателя «Золотого мяча» Родри. "
            "Английский клуб официально объявил о трансфере.\n\n"
            "После сделки Ханс-Дитер Флик обратился к руководству каталонцев по поводу дальнейших приобретений. "
            "Точная формулировка его реакции не приводится."
        )
        out = strip_hedge_tails(src)
        self.assertNotIn("не приводится", out)
        self.assertIn("официально объявил о трансфере", out)
        self.assertIn("Флик обратился", out)

    def test_keeps_clean_post(self):
        src = "Локомотив уступил Ростову в Кубке России со счётом 0:1."
        self.assertEqual(strip_hedge_tails(src), src)

    def test_strips_orphan_emoji_paragraph(self):
        src = "Батраков вылетел в Стамбул.\n\n✍️"
        out = strip_hedge_tails(src)
        self.assertEqual(out, "Батраков вылетел в Стамбул.")
        self.assertNotIn("✍️", out)

    def test_rewrite_keeps_structural_stickers_in_paragraphs(self):
        payload = {
            "post_text": (
                "⚽ Полузащитник «Локомотива» Алексей Батраков вылетел в Стамбул для медосмотра "
                "и подписания контракта с «Галатасараем».\n\n"
                "✍️ По данным «СЭ», хавбеку предоставили частный самолёт. В Турции рассчитывают, "
                "что переход завершится в ближайшие дни и игрок сразу присоединится к основе."
            ),
            "headline": "Батраков в Стамбуле",
            "emoji_lead": "⚽",
            "stickers": ["✍️", "⚽"],
        }
        with patch("editorial.editor.llm.rewrite", return_value=payload):
            out = rewrite({"title": "Батраков вылетел"}, facts="x")
        self.assertIn("✍️ По данным", out["post_text"])
        self.assertNotRegex(out["post_text"], r"\n\n✍️\s*$")
        self.assertIn("Галатасараем", out["post_text"])
        self.assertEqual(out["stickers"], ["✍️", "⚽"])

    def test_post_text_ok_rejects_english_and_short(self):
        ok, why = post_text_ok("Inter agree £30m deal for Liverpool's Jones", title="x")
        self.assertFalse(ok)
        self.assertIn("латиниц", why)
        ok, why = post_text_ok("Родри\n\n⚽", title="Родри")
        self.assertFalse(ok)

    def test_normalize_nested_quotes(self):
        t = normalize_ru_typo('«Энцо — игрок „Челси“», — заявил Алонсо')
        self.assertIn("«", t)
        self.assertNotIn("„", t)
        self.assertNotIn("“", t)


if __name__ == "__main__":
    unittest.main()
