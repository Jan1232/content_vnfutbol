from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings, load_dotenv_manual
from app.db import init_db
from editorial.tg_moderator.bot import run_poll_loop


def main() -> None:
    load_dotenv_manual()
    settings = get_settings()
    init_db()
    if not settings.api_telegram_bot_token:
        raise SystemExit("API_TELEGRAM_BOT_TOKEN не задан")
    if not int(settings.telegram_admin_id or 0):
        raise SystemExit("TELEGRAM_ADMIN_ID не задан")
    print(
        f"[tg-moderator] admin={settings.telegram_admin_id} moderation={settings.editorial_tg_moderation}",
        flush=True,
    )
    try:
        run_poll_loop()
    except KeyboardInterrupt:
        print("[tg-moderator] stop", flush=True)
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
