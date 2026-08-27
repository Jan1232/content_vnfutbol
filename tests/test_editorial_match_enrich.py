from __future__ import annotations

import unittest
from unittest.mock import patch

from editorial.match_enrich import (
    body_has_score,
    enrich_match_body,
    parse_score_from_text,
    parse_score_from_url,
)

HULL_URL = (
    "https://www.championat.com/football/news-6591710-hall-siti-manchester-yunajted-"
    "rezultat-matcha-22-avgusta-schet-2-0-1-j-tur-apl-2026-2027.html"
)
RSS_BODY = (
    "Окончен матч 1-го тура английской Премьер-лиги сезона-2026/2027, "
    "в котором играли «Халл Сити» и «Манчестер Юнайтед»."
)
ARTICLE_HTML = """
<html><body>
<div class="article-content">
<p>Окончен матч 1-го тура английской Премьер-лиги сезона-2026/2027, в котором играли «Халл Сити» и «Манчестер Юнайтед». Победу со счётом 2:0 в этой игре одержали «тигры».</p>
<p>Семи Аджайи вывел «Халл Сити» вперёд на 17-й минуте.</p>
</div>
</body></html>
"""


class MatchEnrichTests(unittest.TestCase):
    def test_parse_score_from_championat_url(self):
        self.assertEqual(parse_score_from_url(HULL_URL), (2, 0))

    def test_parse_score_from_text(self):
        self.assertEqual(parse_score_from_text("Победу со счётом 2:0 одержали тигры"), (2, 0))

    def test_body_without_score_detected(self):
        self.assertFalse(body_has_score(RSS_BODY))

    def test_light_enrich_from_url(self):
        body, meta = enrich_match_body(
            title="«МЮ» сенсационно уступил «Халл Сити»",
            body=RSS_BODY,
            url=HULL_URL,
            event_type="match_result",
            fetch_article=False,
        )
        self.assertTrue(meta.get("enriched"))
        self.assertEqual(meta.get("score"), "2:0")
        self.assertIn("2:0", body)

    def test_full_enrich_fetches_article(self):
        with patch(
            "editorial.match_enrich.fetch_article_body",
            return_value=(
                "Окончен матч. Победу со счётом 2:0 в этой игре одержали «тигры».\n\n"
                "Семи Аджайи вывел «Халл Сити» вперёд на 17-й минуте."
            ),
        ):
            body, meta = enrich_match_body(
                title="Результат матча",
                body=RSS_BODY,
                url="https://example.com/news/football/team-a-team-b.html",
                event_type="match_result",
                fetch_article=True,
            )
        self.assertEqual(meta.get("via"), "article")
        self.assertEqual(meta.get("score"), "2:0")
        self.assertIn("Аджайи", body)

    def test_championat_article_parser(self):
        from editorial.match_enrich import _championat_article_text

        text = _championat_article_text(ARTICLE_HTML)
        self.assertIn("2:0", text)
        self.assertIn("Аджайи", text)


if __name__ == "__main__":
    unittest.main()
