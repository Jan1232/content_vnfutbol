"""Временная разметка фото: pool.json (модель) + labels.json (человек) + правила."""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT

LABEL_DIR = ROOT / "data" / "editorial" / "labeling" / "imagery_4d"
POOL_PATH = LABEL_DIR / "pool.json"
LABELS_PATH = LABEL_DIR / "labels.json"
COMPARISON_PATH = LABEL_DIR / "comparison.json"
PHOTOS_DIR = LABEL_DIR / "photos"
FITTED_DIR = LABEL_DIR / "fitted"
IMAGES_DIR = ROOT / "data" / "editorial" / "images"
RULES_PATH = ROOT / "editorial" / "rules_imagery.yaml"

_CHAMP_KEY = re.compile(r"news/big/(.+?)(?:\?|$)", re.I)


def _dump(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_pool() -> dict[str, Any]:
    if not POOL_PATH.is_file():
        return {"kind": "imagery_calibration", "items": [], "stats": {}}
    return json.loads(POOL_PATH.read_text(encoding="utf-8"))


def load_labels() -> dict[str, Any]:
    if not LABELS_PATH.is_file():
        return {
            "kind": "imagery_human_labels",
            "updated_at": "",
            "items": {},
        }
    return json.loads(LABELS_PATH.read_text(encoding="utf-8"))


def pool_items() -> list[dict[str, Any]]:
    return list((load_pool().get("items") or []))


def story_key(item: dict[str, Any]) -> str:
    """Один сюжет = одна карточка разметки. Не прод-кластер, а «та же фотозадача»."""
    from editorial.catalogs import canonical_team, norm_name

    news = item.get("news") or {}
    title = str(news.get("title") or "")
    query = str((item.get("model") or {}).get("query") or "")
    blob = norm_name(f"{title} {query}")
    et = str(news.get("event_type") or "other")
    players = [
        norm_name(canonical_team(str(p)))
        for p in ((news.get("entities") or {}).get("players") or [])
        if p
    ]
    if (
        "франц" in blob
        and (
            "суперкубок" in blob
            or "суперкубке" in blob
            or ("ланс" in blob and ("псж" in blob or "сафонов" in blob or "антоньо" in blob))
        )
    ):
        return "story:lens_psg_sc"
    if ("суперкубок" in blob or "суперкубке" in blob) and "англ" in blob:
        return "story:arsenal_cs"
    if "батраков" in blob:
        return "story:batrakov"
    if "родри" in blob and any(x in blob for x in ("барселон", "сити", "манчестер")):
        return "story:rodri_barca"
    if "мусиала" in blob:
        return "story:musiala"
    if "моуринью" in blob and "синяк" in blob:
        return "story:mourinho_eye"
    if "колосков" in blob:
        return "story:koloskov"
    if "лукуми" in blob:
        return "story:lucumi"
    if "викарио" in blob:
        return "story:vicario"
    if ("мартинес" in blob or "эми" in blob) and ("ювентус" in blob or "юве" in blob):
        return "story:emi_martinez"
    if "константелиас" in blob:
        return "story:konstantelias"
    if et in {"transfer", "injury"} and players:
        return f"{et}:{players[0]}"
    toks = [t for t in norm_name(query or title).split() if t not in {"фото", "2026"}]
    return "q:" + " ".join(toks[:4])


def dedupe_pool(*, backup: bool = True) -> dict[str, Any]:
    """Оставить по одному сюжету. Уже размеченные карточки не трогаем."""
    payload = load_pool()
    items = list(payload.get("items") or [])
    labeled_ids = set((load_labels().get("items") or {}).keys())
    if backup and POOL_PATH.is_file():
        bak = LABEL_DIR / "pool_before_story_dedupe.json"
        if not bak.is_file():
            shutil.copy2(POOL_PATH, bak)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in items:
        groups[story_key(row)].append(row)
    keep_ids: set[str] = set()
    dropped = 0
    for rows in groups.values():
        marked = [r for r in rows if str(r.get("id")) in labeled_ids]
        rest = [r for r in rows if str(r.get("id")) not in labeled_ids]
        for r in marked:
            keep_ids.add(str(r.get("id")))
        if marked:
            dropped += len(rest)
            continue
        rest.sort(key=lambda r: (0 if r.get("photo") else 1, str(r.get("id") or "")))
        if rest:
            keep_ids.add(str(rest[0].get("id")))
            dropped += len(rest) - 1
    kept = [r for r in items if str(r.get("id")) in keep_ids]
    stats = dict(payload.get("stats") or {})
    stats["take"] = len(kept)
    stats["done"] = len(kept)
    stats["picked"] = sum(1 for r in kept if r.get("photo"))
    stats["held"] = sum(1 for r in kept if not r.get("photo"))
    stats["deduped"] = dropped
    payload["items"] = kept
    payload["stats"] = stats
    payload["window"] = dict(payload.get("window") or {})
    payload["window"]["deduped"] = True
    _dump(payload, POOL_PATH)
    return {"kept": len(kept), "dropped": dropped, "labeled": len(labeled_ids & keep_ids)}


def item_by_id(item_id: str) -> dict[str, Any] | None:
    for row in pool_items():
        if str(row.get("id")) == str(item_id):
            return row
    return None


def _cand_key(url: str) -> str:
    raw = (url or "").split("?")[0]
    m = _CHAMP_KEY.search(raw)
    if m:
        return "champ:" + m.group(1)
    name = raw.rsplit("/", 1)[-1]
    return name or raw


def _same_url(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return _cand_key(a) == _cand_key(b) or a.split("?")[0] == b.split("?")[0]


def _safe_under(path: Path, *roots: Path) -> Path | None:
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return resolved
        except ValueError:
            continue
    return None


def photo_file_for_item(item: dict[str, Any]) -> Path | None:
    rel = ((item.get("photo") or {}) or {}).get("file") or ""
    if not rel:
        return None
    return _safe_under(LABEL_DIR / rel, LABEL_DIR)


def fit_photo_to_template(src: Path, dest: Path, template: str) -> Path:
    """Тот же smart_crop, что перед PNG на проде: cover в размер шаблона + лица."""
    from editorial.imagery import _template_size, smart_crop

    tw, th = _template_size(template)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return smart_crop(src, tw, th, template=template, dest=dest)


def source_for_fitted(item: dict[str, Any], idx: int | None) -> Path | None:
    if idx is None:
        return photo_file_for_item(item)
    for cand in (item.get("model") or {}).get("vision") or []:
        try:
            if int(cand.get("idx")) == int(idx):
                return cand_file(cand)
        except (TypeError, ValueError):
            continue
    return None


def ensure_fitted(item: dict[str, Any], idx: int | None = None) -> Path | None:
    src = source_for_fitted(item, idx)
    if not src:
        return None
    template = str(card_for_item(item).get("template") or "default")
    key = "pick" if idx is None else str(int(idx))
    dest = FITTED_DIR / f"{item.get('id')}_{key}_{template}.jpg"
    if dest.is_file() and dest.stat().st_size > 1000:
        return dest
    try:
        return fit_photo_to_template(src, dest, template)
    except Exception as e:
        print(f"[label] fit fail {item.get('id')} idx={idx}: {e}", flush=True)
        return None


def cand_file(cand: dict[str, Any]) -> Path | None:
    raw = cand.get("path") or ""
    if not raw:
        return None
    return _safe_under(Path(raw), IMAGES_DIR, LABEL_DIR)


def unique_candidates(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Один кадр на сюжет: Championat 900/1200/big не дублируем в сетке."""
    photo = item.get("photo") or {}
    pick_url = str(photo.get("source_url") or "")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for cand in (item.get("model") or {}).get("vision") or []:
        url = str(cand.get("url") or "")
        key = _cand_key(url)
        if key in seen:
            continue
        seen.add(key)
        idx = int(cand.get("idx") or 0)
        is_pick = bool(pick_url and _same_url(url, pick_url))
        out.append(
            {
                "idx": idx,
                "url": url,
                "via": cand.get("via") or "",
                "kept": bool(cand.get("kept")),
                "score": cand.get("score"),
                "who": cand.get("who") or "",
                "reason": cand.get("reason") or "",
                "wrong_subject": bool(cand.get("wrong_subject")),
                "is_pick": is_pick,
                "has_file": cand_file(cand) is not None,
            }
        )
    if pick_url and not any(c["is_pick"] for c in out):
        out.insert(
            0,
            {
                "idx": -1,
                "url": pick_url,
                "via": photo.get("via") or "",
                "kept": True,
                "score": photo.get("score"),
                "who": ((item.get("model") or {}).get("thought") or {}).get("who") or "",
                "reason": ((item.get("model") or {}).get("thought") or {}).get("reason") or "",
                "wrong_subject": False,
                "is_pick": True,
                "has_file": photo_file_for_item(item) is not None,
            },
        )
    return out


def progress() -> dict[str, int]:
    items = pool_items()
    labels = load_labels().get("items") or {}
    done = sum(1 for row in items if str(row.get("id")) in labels)
    return {"total": len(items), "done": done, "left": max(0, len(items) - done)}


def next_unlabeled(after_id: str = "") -> dict[str, Any] | None:
    labels = load_labels().get("items") or {}
    items = pool_items()
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


def neighbor_ids(item_id: str) -> tuple[str | None, str | None]:
    ids = [str(r.get("id")) for r in pool_items()]
    try:
        i = ids.index(item_id)
    except ValueError:
        return None, None
    prev_id = ids[i - 1] if i > 0 else None
    next_id = ids[i + 1] if i + 1 < len(ids) else None
    return prev_id, next_id


def apply_decision(
    item: dict[str, Any],
    *,
    decision: str,
    chosen_idx: int | None,
    note: str = "",
    better_query: str = "",
) -> dict[str, Any]:
    photo = item.get("photo") or {}
    model_url = str(photo.get("source_url") or "")
    model_query = str((item.get("model") or {}).get("query") or "")
    thought = ((item.get("model") or {}).get("thought") or {})
    cands = {(c.get("idx")): c for c in unique_candidates(item)}
    chosen = cands.get(chosen_idx) if chosen_idx is not None else None
    human_query = " ".join((better_query or "").split())
    if human_query.casefold() == " ".join(model_query.split()).casefold():
        human_query = ""
    if decision == "none":
        rec = {
            "id": item.get("id"),
            "decision": "none",
            "keep_photo": False,
            "better_idx": None,
            "chosen_url": "",
            "chosen_via": "",
            "model_url": model_url,
            "model_outcome": (item.get("model") or {}).get("outcome") or "",
            "agree": False,
            "note": (note or "ни одна не подходит").strip(),
        }
    else:
        if chosen and not chosen.get("is_pick"):
            rec = {
                "id": item.get("id"),
                "decision": "accept_other",
                "keep_photo": False,
                "better_idx": int(chosen["idx"]),
                "chosen_url": chosen.get("url") or "",
                "chosen_via": chosen.get("via") or "",
                "model_url": model_url,
                "model_outcome": (item.get("model") or {}).get("outcome") or "",
                "agree": False,
                "note": (note or chosen.get("reason") or "").strip(),
            }
        else:
            rec = {
                "id": item.get("id"),
                "decision": "accept_model",
                "keep_photo": True,
                "better_idx": None,
                "chosen_url": model_url,
                "chosen_via": photo.get("via") or "",
                "model_url": model_url,
                "model_outcome": (item.get("model") or {}).get("outcome") or "",
                "agree": True,
                "note": (note or thought.get("reason") or "ок").strip(),
            }
    rec["model_query"] = model_query
    rec["better_query"] = human_query
    rec["ts"] = datetime.now(timezone.utc).isoformat()
    return rec


def save_label(rec: dict[str, Any]) -> dict[str, Any]:
    payload = load_labels()
    items = dict(payload.get("items") or {})
    items[str(rec["id"])] = rec
    payload["kind"] = "imagery_human_labels"
    payload["updated_at"] = rec.get("ts") or datetime.now(timezone.utc).isoformat()
    payload["items"] = items
    payload["done"] = len(items)
    _dump(payload, LABELS_PATH)
    if progress()["left"] == 0:
        write_comparison_and_rules()
    return payload


def write_comparison_and_rules() -> dict[str, Any]:
    pool = load_pool()
    labels = load_labels().get("items") or {}
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    other_via: Counter[str] = Counter()
    none_notes: Counter[str] = Counter()
    model_via_when_none: Counter[str] = Counter()
    query_fixes: list[dict[str, str]] = []
    for item in pool.get("items") or []:
        lid = str(item.get("id"))
        lab = labels.get(lid)
        if not lab:
            continue
        decision = str(lab.get("decision") or "")
        counts[decision] += 1
        if decision == "accept_other":
            other_via[str(lab.get("chosen_via") or "?")] += 1
        if decision == "none":
            none_notes[str(lab.get("note") or "ни одна")[:80]] += 1
            model_via_when_none[str((item.get("photo") or {}).get("via") or lab.get("model_outcome") or "?")] += 1
        model_query = str(lab.get("model_query") or (item.get("model") or {}).get("query") or "")
        better_query = str(lab.get("better_query") or "").strip()
        if better_query:
            query_fixes.append({"id": lid, "from": model_query, "to": better_query})
        rows.append(
            {
                "id": lid,
                "title": (item.get("news") or {}).get("title") or "",
                "model_url": lab.get("model_url") or "",
                "model_outcome": lab.get("model_outcome") or "",
                "model_query": model_query,
                "better_query": better_query,
                "human": decision,
                "better_idx": lab.get("better_idx"),
                "agree": bool(lab.get("agree")),
                "note": lab.get("note") or "",
            }
        )
    total = max(1, len(rows))
    agree_n = counts["accept_model"]
    comparison = {
        "kind": "imagery_comparison",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "n": len(rows),
        "agree": agree_n,
        "agree_pct": round(100.0 * agree_n / total, 1),
        "accept_other": counts["accept_other"],
        "none": counts["none"],
        "query_fixes": len(query_fixes),
        "other_via": dict(other_via),
        "files": {
            "model": str(POOL_PATH),
            "human": str(LABELS_PATH),
        },
        # rows — для Jinja: у dict есть метод .items, ключ items в шаблоне ломает сводку.
        "items": rows,
        "rows": rows,
    }
    _dump(comparison, COMPARISON_PATH)
    _write_rules(comparison, none_notes, other_via, model_via_when_none, query_fixes)
    return comparison


def _write_rules(
    comparison: dict[str, Any],
    none_notes: Counter[str],
    other_via: Counter[str],
    model_via_when_none: Counter[str],
    query_fixes: list[dict[str, str]] | None = None,
) -> None:
    learned: list[dict[str, Any]] = []
    n = int(comparison.get("n") or 0)
    none_n = int(comparison.get("none") or 0)
    other_n = int(comparison.get("accept_other") or 0)
    if n:
        learned.append(
            {
                "id": "agreement",
                "when": "сводка после разметки",
                "action": (
                    f"согласие с моделью {comparison.get('agree_pct')}% "
                    f"(n={n}, other={other_n}, none={none_n})"
                ),
                "evidence": [str(COMPARISON_PATH)],
            }
        )
    if query_fixes:
        learned.append(
            {
                "id": "search_query",
                "when": f"человек дал другой поисковый запрос ({len(query_fixes)} раз)",
                "action": (
                    "строить query как человек: герой новости + клуб; "
                    "цитату не искать — только автор (+ его клуб); "
                    "не страна вместо клуба, не турнир без игрока."
                ),
                "evidence": [f"{x.get('from','')} → {x.get('to','')}" for x in query_fixes[:12]],
            }
        )
    if other_via:
        top_via = other_via.most_common(1)[0]
        learned.append(
            {
                "id": "prefer_source_when_human_overrides",
                "when": "человек берёт другой кадр, не выбор модели",
                "action": (
                    f"чаще выбирают via={top_via[0]} ({top_via[1]} раз). "
                    "В поиске поднимать этот источник выше и не отдавать vision первый попавшийся kept."
                ),
                "evidence": [f"{k}:{v}" for k, v in other_via.most_common(5)],
            }
        )
    if none_n and none_n / max(n, 1) >= 0.15:
        learned.append(
            {
                "id": "none_too_often",
                "when": f"человек жмёт «ни одна» в {none_n}/{n} карточках",
                "action": (
                    "ужесточить vision: relevant=false при чужом клубе, коллаже, тренере вместо игрока, "
                    "гербе/лого. Запрос только игрок+клуб из заголовка, не первая сущность."
                ),
                "evidence": [f"{k}:{v}" for k, v in none_notes.most_common(8)],
            }
        )
    if model_via_when_none.get("yandex", 0) >= 3:
        learned.append(
            {
                "id": "yandex_offtopic_pick",
                "when": "модель взяла Яндекс, человек отверг все кадры",
                "action": "если article дал 900×900/1200×900 Championat — не брать yandex выше article",
                "evidence": [f"yandex_none={model_via_when_none.get('yandex')}"],
            }
        )
    learned.append(
        {
            "id": "quote_search_author",
            "when": "заголовок — цитата героя",
            "action": "query = автор + его клуб, не текст цитаты. Пример: Диаш Ман Сити, не «Многое меняется…».",
            "evidence": [x.get("to", "") for x in (query_fixes or []) if x.get("to") in {"Диаш Ман Сити", "Микель Артета", "Тюкавин"}],
        }
    )
    learned.append(
        {
            "id": "no_overlay_text",
            "when": "на фото читаемый текст",
            "action": "relevant=false, кроме логотипа/эмблемы клуба и Here we go. Остальной текст на кадре — мимо.",
            "evidence": ["калибровка: «Все фото низкого качества или есть текст»"],
        }
    )
    payload = {
        "version": 1,
        "source": "data/editorial/labeling/imagery_4d/labels.json",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "local_gates": {
            "no_wikimedia": True,
            "no_bing": True,
            "search": "yandex",
            "championat_prefer_900x900": True,
            "skip_crests_and_favicons": True,
            "max_upscale": 1.75,
            "max_dark_ratio": 0.55,
            "min_sharpness": 100,
            "query_prefer_club_over_country": True,
        },
        "learned": learned,
        "notes": [
            "Промпт vision: wrong_subject=true если человек/клуб не из заголовка.",
            "Не выбирать кадр только потому что kept=true — нужен максимальный score среди subject_present.",
            "Цитата в заголовке: query = автор + клуб, не текст цитаты.",
            "Текст на фото (кроме логотипа клуба и Here we go) — relevant=false.",
        ],
    }
    RULES_PATH.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def card_for_item(item: dict[str, Any]) -> dict[str, Any]:
    from editorial.channel_config import get_channel
    from editorial.cover_text import clip_to_cover
    from editorial.render import BADGE_FOR_EVENT, _VIEWPORTS

    cfg = get_channel("vnf_editorial")
    event = str((item.get("news") or {}).get("event_type") or "other")
    template = cfg.template_for(event) if cfg else "default"
    width, height = _VIEWPORTS.get(template) or (1080, 1080)
    title = str((item.get("news") or {}).get("title") or "")
    return {
        "template": template,
        "width": width,
        "height": height,
        "caption": clip_to_cover(title),
        "badge": BADGE_FOR_EVENT.get(event, "НОВОСТЬ"),
        "brand": {
            "name": (cfg.brand.name if cfg else "") or "ВСЕ НА ФУТБОЛ",
            "accent_color": (cfg.brand.accent_color if cfg else "") or "#E11D2A",
            "cover_handle": (cfg.brand.cover_handle if cfg else "") or "@channel_vnfutbol",
        },
    }


def preview_card_html(item: dict[str, Any], *, photo_url: str) -> str:
    from editorial.render import preview_html

    card = card_for_item(item)
    return preview_html(
        card["template"],
        photo_url,
        card["caption"],
        None,
        card["badge"],
        card["brand"],
    )


def view_item(item: dict[str, Any]) -> dict[str, Any]:
    news = item.get("news") or {}
    photo = item.get("photo") or {}
    model = item.get("model") or {}
    thought = model.get("thought") or {}
    prev_id, next_id = neighbor_ids(str(item.get("id")))
    labeled = (load_labels().get("items") or {}).get(str(item.get("id")))
    return {
        "id": item.get("id"),
        "news": news,
        "photo": photo,
        "thought": thought,
        "query": model.get("query") or "",
        "outcome": model.get("outcome") or "",
        "candidates": unique_candidates(item),
        "has_photo": bool(photo_file_for_item(item)),
        "prev_id": prev_id,
        "next_id": next_id,
        "labeled": labeled,
        "progress": progress(),
        "card": card_for_item(item),
    }
