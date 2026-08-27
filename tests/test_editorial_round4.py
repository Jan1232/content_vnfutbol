"""Tests for round-4 FIX 1–5 editorial changes."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from editorial.channel_config import EditorialChannelConfig, EditorialFeed, ModerationConfig
from editorial.models import NewsItem
from editorial.moderation import can_dispatch_review
from editorial.moderation_session import (
    clear_input_step,
    get_awaiting_input_session,
    get_session_by_prompt_message,
    upsert_session,
)


def setUpModule() -> None:
    from app.db import init_db

    init_db()


class SessionInputRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.db import db

        with db() as conn:
            conn.execute("DELETE FROM editorial_moderation_session WHERE news_id IN (501, 502, 503)")

    def test_clear_input_keeps_except_news(self):
        upsert_session(501, admin_id="42", step="photo_query", prompt_message_id=11)
        upsert_session(502, admin_id="42", step="edit_text", prompt_message_id=12)
        clear_input_step("42", except_news_id=501)
        a = get_awaiting_input_session("42")
        self.assertIsNotNone(a)
        self.assertEqual(int(a["news_id"]), 501)
        self.assertEqual(a["step"], "photo_query")

    def test_reply_resolves_first_card_not_latest(self):
        upsert_session(501, admin_id="42", step="photo_query", tg_message_id=100, prompt_message_id=201)
        upsert_session(502, admin_id="42", step="review", tg_message_id=101, prompt_message_id=0)
        # latest session is 502, but reply to prompt of 501
        hit = get_session_by_prompt_message("42", 201)
        self.assertIsNotNone(hit)
        self.assertEqual(int(hit["news_id"]), 501)

    def test_handle_message_uses_reply_news_id(self):
        from editorial.tg_moderator import bot as bot_mod

        upsert_session(501, admin_id="99", step="photo_query", prompt_message_id=777)
        upsert_session(502, admin_id="99", step="review", prompt_message_id=0)
        update = {
            "message": {
                "from": {"id": 99},
                "chat": {"id": 99},
                "text": "Messi Barcelona",
                "reply_to_message": {"message_id": 777},
            }
        }
        with (
            patch.object(bot_mod, "_admin_id", return_value=99),
            patch.object(bot_mod, "_is_admin", return_value=True),
            patch.object(bot_mod, "_build_photo_pool") as build,
            patch.object(bot_mod.api, "send_message"),
        ):
            bot_mod.handle_message(update)
        build.assert_called_once()
        self.assertEqual(build.call_args[0][0], 501)
        self.assertEqual(build.call_args[0][1], "Messi Barcelona")


class MemeMediaSelectTests(unittest.TestCase):
    def test_transfer_text_video_skipped(self):
        from editorial.sources import parse_telegram_meme_feed

        feed = EditorialFeed(
            name="soccerblog_memes",
            kind="telegram",
            handle="thesoccerblogteam",
            take_only=("video", "meme_image"),
        )
        post = SimpleNamespace(
            text="ХИРВИГОУ: Батраков едет в аренду с опцией выкупа за 35 миллионов евро",
            title="",
            external_id="tg1",
            source_url="https://t.me/x/1",
            media=[{"type": "video", "url": "https://example.com/v.mp4"}],
        )
        with (
            patch("app.config.get_settings", return_value=MagicMock(meme_source_enabled=True)),
            patch("parsers.telegram.parse_telegram", return_value=("ch", [post])),
            patch("editorial.sources._extract_entities", return_value={}),
        ):
            items = parse_telegram_meme_feed(feed)
        self.assertEqual(items, [])

    def test_lifestyle_meme_kept(self):
        from editorial.sources import parse_telegram_meme_feed

        feed = EditorialFeed(
            name="soccerblog_memes",
            kind="telegram",
            handle="thesoccerblogteam",
            take_only=("video", "meme_image"),
        )
        post = SimpleNamespace(
            text="Тем временем Холланд окончательно поплыл и сбрил шевелюру",
            title="",
            external_id="tg2",
            source_url="https://t.me/x/2",
            media=[{"type": "image", "url": "https://example.com/i.jpg"}],
        )
        with (
            patch("app.config.get_settings", return_value=MagicMock(meme_source_enabled=True)),
            patch("parsers.telegram.parse_telegram", return_value=("ch", [post])),
            patch("editorial.sources._extract_entities", return_value={}),
        ):
            items = parse_telegram_meme_feed(feed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].event_type, "lifestyle")
        self.assertEqual(items[0].raw.get("post_kind"), "meme")


class QueueDepthTests(unittest.TestCase):
    def test_queue_depth_allows_three(self):
        cfg = EditorialChannelConfig(
            slug="test_qd",
            chat_id=-1,
            moderation=ModerationConfig(queue_depth=3),
        )
        ready = [
            {
                "id": i,
                "event_type": "transfer",
                "meme_source": 0,
                "post_kind": "news",
                "cover_path": "/tmp/x.jpg",
            }
            for i in (1, 2, 3, 4)
        ]
        with (
            patch("editorial.moderation.count_awaiting_review", return_value=2),
            patch("editorial.moderation._ready_pool", return_value=ready),
            patch("editorial.moderation.is_priority", return_value=True),
        ):
            self.assertTrue(can_dispatch_review(cfg, force=True))
        with (
            patch("editorial.moderation.count_awaiting_review", return_value=3),
            patch("editorial.moderation._ready_pool", return_value=ready),
        ):
            self.assertFalse(can_dispatch_review(cfg, force=True))


class StoryKeyBatrakovTests(unittest.TestCase):
    def test_five_titles_same_key(self):
        from editorial.story_throttle import story_key

        titles = [
            "Батраков летит в Стамбул",
            "Батраков проходит медосмотр",
            "Батраков близок к Галатасараю",
            "Интерес к Батракову растёт",
            "Батраков: переговоры продолжаются",
        ]
        keys = set()
        for t in titles:
            item = NewsItem(
                external_id=f"e:{t}",
                source="t",
                url="https://x",
                title=t,
                body=t + " трансфер",
                lang="ru",
                published_at=datetime.now(timezone.utc),
                event_type="transfer",
                entities={"players": ["Батраков"], "teams": ["Локомотив"]},
            )
            keys.add(story_key(item))
        self.assertEqual(len(keys), 1)
        self.assertTrue(list(keys)[0].endswith("|transfer"))


class EntertainmentMemeTests(unittest.TestCase):
    def test_meme_event_is_entertainment(self):
        from editorial.cycle import _is_entertainment

        self.assertTrue(_is_entertainment({"event_type": "meme", "post_kind": "news", "meme_source": 0}))
        self.assertTrue(_is_entertainment({"event_type": "lifestyle", "meme_source": 0, "post_kind": "news"}))


if __name__ == "__main__":
    unittest.main()
