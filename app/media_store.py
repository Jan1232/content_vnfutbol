"""Общий кэш медиа: одна скачка на все каналы с одним источником."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from app.config import get_settings

_REF_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def is_oversized_video(item: dict[str, Any] | None) -> bool:
    if not isinstance(item, dict):
        return False
    if (item.get("type") or "").lower() != "video":
        return False
    return bool(item.get("too_big"))


def cache_dir() -> Path:
    d = get_settings().data_dir / "media_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ref_stem(tg_ref: str) -> str:
    return _REF_SAFE.sub("_", (tg_ref or "").strip())[:120]


def _url_stem(url: str) -> str:
    h = hashlib.sha256((url or "").encode("utf-8", "ignore")).hexdigest()[:24]
    return f"url_{h}"


def local_media_path(item: dict[str, Any] | None) -> Path | None:
    if not isinstance(item, dict):
        return None
    candidates: list[str] = []
    lp = (item.get("local_path") or "").strip()
    if lp:
        candidates.append(lp)
    raw = (item.get("url") or "").strip()
    if raw.startswith("file://"):
        candidates.append(unquote(urlparse(raw).path))
    elif raw.startswith("/") and not raw.startswith("//"):
        candidates.append(raw)
    for cand in candidates:
        p = Path(cand)
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def lookup_cached(item: dict[str, Any]) -> Path | None:
    found = local_media_path(item)
    if found:
        return found
    d = cache_dir()
    tg_ref = (item.get("tg_ref") or "").strip()
    if tg_ref:
        stem = _ref_stem(tg_ref)
        for p in d.glob(f"ref_{stem}.*"):
            if p.is_file() and p.stat().st_size > 0:
                return p
        tg_cache = get_settings().data_dir / "tg_cache"
        if tg_cache.is_dir():
            uname, _, mid = tg_ref.partition("/")
            if uname and mid:
                for p in tg_cache.glob(f"{uname}_{mid}.*"):
                    if p.is_file() and p.stat().st_size > 0:
                        return p
    url = (item.get("url") or "").strip()
    if url and not url.startswith("file://") and not url.startswith("/"):
        stem = _url_stem(url)
        for p in d.glob(f"{stem}.*"):
            if p.is_file() and p.stat().st_size > 0:
                return p
    return None


def _suffix_for(item: dict[str, Any], source: Path | None = None, ctype: str = "") -> str:
    if source and source.suffix:
        return source.suffix[:8]
    fname = (item.get("filename") or "").strip()
    if fname and Path(fname).suffix:
        return Path(fname).suffix[:8]
    if (item.get("type") or "").lower() == "video" or "video" in (ctype or ""):
        return ".mp4"
    if "png" in (ctype or ""):
        return ".png"
    if "webp" in (ctype or ""):
        return ".webp"
    return ".jpg"


def remember(item: dict[str, Any], path: Path) -> Path:
    path = path.resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        return path
    d = cache_dir()
    suf = _suffix_for(item, path)
    targets: list[Path] = []
    tg_ref = (item.get("tg_ref") or "").strip()
    if tg_ref:
        targets.append(d / f"ref_{_ref_stem(tg_ref)}{suf}")
    url = (item.get("url") or "").strip()
    if url and not url.startswith("file://") and not url.startswith("/"):
        targets.append(d / f"{_url_stem(url)}{suf}")
    last = path
    for dest in targets:
        if dest.resolve() == path:
            last = dest
            continue
        if dest.exists() and dest.stat().st_size > 0:
            last = dest
            continue
        try:
            dest.unlink(missing_ok=True)
            dest.hardlink_to(path)
        except OSError:
            shutil.copy2(path, dest)
        last = dest
    return last


def with_local_path(item: dict[str, Any], path: Path) -> dict[str, Any]:
    out = dict(item)
    resolved = path.resolve()
    out["local_path"] = str(resolved)
    out["url"] = resolved.as_uri()
    out["too_big"] = False
    return out


def _download_url(url: str, item: dict[str, Any], *, attempts: int = 3) -> Path | None:
    from app.http_util import http_client

    last_err = ""
    for i in range(attempts):
        try:
            with http_client(timeout=180.0) as c:
                r = c.get(url)
                if r.status_code >= 500:
                    last_err = f"HTTP {r.status_code}"
                    time.sleep(0.6 * (i + 1))
                    continue
                r.raise_for_status()
                content = r.content
                ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not content or len(content) < 64:
                last_err = "empty body"
                time.sleep(0.4)
                continue
            if "text/html" in ctype:
                last_err = "html instead of media"
                time.sleep(0.4)
                continue
            suf = _suffix_for(item, ctype=ctype)
            dest = cache_dir() / f"{_url_stem(url)}{suf}"
            dest.write_bytes(content)
            return dest
        except Exception as e:
            last_err = str(e)
            time.sleep(0.6 * (i + 1))
    print(f"[media] download fail {url[:80]}: {last_err}", flush=True)
    return None


def materialize_item(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return item
    if (item.get("type") or "").lower() not in {"image", "video"}:
        return item

    found = lookup_cached(item)
    if found:
        stored = remember(item, found)
        return with_local_path(item, stored)

    url = (item.get("url") or "").strip()
    if not is_oversized_video(item) and (
        url.startswith("http://") or url.startswith("https://")
    ):
        path = _download_url(url, item)
        if path:
            stored = remember(item, path)
            return with_local_path(item, stored)

    tg_ref = (item.get("tg_ref") or "").strip()
    if tg_ref:
        from app.tg_mtproto import download_tg_media

        path = download_tg_media(tg_ref)
        if path:
            stored = remember(item, path)
            return with_local_path(item, stored)
    return item


def materialize_media_list(media: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in media or []:
        if isinstance(item, dict):
            out.append(materialize_item(item))
        else:
            out.append(item)
    return out


def drop_oversized_videos(media: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        m
        for m in (media or [])
        if not (isinstance(m, dict) and is_oversized_video(m) and local_media_path(m) is None)
    ]


def merge_local_paths(
    dst_media: list[dict[str, Any]] | None,
    src_media: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    by_ref: dict[str, dict[str, Any]] = {}
    by_url: dict[str, dict[str, Any]] = {}
    for m in src_media or []:
        if not isinstance(m, dict) or local_media_path(m) is None:
            continue
        ref = (m.get("tg_ref") or "").strip()
        if ref:
            by_ref[ref] = m
        raw = (m.get("url") or "").strip()
        if raw:
            by_url[raw] = m
        lp = (m.get("local_path") or "").strip()
        if lp:
            by_url[lp] = m
    out: list[dict[str, Any]] = []
    for m in dst_media or []:
        if not isinstance(m, dict) or local_media_path(m) is not None:
            out.append(m)
            continue
        hit = by_ref.get((m.get("tg_ref") or "").strip()) or by_url.get((m.get("url") or "").strip())
        if hit:
            path = local_media_path(hit)
            out.append(with_local_path(m, path) if path else m)
        else:
            out.append(m)
    return out


def persist_media_to_siblings(
    conn: Any,
    *,
    external_id: str,
    media: list[dict[str, Any]],
    except_id: int,
) -> int:
    ext = (external_id or "").strip()
    if not ext:
        return 0
    blob = json.dumps(media, ensure_ascii=False)
    rows = conn.execute(
        """
        SELECT id, media_json FROM posts
        WHERE external_id=? AND id!=?
          AND publish_status IN ('pending', 'queued')
        """,
        (ext, int(except_id)),
    ).fetchall()
    n = 0
    for row in rows:
        try:
            other = json.loads(row["media_json"] or "[]")
        except Exception:
            other = []
        merged = merge_local_paths(other, media)
        if merged == other:
            continue
        conn.execute(
            "UPDATE posts SET media_json=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), row["id"]),
        )
        n += 1
    _ = blob
    return n
