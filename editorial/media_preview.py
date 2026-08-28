"""Preview bytes for multimodal gates (image URL or video first frame)."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.http_util import http_client
from editorial.imagery import preview_jpeg


def fetch_image_preview(url: str, *, max_side: int = 512) -> bytes | None:
    url = (url or "").strip()
    if not url:
        return None
    try:
        with http_client() as client:
            r = client.get(url, follow_redirects=True, timeout=30.0)
            r.raise_for_status()
            content = r.content
        if len(content) < 64:
            return None
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(content)
            path = Path(tmp.name)
        try:
            return preview_jpeg(path, max_side=max_side)
        finally:
            path.unlink(missing_ok=True)
    except Exception as e:
        print(f"[editorial] image preview fail: {e}", flush=True)
        return None


def fetch_video_frame_preview(url: str, *, max_side: int = 512) -> bytes | None:
    """Первый кадр видео через ffmpeg."""
    url = (url or "").strip()
    if not url:
        return None
    out = Path(tempfile.mktemp(suffix=".jpg"))
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                "0",
                "-i",
                url,
                "-frames:v",
                "1",
                "-q:v",
                "4",
                str(out),
            ],
            capture_output=True,
            timeout=45,
            check=False,
        )
        if proc.returncode != 0 or not out.is_file():
            print(f"[editorial] ffmpeg frame fail: {proc.stderr.decode()[:200]}", flush=True)
            return None
        return preview_jpeg(out, max_side=max_side)
    except Exception as e:
        print(f"[editorial] video preview fail: {e}", flush=True)
        return None
    finally:
        out.unlink(missing_ok=True)


def media_preview_from_post(
    media: list[dict[str, Any]],
    *,
    media_type: str = "",
    max_side: int = 512,
) -> bytes | None:
    """Картинка или первый кадр видео для vision-gate."""
    media_type = (media_type or "").strip().lower()
    for m in media or []:
        if not isinstance(m, dict):
            continue
        mtype = str(m.get("type") or "").lower()
        url = str(m.get("url") or "").strip()
        if not url:
            continue
        if media_type == "video" or mtype == "video":
            prev = fetch_video_frame_preview(url, max_side=max_side)
            if prev:
                return prev
        if media_type == "image" or mtype == "image":
            return fetch_image_preview(url, max_side=max_side)
    for m in media or []:
        if isinstance(m, dict) and m.get("url"):
            return fetch_image_preview(str(m["url"]), max_side=max_side)
    return None
