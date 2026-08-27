from __future__ import annotations

import unittest
from datetime import datetime, timezone

from editorial.models import NewsItem
from editorial.topic_gate import check, extract_entities, rule_prefilter


def _item(title: str, body: str = "") -> NewsItem:
    text = f"{title}\n{body}"
    entities = extract_entities(text)
    return NewsItem(
        external_id="t:1",
        source="test",
        url="https://example.com/1",
        title=title,
        body=body,
        lang="ru",
        published_at=datetime.now(timezone.utc),
        entities=entities,
        event_type=entities.get("event_type") or "other",
    )


class TopicGateTests(unittest.TestCase):
    def test_basketball_off_topic(self):
        item = _item("НБА: «Лейкерс» обыграли «Бостон» в седьмом матче")
        ok, reason = rule_prefilter(item)
        self.assertFalse(ok, reason)
        is_fb, _, _ = check(item, use_llm=False)
        self.assertFalse(is_fb)

    def test_politics_off_topic(self):
        item = _item("Госдума приняла новый закон о выборах")
        ok, reason = rule_prefilter(item)
        self.assertFalse(ok, reason)

    def test_football_match_passes(self):
        item = _item("Реал Мадрид обыграл Барселону в Эль-Класико, счёт 2:1")
        ok, reason = rule_prefilter(item)
        self.assertTrue(ok, reason)
        self.assertTrue(item.entities.get("teams"))
        is_fb, _, _ = check(item, use_llm=False)
        self.assertTrue(is_fb)

    def test_transfer_passes(self):
        item = _item("Арсенал подписал нового игрока в летнее трансферное окно")
        ok, reason = rule_prefilter(item)
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
