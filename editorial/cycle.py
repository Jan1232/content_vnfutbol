"""Editorial tick: discovery → gates → render → cadence publish."""

from __future__ import annotations

import json
import re
from typing import Any

from app.config import get_settings
from app.db import db, get_meta, init_db, set_meta
from app.max_api import MaxApiError, MaxClient
from editorial.caption import generate as generate_caption
from editorial.channel_config import (
    EditorialChannelConfig,
    brand_render_context,
    load_editorial_channels,
    reload_editorial_channels,
)
from editorial.dedup import filter_new
from editorial.discovery import fetch_fresh_news
from editorial.editor import facts_from_item, rewrite as rewrite_item
from editorial.match_enrich import enrich_news_item, enrich_row
from editorial.factcheck import verify
from editorial.fifa_ranking import refresh_top100, seed_from_yaml_if_empty
from editorial.imagery import ensure_template_crop, find_photo
from editorial.models import NewsItem, utcnow_iso
from editorial.openai_client import usage_scope
from editorial.publisher import publish
from editorial.render import BADGE_FOR_EVENT, render_post
from editorial.scheduler import (
    ensure_slot_initialized,
    is_priority,
    mark_normal_published,
    mark_priority_published,
    pick_best,
    slot_ready,
)
from editorial.matchday import matchday_tick
from editorial.results import results_tick
from editorial.pick import human_factor_share, pick as editorial_pick, story_throttle_ok
from editorial.policy import HUMAN_FACTOR_WINDOW
from editorial.store import (
    bump_retry,
    cluster_published,
    count_meme_source_today,
    expire_stale,
    get_news,
    insert_news,
    list_open_news,
    list_ready,
    recent_published,
    update_news,
)
from editorial.topic_gate import check as topic_check, cluster_id_for, classify_event_rules

_SCORE_RE = re.compile(r"(\d+)\s*[:\-–]\s*(\d+)")


def _channel_enabled(cfg: EditorialChannelConfig) -> bool:
    with db() as conn:
        ov = get_meta(conn, f"editorial_enabled:{cfg.slug}", "")
    if ov == "0":
        return False
    if ov == "1":
        return True
    return bool(cfg.enabled)


def set_channel_enabled(slug: str, enabled: bool) -> None:
    with db() as conn:
        set_meta(conn, f"editorial_enabled:{slug}", "1" if enabled else "0")


def _entities_json(item: NewsItem) -> str:
    return json.dumps(item.entities or {}, ensure_ascii=False)


def _entities_json_with_raw(item: NewsItem) -> str:
    entities = dict(item.entities or {})
    raw = item.raw or {}
    if raw.get("media"):
        entities["raw"] = {"media": raw.get("media")}
    return json.dumps(entities, ensure_ascii=False)


def _teams_json(item: NewsItem) -> str:
    teams = (item.entities or {}).get("teams") or []
    return json.dumps(teams, ensure_ascii=False)


