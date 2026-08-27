from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from editorial.imagery import ImageCandidate, _needs_strict_attribution, score_relevance


def _item(*, teams: list[str] | None = None, event_type: str = "transfer") -> dict:
    entities = {"teams": teams or ["Arsenal"], "players": ["Сака"]}
    return {
        "title": "Новая форма Арсенала представлена клубом",
        "event_type": event_type,
        "entities_json": json.dumps(entities, ensure_ascii=False),
    }


class AttributionTests(unittest.TestCase):
    def test_strict_when_club_in_entities(self):
        self.assertTrue(_needs_strict_attribution(_item()))

    def test_not_strict_for_generic(self):
        self.assertFalse(
            _needs_strict_attribution(
                {"title": "Обзор тура АПЛ", "event_type": "other", "entities_json": "{}"}
            )
        )

    def test_rejects_wrong_kit(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c0.jpg"
            p.write_bytes(b"\xff\xd8\xff\xd8")
            cand = ImageCandidate(path=p, url="http://x/0.jpg", via="test", width=800, height=600)
            vision = {
                "results": [
                    {
                        "idx": 0,
                        "relevant": True,
                        "subject_present": True,
                        "club_on_photo": "Krasnodar",
                        "league_on_photo": "RPL",
                        "attribution_match": False,
                        "quality": "good",
                        "score": 0.9,
                        "reason": "форма Кrasnodar",
                    }
                ]
            }

            class _FakeClient:
                def vision(self, *args, **kwargs):
                    return vision

            with patch("editorial.openai_client.get_client", return_value=_FakeClient()):
                with patch("editorial.imagery.preview_jpeg", return_value=b"jpeg"):
                    kept = score_relevance([cand], _item())
            self.assertEqual(kept, [])


if __name__ == "__main__":
    unittest.main()
