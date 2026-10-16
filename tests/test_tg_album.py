"""Альбомы Telegram: все /id?single, не только файлы с прямым URL."""

from __future__ import annotations

from bs4 import BeautifulSoup

from parsers.telegram import collect_message_media, parse_telegram
from app.tg_mtproto import telegram_media_needs_album_expand


GROUPED_HTML = """
<div class="tgme_widget_message" data-post="ASupersharij/59940">
  <div class="tgme_widget_message_grouped_layer">
    <a class="tgme_widget_message_video_player grouped_media_wrap blured not_supported"
       href="https://t.me/ASupersharij/59940?single">
      <div class="message_media_not_supported_label">Media is too big</div>
    </a>
    <a class="tgme_widget_message_video_player grouped_media_wrap blured not_supported"
       href="https://t.me/ASupersharij/59941?single">
      <div class="message_media_not_supported_label">Media is too big</div>
    </a>
    <a class="tgme_widget_message_photo_wrap grouped_media_wrap js-message_photo"
       href="https://t.me/ASupersharij/59948?single"
       style="background-image:url('https://cdn.example/a.jpg')"></a>
    <a class="tgme_widget_message_photo_wrap grouped_media_wrap js-message_photo"
       href="https://t.me/ASupersharij/59949?single"
       style="background-image:url('https://cdn.example/b.jpg')"></a>
  </div>
</div>
"""


def test_grouped_album_keeps_every_item() -> None:
    soup = BeautifulSoup(GROUPED_HTML, "lxml")
    msg = soup.select_one(".tgme_widget_message")
    media = collect_message_media(msg, "ASupersharij/59940")
    refs = [m.get("tg_ref") for m in media]
    types = [m.get("type") for m in media]
    assert refs == [
        "ASupersharij/59940",
        "ASupersharij/59941",
        "ASupersharij/59948",
        "ASupersharij/59949",
    ], refs
    assert types == ["video", "video", "image", "image"], types
    assert media[0]["too_big"] is True
    assert media[2]["url"].endswith("a.jpg")


def test_live_shariy_album_not_truncated() -> None:
    _title, posts = parse_telegram("https://t.me/s/ASupersharij")
    hit = [p for p in posts if p.external_id == "tg:ASupersharij/59940"]
    if not hit:
        return
    n = sum(1 for m in hit[0].media if (m.get("type") or "") in {"image", "video"})
    assert n >= 8, n


def test_album_expand_heuristic_is_global() -> None:
    assert not telegram_media_needs_album_expand("tg:foo/1", [])
    assert telegram_media_needs_album_expand(
        "tg:foo/10",
        [{"type": "video", "tg_ref": "foo/10", "too_big": True}],
    )
    assert not telegram_media_needs_album_expand(
        "tg:foo/10",
        [{"type": "video", "tg_ref": f"foo/{i}"} for i in range(10, 20)],
    )
    assert not telegram_media_needs_album_expand("vk:1_2", [{"type": "image"}])


if __name__ == "__main__":
    test_grouped_album_keeps_every_item()
    print("OK grouped fixture")
    test_album_expand_heuristic_is_global()
    print("OK heuristic")
    test_live_shariy_album_not_truncated()
    print("OK live album")
