"""Generate SEO post text + cover image (logos + date)."""

from __future__ import annotations

import io
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from seo.fixtures import Match, MSK
from seo.titles import team_display_ru, team_name_ru

SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"

_MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def format_kickoff_msk(m: Match) -> str:
    """20 августа 2026, четверг — 22:00 МСК"""
    dt = m.kickoff_msk
    return (
        f"{dt.day} {_MONTHS_RU[dt.month - 1]} {dt.year}, "
        f"{_WEEKDAYS_RU[dt.weekday()]} — {dt.strftime('%H:%M')} МСК"
    )


def format_kickoff_short(m: Match) -> str:
    """Короткая дата для картинки: 20 августа · 22:00 МСК"""
    dt = m.kickoff_msk
    return f"{dt.day} {_MONTHS_RU[dt.month - 1]} · {dt.strftime('%H:%M')} МСК"


def resolve_competition_label(
    m: Match,
    *,
    label: str = "",
    label_qual: str = "",
) -> str:
    if m.is_qualifying:
        if label_qual:
            return label_qual.strip()
        base = (label or m.competition_name or m.competition or "матч").strip()
        if base.lower().startswith("квалификация"):
            return base
        # «Лига Европы УЕФА» → «Квалификация Лиги Европы УЕФА»
        parts = base.split(None, 1)
        if len(parts) == 2 and parts[0].casefold() == "лига":
            return f"Квалификация Лиги {parts[1]}"
        return f"Квалификация · {base}"
    if label:
        return label.strip()
    return (m.competition_name or m.competition or "матч").strip()


def build_post_text_template(
    m: Match,
    *,
    home_ru: str,
    away_ru: str,
    target_community: str,
    competition_label: str = "",
    hook: str = "",
) -> str:
    comp = competition_label or m.competition_name or m.competition or "матч"
    when = format_kickoff_msk(m)
    home = home_ru or team_display_ru(m.home_team)
    away = away_ru or team_display_ru(m.away_team)
    hook = (hook or "").strip()
    if hook:
        headline = f"⚽️ {home} — {away}: {hook}"
    else:
        headline = f"⚽️ {home} — {away}"
    return (
        f"{headline}\n"
        f"\n"
        f"🏆 {comp}\n"
        f"🗓 {when}\n"
        f"\n"
        f"Новости, составы и главное о матче:\n"
        f"{target_community}"
    )


def _default_hook(m: Match) -> str:
    if m.is_qualifying:
        return "битва за путёвку в следующий раунд"
    return "главный матч дня"


