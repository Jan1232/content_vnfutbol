"""Jinja HTML template → PNG via Playwright screenshot of #card."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import ROOT, get_settings
from editorial.cover_text import clip_to_cover

TEMPLATES_DIR = ROOT / "editorial" / "templates"
COVERS_DIR = ROOT / "data" / "editorial" / "covers"

_VIEWPORTS = {
    "breaking": (1080, 1080),
    "transfer": (1080, 1080),
    "default": (1080, 1080),
    "roundup": (1080, 1080),
    "matchday": (1080, 1350),
    "result": (1080, 1080),
}

PREVIEW_TPL_PREFIX = "/editorial/label-photos/tpl"
_PLASHKA_BY_TEMPLATE = {
    "transfer": "plashka_transpher.png",
}


def _plashka_filename(template_name: str) -> str:
    return _PLASHKA_BY_TEMPLATE.get(template_name or "", "plashka_default.png")


_TPL_ASSET_FILES = {
    "logo.png": TEMPLATES_DIR / "assets" / "logo.png",
    "logo_vnf.png": TEMPLATES_DIR / "assets" / "logo_vnf.png",
    "plashka_default.png": TEMPLATES_DIR / "assets" / "plashka_default.png",
    "plashka_transpher.png": TEMPLATES_DIR / "assets" / "plashka_transpher.png",
    "BebasNeue-Regular.ttf": TEMPLATES_DIR / "assets" / "fonts" / "BebasNeue-Regular.ttf",
    "SofiaSansExtraCondensed-Black.ttf": TEMPLATES_DIR
    / "assets"
    / "fonts"
    / "SofiaSansExtraCondensed-Black.ttf",
    "Inter-Regular.ttf": TEMPLATES_DIR / "assets" / "fonts" / "Inter-Regular.ttf",
    "Inter-Regular-Cyr.ttf": TEMPLATES_DIR / "assets" / "fonts" / "Inter-Regular-Cyr.ttf",
    "Oswald-Bold.ttf": TEMPLATES_DIR / "assets" / "fonts" / "Oswald-Bold.ttf",
    "Oswald-Bold-Cyr.ttf": TEMPLATES_DIR / "assets" / "fonts" / "Oswald-Bold-Cyr.ttf",
}


def tpl_asset_path(filename: str) -> Path | None:
    path = _TPL_ASSET_FILES.get(filename)
    if path is None or not path.is_file():
        return None
    return path


def preview_asset_context(template_name: str = "default") -> dict[str, str]:
    p = PREVIEW_TPL_PREFIX
    return {
        "cover_logo_uri": f"{p}/logo.png",
        "logo_uri": f"{p}/logo_vnf.png",
        "plashka_uri": f"{p}/{_plashka_filename(template_name)}",
        "font_oswald": f"{p}/Oswald-Bold.ttf",
        "font_oswald_cyr": f"{p}/Oswald-Bold-Cyr.ttf",
        "font_inter": f"{p}/Inter-Regular.ttf",
        "font_inter_cyr": f"{p}/Inter-Regular-Cyr.ttf",
        "font_bebas": f"{p}/BebasNeue-Regular.ttf",
        "font_bebas_cyr": f"{p}/SofiaSansExtraCondensed-Black.ttf",
    }


def preview_html(
    template_name: str,
    photo_uri: str,
    caption_line1: str,
    caption_line2: str | None,
    badge_text: str,
    channel_brand: dict[str, Any],
) -> str:
    """Тот же HTML, что уходит в PNG, но с HTTP-ассетами — для живого превью в админке."""
    name = (template_name or "default").strip()
    if name not in _VIEWPORTS:
        name = "default"
    width, height = _VIEWPORTS[name]
    headline = clip_to_cover(
        " ".join(part for part in (caption_line1, caption_line2) if part)
    )
    return _env().get_template(f"{name}.html.j2").render(
        photo_uri=photo_uri or "",
        caption_line1=headline,
        caption_line2="",
        badge_text=badge_text or "",
        brand_name=channel_brand.get("name") or "",
        accent_color=channel_brand.get("accent_color") or "#E11D2A",
        width=width,
        height=height,
        cover_handle=channel_brand.get("cover_handle") or "@channel_vnfutbol",
        **preview_asset_context(name),
    )


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )


def _file_to_data_uri(path: Path) -> str:
    if not path.is_file():
        return ""
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    if path.suffix.lower() in {".ttf", ".otf"}:
        mime = "font/ttf"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _ensure_logo(logo_path: str, brand_name: str, accent: str) -> str:
    path = Path(logo_path) if logo_path else Path()
    if not path.is_absolute():
        path = ROOT / "editorial" / path
    if path.is_file():
        return _file_to_data_uri(path)
    generated = ROOT / "editorial" / "templates" / "assets" / "logo_vnf.png"
    if not generated.is_file():
        _generate_logo(generated, brand_name or "VNF", accent or "#E11D2A")
    return _file_to_data_uri(generated) if generated.is_file() else ""


def _generate_logo(dest: Path, name: str, accent: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    dest.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((8, 8, 248, 248), radius=48, fill=accent)
    initials = "".join(ch for ch in name.upper() if ch.isalnum())[:3] or "VNF"
    font_path = TEMPLATES_DIR / "assets" / "fonts" / "Oswald-Bold-Cyr.ttf"
    if not font_path.is_file():
        font_path = TEMPLATES_DIR / "assets" / "fonts" / "Oswald-Bold.ttf"
    try:
        font = ImageFont.truetype(str(font_path), 64)
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), initials, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((256 - tw) / 2, (256 - th) / 2 - 8), initials, fill="white", font=font)
    img.save(dest)


def render_post(
    template_name: str,
    photo_path: str,
    caption_line1: str,
    caption_line2: str | None,
    badge_text: str,
    channel_brand: dict[str, Any],
    *,
    news_id: int | str = "cover",
) -> str:
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    name = (template_name or "default").strip()
    if name not in _VIEWPORTS:
        name = "default"
    tpl_file = f"{name}.html.j2"
    photo = Path(photo_path)
    photo_uri = _file_to_data_uri(photo) if photo.is_file() else ""
    logo_uri = _ensure_logo(
        str(channel_brand.get("logo") or ""),
        str(channel_brand.get("name") or ""),
        str(channel_brand.get("accent_color") or "#E11D2A"),
    )
    assets = TEMPLATES_DIR / "assets"
    width, height = _VIEWPORTS[name]
    headline = clip_to_cover(
        " ".join(part for part in (caption_line1, caption_line2) if part)
    )
    html = _env().get_template(tpl_file).render(
        photo_uri=photo_uri,
        caption_line1=headline,
        caption_line2="",
        badge_text=badge_text or "",
        brand_name=channel_brand.get("name") or "",
        accent_color=channel_brand.get("accent_color") or "#E11D2A",
        logo_uri=logo_uri,
        cover_logo_uri=_file_to_data_uri(assets / "logo.png"),
        plashka_uri=_file_to_data_uri(assets / _plashka_filename(name)),
        width=width,
        height=height,
        font_oswald=_file_to_data_uri(assets / "fonts" / "Oswald-Bold.ttf"),
        font_oswald_cyr=_file_to_data_uri(assets / "fonts" / "Oswald-Bold-Cyr.ttf"),
        font_inter=_file_to_data_uri(assets / "fonts" / "Inter-Regular.ttf"),
        font_inter_cyr=_file_to_data_uri(assets / "fonts" / "Inter-Regular-Cyr.ttf"),
        font_bebas=_file_to_data_uri(assets / "fonts" / "BebasNeue-Regular.ttf"),
        font_bebas_cyr=_file_to_data_uri(assets / "fonts" / "SofiaSansExtraCondensed-Black.ttf"),
        cover_handle=channel_brand.get("cover_handle") or "@channel_vnfutbol",
    )
    out_path = COVERS_DIR / f"{news_id}.png"
    _screenshot(html, out_path, width, height)
    return str(out_path)


def _brand_context(channel_brand: dict[str, Any], *, template_name: str = "default") -> dict[str, Any]:
    assets = TEMPLATES_DIR / "assets"
    logo_uri = _ensure_logo(
        str(channel_brand.get("logo") or ""),
        str(channel_brand.get("name") or ""),
        str(channel_brand.get("accent_color") or "#E11D2A"),
    )
    return {
        "brand_name": channel_brand.get("name") or "",
        "accent_color": channel_brand.get("accent_color") or "#E11D2A",
        "logo_uri": logo_uri,
        "cover_logo_uri": _file_to_data_uri(assets / "logo.png") or logo_uri,
        "plashka_uri": _file_to_data_uri(assets / _plashka_filename(template_name)),
        "font_oswald": _file_to_data_uri(assets / "fonts" / "Oswald-Bold.ttf"),
        "font_oswald_cyr": _file_to_data_uri(assets / "fonts" / "Oswald-Bold-Cyr.ttf"),
        "font_inter": _file_to_data_uri(assets / "fonts" / "Inter-Regular.ttf"),
        "font_inter_cyr": _file_to_data_uri(assets / "fonts" / "Inter-Regular-Cyr.ttf"),
        "font_bebas": _file_to_data_uri(assets / "fonts" / "BebasNeue-Regular.ttf"),
        "font_bebas_cyr": _file_to_data_uri(assets / "fonts" / "SofiaSansExtraCondensed-Black.ttf"),
        "cover_handle": channel_brand.get("cover_handle") or "@channel_vnfutbol",
    }


def render_card(
    template_name: str,
    context: dict[str, Any],
    *,
    news_id: int | str = "cover",
    channel_brand: dict[str, Any] | None = None,
) -> str:
    """Произвольный HTML→PNG (сетка дня / счёт) без фото игрока."""
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    name = (template_name or "default").strip()
    width, height = _VIEWPORTS.get(name) or (1080, 1080)
    payload = {
        "width": width,
        "height": height,
        **_brand_context(channel_brand or {}, template_name=name),
        **context,
    }
    html = _env().get_template(f"{name}.html.j2").render(**payload)
    out_path = COVERS_DIR / f"{news_id}.png"
    _screenshot(html, out_path, width, height)
    return str(out_path)


def _screenshot(html: str, out_path: Path, width: int, height: int) -> None:
    from playwright.sync_api import sync_playwright

    settings = get_settings()
    headless = bool(settings.playwright_headless)
    tmp = out_path.with_suffix(".html")
    tmp.write_text(html, encoding="utf-8")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )
            page.goto(tmp.resolve().as_uri(), wait_until="networkidle")
            page.wait_for_timeout(250)
            card = page.locator("#card")
            card.wait_for(state="visible", timeout=8000)
            card.screenshot(path=str(out_path), type="png")
            browser.close()
    finally:
        if tmp.exists():
            tmp.unlink()
    if not out_path.is_file() or out_path.stat().st_size < 1000:
        raise RuntimeError("render: пустой PNG")


BADGE_FOR_EVENT = {
    "transfer": "ТРАНСФЕР",
    "injury": "BREAKING",
    "match_result": "СЧЁТ",
    "official_statement": "ОФИЦИАЛЬНО",
    "lineup": "СОСТАВ",
    "lifestyle": "ЛЮДИ",
}

_HANDLE_TEMPLATES = frozenset({"default", "transfer", "breaking", "result"})


def render_mirror_cover(
    channel: Any,
    item: dict[str, Any],
    *,
    channel_brand: dict[str, Any],
) -> str | None:
    """Перерендер обложки с TG-handle (default/transfer)."""
    from editorial.channel_config import EditorialChannelConfig

    if not isinstance(channel, EditorialChannelConfig):
        return None
    post_kind = str(item.get("post_kind") or "")
    if post_kind in {"meme", "video"} or str(item.get("media_type") or "") == "video":
        return None
    template = channel.template_for(item.get("event_type") or "other")
    if template not in _HANDLE_TEMPLATES:
        return None
    image_path = str(item.get("image_path") or "")
    if not image_path or not Path(image_path).is_file():
        return None
    news_id = item.get("id") or "cover"
    badge = BADGE_FOR_EVENT.get(item.get("event_type") or "", "НОВОСТЬ")
    return render_post(
        template,
        image_path,
        item.get("caption_line1") or "",
        item.get("caption_line2") or None,
        badge,
        channel_brand,
        news_id=f"{news_id}_tg",
    )
