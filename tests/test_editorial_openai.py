from __future__ import annotations

import unittest
from unittest.mock import patch

from editorial.openai_client import OpenAIClient, assert_platform_transport, _hits_from_search


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or '{"ok":true}'

    def json(self):
        return self._payload


class _Client:
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        _Client.calls.append({"kwargs": kwargs})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        _Client.calls[-1]["url"] = url
        _Client.calls[-1]["json"] = json
        if json and json.get("model") == "missing-model":
            return _Resp(
                400,
                {"error": {"code": "model_not_found", "message": "does not exist"}},
                '{"error":{"code":"model_not_found","message":"does not exist"}}',
            )
        return _Resp(
            200,
            {
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 4},
            },
            '{"id":"x"}',
        )


class TransportTests(unittest.TestCase):
    def test_rejects_openclaw_gateway(self):
        with self.assertRaisesRegex(RuntimeError, "OpenClaw"):
            assert_platform_transport("http://127.0.0.1:18789/v1")

    def test_accepts_platform(self):
        assert_platform_transport("https://api.openai.com/v1")


class OpenAIClientTests(unittest.TestCase):
    def setUp(self):
        _Client.calls = []
        self.client = OpenAIClient(
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
            proxy="http://127.0.0.1:10809",
            timeout=10,
            max_retry=1,
        )

    def test_chat_uses_proxy_and_completion_tokens(self):
        with (
            patch("editorial.openai_client.httpx.Client", _Client),
            patch("editorial.openai_client._record_usage"),
        ):
            out = self.client.chat(
                "gpt-5.6-luna",
                [{"role": "user", "content": "ping"}],
                json_mode=True,
                max_tokens=64,
                fallback="gpt-5-mini",
                task="rewrite",
            )
        self.assertIn("ok", out)
        last = _Client.calls[-1]
        self.assertEqual(last["kwargs"].get("proxy"), "http://127.0.0.1:10809")
        self.assertTrue(str(last["url"]).endswith("/chat/completions"))
        self.assertEqual(last["json"]["model"], "gpt-5.6-luna")
        self.assertIn("max_completion_tokens", last["json"])
        self.assertNotIn("max_tokens", last["json"])
        self.assertEqual(last["json"]["response_format"], {"type": "json_object"})

    def test_model_not_found_falls_back(self):
        with (
            patch("editorial.openai_client.httpx.Client", _Client),
            patch("editorial.openai_client._record_usage"),
        ):
            out = self.client.chat(
                "missing-model",
                [{"role": "user", "content": "ping"}],
                fallback="gpt-5-mini",
            )
        self.assertIn("ok", out)
        models = [c["json"]["model"] for c in _Client.calls if c.get("json")]
        self.assertEqual(models, ["missing-model", "gpt-5-mini"])

    def test_vision_sends_images_and_json_mode(self):
        with (
            patch("editorial.openai_client.httpx.Client", _Client),
            patch("editorial.openai_client._record_usage"),
        ):
            data = self.client.vision(
                "gpt-4o-mini",
                [b"\xff\xd8fakejpeg"],
                "score these",
                task="image_vision",
            )
        self.assertEqual(data.get("ok"), True)
        payload = _Client.calls[-1]["json"]
        self.assertEqual(payload["model"], "gpt-4o-mini")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        user = payload["messages"][1]["content"]
        self.assertEqual(user[0]["type"], "text")
        self.assertEqual(user[1]["type"], "image_url")
        self.assertIn("data:image/jpeg;base64,", user[1]["image_url"]["url"])
        self.assertEqual(user[1]["image_url"]["detail"], "low")

    def test_refuses_without_proxy(self):
        with self.assertRaisesRegex(RuntimeError, "PROXY"):
            OpenAIClient(api_key="sk-test", proxy=None)

    def test_search_hits_from_json_and_annotations(self):
        text = '{"results":[{"title":"BBC","url":"https://www.bbc.co.uk/a","snippet":"Mbappe","domain":"bbc.co.uk"}]}'
        anns = [
            {
                "type": "url_citation",
                "url_citation": {
                    "title": "ESPN",
                    "url": "https://www.espn.com/b",
                    "start_index": 0,
                    "end_index": 1,
                },
            }
        ]
        hits = _hits_from_search(text, anns, limit=8)
        domains = {h["domain"] for h in hits}
        self.assertIn("bbc.co.uk", domains)
        self.assertIn("espn.com", domains)


class GroqFlagTests(unittest.TestCase):
    def test_llm_does_not_import_openclaw_ids(self):
        import editorial.llm as llm

        self.assertFalse(hasattr(llm, "_chat_openclaw"))
        self.assertFalse(hasattr(llm, "_editorial_openclaw_ids"))
