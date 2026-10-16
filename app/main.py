from __future__ import annotations

import hashlib
import hmac
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.db import (
    add_source,
    db,
    delete_source,
    get_channel,
    init_db,
    list_channels,
    list_recent_posts,
    list_sources,
    requeue_vk_media_errors,
    set_channel_footer_link,
    set_channel_require_media,
    set_channel_require_text,
    set_channel_upload_token,
    set_channel_watermark,
)
from parsers.telegram import detect_kind
from workers.run import (
    add_tg_channel_by_ref,
    add_vk_group_by_token,
    bootstrap_source,
    get_tg_bot_connections,
    normalize_source_url,
    sync_chats,
    sync_tg_chats,
    telegram_bot_tokens,
    telegram_username_from_url,
)
from app.max_api import MaxClient

settings = get_settings()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.admin_secret,
    session_cookie="maxrepost_sess",
    same_site="strict",
    https_only=False,
    max_age=60 * 60 * 24 * 14,
)

templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
templates.env.filters["ts"] = lambda v: (
    datetime.fromtimestamp(float(v)).strftime("%d.%m %H:%M") if v else "—"
)
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")


def _pwd_hash(password: str) -> str:
    return hashlib.sha256(f"{settings.admin_secret}:{password}".encode()).hexdigest()


def _check_password(password: str) -> bool:
    expected = _pwd_hash(settings.admin_password)
    return hmac.compare_digest(_pwd_hash(password), expected)


