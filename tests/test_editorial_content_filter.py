from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from editorial.content_filter import load_content_filter
from editorial.models import NewsItem
from editorial.pick import pick, rule_reject


def _row(title: str, body: str = "", event_type: str = "other", lang: str = "ru") -> dict:
    return {
        "title": title,
        "body": body,
        "lang": lang,
        "event_type_guess": event_type,
        "published_at": "2026-08-19T12:00:00+00:00",
    }


class ContentFilterUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cf = load_content_filter()

    def test_reaction_without_event_rejected(self):
        dec = self.cf.decide(_row("Тренер «Локомотива» Галактионов отреагировал на поражение от «Ростова»"))
        self.assertFalse(dec.take)
        self.assertIn("реакц", dec.note)

    def test_bare_score_rejected(self):
        dec = self.cf.decide(
            _row("ПСЖ обыграл Астон Виллу со счётом 2:1", event_type="match_result")
        )
        self.assertFalse(dec.take)

    def test_friendly_without_narrative_rejected(self):
        dec = self.cf.decide(_row("Товарищеский матч. «Челси» победил «Сосьедад»"))
        self.assertFalse(dec.take)
        self.assertIn("товарищеск", dec.note)

    def test_transfer_with_fee_taken(self):
        dec = self.cf.decide(
            _row(
                "«Барса» купила Родри у «Ман Сити» за 76,5 млн евро",
                "Контракт до 2030 года.",
                event_type="transfer",
            )
        )
        self.assertTrue(dec.take)
        self.assertEqual(dec.tag, "transfer_money")

    def test_service_broadcast_rejected(self):
        dec = self.cf.decide(_row("Фенербахче — Лион: смотреть онлайн прямую трансляцию матча"))
        self.assertFalse(dec.take)

    def test_rpl_routine_rejected(self):
        dec = self.cf.decide(
            _row("Черчесов о 2:1 с «Факелом»: «Ахмат» играл так, как хотел", event_type="match_result")
        )
        self.assertFalse(dec.take)
        self.assertIn("РПЛ", dec.note)

    def test_ru_export_taken(self):
        dec = self.cf.decide(
            _row(
                "«Галатасарай» предложил «Локомотиву» € 28 млн за Батракова",
                event_type="transfer",
            )
        )
        self.assertTrue(dec.take, dec.note)
        self.assertEqual(dec.tag, "rpl_exception")

    def test_dedup_repeat_vs_addition(self):
        items = [
            _row("«Барселона» подписала Родри", event_type="transfer"),
            _row(
                "«Барселона» подписала Родри: медосмотр пройден, контракт до 2030, сумма 76 млн евро",
                event_type="transfer",
            ),
            _row("«Барселона» подписала Родри", event_type="transfer"),
        ]
        items[0]["published_at"] = "2026-08-10T10:00:00+00:00"
        items[1]["published_at"] = "2026-08-10T12:00:00+00:00"
        items[2]["published_at"] = "2026-08-10T14:00:00+00:00"
        self.cf.decide_batch(items)
        self.assertTrue(items[0]["model_take"])
        self.assertTrue(items[1]["model_take"])
        self.assertEqual(items[1]["model_tag"], "addition")
        self.assertFalse(items[2]["model_take"])
        self.assertEqual(items[2]["note"], "повтор события")


class ContentFilterGoldTests(unittest.TestCase):
    def test_f1_against_annotated_pool_by_url(self):
        path = Path("/var/max-repost/data/editorial/labeling/result/pool_14d_annotated.json")
        if not path.is_file():
            self.skipTest("нет золотой разметки 14d")
        data = json.loads(path.read_text(encoding="utf-8"))
        items = []
        gold = {}
        for row in data.get("items") or []:
            url = row.get("url") or ""
            if not url or row.get("take_to_prod") is None:
                continue
            gold[url] = bool(row["take_to_prod"])
            items.append(
                {
                    "title": row.get("title") or "",
                    "body": row.get("body") or "",
                    "lang": row.get("lang") or "ru",
                    "event_type_guess": row.get("event_type_guess") or "other",
                    "published_at": row.get("published_at") or "",
                    "url": url,
                }
            )
        cf = load_content_filter()
        cf.decide_batch(items)
        tp = fp = fn = 0
        for it in items:
            pred = bool(it["model_take"])
            g = gold[it["url"]]
            if pred and g:
                tp += 1
            elif pred and not g:
                fp += 1
            elif (not pred) and g:
                fn += 1
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        self.assertGreaterEqual(f1, 0.70, f"F1={f1:.3f} P={prec:.3f} R={rec:.3f} tp={tp} fp={fp} fn={fn}")


class PickCompatTests(unittest.TestCase):
    def test_rule_reject_broadcast(self):
        item = NewsItem(
            external_id="t",
            source="t",
            url="https://example.com/1",
            title="Фенербахче — Лион: смотреть онлайн прямую трансляцию матча",
            body="",
            lang="ru",
            published_at=datetime.now(timezone.utc),
        )
        v = rule_reject(item)
        self.assertIsNotNone(v)
        self.assertFalse(v.take)

    def test_pick_offline_filters_reaction(self):
        item = NewsItem(
            external_id="t",
            source="t",
            url="https://example.com/1",
            title="Флик отреагировал на приобретение Родри из «Манчестер Сити»",
            body="",
            lang="ru",
            published_at=datetime.now(timezone.utc),
            event_type="other",
        )
        v = pick(item, use_llm=False)
        self.assertFalse(v.take)


if __name__ == "__main__":
    unittest.main()
