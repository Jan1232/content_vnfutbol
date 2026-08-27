#!/usr/bin/env python3
"""Калибровка фото: новости за N дней → поиск картинки → JSON для разметки.

В ленту симуляции и в MAX ничего не пишется. Воркер editorial на время прогона ставится на паузу.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_dotenv_manual
from editorial.channel_config import get_channel, reload_editorial_channels
from editorial.cycle import _channel_enabled, set_channel_enabled
from editorial.imagery import find_photo
from editorial.imagery_trace import load_traces
from editorial.pick import pick_offline
from editorial.topic_gate import check as topic_check, classify_event_rules

OUT_DIR = ROOT / "data" / "editorial" / "labeling" / "imagery_4d"
INSTRUCTION = (
    "keep_photo: true — этот кадр можно в канал; false — нельзя. "
    "better_idx — индекс model.vision[], который взял бы ты (если не выбранный). "
    "note — коротко: не тот игрок / чужой клуб / коллаж / ок / нет фото."
)


def _load_sim():
    path = ROOT / "scripts" / "simulate_editorial_day.py"
    spec = importlib.util.spec_from_file_location("simulate_editorial_day", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"нет {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cal_id(url: str) -> str:
    return "cal-" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:12]


def _dump(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _trace_for(news_id: str) -> dict[str, Any]:
    rows = [t for t in load_traces(None) if str(t.get("news_id")) == str(news_id)]
    return rows[-1] if rows else {}


def _label_item(
    *,
    idx: int,
    news: dict[str, Any],
    trace: dict[str, Any],
    cropped: str | None,
    photos_dir: Path,
) -> dict[str, Any]:
    lid = f"img-{idx:04d}"
    pick = trace.get("pick") or {}
    vision = trace.get("vision") or {}
    photo_rel = ""
    if cropped:
        src = Path(cropped)
        if src.is_file():
            dest = photos_dir / f"{lid}{src.suffix or '.jpg'}"
            shutil.copy2(src, dest)
            photo_rel = f"photos/{dest.name}"
    return {
        "id": lid,
        "keep_photo": None,
        "better_idx": None,
        "note": "",
        "news": news,
        "photo": {
            "file": photo_rel,
            "source_url": pick.get("url") or "",
            "via": pick.get("via") or "",
            "score": pick.get("score"),
            "width": pick.get("width"),
            "height": pick.get("height"),
        }
        if photo_rel
        else None,
        "model": {
            "query": trace.get("query") or "",
            "outcome": trace.get("outcome") or ("picked" if photo_rel else "none"),
            "thought": {
                "who": pick.get("who") or "",
                "reason": pick.get("reason") or "",
                "score": pick.get("score"),
                "via": pick.get("via") or "",
            },
            "searches": list(trace.get("searches") or []),
            "quality_drops": list(trace.get("quality_drops") or []),
            "vision": list(vision.get("candidates") or []),
            "prompt": (vision.get("prompt") or "")[:2000],
            "vision_error": vision.get("error"),
        },
    }


def _row_from_news(news: dict[str, Any]) -> dict[str, Any]:
    url = str(news.get("url") or "")
    return {
        "id": _cal_id(url),
        "title": news.get("title") or "",
        "url": url,
        "event_type": news.get("event_type") or "other",
        "entities_json": json.dumps(news.get("entities") or {}, ensure_ascii=False),
        "caption": news.get("title") or "",
        "published_at": news.get("published_at") or "",
    }


def _news_from_item(item: Any) -> dict[str, Any]:
    return {
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "published_at": item.published_at.isoformat() if item.published_at else "",
        "event_type": item.event_type or "other",
        "competition": item.competition or "",
        "entities": {
            "players": list((item.entities or {}).get("players") or [])[:6],
            "teams": list((item.entities or {}).get("teams") or [])[:6],
        },
        "body": (item.body or "")[:500],
    }


def _load_news_from_pool(path: Path, *, limit: int) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    news_rows: list[dict[str, Any]] = []
    for item in payload.get("items") or []:
        news = item.get("news") if isinstance(item, dict) else None
        if not isinstance(news, dict) or not news.get("title"):
            continue
        news_rows.append(news)
        if limit and len(news_rows) >= limit:
            break
    return news_rows


def _reset_label_artifacts(out_dir: Path) -> None:
    photos_dir = out_dir / "photos"
    fitted_dir = out_dir / "fitted"
    if photos_dir.is_dir():
        shutil.rmtree(photos_dir)
    photos_dir.mkdir(parents=True, exist_ok=True)
    if fitted_dir.is_dir():
        shutil.rmtree(fitted_dir)
    for name in ("labels.json", "comparison.json"):
        p = out_dir / name
        if p.is_file():
            p.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=4)
    parser.add_argument("--slug", default="vnf_editorial")
    parser.add_argument("--limit", type=int, default=0, help="макс. новостей на фото, 0 = все take")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument(
        "--from-pool",
        default="",
        help="pool.json: не собирать новости, только заново искать фото",
    )
    args = parser.parse_args()

    load_dotenv_manual()
    reload_editorial_channels()
    cfg = get_channel(args.slug)
    if not cfg:
        raise SystemExit(f"нет канала {args.slug}")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(1, args.days))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    photos_dir = out_dir / "photos"
    pool_path = out_dir / "pool.json"
    log_path = out_dir / "run.log"

    n_off = n_filt = 0
    collected = 0
    if args.from_pool:
        src = Path(args.from_pool)
        if not src.is_file():
            raise SystemExit(f"нет пула {src}")
        news_list = _load_news_from_pool(src, limit=args.limit)
        collected = len(news_list)
        print(
            f"[cal] rerun photos from {src} n={len(news_list)} (новости как есть)",
            flush=True,
        )
        if src.resolve() == pool_path.resolve() and pool_path.is_file():
            bak = out_dir / "pool_v1_entity_query.json"
            if not bak.is_file():
                shutil.copy2(pool_path, bak)
                print(f"[cal] backup {bak}", flush=True)
        _reset_label_artifacts(out_dir)
        window = {"start": start.isoformat(), "end": end.isoformat(), "days": args.days, "mode": "rerun_photos"}
    else:
        sim = _load_sim()
        print(f"[cal] window {start.isoformat()} .. {end.isoformat()}", flush=True)
        pool_items = sim.load_from_pool(start, end)
        live_items = sim.collect_live(start, end)
        items = sim.merge_items(pool_items, live_items)
        collected = len(items)
        print(
            f"[cal] collected pool={len(pool_items)} live={len(live_items)} unique={len(items)}",
            flush=True,
        )
        takes: list[Any] = []
        for item in items:
            ok, _reason, _payload = topic_check(
                item, extra_teams=cfg.always_priority_teams, use_llm=False
            )
            if not ok:
                n_off += 1
                continue
            if not item.event_type or item.event_type == "other":
                item.event_type = classify_event_rules(f"{item.title}\n{item.body}")
            verdict = pick_offline(item, allow_rumors=cfg.allow_rumors)
            if not verdict.take:
                n_filt += 1
                continue
            takes.append(item)
        if args.limit:
            takes = takes[: args.limit]
        news_list = [_news_from_item(item) for item in takes]
        photos_dir.mkdir(parents=True, exist_ok=True)
        print(
            f"[cal] take={len(news_list)} off_topic={n_off} filtered={n_filt} "
            f"(фото только у take, как в editorial)",
            flush=True,
        )
        window = {"start": start.isoformat(), "end": end.isoformat(), "days": args.days}

    payload: dict[str, Any] = {
        "kind": "imagery_calibration",
        "window": window,
        "instruction": INSTRUCTION,
        "stats": {
            "collected": collected,
            "take": len(news_list),
            "off_topic": n_off,
            "filtered": n_filt,
            "done": 0,
            "picked": 0,
            "held": 0,
            "errors": 0,
        },
        "items": [],
    }
    _dump(payload, pool_path)

    was_yaml = bool(cfg.enabled)
    set_channel_enabled(cfg.slug, False)
    print(f"[cal] paused editorial worker yaml_enabled={was_yaml}", flush=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    try:
        for i, news in enumerate(news_list, start=1):
            row = _row_from_news(news)
            template = cfg.template_for(row["event_type"])
            print(f"[cal] {i}/{len(news_list)} {template} {(news.get('title') or '')[:80]}", flush=True)
            cropped = None
            try:
                cropped = find_photo(row, template_name=template)
            except Exception as e:
                traceback.print_exc()
                payload["stats"]["errors"] += 1
                print(f"[cal]   fail {e}", flush=True)
            trace = _trace_for(row["id"])
            label = _label_item(
                idx=i, news=news, trace=trace, cropped=cropped, photos_dir=photos_dir
            )
            payload["items"].append(label)
            if label.get("photo"):
                payload["stats"]["picked"] += 1
            else:
                payload["stats"]["held"] += 1
            payload["stats"]["done"] = i
            _dump(payload, pool_path)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"{i}/{len(news_list)} {label['model']['outcome']} {(news.get('title') or '')[:90]}\n"
                )
            time.sleep(0.4)
    finally:
        set_channel_enabled(cfg.slug, was_yaml)
        print(f"[cal] restored editorial enabled={was_yaml}", flush=True)

    _dump(payload, pool_path)
    st = payload["stats"]
    print(
        f"[cal] done picked={st['picked']} held={st['held']} err={st['errors']} "
        f"json={pool_path}",
        flush=True,
    )
    print("[cal] разметка: /editorial/label-photos", flush=True)
    return 0 if st["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
