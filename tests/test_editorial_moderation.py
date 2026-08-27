from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from editorial.channel_config import EditorialChannelConfig
from editorial.content_blocks import add_content_block, content_profile, is_content_blocked
from editorial.moderation import can_dispatch_review, moderation_enabled
from editorial.publisher import publish


class ContentBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.blocks = patch(
            "editorial.content_blocks._blocks_path",
            return_value=__import__("pathlib").Path(self._tmp.name) / "blocks.json",
        )
        self.blocks.start()

    def tearDown(self) -> None:
        self.blocks.stop()
        self._tmp.cleanup()

    def test_block_by_source(self):
        item = {
            "id": 1,
            "source": "soccerblog_memes",
            "event_type": "lifestyle",
            "post_kind": "meme",
            "entities_json": '{"pick":{"tag":"human_factor"}}',
        }
        add_content_block(item, reason="feed_trash", news_id=1)
        blocked, reason = is_content_blocked(item)
        self.assertTrue(blocked)
        self.assertIn("фид", reason.lower())


class ModerationFlowTests(unittest.TestCase):
    def test_moderation_enabled_requires_token_and_admin(self):
        cfg = EditorialChannelConfig(slug="x", chat_id=-1, moderate_before_publish=True)
        with patch("editorial.moderation.get_settings") as gs:
            gs.return_value = MagicMock(
                editorial_tg_moderation=True,
                api_telegram_bot_token="tok",
                telegram_admin_id=123,
            )
            self.assertTrue(moderation_enabled(cfg))

    def test_meme_is_out_of_band(self):
        from editorial.moderation import is_out_of_band_item

        self.assertTrue(is_out_of_band_item({"meme_source": 1, "post_kind": "meme"}))
        self.assertTrue(is_out_of_band_item({"event_type": "fixture_result"}))
        self.assertFalse(is_out_of_band_item({"meme_source": 0, "event_type": "transfer"}))

    def test_try_dispatch_memes_sends_ready_memes(self):
        from editorial.moderation import try_dispatch_memes

        cfg = EditorialChannelConfig(slug="vnf_editorial", chat_id=-1, moderate_before_publish=True)
        meme = {
            "id": 99,
            "meme_source": 1,
            "post_kind": "meme",
            "event_type": "lifestyle",
            "cover_path": "/tmp/x.jpg",
            "media_path": "/tmp/x.jpg",
        }
        news = {
            "id": 100,
            "meme_source": 0,
            "post_kind": "news",
            "event_type": "transfer",
            "cover_path": "/tmp/y.jpg",
        }
        with (
            patch("editorial.moderation.moderation_enabled", return_value=True),
            patch("editorial.moderation._ready_pool", return_value=[meme, news]),
            patch("editorial.moderation.dispatch_review_immediate") as disp,
        ):
            disp.return_value = {"action": "dispatched_review_immediate", "news_id": 99}
            out = try_dispatch_memes(cfg)
        self.assertEqual(len(out), 1)
        disp.assert_called_once_with(cfg, 99)
        self.assertEqual(out[0].get("kind"), "meme")

    def test_publish_no_staged_status(self):
        client = MagicMock()
        client.upload_image_bytes.return_value = {"type": "image", "payload": {"token": "x"}}
        client.send_message.return_value = {"mid": "mid.live"}
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n" + b"0" * 1200)
            tmp.flush()
            cfg = EditorialChannelConfig(
                slug="vnf_editorial",
                chat_id=-75796650734896,
                dry_run=False,
                moderate_before_publish=True,
            )
            item = {"id": 3, "cover_path": tmp.name, "post_text": "Live", "cluster_id": ""}
            with (
                patch("editorial.publisher.story_gate", return_value=(True, "", "", 0)),
                patch("editorial.publisher.update_news") as upd,
            ):
                res = publish(client, cfg, item, force_live=True)
            self.assertEqual(res["action"], "published")
            for call in upd.call_args_list:
                self.assertNotEqual(call.kwargs.get("status"), "staged")


if __name__ == "__main__":
    unittest.main()
