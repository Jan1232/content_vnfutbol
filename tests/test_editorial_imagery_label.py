from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from editorial.imagery_label import (
    apply_decision,
    card_for_item,
    fit_photo_to_template,
    unique_candidates,
)
from editorial.render import preview_html, tpl_asset_path


def _item() -> dict:
    return {
        "id": "img-0001",
        "photo": {
            "file": "photos/img-0001.jpg",
            "source_url": "https://img.championat.com/news/big/a/b/foo.jpg",
            "via": "article",
            "score": 1.0,
        },
        "model": {
            "outcome": "picked",
            "thought": {"who": "Арсенал", "reason": "празднование", "score": 1.0, "via": "article"},
            "vision": [
                {
                    "idx": 0,
                    "url": "https://img.championat.com/c/900x900/news/big/a/b/foo.jpg",
                    "via": "article",
                    "kept": True,
                    "score": 1.0,
                    "who": "Арсенал",
                    "reason": "празднование",
                    "wrong_subject": False,
                    "path": "",
                },
                {
                    "idx": 5,
                    "url": "https://ss.sport-express.ru/userfiles/kane.jpg",
                    "via": "yandex",
                    "kept": False,
                    "score": 0.0,
                    "who": "",
                    "reason": "другая команда",
                    "wrong_subject": True,
                    "path": "",
                },
            ],
        },
    }


class ImageryLabelTests(unittest.TestCase):
    def test_accept_model(self):
        rec = apply_decision(_item(), decision="accept", chosen_idx=None, note="")
        self.assertEqual(rec["decision"], "accept_model")
        self.assertTrue(rec["keep_photo"])
        self.assertTrue(rec["agree"])

    def test_accept_other(self):
        rec = apply_decision(_item(), decision="accept", chosen_idx=5, note="лучше Кейн")
        self.assertEqual(rec["decision"], "accept_other")
        self.assertEqual(rec["better_idx"], 5)
        self.assertFalse(rec["keep_photo"])
        self.assertFalse(rec["agree"])

    def test_better_query_saved_when_different(self):
        rec = apply_decision(
            _item(),
            decision="accept",
            chosen_idx=None,
            better_query="Арсенал Суперкубок футбол фото",
        )
        self.assertEqual(rec["better_query"], "Арсенал Суперкубок футбол фото")
        rec2 = apply_decision(
            {**_item(), "model": {**_item()["model"], "query": "Арсенал футбол фото"}},
            decision="accept",
            chosen_idx=None,
            better_query="  Арсенал футбол фото  ",
        )
        self.assertEqual(rec2["better_query"], "")

    def test_story_key_groups_super_cup(self):
        from editorial.imagery_label import story_key

        a = {
            "news": {"title": "«Арсенал» в восьмой раз выиграл Суперкубок Англии"},
            "model": {"query": "Арсенал выиграл Суперкубок Англии 2026"},
        }
        b = {
            "news": {"title": "Калафьори установил рекорд в Суперкубке Англии"},
            "model": {"query": "Калафьори Суперкубок Англии"},
        }
        c = {
            "news": {"title": "Сафонов в составе ПСЖ на матч с Лансом"},
            "model": {"query": "Матвей Сафонов ПСЖ"},
        }
        self.assertEqual(story_key(a), story_key(b))
        self.assertNotEqual(story_key(a), story_key(c))

    def test_none(self):
        rec = apply_decision(_item(), decision="none", chosen_idx=None, note="")
        self.assertEqual(rec["decision"], "none")
        self.assertFalse(rec["keep_photo"])

    def test_dedup_championat_sizes(self):
        item = _item()
        item["model"]["vision"].insert(
            1,
            {
                "idx": 1,
                "url": "https://img.championat.com/c/1200x900/news/big/a/b/foo.jpg",
                "via": "article",
                "kept": True,
                "score": 1.0,
                "who": "Арсенал",
                "reason": "тот же кадр",
                "wrong_subject": False,
                "path": "",
            },
        )
        cands = unique_candidates(item)
        keys = [c["url"] for c in cands]
        self.assertEqual(len(cands), 2)
        self.assertTrue(any(c["is_pick"] for c in cands))
        self.assertEqual(len(keys), len(set(keys)))

    def test_preview_html_uses_real_template(self):
        html = preview_html(
            "default",
            "/editorial/label-photos/photo/img-0001",
            "Арсенал взял суперкубок",
            None,
            "СЧЁТ",
            {"name": "ВСЕ НА ФУТБОЛ", "accent_color": "#E11D2A"},
        )
        self.assertIn('id="card"', html)
        self.assertIn("plashka_default.png", html)
        self.assertIn("logo.png", html)
        self.assertIn("Арсенал", html)

    def test_tpl_asset_allowlist(self):
        self.assertIsNotNone(tpl_asset_path("plashka_default.png"))
        self.assertIsNotNone(tpl_asset_path("plashka_transpher.png"))
        self.assertIsNotNone(tpl_asset_path("logo.png"))
        self.assertIsNone(tpl_asset_path("../default.html.j2"))
        self.assertIsNone(tpl_asset_path("deploy.prototxt"))

    def test_fit_photo_to_template_matches_prod_size(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw)
            src = d / "tall.jpg"
            Image.new("RGB", (400, 900), (20, 160, 50)).save(src, "JPEG")
            out = fit_photo_to_template(src, d / "out.jpg", "default")
            with Image.open(out) as im:
                self.assertEqual(im.size, (1080, 1080))
            transfer = fit_photo_to_template(src, d / "transfer.jpg", "transfer")
            with Image.open(transfer) as im:
                self.assertEqual(im.size, (1080, 1080))

    def test_transfer_card_uses_transfer_plashka(self):
        card = card_for_item({"news": {"event_type": "transfer", "title": "Игрок перешёл в клуб"}})
        self.assertEqual(card["template"], "transfer")
        self.assertEqual(card["width"], 1080)
        self.assertEqual(card["height"], 1080)
        html = preview_html(
            "transfer",
            "/editorial/label-photos/photo/img-0001",
            "Родри перешёл в Барселону",
            None,
            "ТРАНСФЕР",
            {"name": "ВСЕ НА ФУТБОЛ", "accent_color": "#E11D2A"},
        )
        self.assertIn("plashka_transpher.png", html)
        self.assertNotIn("plashka_default.png", html)
        self.assertIn("plashka_default.png", preview_html(
            "default",
            "/editorial/label-photos/photo/img-0001",
            "Арсенал взял суперкубок",
            None,
            "СЧЁТ",
            {"name": "ВСЕ НА ФУТБОЛ", "accent_color": "#E11D2A"},
        ))


if __name__ == "__main__":
    unittest.main()