def ingest_channel(channel: EditorialChannelConfig) -> int:
    items = fetch_fresh_news(channel)
    fresh = filter_new(channel.slug, items)
    inserted = 0
    for item in fresh:
        if item.event_type == "rumor" and not channel.allow_rumors:
            news_id = insert_news(
                {
                    "channel_slug": channel.slug,
                    "external_id": item.external_id,
                    "cluster_id": item.cluster_id or cluster_id_for(item),
                    "source": item.source,
                    "url": item.url,
                    "event_type": "rumor",
                    "competition": item.competition,
                    "is_national": 1 if item.entities.get("is_national") else 0,
                    "teams_json": _teams_json(item),
                    "title": item.title,
                    "body": item.body,
                    "lang": item.lang,
                    "source_published_at": item.published_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "entities_json": _entities_json(item),
                    "status": "skipped",
                }
            )
            if news_id:
                update_news(news_id, last_error="rumor disabled", status="skipped")
            continue
        # мем-источник: event_type всегда meme/lifestyle — не режем по тексту
        if (item.entities or {}).get("meme_source") and (item.event_type or "") not in {
            "lifestyle",
            "meme",
        }:
            continue
        allowed_types = set(channel.event_types or ())
        if allowed_types and item.event_type not in allowed_types and item.event_type != "other":
            # still ingest "other" to let LLM classify later
            pass
        enrich_news_item(item, fetch_article=False)
        nid = insert_news(
            {
                "channel_slug": channel.slug,
                "external_id": item.external_id,
                "cluster_id": item.cluster_id or cluster_id_for(item),
                "source": item.source,
                "url": item.url,
                "event_type": item.event_type,
                "competition": item.competition,
                "is_national": 1 if (item.entities or {}).get("is_national") else 0,
                "teams_json": _teams_json(item),
                "title": item.title,
                "body": item.body,
                "lang": item.lang,
                "source_published_at": item.published_at.strftime("%Y-%m-%d %H:%M:%S"),
                "entities_json": _entities_json_with_raw(item),
                "status": "new",
                "post_kind": str((item.raw or {}).get("post_kind") or ""),
                "media_type": str((item.raw or {}).get("media_type") or ""),
                "meme_source": 1 if (item.entities or {}).get("meme_source") else 0,
            }
        )
        if nid:
            inserted += 1
    return inserted


