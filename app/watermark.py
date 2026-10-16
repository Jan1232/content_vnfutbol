from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "fonts"
FONT_PATH = ASSETS / "Inter-Medium.ttf"  # weight 500
FONT_FALLBACK = ASSETS / "Inter-Regular.ttf"
_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _font(size: int = 32) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if size not in _FONT_CACHE:
        path = FONT_PATH if FONT_PATH.exists() else FONT_FALLBACK
        if path.exists():
            _FONT_CACHE[size] = ImageFont.truetype(str(path), size=size)
        else:
            _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


def apply_text_watermark(
    image_bytes: bytes,
    text: str,
    *,
    color: tuple[int, int, int, int] = (255, 255, 255, 255),
    font_size: int = 32,
    margin: int = 20,
    shadow_opacity: float = 0.25,
    shadow_blur: float = 10,
) -> bytes:
    """Вотермарка: #fff Inter Medium 32px, верхний правый угол, тень 0.25 / blur 10."""
    text = (text or "").strip()
    if not text:
        return image_bytes

    img = Image.open(BytesIO(image_bytes))
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    font = _font(font_size)
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = tmp.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # верхний правый угол
    x = max(margin, img.width - tw - margin)
    y = margin

    shadow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_draw.text((x, y), text, font=font, fill=(0, 0, 0, 255))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=shadow_blur))
    r, g, b, a = shadow_layer.split()
    a = a.point(lambda p: int(p * shadow_opacity))
    shadow_layer = Image.merge("RGBA", (r, g, b, a))

    text_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_layer)
    text_draw.text((x, y), text, font=font, fill=color)

    composed = Image.alpha_composite(img, shadow_layer)
    composed = Image.alpha_composite(composed, text_layer).convert("RGB")

    buf = BytesIO()
    composed.save(buf, format="JPEG", quality=92, optimize=True)
    return buf.getvalue()
