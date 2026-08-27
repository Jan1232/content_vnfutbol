"""SEO beacon cycle: pick match → rename channel → one promo post."""

from __future__ import annotations

import html as html_lib
import time
from datetime import datetime, timezone
from typing import Any

from app.db import (
    clear_seo_active,
    db,
    get_seo_active,
    init_db,
    set_seo_error,
    upsert_seo_active,
)
from app.max_api import MaxApiError, MaxClient
from seo.channel_config import SeoChannelConfig, load_seo_channels, reload_seo_channels
from seo.content import generate_cover_image, generate_post_text, resolve_competition_label
from seo.fixtures import (
    FootballDataError,
    Match,
    fetch_matches_for_competitions,
    match_needs_rotation,
    pick_national_priority_match,
    pick_top_match,
)
from seo.titles import build_seo_title


def _extract_message_id(resp: dict[str, Any] | None) -> str:
    if not isinstance(resp, dict):
        return ""
    # common shapes: {message: {body: {mid}}}, {mid}, {message_id}
    for key in ("mid", "message_id", "id"):
        if isinstance(resp.get(key), str) and resp[key]:
            return resp[key]
    msg = resp.get("message")
    if isinstance(msg, dict):
        body = msg.get("body") if isinstance(msg.get("body"), dict) else msg
        if isinstance(body, dict):
            for key in ("mid", "message_id", "id"):
                if isinstance(body.get(key), str) and body[key]:
                    return body[key]
        for key in ("mid", "message_id", "id"):
            if isinstance(msg.get(key), str) and msg[key]:
                return msg[key]
    return ""


