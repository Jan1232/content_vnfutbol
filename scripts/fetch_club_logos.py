#!/usr/bin/env python3
"""Скачать гербы клубов с Wikimedia Commons → editorial/templates/assets/logos/."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
from PIL import Image

from app.config import ROOT as APP_ROOT
from editorial.club_logos import logos_json_path, logo_dir, reload_catalog

_WIKI_UA = "VNFutbolEditorial/1.0 (club logos; contact: editorial@vnfutbol)"
_SIZE = 200


def _enwiki_thumb(title: str) -> str | None:
    params = {
        "action": "query",
        "format": "json",
        "titles": title,
        "prop": "pageimages",
        "pithumbsize": 512,
        "pilicense": "any",
    }
    try:
        with httpx.Client(timeout=40.0, headers={"User-Agent": _WIKI_UA}) as client:
            r = client.get("https://en.wikipedia.org/w/api.php", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        print(f"[enwiki] {title}: {e}", flush=True)
        return None
    pages = (data.get("query") or {}).get("pages") or {}
    for page in pages.values():
        thumb = (page.get("thumbnail") or {}).get("source")
        if thumb:
            return str(thumb)
    return None


def _search_commons(query: str) -> str | None:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query} logo",
        "gsrlimit": 5,
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": 512,
    }
    with httpx.Client(timeout=40.0, headers={"User-Agent": _WIKI_UA}) as client:
        r = client.get("https://commons.wikimedia.org/w/api.php", params=params)
        r.raise_for_status()
        data = r.json()
    pages = (data.get("query") or {}).get("pages") or {}
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        mime = str(info.get("mime") or "")
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/svg+xml"}:
            continue
        url = info.get("thumburl") or info.get("url")
        if url:
            return str(url)
    return None


def _normalize_png(data: bytes, dest: Path) -> None:
    from io import BytesIO

    img = Image.open(BytesIO(data)).convert("RGBA")
    img.thumbnail((_SIZE, _SIZE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (_SIZE, _SIZE), (0, 0, 0, 0))
    x = (_SIZE - img.width) // 2
    y = (_SIZE - img.height) // 2
    canvas.paste(img, (x, y), img)
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest, format="PNG", optimize=True)


def _placeholder_logo(slug: str, label: str, dest: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (_SIZE, _SIZE), (30, 30, 36, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse((20, 20, _SIZE - 20, _SIZE - 20), fill=(50, 50, 58, 255))
    text = (label[:3] or slug[:3]).upper()
    try:
        font = ImageFont.truetype(str(APP_ROOT / "editorial/templates/assets/fonts/Oswald-Bold.ttf"), 72)
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((_SIZE - tw) / 2, (_SIZE - th) / 2 - 6), text, fill="white", font=font)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")


def fetch_logo(slug: str, names: list[str], *, placeholders: bool = False) -> bool:
    primary = names[0] if names else slug.replace("_", " ")
    queries = [
        f"{primary} football club",
        primary.replace(" ", "_"),
        slug.replace("_", " "),
    ]
    url = None
    for q in queries:
        url = _enwiki_thumb(q)
        if url:
            break
    if not url:
        try:
            url = _search_commons(f"{primary} football club emblem")
        except Exception as e:
            print(f"[commons] {slug}: {e}", flush=True)
            url = None
    if not url:
        if placeholders:
            _placeholder_logo(slug, primary, logo_dir() / f"{slug}.png")
            print(f"[placeholder] {slug}", flush=True)
            return True
        print(f"[skip] {slug}: not found", flush=True)
        return False
    with httpx.Client(timeout=60.0, headers={"User-Agent": _WIKI_UA}, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.content
    dest = logo_dir() / f"{slug}.png"
    _normalize_png(data, dest)
    print(f"[ok] {slug} → {dest}", flush=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", action="append", help="только указанные slug")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--placeholders", action="store_true", help="заглушки если нет файла")
    args = parser.parse_args()

    path = logos_json_path()
    if not path.is_file():
        print(f"missing {path}", file=sys.stderr)
        return 1
    catalog = json.loads(path.read_text(encoding="utf-8")).get("clubs") or {}
    slugs = args.slug or list(catalog.keys())
    ok = 0
    for slug in slugs:
        row = catalog.get(slug)
        if not isinstance(row, dict):
            continue
        names = [str(x) for x in (row.get("names") or []) if str(x).strip()]
        if args.dry_run:
            print(f"would fetch {slug}: {names[:2]}", flush=True)
            continue
        if fetch_logo(slug, names, placeholders=args.placeholders):
            ok += 1
        time.sleep(0.35)
    reload_catalog()
    print(f"done: {ok}/{len(slugs)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
