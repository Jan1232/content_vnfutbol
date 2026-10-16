"""Снимает пользовательский access_token VK из сохранённой сессии браузера."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote

from app.config import get_settings
from app.db import (
    db,
    get_meta,
    requeue_vk_media_errors,
    set_all_vk_upload_tokens,
    set_meta,
)
from app.tg_bot_api import TgBotApiError, TgBotClient

STATE_NAME = "vk_storage_state.json"
PROFILE_NAME = "vk_browser"
_LOGIN_ALERT_KEY = "vk_session_login_alert_ts"
_REFRESHED_KEY = "vk_upload_token_refreshed_at"
_ERROR_KEY = "vk_upload_token_last_error"
_TOKEN_RE = re.compile(
    r"""(?:access_token|accessToken)["'\s:=]+([A-Za-z0-9._~+/=%-]{20,})"""
)
_VK_TOKEN_RE = re.compile(r"(vk[12]\.[A-Za-z0-9._~+/=%-]{40,})")


def vk_state_path() -> Path:
    return get_settings().data_dir / STATE_NAME


def vk_profile_dir() -> Path:
    return get_settings().data_dir / PROFILE_NAME


def _usable_token(raw: str) -> str:
    token = (raw or "").strip().rstrip("&\"',")
    if len(token) < 40 or token.lower().startswith("anonym"):
        return ""
    return token


def extract_access_token(*blobs: str) -> str:
    found = ""
    for blob in blobs:
        if not blob:
            continue
        m = _TOKEN_RE.search(blob)
        if m:
            raw = _usable_token(unquote(m.group(1)))
            if raw.startswith(("vk1.", "vk2.")):
                return raw
            if raw and not found:
                found = raw
        m = _VK_TOKEN_RE.search(blob)
        if m:
            raw = _usable_token(unquote(m.group(1)))
            if raw.startswith(("vk1.", "vk2.")):
                return raw
            if raw and not found:
                found = raw
    return found


