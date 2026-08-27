"""Fixtures provider: football-data.org (free) + ESPN fallback for RPL/UNL/NT.

Free football-data: счёт ЗАДЕРЖАН (live-скоры платные). Карточка «по свистку»
выходит с задержкой в несколько минут — для канала это приемлемо.
Настоящий live: livescores add-on (€12/мес) и FIXTURES_LIVE=true.
ESPN (РПЛ/UNL/NT) обычно отдаёт счёт быстрее, но менее стабилен.
Egress — через тот же прокси, что и OpenAI (OPENAI_HTTP_PROXY / xray).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dtime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
import yaml

from app.config import ROOT, get_settings
from app.http_util import SYSTEM_CA, openai_proxy, scraper_proxy
from editorial.catalogs import (
    canonical_team,
    is_grand,
    load_fifa_top100_names,
    norm_name,
    team_display_ru,
)

MSK = ZoneInfo("Europe/Moscow")
LIVE_STATUSES = {"LIVE", "IN_PLAY", "PAUSED"}
FINISHED_STATUSES = {"FINISHED", "AWARDED"}
NATIONAL_COMPS = {"WC", "EC", "UNL", "NT"}
EURO_CUPS = {"CL", "EL"}
SEMI_STAGES = {
    "FINAL",
    "SEMI_FINALS",
    "SEMI_FINAL",
    "SEMIFINAL",
    "SEMIFINALS",
    "LAST_4",
    "PLAYOFF_ROUND",
}

COMP_GROUP_ORDER = ("CL", "EL", "NT", "UNL", "WC", "EC", "PL", "PD", "SA", "BL", "FL1", "RPL")


@dataclass
class Match:
    provider_id: str
    competition: str
    home: str
    away: str
    home_ru: str
    away_ru: str
    kickoff_utc: datetime
    status: str
    score_home: int | None = None
    score_away: int | None = None
    stage: str | None = None
    is_national: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def kickoff_msk(self) -> datetime:
        ko = self.kickoff_utc
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        return ko.astimezone(MSK)

    @property
    def date_msk(self) -> date:
        return self.kickoff_msk.date()


class FixturesProvider(Protocol):
    def matches_on(self, date_msk: date) -> list[Match]: ...
    def match_status(self, match_id: str) -> Match | None: ...
    def finished_since(self, since_ts: datetime) -> list[Match]: ...


@lru_cache
def load_leagues() -> dict[str, Any]:
    settings = get_settings()
    path = Path(getattr(settings, "fixtures_leagues_file", "") or ROOT / "editorial" / "fixtures_leagues.yaml")
    if not path.is_file():
        path = ROOT / "editorial" / "fixtures_leagues.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def competition_label_ru(code: str) -> str:
    labels = (load_leagues().get("labels_ru") or {}) if load_leagues() else {}
    return str(labels.get(code) or code)


def _proxy() -> str | None:
    return openai_proxy() or scraper_proxy()


def _verify() -> str | bool:
    return SYSTEM_CA if Path(SYSTEM_CA).exists() else True


class RateBudget:
    """Держит football-data ниже 10 req/min (берём 8 с запасом)."""

    def __init__(self, per_min: int = 8) -> None:
        self.per_min = per_min
        self._hits: deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self._hits and now - self._hits[0] > 60:
            self._hits.popleft()
        if len(self._hits) >= self.per_min:
            sleep_for = 60 - (now - self._hits[0]) + 0.25
            if sleep_for > 0:
                print(f"[fixtures] rate-budget sleep {sleep_for:.1f}s", flush=True)
                time.sleep(sleep_for)
        self._hits.append(time.monotonic())


_fd_budget = RateBudget()


def _normalize_status(raw: str) -> str:
    s = (raw or "").upper()
    if s in {"TIMED", "SCHEDULED", "NS"}:
        return "SCHEDULED"
    if s in {"IN_PLAY", "LIVE", "INPLAY", "1H", "2H", "HT", "HALFTIME"}:
        return "IN_PLAY" if s != "PAUSED" else "PAUSED"
    if s in {"PAUSED", "BREAK"}:
        return "PAUSED"
    if s in {"FINISHED", "FT", "AET", "PEN", "AWARDED", "FULL_TIME"}:
        return "FINISHED"
    if "FINAL" in s or "FULL" in s:
        return "FINISHED"
    if "PROGRESS" in s or "LIVE" in s:
        return "IN_PLAY"
    return s or "SCHEDULED"


def _score_pair(home: Any, away: Any) -> tuple[int | None, int | None]:
    try:
        h = int(home) if home is not None else None
    except (TypeError, ValueError):
        h = None
    try:
        a = int(away) if away is not None else None
    except (TypeError, ValueError):
        a = None
    return h, a


def _canon_comp(code: str) -> str:
    c = (code or "").upper()
    if c == "BL1":
        return "BL"
    return c


def is_significant(
    match: Match,
    *,
    always_priority: tuple[str, ...] = ("Russia",),
    grands: bool = True,
    all_cl_el: bool = True,
    national_top100: bool = True,
) -> bool:
    if all_cl_el and match.competition in EURO_CUPS:
        return True
    stage = (match.stage or "").upper().replace(" ", "_")
    if stage in SEMI_STAGES or stage == "FINAL":
        return True
    if grands and (is_grand(match.home) or is_grand(match.away)):
        return True
    priority = {norm_name(x) for x in always_priority}
    home_n = norm_name(canonical_team(match.home))
    away_n = norm_name(canonical_team(match.away))
    if home_n in priority or away_n in priority or norm_name(match.home) in priority or norm_name(match.away) in priority:
        return True
    if national_top100 and (match.is_national or match.competition in NATIONAL_COMPS):
        fifa = load_fifa_top100_names()
        if home_n in fifa and away_n in fifa:
            return True
        if norm_name(match.home) in fifa and norm_name(match.away) in fifa:
            return True
    return False


def _match_from_fd(item: dict[str, Any]) -> Match | None:
    try:
        mid = str(item.get("id") or "")
        if not mid:
            return None
        utc = datetime.fromisoformat(str(item.get("utcDate") or "").replace("Z", "+00:00"))
        if utc.tzinfo is None:
            utc = utc.replace(tzinfo=timezone.utc)
        home = (item.get("homeTeam") or {}).get("name") or (item.get("homeTeam") or {}).get("shortName") or ""
        away = (item.get("awayTeam") or {}).get("name") or (item.get("awayTeam") or {}).get("shortName") or ""
        if not home or not away:
            return None
        score = item.get("score") or {}
        ft = score.get("fullTime") or {}
        sh, sa = _score_pair(ft.get("home"), ft.get("away"))
        if sh is None:
            ht = score.get("halfTime") or {}
            sh, sa = _score_pair(ht.get("home"), ht.get("away"))
        comp = _canon_comp(((item.get("competition") or {}).get("code") or ""))
        home_c = canonical_team(str(home))
        away_c = canonical_team(str(away))
        return Match(
            provider_id=f"fd:{mid}",
            competition=comp,
            home=home_c,
            away=away_c,
            home_ru=team_display_ru(home_c),
            away_ru=team_display_ru(away_c),
            kickoff_utc=utc,
            status=_normalize_status(str(item.get("status") or "")),
            score_home=sh,
            score_away=sa,
            stage=str(item.get("stage") or "") or None,
            is_national=comp in NATIONAL_COMPS,
            raw=item,
        )
    except Exception:
        return None


class FootballDataProvider:
    def __init__(self) -> None:
        self._day: dict[str, tuple[float, list[Match]]] = {}
        self._one: dict[str, tuple[float, Match]] = {}

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=30.0, verify=_verify(), proxy=_proxy(), follow_redirects=True)

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        settings = get_settings()
        token = (settings.football_data_token or "").strip()
        if not token:
            raise RuntimeError("FOOTBALL_DATA_TOKEN не задан")
        base = (settings.football_data_base or "https://api.football-data.org/v4").rstrip("/")
        _fd_budget.wait()
        headers = {"X-Auth-Token": token}
        with self._client() as client:
            r = client.get(f"{base}{path}", params=params or {}, headers=headers)
            if r.status_code == 429:
                reset = int(r.headers.get("X-RequestCounter-Reset") or "60")
                time.sleep(min(max(reset, 5), 90))
                _fd_budget.wait()
                r = client.get(f"{base}{path}", params=params or {}, headers=headers)
            if r.status_code >= 400:
                raise RuntimeError(f"football-data {r.status_code}: {r.text[:240]}")
            return r.json() if r.content else {}

    def _codes(self) -> str:
        mapping = load_leagues().get("football_data") or {}
        return ",".join(str(v) for v in mapping.values() if v)

    def matches_on(self, date_msk: date) -> list[Match]:
        key = date_msk.isoformat()
        hit = self._day.get(key)
        live = bool(hit and any(m.status in LIVE_STATUSES for m in hit[1]))
        ttl = 60.0 if live else 1800.0
        if hit and (time.monotonic() - hit[0]) < ttl:
            return list(hit[1])
        start = datetime.combine(date_msk, dtime.min, tzinfo=MSK).astimezone(timezone.utc)
        end = datetime.combine(date_msk, dtime.max.replace(microsecond=0), tzinfo=MSK).astimezone(
            timezone.utc
        )
        data = self._get(
            "/matches",
            {
                "dateFrom": start.date().isoformat(),
                "dateTo": end.date().isoformat(),
                "competitions": self._codes(),
            },
        )
        out: list[Match] = []
        for item in data.get("matches") or []:
            m = _match_from_fd(item)
            if m and m.date_msk == date_msk:
                out.append(m)
        self._day[key] = (time.monotonic(), out)
        return list(out)

    def match_status(self, match_id: str) -> Match | None:
        raw_id = match_id.split(":", 1)[-1] if match_id.startswith("fd:") else match_id
        cached = self._one.get(raw_id)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        data = self._get(f"/matches/{raw_id}")
        m = _match_from_fd(data) if isinstance(data, dict) else None
        if m:
            self._one[raw_id] = (time.monotonic(), m)
        return m

    def finished_since(self, since_ts: datetime) -> list[Match]:
        today = datetime.now(MSK).date()
        out = []
        for m in self.matches_on(today):
            if m.status in FINISHED_STATUSES and m.kickoff_utc >= since_ts - timedelta(hours=6):
                out.append(m)
        return out


def _espn_status(name: str) -> str:
    n = (name or "").upper()
    if "FINAL" in n or "FULL" in n:
        return "FINISHED"
    if "HALFTIME" in n:
        return "PAUSED"
    if "IN_PROGRESS" in n or "LIVE" in n:
        return "IN_PLAY"
    return "SCHEDULED"


def _match_from_espn(ev: dict[str, Any], competition: str) -> Match | None:
    try:
        eid = str(ev.get("id") or "")
        if not eid:
            return None
        utc = datetime.fromisoformat(str(ev.get("date") or "").replace("Z", "+00:00"))
        if utc.tzinfo is None:
            utc = utc.replace(tzinfo=timezone.utc)
        comps = ev.get("competitions") or []
        comp0 = comps[0] if comps else {}
        teams = comp0.get("competitors") or []
        home = away = ""
        sh = sa = None
        for t in teams:
            nm = ((t.get("team") or {}).get("displayName") or t.get("displayName") or "").strip()
            score = t.get("score")
            try:
                sc = int(score) if score not in {None, ""} else None
            except (TypeError, ValueError):
                sc = None
            if str(t.get("homeAway") or "") == "home":
                home, sh = nm, sc
            else:
                away, sa = nm, sc
        if not home or not away:
            return None
        st = _espn_status(((ev.get("status") or {}).get("type") or {}).get("name") or "")
        home_c = canonical_team(home)
        away_c = canonical_team(away)
        return Match(
            provider_id=f"espn:{competition}:{eid}",
            competition=competition,
            home=home_c,
            away=away_c,
            home_ru=team_display_ru(home_c),
            away_ru=team_display_ru(away_c),
            kickoff_utc=utc,
            status=st,
            score_home=sh if st in FINISHED_STATUSES | LIVE_STATUSES else sh,
            score_away=sa if st in FINISHED_STATUSES | LIVE_STATUSES else sa,
            stage=None,
            is_national=competition in NATIONAL_COMPS,
            raw=ev,
        )
    except Exception:
        return None


class EspnProvider:
    def __init__(self) -> None:
        self._day: dict[str, tuple[float, list[Match]]] = {}

    def matches_on(self, date_msk: date) -> list[Match]:
        key = date_msk.isoformat()
        hit = self._day.get(key)
        if hit and time.monotonic() - hit[0] < 1800:
            if not any(m.status in LIVE_STATUSES for m in hit[1]) or time.monotonic() - hit[0] < 60:
                return list(hit[1])
        mapping = load_leagues().get("espn") or {}
        stamp = date_msk.strftime("%Y%m%d")
        out: list[Match] = []
        with httpx.Client(
            timeout=25.0, verify=_verify(), proxy=_proxy(), follow_redirects=True
        ) as client:
            for our, paths in mapping.items():
                our_c = str(our).upper()
                if our_c not in {"RPL", "UNL", "NT"}:
                    continue
                for path in paths or []:
                    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{path}/scoreboard"
                    r = client.get(url, params={"dates": stamp, "limit": 100})
                    if r.status_code >= 400:
                        print(f"[fixtures] ESPN {path}: {r.status_code}", flush=True)
                        continue
                    data = r.json()
                    for ev in data.get("events") or []:
                        m = _match_from_espn(ev, our_c)
                        if m and m.date_msk == date_msk:
                            out.append(m)
        self._day[key] = (time.monotonic(), out)
        return list(out)

    def match_status(self, match_id: str) -> Match | None:
        parts = match_id.split(":")
        # espn:RPL:123
        today = datetime.now(MSK).date()
        for m in self.matches_on(today):
            if m.provider_id == match_id:
                return m
        if len(parts) == 3:
            yday = today - timedelta(days=1)
            for m in self.matches_on(yday):
                if m.provider_id == match_id:
                    return m
        return None

    def finished_since(self, since_ts: datetime) -> list[Match]:
        today = datetime.now(MSK).date()
        return [
            m
            for m in self.matches_on(today)
            if m.status in FINISHED_STATUSES and m.kickoff_utc >= since_ts - timedelta(hours=6)
        ]


def _dedup(matches: list[Match]) -> list[Match]:
    seen: dict[tuple[str, str, str], Match] = {}
    for m in matches:
        key = (norm_name(m.home), norm_name(m.away), m.date_msk.isoformat())
        prev = seen.get(key)
        if prev is None:
            seen[key] = m
            continue
        # предпочитаем football-data, но ESPN если у него уже есть счёт
        if m.provider_id.startswith("fd:") and prev.provider_id.startswith("espn:"):
            if prev.score_home is not None and m.score_home is None:
                seen[key] = replace(m, score_home=prev.score_home, score_away=prev.score_away, status=prev.status)
            else:
                seen[key] = m
        elif m.score_home is not None and prev.score_home is None:
            seen[key] = m
    return list(seen.values())


class CombinedProvider:
    def __init__(self, backend: str = "both") -> None:
        self.backend = (backend or "both").strip().lower()
        self.fd = FootballDataProvider()
        self.espn = EspnProvider()

    def matches_on(self, date_msk: date) -> list[Match]:
        out: list[Match] = []
        if self.backend in {"football_data", "both", ""}:
            try:
                out.extend(self.fd.matches_on(date_msk))
            except Exception as e:
                print(f"[fixtures] football-data fail: {e}", flush=True)
        if self.backend in {"espn", "both", ""}:
            try:
                out.extend(self.espn.matches_on(date_msk))
            except Exception as e:
                print(f"[fixtures] ESPN fail: {e}", flush=True)
        return _dedup(out)

    def match_status(self, match_id: str) -> Match | None:
        if match_id.startswith("espn:"):
            return self.espn.match_status(match_id)
        return self.fd.match_status(match_id)

    def finished_since(self, since_ts: datetime) -> list[Match]:
        return _dedup(self.fd.finished_since(since_ts) + self.espn.finished_since(since_ts))


_provider: CombinedProvider | None = None


def get_provider() -> CombinedProvider:
    global _provider
    if _provider is None:
        backend = (get_settings().fixtures_backend or "both").strip().lower()
        _provider = CombinedProvider(backend)
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None


def significant_matches(
    matches: list[Match],
    *,
    always_priority: tuple[str, ...] = ("Russia",),
    grands: bool = True,
    all_cl_el: bool = True,
    national_top100: bool = True,
) -> list[Match]:
    return [
        m
        for m in matches
        if is_significant(
            m,
            always_priority=always_priority,
            grands=grands,
            all_cl_el=all_cl_el,
            national_top100=national_top100,
        )
    ]


def sort_matchday(matches: list[Match], group_order: tuple[str, ...] | None = None) -> list[Match]:
    order = group_order or COMP_GROUP_ORDER
    rank = {c: i for i, c in enumerate(order)}
    return sorted(
        matches,
        key=lambda m: (rank.get(m.competition, 99), m.kickoff_utc),
    )


def in_poll_window(
    match: Match,
    now: datetime,
    *,
    pre_min: int = 5,
    post_min: int = 30,
    posted: bool = False,
) -> bool:
    if posted:
        return False
    ko = match.kickoff_utc
    if ko.tzinfo is None:
        ko = ko.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if now < ko - timedelta(minutes=pre_min):
        return False
    if match.status in LIVE_STATUSES:
        return True
    if match.status in FINISHED_STATUSES:
        # закрыть опрос ~post_min после типичного финального свистка (+ запас на delay free-tier)
        return now <= ko + timedelta(hours=2, minutes=30 + max(post_min, 0))
    return now <= ko + timedelta(hours=4)
