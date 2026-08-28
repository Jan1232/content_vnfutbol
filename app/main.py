from __future__ import annotations

import hashlib
import hmac
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.bootstrap_editorial_pyc  # noqa: E402 — pyc-only editorial modules

from app.config import get_settings
from app.db import (
    add_source,
    db,
    delete_source,
    get_channel,
    get_source,
    init_db,
    list_channels,
    list_recent_posts,
    list_simulated_posts,
    list_sources,
    set_channel_footer_link,
    set_channel_watermark,
)
from parsers.telegram import detect_kind
from workers.run import normalize_source_url, bootstrap_source, sync_chats
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
            sync_chats(client)
    except Exception as e:
        print(f"[admin] initial sync failed: {e}", flush=True)


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
    return render(
        request,
        "index.html",
        {"channels": channels, "csrf": request.session.get("csrf")},
    )


@app.post("/sync")
def sync_now(request: Request, csrf: str = Form("")):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    try:
        with MaxClient() as client:
            sync_chats(client)
    except Exception:
        pass
    return RedirectResponse("/", status_code=302)


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
        news_id = None
        ext = str(d.get("external_id") or "")
        if ext.startswith("editorial:"):
            try:
                news_id = int(ext.split(":", 1)[1])
            except ValueError:
                news_id = None
        if news_id is None:
            for m in media:
                if m.get("news_id"):
                    try:
                        news_id = int(m["news_id"])
                    except (TypeError, ValueError):
                        news_id = None
        d["news_id"] = news_id
        d["cover_url"] = f"/editorial/cover/{news_id}" if news_id else ""
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
        sources = [dict(r) for r in list_sources(conn, chat_id)]
        recent = _enrich_posts(list_recent_posts(conn, chat_id, limit=25))
        editorial_feed = _enrich_posts(list_simulated_posts(conn, chat_id, limit=80))
    return render(
        request,
        "sources.html",
        {
            "channel": dict(channel),
            "sources": sources,
            "recent_posts": recent,
            "editorial_feed": editorial_feed,
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
        sources = [dict(r) for r in list_sources(conn, chat_id)]
        recent = _enrich_posts(list_recent_posts(conn, chat_id, limit=25))
        editorial_feed = _enrich_posts(list_simulated_posts(conn, chat_id, limit=80))
    return render(
        request,
        "sources.html",
        {
            "channel": dict(channel) if channel else {"chat_id": chat_id, "title": ""},
            "sources": sources,
            "recent_posts": recent,
            "editorial_feed": editorial_feed,
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
        src = get_source(conn, source_id)
        if src and (src["kind"] or "") == "editorial":
            cid = int(src["chat_id"])
            return RedirectResponse(f"/channels/{cid}/sources", status_code=302)
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


@app.get("/editorial", response_class=HTMLResponse)
def editorial_page(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.channel_config import load_editorial_channels, reload_editorial_channels
    from editorial.cycle import _channel_enabled
    from editorial.fixtures import Match, is_significant
    from editorial.store import (
        get_channel_state,
        list_covers,
        list_recent_results,
        list_today_fixtures,
        recent_published,
        status_counts,
        top_stuck_errors,
    )
    from editorial.usage import usage_dashboard

    reload_editorial_channels()
    configs = load_editorial_channels(include_disabled=True)
    channels = []
    today_rows = list_today_fixtures()
    fixtures_today = []
    for row in today_rows:
        try:
            ko = datetime.fromisoformat(str(row.get("kickoff_utc") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        m = Match(
            provider_id=str(row.get("provider_id") or ""),
            competition=str(row.get("competition") or ""),
            home=str(row.get("home") or ""),
            away=str(row.get("away") or ""),
            home_ru=str(row.get("home") or ""),
            away_ru=str(row.get("away") or ""),
            kickoff_utc=ko,
            status=str(row.get("status") or ""),
            score_home=row.get("score_home"),
            score_away=row.get("score_away"),
            stage=row.get("stage"),
            is_national=bool(int(row.get("is_national") or 0)),
        )
        fixtures_today.append({**row, "significant": is_significant(m)})

    for cfg in configs:
        counts_rows = status_counts(cfg.slug)
        channels.append(
            {
                "slug": cfg.slug,
                "chat_id": cfg.chat_id,
                "brand": {"name": cfg.brand.name},
                "runtime_enabled": _channel_enabled(cfg),
                "dry_run": cfg.dry_run,
                "counts": {r["status"]: r["n"] for r in counts_rows},
                "stuck_errors": top_stuck_errors(cfg.slug, limit=5),
                "recent": recent_published(cfg.slug, limit=5),
                "covers": list_covers(cfg.slug, limit=24),
                "fixtures_today": fixtures_today,
                "recent_results": list_recent_results(cfg.slug, limit=8),
                **get_channel_state(cfg.slug),
            }
        )
    return render(
        request,
        "editorial.html",
        {
            "channels": channels,
            "usage": usage_dashboard(),
            "csrf": request.session.get("csrf"),
            "message": request.query_params.get("msg") or "",
        },
    )


@app.get("/editorial/queue", response_class=HTMLResponse)
def editorial_queue(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.store import list_moderation

    return render(
        request,
        "editorial_queue.html",
        {
            "items": list_moderation(),
            "csrf": request.session.get("csrf"),
            "message": request.query_params.get("msg") or "",
        },
    )


@app.get("/editorial/preview/{news_id}", response_class=HTMLResponse)
def editorial_preview(request: Request, news_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.store import get_news

    item = get_news(news_id)
    if not item:
        return RedirectResponse("/editorial/queue", status_code=302)
    cover_url = f"/editorial/cover/{news_id}" if item.get("cover_path") else ""
    return render(
        request,
        "editorial_preview.html",
        {"item": item, "cover_url": cover_url, "csrf": request.session.get("csrf")},
    )


@app.get("/editorial/cover/{news_id}")
def editorial_cover(request: Request, news_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from pathlib import Path

    from editorial.store import get_news

    item = get_news(news_id)
    if not item or not item.get("cover_path"):
        return RedirectResponse("/editorial/queue", status_code=302)
    path = Path(item["cover_path"])
    if not path.is_file():
        return RedirectResponse("/editorial/queue", status_code=302)
    return FileResponse(path, media_type="image/png")


@app.post("/editorial/force/{slug}")
def editorial_force(request: Request, slug: str, csrf: str = Form("")):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from urllib.parse import quote

    from editorial.cycle import run_editorial_tick

    try:
        results = run_editorial_tick(force_slug=slug)
        msg = str(results[0] if results else {"action": "empty"})
    except Exception as e:
        msg = f"error: {e}"
    return RedirectResponse(f"/editorial?msg={quote(msg[:400])}", status_code=302)


def _force_fixtures_tick(slug: str, kind: str) -> str:
    from editorial.channel_config import get_channel
    from editorial.matchday import matchday_tick
    from editorial.results import results_tick
    from app.db import init_db

    init_db()
    cfg = get_channel(slug)
    if not cfg:
        return f"no channel {slug}"
    client: MaxClient | None = None
    ctx = None
    if not cfg.dry_run:
        ctx = MaxClient()
        client = ctx.__enter__()
    try:
        if kind == "matchday":
            res = matchday_tick(cfg, client, force=True)
        else:
            res = results_tick(cfg, client, force=True)
        return str(res)[:400]
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)


@app.post("/editorial/matchday/{slug}")
def editorial_matchday_force(request: Request, slug: str, csrf: str = Form("")):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from urllib.parse import quote

    try:
        msg = _force_fixtures_tick(slug, "matchday")
    except Exception as e:
        msg = f"error: {e}"
    return RedirectResponse(f"/editorial?msg={quote(msg[:400])}", status_code=302)


@app.post("/editorial/results/{slug}")
def editorial_results_force(request: Request, slug: str, csrf: str = Form("")):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from urllib.parse import quote

    try:
        msg = _force_fixtures_tick(slug, "results")
    except Exception as e:
        msg = f"error: {e}"
    return RedirectResponse(f"/editorial?msg={quote(msg[:400])}", status_code=302)


@app.post("/editorial/toggle/{slug}")
def editorial_toggle(request: Request, slug: str, csrf: str = Form(""), enabled: str = Form("1")):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from editorial.cycle import set_channel_enabled

    set_channel_enabled(slug, enabled in {"1", "true", "on", "yes"})
    return RedirectResponse("/editorial", status_code=302)


@app.get("/editorial/label-photos", response_class=HTMLResponse)
def editorial_label_photos(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.imagery_label import (
        item_by_id,
        next_unlabeled,
        pool_items,
        progress,
        view_item,
        write_comparison_and_rules,
    )

    item_id = (request.query_params.get("id") or "").strip()
    item = item_by_id(item_id) if item_id else next_unlabeled()
    prog = progress()
    if item is None:
        comparison = write_comparison_and_rules() if prog["total"] else {
            "n": 0,
            "agree_pct": 0,
            "accept_other": 0,
            "none": 0,
            "files": {"model": "", "human": ""},
        }
        first = (pool_items() or [{}])[0].get("id") or ""
        return render(
            request,
            "editorial_label_photos.html",
            {
                "done": True,
                "progress": prog,
                "comparison": comparison,
                "first_id": first,
                "csrf": request.session.get("csrf"),
            },
        )
    return render(
        request,
        "editorial_label_photos.html",
        {
            "done": False,
            "view": view_item(item),
            "csrf": request.session.get("csrf"),
        },
    )


@app.get("/editorial/label-photos/summary", response_class=HTMLResponse)
def editorial_label_photos_summary(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.imagery_label import write_comparison_and_rules

    comparison = write_comparison_and_rules()
    return render(
        request,
        "editorial_label_summary.html",
        {"comparison": comparison, "csrf": request.session.get("csrf")},
    )


@app.post("/editorial/label-photos/{item_id}")
def editorial_label_photos_save(
    request: Request,
    item_id: str,
    csrf: str = Form(""),
    decision: str = Form(""),
    chosen_idx: str = Form(""),
    note: str = Form(""),
    better_query: str = Form(""),
):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from editorial.imagery_label import apply_decision, item_by_id, next_unlabeled, save_label

    item = item_by_id(item_id)
    if not item:
        return RedirectResponse("/editorial/label-photos", status_code=302)
    idx: int | None = None
    raw = (chosen_idx or "").strip()
    if raw not in {"", "-1"}:
        try:
            idx = int(raw)
        except ValueError:
            idx = None
    rec = apply_decision(
        item,
        decision="none" if decision == "none" else "accept",
        chosen_idx=idx,
        note=note,
        better_query=better_query,
    )
    save_label(rec)
    nxt = next_unlabeled(after_id=item_id)
    if nxt:
        return RedirectResponse(f"/editorial/label-photos?id={nxt['id']}", status_code=302)
    return RedirectResponse("/editorial/label-photos/summary", status_code=302)


@app.get("/editorial/label-photos/card/{item_id}")
def editorial_label_card_preview(request: Request, item_id: str):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.imagery_label import item_by_id, photo_file_for_item, preview_card_html

    item = item_by_id(item_id)
    if not item:
        return HTMLResponse("not found", status_code=404)
    photo_url = f"/editorial/label-photos/fitted/{item_id}" if photo_file_for_item(item) else ""
    return HTMLResponse(preview_card_html(item, photo_url=photo_url))


@app.get("/editorial/label-photos/tpl/{filename}")
def editorial_label_tpl_asset(request: Request, filename: str):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.render import tpl_asset_path

    path = tpl_asset_path(filename)
    if not path:
        return HTMLResponse("not found", status_code=404)
    media = "image/png" if path.suffix.lower() == ".png" else "font/ttf"
    return FileResponse(
        path,
        media_type=media,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/editorial/label-photos/fitted/{item_id}")
def editorial_label_fitted_pick(request: Request, item_id: str):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.imagery_label import ensure_fitted, item_by_id

    item = item_by_id(item_id)
    path = ensure_fitted(item, None) if item else None
    if not path:
        return RedirectResponse("/editorial/label-photos", status_code=302)
    return FileResponse(path)


@app.get("/editorial/label-photos/fitted/{item_id}/{idx}")
def editorial_label_fitted_cand(request: Request, item_id: str, idx: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.imagery_label import ensure_fitted, item_by_id

    item = item_by_id(item_id)
    path = ensure_fitted(item, idx) if item else None
    if not path:
        return RedirectResponse("/editorial/label-photos", status_code=302)
    return FileResponse(path)


@app.get("/editorial/label-photos/photo/{item_id}")
def editorial_label_photo_file(request: Request, item_id: str):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.imagery_label import item_by_id, photo_file_for_item

    item = item_by_id(item_id)
    path = photo_file_for_item(item) if item else None
    if not path:
        return RedirectResponse("/editorial/label-photos", status_code=302)
    return FileResponse(path)


@app.get("/editorial/label-photos/cand/{item_id}/{idx}")
def editorial_label_cand_file(request: Request, item_id: str, idx: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.imagery_label import cand_file, item_by_id

    item = item_by_id(item_id)
    if not item:
        return RedirectResponse("/editorial/label-photos", status_code=302)
    cand = None
    for row in (item.get("model") or {}).get("vision") or []:
        try:
            if int(row.get("idx")) == int(idx):
                cand = row
                break
        except (TypeError, ValueError):
            continue
    path = cand_file(cand) if cand else None
    if not path:
        return RedirectResponse("/editorial/label-photos", status_code=302)
    return FileResponse(path)


@app.get("/editorial/label-day", response_class=HTMLResponse)
def editorial_label_day_index(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.day_sim_label import list_days

    days = list_days()
    if len(days) == 1:
        return RedirectResponse(f"/editorial/label-day/{days[0]}", status_code=302)
    return render(
        request,
        "editorial_label_day_index.html",
        {"days": days, "csrf": request.session.get("csrf")},
    )


@app.get("/editorial/label-day/{day}", response_class=HTMLResponse)
def editorial_label_day(request: Request, day: str):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.day_sim_label import (
        item_by_id,
        next_unlabeled,
        pool_items,
        pool_path,
        progress,
        view_item,
        write_summary,
    )

    if not pool_path(day).is_file():
        return RedirectResponse("/editorial/label-day", status_code=302)
    item_id = (request.query_params.get("id") or "").strip()
    item = item_by_id(day, item_id) if item_id else next_unlabeled(day)
    prog = progress(day)
    if item is None:
        summary = write_summary(day) if prog["total"] else {
            "n": 0,
            "accept": 0,
            "reject": 0,
            "should_not_pool": 0,
        }
        first = (pool_items(day) or [{}])[0].get("id") or ""
        return render(
            request,
            "editorial_label_day.html",
            {
                "done": True,
                "day": day,
                "progress": prog,
                "summary": summary,
                "first_id": first,
                "csrf": request.session.get("csrf"),
            },
        )
    return render(
        request,
        "editorial_label_day.html",
        {
            "done": False,
            "day": day,
            "view": view_item(day, item),
            "csrf": request.session.get("csrf"),
        },
    )


@app.get("/editorial/label-day/{day}/summary", response_class=HTMLResponse)
def editorial_label_day_summary(request: Request, day: str):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.day_sim_label import write_summary

    summary = write_summary(day)
    return render(
        request,
        "editorial_label_day_summary.html",
        {"day": day, "summary": summary, "csrf": request.session.get("csrf")},
    )


@app.post("/editorial/label-day/{day}/{item_id}")
def editorial_label_day_save(
    request: Request,
    day: str,
    item_id: str,
    csrf: str = Form(""),
    decision: str = Form(""),
    comment: str = Form(""),
):
    if not require_auth(request) or not _csrf_ok(request, csrf):
        return RedirectResponse("/login", status_code=302)
    from editorial.day_sim_label import apply_decision, item_by_id, next_unlabeled, save_label

    item = item_by_id(day, item_id)
    if not item:
        return RedirectResponse(f"/editorial/label-day/{day}", status_code=302)
    rec = apply_decision(item, decision=decision, comment=comment)
    save_label(day, rec)
    nxt = next_unlabeled(day, after_id=item_id)
    if nxt:
        return RedirectResponse(f"/editorial/label-day/{day}?id={nxt['id']}", status_code=302)
    return RedirectResponse(f"/editorial/label-day/{day}/summary", status_code=302)


@app.get("/editorial/label-day/{day}/cover/{item_id}")
def editorial_label_day_cover(request: Request, day: str, item_id: str):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=302)
    from editorial.day_sim_label import cover_file, item_by_id

    item = item_by_id(day, item_id)
    path = cover_file(day, item) if item else None
    if not path:
        return HTMLResponse("not found", status_code=404)
    return FileResponse(path, media_type="image/png")
