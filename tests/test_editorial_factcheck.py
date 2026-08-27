from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from editorial.factcheck import verify
from editorial.models import NewsItem
from editorial.topic_gate import cluster_id_for, extract_entities


def _item(title: str, body: str = "", url: str = "https://clickbait.example/health") -> NewsItem:
    entities = extract_entities(f"{title}\n{body}")
    item = NewsItem(
        external_id="t:health",
        source="clickbait",
        url=url,
        title=title,
        body=body,
        lang="ru",
        published_at=datetime.now(timezone.utc),
        entities=entities,
        event_type="injury",
    )
    item.cluster_id = cluster_id_for(item)
    return item


class FactcheckTests(unittest.TestCase):
    @patch("editorial.factcheck.llm.web_search", return_value=[])
    @patch("editorial.factcheck.list_recent_corpus", return_value=[])
    def test_single_sensational_health_rejected(self, _corpus, _web):
        item = _item(
            "У игрока X диагностировано редкое неврологическое заболевание",
            "Врачи якобы подтвердили страшный диагноз.",
        )
        verdict = verify(item, min_sources=2, use_llm=True, web_search=True)
        self.assertEqual(verdict.status, "REJECTED")
        self.assertIn("сенсацион", verdict.reason.lower())

    @patch("editorial.factcheck.llm.web_search", return_value=[])
    @patch("editorial.factcheck.record_domain")
    @patch("editorial.factcheck.cluster_domains", return_value=set())
    def test_three_independent_domains_confirmed(self, _seen, _rec, _web):
        item = _item(
            "Haaland scored a hat-trick as Manchester City beat Arsenal 3-1",
            "Premier League. Official match report.",
            url="https://www.bbc.com/sport/1",
        )
        item.event_type = "match_result"
        item.cluster_id = cluster_id_for(item)
        corpus = [
            {
                "cluster_id": item.cluster_id,
                "url": "https://www.theguardian.com/football/1",
                "source": "guardian",
                "title": item.title,
                "body": "City 3-1 Arsenal",
                "event_type": "match_result",
                "entities_json": "{}",
            },
            {
                "cluster_id": item.cluster_id,
                "url": "https://www.espn.com/soccer/1",
                "source": "espn",
                "title": item.title,
                "body": "Official: Haaland hat-trick",
                "event_type": "match_result",
                "entities_json": "{}",
            },
        ]
        with patch("editorial.factcheck.list_recent_corpus", return_value=corpus):
            verdict = verify(item, min_sources=2, use_llm=False, web_search=False)
        self.assertEqual(verdict.status, "CONFIRMED")
        self.assertGreaterEqual(verdict.unique_domains, 3)

    @patch("editorial.factcheck.llm.web_search", return_value=[])
    @patch("editorial.factcheck.record_domain")
    @patch("editorial.factcheck.cluster_domains", return_value=set())
    @patch(
        "editorial.factcheck.llm.factcheck",
        return_value={
            "consistent": False,
            "contradiction": "один источник пишет 2:1, другой 3:1",
            "is_official": False,
            "confidence": 0.4,
            "reason": "счёт не сходится",
        },
    )
    def test_contradiction_rejected(self, _fc, _seen, _rec, _web):
        item = _item(
            "Real Madrid beat Barcelona 2-1",
            "El Clasico.",
            url="https://www.marca.com/1",
        )
        item.event_type = "match_result"
        item.cluster_id = cluster_id_for(item)
        corpus = [
            {
                "cluster_id": item.cluster_id,
                "url": "https://www.as.com/1",
                "source": "as",
                "title": "Barca 3-1",
                "body": "другой счёт",
                "event_type": "match_result",
                "entities_json": "{}",
            },
        ]
        with patch("editorial.factcheck.list_recent_corpus", return_value=corpus):
            verdict = verify(item, min_sources=2, use_llm=True, web_search=False)
        self.assertEqual(verdict.status, "REJECTED")
        self.assertIn("противореч", verdict.reason.lower())

    @patch("editorial.factcheck.llm.web_search", side_effect=RuntimeError("search-api 429"))
    @patch("editorial.factcheck.list_recent_corpus", return_value=[])
    def test_search_api_fail_is_uncertain(self, _corpus, _web):
        item = _item(
            "Darwin Nunez to Trabzonspor from Al Hilal",
            "Transfer talks.",
            url="https://www.bbc.com/sport/1",
        )
        item.event_type = "transfer"
        verdict = verify(item, min_sources=2, use_llm=True, web_search=True)
        self.assertEqual(verdict.status, "UNCERTAIN")
        self.assertIn("search-api", verdict.reason.lower())

    @patch("editorial.factcheck.llm.web_search")
    @patch("editorial.factcheck.record_domain")
    @patch("editorial.factcheck.cluster_domains", return_value=set())
    def test_match_result_skips_search_api(self, _seen, _rec, web):
        item = _item(
            "Haaland scored a hat-trick as Manchester City beat Arsenal 3-1",
            "Premier League. Official match report.",
            url="https://www.bbc.com/sport/1",
        )
        item.event_type = "match_result"
        item.cluster_id = cluster_id_for(item)
        corpus = [
            {
                "cluster_id": item.cluster_id,
                "url": "https://www.theguardian.com/football/1",
                "source": "guardian",
                "title": item.title,
                "body": "City 3-1 Arsenal",
                "event_type": "match_result",
                "entities_json": "{}",
            },
        ]
        with patch("editorial.factcheck.list_recent_corpus", return_value=corpus):
            verdict = verify(item, min_sources=2, use_llm=False, web_search=True)
        web.assert_not_called()
        self.assertEqual(verdict.status, "CONFIRMED")

    @patch("editorial.factcheck.llm.web_search")
    def test_human_factor_skips_search_api(self, web):
        from editorial.factcheck import needs_web_search

        item = _item("Роналду показал новое тату", "lifestyle", url="https://www.sports.ru/1")
        item.event_type = "lifestyle"
        item.entities = {"pick": {"tag": "human_factor", "take": True}}
        self.assertFalse(needs_web_search(item))
        verify(item, min_sources=1, use_llm=False, web_search=True)
        web.assert_not_called()

    def test_transfer_and_injury_need_search(self):
        from editorial.factcheck import needs_web_search, search_query_for

        item = _item("Nunez to Trabzonspor", "", url="https://www.championat.com/1")
        item.event_type = "transfer"
        item.entities = {"players": ["Darwin Nunez"], "teams": ["Al Hilal", "Trabzonspor"]}
        self.assertTrue(needs_web_search(item))
        q = search_query_for(item)
        self.assertIn("Darwin Nunez", q)
        self.assertIn("Al Hilal", q)
        self.assertNotIn("Nunez to Trabzonspor", q)
        self.assertLessEqual(len(q), 160)

        inj = _item("Gatti hamstring", "")
        inj.event_type = "injury"
        self.assertTrue(needs_web_search(inj))


if __name__ == "__main__":
    unittest.main()
