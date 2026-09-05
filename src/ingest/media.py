# -*- coding: utf-8 -*-
"""Сборка медиа по плану (source file / yandex / none)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.ingest.media_router import MediaPlan, media_source_allowed, resolve_media_plan
from src.ingest.yandex_images import fetch_yandex_image

log = logging.getLogger("ingest.media")


def build_media(
    *,
    archetype: str,
    source: str,
    source_media_path: str | None,
    media_kind: str | None,
    image_query: str | None,
    dest_stem: str,
) -> dict[str, Any]:
    """Возвращает поля медиа для generated_live."""
    has = bool(source_media_path and Path(source_media_path).is_file())
    plan: MediaPlan = resolve_media_plan(
        archetype=archetype,
        source=source,
        has_source_media=has,
        media_kind=media_kind,
    )
    log.info(
        "media plan arch=%s source=%s strategy=%s has_file=%s kind=%s query=%r path=%s",
        archetype,
        source,
        plan.strategy,
        has,
        media_kind,
        image_query,
        source_media_path,
    )
    out: dict[str, Any] = {
        "media_strategy": plan.strategy,
        "media_kind": None,
        "media_path": None,
        "media_url": None,
        "image_query": image_query,
        "media_warning": None,
        "media_fail_reason": None,
    }

    if plan.strategy == "none":
        log.info("media skip (strategy=none) arch=%s", archetype)
        return out

    if plan.strategy in ("source", "as_is"):
        if not has:
            out["media_strategy"] = "missing"
            out["media_warning"] = "⚠ картинка не найдена"
            out["media_fail_reason"] = "no_source_file"
            log.warning("media missing: no source file for %s/%s", source, archetype)
            return out
        if not media_source_allowed(
            strategy=plan.strategy, source=source, media_kind=media_kind
        ):
            if media_kind == "video":
                out["media_path"] = source_media_path
                out["media_kind"] = "video"
                log.info("media as_is video allowed source=%s", source)
                return out
            log.info("block unclean image, fallback yandex source=%s", source)
            plan = MediaPlan("yandex", reason="watermark/unclean block")
            out["media_strategy"] = "yandex"
        else:
            out["media_path"] = source_media_path
            out["media_kind"] = media_kind or "photo"
            log.info("media from source ok path=%s kind=%s", source_media_path, out["media_kind"])
            return out

    if plan.strategy == "yandex":
        q = (image_query or "").strip()
        if not q:
            out["media_strategy"] = "missing"
            out["media_warning"] = "⚠ картинка не найдена"
            out["media_fail_reason"] = "empty_image_query"
            log.warning("media missing: empty image_query")
            return out
        url, path, reason = fetch_yandex_image(q, dest_stem)
        out["media_url"] = url
        if not path:
            out["media_strategy"] = "missing"
            out["media_warning"] = "⚠ картинка не найдена"
            out["media_fail_reason"] = reason
            log.warning("media missing yandex q=%r reason=%s url=%s", q, reason, url)
            return out
        out["media_path"] = str(path)
        out["media_kind"] = "photo"
        out["media_strategy"] = "yandex"
        log.info("media yandex ok q=%r path=%s", q, path)
        return out

    out["media_strategy"] = "missing"
    out["media_warning"] = "⚠ картинка не найдена"
    out["media_fail_reason"] = "unknown_strategy"
    return out
