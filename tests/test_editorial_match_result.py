from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from editorial.club_logos import find_slug, normalize_name, reload_catalog
from editorial.match_result import (
    extract_match_result_from_image,
    is_match_result_row,
    validate_match_result,
)
from editorial.render import render_match_result


class ClubLogoMatchTests(unittest.TestCase):
    def test_alias_ru_en(self):
        reload_catalog()
        self.assertEqual(find_slug("Галатасарай"), "galatasaray")
        self.assertEqual(find_slug("Galatasaray"), "galatasaray")
        self.assertEqual(find_slug("ПСЖ"), "psg")

    def test_normalize(self):
        self.assertEqual(normalize_name("  FC Barcelona  "), "barcelona")


class MatchResultValidateTests(unittest.TestCase):
    def test_low_confidence_held(self):
        data = {
            "home_team": "A",
            "away_team": "B",
            "score_home": 1,
            "score_away": 0,
            "scorers_home": [{"name": "X", "minute": "10"}],
            "scorers_away": [],
            "confidence": 0.4,
        }
        ok, why = validate_match_result(data, {"path": "/x", "missing": False}, {"path": "/y", "missing": False})
        self.assertFalse(ok)
        self.assertIn("confidence", why)

    def test_missing_logo_held(self):
        data = {
            "home_team": "Unknown FC",
            "away_team": "B",
            "score_home": 2,
            "score_away": 1,
            "scorers_home": [{"name": "X", "minute": "10"}],
            "scorers_away": [{"name": "Y", "minute": "20"}],
            "confidence": 0.95,
        }
        with patch("editorial.match_result.get_settings") as gs:
            gs.return_value = MagicMock(
                result_min_conf=0.7,
                result_require_scorers=True,
                result_logo_fallback=False,
            )
            ok, why = validate_match_result(
                data,
                {"path": "", "missing": True},
                {"path": "/y.png", "missing": False},
            )
        self.assertFalse(ok)
        self.assertIn("логотипа", why)


class MatchResultVisionTests(unittest.TestCase):
    def test_extract_from_image_mock(self):
        payload = {
            "home_team": "Галатасарай",
            "away_team": "Гёзтепе",
            "score_home": 3,
            "score_away": 2,
            "scorers_home": [
                {"name": "Санчес", "minute": "33"},
                {"name": "Сара", "minute": "55"},
                {"name": "Осимхен", "minute": "63"},
            ],
            "scorers_away": [
                {"name": "Мироши", "minute": "16"},
                {"name": "Хуан", "minute": "65"},
            ],
            "confidence": 0.92,
            "source": "image",
        }
        with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
            Path(tmp.name).write_bytes(b"\xff\xd8\xff\xd8")
            with (
                patch("editorial.imagery.preview_jpeg", return_value=b"jpeg"),
                patch("editorial.match_result.get_client") as gc,
            ):
                gc.return_value.vision.return_value = payload
                out = extract_match_result_from_image(tmp.name, "текст без счёта")
        self.assertEqual(out["score_home"], 3)
        self.assertEqual(out["score_away"], 2)
        self.assertEqual(len(out["scorers_home"]), 3)

    def test_is_match_result_row(self):
        row = {"event_type": "other", "entities_json": "{}"}
        ent = {"donor_gate": {"post_subtype": "match_result", "kind": "template"}}
        with patch("editorial.match_result.get_settings") as gs:
            gs.return_value = MagicMock(result_template_enabled=True)
            self.assertTrue(is_match_result_row(row, ent))


class MatchResultRenderTests(unittest.TestCase):
    def test_render_context(self):
        logo = Path("/var/max-repost/editorial/templates/assets/logo.png")
        if not logo.is_file():
            self.skipTest("no logo asset")
        match = {
            "home_team": "Галатасарай",
            "away_team": "Гёзтепе",
            "score_home": 3,
            "score_away": 2,
            "scorers_home": [{"name": "Санчес", "minute": "33"}],
            "scorers_away": [{"name": "Мироши", "minute": "16"}],
            "competition": "Суперлига",
            "home_logo": {"path": str(logo)},
            "away_logo": {"path": str(logo)},
        }
        with patch("editorial.render._screenshot") as shot:
            shot.side_effect = lambda html, out, w, h: out.write_bytes(b"x" * 1200)
            path = render_match_result(match, news_id="test_mr")
        self.assertTrue(path.endswith(".png"))


if __name__ == "__main__":
    unittest.main()
