"""Разметка готовых постов дневной симуляции: принять / отклонить / не в пул."""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT

LABEL_ROOT = ROOT / "data" / "editorial" / "labeling"
DAY_RE = re.compile(r"^day_(\d{4}-\d{2}-\d{2})$")


def day_dir(day: str) -> Path:
    return LABEL_ROOT / f"day_{day}"


def pool_path(day: str) -> Path:
    return day_dir(day) / "pool.json"


def labels_path(day: str) -> Path:
    return day_dir(day) / "labels.json"


def summary_path(day: str) -> Path:
    return day_dir(day) / "summary.json"


def covers_dir(day: str) -> Path:
    return day_dir(day) / "covers"


def _dump(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_days() -> list[str]:
    if not LABEL_ROOT.is_dir():
        return []
    days: list[str] = []
    for p in LABEL_ROOT.iterdir():
        m = DAY_RE.match(p.name)
        if m and (p / "pool.json").is_file():
            days.append(m.group(1))
    return sorted(days, reverse=True)


def load_pool(day: str) -> dict[str, Any]:
    path = pool_path(day)
    if not path.is_file():
        return {"kind": "day_sim_posts", "day": day, "items": [], "stats": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def load_labels(day: str) -> dict[str, Any]:
    path = labels_path(day)
    if not path.is_file():
        return {"kind": "day_sim_human_labels", "day": day, "updated_at": "", "items": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def pool_items(day: str) -> list[dict[str, Any]]:
    return list((load_pool(day).get("items") or []))


def item_by_id(day: str, item_id: str) -> dict[str, Any] | None:
    for row in pool_items(day):
        if str(row.get("id")) == str(item_id):
            return row
    return None


def progress(day: str) -> dict[str, int]:
    items = pool_items(day)
    labels = load_labels(day).get("items") or {}
    done = sum(1 for row in items if str(row.get("id")) in labels)
    return {"total": len(items), "done": done, "left": max(0, len(items) - done)}


def next_unlabeled(day: str, after_id: str = "") -> dict[str, Any] | None:
    labels = load_labels(day).get("items") or {}
    items = pool_items(day)
    start = 0
    if after_id:
        for i, row in enumerate(items):
            if str(row.get("id")) == after_id:
                start = i + 1
                break
    for row in items[start:] + items[:start]:
        if str(row.get("id")) not in labels:
            return row
    return None


def neighbor_ids(day: str, item_id: str) -> tuple[str | None, str | None]:
    ids = [str(r.get("id")) for r in pool_items(day)]
    try:
        i = ids.index(item_id)
    except ValueError:
        return None, None
    prev_id = ids[i - 1] if i > 0 else None
    next_id = ids[i + 1] if i + 1 < len(ids) else None
    return prev_id, next_id


def cover_file(day: str, item: dict[str, Any]) -> Path | None:
    rel = str(item.get("cover_file") or "")
    if not rel:
        return None
    path = (day_dir(day) / rel).resolve()
    try:
        path.relative_to(day_dir(day).resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def apply_decision(
    item: dict[str, Any],
    *,
    decision: str,
    comment: str = "",
) -> dict[str, Any]:
    dec = (decision or "").strip()
    if dec not in {"accept", "reject", "should_not_pool"}:
        dec = "reject"
    return {
        "id": item.get("id"),
        "decision": dec,
        "comment": (comment or "").strip()[:500],
        "title": (item.get("title") or "")[:200],
        "event_type": item.get("event_type") or "",
        "news_id": item.get("news_id"),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def save_label(day: str, rec: dict[str, Any]) -> dict[str, Any]:
    payload = load_labels(day)
    items = dict(payload.get("items") or {})
    items[str(rec["id"])] = rec
    payload["kind"] = "day_sim_human_labels"
    payload["day"] = day
    payload["updated_at"] = rec.get("ts") or datetime.now(timezone.utc).isoformat()
    payload["items"] = items
    payload["done"] = len(items)
    _dump(payload, labels_path(day))
    if progress(day)["left"] == 0:
        write_summary(day)
    return payload


def write_summary(day: str) -> dict[str, Any]:
    pool = load_pool(day)
    labels = load_labels(day).get("items") or {}
    counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    comments: list[dict[str, str]] = []
    for item in pool.get("items") or []:
        lid = str(item.get("id"))
        lab = labels.get(lid)
        if not lab:
            continue
        dec = str(lab.get("decision") or "")
        counts[dec] += 1
        row = {
            "id": lid,
            "title": item.get("title") or "",
            "event_type": item.get("event_type") or "",
            "decision": dec,
            "comment": lab.get("comment") or "",
            "slot": item.get("slot") or "",
        }
        rows.append(row)
        if lab.get("comment"):
            comments.append({"id": lid, "decision": dec, "comment": str(lab["comment"])})
    n = len(rows)
    summary = {
        "kind": "day_sim_summary",
        "day": day,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "n": n,
        "accept": counts["accept"],
        "reject": counts["reject"],
        "should_not_pool": counts["should_not_pool"],
        "accept_pct": round(100.0 * counts["accept"] / max(1, n), 1),
        "comments": comments,
        "rows": rows,
        "files": {
            "pool": str(pool_path(day)),
            "labels": str(labels_path(day)),
        },
    }
    _dump(summary, summary_path(day))
    return summary


def view_item(day: str, item: dict[str, Any]) -> dict[str, Any]:
    prev_id, next_id = neighbor_ids(day, str(item.get("id")))
    labeled = (load_labels(day).get("items") or {}).get(str(item.get("id")))
    return {
        "id": item.get("id"),
        "day": day,
        "title": item.get("title") or "",
        "source_title": item.get("source_title") or "",
        "url": item.get("url") or "",
        "event_type": item.get("event_type") or "",
        "slot": item.get("slot") or "",
        "post_text": item.get("post_text") or "",
        "caption": item.get("caption") or "",
        "headline": item.get("headline") or "",
        "pick_tag": item.get("pick_tag") or "",
        "pick_reason": item.get("pick_reason") or "",
        "factcheck": item.get("factcheck") or {},
        "has_cover": cover_file(day, item) is not None,
        "prev_id": prev_id,
        "next_id": next_id,
        "labeled": labeled,
        "progress": progress(day),
    }


def export_day_from_db(
    day: str,
    *,
    slug: str = "vnf_editorial",
    prefix: str = "sim",
) -> dict[str, Any]:
    """Собрать опубликованные посты симуляции суток в pool.json + копии обложек."""
    from editorial.pick import pick_tag_of
    from editorial.store import get_news

    from app.db import db

    ext_prefix = f"{prefix}-{day}:"
    with db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id FROM editorial_news
                WHERE channel_slug=? AND external_id LIKE ? AND status='published'
                ORDER BY published_at ASC, id ASC
                """,
                (slug, ext_prefix + "%"),
            ).fetchall()
        ]
        # матчи дня / результаты того же дня (если опубликованы)
        extra = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id FROM editorial_news
                WHERE channel_slug=? AND status='published'
                  AND (
                    external_id=?
                    OR (event_type IN ('matchday','fixture_result')
                        AND substr(published_at, 1, 10)=?)
                  )
                ORDER BY published_at ASC, id ASC
                """,
                (slug, f"matchday:{day}", day),
            ).fetchall()
        ]
    seen: set[int] = set()
    news_ids: list[int] = []
    for r in rows + extra:
        nid = int(r["id"])
        if nid in seen:
            continue
        seen.add(nid)
        news_ids.append(nid)

    out = day_dir(day)
    cov = covers_dir(day)
    if out.exists():
        shutil.rmtree(out)
    cov.mkdir(parents=True, exist_ok=True)

    items: list[dict[str, Any]] = []
    for i, news_id in enumerate(news_ids, start=1):
        row = get_news(news_id) or {}
        lid = f"post-{i:03d}"
        cover_rel = ""
        src = Path(str(row.get("cover_path") or ""))
        if src.is_file():
            dest = cov / f"{lid}.png"
            shutil.copy2(src, dest)
            cover_rel = f"covers/{dest.name}"
        ent: dict[str, Any] = {}
        try:
            ent = json.loads(row.get("entities_json") or "{}")
        except Exception:
            ent = {}
        pick = ent.get("pick") if isinstance(ent.get("pick"), dict) else {}
        items.append(
            {
                "id": lid,
                "news_id": news_id,
                "title": row.get("title") or "",
                "source_title": row.get("title") or "",
                "url": row.get("url") or "",
                "source": row.get("source") or "",
                "event_type": row.get("event_type") or "",
                "slot": row.get("published_at") or "",
                "post_text": row.get("post_text") or "",
                "caption": row.get("caption") or row.get("caption_line1") or "",
                "headline": row.get("headline") or "",
                "cover_file": cover_rel,
                "pick_tag": pick_tag_of(row) or pick.get("tag") or "",
                "pick_reason": pick.get("reason") or "",
                "factcheck": {
                    "status": row.get("factcheck_status") or "",
                    "reason": row.get("factcheck_reason") or "",
                    "sources": row.get("factcheck_sources"),
                },
                "entities": {
                    "players": list(ent.get("players") or [])[:6],
                    "teams": list(ent.get("teams") or [])[:6],
                },
            }
        )

    payload = {
        "kind": "day_sim_posts",
        "day": day,
        "slug": slug,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "instruction": (
            "accept — пост можно в канал как есть; "
            "reject — пост плохой (текст/фото/подача), но новость в принципе могла попасть; "
            "should_not_pool — новость вообще не должна была пройти отбор; "
            "comment — по желанию."
        ),
        "stats": {
            "published": len(items),
            "with_cover": sum(1 for x in items if x.get("cover_file")),
        },
        "items": items,
    }
    _dump(payload, pool_path(day))
    labels_path(day).write_text(
        json.dumps(
            {
                "kind": "day_sim_human_labels",
                "day": day,
                "updated_at": "",
                "items": {},
                "done": 0,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"day": day, "n": len(items), "path": str(pool_path(day))}
