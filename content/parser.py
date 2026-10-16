"""Каталог сериалов tvoe.live через публичный API + карточка сериала."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.config import get_settings
from app.http_util import http_client

_YEAR_RE = re.compile(r"^(\d{4})")


@dataclass(frozen=True)
class SeriesItem:
    id: str
    slug: str
    title: str
    year: int | None
    seasons: int | None
    description: str
    poster_url: str
    url: str
    genres: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "title": self.title,
            "year": self.year,
            "seasons": self.seasons,
            "description": self.description,
            "poster_url": self.poster_url,
            "url": self.url,
            "genres": list(self.genres),
        }


def referral_url(url: str, ref: str | None = None) -> str:
    """К канонической ссылке сериала добавляем ?r=... (или &r=, если query уже есть)."""
    settings = get_settings()
    token = (ref if ref is not None else settings.content_ref).strip()
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["r"] = token
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _headers() -> dict[str, str]:
    site = get_settings().content_site_base.rstrip("/")
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
        "Origin": site,
        "Referer": f"{site}/serialy",
    }


def _abs_site(path: str) -> str:
    settings = get_settings()
    raw = (path or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return urljoin(settings.content_site_base.rstrip("/") + "/", raw.lstrip("/"))


def _abs_cdn(path: str) -> str:
    settings = get_settings()
    raw = (path or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return urljoin(settings.content_cdn_base.rstrip("/") + "/", raw.lstrip("/"))


def _year_from_date(value: Any) -> int | None:
    raw = str(value or "").strip()
    m = _YEAR_RE.match(raw)
    if not m:
        return None
    year = int(m.group(1))
    return year if 1900 <= year <= 2100 else None


def _seasons(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _media_src(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("src") or "")
    return str(value or "")


def parse_genres(raw: dict[str, Any] | None) -> tuple[str, ...]:
    if not raw:
        return ()
    names: list[Any] = []
    if isinstance(raw.get("genreNames"), list):
        names.extend(raw.get("genreNames") or [])
    single = raw.get("genreName")
    if isinstance(single, str) and single.strip():
        names.append(single)
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        label = str(name or "").strip()
        key = label.casefold()
        if not label or key in seen:
            continue
        seen.add(key)
        out.append(label)
    return tuple(out)


def parse_catalog_item(raw: dict[str, Any]) -> SeriesItem | None:
    sid = str(raw.get("_id") or "").strip()
    title = str(raw.get("name") or "").strip()
    path = str(raw.get("url") or "").strip()
    if not sid or not title or not path:
        return None
    url = _abs_site(path)
    slug = urlsplit(url).path.rstrip("/").split("/")[-1]
    return SeriesItem(
        id=sid,
        slug=slug,
        title=title,
        year=_year_from_date(raw.get("dateReleased")),
        seasons=_seasons(raw.get("seasonsCount")),
        description=str(raw.get("fullDesc") or raw.get("shortDesc") or "").strip(),
        poster_url=_abs_cdn(_media_src(raw.get("poster"))),
        url=url,
        genres=parse_genres(raw),
    )


def fetch_catalog() -> list[SeriesItem]:
    """Все сериалы из /v2/catalog?categoryAlias=serials (постранично)."""
    settings = get_settings()
    api = settings.content_api_base.rstrip("/")
    limit = max(1, min(int(settings.content_catalog_limit or 100), 100))
    out: list[SeriesItem] = []
    seen: set[str] = set()
    skip = 0
    total = None
    with http_client(timeout=45.0, headers=_headers()) as client:
        while True:
            resp = client.get(
                f"{api}/v2/catalog",
                params={
                    "categoryAlias": "serials",
                    "limit": limit,
                    "skip": skip,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if total is None:
                total = int(data.get("totalSize") or 0)
            items = data.get("items") or []
            if not items:
                break
            new_items = 0
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                item = parse_catalog_item(raw)
                if item is None or item.id in seen:
                    continue
                seen.add(item.id)
                out.append(item)
                new_items += 1
            skip += len(items)
            if total and skip >= total:
                break
            if len(items) < limit or new_items == 0:
                break
    return out


def fetch_series_details(url: str) -> dict[str, Any]:
    """Полное описание, постер и жанры с карточки сериала (__NEXT_DATA__)."""
    page_url = _abs_site(url)
    if not page_url:
        return {}
    with http_client(timeout=45.0, headers=_headers()) as client:
        resp = client.get(page_url)
        resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    node = soup.find("script", id="__NEXT_DATA__")
    if node is None or not node.string:
        return {}
    try:
        payload = json.loads(node.string)
    except json.JSONDecodeError:
        return {}
    data = ((payload.get("props") or {}).get("pageProps") or {}).get("data") or {}
    if not isinstance(data, dict):
        return {}
    desc = str(data.get("fullDesc") or data.get("shortDesc") or "").strip()
    poster = _abs_cdn(_media_src(data.get("poster")))
    genres = parse_genres(data)
    out: dict[str, Any] = {}
    if desc:
        out["description"] = desc
    if poster:
        out["poster_url"] = poster
    if genres:
        out["genres"] = genres
    return out
