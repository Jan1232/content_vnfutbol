"""Локальные картинки для квизов: player/ и club/ с суффиксом канала."""

from __future__ import annotations

import random
from pathlib import Path

from app.config import get_settings

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Канал → суффикс в имени файла (player_apl.png, club_fifa.png)
CHANNEL_IMAGE_SUFFIX: dict[str, str] = {
    "quiz_apl": "_apl",
    "quiz_fifa": "_fifa",
}


def quiz_assets_root() -> Path:
    settings = get_settings()
    root = Path(settings.opros_quiz_assets_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "player").mkdir(parents=True, exist_ok=True)
    (root / "club").mkdir(parents=True, exist_ok=True)
    return root


def image_suffix_for(channel_key: str) -> str:
    return CHANNEL_IMAGE_SUFFIX.get(channel_key, "")


def list_images(kind: str, *, channel_key: str | None = None) -> list[Path]:
    folder = quiz_assets_root() / kind
    if not folder.is_dir():
        return []
    suffix = image_suffix_for(channel_key) if channel_key else ""
    files: list[Path] = []
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS or p.name.startswith("."):
            continue
        stem = p.stem.casefold()
        if suffix:
            # только файлы с нужным суффиксом: *_apl / *_fifa
            if not stem.endswith(suffix.casefold()):
                continue
        files.append(p)
    return files


def pick_image(
    kind: str,
    *,
    channel_key: str | None = None,
    used_names: set[str] | None = None,
) -> Path | None:
    """Случайная картинка канала; предпочитает ещё не использованные имена файлов."""
    files = list_images(kind, channel_key=channel_key)
    if not files:
        return None
    used = {n.casefold() for n in (used_names or set())}
    fresh = [p for p in files if p.name.casefold() not in used]
    pool = fresh or files
    return random.choice(pool)


def ensure_quiz_asset_dirs() -> dict[str, str]:
    root = quiz_assets_root()
    readme = root / "README.txt"
    readme.write_text(
        "Картинки для футбольных квизов (MAX).\n"
        "\n"
        "player/  — изображения для квиза «Угадай игрока»\n"
        "club/    — изображения для квиза «Угадай клуб»\n"
        "\n"
        "Имена файлов по каналу:\n"
        "  *_apl   → https://max.ru/channel_aplmatch  (quiz_apl)\n"
        "  *_fifa  → https://max.ru/channel_fifamatch (quiz_fifa)\n"
        "\n"
        "Примеры: player_apl.png, club_apl.png, player_fifa.png, club_fifa.png\n"
        "Форматы: jpg, jpeg, png, webp, gif.\n",
        encoding="utf-8",
    )
    return {
        "root": str(root),
        "player": str(root / "player"),
        "club": str(root / "club"),
    }