def require_auth(request: Request) -> bool:
    return bool(request.session.get("uid") == settings.admin_login)


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    context = dict(ctx or {})
    return templates.TemplateResponse(request, name, context, status_code=status_code)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    try:
        with MaxClient() as client:
            sync_chats(client, refresh_meta=True)
    except Exception as e:
        print(f"[admin] initial sync failed: {e}", flush=True)
    try:
        sync_tg_chats()
    except Exception as e:
        print(f"[admin] tg sync failed: {e}", flush=True)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if require_auth(request):
        return RedirectResponse("/", status_code=302)
    return render(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, login: str = Form(...), password: str = Form(...)):
    if login == settings.admin_login and _check_password(password):
        request.session["uid"] = settings.admin_login
        request.session["csrf"] = secrets.token_urlsafe(24)
        return RedirectResponse("/", status_code=302)
    return render(
        request,
        "login.html",
        {"error": "Неверный логин или пароль"},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


def _csrf_ok(request: Request, token: str) -> bool:
    return bool(token) and hmac.compare_digest(token, request.session.get("csrf") or "")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    with db() as conn:
        channels = [dict(r) for r in list_channels(conn)]
        for ch in channels:
            ch["sources_count"] = len(list_sources(conn, ch["chat_id"]))
    tg_conn = {"bots": [], "chats": [], "error": ""}
    if telegram_bot_tokens():
        try:
            tg_conn = get_tg_bot_connections()
        except Exception as e:
            tg_conn = {"bots": [], "chats": [], "error": str(e)[:300]}
    from workers.vk_token_refresh import vk_session_status

    vk_sess = vk_session_status()
    refreshed = ""
    if vk_sess["refreshed_at"]:
        refreshed = datetime.fromtimestamp(
            vk_sess["refreshed_at"], tz=timezone.utc
        ).astimezone().strftime("%d.%m.%Y %H:%M")
    return render(
        request,
        "index.html",
        {
            "channels": channels,
            "csrf": request.session.get("csrf"),
            "tg_bots_ready": bool(telegram_bot_tokens()),
            "tg_bots": tg_conn.get("bots") or [],
            "tg_bot_chats": tg_conn.get("chats") or [],
            "tg_bot_error": tg_conn.get("error") or "",
            "vk_session": vk_sess,
            "vk_session_refreshed": refreshed,
            "error": request.query_params.get("err") or "",
        },
    )


@app.post("/sync")
def sync_now(request: Request, csrf: str = Form("")):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    try:
        with MaxClient() as client:
            # Кнопка «Обновить список» — полный refresh аватарок и подписчиков
            sync_chats(client, refresh_meta=True)
    except Exception:
        pass
    try:
        sync_tg_chats()
    except Exception:
        pass
    try:
        get_tg_bot_connections(refresh=True)
    except Exception:
        pass
    return RedirectResponse("/", status_code=302)


@app.post("/channels/telegram/add")
def add_telegram_channel(
    request: Request,
    ref: str = Form(""),
    csrf: str = Form(""),
):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from urllib.parse import quote

    try:
        info = add_tg_channel_by_ref(ref)
        return RedirectResponse(
            f"/channels/{info['chat_id']}/sources",
            status_code=302,
        )
    except Exception as e:
        return RedirectResponse(f"/?err={quote(str(e)[:300])}", status_code=302)


@app.post("/channels/vk/add")
def add_vk_channel(
    request: Request,
    token: str = Form(""),
    user_token: str = Form(""),
    csrf: str = Form(""),
):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from urllib.parse import quote

    try:
        info = add_vk_group_by_token(token, user_token=user_token)
        return RedirectResponse(
            f"/channels/{info['owner_id']}/sources",
            status_code=302,
        )
    except Exception as e:
        return RedirectResponse(f"/?err={quote(str(e)[:300])}", status_code=302)


@app.post("/vk-session")
async def upload_vk_session(
    request: Request,
    csrf: str = Form(""),
    file: UploadFile = File(...),
):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from urllib.parse import quote

    from workers.vk_token_refresh import refresh_vk_user_token, save_storage_state_bytes

    try:
        raw = await file.read()
        if len(raw) > 2_000_000:
            raise ValueError("файл сессии слишком большой")
        save_storage_state_bytes(raw)
        refresh_vk_user_token()
        return RedirectResponse("/", status_code=302)
    except Exception as e:
        return RedirectResponse(f"/?err={quote(str(e)[:300])}", status_code=302)


@app.post("/vk-session/cookies")
def save_vk_session_cookies(
    request: Request,
    csrf: str = Form(""),
    cookie_header: str = Form(""),
):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from urllib.parse import quote

    from workers.vk_token_refresh import (
        refresh_vk_user_token,
        save_storage_state_from_cookie_header,
    )

    try:
        raw = (cookie_header or "").strip()
        if not raw:
            raise ValueError("пустой Cookie")
        if len(raw) > 100_000:
            raise ValueError("строка Cookie слишком длинная")
        save_storage_state_from_cookie_header(raw)
        refresh_vk_user_token()
        return RedirectResponse("/", status_code=302)
    except Exception as e:
        return RedirectResponse(f"/?err={quote(str(e)[:300])}", status_code=302)


@app.post("/vk-session/refresh")
def refresh_vk_session_now(request: Request, csrf: str = Form("")):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from urllib.parse import quote

    from workers.vk_token_refresh import refresh_vk_user_token

    try:
        if not refresh_vk_user_token():
            raise RuntimeError("не удалось обновить ключ — проверьте сессию")
        return RedirectResponse("/", status_code=302)
    except Exception as e:
        return RedirectResponse(f"/?err={quote(str(e)[:300])}", status_code=302)


@app.post("/channels/{chat_id}/vk-upload-token")
def save_vk_upload_token(
    request: Request,
    chat_id: int,
    upload_token: str = Form(""),
    csrf: str = Form(""),
):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from urllib.parse import quote

    raw = (upload_token or "").strip()
    if not raw:
        return RedirectResponse("/", status_code=302)
    try:
        from app.vk_api import VkClient

        with db() as conn:
            ch = get_channel(conn, chat_id)
            if not ch or (ch["platform"] or "") != "vk":
                return RedirectResponse("/", status_code=302)
            VkClient(raw).can_upload_wall_photo(abs(int(chat_id)))
            set_channel_upload_token(conn, chat_id, raw)
            requeue_vk_media_errors(conn, chat_id)
        return RedirectResponse("/", status_code=302)
    except Exception as e:
        return RedirectResponse(f"/?err={quote(str(e)[:300])}", status_code=302)


@app.post("/channels/{chat_id}/watermark")
def save_watermark(
    request: Request,
    chat_id: int,
    watermark_text: str = Form(""),
    csrf: str = Form(""),
):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    with db() as conn:
        if get_channel(conn, chat_id):
            set_channel_watermark(conn, chat_id, watermark_text)
    return RedirectResponse("/", status_code=302)


@app.post("/channels/{chat_id}/footer-link")
def save_footer_link(
    request: Request,
    chat_id: int,
    footer_link: str = Form(""),
    footer_link_text: str = Form(""),
    footer_as_button: str = Form(""),
    csrf: str = Form(""),
):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    with db() as conn:
        if get_channel(conn, chat_id):
            set_channel_footer_link(
                conn,
                chat_id,
                footer_link=footer_link,
                footer_link_text=footer_link_text,
                footer_as_button=footer_as_button in {"1", "on", "true", "yes"},
            )
    return RedirectResponse("/", status_code=302)


@app.post("/channels/require-media")
async def save_require_media(
    request: Request,
    csrf: str = Form(""),
    next: str = Form(""),
):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    items = form.getlist("item")
    text_items = form.getlist("text_item")
    with db() as conn:
        for raw in items:
            text = str(raw or "").strip()
            if ":" not in text:
                continue
            chat_s, flag_s = text.rsplit(":", 1)
            try:
                chat_id = int(chat_s)
            except ValueError:
                continue
            if not get_channel(conn, chat_id):
                continue
            set_channel_require_media(conn, chat_id, flag_s in {"1", "on", "true", "yes"})
        for raw in text_items:
            text = str(raw or "").strip()
            if ":" not in text:
                continue
            chat_s, flag_s = text.rsplit(":", 1)
            try:
                chat_id = int(chat_s)
            except ValueError:
                continue
            if not get_channel(conn, chat_id):
                continue
            set_channel_require_text(conn, chat_id, flag_s in {"1", "on", "true", "yes"})
    dest = (next or "").strip()
    if dest.startswith("/channels/"):
        return RedirectResponse(dest, status_code=302)
    return RedirectResponse("/", status_code=302)


def _mark_tg_bot_sources(sources: list[dict]) -> list[dict]:
    connected: set[str] = set()
    if telegram_bot_tokens():
        try:
            payload = get_tg_bot_connections()
        except Exception:
            payload = {}
        for ch in payload.get("chats") or []:
            uname = (ch.get("username") or "").strip().lower()
            if uname:
                connected.add(uname)
    for s in sources:
        uname = telegram_username_from_url(s.get("url") or "") if s.get("kind") == "telegram" else None
        s["tg_bot_connected"] = bool(uname and uname.lower() in connected)
    return sources


def _enrich_posts(rows: list) -> list[dict]:
    import json

    out = []
    for r in rows:
        d = dict(r)
        try:
            media = json.loads(d.get("media_json") or "[]")
        except Exception:
            media = []
        types = []
        for m in media:
            t = (m.get("type") or "").lower()
            if t and t not in types:
                types.append(t)
        d["media_types"] = types
        d["media_count"] = len(media)
        out.append(d)
    return out


@app.get("/channels/{chat_id}/sources", response_class=HTMLResponse)
def sources_page(request: Request, chat_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    with db() as conn:
        channel = get_channel(conn, chat_id)
        if not channel:
            return RedirectResponse("/", status_code=302)
        sources = _mark_tg_bot_sources([dict(r) for r in list_sources(conn, chat_id)])
        recent = _enrich_posts(list_recent_posts(conn, chat_id, limit=25))
    return render(
        request,
        "sources.html",
        {
            "channel": dict(channel),
            "sources": sources,
            "recent_posts": recent,
            "csrf": request.session.get("csrf"),
            "error": None,
            "groq_ready": bool(
                (settings.groq_api_key or "").strip()
                or (settings.openclaw_api_key or "").strip()
            ),
        },
    )


@app.post("/channels/{chat_id}/sources")
async def add_source_route(
    request: Request,
    chat_id: int,
    url: str = Form(...),
    kind: str = Form("auto"),
    translate: str = Form(""),
    csrf: str = Form(""),
):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    url = url.strip()
    error = None
    do_translate = translate in {"1", "on", "true", "yes"}
    try:
        if do_translate and not (settings.groq_api_key or "").strip():
            raise ValueError(
                "Включён автоперевод, но GROQ_API_KEY пуст. "
                "Добавьте ключ с https://console.groq.com/keys в /var/max-repost/.env"
            )
        resolved_kind = kind if kind in {"telegram", "vk", "rss", "x"} else detect_kind(url)
        normalized = normalize_source_url(resolved_kind, url)
        with db() as conn:
            if not get_channel(conn, chat_id):
                return RedirectResponse("/", status_code=302)
            source_id = add_source(conn, chat_id, resolved_kind, normalized, translate=do_translate)
        bootstrap_source(source_id)
    except Exception as e:
        error = str(e)

    with db() as conn:
        channel = get_channel(conn, chat_id)
        sources = _mark_tg_bot_sources([dict(r) for r in list_sources(conn, chat_id)])
        recent = _enrich_posts(list_recent_posts(conn, chat_id, limit=25))
    return render(
        request,
        "sources.html",
        {
            "channel": dict(channel) if channel else {"chat_id": chat_id, "title": ""},
            "sources": sources,
            "recent_posts": recent,
            "csrf": request.session.get("csrf"),
            "error": error,
            "groq_ready": bool(
                (settings.groq_api_key or "").strip()
                or (settings.openclaw_api_key or "").strip()
            ),
        },
        status_code=400 if error else 200,
    )



@app.post("/sources/{source_id}/delete")
def delete_source_route(request: Request, source_id: int, csrf: str = Form(""), chat_id: int = Form(0)):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    with db() as conn:
        delete_source(conn, source_id)
    if chat_id:
        return RedirectResponse(f"/channels/{chat_id}/sources", status_code=302)
    return RedirectResponse("/", status_code=302)


@app.get("/seo", response_class=HTMLResponse)
def seo_page(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from seo.channel_config import load_seo_channels, reload_seo_channels
    from app.db import list_seo_active

    reload_seo_channels()
    configs = [c.__dict__ for c in load_seo_channels()]
    with db() as conn:
        rows = [dict(r) for r in list_seo_active(conn)]
    return render(
        request,
        "seo.html",
        {
            "rows": rows,
            "configs": configs,
            "csrf": request.session.get("csrf"),
            "message": request.query_params.get("msg") or "",
        },
    )


@app.post("/seo/force/{slug}")
def seo_force(request: Request, slug: str, csrf: str = Form("")):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from seo.cycle import run_seo_tick

    try:
        results = run_seo_tick(force_slug=slug)
        msg = str(results[0] if results else {"action": "empty"})
    except Exception as e:
        msg = f"error: {e}"
    from urllib.parse import quote

    return RedirectResponse(f"/seo?msg={quote(msg[:300])}", status_code=302)
