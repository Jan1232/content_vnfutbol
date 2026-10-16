"""Один раз сохранить сессию VK. На сервере без экрана этот скрипт не запустится.

На этом VPS нет X-сервера — оконный Chrome открыть нельзя.
Вставьте заголовок Cookie в админке (блок «Сессия VK для фото»).

Если есть свой компьютер с монитором:

  cd /var/max-repost
  PYTHONPATH=. .venv/bin/python scripts/vk_save_session.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workers.vk_token_refresh import vk_state_path


def main() -> None:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print(
            "Здесь нет графики (нет $DISPLAY): Chrome с окном не откроется.\n"
            "Не используйте xvfb — в чёрном экране всё равно нельзя войти в VK.\n\n"
            "С компьютера, где вы уже залогинены в VK:\n"
            "  1. DevTools (F12) → Network → любой запрос к vk.com\n"
            "  2. Request Headers → скопируйте Cookie\n"
            "  3. Вставьте в админке, блок «Сессия VK для фото»\n"
            "     (не в этот чат и не через document.cookie — там нет remixsid)."
        )
        raise SystemExit(2)

    from playwright.sync_api import sync_playwright

    out = vk_state_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    print("Откроется Chrome. Войдите в VK аккаунтом-админом групп, затем вернитесь в терминал.")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(out.parent / "vk_browser"),
            channel="chrome",
            headless=False,
            locale="ru-RU",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://m.vk.ru/", wait_until="domcontentloaded")
        input("После входа нажмите Enter здесь… ")
        context.storage_state(path=str(out))
        context.close()
    print(f"Сессия сохранена: {out}")
    print("Загрузите этот json в админке (блок «Сессия VK для фото»).")


if __name__ == "__main__":
    main()