def active_vk_chat_ids() -> list[int]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT chat_id FROM channels
            WHERE COALESCE(platform, 'max') = 'vk' AND status = 'active'
            """
        ).fetchall()
    return [int(r["chat_id"]) for r in rows]


def vk_session_status() -> dict:
    path = vk_state_path()
    with db() as conn:
        raw = get_meta(conn, _REFRESHED_KEY, "")
        err = get_meta(conn, _ERROR_KEY, "")
    try:
        refreshed_at = float(raw) if raw else 0.0
    except ValueError:
        refreshed_at = 0.0
    return {
        "has_session": path.is_file() or (vk_profile_dir() / "Default").is_dir(),
        "refreshed_at": refreshed_at,
        "last_error": err,
        "vk_count": len(active_vk_chat_ids()),
    }


def save_storage_state_bytes(raw: bytes) -> Path:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or "cookies" not in data:
        raise ValueError("Нужен файл Playwright storage_state (есть ключ cookies)")
    path = vk_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    tmp.replace(path)
    return path


def cookies_from_header(header: str) -> list[dict[str, str]]:
    raw = (header or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not name or name.lower() in {
            "path",
            "domain",
            "expires",
            "max-age",
            "secure",
            "httponly",
            "samesite",
        }:
            continue
        if name in seen:
            continue
        seen.add(name)
        items.append({"name": name, "value": value})
    return items


def storage_state_from_cookie_header(header: str) -> dict:
    items = cookies_from_header(header)
    names = {c["name"] for c in items}
    if "remixsid" not in names and "remixnsid" not in names:
        raise ValueError(
            "В строке нет remixsid. Скопируйте заголовок Cookie из DevTools → Network, "
            "не из document.cookie (там нет HttpOnly)."
        )
    now = int(time.time())
    cookies: list[dict] = []
    session_names = {"remixsid", "remixnsid", "remixstid"}
    for domain in (".vk.com", ".vk.ru", "vk.com", "vk.ru", "m.vk.ru", "m.vk.com"):
        for item in items:
            cookies.append(
                {
                    "name": item["name"],
                    "value": item["value"],
                    "domain": domain,
                    "path": "/",
                    "expires": now + 180 * 24 * 3600,
                    "httpOnly": item["name"] in session_names,
                    "secure": True,
                    "sameSite": "None",
                }
            )
    return {"cookies": cookies, "origins": []}


def save_storage_state_from_cookie_header(header: str) -> Path:
    return save_storage_state_bytes(
        json.dumps(storage_state_from_cookie_header(header), ensure_ascii=False).encode("utf-8")
    )


def _first_vk_group_url() -> str:
    with db() as conn:
        row = conn.execute(
            """
            SELECT link FROM channels
            WHERE COALESCE(platform, 'max') = 'vk'
              AND status = 'active'
              AND COALESCE(hidden, 0) = 0
            ORDER BY title COLLATE NOCASE
            LIMIT 1
            """
        ).fetchone()
    link = (row["link"] if row else "") or ""
    if "vk.com/" in link:
        path = link.split("vk.com/", 1)[1]
        return f"https://m.vk.ru/{path}"
    return "https://m.vk.ru/"


def _notify_need_login(reason: str) -> None:
    settings = get_settings()
    chat_id = int(settings.owner_chat_id or 0)
    tokens = settings.telegram_bot_tokens()
    if not chat_id or not tokens:
        return
    now = time.time()
    with db() as conn:
        raw = get_meta(conn, _LOGIN_ALERT_KEY, "")
        try:
            prev = float(raw) if raw else 0.0
        except ValueError:
            prev = 0.0
        if prev and now - prev < 6 * 3600:
            return
        set_meta(conn, _LOGIN_ALERT_KEY, str(now))
    text = (
        "VK-сессия для фото протухла или не сохранена.\n"
        f"{reason}\n"
        "На этом сервере нет экрана. В админке (блок «Сессия VK») вставьте "
        "заголовок Cookie из DevTools → Network на vk.com."
    )
    try:
        with TgBotClient(tokens[0]) as bot:
            bot.send_message(chat_id, text, parse_mode="HTML")
    except TgBotApiError as e:
        print(f"[vk-token] notify fail: {e}", flush=True)


def _looks_like_login(url: str) -> bool:
    href = (url or "").lower()
    return any(x in href for x in ("login", "oauth", "act=login", "id.vk."))


def _start_xvfb() -> subprocess.Popen | None:
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None
    for n in range(110, 140):
        lock = Path(f"/tmp/.X{n}-lock")
        if lock.exists():
            continue
        display = f":{n}"
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24", "-ac", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)
        if proc.poll() is None:
            os.environ["DISPLAY"] = display
            return proc
        continue
    return None


def _unlock_chrome_profile(profile: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock = profile / name
        try:
            if lock.exists() or lock.is_symlink():
                lock.unlink()
        except OSError:
            pass


def capture_token_from_session() -> str:
    """Открыть сохранённую сессию и перехватить access_token с VK."""
    state = vk_state_path()
    profile = vk_profile_dir()
    has_profile = (profile / "Default").is_dir() or (profile / "Local State").is_file()
    if not has_profile and not state.is_file():
        raise RuntimeError("нет файла сессии в админке")

    from playwright.sync_api import sync_playwright

    found = {"token": ""}
    last_url = {"value": ""}
    mobile = _first_vk_group_url()
    desktop = mobile.replace("https://m.vk.ru/", "https://vk.com/")

    def on_request(req) -> None:
        headers = req.headers or {}
        token = extract_access_token(
            req.url or "",
            req.post_data or "",
            headers.get("authorization") or "",
        )
        if not token:
            return
        if token.startswith(("vk1.", "vk2.")) or not found["token"]:
            found["token"] = token

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]
    xvfb = None
    try:
        xvfb = _start_xvfb()
        headed = bool(os.environ.get("DISPLAY"))
        with sync_playwright() as p:
            context = None
            browser = None
            if has_profile:
                _unlock_chrome_profile(profile)
                try:
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=str(profile),
                        channel="chrome",
                        headless=not headed,
                        args=launch_args,
                        locale="ru-RU",
                        viewport={"width": 1400, "height": 900},
                    )
                except Exception:
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=str(profile),
                        headless=not headed,
                        args=launch_args,
                        locale="ru-RU",
                        viewport={"width": 1400, "height": 900},
                    )
            else:
                try:
                    browser = p.chromium.launch(
                        channel="chrome",
                        headless=not headed,
                        args=launch_args,
                    )
                except Exception:
                    browser = p.chromium.launch(headless=not headed, args=launch_args)
                context = browser.new_context(
                    storage_state=str(state),
                    locale="ru-RU",
                    viewport={"width": 1400, "height": 900},
                )
            page = context.pages[0] if context.pages else context.new_page()
            page.on("request", on_request)
            for target in ("https://m.vk.ru/feed", "https://vk.com/feed", desktop, mobile):
                if found["token"].startswith(("vk1.", "vk2.")):
                    break
                page.goto(target, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)
                last_url["value"] = page.url or ""
                if _looks_like_login(last_url["value"]):
                    continue
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(2500)
            if found["token"] and not _looks_like_login(last_url["value"]):
                try:
                    context.storage_state(path=str(state))
                except Exception:
                    pass
            context.close()
            if browser:
                browser.close()
    finally:
        if xvfb:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=3)
            except Exception:
                xvfb.kill()
            if os.environ.get("DISPLAY", "").startswith(":1"):
                os.environ.pop("DISPLAY", None)

    if not found["token"]:
        if _looks_like_login(last_url["value"]):
            raise RuntimeError("сессия не залогинена (страница входа VK)")
        raise RuntimeError("страница открылась, но access_token в запросах не попался")
    return found["token"]


def refresh_vk_user_token() -> bool:
    """Обновить upload_token у всех активных VK-групп. True если записали новый ключ."""
    if not active_vk_chat_ids():
        return False
    try:
        token = capture_token_from_session()
        from app.vk_api import VkClient

        VkClient(token).can_upload_wall_photo(abs(active_vk_chat_ids()[0]))
    except Exception as e:
        msg = str(e)[:300]
        print(f"[vk-token] refresh fail: {msg}", flush=True)
        with db() as conn:
            set_meta(conn, _ERROR_KEY, msg)
        _notify_need_login(msg[:200])
        return False
    with db() as conn:
        n = set_all_vk_upload_tokens(conn, token)
        set_meta(conn, _REFRESHED_KEY, str(time.time()))
        set_meta(conn, _ERROR_KEY, "")
        queued = 0
        for chat_id in active_vk_chat_ids():
            queued += requeue_vk_media_errors(conn, chat_id)
    print(f"[vk-token] refreshed for {n} groups, requeued={queued}", flush=True)
    return n > 0
