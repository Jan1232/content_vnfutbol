"""Tests for LLM usage dashboard aggregation."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from editorial.usage import (
    _usage_summary_for_period,
    purge_old_llm_call_logs,
    sanitize_messages_for_log,
    usage_dashboard,
)


class SanitizeMessagesTests(unittest.TestCase):
    def test_strips_image_base64(self):
        import base64

        raw = b"jpegbytes"
        b64 = base64.b64encode(raw).decode()
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "check"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
                    },
                ],
            }
        ]
        out = sanitize_messages_for_log(msgs)
        part = out[0]["content"][1]
        self.assertIn("placeholder", part["image_url"])
        self.assertNotIn(b64, json.dumps(out))


class CallLogPurgeTests(unittest.TestCase):
    def test_purge_respects_retention(self):
        with patch("editorial.usage.db") as db_ctx:
            conn = MagicMock()
            db_ctx.return_value.__enter__.return_value = conn
            cur = MagicMock()
            cur.rowcount = 3
            conn.execute.return_value = cur
            n = purge_old_llm_call_logs(retention_days=7)
        self.assertEqual(n, 3)


class UsageDashboardTests(unittest.TestCase):
    def test_usage_dashboard_keys(self):
        row = {
            "task": "topic",
            "model": "gpt-5.6-luna",
            "n": 2,
            "ok_n": 2,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 0,
        }
        with patch("editorial.usage.db") as db_ctx:
            conn = MagicMock()
            db_ctx.return_value.__enter__.return_value = conn
            conn.execute.return_value.fetchall.return_value = [row]
            out = usage_dashboard()
        self.assertIn("h24", out)
        self.assertIn("d7", out)
        self.assertIn("call_log", out)
        self.assertEqual(out["h24"]["rows"][0]["total_tokens"], 120)
        self.assertEqual(out["h24"]["rows"][0]["avg_prompt"], 50)

    def test_period_label(self):
        with patch("editorial.usage.db") as db_ctx:
            conn = MagicMock()
            db_ctx.return_value.__enter__.return_value = conn
            conn.execute.return_value.fetchall.return_value = []
            self.assertEqual(_usage_summary_for_period(1)["label"], "24ч")
            self.assertEqual(_usage_summary_for_period(7)["label"], "7д")


if __name__ == "__main__":
    unittest.main()