def generate_hook_openclaw(
    *,
    home: str,
    away: str,
    competition: str,
    when: str,
) -> str:
    """Короткая фраза-хук после двоеточия; при ошибке — дефолт."""
    settings = get_settings()
    base = (settings.openclaw_base_url or "").rstrip("/")
    token = (settings.openclaw_api_key or "").strip()
    if not base or not token:
        return ""

    prompt = (
        "Напиши ТОЛЬКО короткую фразу-хук на русском для анонса матча "
        "(3–8 слов), без кавычек, без эмодзи, без точки в конце. "
        "Пример: битва за путёвку в Лигу Европы\n"
        f"Команды: {home} — {away}\n"
        f"Турнир: {competition}\n"
        f"Когда: {when}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    backend = (settings.openclaw_backend_model or "").strip()
    if backend:
        headers["x-openclaw-model"] = backend
    payload = {
        "model": settings.openclaw_model or "openclaw/default",
        "temperature": 0.5,
        "max_tokens": 80,
        "messages": [
            {"role": "system", "content": "Ответь только хук-фразой."},
            {"role": "user", "content": prompt},
        ],
    }
    verify = SYSTEM_CA if Path(SYSTEM_CA).exists() else True
    try:
        with httpx.Client(timeout=60.0, verify=verify) as client:
            r = client.post(f"{base}/chat/completions", headers=headers, json=payload)
            if r.status_code >= 400:
                return ""
            data = r.json()
        text = data["choices"][0]["message"]["content"].strip()
        text = re.sub(r"^['\"«»]+|['\"«»]+$", "", text).strip()
        text = re.sub(r"[.!?…]+$", "", text).strip()
        text = re.sub(r"\s+", " ", text)
        if len(text) < 3 or len(text) > 80:
            return ""
        # после двоеточия — со строчной
        if text[0].isupper() and not text.isupper():
            text = text[0].lower() + text[1:]
        return text
    except Exception as e:
        print(f"[seo] openclaw hook error: {e}", flush=True)
        return ""


def generate_post_text(
    m: Match,
    *,
    target_community: str,
    competition_label: str = "",
    competition_label_qual: str = "",
    polish: bool = True,
) -> tuple[str, str, str]:
    home_ru = team_display_ru(m.home_team)
    away_ru = team_display_ru(m.away_team)
    # SEO-хранилище по-прежнему lowercase
    home_store = team_name_ru(m.home_team)
    away_store = team_name_ru(m.away_team)
    comp = resolve_competition_label(
        m, label=competition_label, label_qual=competition_label_qual
    )
    when = format_kickoff_msk(m)
    hook = ""
    if polish:
        hook = generate_hook_openclaw(
            home=home_ru, away=away_ru, competition=comp, when=when
        )
    if not hook:
        hook = _default_hook(m)
    text = build_post_text_template(
        m,
        home_ru=home_ru,
        away_ru=away_ru,
        target_community=target_community,
        competition_label=comp,
        hook=hook,
    )
    return text, home_store, away_store


def _download_logo(url: str, client: httpx.Client) -> Any | None:
    if not url:
        return None
    try:
        from PIL import Image

        r = client.get(url, timeout=20.0)
        if r.status_code >= 400 or not r.content:
            return None
        img = Image.open(io.BytesIO(r.content)).convert("RGBA")
        return img
    except Exception as e:
        print(f"[seo] logo download fail {url[:80]}: {e}", flush=True)
        return None


def _fit_logo(img: Any, size: int) -> Any:
    from PIL import Image

    img = img.copy()
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    x = (size - img.width) // 2
    y = (size - img.height) // 2
    canvas.paste(img, (x, y), img)
    return canvas


def _load_font(size: int, bold: bool = False) -> Any:
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if path and Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _draw_centered(draw: Any, y: int, text: str, font: Any, fill: tuple, width: int) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((width - tw) // 2, y), text, font=font, fill=fill)


def compose_match_poster(
    m: Match,
    *,
    home_ru: str,
    away_ru: str,
    competition_label: str = "",
    background: bytes | None = None,
) -> bytes | None:
    """Обложка матча: фон + логотипы клубов + дата."""
    try:
        from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    except Exception:
        return None

    w, h = 1280, 720
    if background:
        try:
            bg = Image.open(io.BytesIO(background)).convert("RGB")
            bg = bg.resize((w, h), Image.Resampling.LANCZOS)
            bg = ImageEnhance.Brightness(bg).enhance(0.45)
            bg = ImageEnhance.Contrast(bg).enhance(1.1)
            bg = bg.filter(ImageFilter.GaussianBlur(radius=1.2))
        except Exception:
            bg = Image.new("RGB", (w, h), (10, 22, 48))
    else:
        bg = Image.new("RGB", (w, h), (10, 22, 48))
        draw0 = ImageDraw.Draw(bg)
        for y in range(h):
            t = y / h
            r = int(8 + 18 * t)
            g = int(16 + 30 * (1 - t))
            b = int(40 + 50 * t)
            draw0.line([(0, y), (w, y)], fill=(r, g, b))

    canvas = bg.convert("RGBA")
    # тёмная подложка для читаемости
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([(0, 0), (w, 110)], fill=(0, 0, 0, 120))
    od.rectangle([(0, h - 140), (w, h)], fill=(0, 0, 0, 150))
    canvas = Image.alpha_composite(canvas, overlay)

    home_logo = away_logo = None
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        home_logo = _download_logo(m.home_logo_url, client)
        away_logo = _download_logo(m.away_logo_url, client)

    logo_size = 240
    if home_logo:
        home_logo = _fit_logo(home_logo, logo_size)
        canvas.paste(home_logo, (180, 200), home_logo)
    if away_logo:
        away_logo = _fit_logo(away_logo, logo_size)
        canvas.paste(away_logo, (w - 180 - logo_size, 200), away_logo)

    draw = ImageDraw.Draw(canvas)
    font_comp = _load_font(30, bold=True)
    font_vs = _load_font(54, bold=True)
    font_team = _load_font(36, bold=True)
    font_date = _load_font(40, bold=True)

    comp = (competition_label or m.competition_name or "").strip()
    if comp:
        _draw_centered(draw, 36, comp, font_comp, (230, 235, 245), w)

    _draw_centered(draw, 280, "—", font_vs, (255, 255, 255), w)

    home = home_ru or team_display_ru(m.home_team)
    away = away_ru or team_display_ru(m.away_team)
    # имена под логотипами
    hb = draw.textbbox((0, 0), home, font=font_team)
    ab = draw.textbbox((0, 0), away, font=font_team)
    draw.text(
        (180 + logo_size // 2 - (hb[2] - hb[0]) // 2, 460),
        home,
        font=font_team,
        fill=(255, 255, 255),
    )
    draw.text(
        (w - 180 - logo_size // 2 - (ab[2] - ab[0]) // 2, 460),
        away,
        font=font_team,
        fill=(255, 255, 255),
    )

    dt = m.kickoff_msk
    full_date = (
        f"{dt.day} {_MONTHS_RU[dt.month - 1]} {dt.year}  ·  {dt.strftime('%H:%M')} МСК"
    )
    _draw_centered(draw, h - 100, full_date, font_date, (255, 220, 90), w)

    out = canvas.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _image_prompt(m: Match, home_ru: str, away_ru: str, competition_label: str) -> str:
    comp = competition_label or m.competition_name or "UEFA Europa League"
    return (
        f"Wide cinematic football stadium night atmosphere background only, "
        f"floodlights, empty pitch blur, dramatic smoke and color wash for "
        f"{home_ru} vs {away_ru}, {comp}. "
        f"No people faces, no players, no club logos, no crests, no badges, "
        f"no readable text, no watermarks, no scoreboard. Soft bokeh, 16:9."
    )


def _generate_ai_background(
    m: Match,
    *,
    home_ru: str,
    away_ru: str,
    competition_label: str,
    out_dir: Path,
) -> bytes | None:
    prompt = _image_prompt(m, home_ru, away_ru, competition_label)
    out_path = out_dir / f"bg_{m.match_id.replace(':', '_')}.png"
    cmd = [
        "openclaw",
        "infer",
        "image",
        "generate",
        "--prompt",
        prompt,
        "--size",
        "1536x1024",
        "--output",
        str(out_path),
        "--json",
        "--timeout-ms",
        "180000",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=200,
            check=False,
        )
    except FileNotFoundError:
        print("[seo] openclaw binary not found", flush=True)
        return None
    except subprocess.TimeoutExpired:
        print("[seo] openclaw image timeout", flush=True)
        return None

    if out_path.is_file() and out_path.stat().st_size > 1000:
        return out_path.read_bytes()

    raw = (proc.stdout or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            b64 = _extract_b64(data)
            if b64:
                import base64

                return base64.b64decode(b64)
            path = _extract_path(data)
            if path and Path(path).is_file():
                return Path(path).read_bytes()
        except Exception:
            pass
    if proc.returncode != 0:
        print(
            f"[seo] openclaw image fail rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[:300]}",
            flush=True,
        )
    return None


def generate_cover_image(
    m: Match,
    *,
    home_ru: str,
    away_ru: str,
    competition_label: str = "",
    out_dir: Path | None = None,
) -> bytes | None:
    """Фон (AI или градиент) + обязательные логотипы клубов и дата."""
    settings = get_settings()
    out_dir = out_dir or (settings.data_dir / "seo_covers")
    out_dir.mkdir(parents=True, exist_ok=True)

    home_disp = team_display_ru(m.home_team)
    away_disp = team_display_ru(m.away_team)

    bg = _generate_ai_background(
        m,
        home_ru=home_disp,
        away_ru=away_disp,
        competition_label=competition_label,
        out_dir=out_dir,
    )
    poster = compose_match_poster(
        m,
        home_ru=home_disp,
        away_ru=away_disp,
        competition_label=competition_label,
        background=bg,
    )
    if poster:
        out_path = out_dir / f"match_{m.match_id.replace(':', '_')}.jpg"
        out_path.write_bytes(poster)
        return poster
    return _fallback_placeholder_image(home_disp, away_disp, m)


def _extract_b64(data: Any) -> str | None:
    if isinstance(data, dict):
        for k in ("b64_json", "base64", "image_base64"):
            if isinstance(data.get(k), str) and len(data[k]) > 100:
                return data[k]
        for v in data.values():
            found = _extract_b64(v)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _extract_b64(item)
            if found:
                return found
    return None


def _extract_path(data: Any) -> str | None:
    if isinstance(data, dict):
        for k in ("path", "output", "file", "filename"):
            v = data.get(k)
            if isinstance(v, str) and v.endswith((".png", ".jpg", ".jpeg", ".webp")):
                return v
        for v in data.values():
            found = _extract_path(v)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _extract_path(item)
            if found:
                return found
    return None


def _fallback_placeholder_image(
    home_ru: str, away_ru: str, m: Match | None = None
) -> bytes | None:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    w, h = 1280, 720
    img = Image.new("RGB", (w, h), (12, 28, 58))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        c = int(12 + (40 * y / h))
        draw.line([(0, y), (w, y)], fill=(c, 20, 70))
    font_big = _load_font(48, bold=True)
    font_sm = _load_font(28, bold=False)
    title = f"{home_ru}  —  {away_ru}"
    _draw_centered(draw, h // 2 - 60, title, font_big, (255, 255, 255), w)
    if m is not None:
        _draw_centered(
            draw,
            h // 2 + 20,
            format_kickoff_short(m),
            font_sm,
            (200, 210, 230),
            w,
        )
    buf = tempfile.SpooledTemporaryFile()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return buf.read()
