"""football-data.org fixtures client + hyped-match picker (AI)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings

MSK = ZoneInfo("Europe/Moscow")
SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"

# Prestige 0–100 (эвристика; основа ранга пары).
_CLUB_PRESTIGE = {
    "real madrid": 100,
    "barcelona": 98,
    "bayern": 96,
    "manchester city": 95,
    "liverpool": 94,
    "psg": 93,
    "paris saint": 93,
    "arsenal": 90,
    "manchester united": 89,
    "chelsea": 88,
    "inter": 87,
    "juventus": 86,
    "milan": 85,
    "napoli": 84,
    "atletico": 83,
    "dortmund": 82,
    "tottenham": 78,
    "benfica": 76,
    "porto": 74,
    "ajax": 73,
    "leverkusen": 72,
    "leipzig": 70,
    "lyon": 69,
    "olympique lyonnais": 69,
    "fenerbahce": 68,
    "fenerbahçe": 68,
    "aston villa": 68,
    "newcastle": 67,
    "marseille": 65,
    "monaco": 64,
    "sporting": 63,
    "galatasaray": 62,
    "besiktas": 61,
    "beşiktaş": 61,
    "celtic": 60,
    "roma": 70,
    "lazio": 66,
    "fiorentina": 64,
    "atalanta": 67,
    "torino": 52,
    "bologna": 55,
    "genoa": 48,
    "salzburg": 58,
    "rb salzburg": 58,
    "anderlecht": 54,
    "dinamo zagreb": 56,
    "dynamo zagreb": 56,
    "red star": 57,
    "crvena": 57,
    "ferencvaros": 53,
    "ferencváros": 53,
    "trabzonspor": 52,
    "plzen": 50,
    "viktoria": 50,
    "aek": 49,
    "bodø": 48,
    "bodo": 48,
    "glimt": 48,
    "kairat": 42,
    "monza": 40,
    "como": 38,
    "udinese": 45,
    "lecce": 36,
    "sassuolo": 40,
    "parma": 42,
    "cagliari": 40,
    "venezia": 35,
    "frosinone": 34,
    "lask": 44,
    "viking": 38,
    "levski": 40,
    "aarus": 30,
    "agf": 30,
    "aarhus": 30,
    # Premier League mid/lower
    "brighton": 58,
    "west ham": 57,
    "wolves": 54,
    "wolverhampton": 54,
    "fulham": 53,
    "brentford": 52,
    "crystal palace": 51,
    "everton": 50,
    "bournemouth": 49,
    "nottingham": 48,
    "forest": 48,
    "leicester": 47,
    "leeds": 46,
    "ipswich": 40,
    "sunderland": 39,
    "hull": 36,
    "coventry": 37,
    # La Liga mid/lower
    "sevilla": 72,
    "villarreal": 68,
    "athletic": 66,
    "real sociedad": 65,
    "sociedad": 65,
    "betis": 64,
    "girona": 60,
    "valencia": 58,
    "celta": 52,
    "osasuna": 50,
    "getafe": 48,
    "mallorca": 47,
    "rayo": 46,
    "alaves": 44,
    "alavés": 44,
    "espanyol": 45,
    "las palmas": 42,
    "cadiz": 40,
    "cádiz": 40,
    "elche": 38,
    "levante": 40,
    "malaga": 36,
    "málaga": 36,
    "racing": 35,
    "deportivo": 37,
    "leganes": 39,
    "leganés": 39,
    "valladolid": 38,
}

_STAGE_PRIORITY = {
    "FINAL": 100,
    "THIRD_PLACE": 90,
    "SEMI_FINALS": 80,
    "SEMI_FINAL": 80,
    "QUARTER_FINALS": 70,
    "QUARTER_FINAL": 70,
    "LAST_16": 60,
    "ROUND_OF_16": 60,
    "PLAYOFFS": 50,
    "PLAY_OFF_ROUND": 50,
    "LEAGUE_STAGE": 25,
    "GROUP_STAGE": 20,
    "REGULAR_SEASON": 15,
}


@dataclass(frozen=True)
class Match:
    match_id: str
    competition: str
    competition_name: str
    home_team: str
    away_team: str
    utc_date: datetime
    status: str
    stage: str = ""
    matchday: int | None = None
    home_logo_url: str = ""
    away_logo_url: str = ""

    @property
    def kickoff_msk(self) -> datetime:
        return self.utc_date.astimezone(MSK)

    @property
    def is_finished(self) -> bool:
        return (self.status or "").upper() in {"FINISHED", "AWARDED"}

    @property
    def is_live(self) -> bool:
        return (self.status or "").upper() in {"IN_PLAY", "PAUSED", "LIVE"}

    @property
    def is_qualifying(self) -> bool:
        name = (self.competition_name or "").casefold()
        stage = (self.stage or "").upper()
        if "qual" in name:
            return True
        return stage in {
            "PLAYOFFS",
            "PLAY_OFF_ROUND",
            "QUALIFYING",
            "QUALIFICATION",
            "PRELIMINARY_ROUND",
            "1ST_QUALIFYING_ROUND",
            "2ND_QUALIFYING_ROUND",
            "3RD_QUALIFYING_ROUND",
        }


class FootballDataError(RuntimeError):
    pass


def _parse_utc(raw: str) -> datetime:
    s = (raw or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _match_from_api(item: dict[str, Any]) -> Match | None:
    try:
        mid = item.get("id")
        home = (item.get("homeTeam") or {}).get("name") or ""
        away = (item.get("awayTeam") or {}).get("name") or ""
        utc = item.get("utcDate") or ""
        if not mid or not home or not away or not utc:
            return None
        comp = item.get("competition") or {}
        home_logo = str((item.get("homeTeam") or {}).get("crest") or "").strip()
        away_logo = str((item.get("awayTeam") or {}).get("crest") or "").strip()
        return Match(
            match_id=str(mid),
            competition=str(comp.get("code") or ""),
            competition_name=str(comp.get("name") or ""),
            home_team=home.strip(),
            away_team=away.strip(),
            utc_date=_parse_utc(utc),
            status=str(item.get("status") or ""),
            stage=str(item.get("stage") or ""),
            matchday=item.get("matchday"),
            home_logo_url=home_logo,
            away_logo_url=away_logo,
        )
    except Exception:
        return None


def _respect_rate_limit(headers: httpx.Headers) -> None:
    """football-data: смотрим X-Requests-Available-Minute / X-RequestCounter-Reset."""
    try:
        available = int(headers.get("X-Requests-Available-Minute") or "99")
    except ValueError:
        available = 99
    try:
        reset_sec = int(headers.get("X-RequestCounter-Reset") or "0")
    except ValueError:
        reset_sec = 0
    if available <= 1 and reset_sec > 0:
        sleep_for = min(max(reset_sec, 1), 60)
        print(f"[seo] football-data throttle: sleep {sleep_for}s (avail={available})", flush=True)
        time.sleep(sleep_for)


def fetch_competition_matches(
    code: str,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    statuses: list[str] | None = None,
) -> list[Match]:
    settings = get_settings()
    token = (settings.football_data_token or "").strip()
    if not token:
        raise FootballDataError(
            "FOOTBALL_DATA_TOKEN не задан. "
            "Бесплатный ключ: https://www.football-data.org/client/register"
        )
    base = (settings.football_data_base or "https://api.football-data.org/v4").rstrip("/")
    now = datetime.now(timezone.utc)
    d0 = (date_from or (now - timedelta(days=1))).date().isoformat()
    d1 = (date_to or (now + timedelta(days=21))).date().isoformat()
    params: dict[str, str] = {"dateFrom": d0, "dateTo": d1}
    if statuses:
        params["status"] = ",".join(statuses)

    headers = {"X-Auth-Token": token}
    url = f"{base}/competitions/{code.upper()}/matches"
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url, params=params, headers=headers)
        _respect_rate_limit(r.headers)
        if r.status_code == 429:
            reset_sec = int(r.headers.get("X-RequestCounter-Reset") or "60")
            time.sleep(min(max(reset_sec, 5), 90))
            r = client.get(url, params=params, headers=headers)
            _respect_rate_limit(r.headers)
        if r.status_code == 403:
            raise FootballDataError(
                f"football-data: соревнование {code} недоступно на free-тарифе (403)"
            )
        if r.status_code >= 400:
            raise FootballDataError(f"football-data {r.status_code}: {r.text[:300]}")
        data = r.json()

    out: list[Match] = []
    for item in data.get("matches") or []:
        m = _match_from_api(item)
        if m:
            out.append(m)
    return out


# ESPN public scoreboard paths for cups not on football-data free tier.
# EL/CL/WC/EC = основной турнир + квалификация где есть.
_ESPN_PATHS = {
    "EL": ("uefa.europa_qual", "uefa.europa"),
    "CL": ("uefa.champions_qual", "uefa.champions"),
    "WC": ("fifa.worldq", "fifa.world"),
    "EC": ("uefa.euroq", "uefa.euro"),
    "CA": ("conmebol.america",),
    # Товарищеские матчи сборных
    "INT": ("fifa.friendly",),
    "SA": ("ita.1",),
    "PL": ("eng.1",),
    "PD": ("esp.1",),
}

_ESPN_LABELS = {
    ("EL", False): "UEFA Europa League",
    ("EL", True): "UEFA Europa League Qualifying",
    ("CL", False): "UEFA Champions League",
    ("CL", True): "UEFA Champions League Qualifying",
    ("WC", False): "FIFA World Cup",
    ("WC", True): "FIFA World Cup Qualifying",
    ("EC", False): "UEFA European Championship",
    ("EC", True): "UEFA European Championship Qualifying",
    ("CA", False): "Copa América",
    ("CA", True): "Copa América Qualifying",
    ("INT", False): "International Friendly",
    ("INT", True): "International Friendly",
    ("SA", False): "Serie A",
    ("SA", True): "Serie A",
    ("PL", False): "Premier League",
    ("PL", True): "Premier League",
    ("PD", False): "La Liga",
    ("PD", True): "La Liga",
}


def _espn_status(name: str) -> str:
    n = (name or "").upper()
    if "FULL" in n or "FINAL" in n:
        return "FINISHED"
    if "IN_PROGRESS" in n or "HALFTIME" in n or "LIVE" in n:
        return "IN_PLAY"
    if "SCHEDULED" in n or "STATUS_SCHEDULED" in n or "PRE" in n:
        return "TIMED"
    return "SCHEDULED"


def fetch_espn_competition_matches(
    code: str,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[Match]:
    """ESPN site API — бесплатно, без ключа. Для EL / CL (в т.ч. квалификация)."""
    paths = _ESPN_PATHS.get(code.upper())
    if not paths:
        raise FootballDataError(f"ESPN: нет маппинга для {code}")
    now = datetime.now(timezone.utc)
    d0 = date_from or (now - timedelta(days=1))
    d1 = date_to or (now + timedelta(days=60))
    dates = f"{d0.strftime('%Y%m%d')}-{d1.strftime('%Y%m%d')}"

    out: list[Match] = []
    seen: set[str] = set()
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        for path in paths:
            url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{path}/scoreboard"
            r = client.get(url, params={"dates": dates, "limit": 200})
            if r.status_code >= 400:
                print(f"[seo] ESPN {path}: {r.status_code}", flush=True)
                continue
            data = r.json()
            is_qual = "qual" in path or path.endswith("q")
            label = _ESPN_LABELS.get(
                (code.upper(), is_qual),
                _ESPN_LABELS.get((code.upper(), False), code),
            )
            for ev in data.get("events") or []:
                try:
                    eid = str(ev.get("id") or "")
                    if not eid or eid in seen:
                        continue
                    utc = _parse_utc(ev.get("date") or "")
                    st = _espn_status(
                        ((ev.get("status") or {}).get("type") or {}).get("name") or ""
                    )
                    comps = ev.get("competitions") or []
                    comp0 = comps[0] if comps else {}
                    teams = comp0.get("competitors") or []
                    home = next((t for t in teams if t.get("homeAway") == "home"), None)
                    away = next((t for t in teams if t.get("homeAway") == "away"), None)
                    if not home or not away:
                        continue
                    hname = (
                        (home.get("team") or {}).get("displayName")
                        or (home.get("team") or {}).get("name")
                        or ""
                    ).strip()
                    aname = (
                        (away.get("team") or {}).get("displayName")
                        or (away.get("team") or {}).get("name")
                        or ""
                    ).strip()
                    if not hname or not aname:
                        continue
                    hlogo = str((home.get("team") or {}).get("logo") or "").strip()
                    alogo = str((away.get("team") or {}).get("logo") or "").strip()
                    seen.add(eid)
                    out.append(
                        Match(
                            match_id=f"espn:{eid}",
                            competition=code.upper(),
                            competition_name=label,
                            home_team=hname,
                            away_team=aname,
                            utc_date=utc,
                            status=st,
                            stage="PLAYOFFS" if is_qual else "",
                            matchday=None,
                            home_logo_url=hlogo,
                            away_logo_url=alogo,
                        )
                    )
                except Exception:
                    continue
    if not out:
        return out
    return out


def fetch_matches_for_competitions(
    codes: list[str] | tuple[str, ...],
    *,
    horizon_days: int = 14,
    providers: list[str] | tuple[str, ...] = ("auto",),
) -> list[Match]:
    now = datetime.now(timezone.utc)
    date_from = now - timedelta(days=1)
    date_to = now + timedelta(days=max(1, horizon_days))
    providers_cf = [p.lower() for p in (providers or ("auto",))]
    all_matches: list[Match] = []

    for code in codes:
        code_u = str(code).upper()
        got = False
        code_errors: list[str] = []

        use_fd = "football-data" in providers_cf or "auto" in providers_cf
        use_espn = "espn" in providers_cf or "auto" in providers_cf
        # INT нет в football-data; CA/EL на free часто 403 — ESPN покрывает.
        if code_u in {"INT"} and "football-data" not in providers_cf:
            use_fd = False

        if use_fd:
            try:
                batch = fetch_competition_matches(
                    code_u,
                    date_from=date_from,
                    date_to=date_to,
                    statuses=[
                        "SCHEDULED",
                        "TIMED",
                        "IN_PLAY",
                        "PAUSED",
                        "FINISHED",
                    ],
                )
                all_matches.extend(batch)
                got = True
                time.sleep(0.35)
            except FootballDataError as e:
                code_errors.append(str(e))
                print(f"[seo] football-data {code_u}: {e}", flush=True)

        # ESPN: для EL/CL/INT всегда тянем, даже если FD что-то отдал
        if use_espn and code_u in _ESPN_PATHS:
            try:
                batch = fetch_espn_competition_matches(
                    code_u, date_from=date_from, date_to=date_to
                )
                # dedupe by match_id
                have = {m.match_id for m in all_matches}
                for m in batch:
                    if m.match_id not in have:
                        all_matches.append(m)
                if batch:
                    got = True
            except FootballDataError as e:
                code_errors.append(str(e))
                print(f"[seo] ESPN {code_u}: {e}", flush=True)

        if not got:
            print(
                f"[seo] no source for {code_u}: {'; '.join(code_errors)[:200]}",
                flush=True,
            )

    return all_matches


def candidate_matches(
    matches: list[Match],
    *,
    horizon_days: int = 14,
    post_match_grace_min: int = 120,
    now: datetime | None = None,
) -> list[Match]:
    """Окно афиши: live / ещё не сыгранные / недавно в grace. Без сортировки по близости."""
    now = now or datetime.now(timezone.utc)
    horizon_end = now + timedelta(days=max(1, horizon_days))
    grace = timedelta(minutes=max(0, post_match_grace_min))
    out: list[Match] = []
    for m in matches:
        if m.utc_date > horizon_end:
            continue
        if m.is_finished:
            if now <= m.utc_date + grace:
                out.append(m)
            continue
        if m.is_live:
            out.append(m)
            continue
        if m.utc_date + grace < now:
            continue
        out.append(m)
    return out


def _club_prestige(name: str) -> int:
    n = (name or "").casefold()
    best = 20
    for key, score in _CLUB_PRESTIGE.items():
        if key in n:
            best = max(best, score)
    return best


def _pair_prestige(home: str, away: str) -> float:
    """Престиж пары: min + 0.5*max (гранд vs аутсайдер < два средних)."""
    a = float(_club_prestige(home))
    b = float(_club_prestige(away))
    lo, hi = (a, b) if a <= b else (b, a)
    return lo + 0.5 * hi


def _heuristic_hype_score(m: Match) -> float:
    """Основа ранга: престиж пары + стадия (+ live)."""
    stage_p = _STAGE_PRIORITY.get((m.stage or "").upper(), 10)
    pair = _pair_prestige(m.home_team, m.away_team)
    # топ-топ бонус
    if _club_prestige(m.home_team) >= 85 and _club_prestige(m.away_team) >= 85:
        pair += 12
    live_bonus = 8 if m.is_live else 0
    # стадия как множитель-аддитив
    return pair * (1.0 + stage_p / 200.0) + stage_p * 0.35 + live_bonus


def _pick_by_heuristic(candidates: list[Match]) -> Match:
    return max(candidates, key=_heuristic_hype_score)


def _close_cluster(
    scored: list[tuple[Match, float]],
    *,
    rel_tol: float = 0.15,
) -> list[tuple[Match, float]]:
    """Матчи в пределах rel_tol от лидера по престижу."""
    if not scored:
        return []
    best = scored[0][1]
    if best <= 0:
        return scored[:1]
    out = [scored[0]]
    for m, s in scored[1:]:
        if s >= best * (1.0 - rel_tol):
            out.append((m, s))
        else:
            break
    return out


def _pick_by_ai(
    candidates: list[Match],
    *,
    competition_label: str = "",
    top_n: int = 5,
) -> Match | None:
    """Престиж → при близких парах Trends → иначе Wordstat → ИИ как мягкий добор."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    scored = sorted(
        ((m, _heuristic_hype_score(m)) for m in candidates),
        key=lambda x: x[1],
        reverse=True,
    )
    for i, (m, s) in enumerate(scored[:8], 1):
        print(
            f"[seo] prestige #{i} {m.match_id} {m.home_team}—{m.away_team} "
            f"score={s:.1f}",
            flush=True,
        )

    # Широкий пул для логов; tie-break только при почти ничьей (~5%).
    cluster = _close_cluster(scored, rel_tol=0.15)
    near = _close_cluster(scored, rel_tol=0.05)
    print(
        f"[seo] prestige cluster size={len(cluster)} near={len(near)} "
        f"(tol=15%/5%, label={competition_label or '-'})",
        flush=True,
    )
    if len(cluster) == 1 or len(near) == 1:
        winner = near[0][0]
        print(f"[seo] prestige clear winner {winner.match_id}", flush=True)
        return winner

    # --- Trends tie-break (только near-tie, без шума) ---
    try:
        from seo.titles import team_display_ru
        from seo.trends import pick_by_trends, trends_available
    except Exception as e:
        print(f"[seo] trends import fail: {e}", flush=True)
        trends_available = lambda: False  # type: ignore
        pick_by_trends = None  # type: ignore
        team_display_ru = lambda x: x  # type: ignore

    if trends_available() and pick_by_trends is not None:
        items = []
        for m, _ in near[:5]:
            items.append(
                (
                    team_display_ru(m.home_team),
                    team_display_ru(m.away_team),
                    m.home_team,
                    m.away_team,
                )
            )
        idx = pick_by_trends(items, geo="")
        if idx is not None and 0 <= idx < len(near):
            winner = near[idx][0]
            print(f"[seo] trends winner {winner.match_id}", flush=True)
            return winner
        print("[seo] trends inconclusive — keep prestige order", flush=True)

    # --- Wordstat tie-break (тот же near-пул) ---
    try:
        from seo.titles import team_display_ru as _tdr
        from seo.wordstat import score_match_phrases, wordstat_configured
    except Exception as e:
        print(f"[seo] wordstat import fail: {e}", flush=True)
        return near[0][0]

    if wordstat_configured():
        best: Match | None = None
        best_score = -1
        for i, (m, prest) in enumerate(near[:5], 1):
            home_ru = _tdr(m.home_team)
            away_ru = _tdr(m.away_team)
            score, details = score_match_phrases(home_ru, away_ru)
            detail_s = ", ".join(f"«{d.phrase}»={d.volume}" for d in details)
            print(
                f"[seo] wordstat-tb #{i} {m.match_id} {home_ru}—{away_ru} "
                f"ws={score} prestige={prest:.1f} ({detail_s})",
                flush=True,
            )
            if score > best_score:
                best_score = score
                best = m
        if best is not None and best_score > 0:
            print(f"[seo] wordstat winner {best.match_id} ws={best_score}", flush=True)
            return best
        print("[seo] wordstat empty — prestige leader", flush=True)
        return near[0][0]

    # Wordstat недоступен: не отдаём выбор ИИ поверх престижа
    print("[seo] wordstat off — prestige leader", flush=True)
    return near[0][0]


