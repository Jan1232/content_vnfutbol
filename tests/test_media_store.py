from __future__ import annotations

import json
from pathlib import Path

from app.media_store import (
    drop_oversized_videos,
    is_oversized_video,
    local_media_path,
    merge_local_paths,
    remember,
    with_local_path,
)
from app.tg_mtproto import media_needs_tg_download


def test_oversized_video_is_dropped() -> None:
    media = [
        {"type": "video", "url": "", "tg_ref": "ASupersharij/60904", "too_big": True},
        {"type": "image", "url": "https://cdn.example/a.jpg", "tg_ref": "ASupersharij/1"},
    ]
    assert is_oversized_video(media[0])
    assert not is_oversized_video(media[1])
    kept = drop_oversized_videos(media)
    assert [m["type"] for m in kept] == ["image"]


def test_small_video_not_oversized() -> None:
    item = {
        "type": "video",
        "url": "https://cdn.telesco.pe/file/x.mp4",
        "tg_ref": "ASupersharij/60922",
        "too_big": False,
    }
    assert not is_oversized_video(item)
    assert drop_oversized_videos([item]) == [item]


def test_too_big_video_does_not_need_tg_retry_loop() -> None:
    assert not media_needs_tg_download(
        [{"type": "video", "tg_ref": "ASupersharij/60904", "too_big": True, "url": ""}]
    )
    assert media_needs_tg_download(
        [{"type": "image", "tg_ref": "chan/1", "too_big": True, "url": ""}]
    )


def test_shared_cache_and_sibling_merge(tmp_path: Path) -> None:
    from app import media_store

    media_store.cache_dir = lambda: tmp_path  # type: ignore[method-assign]
    src = tmp_path / "orig.mp4"
    src.write_bytes(b"video-bytes-here")
    item = {
        "type": "video",
        "url": "https://cdn.telesco.pe/file/same.mp4?token=abc",
        "tg_ref": "ASupersharij/60922",
        "too_big": False,
    }
    stored = remember(item, src)
    local = with_local_path(item, stored)
    assert local_media_path(local) is not None

    sibling = {
        "type": "video",
        "url": "https://cdn.telesco.pe/file/same.mp4?token=abc",
        "tg_ref": "ASupersharij/60922",
        "too_big": False,
    }
    merged = merge_local_paths([sibling], [local])
    assert local_media_path(merged[0]) is not None
    assert merged[0]["url"].startswith("file://")
    assert json.dumps(merged)


if __name__ == "__main__":
    import tempfile

    test_oversized_video_is_dropped()
    test_small_video_not_oversized()
    test_too_big_video_does_not_need_tg_retry_loop()
    with tempfile.TemporaryDirectory() as d:
        test_shared_cache_and_sibling_merge(Path(d))
    print("ok")
