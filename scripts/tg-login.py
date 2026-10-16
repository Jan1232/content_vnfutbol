#!/usr/bin/env python3
"""QR-логин Telethon для скачивания больших TG-видео.

Запуск:
  cd /var/max-repost && .venv/bin/python scripts/tg-login.py
  cd /var/max-repost && .venv/bin/python scripts/tg-login.py --phone +79001234567
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings, load_dotenv_manual
from app.tg_mtproto import _api_credentials, _proxy_dict, session_path


def _print_qr(url: str) -> None:
    print(f"\nСсылка для входа:\n{url}\n")
    try:
        import qrcode

        q = qrcode.QRCode()
        q.add_data(url)
        q.make(fit=True)
        q.print_ascii(invert=True)
    except Exception:
        print("(qrcode не установлен — откройте ссылку в Telegram)")


async def login_qr(client, timeout: int = 300, password: str | None = None) -> None:
    from telethon.errors import SessionPasswordNeededError

    print("\n=== QR login ===")
    print("Телефон → Telegram → Настройки → Устройства → Подключить устройство\n")
    qr = await client.qr_login()
    _print_qr(qr.url)
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError("QR login timeout")
        wait_for = min(60.0, remaining)
        try:
            await qr.wait(timeout=wait_for)
            return
        except SessionPasswordNeededError:
            pw = (password or "").strip()
            if not pw:
                pw = input("Пароль 2FA: ").strip()
            await client.sign_in(password=pw)
            return
        except asyncio.TimeoutError:
            # токен QR живёт ~30–60с — обновляем
            try:
                qr = await client.qr_login()
            except Exception:
                qr = await qr.recreate()
            if qr is None:
                raise RuntimeError("Не удалось обновить QR")
            print("\nQR обновлён — отсканируйте снова")
            _print_qr(qr.url)


async def login_phone(client, phone: str) -> None:
    await client.send_code_request(phone)
    code = input("Код из Telegram: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except Exception as e:
        if "Two-steps" in type(e).__name__ or "password" in str(e).lower() or "SessionPasswordNeeded" in type(e).__name__:
            pw = input("Пароль 2FA: ").strip()
            await client.sign_in(password=pw)
        else:
            raise


async def main() -> int:
    load_dotenv_manual()
    get_settings.cache_clear()

    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", help="Номер телефона (+7...) вместо QR")
    ap.add_argument("--password", help="Пароль 2FA (если включён)")
    ap.add_argument("--timeout", type=int, default=600, help="Секунд ожидания QR")
    args = ap.parse_args()

    from telethon import TelegramClient

    api_id, api_hash = _api_credentials()
    path = session_path()
    session = str(path).removesuffix(".session")
    proxy = _proxy_dict()

    print(f"session: {session}.session")
    print(f"api_id: {api_id}")
    print(f"proxy: {proxy}")

    client = TelegramClient(session, api_id, api_hash, proxy=proxy)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Уже авторизован: {me.first_name} (@{me.username}) id={me.id}")
            return 0

        if args.phone:
            await login_phone(client, args.phone)
            if args.password and not await client.is_user_authorized():
                await client.sign_in(password=args.password)
        else:
            await login_qr(client, timeout=args.timeout, password=args.password)

        me = await client.get_me()
        print(f"OK: {me.first_name} (@{me.username}) id={me.id}")
        from app.tg_mtproto import invalidate_tg_auth_cache

        invalidate_tg_auth_cache()
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