def _ai_rank_top(
    candidates: list[Match],
    *,
    competition_label: str = "",
    top_n: int = 5,
) -> list[tuple[Match, str]]:
    """Вернуть до top_n матчей [(match, reason), ...] в порядке убывания хайпа."""
    settings = get_settings()
    base = (settings.openclaw_base_url or "").rstrip("/")
    token = (settings.openclaw_api_key or "").strip()
    if not base or not token:
        return []

    lines = []
    for m in candidates[:40]:
        when = m.kickoff_msk.strftime("%d.%m %H:%M МСК")
        lines.append(
            f"- id={m.match_id} | {m.home_team} vs {m.away_team} | "
            f"{when} | stage={m.stage or '-'} | status={m.status} | "
            f"comp={m.competition_name or m.competition}"
        )
    label = competition_label or "этот турнир"
    prompt = (
        "Ты редактор SEO-афиши футбольного канала в России.\n"
        f"Нужно выбрать ТОП-{top_n} самых ГРОМКИХ матчей из списка "
        "(от самого громкого к менее громкому).\n"
        "Громкий = интерес аудитории / хайп / топ-клубы / принципиальность / "
        "медийность. Список уже ограничен ближайшим игровым окном.\n"
        f"Контекст канала: {label}.\n\n"
        "Кандидаты:\n"
        + "\n".join(lines)
        + "\n\nОтветь СТРОГО JSON без markdown:\n"
        '{"picks":[{"match_id":"...","reason":"кратко"}, ...]}'
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    backend = (settings.openclaw_backend_model or "").strip()
    if backend:
        headers["x-openclaw-model"] = backend
    payload = {
        "model": settings.openclaw_model or "openclaw/default",
        "temperature": 0.2,
        "max_tokens": 500,
        "messages": [
            {"role": "system", "content": "Отвечай только JSON."},
            {"role": "user", "content": prompt},
        ],
    }
    verify = SYSTEM_CA if Path(SYSTEM_CA).exists() else True
    try:
        with httpx.Client(timeout=90.0, verify=verify) as client:
            r = client.post(f"{base}/chat/completions", headers=headers, json=payload)
            if r.status_code >= 400:
                print(f"[seo] hype-ai fail: {r.status_code} {r.text[:200]}", flush=True)
                return []
            content = r.json()["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(?:json)?\n?|```$", "", content).strip()
        data = json.loads(content)
        picks = data.get("picks")
        if not isinstance(picks, list):
            # backward compat: single match_id
            mid = str(data.get("match_id") or "").strip()
            if mid:
                picks = [{"match_id": mid, "reason": str(data.get("reason") or "")}]
            else:
                return []
        by_id = {m.match_id: m for m in candidates}
        out: list[tuple[Match, str]] = []
        seen: set[str] = set()
        for item in picks:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("match_id") or "").strip()
            if not mid or mid in seen or mid not in by_id:
                continue
            seen.add(mid)
            out.append((by_id[mid], str(item.get("reason") or "").strip()))
            if len(out) >= top_n:
                break
        if out:
            print(
                f"[seo] hype-ai top{len(out)}: "
                + ", ".join(m.match_id for m, _ in out),
                flush=True,
            )
        return out
    except Exception as e:
        print(f"[seo] hype-ai error: {e}", flush=True)
        return []


