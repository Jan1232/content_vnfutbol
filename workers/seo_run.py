from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings, load_dotenv_manual
from app.db import init_db
from seo.cycle import run_seo_tick


def main() -> None:
    load_dotenv_manual()
    settings = get_settings()
    init_db()
    print(
        f"[seo-worker] start poll={settings.seo_poll_interval_sec}s "
        f"token={'yes' if (settings.football_data_token or '').strip() else 'NO'}",
        flush=True,
    )
    while True:
        try:
            results = run_seo_tick()
            for r in results:
                print(f"[seo-worker] {r}", flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(max(30, int(settings.seo_poll_interval_sec or 300)))


if __name__ == "__main__":
    main()
