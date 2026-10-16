"""Поиск картинки в Google Images: не первая попавшаяся, а достаточно качественная."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, unquote

from src.config import ROOT, _load_env
from src.ingest.yandex_images import download_image, fetch_yandex_image, search_first_image_url as yandex_first

log = logging.getLogger("opros.google_images")

MEDIA_DIR = ROOT / "data" / "media" / "opros"

# Google: больше VGA; клиентский порог чуть мягче (на случай кривых метаданных)
GOOGLE_TBS = "isz:lt,islt:svga"  # larger than 800×600
MIN_WIDTH = 500
MIN_HEIGHT = 400
MIN_SHORT_SIDE = 400
MIN_PIXELS = 500 * 400
MAX_ASPECT = 3.2  # отсекаем баннеры/полоски
MIN_BYTES = 18_000
MAX_TRY = 8

_BLOCK_HOST_PARTS = (
    "gstatic.com/images/branding",
    "google.com/images/branding",
    "googleusercontent.com/proxy",
    "encrypted-tbn",
    "favicon",
    "captcha",
    "/logo",
    "sprite",
    "/icons/",
    "/icon_",
    "1x1",
    "pixel.gif",
)

_LOWRES_PATH_RE = re.compile(
    r"(?i)(?:^|/)(?:thumb|thumbs|thumbnail|small|tiny|preview|miniatur)(?:s)?(?:/|$)|"
    r"\b(?:16|24|32|48|64|96|100|120|128|150)x(?:16|24|32|48|64|96|100|120|128|150)\b"
)


@dataclass(frozen=True)
class ImageCandidate:
    url: str
    width: int = 0
    height: int = 0

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)


def _proxy() -> str | None:
    import os

    _load_env()
    return (
        os.environ.get("SCRAPER_HTTP_PROXY", "").strip()
        or os.environ.get("OPENAI_HTTP_PROXY", "").strip()
        or None
    )


def _clean(raw: str) -> str:
    u = raw.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
    u = unquote(u)
    return u.strip()


def _is_blocked(url: str) -> bool:
    low = (url or "").lower()
    if not low.startswith("http"):
        return True
    if any(b in low for b in _BLOCK_HOST_PARTS):
        return True
    if _LOWRES_PATH_RE.search(low):
        return True
    return False


def _aspect_ok(width: int, height: int) -> bool:
    if width <= 0 or height <= 0:
        return True  # неизвестно — не режем по соотношению
    ratio = max(width, height) / max(1, min(width, height))
    return ratio <= MAX_ASPECT


def _meta_quality_ok(width: int, height: int) -> bool:
    """Если Google отдал размеры — требуем порог. 0×0 = неизвестно, пропускаем дальше."""
    if width <= 0 or height <= 0:
        return True
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return False
    if min(width, height) < MIN_SHORT_SIDE:
        return False
    if width * height < MIN_PIXELS:
        return False
    if not _aspect_ok(width, height):
        return False
    return True


def _file_quality_ok(path: Path) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
    except OSError:
        return False, "missing"
    if size < MIN_BYTES:
        return False, f"too_small_bytes:{size}"
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
            im.verify()
    except Exception as exc:
        # fallback: сырые байты JPEG/PNG и размер файла
        try:
            data = path.read_bytes()[:32]
        except OSError:
            return False, f"unreadable:{type(exc).__name__}"
        if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n":
            if size >= MIN_BYTES * 2:
                return True, "ok_magic"
        return False, f"decode_fail:{type(exc).__name__}"
    if w < MIN_WIDTH or h < MIN_HEIGHT or min(w, h) < MIN_SHORT_SIDE:
        return False, f"too_small_px:{w}x{h}"
    if w * h < MIN_PIXELS:
        return False, f"too_few_px:{w}x{h}"
    if not _aspect_ok(w, h):
        return False, f"bad_aspect:{w}x{h}"
    return True, f"ok:{w}x{h}"


def _bytes_quality_ok(body: bytes) -> tuple[bool, str, int, int]:
    if len(body) < MIN_BYTES:
        return False, f"too_small_bytes:{len(body)}", 0, 0
    try:
        from PIL import Image

        with Image.open(BytesIO(body)) as im:
            w, h = im.size
    except Exception as exc:
        if body[:2] == b"\xff\xd8" or body[:8] == b"\x89PNG\r\n\x1a\n":
            if len(body) >= MIN_BYTES * 2:
                return True, "ok_magic", 0, 0
        return False, f"decode_fail:{type(exc).__name__}", 0, 0
    if w < MIN_WIDTH or h < MIN_HEIGHT or min(w, h) < MIN_SHORT_SIDE:
        return False, f"too_small_px:{w}x{h}", w, h
    if w * h < MIN_PIXELS:
        return False, f"too_few_px:{w}x{h}", w, h
    if not _aspect_ok(w, h):
        return False, f"bad_aspect:{w}x{h}", w, h
    return True, f"ok:{w}x{h}", w, h


def _dedupe(cands: list[ImageCandidate]) -> list[ImageCandidate]:
    seen: set[str] = set()
    out: list[ImageCandidate] = []
    for c in cands:
        key = c.url.split("?")[0].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _rank(cands: list[ImageCandidate]) -> list[ImageCandidate]:
    """Известные большие кадры выше; без размеров — ниже, но не выкидываем."""

    def key(c: ImageCandidate) -> tuple:
        known = 1 if c.area > 0 else 0
        return (known, c.area, min(c.width, c.height))

    return sorted(cands, key=key, reverse=True)


def search_google_image_candidates(
    query: str,
    *,
    timeout: float = 25.0,
) -> tuple[list[ImageCandidate], str]:
    q = (query or "").strip()
    if not q:
        return [], "empty_query"

    url = (
        f"https://www.google.com/search?q={quote_plus(q)}"
        f"&tbm=isch&hl=ru&safe=off&tbs={GOOGLE_TBS}"
    )
    try:
        import httpx

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
        }
        with httpx.Client(proxy=_proxy(), timeout=timeout, follow_redirects=True) as client:
            r = client.get(url, headers=headers)
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        log.warning("google search fail q=%r: %s", q, type(exc).__name__)
        return [], f"network_error:{type(exc).__name__}"

    low = html.lower()
    if "captcha" in low or "/sorry/" in low:
        return [], "captcha"

    raw: list[ImageCandidate] = []

    # Основной сигнал качества: ["https://…", width, height]
    for m in re.finditer(r'\["(https://[^"]+)",(\d{2,5}),(\d{2,5})\]', html):
        raw.append(
            ImageCandidate(_clean(m.group(1)), int(m.group(2)), int(m.group(3)))
        )

    for m in re.finditer(r'"ou"\s*:\s*"(https?://[^"]+)"', html):
        raw.append(ImageCandidate(_clean(m.group(1))))

    for m in re.finditer(r'"ow"\s*:\s*(\d+).{0,80}"oh"\s*:\s*(\d+).{0,80}"ou"\s*:\s*"(https?://[^"]+)"', html, re.S):
        raw.append(ImageCandidate(_clean(m.group(3)), int(m.group(1)), int(m.group(2))))

    for m in re.finditer(r'"(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', html, re.I):
        raw.append(ImageCandidate(_clean(m.group(1))))

    for m in re.finditer(r"(?:imgurl|imgrefurl)=([^&\s\"']+)", html):
        try:
            raw.append(ImageCandidate(_clean(unquote(m.group(1)))))
        except Exception:
            pass

    filtered: list[ImageCandidate] = []
    skipped_low = 0
    for c in _dedupe(raw):
        if _is_blocked(c.url):
            continue
        if not _meta_quality_ok(c.width, c.height):
            skipped_low += 1
            log.debug("google skip low-res %sx%s %s", c.width, c.height, c.url[:90])
            continue
        filtered.append(c)

    ranked = _rank(filtered)
    if not ranked:
        return [], f"empty_or_blocked:{len(raw)}:lowres={skipped_low}"

    log.info(
        "google candidates q=%r n=%s skipped_low=%s best=%sx%s",
        q,
        len(ranked),
        skipped_low,
        ranked[0].width,
        ranked[0].height,
    )
    return ranked, "ok"


def search_google_first_image_url(query: str, *, timeout: float = 25.0) -> tuple[str | None, str]:
    """Лучший URL (по метаданным Google), не обязательно первый в выдаче."""
    cands, reason = search_google_image_candidates(query, timeout=timeout)
    if not cands:
        return None, reason
    return cands[0].url, reason


def _try_download(url: str, stem: str) -> tuple[Path | None, str]:
    path, dreason = download_image(url, stem)
    if not path:
        return None, dreason
    ok, qreason = _file_quality_ok(path)
    if not ok:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None, qreason
    return path, qreason


def fetch_first_image(query: str, dest_stem: str | None = None) -> tuple[str | None, Path | None, str]:
    """
    Ищет картинку: Google (крупные кадры) → fallback Яндекс.
    Низкое разрешение отбрасывается даже если оно первое в выдаче.
    """
    q = (query or "").strip()
    if not q:
        return None, None, "empty_query"

    stem = dest_stem or ("opros_" + hashlib.sha1(q.encode("utf-8")).hexdigest()[:16])
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    cands, greason = search_google_image_candidates(q)
    last_fail = greason
    for i, cand in enumerate(cands[:MAX_TRY]):
        path, dreason = _try_download(cand.url, f"{stem}_g{i}")
        if path:
            log.info(
                "google pick q=%r i=%s meta=%sx%s file=%s url=%s",
                q,
                i,
                cand.width,
                cand.height,
                dreason,
                cand.url[:120],
            )
            return cand.url, path, f"ok:google:{dreason}"
        last_fail = dreason
        log.info("google reject q=%r i=%s reason=%s url=%s", q, i, dreason, cand.url[:100])

    log.info("google miss q=%r reason=%s → yandex", q, last_fail)
    y_url, y_path, y_reason = fetch_yandex_image(q, f"{stem}_y")
    if y_path:
        ok, qreason = _file_quality_ok(y_path)
        if ok:
            return y_url, y_path, f"ok:yandex:{qreason}"
        try:
            y_path.unlink(missing_ok=True)
        except OSError:
            pass
        return y_url, None, f"yandex_low_quality:{qreason}"
    return None, None, f"google+yandex:{last_fail}/{y_reason}"


def main() -> None:
    """CLI: python -m opros.google_images "Реал Мадрид"."""
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Качественная картинка Google Images")
    p.add_argument("query", help="поисковый запрос")
    p.add_argument("--url-only", action="store_true", help="только URL, без скачивания")
    args = p.parse_args()

    if args.url_only:
        cands, reason = search_google_image_candidates(args.query)
        if not cands:
            yurl, yreason = yandex_first(args.query)
            print(
                json.dumps(
                    {"ok": False, "google": reason, "yandex_url": yurl, "yandex": yreason},
                    ensure_ascii=False,
                )
            )
            sys.exit(1)
        best = cands[0]
        print(
            json.dumps(
                {
                    "ok": True,
                    "url": best.url,
                    "width": best.width,
                    "height": best.height,
                    "candidates": len(cands),
                    "reason": reason,
                },
                ensure_ascii=False,
            )
        )
        return

    url, path, reason = fetch_first_image(args.query)
    print(
        json.dumps(
            {
                "ok": bool(url and path),
                "url": url,
                "path": str(path) if path else None,
                "reason": reason,
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0 if url and path else 1)


if __name__ == "__main__":
    main()
