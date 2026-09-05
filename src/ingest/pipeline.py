"""Пайплайн v3.1: фильтр → extract/schedule → embed → dedup → медиа → Terra/фраза → очередь."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from src.generate.fan import generate_single
from src.generate.guardrail import check_guardrail
from src.ingest import db
from src.ingest.dedup import find_duplicate, fingerprint_hash, soft_zh_overlap
from src.ingest.embed import embed_text
from src.ingest.extract import extract_fact
from src.ingest.filter import check_garbage
from src.ingest.media import build_media
from src.ingest.schedule import is_schedule_post, schedule_phrase
from src.ingest.yandex_images import fetch_yandex_image

log = logging.getLogger("ingest.pipeline")

_TEST_SOURCE_PREFIXES = ("test_", "test_v")


def _is_test_source(source: str) -> bool:
    s = (source or "").strip().lower()
    return s.startswith(_TEST_SOURCE_PREFIXES) or s in {"test", "test_source"}


def _produce_text(
    *,
    archetype: str,
    fact: str,
    veracity: str,
    is_sensation: bool,
    note: str | None,
    source_text: str | None = None,
) -> tuple[str, list[str]]:
    if archetype == "schedule":
        return schedule_phrase(), []
    if archetype == "meme" and source_text and len(source_text.strip()) < 200:
        # подпись как есть, если короткая
        return source_text.strip() or fact, []
    gen = generate_single(fact, veracity, archetype, is_sensation, note=note)
    post = gen["post"]
    flags = gen["flags"] or check_guardrail(post, veracity, fact=fact)
    return post, flags or []


def process_message(
    *,
    source: str,
    msg_id: int,
    text: str,
    ts: int | None = None,
    is_forward: bool = False,
    has_media_only: bool = False,
    skip_generate: bool = False,
    source_media_path: str | None = None,
    media_kind: str | None = None,
    run_tag: str | None = None,
    replace_raw: bool = False,
) -> dict[str, Any]:
    ts = ts or int(time.time())
    result: dict[str, Any] = {
        "source": source,
        "msg_id": msg_id,
        "status": "unknown",
    }

    schedule_hit = is_schedule_post(text, source)
    allow_media = has_media_only and source in (
        "footballhourss",
        "thesoccerblogteam",
    )

    reason = check_garbage(
        text,
        is_forward=is_forward,
        has_media_only=has_media_only and not allow_media and not schedule_hit,
    )
    if reason and schedule_hit:
        reason = None
    if reason and allow_media and not text.strip():
        reason = None

    is_test = _is_test_source(source)
    raw_id = db.insert_raw(
        source=source,
        msg_id=msg_id,
        text=text or "",
        ts=ts,
        is_filtered=1 if reason else 0,
        filter_reason=reason,
        replace=replace_raw,
        is_test=is_test,
    )
    if raw_id is None:
        result["status"] = "duplicate_raw"
        return result

    result["raw_id"] = raw_id
    if reason:
        result["status"] = "filtered"
        result["filter_reason"] = reason
        return result

    # после extract — ещё раз жёстко режем рекламу (LLM иногда пропускает)
    if not schedule_hit:
        ad2 = check_garbage(text, is_forward=is_forward, has_media_only=False)
        if ad2 and ad2.startswith("ad_"):
            db.mark_filtered(raw_id, ad2)
            result["status"] = "filtered"
            result["filter_reason"] = ad2
            return result

    image_query: str | None = None
    attribution = None
    is_sensation = False
    veracity = "verified"
    event: dict[str, Any] = {
        "teams": [],
        "player": None,
        "to_club": None,
        "score": None,
        "minute": None,
        "event_kind": "other",
    }

    if schedule_hit:
        archetype = "schedule"
        fact = "Расписание матчей на сегодня"
        # без LLM
    elif allow_media and not (text or "").strip():
        archetype = "video" if media_kind == "video" else "meme"
        fact = "Мем/видео без текста" if archetype == "meme" else "Видео из источника"
        is_sensation = False
        veracity = "verified"
    else:
        try:
            extracted = extract_fact(text, source)
        except Exception:
            log.exception("extract failed")
            db.mark_filtered(raw_id, "extract_error")
            result["status"] = "extract_error"
            return result

        if extracted.get("is_garbage"):
            reason = extracted.get("skip_reason") or "is_garbage"
            db.mark_filtered(raw_id, f"filtered_extract:{reason}", is_garbage=True)
            result["status"] = "filtered_extract"
            result["filter_reason"] = f"filtered_extract:{reason}"
            result["is_garbage"] = True
            return result

        if not extracted.get("is_news") or not (extracted.get("fact") or "").strip():
            # медиа-мем с подписью-шуткой
            if source_media_path and source in ("footballhourss", "thesoccerblogteam"):
                archetype = "video" if media_kind == "video" else "meme"
                fact = (text or "").strip()[:300] or "Мем из источника"
            else:
                reason = extracted.get("skip_reason") or "not_news"
                db.mark_filtered(raw_id, f"extractor:{reason}")
                result["status"] = "filtered"
                result["filter_reason"] = f"extractor:{reason}"
                return result
        else:
            fact = extracted["fact"].strip()
            archetype = extracted["archetype"]
            veracity = extracted["veracity"]
            is_sensation = bool(extracted["is_sensation"])
            attribution = extracted.get("source_attribution")
            event = extracted.get("event") or event
            image_query = extracted.get("image_query")

    fp = fingerprint_hash(event)

    try:
        fact_emb = embed_text(fact)
    except Exception:
        log.exception("embed failed")
        result["status"] = "embed_error"
        return result

    dup = find_duplicate(event=event, fact_text=fact, embedding=fact_emb)
    if dup:
        fact_id, layer, score = dup
        db.increment_confirms(fact_id)
        db.insert_fact(
            raw_msg_id=raw_id,
            fact=f"[dedup of {fact_id} layer={layer} score={score:.3f}]",
            archetype=archetype,
            veracity=veracity,
            is_sensation=is_sensation,
            attribution=attribution,
            embedding=fact_emb,
            confirms_count=0,
            dedup_of=fact_id,
            event=event,
            event_fingerprint=fp,
            dedup_layer=layer,
            image_query=image_query,
        )
        result["status"] = "dedup"
        result["dedup_of"] = fact_id
        result["dedup_layer"] = layer
        result["score"] = score
        return result

    zh_hit = soft_zh_overlap(event) if source != "zhfootballll" else None

    fact_id = db.insert_fact(
        raw_msg_id=raw_id,
        fact=fact,
        archetype=archetype,
        veracity=veracity,
        is_sensation=is_sensation,
        attribution=attribution,
        embedding=fact_emb,
        event=event,
        event_fingerprint=fp,
        image_query=image_query,
    )
    result["fact_id"] = fact_id
    result["fact"] = fact
    result["archetype"] = archetype

    if skip_generate:
        result["status"] = "fact_only"
        return result

    note = None
    if attribution:
        note = f"Атрибуция из источника: {attribution}"
    if source == "zhfootballll":
        note = (note + " | " if note else "") + (
            "Источник-образец @zhfootballll — голос свой, не копировать дословно."
        )
    if zh_hit:
        note = (note + " | " if note else "") + f"Пересечение с ЖФ fact_id={zh_hit}."

    try:
        post, flags = _produce_text(
            archetype=archetype,
            fact=fact,
            veracity=veracity,
            is_sensation=is_sensation,
            note=note,
            source_text=text,
        )
    except Exception:
        log.exception("produce text failed")
        result["status"] = "generate_error"
        return result

    media = build_media(
        archetype=archetype,
        source=source,
        source_media_path=source_media_path,
        media_kind=media_kind,
        image_query=image_query,
        dest_stem=f"gen_{source}_{msg_id}",
    )
    warn = media.get("media_warning")
    if warn:
        flags = list(flags) + [warn]

    gid = db.insert_generated(
        fact_id=fact_id,
        text=post,
        guardrail_flag=" | ".join(flags) if flags else None,
        media_path=media.get("media_path"),
        media_url=media.get("media_url"),
        media_kind=media.get("media_kind"),
        media_strategy=media.get("media_strategy"),
        image_query=media.get("image_query") or image_query,
        media_warning=warn,
        archetype_override=None,
        run_tag=run_tag,
        is_test=is_test,
    )
    if run_tag and (
        run_tag == "run_24h" or run_tag.startswith("run_24h") or run_tag.startswith("v3")
    ):
        db.register_run_24h(
            news_id=gid,
            generated_id=gid,
            fact_id=fact_id,
            source=source,
            msg_id=msg_id,
            raw_text=text,
            fact=fact,
            event=event,
            image_query=image_query,
            archetype=archetype,
            media_strategy=media.get("media_strategy"),
            run_tag=run_tag,
            is_test=_is_test_source(source),
        )
    result["status"] = "queued"
    result["generated_id"] = gid
    result["news_id"] = gid
    result["post"] = post
    result["media"] = media
    log.info(
        "queued source=%s msg=%s fact_id=%s news_id=%s arch=%s media=%s",
        source,
        msg_id,
        fact_id,
        gid,
        archetype,
        media.get("media_strategy"),
    )
    return result


def regenerate_for_category(
    *,
    fact_id: int,
    new_archetype: str,
    source_media_path: str | None = None,
    media_kind: str | None = None,
) -> dict[str, Any]:
    """Смена категории: новый generated_live по правилам ветки."""
    bundle = db.get_fact_bundle(fact_id)
    if not bundle:
        raise RuntimeError(f"fact {fact_id} not found")
    old = bundle["archetype"]
    source = bundle.get("source") or ""
    msg_id = bundle.get("msg_id")
    fact = bundle["fact"]
    veracity = bundle.get("veracity") or "rumored"
    is_sensation = bool(bundle.get("is_sensation"))
    image_query = bundle.get("image_query")
    source_text = bundle.get("source_text") or ""

    # raw мог быть удалён — достаём source/msg из run_24h
    if not source or not msg_id:
        r24 = db.get_run24_source(fact_id)
        if r24:
            source = source or (r24.get("source") or "")
            msg_id = msg_id or r24.get("msg_id")
            if not source_text:
                source_text = r24.get("raw_text") or ""

    if not source_media_path:
        source_media_path, guessed_kind = db.find_source_media_file(source, msg_id)
        media_kind = media_kind or guessed_kind
        log.info(
            "regen media lookup source=%s msg=%s -> %s kind=%s",
            source,
            msg_id,
            source_media_path,
            media_kind,
        )

    db.update_fact_archetype(fact_id, new_archetype)
    post, flags = _produce_text(
        archetype=new_archetype,
        fact=fact,
        veracity=veracity,
        is_sensation=is_sensation,
        note=f"Смена категории {old} → {new_archetype}",
        source_text=source_text,
    )
    media = build_media(
        archetype=new_archetype,
        source=source,
        source_media_path=source_media_path,
        media_kind=media_kind,
        image_query=image_query,
        dest_stem=f"regen_{fact_id}_{new_archetype}",
    )
    warn = media.get("media_warning")
    if warn:
        flags = list(flags) + [warn]
    gid = db.insert_generated(
        fact_id=fact_id,
        text=post,
        guardrail_flag=" | ".join(flags) if flags else None,
        media_path=media.get("media_path"),
        media_url=media.get("media_url"),
        media_kind=media.get("media_kind"),
        media_strategy=media.get("media_strategy"),
        image_query=media.get("image_query") or image_query,
        media_warning=warn,
        archetype_override=new_archetype,
        run_tag="run_24h",
    )
    if media.get("media_fail_reason"):
        log.warning(
            "regen media fail fact=%s arch=%s reason=%s",
            fact_id,
            new_archetype,
            media.get("media_fail_reason"),
        )
    return {
        "generated_id": gid,
        "news_id": gid,
        "old_category": old,
        "new_category": new_archetype,
        "post": post,
        "media": media,
    }


def replace_media_manual_query(
    *,
    fact_id: int,
    post_text: str,
    manual_query: str,
    archetype: str | None = None,
    visible_news_id: int | None = None,
) -> dict[str, Any]:
    """Ручной Yandex-поиск: тот же текст, только новое медиа."""
    q = (manual_query or "").strip()
    if not q:
        raise ValueError("empty manual_query")

    dest_stem = f"manual_{fact_id}_{int(time.time())}"
    url, path, reason = fetch_yandex_image(q, dest_stem)
    if path:
        media: dict[str, Any] = {
            "media_strategy": "yandex",
            "media_kind": "photo",
            "media_path": str(path),
            "media_url": url,
            "image_query": q,
            "media_warning": None,
            "media_fail_reason": None,
        }
    else:
        media = {
            "media_strategy": "missing",
            "media_kind": None,
            "media_path": None,
            "media_url": url,
            "image_query": q,
            "media_warning": "⚠ картинка не найдена",
            "media_fail_reason": reason,
        }
        log.warning(
            "manual image miss fact=%s q=%r reason=%s", fact_id, q, reason
        )

    gid = db.insert_generated(
        fact_id=fact_id,
        text=post_text,
        guardrail_flag=None,
        media_path=media.get("media_path"),
        media_url=media.get("media_url"),
        media_kind=media.get("media_kind"),
        media_strategy=media.get("media_strategy"),
        image_query=q,
        media_warning=media.get("media_warning"),
        archetype_override=archetype,
        run_tag="run_24h",
    )
    news_id = visible_news_id if visible_news_id is not None else gid
    if visible_news_id is not None and visible_news_id != gid:
        db.set_generated_news_id(gid, visible_news_id)
    return {
        "generated_id": gid,
        "news_id": news_id,
        "post": post_text,
        "media": media,
    }
