# -*- coding: utf-8 -*-
"""Слепой поиск Яндекс.Картинок: первая выдача, fallback None."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote_plus, unquote, urlparse

from src.config import ROOT, _load_env

log = logging.getLogger("ingest.yandex_images")

MEDIA_DIR = ROOT / "data" / "media"

# UI/CDN мусор — не картинки выдачи
_BLOCK_HOST_PARTS = (
    "yastatic.net",
    "yandex.ru/clck",
    "yandex.net/clck",
    "captcha",
    "favicon",
    "fiji-static",
    "avatars.mds.yandex.net/get-yapic",
    "mc.yandex",
)


def _proxy() -> str | None:
    _load_env()
    return (
        os.environ.get("SCRAPER_HTTP_PROXY", "").strip()
        or os.environ.get("OPENAI_HTTP_PROXY", "").strip()
        or None
    )


def _is_blocked_url(url: str) -> bool:
    low = url.lower()
    return any(b in low for b in _BLOCK_HOST_PARTS)


def _clean_url(raw: str) -> str:
    u = raw.encode("utf-8").decode("unicode_escape")
    u = u.replace("\\/", "/").replace("\\u0026", "&")
    u = unquote(u)
    return u.strip()


def _pick_url(candidates: list[str]) -> str | None:
    for u in candidates:
        if not u.startswith("http"):
            continue
        if _is_blocked_url(u):
            continue
        # предпочитаем прямые картинки / imghost
        return u
    return None


def search_first_image_url(query: str, *, timeout: float = 25.0) -> tuple[str | None, str]:
    """Возвращает (url|None, reason). reason для логов/диагностики."""
    q = (query or "").strip()
    if not q:
        return None, "empty_query"
    url = f"https://yandex.ru/images/search?text={quote_plus(q)}"
    try:
        import httpx

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        }
        with httpx.Client(proxy=_proxy(), timeout=timeout, follow_redirects=True) as client:
            r = client.get(url, headers=headers)
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        log.exception("yandex search failed q=%r", q)
        return None, f"network_error:{type(exc).__name__}"

    low = html.lower()
    if "captcha" in low and ("smartcaptcha" in low or "showcaptcha" in low):
        log.warning("yandex captcha q=%r", q)
        return None, "captcha"

    candidates: list[str] = []

    # JSON-блоки в странице (serp-item / initialState)
    for m in re.finditer(
        r'"origUrl"\s*:\s*"(https:[^"]+)"|'
        r'"origin"\s*:\s*\{\s*"url"\s*:\s*"(https:[^"]+)"|'
        r'"url"\s*:\s*"(https:\\?/\\?/[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
        html,
        re.I,
    ):
        g = next(x for x in m.groups() if x)
        candidates.append(_clean_url(g))

    # img_url= в ссылках
    for m in re.finditer(r"(?:img_url|url)=([^&\s\"']+)", html):
        try:
            candidates.append(_clean_url(unquote(m.group(1))))
        except Exception:
            pass

    # data-bem serp-item
    for m in re.finditer(r"data-bem='(\{[^']+\})'", html):
        try:
            data = json.loads(m.group(1).replace("&quot;", '"'))
            item = data.get("serp-item") or {}
            for key in ("thumb", "preview", "img_href", "detail_url"):
                val = item.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    candidates.append(_clean_url(val))
                elif isinstance(val, dict) and val.get("url"):
                    candidates.append(_clean_url(val["url"]))
            for d in item.get("preview") or []:
                if isinstance(d, dict) and d.get("url"):
                    candidates.append(_clean_url(d["url"]))
        except Exception:
            continue

    picked = _pick_url(candidates)
    if picked:
        log.info("yandex ok q=%r url=%s (from %s candidates)", q, picked[:100], len(candidates))
        return picked, "ok"

    log.warning(
        "yandex empty/blocked q=%r html_len=%s candidates=%s",
        q,
        len(html),
        len(candidates),
    )
    return None, "empty_or_blocked"


def download_image(url: str, dest_stem: str, *, timeout: float = 30.0) -> tuple[Path | None, str]:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if _is_blocked_url(url):
        return None, "blocked_url"
    try:
        import httpx

        with httpx.Client(proxy=_proxy(), timeout=timeout, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").lower()
            body = r.content
            if "text/html" in ctype or body[:200].lstrip().startswith(b"<"):
                return None, "html_instead_of_image"
            if len(body) < 1500:
                return None, f"too_small:{len(body)}"
            ext = ".jpg"
            if "png" in ctype or url.lower().endswith(".png"):
                ext = ".png"
            elif "webp" in ctype:
                ext = ".webp"
            # magic bytes
            if body[:8] == b"\x89PNG\r\n\x1a\n":
                ext = ".png"
            elif body[:2] == b"\xff\xd8":
                ext = ".jpg"
            path = MEDIA_DIR / f"{dest_stem}{ext}"
            path.write_bytes(body)
            return path, "ok"
    except Exception as exc:
        log.exception("download image failed url=%s", url[:120])
        return None, f"download_error:{type(exc).__name__}"


def fetch_yandex_image(query: str, dest_stem: str) -> tuple[str | None, Path | None, str]:
    """Возвращает (url, local_path, reason)."""
    url, reason = search_first_image_url(query)
    if not url:
        return None, None, reason
    path, dreason = download_image(url, dest_stem)
    if not path:
        return url, None, dreason
    return url, path, "ok"
