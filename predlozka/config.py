from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def bot_token() -> str:
    token = (os.environ.get("PREDLOZKA_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("PREDLOZKA_BOT_TOKEN не задан")
    return token


def content_bot_token() -> str:
    """Токен бота контент-менеджера (MAX_BOT_TOKEN), для ЛС с рекламными ссылками."""
    token = (os.environ.get("MAX_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("MAX_BOT_TOKEN не задан")
    return token


def api_base() -> str:
    return (os.environ.get("MAX_API_BASE") or "https://platform-api2.max.ru").rstrip("/")


def parse_user_ids(raw: str) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            uid = int(part)
        except ValueError:
            continue
        if uid and uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


def erid_owner_ids() -> list[int]:
    for key in ("PREDLOZKA_ERID_OWNER", "DIGEST_OWNER_USER_ID", "PREDLOZKA_GUARD_OWNER"):
        ids = parse_user_ids((os.environ.get(key) or "").strip())
        if ids:
            return ids
    return []


def erid_owner_id() -> int:
    ids = erid_owner_ids()
    return ids[0] if ids else 0


def erid_tz() -> ZoneInfo:
    name = (os.environ.get("PREDLOZKA_ERID_TZ") or "Asia/Yekaterinburg").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Yekaterinburg")


def erid_digest_hm() -> tuple[int, int]:
    hour = 22
    minute = 0
    raw_h = (os.environ.get("PREDLOZKA_ERID_HOUR") or "").strip()
    raw_m = (os.environ.get("PREDLOZKA_ERID_MINUTE") or "").strip()
    try:
        if raw_h:
            hour = int(raw_h)
        if raw_m:
            minute = int(raw_m)
    except ValueError:
        hour, minute = 22, 0
    return max(0, min(23, hour)), max(0, min(59, minute))


def db_path() -> Path:
    raw = (os.environ.get("PREDLOZKA_DB") or "").strip()
    if raw:
        return Path(raw)
    return ROOT / "data" / "predlozka.db"