def _as_news_item(row: dict[str, Any]) -> NewsItem:
    from datetime import datetime, timezone

    try:
        entities = json.loads(row.get("entities_json") or "{}")
    except Exception:
        entities = {}
    published_raw = row.get("source_published_at") or ""
    try:
        published = datetime.fromisoformat(str(published_raw).replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
    except Exception:
        published = datetime.now(timezone.utc)
    return NewsItem(
        external_id=row.get("external_id") or "",
        source=row.get("source") or "",
        url=row.get("url") or "",
        title=row.get("title") or "",
        body=row.get("body") or "",
        lang=row.get("lang") or "",
        published_at=published,
        entities=entities,
        event_type=row.get("event_type") or "other",
        competition=row.get("competition") or "",
        is_national=bool(int(row.get("is_national") or 0)),
        cluster_id=row.get("cluster_id") or "",
    )


def _score_key(row: dict[str, Any]) -> str:
    from editorial.match_enrich import parse_score_from_text, parse_score_from_url

    blob = f"{row.get('title') or ''} {row.get('body') or ''}"
    m = _SCORE_RE.search(blob)
    if not m:
        url_score = parse_score_from_url(str(row.get("url") or ""))
        if url_score:
            a, b = url_score
            return f"{min(a, b)}-{max(a, b)}"
        parsed = parse_score_from_text(blob)
        if parsed:
            a, b = parsed
            return f"{min(a, b)}-{max(a, b)}"
        return "final"
    a, b = int(m.group(1)), int(m.group(2))
    return f"{min(a, b)}-{max(a, b)}"


def advance_item(channel: EditorialChannelConfig, news_id: int) -> str:
    settings = get_settings()
    max_retry = int(settings.editorial_max_retry or 3)
    row = get_news(news_id)
    if not row:
        return "missing"

    status = row.get("status") or "new"
    try:
        with usage_scope(news_id=news_id):
            return _advance_unlocked(channel, news_id, row, status, max_retry=max_retry)
    except Exception as e:
        print(f"[editorial] item {news_id} fail: {e}", flush=True)
        return bump_retry(news_id, str(e), max_retry=max_retry)


def _advance_unlocked(
    channel: EditorialChannelConfig,
    news_id: int,
    row: dict[str, Any],
    status: str,
    *,
    max_retry: int,
) -> str:
    try:
        if status == "deferred":
            item = _as_news_item(row)
            ok_story, story_reason = story_throttle_ok(channel.slug, item)
            if ok_story:
                update_news(news_id, status="verifying", last_error="")
                status = "verifying"
                row = get_news(news_id) or row
            else:
                update_news(news_id, last_error=(f"story: {story_reason}")[:800])
                return "deferred"
        if status == "new":
            status = _step_topic(channel, row)
            row = get_news(news_id) or row
        if status == "verifying":
            status = _step_factcheck(channel, row)
            row = get_news(news_id) or row
        if status == "confirmed":
            update_news(news_id, status="editing")
            status = "editing"
            row = get_news(news_id) or row
        if status == "editing":
            status = _step_edit(row)
            row = get_news(news_id) or row
        if status == "imaging":
            status = _step_image(channel, row)
            row = get_news(news_id) or row
        if status == "captioning":
            status = _step_caption(row)
            row = get_news(news_id) or row
        if status == "rendering":
            status = _step_render(channel, row)
    except Exception as e:
        print(f"[editorial] item {news_id} fail: {e}", flush=True)
        return bump_retry(news_id, str(e), max_retry=max_retry)
    return get_news(news_id).get("status") if get_news(news_id) else status


def _step_topic(channel: EditorialChannelConfig, row: dict[str, Any]) -> str:
    news_id = int(row["id"])
    if int(row.get("meme_source") or 0):
        item = _as_news_item(row)
        cluster_id = item.cluster_id or cluster_id_for(item)
        update_news(
            news_id,
            status="verifying",
            topic_status="football",
            event_type=row.get("event_type") or "meme",
            cluster_id=cluster_id,
            last_error="",
        )
        return "verifying"
    item = _as_news_item(row)
    ok, reason, payload = topic_check(item, extra_teams=channel.always_priority_teams)
    news_id = int(row["id"])
    subtype = (payload or {}).get("subtype")
    event_type = row.get("event_type") or item.event_type
    if event_type in {"", "other"}:
        event_type = classify_event_rules(f"{item.title}\n{item.body}")
        if payload.get("subtype") in {"match", "transfer", "injury"}:
            mapping = {"match": "match_result", "transfer": "transfer", "injury": "injury"}
            event_type = mapping.get(str(subtype), event_type)
    if not ok:
        update_news(
            news_id,
            status="off_topic",
            topic_status="off_topic",
            event_type=event_type,
            last_error=reason[:800],
        )
        return "off_topic"
    allowed = set(channel.event_types or ())
    if allowed and event_type not in allowed and event_type != "other":
        update_news(
            news_id,
            status="skipped",
            topic_status="football",
            event_type=event_type,
            last_error=f"event_type {event_type} not in channel filter",
        )
        return "skipped"

    item.event_type = event_type
    cluster_id = item.cluster_id or cluster_id_for(item)
    item.cluster_id = cluster_id
    already = cluster_published(cluster_id, str(channel.chat_id))
    hf_ratio = human_factor_share(recent_published(channel.slug, limit=HUMAN_FACTOR_WINDOW))
    verdict = editorial_pick(
        item,
        allow_rumors=channel.allow_rumors,
        cluster_already_published=already,
        human_factor_ratio=hf_ratio,
    )
    entities = dict(item.entities or {})
    entities["pick"] = verdict.as_dict()
    entities_json = json.dumps(entities, ensure_ascii=False)
    if not verdict.take:
        update_news(
            news_id,
            status="filtered",
            topic_status="football",
            event_type=event_type,
            cluster_id=cluster_id,
            entities_json=entities_json,
            last_error=(f"filter: {verdict.reason}")[:800],
        )
        return "filtered"
    ok_story, story_reason = story_throttle_ok(channel.slug, item)
    if not ok_story:
        entities["story"] = {"reason": story_reason[:400]}
        entities_json = json.dumps(entities, ensure_ascii=False)
        update_news(
            news_id,
            status="deferred",
            topic_status="football",
            event_type=event_type,
            cluster_id=cluster_id,
            entities_json=entities_json,
            last_error=(f"story: {story_reason}")[:800],
        )
        return "deferred"
    update_news(
        news_id,
        status="verifying",
        topic_status="football",
        event_type=event_type,
        cluster_id=cluster_id,
        entities_json=entities_json,
        last_error="",
    )
    return "verifying"


def _step_factcheck(channel: EditorialChannelConfig, row: dict[str, Any]) -> str:
    news_id = int(row["id"])
    if int(row.get("meme_source") or 0):
        item = _as_news_item(row)
        update_news(
            news_id,
            status="confirmed",
            cluster_id=item.cluster_id or cluster_id_for(item),
            factcheck_status="skipped",
            factcheck_conf=1.0,
            factcheck_sources=0,
            factcheck_reason="meme source",
            last_error="",
        )
        return "confirmed"
    settings = get_settings()
    if not bool(getattr(settings, "editorial_factcheck_enabled", False)):
        item = _as_news_item(row)
        update_news(
            news_id,
            status="confirmed",
            cluster_id=item.cluster_id or cluster_id_for(item),
            factcheck_status="skipped",
            factcheck_conf=1.0,
            factcheck_sources=1,
            factcheck_reason="factcheck disabled",
            score_key=_score_key(row) if (row.get("event_type") == "match_result") else "",
            last_error="",
        )
        return "confirmed"
    item = _as_news_item(row)
    verdict = verify(item, min_sources=channel.factcheck_min_sources)
    common = {
        "cluster_id": verdict.cluster_id,
        "factcheck_status": verdict.status.lower(),
        "factcheck_conf": verdict.confidence,
        "factcheck_sources": verdict.unique_domains,
        "factcheck_reason": verdict.reason[:800],
        "score_key": _score_key(row) if (row.get("event_type") == "match_result") else "",
    }
    if verdict.status == "CONFIRMED":
        update_news(news_id, status="confirmed", **common)
        return "confirmed"
    if verdict.status == "REJECTED":
        update_news(news_id, status="rejected", **common)
        return "rejected"
    update_news(news_id, status="held", **common, last_error="uncertain factcheck")
    return "held"


def _step_edit(row: dict[str, Any]) -> str:
    news_id = int(row["id"])
    if int(row.get("meme_source") or 0) and not (row.get("media_path") or "").strip():
        try:
            from editorial.tg_media import download_item_media

            path = download_item_media(row)
            update_news(news_id, media_path=path)
            row = get_news(news_id) or row
        except Exception as e:
            update_news(news_id, status="held", last_error=f"media: {e}"[:800])
            return "held"

    from editorial.meme_text import is_meme_row, prepare_meme_post

    if is_meme_row(row):
        result = prepare_meme_post(row)
        post_text = (result.get("post_text") or "").strip()
        if not post_text:
            update_news(news_id, status="held", last_error="пустой текст мема")
            return "held"
        media_type = str(row.get("media_type") or "")
        common = {
            "post_text": post_text,
            "headline": "",
            "caption": "",
            "caption_line1": "",
            "caption_line2": "",
            "emoji_lead": "",
            "last_error": "",
        }
        if media_type == "video":
            update_news(news_id, status="ready", post_kind="video", **common)
            return "ready"
        update_news(news_id, status="imaging", **common)
        return "imaging"
    else:
        enriched, enrich_meta = enrich_row(row, fetch_article=True)
        if enriched.get("body") != (row.get("body") or "") or enrich_meta.get("enriched"):
            update_news(
                news_id,
                body=enriched.get("body") or "",
                entities_json=enriched.get("entities_json") or row.get("entities_json"),
                last_error="",
            )
            row = get_news(news_id) or enriched
        result = rewrite_item(row, facts=facts_from_item(row))
        post_text = result["post_text"]
        settings = get_settings()
        if str(getattr(settings, "profanity_filter", "") or "").lower() == "strict":
            from editorial.profanity import profanity_ok, replace_profanity

            cleaned = replace_profanity(post_text)
            ok, why = profanity_ok(cleaned)
            if not ok:
                update_news(
                    news_id,
                    status="held",
                    post_text=post_text,
                    headline=result["headline"],
                    emoji_lead=result["emoji_lead"],
                    last_error=f"profanity: {why}",
                )
                return "held"
            post_text = cleaned
    media_type = str(row.get("media_type") or "")
    if media_type == "video":
        update_news(
            news_id,
            status="ready",
            post_text=post_text,
            headline=result["headline"],
            emoji_lead=result["emoji_lead"],
            post_kind="video",
            last_error="",
        )
        return "ready"
    update_news(
        news_id,
        status="imaging",
        post_text=post_text,
        headline=result["headline"],
        emoji_lead=result["emoji_lead"],
        last_error="",
    )
    return "imaging"


def _step_image(channel: EditorialChannelConfig, row: dict[str, Any]) -> str:
    news_id = int(row["id"])
    if str(row.get("media_type") or "") == "video":
        return "ready" if (row.get("status") or "") == "ready" else "imaging"
    settings = get_settings()
    meme_raw = bool(int(row.get("meme_source") or 0)) and not bool(
        getattr(settings, "meme_wrap_template", False)
    )
    if meme_raw and row.get("media_path"):
        update_news(
            news_id,
            status="ready",
            image_path=row.get("media_path"),
            cover_path=row.get("media_path"),
            post_kind="meme",
            last_error="",
        )
        return "ready"
    if not channel.image_rights_ack:
        update_news(
            news_id,
            status="held",
            last_error="image_rights_ack=false — внешнее фото запрещено",
        )
        return "held"
    template = channel.template_for(row.get("event_type") or "other")
    path = find_photo(row, template_name=template)
    if not path:
        update_news(news_id, status="held", last_error="нет релевантного фото")
        return "held"
    update_news(news_id, status="captioning", image_path=path, last_error="")
    return "captioning"


def _step_caption(row: dict[str, Any]) -> str:
    news_id = int(row["id"])
    from editorial.meme_text import is_meme_row

    if is_meme_row(row):
        update_news(
            news_id,
            status="rendering",
            caption="",
            caption_line1="",
            caption_line2="",
            last_error="",
        )
        return "rendering"
    cap = generate_caption(row, row.get("post_text") or "")
    text = (cap.get("caption_line1") or "").strip()
    update_news(
        news_id,
        status="rendering",
        caption=text,
        caption_line1=text,
        caption_line2="",
        last_error="",
    )
    return "rendering"


def _step_render(channel: EditorialChannelConfig, row: dict[str, Any]) -> str:
    news_id = int(row["id"])
    template = channel.template_for(row.get("event_type") or "other")
    badge = BADGE_FOR_EVENT.get(row.get("event_type") or "", "НОВОСТЬ")
    image_path = ensure_template_crop(row.get("image_path") or "", template_name=template)
    if image_path != (row.get("image_path") or ""):
        update_news(news_id, image_path=image_path)
        row = get_news(news_id) or row
    cover = render_post(
        template,
        image_path,
        row.get("caption_line1") or "",
        row.get("caption_line2") or None,
        badge,
        brand_render_context(channel),
        news_id=news_id,
    )
    prio = is_priority(row, channel)
    update_news(
        news_id,
        status="ready",
        cover_path=cover,
        is_priority=1 if prio else 0,
        last_error="",
    )
    return "ready"


def _is_entertainment(row: dict[str, Any]) -> bool:
    if str(row.get("post_kind") or "") in {"meme", "video"}:
        return True
    if int(row.get("meme_source") or 0):
        return True
    if (row.get("event_type") or "") in {"lifestyle", "meme", "human_factor"}:
        return True
    from editorial.pick import pick_tag_of

    return pick_tag_of(row) == "human_factor"


def _pick_with_entertainment_floor(
    channel: EditorialChannelConfig, leftover: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not leftover:
        return None
    settings = get_settings()
    floor = float(getattr(settings, "entertainment_floor_ratio", 0.0) or 0.0)
    if floor > 0:
        from editorial.story_throttle import channel_day

        today = channel_day()
        today_pub = [
            r
            for r in recent_published(channel.slug, limit=40)
            if str(r.get("published_at") or "").startswith(today)
        ]
        if today_pub:
            ent_n = sum(1 for r in today_pub if _is_entertainment(r))
            if ent_n / len(today_pub) < floor:
                ent_pool = [i for i in leftover if _is_entertainment(i)]
                if ent_pool:
                    return pick_best(ent_pool)
    return pick_best(leftover)


def publish_ready(client: MaxClient, channel: EditorialChannelConfig) -> list[dict[str, Any]]:
    from editorial.moderation import (
        can_dispatch_review,
        is_out_of_band_item,
        moderation_enabled,
        try_dispatch_memes,
        try_dispatch_review,
    )
    from editorial.store import status_counts, top_stuck_errors

    expire_stale(channel.slug, channel.cadence.item_ttl_sec)
    ensure_slot_initialized(channel)
    results: list[dict[str, Any]] = []

    counts = {str(r.get("status") or ""): int(r.get("n") or 0) for r in status_counts(channel.slug)}
    stuck = top_stuck_errors(channel.slug, limit=5)
    print(
        f"[editorial] status {channel.slug}: "
        f"held={counts.get('held', 0)} imaging={counts.get('imaging', 0)} "
        f"verifying={counts.get('verifying', 0)} ready={counts.get('ready', 0)} "
        f"awaiting_review={counts.get('awaiting_review', 0)} "
        f"stuck_errors={stuck}",
        flush=True,
    )
    results.append({"action": "status_snapshot", "counts": counts, "stuck_errors": stuck})

    auto_types = {str(x) for x in (channel.moderation.auto_publish_types or ()) if str(x)}
    if auto_types and moderation_enabled(channel):
        for item in list_ready(channel.slug):
            et = str(item.get("event_type") or "")
            if et not in auto_types or is_out_of_band_item(item):
                continue
            try:
                res = publish(client, channel, item)
                if res.get("action") in {"published", "simulated"}:
                    if is_priority(item, channel):
                        mark_priority_published(channel)
                    else:
                        mark_normal_published(channel)
                res["auto_publish"] = True
                results.append(res)
            except Exception as e:
                update_news(int(item["id"]), status="error", last_error=str(e)[:800])
                results.append(
                    {"action": "auto_publish_error", "id": item.get("id"), "error": str(e)[:200]}
                )

    if moderation_enabled(channel):
        try:
            results.extend(try_dispatch_memes(channel))
        except Exception as e:
            results.append({"action": "meme_review_error", "error": str(e)[:200]})
        # до queue_depth карточек в боте
        while can_dispatch_review(channel):
            try:
                disp = try_dispatch_review(channel)
                if not disp:
                    break
                results.append(disp)
            except Exception as e:
                results.append({"action": "review_error", "error": str(e)[:200]})
                break
        if not any(
            r.get("action") in {"dispatched_review", "dispatched_review_immediate"} for r in results
        ):
            results.append({"action": "awaiting_moderation", "slug": channel.slug})
        return results

    ready = list_ready(channel.slug)
    ready = [i for i in ready if (i.get("event_type") or "") not in {"matchday", "fixture_result"}]
    prio = [i for i in ready if is_priority(i, channel)]
    for item in prio:
        try:
            res = publish(client, channel, item)
            if res.get("action") in {"published", "simulated"}:
                mark_priority_published(channel)
            results.append(res)
        except MaxApiError as e:
            body = str(e.body or e)
            if "attachment.not.ready" in body:
                update_news(int(item["id"]), status="ready", last_error=body[:800])
                results.append({"action": "retry_ready", "id": item["id"]})
            else:
                update_news(int(item["id"]), status="error", last_error=body[:800])
                results.append({"action": "error", "id": item["id"], "error": body[:200]})
        except Exception as e:
            update_news(int(item["id"]), status="error", last_error=str(e)[:800])
            results.append({"action": "error", "id": item["id"], "error": str(e)[:200]})

    if not slot_ready(channel):
        return results

    leftover = [
        i
        for i in list_ready(channel.slug)
        if i.get("status") == "ready"
        and not is_priority(i, channel)
        and (i.get("event_type") or "") not in {"matchday", "fixture_result"}
    ]
    picked = _pick_with_entertainment_floor(channel, leftover)
    if picked:
        try:
            res = publish(client, channel, picked)
            if res.get("action") in {"published", "simulated"}:
                nxt = mark_normal_published(channel)
                res["next_slot_at"] = nxt.strftime("%Y-%m-%d %H:%M:%S")
            results.append(res)
        except MaxApiError as e:
            body = str(e.body or e)
            if "attachment.not.ready" in body:
                update_news(int(picked["id"]), status="ready", last_error=body[:800])
            else:
                update_news(int(picked["id"]), status="error", last_error=body[:800])
            results.append({"action": "error", "id": picked["id"], "error": body[:200]})
        except Exception as e:
            update_news(int(picked["id"]), status="error", last_error=str(e)[:800])
            results.append({"action": "error", "id": picked["id"], "error": str(e)[:200]})
    else:
        # слот наступил, постить нечего — не сдвигаем таймер, ждём готовое
        results.append({"action": "slot_idle", "slug": channel.slug})
    return results


def process_channel(channel: EditorialChannelConfig, client: MaxClient | None) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    try:
        extra["matchday"] = matchday_tick(channel, client)
    except Exception as e:
        print(f"[editorial] matchday fail: {e}", flush=True)
        extra["matchday"] = {"action": "error", "error": str(e)[:300]}
    try:
        extra["results"] = results_tick(channel, client)
    except Exception as e:
        print(f"[editorial] results fail: {e}", flush=True)
        extra["results"] = {"action": "error", "error": str(e)[:300]}
    ingested = ingest_channel(channel)
    advanced: list[str] = []
    for row in list_open_news(channel.slug):
        st = advance_item(channel, int(row["id"]))
        advanced.append(st)
    published: list[dict[str, Any]] = []
    if client is not None:
        published = publish_ready(client, channel)
    return {
        "slug": channel.slug,
        "ingested": ingested,
        "advanced": advanced,
        "published": published,
        "matchday": extra.get("matchday"),
        "results": extra.get("results"),
        "ts": utcnow_iso(),
    }


def run_editorial_tick(*, force_slug: str | None = None) -> list[dict[str, Any]]:
    init_db()
    reload_editorial_channels()
    try:
        ranking = refresh_top100()
        print(f"[editorial] fifa ranking: {ranking}", flush=True)
    except Exception as e:
        print(f"[editorial] fifa ranking skip: {e}", flush=True)
    seed_from_yaml_if_empty()

    configs = load_editorial_channels(include_disabled=True)
    results: list[dict[str, Any]] = []
    with MaxClient() as client:
        for cfg in configs:
            if force_slug and cfg.slug != force_slug:
                continue
            if not force_slug and not _channel_enabled(cfg):
                continue
            print(f"[editorial] tick {cfg.slug} chat={cfg.chat_id}", flush=True)
            try:
                res = process_channel(cfg, client)
            except Exception as e:
                print(f"[editorial] {cfg.slug} error: {e}", flush=True)
                res = {"action": "error", "slug": cfg.slug, "error": str(e)}
            results.append(res)
    return results


def resume_from(news_id: int, *, to_status: str | None = None) -> str:
    """Manual moderation: continue / regenerate."""
    row = get_news(news_id)
    if not row:
        return "missing"
    from editorial.channel_config import get_channel

    cfg = get_channel(row["channel_slug"])
    if not cfg:
        return "no_channel"
    if to_status:
        update_news(news_id, status=to_status, last_error="")
    return advance_item(cfg, news_id)
