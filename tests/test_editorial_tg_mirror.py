from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from editorial.channel_config import EditorialChannelConfig, TelegramMirrorConfig
from editorial.publisher import publish
from editorial.tg_content.publisher import mirror_enabled, publish_mirror


class TelegramMirrorTests(unittest.TestCase):
    def test_mirror_enabled_requires_token_and_channel(self):
        cfg = EditorialChannelConfig(
            slug="vnf_editorial",
            chat_id=-1,
            telegram_mirror=TelegramMirrorConfig(enabled=True, channel="@vnfutbol"),
        )
        with patch("editorial.tg_content.publisher.get_settings") as gs:
            gs.return_value.telegram_content_bot_token = "tok"
            gs.return_value.telegram_content_channel = ""
            self.assertTrue(mirror_enabled(cfg))
        with patch("editorial.tg_content.publisher.get_settings") as gs:
            gs.return_value.telegram_content_bot_token = ""
            gs.return_value.telegram_content_channel = "@vnfutbol"
            self.assertFalse(mirror_enabled(cfg))

    def test_publish_mirror_sends_photo(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n" + b"0" * 1200)
            tmp.flush()
            cfg = EditorialChannelConfig(
                slug="vnf_editorial",
                chat_id=-1,
                telegram_mirror=TelegramMirrorConfig(enabled=True, channel="@vnfutbol"),
            )
            item = {"post_text": "Тест", "cover_path": tmp.name}
            with patch("editorial.tg_content.publisher.api.send_photo") as send:
                send.return_value = {"message_id": 42}
                res = publish_mirror(cfg, item)
            self.assertTrue(res["ok"])
            self.assertEqual(res["message_id"], 42)
            send.assert_called_once()

    def test_live_publish_triggers_tg_mirror(self):
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
                telegram_mirror=TelegramMirrorConfig(enabled=True, channel="@vnfutbol"),
            )
            item = {
                "id": 7,
                "cover_path": tmp.name,
                "post_text": "Боевой пост",
                "cluster_id": "",
            }
            with (
                patch("editorial.publisher.update_news"),
                patch("editorial.publisher.story_gate", return_value=(True, "", "", 0)),
                patch("editorial.publisher.mirror_enabled", return_value=True),
                patch("editorial.tg_content.publisher.publish_mirror") as mirror,
            ):
                mirror.return_value = {"ok": True, "message_id": 99}
                res = publish(client, cfg, item)
            self.assertEqual(res["action"], "published")
            self.assertTrue(res["tg_mirror"]["ok"])
            mirror.assert_called_once()


if __name__ == "__main__":
    unittest.main()