def _nearest_match_key(m: Match, now: datetime) -> tuple:
    live = 0 if m.is_live else 1
    past = 0 if m.utc_date >= now - timedelta(hours=1) else 1
    return (past, live, m.utc_date.timestamp())


def nearest_window_candidates(
    candidates: list[Match],
    *,
    now: datetime | None = None,
    window_days: int = 4,
) -> list[Match]:
    """Пул матчей в окне от ближайшего кикоффа (не весь горизонт)."""
    if not candidates:
        return []
    now = now or datetime.now(timezone.utc)
    nearest = min(candidates, key=lambda m: _nearest_match_key(m, now))
    window = max(1, int(window_days))
    window_end = nearest.utc_date + timedelta(days=window)
    pool = [
        m
        for m in candidates
        if m.utc_date <= window_end
        and m.utc_date >= nearest.utc_date - timedelta(hours=6)
    ]
    return pool or [nearest]


def pick_top_match(
    matches: list[Match],
    *,
    horizon_days: int = 14,
    post_match_grace_min: int = 120,
    now: datetime | None = None,
    competition_label: str = "",
    use_ai: bool = True,
    hype_window_days: int = 4,
) -> Match | None:
    """Самый громкий матч среди ближайших (окно от первого кикоффа), не за весь месяц."""
    now = now or datetime.now(timezone.utc)
    candidates = candidate_matches(
        matches,
        horizon_days=horizon_days,
        post_match_grace_min=post_match_grace_min,
        now=now,
    )
    if not candidates:
        return None
    pool = nearest_window_candidates(
        candidates, now=now, window_days=hype_window_days
    )
    nearest = min(pool, key=lambda m: _nearest_match_key(m, now))
    print(
        f"[seo] hype-window {hype_window_days}d from "
        f"{nearest.kickoff_msk.date().isoformat()} pool={len(pool)}/"
        f"{len(candidates)}",
        flush=True,
    )
    if use_ai:
        picked = _pick_by_ai(
            pool,
            competition_label=competition_label,
            top_n=int(get_settings().wordstat_top_n or 5),
        )
        if picked is not None:
            return picked
    return _pick_by_heuristic(pool)


