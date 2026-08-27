from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from editorial.models import NewsItem
from editorial.pick import (
    human_factor_share,
    pick,
    rule_reject,
)
from editorial.policy import HUMAN_FACTOR_CAP
from editorial.scheduler import pick_best
from editorial.topic_gate import classify_event_rules


def _item(title: str, body: str = "", event_type: str = "other") -> NewsItem:
    return NewsItem(
        external_id="t:1",
        source="test",
        url="https://example.com/1",
        title=title,
        body=body,
        lang="ru",
        published_at=datetime.now(timezone.utc),
        event_type=event_type,
    )


class RuleRejectTests(unittest.TestCase):
    def test_broadcast_rejected(self):
        item = _item("Фенербахче — Лион: смотреть онлайн прямую трансляцию матча, 18 августа 2026")
        v = rule_reject(item)
        self.assertIsNotNone(v)
        self.assertFalse(v.take)
        self.assertIn("трансляц", v.reason)

    def test_results_roundup_rejected(self):
        item = _item("Результаты матчей чемпионата России по футболу — 2026/2027 на 16 августа")
        v = rule_reject(item)
        self.assertIsNotNone(v)
        self.assertFalse(v.take)

    def test_schedule_rejected(self):
        item = _item("Расписание матчей Кубка Либертадорес — 2026 по футболу на 19 августа")
        v = rule_reject(item)
        self.assertIsNotNone(v)

    def test_video_review_rejected(self):
        item = _item("Видеообзор победы ЦСКА в матче 4-го тура РПЛ с «Факелом»")
        v = rule_reject(item)
        self.assertIsNotNone(v)

    def test_gossip_rejected(self):
        item = _item("Al Hilal target England's Kane - Wednesday's gossip", "Transfer rumours.")
        v = rule_reject(item)
        self.assertIsNotNone(v)

    def test_podcast_rejected(self):
        item = _item("Football Daily", "Arsenal seek to defend their Premier League title")
        v = rule_reject(item)
        self.assertIsNotNone(v)

    def test_preview_rejected(self):
        item = _item("Premier League 2026-27 preview No 15: Manchester City")
        v = rule_reject(item)
        self.assertIsNotNone(v)

    def test_rumor_rejected(self):
        item = _item("Мареска отреагировал на слухи о переходе Родри", event_type="rumor")
        v = rule_reject(item, allow_rumors=False)
        self.assertIsNotNone(v)
        self.assertIn("слух", v.reason)

    def test_rumor_allowed_when_flag_on(self):
        item = _item("Слухи о переходе Родри", event_type="rumor")
        v = rule_reject(item, allow_rumors=True)
        self.assertIsNone(v)

    def test_wedding_cost_not_broadcast(self):
        item = _item(
            "Сафонов о стоимости свадьбы с Кондратюк: «Больше 10 млн»",
            "Матвей Сафонов рассказал, во сколько обошлась его свадьба.",
            event_type="lifestyle",
        )
        self.assertIsNone(rule_reject(item))

    def test_top_transfer_not_rejected(self):
        item = _item(
            "«Барса» купила Родри у «Ман Сити» за 76,5 млн евро",
            "Контракт до 2030 года.",
            event_type="transfer",
        )
        self.assertIsNone(rule_reject(item))

    def test_cluster_repeat_without_llm(self):
        item = _item("Флик отреагировал на приобретение Родри из «Манчестер Сити»")
        v = pick(item, cluster_already_published=True, use_llm=False)
        self.assertFalse(v.take)
        self.assertEqual(v.reason, "повтор события")

    def test_human_factor_cap(self):
        published = [
            {"entities_json": json.dumps({"pick": {"tag": "human_factor"}})}
            for _ in range(4)
        ] + [{"entities_json": json.dumps({"pick": {"tag": "top_name"}})} for _ in range(6)]
        self.assertGreaterEqual(human_factor_share(published), HUMAN_FACTOR_CAP)
        item = _item("Появилось видео со свадебной церемонии Роналду и Джорджины")
        with patch(
            "editorial.llm.pick_news",
            return_value={"take": True, "tag": "human_factor", "reason": "человеческий фактор/юмор"},
        ):
            v = pick(item, human_factor_ratio=human_factor_share(published), use_llm=True)
        self.assertFalse(v.take)
        self.assertEqual(v.by, "cap")


class LifestyleClassifyTests(unittest.TestCase):
    def test_wedding_is_lifestyle(self):
        self.assertEqual(
            classify_event_rules("Появилось видео со свадебной церемонии Роналду"),
            "lifestyle",
        )


class PickBestTagTests(unittest.TestCase):
    def test_prefers_transfer_over_human_factor(self):
        hf = {
            "id": 1,
            "teams_json": '["Real Madrid"]',
            "entities_json": json.dumps({"pick": {"tag": "human_factor"}}),
            "source_published_at": "2026-08-19 12:00:00",
        }
        tr = {
            "id": 2,
            "teams_json": '["Brighton"]',
            "entities_json": json.dumps({"pick": {"tag": "transfer_money"}}),
            "source_published_at": "2026-08-19 11:00:00",
        }
        self.assertEqual(pick_best([hf, tr])["id"], 2)


class LabelPoolRuleSafetyTests(unittest.TestCase):
    """Правила не должны резать размеченные take=true (кроме служебного regex-шума)."""

    def test_true_labels_survive_rules(self):
        path = Path("/var/max-repost/data/editorial/labeling/pool_10d.json")
        if not path.is_file():
            self.skipTest("нет пула разметки")
        data = json.loads(path.read_text(encoding="utf-8"))
        true_items = [x for x in data.get("items") or [] if x.get("take_to_prod") is True]
        hits = []
        for row in true_items:
            item = _item(row.get("title") or "", row.get("body") or "", row.get("event_type_guess") or "other")
            v = rule_reject(item)
            if v is not None:
                hits.append((row["id"], row["title"][:80], v.reason))
        self.assertLessEqual(len(hits), 2, hits[:8])

    def test_service_false_labels_caught(self):
        path = Path("/var/max-repost/data/editorial/labeling/pool_10d.json")
        if not path.is_file():
            self.skipTest("нет пула разметки")
        data = json.loads(path.read_text(encoding="utf-8"))
        service = [
            x
            for x in data.get("items") or []
            if x.get("take_to_prod") is False
            and (x.get("note") or "").startswith("служебная")
        ]
        caught = sum(1 for x in service if rule_reject(_item(x["title"], x.get("body") or "")) is not None)
        self.assertGreaterEqual(caught, 80)
        self.assertGreaterEqual(caught / max(len(service), 1), 0.7)
