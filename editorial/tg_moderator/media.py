"""Prepare local images for Telegram preview uploads."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import ROOT

_PREVIEW_DIR = ROOT / "data" / "editorial" / "tg_previews"
_MAX_SIDE = 1280


def prepare_tg_preview(path: Path | str) -> Path:
    """JPEG-превью для sendPhoto (конвертация/ресайз проблемных форматов)."""
    from PIL import Image

    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    _PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(src.read_bytes()).hexdigest()[:12]
    dest = _PREVIEW_DIR / f"{src.stem}_{digest}.jpg"
    if dest.is_file() and dest.stat().st_size > 1000:
        return dest

    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        side = max(w, h)
        if side > _MAX_SIDE:
            scale = _MAX_SIDE / side
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        im.save(dest, format="JPEG", quality=88, optimize=True)
    return dest
