"""Yandex Wordstat client for match SEO volume checks.

Preferred: Yandex Cloud Search API
  https://searchapi.api.cloud.yandex.net/v2/wordstat/...
  Authorization: Api-Key … + folderId

Fallback: official Wordstat OAuth API
  https://api.wordstat.yandex.net
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"
_WORDSTAT_BASE = "https://api.wordstat.yandex.net"
_CLOUD_BASE = "https://searchapi.api.cloud.yandex.net/v2/wordstat"

# phrase -> (volume, expires_at)
_CACHE: dict[str, tuple[int, float]] = {}
_CACHE_TTL_SEC = 6 * 3600


@dataclass(frozen=True)
class PhraseScore:
    phrase: str
    volume: int


def wordstat_configured() -> bool:
    s = get_settings()
    if (s.yandex_cloud_api_key or "").strip() and (s.yandex_folder_id or "").strip():
        return True
    if (s.wordstat_oauth_token or "").strip():
        return True
    return False


def wordstat_region_ids() -> list[str]:
    """ID регионов Wordstat. Пустой список = весь мир (без фильтра regions)."""
    raw = (get_settings().wordstat_regions or "").strip()
    if not raw:
        return []
    out: list[str] = []
    for part in raw.split(","):
        p = part.strip()
        if p:
            out.append(p)
    return out


def clear_wordstat_cache() -> None:
    _CACHE.clear()


def _cloud_ready() -> bool:
    s = get_settings()
    return bool(
        (s.yandex_cloud_api_key or "").strip() and (s.yandex_folder_id or "").strip()
    )


def _verify_cloud() -> str | bool:
    return SYSTEM_CA if Path(SYSTEM_CA).exists() else True


def _cache_get(phrase: str) -> int | None:
    key = phrase.casefold().strip()
    hit = _CACHE.get(key)
    if not hit:
        return None
    vol, exp = hit
    if time.time() > exp:
        _CACHE.pop(key, None)
        return None
    return vol


def _cache_set(phrase: str, volume: int) -> None:
    _CACHE[phrase.casefold().strip()] = (int(volume), time.time() + _CACHE_TTL_SEC)


def _as_int(val: Any) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _phrase_volume_from_payload(phrase: str, data: dict[str, Any]) -> int:
    """Cloud: totalCount; иначе exact/max из results|topRequests."""
    total = _as_int(data.get("totalCount"))
    items = list(data.get("results") or data.get("topRequests") or [])
    exact = 0
    best = 0
    needle = phrase.casefold().strip()
    for i in items:
        cnt = _as_int(i.get("count"))
        best = max(best, cnt)
        p = str(i.get("phrase") or "").casefold().strip()
        if p == needle or set(p.split()) == set(needle.split()):
            exact = max(exact, cnt)
    # totalCount — основной показатель Wordstat Cloud
    if total > 0:
        return max(total, exact)
    if exact > 0:
        return exact
    return best


def fetch_top_payload(phrase: str) -> dict[str, Any]:
    phrase = (phrase or "").strip()
    if not phrase:
        return {}
    s = get_settings()

    # 1) Cloud Search API (рабочий путь)
    if _cloud_ready():
        body: dict[str, Any] = {
            "phrase": phrase,
            "folderId": s.yandex_folder_id.strip(),
            "numPhrases": 30,
        }
        regions = wordstat_region_ids()
        if regions:
            body["regions"] = regions
        try:
            with httpx.Client(timeout=30.0, verify=_verify_cloud()) as client:
                r = client.post(
                    f"{_CLOUD_BASE}/topRequests",
                    headers={
                        "Authorization": f"Api-Key {s.yandex_cloud_api_key.strip()}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            if r.status_code == 429:
                print("[seo] wordstat cloud 429 quota — skip", flush=True)
                return {}
            if r.status_code >= 400:
                print(
                    f"[seo] wordstat cloud {r.status_code}: {r.text[:200]}",
                    flush=True,
                )
            else:
                return r.json() if r.content else {}
        except Exception as e:
            print(f"[seo] wordstat cloud error: {e}", flush=True)

    # 2) Official OAuth API (часто 404 без заявки в Директ)
    tok = (s.wordstat_oauth_token or "").strip()
    if tok:
        try:
            body_o: dict[str, Any] = {"phrase": phrase}
            regions = [int(x) for x in wordstat_region_ids() if str(x).isdigit()]
            if regions:
                body_o["regions"] = regions
            with httpx.Client(timeout=30.0, verify=False) as client:
                r = client.post(
                    f"{_WORDSTAT_BASE}/v1/topRequests",
                    headers={
                        "Authorization": f"Bearer {tok}",
                        "Content-Type": "application/json;charset=utf-8",
                    },
                    json=body_o,
                )
            if r.status_code >= 400:
                print(
                    f"[seo] wordstat oauth {r.status_code}: {r.text[:200]}",
                    flush=True,
                )
                return {}
            return r.json() if r.content else {}
        except Exception as e:
            print(f"[seo] wordstat oauth error: {e}", flush=True)
    return {}


def phrase_volume(phrase: str) -> int:
    """Частотность фразы (кэш 6ч). Предпочтительно totalCount Cloud API."""
    phrase = " ".join((phrase or "").split()).strip().lower()
    if not phrase:
        return 0
    cached = _cache_get(phrase)
    if cached is not None:
        return cached
    if not wordstat_configured():
        return 0

    data = fetch_top_payload(phrase)
    volume = _phrase_volume_from_payload(phrase, data) if data else 0
    _cache_set(phrase, volume)
    time.sleep(0.25)
    return volume


def match_seo_phrases(home_ru: str, away_ru: str) -> list[str]:
    """Фразы под Wordstat: оба порядка + матч/смотреть."""
    h = " ".join((home_ru or "").lower().split())
    a = " ".join((away_ru or "").lower().split())
    if not h or not a:
        return []
    phrases = [
        f"{h} {a}",
        f"{a} {h}",
        f"{h} {a} матч",
        f"{a} {h} матч",
        f"{h} {a} смотреть",
    ]
    # dedupe preserve order
    out: list[str] = []
    seen: set[str] = set()
    for p in phrases:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def score_match_phrases(home_ru: str, away_ru: str) -> tuple[int, list[PhraseScore]]:
    """Оценка пары: max по вариантам порядка (без двойного счёта)."""
    phrases = match_seo_phrases(home_ru, away_ru)
    details: list[PhraseScore] = []
    vols: list[int] = []
    for p in phrases:
        vol = phrase_volume(p)
        details.append(PhraseScore(phrase=p, volume=vol))
        vols.append(vol)
    total = max(vols) if vols else 0
    return total, details
