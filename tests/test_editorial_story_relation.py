"""Tests for reasoning model routing and stable system prefix (prompt caching)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class ReasoningModelTests(unittest.TestCase):
    def test_story_relation_hybrid_luna_first(self):
        from editorial import llm as llm_mod

        captured: list[str] = []

        def _chat(model, messages, **kwargs):
            captured.append(model)
            return '{"relation":"duplicate","new_facts":[],"confidence":0.9,"reason":"x"}'

        with (
            patch.object(llm_mod, "get_client") as gc,
            patch.object(
                llm_mod,
                "get_settings",
                return_value=MagicMock(
                    editorial_reasoning_model="gpt-5.6-terra",
                    editorial_reasoning_fallback="gpt-5.6-luna",
                    editorial_text_model="gpt-5.6-luna",
                    editorial_text_fallback="gpt-5-mini",
                    editorial_allow_groq_fallback=False,
                    story_relation_hybrid=True,
                    reasoning_escalate=0.7,
                ),
            ),
        ):
            client = MagicMock()
            client.chat.side_effect = _chat
            gc.return_value = client
            out = llm_mod.story_relation("Драка", "текст", ["уже была драка"])
        self.assertEqual(out["relation"], "duplicate")
        self.assertEqual(captured[0], "gpt-5.6-luna")
        self.assertEqual(client.chat.call_args.kwargs.get("task"), "story_relation_luna")
        self.assertEqual(len(captured), 1)

    def test_story_relation_escalates_to_terra(self):
        from editorial import llm as llm_mod

        captured: list[str] = []

        def _chat(model, messages, **kwargs):
            captured.append(model)
            if len(captured) == 1:
                return '{"relation":"development","confidence":0.4,"reason":"unclear"}'
            return '{"relation":"development","confidence":0.9,"reason":"new fact"}'

        with (
            patch.object(llm_mod, "get_client") as gc,
            patch.object(
                llm_mod,
                "get_settings",
                return_value=MagicMock(
                    editorial_reasoning_model="gpt-5.6-terra",
                    editorial_reasoning_fallback="gpt-5.6-luna",
                    editorial_text_model="gpt-5.6-luna",
                    editorial_text_fallback="gpt-5-mini",
                    editorial_allow_groq_fallback=False,
                    story_relation_hybrid=True,
                    reasoning_escalate=0.7,
                ),
            ),
        ):
            client = MagicMock()
            client.chat.side_effect = _chat
            gc.return_value = client
            out = llm_mod.story_relation("Драка", "текст", ["уже была драка"])
        self.assertEqual(out["relation"], "development")
        self.assertEqual(captured, ["gpt-5.6-luna", "gpt-5.6-terra"])

    def test_rewrite_system_prefix_stable_no_post_vars(self):
        from editorial import llm as llm_mod

        seen: list[list] = []

        def _fake_chat(messages, **kwargs):
            seen.append(messages)
            return '{"post_text":"x","headline":"h","emoji_lead":"⚽","stickers":[]}'

        with (
            patch.object(llm_mod, "chat", side_effect=_fake_chat),
            patch("editorial.stickers.pool_for_prompt", return_value=["🔥"]),
        ):
            llm_mod.rewrite({"title": "Новость А", "body": "тело А", "event_type": "transfer"}, facts="f1")
            llm_mod.rewrite({"title": "Новость Б", "body": "тело Б", "event_type": "injury"}, facts="f2")
        self.assertEqual(len(seen), 2)
        sys_a = seen[0][0]["content"]
        sys_b = seen[1][0]["content"]
        self.assertEqual(sys_a, sys_b)
        self.assertNotIn("Новость А", sys_a)
        self.assertNotIn("тело А", sys_a)
        self.assertIn("Новость А", seen[0][1]["content"])
        self.assertIn("🔥", seen[0][1]["content"])  # пул стикеров в user


if __name__ == "__main__":
    unittest.main()