def rotate_channel(
    client: MaxClient,
    cfg: SeoChannelConfig,
    match: Match,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Delete old post, rename channel, publish new SEO post."""
    title = build_seo_title(
        match.home_team,
        match.away_team,
        suffix=cfg.title_suffix,
    )

    with db() as conn:
        active = get_seo_active(conn, cfg.chat_id)
        old_mid = (active["message_id"] if active else "") or ""
        old_match = (active["match_id"] if active else "") or ""

    if not force and old_match and str(old_match) == str(match.match_id):
        # same match — maybe refresh status only
        with db() as conn:
            upsert_seo_active(
                conn,
                chat_id=cfg.chat_id,
                slug=cfg.slug,
                match_id=match.match_id,
                competition=match.competition,
                home_team=match.home_team,
                away_team=match.away_team,
                home_team_ru="",
                away_team_ru="",
                kickoff_at=match.utc_date.timestamp(),
                status=match.status,
                channel_title=title,
                message_id=old_mid,
                post_text=(active["post_text"] if active else "") or "",
            )
        return {"action": "noop", "match_id": match.match_id, "title": title}

    # 1) delete previous post
    if old_mid:
        try:
            client.delete_message(old_mid)
            print(f"[seo] deleted old message {old_mid}", flush=True)
            time.sleep(0.6)
        except MaxApiError as e:
            print(f"[seo] delete old post fail: {e}", flush=True)

    # 2) rename without notify
    try:
        client.patch_chat(
            cfg.chat_id,
            title=title,
            notify=bool(cfg.notify_title_change),
        )
        print(f"[seo] renamed chat {cfg.chat_id} → {title!r}", flush=True)
    except MaxApiError as e:
        with db() as conn:
            set_seo_error(conn, cfg.chat_id, f"rename: {e}")
        raise

    # 3) generate content
    comp_label = resolve_competition_label(
        match,
        label=cfg.label_for(match.competition, qualifying=False),
        label_qual=cfg.label_for(match.competition, qualifying=True),
    )
    text, home_ru, away_ru = generate_post_text(
        match,
        target_community=cfg.target_community,
        competition_label=cfg.label_for(match.competition, qualifying=False),
        competition_label_qual=cfg.label_for(match.competition, qualifying=True),
        polish=True,
    )
    image_bytes = generate_cover_image(
        match,
        home_ru=home_ru,
        away_ru=away_ru,
        competition_label=comp_label,
    )

    attachments: list[dict[str, Any]] = []
    if image_bytes:
        att = client.upload_image_bytes(image_bytes, filename="cover.jpg", ctype="image/jpeg")
        if att:
            attachments.append(att)
            time.sleep(0.4)

    # CTA button to main community
    attachments.append(
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {
                            "type": "link",
                            "text": "Футбол сегодня →",
                            "url": cfg.target_community,
                        }
                    ]
                ]
            },
        }
    )

    href = html_lib.escape(cfg.target_community, quote=True)
    # text already contains URL; send as plain
    try:
        resp = client.send_message(
            chat_id=cfg.chat_id,
            text=text,
            attachments=attachments or None,
            notify=True,
            disable_link_preview=False,
        )
    except MaxApiError as e:
        with db() as conn:
            set_seo_error(conn, cfg.chat_id, f"publish: {e}")
        raise

    mid = _extract_message_id(resp)
    if cfg.pin_post and mid:
        try:
            client.pin_message(cfg.chat_id, mid, notify=False)
            print(f"[seo] pinned {mid}", flush=True)
        except MaxApiError as e:
            print(f"[seo] pin fail: {e}", flush=True)

    with db() as conn:
        upsert_seo_active(
            conn,
            chat_id=cfg.chat_id,
            slug=cfg.slug,
            match_id=match.match_id,
            competition=match.competition,
            home_team=match.home_team,
            away_team=match.away_team,
            home_team_ru=home_ru,
            away_team_ru=away_ru,
            kickoff_at=match.utc_date.timestamp(),
            status=match.status,
            channel_title=title,
            message_id=mid,
            post_text=text,
            last_error="",
        )

    return {
        "action": "rotated",
        "match_id": match.match_id,
        "title": title,
        "message_id": mid,
        "home": home_ru,
        "away": away_ru,
    }


def process_channel(client: MaxClient, cfg: SeoChannelConfig, *, force: bool = False) -> dict[str, Any]:
    try:
        matches = fetch_matches_for_competitions(
            cfg.competitions,
            horizon_days=cfg.horizon_days,
            providers=cfg.providers,
        )
    except FootballDataError as e:
        with db() as conn:
            set_seo_error(conn, cfg.chat_id, str(e))
        return {"action": "error", "error": str(e)}

    if cfg.pick_mode == "national_priority":
        picked = pick_national_priority_match(
            matches,
            horizon_days=cfg.horizon_days,
            post_match_grace_min=cfg.post_match_grace_min,
            priority_teams=cfg.priority_teams or None,
            major_competitions=cfg.major_competitions or ("WC", "EC", "CA"),
            priority_window_days=cfg.priority_window_days or 4,
            competition_label=cfg.competition_label or cfg.slug,
            use_ai_for_major=True,
        )
    else:
        picked = pick_top_match(
            matches,
            horizon_days=cfg.horizon_days,
            post_match_grace_min=cfg.post_match_grace_min,
            competition_label=cfg.competition_label_qual
            or cfg.competition_label
            or cfg.slug,
            use_ai=True,
            hype_window_days=cfg.hype_window_days or 4,
        )

    with db() as conn:
        active = get_seo_active(conn, cfg.chat_id)
        active_id = (active["match_id"] if active else "") or ""
        active_ko = float(active["kickoff_at"]) if active and active["kickoff_at"] else None

    if picked is None:
        print(f"[seo] {cfg.slug}: no upcoming matches", flush=True)
        # сброс тестового/устаревшего состояния + idle title
        return apply_idle(client, cfg)

    if not force and not match_needs_rotation(active_id, active_ko, picked):
        # refresh status
        with db() as conn:
            if active:
                upsert_seo_active(
                    conn,
                    chat_id=cfg.chat_id,
                    slug=cfg.slug,
                    match_id=picked.match_id,
                    competition=picked.competition,
                    home_team=picked.home_team,
                    away_team=picked.away_team,
                    home_team_ru=(active["home_team_ru"] if active else "") or "",
                    away_team_ru=(active["away_team_ru"] if active else "") or "",
                    kickoff_at=picked.utc_date.timestamp(),
                    status=picked.status,
                    channel_title=(active["channel_title"] if active else "") or "",
                    message_id=(active["message_id"] if active else "") or "",
                    post_text=(active["post_text"] if active else "") or "",
                )
        return {
            "action": "hold",
            "match_id": picked.match_id,
            "status": picked.status,
            "kickoff": picked.kickoff_msk.isoformat(),
        }

    return rotate_channel(client, cfg, picked, force=force)


def apply_idle(client: MaxClient, cfg: SeoChannelConfig) -> dict[str, Any]:
    """Удалить старый SEO-пост и поставить idle_title, если задан."""
    with db() as conn:
        active = get_seo_active(conn, cfg.chat_id)
        old_mid = (active["message_id"] if active else "") or ""

    if old_mid:
        try:
            client.delete_message(old_mid)
            print(f"[seo] idle: deleted {old_mid}", flush=True)
            time.sleep(0.5)
        except MaxApiError as e:
            print(f"[seo] idle delete fail: {e}", flush=True)

    idle = (cfg.idle_title or "").strip()
    if idle:
        try:
            current = (client.get_chat(cfg.chat_id).get("title") or "").strip()
        except MaxApiError:
            current = ""
        if current != idle:
            try:
                client.patch_chat(cfg.chat_id, title=idle[:200], notify=False)
                print(f"[seo] idle title → {idle!r}", flush=True)
            except MaxApiError as e:
                print(f"[seo] idle rename fail: {e}", flush=True)

    with db() as conn:
        clear_seo_active(conn, cfg.chat_id)

    return {"action": "idle", "reason": "no_matches", "title": idle}


def run_seo_tick(*, force_slug: str | None = None) -> list[dict[str, Any]]:
    init_db()
    reload_seo_channels()
    configs = load_seo_channels()
    results: list[dict[str, Any]] = []
    with MaxClient() as client:
        for cfg in configs:
            if force_slug and cfg.slug != force_slug:
                continue
            print(f"[seo] tick {cfg.slug} chat={cfg.chat_id}", flush=True)
            try:
                res = process_channel(client, cfg, force=bool(force_slug))
            except Exception as e:
                print(f"[seo] {cfg.slug} error: {e}", flush=True)
                with db() as conn:
                    set_seo_error(conn, cfg.chat_id, str(e))
                res = {"action": "error", "slug": cfg.slug, "error": str(e)}
            res["slug"] = cfg.slug
            results.append(res)
            time.sleep(1.0)
    return results
