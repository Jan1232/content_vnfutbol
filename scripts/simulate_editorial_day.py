"""Симуляция editorial-ленты за календарные сутки. В MAX ничего не уходит."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import sys
import traceback
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import ROOT as APP_ROOT, get_settings, load_dotenv_manual
from app.db import init_db
from editorial.catalogs import detect_competition
from editorial.channel_config import get_channel, reload_editorial_channels
from editorial.cycle import (
    _SCORE_RE,
    _as_news_item,
    _channel_enabled,
    _entities_json,
    _score_key,
    _step_caption,
    _step_edit,
    _step_image,
    _step_render,
    _teams_json,
    set_channel_enabled,
)
from editorial.factcheck import verify
from editorial.fifa_ranking import seed_from_yaml_if_empty
from editorial.models import NewsItem
from editorial.pick import pick_offline, pick_tag_of
from editorial.policy import HUMAN_FACTOR_WINDOW
from editorial.publisher import publish
from editorial.scheduler import is_priority, pick_best, random_gap_minutes
from editorial.store import (
    cluster_published,
    get_by_external,
    get_channel_state,
    get_news,
    insert_news,
    recent_published,
    update_news,
    upsert_channel_state,
)
from editorial.topic_gate import check as topic_check, classify_event_rules, cluster_id_for, extract_entities

MSK = timezone(timedelta(hours=3))
POOL_PATH = APP_ROOT / "data" / "editorial" / "labeling" / "pool_14d.json"
SIM_PREFIX = "sim"


def _load_collector():
    path = APP_ROOT / "scripts" / "collect_editorial_labeling_pool.py"
    spec = importlib.util.spec_from_file_location("collect_editorial_labeling_pool", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"нет коллектора {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=MSK).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def _norm_url(url: str) -> str:
    import re

    return re.sub(r"[?#].*$", "", (url or "").strip()).rstrip("/")


def _external_id(day: date, source: str, url: str) -> str:
    digest = hashlib.sha1(_norm_url(url).encode("utf-8")).hexdigest()[:16]
    return f"{SIM_PREFIX}-{day.isoformat()}:{source}:{digest}"


def _from_raw(raw: dict[str, Any]) -> NewsItem | None:
    title = str(raw.get("title") or "").strip()
    url = str(raw.get("url") or "").strip()
    published = _parse_dt(raw.get("published_at"))
    if not title or not url or published is None:
        return None
    body = str(raw.get("body") or "")
    text = f"{title}\n{body}"
    entities = extract_entities(text)
    event_type = str(raw.get("event_type_guess") or "") or classify_event_rules(text)
    competition = detect_competition(text) or str(entities.get("competition") or "")
    item = NewsItem(
        external_id=url,
        source=str(raw.get("source") or "unknown"),
        url=url,
        title=title,
        body=body,
        lang=str(raw.get("lang") or ""),
        published_at=published,
        entities=entities,
        event_type=event_type or "other",
        competition=competition,
        is_national=bool(entities.get("is_national")),
    )
    item.cluster_id = cluster_id_for(item)
    return item


def load_from_pool(start: datetime, end: datetime) -> list[NewsItem]:
    if not POOL_PATH.is_file():
        return []
    payload = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    out: list[NewsItem] = []
    for raw in payload.get("items") or []:
        item = _from_raw(raw)
        if item and start <= item.published_at < end:
            out.append(item)
    return out


def collect_live(start: datetime, end: datetime) -> list[NewsItem]:
    mod = _load_collector()
    from app.http_util import http_client

    now = datetime.now(timezone.utc)
    raw: list[dict[str, Any]] = []
    with http_client() as client:
        raw.extend(mod.collect_championat(client, start, end))
        raw.extend(mod.collect_rss(client, "sportsru_football", mod.SPORTSRU_RSS, start, end))
        try:
            raw.extend(mod.collect_sportsru_html(client, start, end, now))
        except Exception as e:
            print(f"[sim] sportsru html skip: {e}", flush=True)
        for name, url in mod.EN_FEEDS:
            try:
                raw.extend(mod.collect_rss(client, name, url, start, end))
            except Exception as e:
                print(f"[sim] {name} skip: {e}", flush=True)
    items: list[NewsItem] = []
    for row in raw:
        item = _from_raw(row)
        if item and start <= item.published_at < end:
            items.append(item)
    return items


def merge_items(*batches: list[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    out: list[NewsItem] = []
    for batch in batches:
        for item in sorted(batch, key=lambda x: x.published_at):
            key = _norm_url(item.url) or item.title.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
    out.sort(key=lambda x: x.published_at)
    return out


def reset_sim_day(day: date, slug: str) -> dict[str, int]:
    """Стирает прошлую симуляцию суток: посты в ленте и статусы, чтобы фильтр прошёл заново."""
    prefix = f"{SIM_PREFIX}-{day.isoformat()}:"
    matchday_ext = f"matchday:{day.isoformat()}"
    from app.db import db

    with db() as conn:
        ids = [
            int(r["id"])
            for r in conn.execute(
                """
                SELECT id FROM editorial_news
                WHERE channel_slug=? AND (
                    external_id LIKE ?
                    OR external_id=?
                    OR event_type IN ('matchday', 'fixture_result')
                )
                """,
                (slug, prefix + "%", matchday_ext),
            ).fetchall()
        ]
        posts = 0
        if ids:
            placeholders = ",".join("?" * len(ids))
            ext = [f"editorial:{i}" for i in ids]
            cur = conn.execute(
                f"DELETE FROM posts WHERE external_id IN ({placeholders})",
                ext,
            )
            posts = int(cur.rowcount or 0)
        cur = conn.execute(
            """
            UPDATE editorial_news
            SET status='skipped',
                last_error='',
                retry_count=0,
                mid='',
                published_at='',
                image_path='',
                cover_path='',
                caption='',
                caption_line1='',
                caption_line2='',
                post_text='',
                headline='',
                topic_status='',
                factcheck_status=''
            WHERE channel_slug=? AND external_id LIKE ?
            """,
            (slug, prefix + "%"),
        )
        news = int(cur.rowcount or 0)
        conn.execute(
            """
            DELETE FROM editorial_news
            WHERE channel_slug=? AND (
                external_id=? OR event_type IN ('matchday', 'fixture_result')
            )
            """,
            (slug, matchday_ext),
        )
        conn.execute(
            "DELETE FROM match_results_posted WHERE channel_slug=?",
            (slug,),
        )
        conn.execute(
            """
            UPDATE editorial_channel_state
            SET matchday_last_date=''
            WHERE channel_slug=? AND matchday_last_date=?
            """,
            (slug, day.isoformat()),
        )
    return {"posts": posts, "news": news, "ids": len(ids)}


def reset_failed_candidates(day: date) -> int:
    prefix = f"{SIM_PREFIX}-{day.isoformat()}:"
    from app.db import db

    with db() as conn:
        cur = conn.execute(
            """
            UPDATE editorial_news
            SET status='skipped',
                last_error='sim-pool: кандидат в слот',
                retry_count=0
            WHERE external_id LIKE ?
              AND status IN (
                'held','rejected','error','verifying','confirmed',
                'editing','imaging','captioning','rendering','ready'
              )
            """,
            (prefix + "%",),
        )
        return int(cur.rowcount or 0)


def simulate_fixtures(cfg, day: date) -> dict[str, Any]:
    """Утренняя сетка 09:00 МСК + карточки счёта значимых матчей. Вне каденса."""
    from editorial.fixtures import FINISHED_STATUSES, get_provider, significant_matches
    from editorial.matchday import matchday_tick
    from editorial.results import _publish_result
    from editorial.store import result_already_posted, upsert_fixture

    out: dict[str, Any] = {"matchday": None, "results": []}
    saved_md = (get_channel_state(cfg.slug).get("matchday_last_date") or "")
    morning = datetime(day.year, day.month, day.day, 9, 5, tzinfo=MSK)
    stamp = morning.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        md = matchday_tick(cfg, None, now=morning, force=True, published_at=stamp)
        out["matchday"] = md
        print(f"[sim] matchday {morning.strftime('%d.%m %H:%M')} -> {md}", flush=True)
    except Exception as e:
        traceback.print_exc()
        out["matchday"] = {"action": "error", "error": str(e)[:300]}
        print(f"[sim] matchday fail: {e}", flush=True)

    src = get_provider()
    raw = src.matches_on(day)
    for m in raw:
        upsert_fixture(m)
    sig = significant_matches(
        raw,
        always_priority=cfg.always_priority_teams,
        grands=cfg.matchday.grands,
        all_cl_el=cfg.matchday.all_cl_el,
        national_top100=cfg.matchday.national_top100,
    )
    finished = [
        m
        for m in sig
        if m.status in FINISHED_STATUSES and m.score_home is not None and m.score_away is not None
    ]
    print(f"[sim] fixtures day={day} total={len(raw)} significant={len(sig)} finished={len(finished)}", flush=True)
    for m in sorted(finished, key=lambda x: x.kickoff_utc):
        if result_already_posted(m.provider_id, cfg.slug):
            continue
        when = m.kickoff_utc + timedelta(hours=2)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        stamp_r = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[sim] result {m.home_ru} {m.score_home}:{m.score_away} {m.away_ru} "
            f"{when.astimezone(MSK).strftime('%H:%M')}",
            flush=True,
        )
        try:
            res = _publish_result(cfg, None, m, published_at=stamp_r)
            out["results"].append(res)
            print(f"[sim]   -> {res}", flush=True)
        except Exception as e:
            traceback.print_exc()
            out["results"].append({"action": "error", "error": str(e)[:200], "provider_id": m.provider_id})
            print(f"[sim]   -> error {e}", flush=True)

    if saved_md and saved_md != day.isoformat():
        upsert_channel_state(cfg.slug, matchday_last_date=saved_md)
    else:
        upsert_channel_state(cfg.slug, matchday_last_date="")
    return out


def ingest_item(channel_slug: str, day: date, item: NewsItem) -> int | None:
    ext = _external_id(day, item.source, item.url)
    existing = get_by_external(channel_slug, ext)
    if existing:
        return int(existing["id"])
    return insert_news(
        {
            "channel_slug": channel_slug,
            "external_id": ext,
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
            "entities_json": _entities_json(item),
            "status": "skipped",
        }
    )


def filter_item(channel, row: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    item = _as_news_item(row)
    ok, reason, payload = topic_check(
        item,
        extra_teams=channel.always_priority_teams,
        # LLM topic на всех RSS суток — часы; правила + LLM-pick у take достаточно.
        use_llm=False,
    )
    news_id = int(row["id"])
    event_type = row.get("event_type") or item.event_type
    if event_type in {"", "other"}:
        event_type = classify_event_rules(f"{item.title}\n{item.body}")
    if not ok:
        update_news(
            news_id,
            status="off_topic",
            topic_status="off_topic",
            event_type=event_type,
            last_error=reason[:800],
        )
        return get_news(news_id) or row
    already = False
    cluster = item.cluster_id or cluster_id_for(item)
    if cluster:
        already = cluster_published(cluster, str(channel.chat_id), score_key=str(row.get("score_key") or ""))
    hf_ratio = human_factor_share_now(channel.slug)
    # Отбор как content_filter (+ LLM-pick опционально дорого на сотнях новостей).
    # --full усиливает фактчек/web_search на этапе produce, не фильтр.
    verdict = pick_offline(
        item,
        allow_rumors=channel.allow_rumors,
        cluster_already_published=already,
        human_factor_ratio=hf_ratio,
    )
    entities = dict(item.entities or {})
    entities["pick"] = verdict.as_dict()
    if payload:
        entities["topic"] = payload
    status = "skipped"
    err = (f"sim-pool: {verdict.reason}")[:800]
    if verdict.take:
        status = "skipped"
        err = "sim-pool: кандидат в слот"
    else:
        status = "filtered"
        err = (f"filter: {verdict.reason}")[:800]
    update_news(
        news_id,
        status=status,
        topic_status="football",
        event_type=event_type,
        cluster_id=item.cluster_id or cluster_id_for(item),
        entities_json=json.dumps(entities, ensure_ascii=False),
        last_error=err,
        score_key=_score_key({**row, "event_type": event_type}) if event_type == "match_result" else "",
    )
    return get_news(news_id) or row


def human_factor_share_now(slug: str) -> float:
    from editorial.pick import human_factor_share

    return human_factor_share(recent_published(slug, limit=HUMAN_FACTOR_WINDOW))


def is_candidate(row: dict[str, Any]) -> bool:
    if row.get("status") != "skipped":
        return False
    return str(row.get("last_error") or "").startswith("sim-pool: кандидат")


def parse_source_dt(row: dict[str, Any]) -> datetime | None:
    return _parse_dt(row.get("source_published_at"))


def available_at(
    rows: list[dict[str, Any]],
    when: datetime,
    ttl: timedelta,
    *,
    chat_id: str,
) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        if not is_candidate(row):
            continue
        published = parse_source_dt(row)
        if published is None:
            continue
        if published > when:
            continue
        if when - published > ttl:
            continue
        cluster = row.get("cluster_id") or ""
        if cluster and cluster_published(cluster, chat_id, score_key=str(row.get("score_key") or "")):
            if pick_tag_of(row) != "addition":
                continue
        out.append(row)
    return out


def produce(channel, news_id: int, *, full: bool = False, factcheck: bool = False) -> str:
    update_news(news_id, status="verifying", last_error="")
    row = get_news(news_id) or {}
    item = _as_news_item(row)
    if factcheck:
        if full:
            verdict = verify(
                item,
                min_sources=channel.factcheck_min_sources,
                use_llm=True,
                web_search=True,
            )
        else:
            verdict = verify(item, min_sources=1, use_llm=False, web_search=False)
        common = {
            "cluster_id": verdict.cluster_id,
            "factcheck_status": verdict.status.lower(),
            "factcheck_conf": verdict.confidence,
            "factcheck_sources": verdict.unique_domains,
            "factcheck_reason": (verdict.reason or "sim")[:800],
            "score_key": _score_key(row) if (row.get("event_type") == "match_result") else "",
        }
        if verdict.status == "REJECTED":
            update_news(news_id, status="rejected", **common)
            return "rejected"
    else:
        common = {
            "cluster_id": item.cluster_id or cluster_id_for(item),
            "factcheck_status": "skipped",
            "factcheck_conf": 1.0,
            "factcheck_sources": 1,
            "factcheck_reason": "sim: factcheck off",
            "score_key": _score_key(row) if (row.get("event_type") == "match_result") else "",
        }
    update_news(news_id, status="confirmed", **common)
    update_news(news_id, status="editing")
    try:
        status = _step_edit(get_news(news_id) or row)
    except Exception as e:
        print(f"[sim] rewrite fail #{news_id}: {e}", flush=True)
        update_news(news_id, status="held", last_error=f"rewrite: {e}"[:800])
        return "held"
    row = get_news(news_id) or row
    if status != "imaging":
        return status
    status = _step_image(channel, row)
    row = get_news(news_id) or row
    if status != "captioning":
        return status
    status = _step_caption(row)
    row = get_news(news_id) or row
    if status != "rendering":
        return status
    return _step_render(channel, get_news(news_id) or row)


def try_publish(
    channel,
    news_id: int,
    slot: datetime,
    *,
    full: bool = False,
    factcheck: bool = False,
) -> dict[str, Any]:
    try:
        status = produce(channel, news_id, full=full, factcheck=factcheck)
    except Exception as e:
        traceback.print_exc()
        update_news(news_id, status="error", last_error=str(e)[:800])
        return {"action": "error", "id": news_id, "error": str(e)[:200]}
    if status != "ready":
        return {"action": status, "id": news_id}
    row = get_news(news_id)
    if not row:
        return {"action": "missing", "id": news_id}
    stamp = slot.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    res = publish(None, channel, row, published_at=stamp)
    return res


def refresh_rows(ids: list[int]) -> list[dict[str, Any]]:
    rows = []
    for news_id in ids:
        row = get_news(news_id)
        if row:
            rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-08-19")
    parser.add_argument("--slug", default="vnf_editorial")
    parser.add_argument("--no-collect", action="store_true")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Удалить прошлые simulated-посты суток и заново собрать ленту",
    )
    parser.add_argument("--seed", type=int, default=19)
    parser.add_argument(
        "--no-fixtures",
        action="store_true",
        help="Не добавлять утреннюю сетку и результаты матчей",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="(устар.) раньше включал factcheck+web_search; больше не жжёт search-api",
    )
    parser.add_argument(
        "--factcheck",
        action="store_true",
        help="Включить фактчек (дорого: gpt-5-search-api). По умолчанию выкл.",
    )
    parser.add_argument(
        "--export-label",
        action="store_true",
        default=True,
        help="Экспорт опубликованных постов в data/editorial/labeling/day_YYYY-MM-DD/",
    )
    parser.add_argument(
        "--no-export-label",
        action="store_true",
        help="Не экспортировать пул для разметки",
    )
    args = parser.parse_args()

    load_dotenv_manual()
    init_db()
    seed_from_yaml_if_empty()
    reload_editorial_channels()
    cfg = get_channel(args.slug)
    if not cfg:
        raise SystemExit(f"нет канала {args.slug}")
    cfg = replace(cfg, dry_run=True)
    if not cfg.dry_run:
        raise SystemExit("отказ: dry_run выключен")

    was_enabled = _channel_enabled(cfg)
    if not was_enabled and cfg.enabled:
        # прошлый прогон мог оставить паузу воркера
        was_enabled = True
    set_channel_enabled(cfg.slug, False)
    print(f"[sim] paused editorial worker for {cfg.slug} (was_enabled={was_enabled})", flush=True)
    try:
        _run_sim(args, cfg)
    finally:
        set_channel_enabled(cfg.slug, was_enabled)
        print(f"[sim] restored editorial enabled={was_enabled}", flush=True)


def _run_sim(args: argparse.Namespace, cfg) -> None:

    day = date.fromisoformat(args.date)
    start, end = _day_window(day)
    random.seed(args.seed)
    ttl = timedelta(seconds=int(cfg.cadence.item_ttl_sec or 10800))
    print(
        f"[sim] day={day.isoformat()} window={start.isoformat()}..{end.isoformat()} "
        f"slug={cfg.slug} dry_run={cfg.dry_run}",
        flush=True,
    )

    pool_items = load_from_pool(start, end)
    print(f"[sim] pool items: {len(pool_items)}", flush=True)
    live_items: list[NewsItem] = []
    if not args.no_collect:
        try:
            live_items = collect_live(start, end)
            print(f"[sim] live collect: {len(live_items)}", flush=True)
        except Exception as e:
            print(f"[sim] live collect fail, pool only: {e}", flush=True)
    items = merge_items(pool_items, live_items)
    print(f"[sim] unique items: {len(items)}", flush=True)
    if not items:
        raise SystemExit("нет новостей за выбранные сутки")

    if args.reset:
        wiped = reset_sim_day(day, cfg.slug)
        print(
            f"[sim] reset previous run: posts={wiped['posts']} news={wiped['news']} ids={wiped['ids']}",
            flush=True,
        )

    ids: list[int] = []
    for item in items:
        nid = ingest_item(cfg.slug, day, item)
        if nid:
            ids.append(nid)
    print(f"[sim] ingested rows: {len(ids)}", flush=True)
    reset_n = reset_failed_candidates(day)
    print(f"[sim] reset failed candidates: {reset_n}", flush=True)

    taken = 0
    for news_id in ids:
        row = get_news(news_id)
        if not row:
            continue
        if row.get("status") == "published":
            continue
        if is_candidate(row):
            taken += 1
            continue
        if row.get("status") in {"filtered", "off_topic"}:
            continue
        row = filter_item(cfg, row, full=bool(args.full))
        if is_candidate(row):
            taken += 1
    print(f"[sim] filter take: {taken} full={bool(args.full)}", flush=True)

    rows = refresh_rows(ids)
    first_times = [parse_source_dt(r) for r in rows if is_candidate(r)]
    first_times = [t for t in first_times if t is not None]
    if not first_times:
        raise SystemExit("фильтр не оставил ни одного кандидата")
    clock = min(first_times)
    if clock < start:
        clock = start
    next_slot = clock
    published: list[dict[str, Any]] = []
    slot_n = 0

    while next_slot < end:
        slot_n += 1
        rows = refresh_rows(ids)
        pool = available_at(rows, next_slot, ttl, chat_id=str(cfg.chat_id))
        if not pool:
            later = [parse_source_dt(r) for r in rows if is_candidate(r)]
            later = [t for t in later if t and t > next_slot]
            if not later:
                print(f"[sim] slot {slot_n} {next_slot.isoformat()} empty, stop", flush=True)
                break
            next_slot = min(later)
            continue

        prio = [
            r
            for r in pool
            if is_priority(r, cfg) and _SCORE_RE.search(f"{r.get('title') or ''} {r.get('body') or ''}")
        ]
        prio_ids = {int(p["id"]) for p in prio}
        normal = [r for r in pool if int(r["id"]) not in prio_ids]

        for cand in prio:
            news_id = int(cand["id"])
            print(
                f"[sim] produce #{news_id} PRIORITY "
                f"{next_slot.astimezone(MSK).strftime('%d.%m %H:%M')} "
                f"{(cand.get('title') or '')[:90]}",
                flush=True,
            )
            res = try_publish(
                cfg,
                news_id,
                next_slot,
                full=bool(args.full),
                factcheck=bool(args.factcheck),
            )
            print(f"[sim]   -> {res}", flush=True)
            if res.get("action") in {"published", "simulated"}:
                published.append(res)

        remaining = list(normal)
        for _attempt in range(8):
            if not remaining:
                break
            best = pick_best(remaining)
            if not best:
                break
            news_id = int(best["id"])
            remaining = [r for r in remaining if int(r["id"]) != news_id]
            print(
                f"[sim] produce #{news_id} SLOT "
                f"{next_slot.astimezone(MSK).strftime('%d.%m %H:%M')} "
                f"{(best.get('title') or '')[:90]}",
                flush=True,
            )
            res = try_publish(
                cfg,
                news_id,
                next_slot,
                full=bool(args.full),
                factcheck=bool(args.factcheck),
            )
            print(f"[sim]   -> {res}", flush=True)
            if res.get("action") in {"published", "simulated"}:
                published.append(res)
                break

        gap = random_gap_minutes(cfg)
        next_slot = next_slot + timedelta(minutes=gap)

    leftover = 0
    for row in refresh_rows(ids):
        if is_candidate(row):
            leftover += 1
            update_news(
                int(row["id"]),
                status="skipped",
                last_error="sim: не взяли в слот суток",
            )

    if not args.no_fixtures:
        fx = simulate_fixtures(cfg, day)
        md = fx.get("matchday") or {}
        nres = len([r for r in (fx.get("results") or []) if r.get("action") in {"published", "simulated"}])
        print(
            f"[sim] fixtures matchday={md.get('action')} results_posted={nres}",
            flush=True,
        )

    print(
        f"[sim] done published={len(published)} leftover_candidates={leftover} slots={slot_n}",
        flush=True,
    )
    if args.export_label and not args.no_export_label:
        from editorial.day_sim_label import export_day_from_db

        exp = export_day_from_db(day.isoformat(), slug=cfg.slug)
        print(
            f"[sim] label pool: n={exp['n']} → {exp['path']}",
            flush=True,
        )
        print(f"[sim] разметка: /editorial/label-day/{day.isoformat()}", flush=True)
    settings = get_settings()
    print(f"[sim] смотри админку /editorial и ленту источника chat={cfg.chat_id}", flush=True)
    print(f"[sim] db={settings.db_path}", flush=True)


if __name__ == "__main__":
    main()
