from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from editorial import day_sim_label as dsl


class DaySimLabelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._patch = patch.object(dsl, "LABEL_ROOT", self.root)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write_pool(self, day: str, n: int = 2) -> None:
        d = self.root / f"day_{day}"
        (d / "covers").mkdir(parents=True)
        items = []
        for i in range(1, n + 1):
            lid = f"post-{i:03d}"
            cov = d / "covers" / f"{lid}.png"
            cov.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
            items.append(
                {
                    "id": lid,
                    "news_id": 100 + i,
                    "title": f"Новость {i}",
                    "post_text": f"Текст {i}",
                    "event_type": "transfer" if i == 1 else "match_result",
                    "cover_file": f"covers/{lid}.png",
                    "slot": f"{day} 10:0{i}:00",
                    "pick_tag": "top_name",
                    "pick_reason": "тест",
                    "factcheck": {"status": "confirmed", "reason": "ok"},
                }
            )
        payload = {"kind": "day_sim_posts", "day": day, "items": items, "stats": {"published": n}}
        (d / "pool.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_progress_and_decisions(self):
        day = "2026-08-20"
        self._write_pool(day)
        self.assertEqual(dsl.progress(day)["total"], 2)
        self.assertEqual(dsl.progress(day)["left"], 2)
        item = dsl.next_unlabeled(day)
        self.assertEqual(item["id"], "post-001")
        rec = dsl.apply_decision(item, decision="accept", comment="ок")
        dsl.save_label(day, rec)
        self.assertEqual(dsl.progress(day)["done"], 1)
        nxt = dsl.next_unlabeled(day, after_id="post-001")
        self.assertEqual(nxt["id"], "post-002")
        rec2 = dsl.apply_decision(nxt, decision="should_not_pool", comment="слухи")
        dsl.save_label(day, rec2)
        self.assertEqual(dsl.progress(day)["left"], 0)
        summary = dsl.write_summary(day)
        self.assertEqual(summary["accept"], 1)
        self.assertEqual(summary["should_not_pool"], 1)
        self.assertEqual(len(summary["comments"]), 2)

    def test_cover_path_safe(self):
        day = "2026-08-20"
        self._write_pool(day, n=1)
        item = dsl.item_by_id(day, "post-001")
        self.assertIsNotNone(dsl.cover_file(day, item))
        bad = {**item, "cover_file": "../pool.json"}
        self.assertIsNone(dsl.cover_file(day, bad))


if __name__ == "__main__":
    unittest.main()
