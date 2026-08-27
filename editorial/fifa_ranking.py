"""Auto-refresh FIFA men's top-100. Never hardcode Russia into this file."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT, get_settings
from app.db import db, get_meta, set_meta
from app.http_util import http_client
from editorial.catalogs import canonical_team, reload_catalogs
from editorial.store import list_fifa_top100, replace_fifa_top100

FIFA_API = "https://api.fifa.com/api/v3/rankings"
WIKI_RANKING = "https://en.wikipedia.org/wiki/FIFA_Men%27s_World_Ranking"
SOFA_RANKING = "https://api.sofascore.com/api/v1/rankings/fifa/type/2"


def _yaml_path() -> Path:
    settings = get_settings()
    path = Path(settings.fifa_top100_file)
    if path.suffix != ".yaml":
        path = ROOT / "editorial" / "fifa_top100.yaml"
    if not path.is_absolute():
        path = ROOT / path
    return path


def _write_yaml(rows: list[dict[str, Any]]) -> None:
    path = _yaml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": "auto",
        "teams": [
            {
                "rank": int(r["rank"]),
                "team": r["team"],
                "team_ru": r.get("team_ru") or "",
                "points": r.get("points"),
            }
            for r in rows[:100]
        ],
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    reload_catalogs()


def _persist(rows: list[dict[str, Any]], source: str) -> int:
    rows = [r for r in rows if r.get("team") and int(r.get("rank") or 0) >= 1][:100]
    if len(rows) < 50:
        raise RuntimeError(f"слишком мало команд в рейтинге ({len(rows)}) source={source}")
    replace_fifa_top100(rows)
    _write_yaml(rows)
    with db() as conn:
        set_meta(conn, "fifa_top100_refreshed_at", datetime.now(timezone.utc).isoformat())
        set_meta(conn, "fifa_top100_source", source)
    print(f"[editorial] fifa top-100 refreshed via {source}: {len(rows)} teams", flush=True)
    return len(rows)


def _from_fifa_api() -> list[dict[str, Any]]:
    with http_client() as client:
        r = client.get(
            FIFA_API,
            params={"gender": 1, "count": 100, "language": "en-GB"},
        )
        r.raise_for_status()
        data = r.json()
    results = data.get("Results") or []
    rows: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        rank = int(item.get("Rank") or 0)
        if rank < 1 or rank > 100:
            continue
        names = item.get("TeamName") or []
        team = ""
        for block in names:
            if str(block.get("Locale") or "").lower().startswith("en"):
                team = str(block.get("Description") or "").strip()
                break
        if not team and names:
            team = str(names[0].get("Description") or "").strip()
        if not team:
            continue
        points = item.get("DecimalTotalPoints") or item.get("TotalPoints")
        ru = canonical_team(team)
        rows.append(
            {
                "rank": rank,
                "team": team,
                "team_ru": ru if ru != team else "",
                "points": float(points) if points is not None else None,
            }
        )
    return rows


def _from_wikipedia() -> list[dict[str, Any]]:
    with http_client() as client:
        r = client.get(WIKI_RANKING)
        r.raise_for_status()
        html = r.text
    # Current ranking table: rank, team name in a wikitable
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    table = None
    for candidate in soup.select("table.wikitable"):
        header = candidate.get_text(" ", strip=True).lower()
        if "rank" in header and ("team" in header or "nation" in header):
            table = candidate
            break
    if table is None:
        tables = soup.select("table.wikitable")
        table = tables[0] if tables else None
    if table is None:
        raise RuntimeError("wikipedia: нет таблицы рейтинга")
    rows: list[dict[str, Any]] = []
    for tr in table.select("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        rank_m = re.match(r"^(\d{1,3})", cells[0].replace(",", ""))
        if not rank_m:
            continue
        rank = int(rank_m.group(1))
        if rank < 1 or rank > 100:
            continue
        team = cells[1]
        team = re.sub(r"\[.*?\]", "", team).strip()
        if not team or team.lower() in {"team", "nation"}:
            continue
        points = None
        for cell in cells[2:]:
            m = re.search(r"(\d+(?:[.,]\d+)?)", cell.replace("\xa0", " "))
            if m:
                try:
                    points = float(m.group(1).replace(",", ""))
                    break
                except ValueError:
                    pass
        ru = canonical_team(team)
        rows.append({"rank": rank, "team": team, "team_ru": ru if ru != team else "", "points": points})
        if len(rows) >= 100:
            break
    return rows


def _from_sofascore() -> list[dict[str, Any]]:
    with http_client(headers={"User-Agent": "Mozilla/5.0"}) as client:
        r = client.get(SOFA_RANKING)
        r.raise_for_status()
        data = r.json()
    rankings = data.get("rankings") or data.get("teams") or []
    rows: list[dict[str, Any]] = []
    for i, item in enumerate(rankings, start=1):
        if not isinstance(item, dict):
            continue
        team_obj = item.get("team") if isinstance(item.get("team"), dict) else item
        name = str(team_obj.get("name") or team_obj.get("team") or "").strip()
        if not name:
            continue
        rank = int(item.get("ranking") or item.get("rank") or i)
        if rank > 100:
            continue
        rows.append(
            {
                "rank": rank,
                "team": name,
                "team_ru": "",
                "points": item.get("points") or item.get("rankingPoints"),
            }
        )
        if len(rows) >= 100:
            break
    return rows


def refresh_top100(*, force: bool = False) -> dict[str, Any]:
    """Pull ranking and rewrite yaml + DB cache. On failure keep last snapshot."""
    settings = get_settings()
    interval = int(settings.fifa_ranking_refresh_sec or 86400)
    with db() as conn:
        last = get_meta(conn, "fifa_top100_refreshed_at", "")
    if last and not force:
        try:
            prev = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - prev).total_seconds()
            if age < interval:
                return {"action": "skip", "age_sec": int(age)}
        except Exception:
            pass

    backend = (settings.fifa_ranking_backend or "fifa").strip().lower()
    errors: list[str] = []
    order = {
        "fifa": ("fifa", "wikipedia", "sofascore"),
        "sofascore": ("sofascore", "fifa", "wikipedia"),
        "footballdata": ("fifa", "wikipedia", "sofascore"),
        "wikipedia": ("wikipedia", "fifa"),
    }.get(backend, ("fifa", "wikipedia", "sofascore"))

    fetchers = {
        "fifa": _from_fifa_api,
        "wikipedia": _from_wikipedia,
        "sofascore": _from_sofascore,
    }

    for name in order:
        try:
            rows = fetchers[name]()
            n = _persist(rows, name)
            return {"action": "refreshed", "source": name, "count": n}
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"[editorial] fifa ranking {name} fail: {e}", flush=True)

    cached = list_fifa_top100()
    return {
        "action": "stale",
        "error": " | ".join(errors)[:400],
        "cached": len(cached),
    }


def seed_from_yaml_if_empty() -> None:
    if list_fifa_top100():
        return
    path = _yaml_path()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    teams = (raw or {}).get("teams") if isinstance(raw, dict) else None
    if not isinstance(teams, list) or not teams:
        return
    rows: list[dict[str, Any]] = []
    for item in teams:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "rank": int(item.get("rank") or 0),
                "team": str(item.get("team") or ""),
                "team_ru": str(item.get("team_ru") or ""),
                "points": item.get("points"),
            }
        )
    if rows:
        replace_fifa_top100(rows)
        print(f"[editorial] fifa top-100 seeded from yaml: {len(rows)}", flush=True)


def fifa_name_set() -> set[str]:
    from editorial.catalogs import load_fifa_top100_names, norm_name

    names = set(load_fifa_top100_names())
    for row in list_fifa_top100():
        names.add(norm_name(row.get("team") or ""))
        if row.get("team_ru"):
            names.add(norm_name(row["team_ru"]))
    return {n for n in names if n}
