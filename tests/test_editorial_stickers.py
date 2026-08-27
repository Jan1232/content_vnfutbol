from __future__ import annotations

import unittest
from unittest.mock import patch

from editorial.editor import accept_edited_text, post_text_ok, rewrite
from editorial.stickers import load_pool, register_from_text, save_pool


GOOD_POST = (
    "⚽ Полузащитник «Локомотива» Алексей Батраков вылетел в Стамбул для медосмотра "
    "и подписания контракта с «Галатасараем».\n\n"
    "✍️ По данным «СЭ», хавбеку предоставили частный самолёт. В Турции рассчитывают, "
    "что переход завершится в ближайшие дни и игрок сразу присоединится к основе."
)


class StickerTests(unittest.TestCase):
    def test_post_text_ok_requires_paragraph_stickers(self):
        ok, why = post_text_ok(GOOD_POST, title="Батраков")
        self.assertTrue(ok, why)

    def test_rejects_orphan_emoji_line(self):
        bad = "⚽ Текст про матч и трансфер с достаточным количеством слов для проверки.\n\n✍️"
        ok, why = post_text_ok(bad, title="x")
        self.assertFalse(ok)
        self.assertIn("orphan", why)

    def test_rejects_flat_text_without_stickers(self):
        flat = (
            "Полузащитник «Локомотива» Алексей Батраков вылетел в Стамбул для медосмотра "
            "и подписания контракта с «Галатасараем».\n\n"
            "По данным «СЭ», хавбеку предоставили частный самолёт. В Турции рассчитывают, "
            "что переход завершится в ближайшие дни."
        )
        ok, why = post_text_ok(flat, title="Батраков")
        self.assertFalse(ok)
        self.assertIn("стикер", why)

    def test_rewrite_accepts_stickered_post(self):
        payload = {
            "post_text": GOOD_POST,
            "headline": "Батраков в Стамбуле",
            "emoji_lead": "⚽",
            "stickers": ["✍️"],
        }
        with patch("editorial.editor.llm.rewrite", return_value=payload):
            out = rewrite({"title": "Батраков вылетел"}, facts="x")
        self.assertIn("⚽", out["post_text"])
        self.assertIn("✍️", out["post_text"])

    def test_accepts_unknown_emoji_and_registers_in_pool(self):
        post = (
            "🤯 «Манчестер Юнайтед» сенсационно уступил «Халл Сити» в матче Кубка лиги "
            "и вылетел из турнира уже на ранней стадии.\n\n"
            "🔥 Новичок АПЛ начал сезон с громкой сенсации и доказал, что готов бороться "
            "с грандами английского футбола на равных."
        )
        ok, why = post_text_ok(post, title="Hull City")
        self.assertTrue(ok, why)

        save_pool(["⚽", "🔥"])
        added = register_from_text(post)
        self.assertIn("🤯", added)
        self.assertIn("🤯", load_pool())

    def test_accept_edited_text_allows_single_paragraph(self):
        post = (
            "😂 Болельщик «Манчестер Юнайтед» Фрэнк Иллетт показал, "
            "как выглядит на 686-й день без стрижки."
        )
        ok, why, cleaned = accept_edited_text(post, title="Иллетт")
        self.assertTrue(ok, why)
        self.assertIn("Иллетт", cleaned)

    def test_accept_edited_text_registers_new_stickers(self):
        post = (
            "🤯 «Манчестер Юнайтед» сенсационно уступил «Халл Сити» в матче Кубка лиги "
            "и вылетел из турнира уже на ранней стадии.\n\n"
            "🔥 Новичок АПЛ начал сезон с громкой сенсации и доказал, что готов бороться "
            "с грандами английского футбола на равных."
        )
        save_pool(["⚽"])
        ok, why, cleaned = accept_edited_text(post, title="Hull City")
        self.assertTrue(ok, why)
        self.assertIn("🤯", load_pool())
        self.assertIn("🤯", cleaned)


if __name__ == "__main__":
    unittest.main()
