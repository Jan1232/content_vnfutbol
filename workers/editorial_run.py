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
from editorial.cycle import run_editorial_tick


def main() -> None:
    load_dotenv_manual()
    settings = get_settings()
    init_db()
    interval = max(15, int(settings.editorial_poll_interval_sec or 60))
    print(f"[editorial-worker] start poll={interval}s", flush=True)
    while True:
        try:
            results = run_editorial_tick()
            for r in results:
                print(f"[editorial-worker] {r}", flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(interval)


if __name__ == "__main__":
    main()