# Алиасы приоритетных сборных (ранг = индекс в списке).
_DEFAULT_NT_PRIORITY: tuple[tuple[str, ...], ...] = (
    ("russia", "россия", "российская", "рф"),
    ("spain", "испания", "españa"),
    ("france", "франция"),
    ("argentina", "аргентина"),
)


def _norm_team(name: str) -> str:
    return (name or "").casefold().replace("é", "e").replace("ñ", "n")


def national_team_priority_rank(
    home: str,
    away: str,
    priority: list[list[str]] | tuple[tuple[str, ...], ...] | None = None,
) -> int:
    """0 = высший приоритет; 10_000 = нет совпадения."""
    groups = priority or _DEFAULT_NT_PRIORITY
    h = _norm_team(home)
    a = _norm_team(away)
    best = 10_000
    for idx, aliases in enumerate(groups):
        for raw in aliases:
            key = _norm_team(str(raw))
            if not key:
                continue
            if key in h or key in a or h in key or a in key:
                best = min(best, idx)
                break
    return best


def pick_national_priority_match(
    matches: list[Match],
    *,
    horizon_days: int = 60,
    post_match_grace_min: int = 120,
    now: datetime | None = None,
    priority_teams: list[list[str]] | tuple[tuple[str, ...], ...] | None = None,
    major_competitions: list[str] | tuple[str, ...] | None = None,
    priority_window_days: int = 4,
    competition_label: str = "",
    use_ai_for_major: bool = True,
) -> Match | None:
    """Сначала топ-турниры (WC/EC/CA) по хайпу.

    Иначе: берём ближайший матч сборных, смотрим окно ±0…N дней от него
    и внутри окна выбираем по приоритету сборных, затем по времени.
    """
    now = now or datetime.now(timezone.utc)
    candidates = candidate_matches(
        matches,
        horizon_days=horizon_days,
        post_match_grace_min=post_match_grace_min,
        now=now,
    )
    if not candidates:
        return None

    majors = {c.upper() for c in (major_competitions or ("WC", "EC", "CA"))}
    # Основные стадии турниров (не квалификация) — хайп в ближайшем окне
    major_main = [
        m
        for m in candidates
        if m.competition.upper() in majors and not m.is_qualifying
    ]
    if major_main:
        pool = nearest_window_candidates(
            major_main, now=now, window_days=priority_window_days
        )
        if use_ai_for_major:
            picked = _pick_by_ai(pool, competition_label=competition_label)
            if picked is not None:
                return picked
        return _pick_by_heuristic(pool)

    # Ближайший кикофф → окно N дней → приоритет сборных, затем время
    nearest = min(candidates, key=lambda m: _nearest_match_key(m, now))
    pool = nearest_window_candidates(
        candidates, now=now, window_days=priority_window_days
    )

    def sort_key(m: Match) -> tuple:
        rank = national_team_priority_rank(
            m.home_team, m.away_team, priority_teams
        )
        live = 0 if m.is_live else 1
        return (rank, live, m.utc_date.timestamp())

    picked = min(pool, key=sort_key)
    rank = national_team_priority_rank(
        picked.home_team, picked.away_team, priority_teams
    )
    print(
        f"[seo] nt-priority picked {picked.match_id}: "
        f"{picked.home_team} — {picked.away_team} rank={rank} "
        f"window={priority_window_days}d from {nearest.kickoff_msk.date().isoformat()} "
        f"pool={len(pool)} ko={picked.kickoff_msk.isoformat()}",
        flush=True,
    )
    return picked


def match_needs_rotation(active_match_id: str, active_kickoff: float | None, new: Match | None) -> bool:
    if new is None:
        return bool(active_match_id)
    if not active_match_id:
        return True
    return str(active_match_id) != str(new.match_id)
