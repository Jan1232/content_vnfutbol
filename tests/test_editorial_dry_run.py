from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from editorial.channel_config import EditorialChannelConfig
from editorial.publisher import publish


class DryRunPublishTests(unittest.TestCase):
    def test_dry_run_does_not_call_max(self):
        client = MagicMock()
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n" + b"0" * 1200)
            tmp.flush()
            cfg = EditorialChannelConfig(
                slug="vnf_editorial",
                chat_id=-75796650734896,
                dry_run=True,
            )
            item = {
                "id": 99,
                "cover_path": tmp.name,
                "post_text": "Тестовый пост",
                "url": "https://example.com/news",
                "cluster_id": "",
            }
            with (
                patch("editorial.publisher.ensure_editorial_source", return_value=32),
                patch("editorial.publisher.insert_simulated_editorial_post", return_value=True) as ins,
                patch("editorial.publisher.update_news") as upd,
                patch("editorial.publisher.db") as db_ctx,
            ):
                db_ctx.return_value.__enter__.return_value = MagicMock()
                db_ctx.return_value.__exit__.return_value = False
                res = publish(client, cfg, item)
            self.assertEqual(res["action"], "simulated")
            client.send_message.assert_not_called()
            client.upload_image_bytes.assert_not_called()
            ins.assert_called_once()
            self.assertEqual(upd.call_args.kwargs.get("status"), "published")
            self.assertEqual(upd.call_args.kwargs.get("mid"), "simulated")

    def test_live_publish_sends_to_max(self):
        client = MagicMock()
        client.upload_image_bytes.return_value = {"type": "image", "payload": {"token": "x"}}
        client.send_message.return_value = {"mid": "mid.abc"}
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n" + b"0" * 1200)
            tmp.flush()
            cfg = EditorialChannelConfig(
                slug="vnf_editorial",
                chat_id=-75796650734896,
                dry_run=False,
            )
            item = {
                "id": 7,
                "cover_path": tmp.name,
                "post_text": "Боевой пост",
                "cluster_id": "",
            }
            with patch("editorial.publisher.update_news"):
                res = publish(client, cfg, item)
            self.assertEqual(res["action"], "published")
            client.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
