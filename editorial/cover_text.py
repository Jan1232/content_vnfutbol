"""Pixel budget for the default cover headline (70% width × 4 lines)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.config import ROOT

FONTS_DIR = ROOT / "editorial" / "templates" / "assets" / "fonts"
LATIN_FONT = FONTS_DIR / "BebasNeue-Regular.ttf"
CYR_FONT = FONTS_DIR / "SofiaSansExtraCondensed-Black.ttf"

CARD_WIDTH_PX = 1080
TEXT_WIDTH_RATIO = 0.70
LINE_WIDTH_PX = int(CARD_WIDTH_PX * TEXT_WIDTH_RATIO)  # 756
FONT_SIZE_PX = 81
LETTER_SPACING_EM = 0.02
LETTER_SPACING_PX = FONT_SIZE_PX * LETTER_SPACING_EM  # 1.62
MAX_LINES = 4

# CSS unicode-range of the Cyrillic @font-face (Sofia Sans Extra Condensed).
_CYR_RANGES = (
    (0x0400, 0x045F),
    (0x0490, 0x0491),
    (0x04B0, 0x04B1),
)
_CYR_EXTRA = {0x0301, 0x2116}

# Trailing particles look unfinished if wrap cuts the phrase there.
_TRAILING_PARTICLES = {
    "в",
    "во",
    "на",
    "за",
    "с",
    "со",
    "и",
    "к",
    "ко",
    "у",
    "о",
    "об",
    "от",
    "до",
    "по",
    "из",
    "для",
    "при",
    "про",
    "над",
    "под",
    "без",
    "после",
    "перед",
    "через",
    "вместо",
    "кроме",
}
_ADJ_TAIL = ("ого", "его", "ому", "ему", "ой", "ый", "ий", "ая", "ое", "ые", "их", "ых")

# Mean uppercase Cyrillic glyph ≈ 41.4px → ~18 chars/line. Word-wrap on
# football names wastes ~20%, so the LLM budget is ~60 signs / 12 words.
# The hard cap is still pixel wrap (clip_to_cover), not these numbers.
PROMPT_MAX_CHARS = 60
PROMPT_MAX_WORDS = 12


def _is_cyrillic(ch: str) -> bool:
    code = ord(ch)
    if code in _CYR_EXTRA:
        return True
    return any(lo <= code <= hi for lo, hi in _CYR_RANGES)


@lru_cache(maxsize=1)
def _fonts():
    from PIL import ImageFont

    try:
        latin = ImageFont.truetype(str(LATIN_FONT), FONT_SIZE_PX)
        cyr = ImageFont.truetype(str(CYR_FONT), FONT_SIZE_PX)
    except OSError:
        return None
    return latin, cyr


def measure_width(text: str) -> float:
    """Advance width of uppercase headline text, matching the cover CSS."""
    raw = (text or "").upper()
    if not raw:
        return 0.0
    fonts = _fonts()
    if fonts is None:
        return float(len(raw) * 42)
    latin, cyr = fonts
    total = 0.0
    i = 0
    n = len(raw)
    while i < n:
        use_cyr = _is_cyrillic(raw[i])
        j = i + 1
        while j < n and _is_cyrillic(raw[j]) == use_cyr:
            j += 1
        font = cyr if use_cyr else latin
        total += font.getlength(raw[i:j])
        i = j
    if n > 1:
        total += LETTER_SPACING_PX * (n - 1)
    return float(total)


def wrap_lines(text: str, *, max_lines: int | None = None) -> list[str]:
    """Greedy CSS-like wrap (break on spaces) into at most max_lines."""
    limit = MAX_LINES if max_lines is None else max_lines
    words = (text or "").split()
    if not words or limit <= 0:
        return []
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        if len(lines) >= limit:
            break
        trial = " ".join(current + [word])
        if current and measure_width(trial) > LINE_WIDTH_PX:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current and len(lines) < limit:
        # A single oversized word still occupies a line; CSS break-word clips it.
        lines.append(" ".join(current))
    return lines[:limit]


def line_count(text: str) -> int:
    return len(wrap_lines(text, max_lines=64))


def fits_cover(text: str) -> bool:
    return line_count(text) <= MAX_LINES


def _bare(word: str) -> str:
    return word.lower().strip("«»:;,.…—–-")


def _drop_trailing_particle(text: str) -> str:
    words = text.split()
    while len(words) > 1:
        last = _bare(words[-1])
        if last in _TRAILING_PARTICLES:
            words.pop()
            continue
        if last.endswith(_ADJ_TAIL) and _bare(words[-2]) in _TRAILING_PARTICLES:
            words.pop()
            words.pop()
            continue
        break
    return " ".join(words)


def clip_to_cover(text: str) -> str:
    """Keep only what fits in 70% × 4 lines; join back into one phrase."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    if fits_cover(cleaned):
        return cleaned
    clipped = " ".join(wrap_lines(cleaned))
    return _drop_trailing_particle(clipped)


def prompt_limit_text() -> str:
    return (
        f"Один цельный блок, без деления на две строки: CSS сам перенесёт. "
        f"Ширина текста 70% карточки, максимум {MAX_LINES} строки "
        f"(это не больше {PROMPT_MAX_WORDS} слов и {PROMPT_MAX_CHARS} знаков с пробелами "
        f"при заголовке 81px). Лучше короче, чем обрежется."
    )
