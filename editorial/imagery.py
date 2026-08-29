"""Image search: candidates → quality → vision relevance → smart crop.

Поиск: фото статьи (Championat 900×900 и оригинал) + Яндекс.Картинки.
Wikimedia и Bing не используем — оффтоп, 429 и чужие портреты.
Сетка дня / результаты матчей этот модуль не вызывают.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import io
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from PIL import Image, ImageOps

from app.config import ROOT, get_settings
from app.http_util import http_client, scraper_proxy
from editorial.catalogs import canonical_team, load_fifa_top100_names, load_players, norm_name
from editorial.imagery_trace import append_trace, new_trace

IMAGES_DIR = ROOT / "data" / "editorial" / "images"

_BLOCK_HOST_BITS = (
    "pornhub",
    "xvideos",
    "xhamster",
    "xnxx",
    "xvid",
    "onlyfans",
    "redtube",
    "youporn",
    "spankbang",
    "imagefap",
    "motherless",
    "eporner",
    "xhcdn",
    "xhamster",
    "trafficjunky",
    "chaturbate",
    "stripchat",
    "brazzers",
    "youjizz",
    "tube8",
    "tnaflix",
    "pornpics",
    "pornpic",
    "erome",
    "rule34",
    "nhentai",
    "gelbooru",
    "hentai",
    "fapello",
    "thothub",
    "nsfw",
    "xxx",
    "sex.com",
    "adult",
    "nudevista",
    "gotporn",
    "hqporner",
    "sxyprn",
    "dmm.co.jp",
)

_BLOCK_PATH = re.compile(
    r"(porn|xxx|nude|nsfw|hentai|onlyfans|erotic|sex[-_/]|leaked|only-fans)",
    re.I,
)

# Спортивные/новостные CDN. Википедию сознательно не предпочитаем.
_ALLOW_HOST_SUFFIX = (
    "championat.com",
    "sports.ru",
    "sport-express.ru",
    "matchtv.ru",
    "sportbox.ru",
    "soccer.ru",
    "rbk.ru",
    "transfermarkt.com",
    "transfermarkt.de",
    "uefa.com",
    "fifa.com",
    "premierleague.com",
    "bundesliga.com",
    "laliga.com",
    "legaseriea.it",
    "bbc.co.uk",
    "bbci.co.uk",
    "bbc.com",
    "theguardian.com",
    "guim.co.uk",
    "skysports.com",
    "espn.com",
    "espncdn.com",
    "goal.com",
    "marca.com",
    "as.com",
    "reuters.com",
    "apnews.com",
    "gettyimages.com",
    "gettyimages.co.uk",
    "imago-images.com",
    "sofascore.com",
    "gazzetta.it",
    "rfs.ru",
    "rfpl.org",
    "football-data.org",
    "ss.sport-express.ru",
    "mirror.co.uk",
)

_WIKI_UA = "MaxRepostEditorial/1.0 (football news covers; local pipeline)"


class ImageSearchProvider(ABC):
    name: str = "base"

    @abstractmethod
    def search(self, query: str, limit: int = 8) -> list[str]:
        """Return candidate image URLs."""


def _verify() -> str | bool:
    ca = Path("/etc/ssl/certs/ca-certificates.crt")
    return str(ca) if ca.exists() else True


def _host(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def url_is_blocked(url: str) -> bool:
    raw = (url or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return True
    host = _host(raw)
    path = (urlparse(raw).path or "") + " " + (urlparse(raw).query or "")
    if _BLOCK_PATH.search(raw) or _BLOCK_PATH.search(path):
        return True
    return any(bit in host for bit in _BLOCK_HOST_BITS)


def url_is_allowed(url: str) -> bool:
    if url_is_blocked(url):
        return False
    host = _host(url)
    if not host:
        return False
    return any(host == suf or host.endswith("." + suf) for suf in _ALLOW_HOST_SUFFIX)


def _keep_urls(urls: list[str], *, limit: int, allowlist_only: bool = False) -> list[str]:
    preferred: list[str] = []
    rest: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        u = (raw or "").replace("\\/", "/").replace("&amp;", "&").replace("\\u0026", "&").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        path = urlparse(u).path.lower()
        name = path.rsplit("/", 1)[-1]
        if ".svg" in path:
            continue
        if name.startswith("logo") or "/logo/" in path or "/logos/" in path:
            continue
        if "/tc_team/" in path or "/userpic/" in path or "/icons/" in path:
            continue
        if "/windows-phone/" in path or "/favicon" in path:
            continue
        host = _host(u)
        if "gstatic.com" in host or host.endswith("google.com") or "googleusercontent.com" in host:
            continue
        if "wikipedia.org" in host or "wikimedia.org" in host:
            continue
        if url_is_blocked(u):
            continue
        if allowlist_only and not url_is_allowed(u):
            continue
        (preferred if url_is_allowed(u) else rest).append(u)
    out = preferred + rest
    return out[:limit]


class SerpApiProvider(ImageSearchProvider):
    name = "serpapi"

    def search(self, query: str, limit: int = 8) -> list[str]:
        settings = get_settings()
        key = (settings.image_search_api_key or "").strip()
        if not key:
            return []
        params = {
            "engine": "google_images",
            "q": query,
            "api_key": key,
            "ijn": "0",
            "safe": "active",
        }
        with httpx.Client(timeout=40.0, verify=_verify(), proxy=scraper_proxy()) as client:
            r = client.get("https://serpapi.com/search.json", params=params)
            r.raise_for_status()
            data = r.json()
        urls: list[str] = []
        for item in data.get("images_results") or []:
            url = item.get("original") or item.get("thumbnail")
            if url:
                urls.append(url)
        return _keep_urls(urls, limit=limit, allowlist_only=False)


class YandexProvider(ImageSearchProvider):
    name = "yandex"

    def search(self, query: str, limit: int = 8, *, sites: tuple[str, ...] = ()) -> list[str]:
        q = (query or "").strip()
        if not q:
            return []
        if sites:
            q = " ".join(f"site:{s}" for s in sites) + " " + q
        url = (
            f"https://yandex.ru/images/search?text={quote_plus(q)}"
            f"&itype=photo&family=yes"
        )
        try:
            with http_client(timeout=40.0) as client:
                r = client.get(
                    url,
                    headers={
                        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.4",
                        "User-Agent": (
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                        ),
                    },
                )
                r.raise_for_status()
                html = r.text
        except Exception as e:
            print(f"[editorial] yandex images fail: {e}", flush=True)
            return []
        urls = parse_yandex_orig_urls(html)
        print(f"[editorial] yandex hits={len(urls)} q={q[:80]}", flush=True)
        return _keep_urls(urls, limit=limit, allowlist_only=False)


def parse_yandex_orig_urls(html: str, *, min_side: int = 800) -> list[str]:
    """Достаёт origUrl из HTML выдачи Яндекс.Картинок (в т.ч. &quot;-экранирование)."""
    plain = html_lib.unescape(html or "").replace("\\/", "/")
    found: list[str] = []
    seen: set[str] = set()
    sized = re.finditer(
        r'"origWidth":\s*(\d+)\s*,\s*"origHeight":\s*(\d+)\s*,\s*"origUrl":\s*"(https?://[^"]+)"',
        plain,
    )
    for m in sized:
        w, h = int(m.group(1)), int(m.group(2))
        url = m.group(3).strip()
        if max(w, h) < min_side:
            continue
        if url and url not in seen:
            seen.add(url)
            found.append(url)
    if not found:
        for url in re.findall(r'"origUrl":\s*"(https?://[^"]+)"', plain):
            url = url.strip()
            if url and url not in seen:
                seen.add(url)
                found.append(url)
    return found


class BingProvider(ImageSearchProvider):
    name = "custom"

    def search(self, query: str, limit: int = 8) -> list[str]:
        url = (
            f"https://www.bing.com/images/search?q={quote_plus(query)}"
            f"&form=HDRSC2&first=1&adlt=strict"
            f"&qft=+filterui:photo-photo+filterui:imagesize-large"
        )
        try:
            with http_client(timeout=40.0) as client:
                r = client.get(
                    url,
                    headers={
                        "Accept-Language": "ru,en;q=0.9",
                        "User-Agent": (
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                        ),
                    },
                )
                r.raise_for_status()
                html = r.text
        except Exception as e:
            print(f"[editorial] bing images fail: {e}", flush=True)
            return []
        urls = re.findall(r"murl&quot;:&quot;(https?://.+?)&quot;", html)
        if not urls:
            urls = re.findall(r'"murl":"(https?://[^"]+)"', html)
        # Vision режет оффтоп; allowlist только поднимает спортивные CDN вверх.
        return _keep_urls(urls, limit=limit, allowlist_only=False)


def google_images_url(query: str) -> str:
    from urllib.parse import urlencode

    return "https://www.google.com/search?" + urlencode(
        {
            "q": query,
            "tbm": "isch",
            "safe": "active",
            "tbs": "itp:photo,isz:l",
            "hl": "en",
            "gl": "us",
        }
    )


class GoogleImagesProvider(ImageSearchProvider):
    """Google Images: запрос новости → оригиналы фото. SafeSearch on."""

    name = "google"

    def search(self, query: str, limit: int = 8) -> list[str]:
        url = google_images_url(query)
        try:
            with http_client(timeout=40.0) as client:
                r = client.get(
                    url,
                    headers={
                        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
                        "User-Agent": (
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                        ),
                    },
                )
                r.raise_for_status()
                html = r.text
        except Exception as e:
            print(f"[editorial] google images fail: {e}", flush=True)
            return []
        found: list[str] = []
        for m in re.finditer(r'\["(https://[^"]+)",(\d{3,5}),(\d{3,5})\]', html):
            src, width = m.group(1), int(m.group(2))
            if width < 600:
                continue
            found.append(src)
        if not found:
            found = re.findall(r'"ou":"(https://[^"]+)"', html)
        print(f"[editorial] google images hits={len(found)} q={query[:80]}", flush=True)
        return _keep_urls(found, limit=limit, allowlist_only=False)


class WikimediaProvider(ImageSearchProvider):
    name = "wikimedia"

    def search(self, query: str, limit: int = 8) -> list[str]:
        urls: list[str] = []
        urls.extend(self._commons(query, limit=limit))
        if len(urls) < limit:
            urls.extend(self._enwiki_thumbs(query, limit=limit))
        return _keep_urls(urls, limit=limit, allowlist_only=False)

    def _commons(self, query: str, *, limit: int) -> list[str]:
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": max(limit, 8),
            "prop": "imageinfo",
            "iiprop": "url|size|mime",
            "iiurlwidth": 1600,
        }
        try:
            with httpx.Client(timeout=40.0, verify=_verify(), proxy=scraper_proxy()) as client:
                r = client.get(
                    "https://commons.wikimedia.org/w/api.php",
                    params=params,
                    headers={"User-Agent": _WIKI_UA, "Accept": "application/json"},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            print(f"[editorial] wikimedia commons fail: {e}", flush=True)
            return []
        out: list[str] = []
        pages = (data.get("query") or {}).get("pages") or {}
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            mime = str(info.get("mime") or "")
            if mime not in {"image/jpeg", "image/png", "image/webp"}:
                continue
            width = int(info.get("width") or info.get("thumbwidth") or 0)
            if width and width < 600:
                continue
            url = info.get("thumburl")
            if url:
                out.append(url)
        return out

    def _enwiki_thumbs(self, query: str, *, limit: int) -> list[str]:
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": max(limit, 5),
            "prop": "pageimages",
            "pithumbsize": 1280,
            "pilicense": "any",
        }
        try:
            with httpx.Client(timeout=40.0, verify=_verify(), proxy=scraper_proxy()) as client:
                r = client.get(
                    "https://en.wikipedia.org/w/api.php",
                    params=params,
                    headers={"User-Agent": _WIKI_UA, "Accept": "application/json"},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            print(f"[editorial] wikipedia thumbs fail: {e}", flush=True)
            return []
        out: list[str] = []
        pages = (data.get("query") or {}).get("pages") or {}
        for page in pages.values():
            thumb = page.get("thumbnail") or {}
            url = thumb.get("source")
            if url:
                out.append(url)
        return out


def get_provider() -> ImageSearchProvider:
    settings = get_settings()
    key = (settings.image_search_api_key or "").strip()
    if key:
        return SerpApiProvider()
    return YandexProvider()


def _image_providers() -> list[ImageSearchProvider]:
    """Яндекс.Картинки (+ SerpAPI, если есть ключ). Wiki/Bing/Google scrape — нет."""
    settings = get_settings()
    key = (settings.image_search_api_key or "").strip()
    out: list[ImageSearchProvider] = []
    if key:
        out.append(SerpApiProvider())
    out.append(YandexProvider())
    return out


def _canon_player(name: str) -> str:
    return load_players().get(norm_name(name), (name or "").strip())


def _players_for_photo(item: dict[str, Any]) -> list[str]:
    try:
        entities = json.loads(item.get("entities_json") or "{}")
    except Exception:
        entities = {}
    players = [_canon_player(str(p)) for p in (entities.get("players") or []) if p]
    if players:
        return players[:1]
    blob = norm_name(
        " ".join(str(item.get(k) or "") for k in ("title", "caption", "headline", "caption_line1"))
    )
    found: list[str] = []
    seen: set[str] = set()
    for alias, canon in load_players().items():
        if not alias or len(alias) < 4 or alias not in blob:
            continue
        if canon not in seen:
            seen.add(canon)
            found.append(canon)
    return found[:1]


def _teams_for_photo(item: dict[str, Any]) -> list[str]:
    try:
        entities = json.loads(item.get("entities_json") or "{}")
    except Exception:
        entities = {}
    teams = [canonical_team(str(t)) for t in (entities.get("teams") or []) if t]
    if not teams:
        return []
    fifa = load_fifa_top100_names()
    fifa.add(norm_name("Russia"))
    clubs = [t for t in teams if norm_name(t) not in fifa]
    if clubs:
        return clubs[:1]
    if entities.get("is_national"):
        return teams[:1]
    return teams[:1]


def _entity_query(item: dict[str, Any]) -> str:
    bits = [*_players_for_photo(item), *_teams_for_photo(item), "футбол", "фото"]
    return " ".join(b for b in bits if b).strip()


_QUERY_QUOTES = re.compile(r"[«»\"“”]")
_QUERY_VERB = re.compile(
    r"победил|выиграл|проиграл|забил|переш|возглавил|подписал|удал|выйдет|состав",
    re.I,
)
_QUERY_FILLERS = (
    re.compile(r"\bв\s+\S+\s+раз(?:\s+подряд)?\b", re.I),
    re.compile(r"\bподряд\b", re.I),
    re.compile(r"\bв\s+xxi\s+веке\b", re.I),
    re.compile(r"\bэто лучший результат\b.*", re.I),
    re.compile(r"\bв\s+своём\s+матче\s+за\b", re.I),
    re.compile(r"\s*[—\-–]\s*источник рассказал.*", re.I),
    re.compile(r"\s*\(?фабрицио романо\)?.*", re.I),
    re.compile(r"\s+в гостях\b", re.I),
    re.compile(r"\s+на выезде\b", re.I),
)


def _clean_title(title: str) -> str:
    t = _QUERY_QUOTES.sub("", title or "")
    t = t.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", t).strip()


_SPEECH_QUOTE = re.compile(r"[«\"“]([^»\"”]{8,})[»\"”]")
_QUOTE_THEN_SPEAKER = re.compile(
    r"^[«\"“][^»\"”]+[»\"”]\.?\s+"
    r"(?P<who>[А-ЯЁA-Z][\w.\-]+(?:\s+[А-ЯЁA-Z][\w.\-]+)?)"
    r"(?:\s*[—\-–:]\s*о\s+(?P<rest>.+))?",
)
_SPEAKER_THEN_QUOTE = re.compile(
    r"^(?P<who>[^«\"“:'’]{2,48}?)\s*(?::|[—\-–])\s*[«\"“'’]"
)


def _speech_quotes(title: str) -> list[str]:
    """Цитата героя, не «Арсенал» в одно слово."""
    out: list[str] = []
    for q in _SPEECH_QUOTE.findall(title or ""):
        if len(q.split()) >= 4:
            out.append(q.strip())
    return out


def _club_after_about(rest: str) -> str:
    """Первый клуб после «о …», не соперник после «от»."""
    for club in re.findall(r"«([^»]+)»", rest or ""):
        idx = rest.find(f"«{club}»")
        before = rest[max(0, idx - 6) : idx]
        if re.search(r"\bот\s*$", before):
            continue
        return club.strip()
    return ""


def _quote_author_query(title: str, *, year: str = "") -> str:
    """Цитата в заголовке → автор + его клуб, не слова цитаты."""
    raw = (title or "").strip()
    if not raw or not _speech_quotes(raw):
        m_speaker = _SPEAKER_THEN_QUOTE.match(raw)
        if not m_speaker:
            return ""
        who = m_speaker.group("who").strip(" .")
        if who and not _QUERY_VERB.search(who):
            return _finish_query(who, year)
        return ""
    m = _QUOTE_THEN_SPEAKER.match(raw)
    if m:
        who = (m.group("who") or "").strip()
        club = _club_after_about(m.group("rest") or "")
        if who and club:
            return _finish_query(f"{who} {club}", year)
        if who:
            return _finish_query(who, year)
    m = _SPEAKER_THEN_QUOTE.match(raw)
    if m:
        who = m.group("who").strip(" .")
        if who and not _QUERY_VERB.search(who):
            return _finish_query(who, year)
    return ""


def _query_is_quote_dump(q: str, title: str) -> bool:
    """LLM скопировал цитату вместо автора."""
    qn = re.sub(r"\s+", " ", (q or "").lower()).strip()
    if len(qn) < 8:
        return False
    qw = set(qn.split())
    for quote in _speech_quotes(title):
        qn_quote = re.sub(r"\s+", " ", quote.lower()).strip()
        if qn in qn_quote or qn_quote in qn:
            return True
        tw = set(qn_quote.split())
        if qw and tw and len(qw & tw) / len(qw) >= 0.7:
            return True
    return False


def _item_year(item: dict[str, Any]) -> str:
    for key in ("published_at", "source_published_at"):
        raw = str(item.get(key) or "")
        m = re.match(r"(20\d{2})", raw)
        if m:
            return m.group(1)
    return ""


def _finish_query(q: str, year: str = "") -> str:
    q = re.sub(r"\bфото\b", " ", q or "", flags=re.I)
    q = re.sub(r"\s+", " ", q).strip(" ,.;:-")
    if (
        year
        and year not in q
        and re.search(r"суперкубок|финал|\bкубок\b", q, re.I)
        and not re.search(r"\b20\d{2}\b", q)
    ):
        q = f"{q} {year}"
    words = q.split()
    if len(words) > 8:
        q = " ".join(words[:8])
    return q[:72]


def _compact_query(title: str, *, year: str = "") -> str:
    """Короткий запрос как человек: субъект + событие, не весь заголовок."""
    raw = (title or "").strip()
    if not raw:
        return ""
    quoted = _quote_author_query(raw, year=year)
    if quoted:
        return quoted
    t = _clean_title(raw)

    squad = re.search(r"в составе\s+«([^»]+)»", raw, re.I) or re.search(
        r"в составе\s+(\S+)", t, re.I
    )
    if squad:
        left = re.split(r"в составе", t, maxsplit=1, flags=re.I)[0]
        left = re.sub(r"[—\-–]", " ", left).strip(" ,")
        who = re.split(r"\s+и\s+", left, maxsplit=1)[0].strip()
        who = re.sub(r"\s+выйдет в стартовом составе$", "", who, flags=re.I).strip()
        club = squad.group(1).strip(" «»")
        if who and club:
            return _finish_query(f"{who} {club}", year)

    start = re.search(
        r"^(?P<who>.+?)\s+выйдет в стартовом составе\s+(?P<club>\S+)",
        t,
        re.I,
    )
    if start:
        return _finish_query(f"{start.group('who')} {start.group('club')}", year)

    kicker_m = re.match(r"^([^.]{3,40})\.\s+(.+)$", raw)
    if kicker_m and not _QUERY_VERB.search(_clean_title(kicker_m.group(1))):
        kicker = _clean_title(kicker_m.group(1))
        first_bit = re.split(r",\s+«", kicker_m.group(2), maxsplit=1)[0]
        names = re.findall(r"«([^»]+)»", first_bit)
        if len(names) >= 2:
            return _finish_query(f"{kicker} матч {names[0]} {names[1]}", year)

    parts = re.split(r"\.\s+", t, maxsplit=1)
    if len(parts) == 2 and not _QUERY_VERB.search(parts[1]) and len(parts[1]) < 48:
        t = parts[0]
    for rx in _QUERY_FILLERS:
        t = rx.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.—-")
    return _finish_query(t, year)


def _query_usable(q: str) -> bool:
    words = (q or "").split()
    if len(words) < 2 or len(words) > 6:
        return False
    if len(q) > 56:
        return False
    if re.search(
        r"xxi|в\s+\S+\s+раз|лучший результат|источник рассказал|это лучший",
        q,
        re.I,
    ):
        return False
    has_event = bool(re.search(r"выиграл|победил|матч|переш|возглавил|кубок|лига", q, re.I))
    caps = sum(1 for w in words if w[:1].isupper())
    if caps >= 4 and not has_event:
        return False
    return True


def _players_in_title(item: dict[str, Any]) -> list[str]:
    blob = norm_name(str(item.get("title") or ""))
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()
    for alias, canon in load_players().items():
        if not alias or len(alias) < 4 or alias not in blob:
            continue
        if canon in seen:
            continue
        pos = blob.find(alias)
        if pos < 0:
            continue
        seen.add(canon)
        hits.append((pos, canon))
    hits.sort()
    return [canon for _, canon in hits[:2]]


def _query_for(item: dict[str, Any]) -> str:
    """Поиск картинок: короткий запрос по сути новости, не entities и не весь заголовок."""
    title = str(item.get("title") or "")
    year = _item_year(item)
    llm_q = ""
    try:
        from editorial.llm import image_search_query

        llm_q = image_search_query(
            title,
            year=year,
            event_type=str(item.get("event_type") or ""),
        )
        llm_q = re.sub(r"\bфото\b", " ", llm_q or "", flags=re.I)
        llm_q = re.sub(r"\s+", " ", llm_q).strip(" ,.;:-")
    except Exception as e:
        print(f"[editorial] image query llm fail: {e}", flush=True)
        llm_q = ""
    if _query_usable(llm_q) and not _query_is_quote_dump(llm_q, title):
        return llm_q[:72]
    q = _compact_query(title, year=year)
    if q:
        return q
    fallback = _entity_query(item)
    if fallback and fallback not in {"футбол фото", "футбол", "фото"}:
        return _finish_query(fallback)
    return "футбол"


_DL_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
_OG_NAME = re.compile(
    r'(?:property|name)\s*=\s*["\'](?:og:image|twitter:image)(?::url)?["\']',
    re.I,
)
_OG_CONTENT = re.compile(r'content\s*=\s*["\']([^"\']+)', re.I)


_CHAMP_SIZED = re.compile(
    r"https://img\.championat\.com/(?:s|c)/\d+x\d+/(news/big/.+\.(?:jpg|jpeg|png|webp))",
    re.I,
)
_CHAMP_BARE = re.compile(
    r"https://img\.championat\.com/(news/big/.+\.(?:jpg|jpeg|png|webp))",
    re.I,
)
_ARTICLE_IMG = re.compile(
    r'(?:src|data-src|data-original|content)\s*=\s*["\']([^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
    re.I,
)


def championat_photo_path(url: str) -> str | None:
    raw = (url or "").split("?")[0].strip()
    m = _CHAMP_SIZED.search(raw)
    if m:
        return m.group(1)
    m = _CHAMP_BARE.search(raw)
    if m:
        return m.group(1)
    return None


def publisher_image_variants(url: str) -> list[str]:
    """og:image Championat 1200×630 → квадрат 900×900 и оригинал /news/big/."""
    raw = (url or "").strip()
    if not raw:
        return []
    path = championat_photo_path(raw)
    if not path:
        return [raw]
    variants = [
        f"https://img.championat.com/c/900x900/{path}",
        f"https://img.championat.com/c/1200x900/{path}",
        f"https://img.championat.com/{path}",
        raw,
    ]
    seen: set[str] = set()
    out: list[str] = []
    for item in variants:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_og_images(html: str, base_url: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for tag in _META_TAG.findall(html or ""):
        if not _OG_NAME.search(tag):
            continue
        cm = _OG_CONTENT.search(tag)
        if not cm:
            continue
        raw = html_lib.unescape(cm.group(1).strip())
        if raw.startswith("//"):
            raw = "https:" + raw
        url = urljoin(base_url, raw)
        if url in seen:
            continue
        seen.add(url)
        found.append(url)
    return found


def article_image_urls(article_url: str, *, limit: int = 8) -> list[str]:
    article_url = (article_url or "").strip()
    if not article_url.lower().startswith(("http://", "https://")):
        return []
    try:
        with http_client(timeout=20.0) as client:
            r = client.get(
                article_url,
                headers={"User-Agent": _DL_UA, "Accept": "text/html,application/xhtml+xml"},
            )
            r.raise_for_status()
            html = r.text[:400_000]
    except Exception as e:
        print(f"[editorial] article og:image fail: {e}", flush=True)
        return []
    raw: list[str] = []
    raw.extend(parse_og_images(html, article_url))
    for m in _ARTICLE_IMG.finditer(html):
        src = html_lib.unescape(m.group(1).strip())
        if src.startswith("//"):
            src = "https:" + src
        src = urljoin(article_url, src)
        raw.append(src)
    expanded: list[str] = []
    for url in raw:
        expanded.extend(publisher_image_variants(url))
    return _keep_urls(expanded, limit=limit, allowlist_only=False)


_skip_until: dict[str, float] = {}


def _download(url: str, dest: Path) -> Path | None:
    if url_is_blocked(url):
        print(f"[editorial] skip blocked image host: {_host(url)}", flush=True)
        return None
    host = _host(url)
    if time.monotonic() < _skip_until.get(host, 0):
        return None
    wiki = "wikimedia" in host or "wikipedia" in host
    ua = _WIKI_UA if wiki else _DL_UA
    referer = "https://yandex.ru/"
    if "championat.com" in host:
        referer = "https://www.championat.com/"
    elif "sports.ru" in host:
        referer = "https://www.sports.ru/"
    elif wiki:
        referer = "https://commons.wikimedia.org/"
    if wiki:
        time.sleep(0.12)
    try:
        with http_client(timeout=30.0) as client:
            r = client.get(
                url,
                headers={
                    "User-Agent": ua,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Referer": referer,
                },
            )
            if r.status_code == 429:
                _skip_until[host] = time.monotonic() + 20.0
                print(f"[editorial] 429 {host} — pause 20s", flush=True)
                return None
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").lower()
            if ctype and not ctype.startswith("image/"):
                return None
            data = r.content
        if len(data) < 8000:
            return None
        dest.write_bytes(data)
        return dest
    except Exception as e:
        print(f"[editorial] image dl fail: {e}", flush=True)
        return None


TEMPLATE_SIZE = {
    "breaking": (1080, 1080),
    "transfer": (1080, 1080),
    "default": (1080, 1080),
    "matchday": (1080, 1350),
    "result": (1080, 1080),
}

FACE_DIR = ROOT / "editorial" / "templates" / "assets" / "face"


@dataclass
class ImageCandidate:
    path: Path
    url: str
    via: str
    width: int
    height: int
    relevance: float = 0.0
    relevant: bool = False
    subject_present: bool = False
    reason: str = ""
    quality_label: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def _imagery_settings() -> Any:
    return get_settings()


def _template_size(template: str) -> tuple[int, int]:
    return TEMPLATE_SIZE.get(template) or TEMPLATE_SIZE["default"]


def crop_dims(src_w: int, src_h: int, target_w: int, target_h: int) -> tuple[int, int, float]:
    """Максимальное окно исходника с аспектом шаблона + нужный апскейл до цели."""
    if src_w < 1 or src_h < 1 or target_w < 1 or target_h < 1:
        return 0, 0, 99.0
    tr = target_w / target_h
    sr = src_w / src_h
    if sr > tr:
        crop_h = src_h
        crop_w = max(1, int(round(src_h * tr)))
    else:
        crop_w = src_w
        crop_h = max(1, int(round(src_w / tr)))
    upscale = max(target_w / crop_w, target_h / crop_h)
    return crop_w, crop_h, upscale


def _open_rgb(path: Path) -> Image.Image:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


def _laplacian_var(gray) -> float:
    try:
        import cv2
        import numpy as np

        arr = gray if hasattr(gray, "ndim") else None
        if arr is None:
            return 0.0
        if arr.dtype != np.uint8:
            arr = arr.astype("uint8")
        return float(cv2.Laplacian(arr, cv2.CV_64F).var())
    except Exception:
        return 0.0


def _gray_array(im: Image.Image):
    import numpy as np

    return np.array(im.convert("L"), dtype="uint8")


def _average_hash(path: Path | str, *, size: int = 8) -> int:
    im = _open_rgb(Path(path))
    im = im.resize((size, size), Image.Resampling.LANCZOS).convert("L")
    pixels = list(im.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for i, p in enumerate(pixels):
        if p >= avg:
            bits |= 1 << i
    return bits


def _hamming64(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _is_near_duplicate(h: int, seen: list[int], *, max_dist: int = 6) -> bool:
    return any(_hamming64(h, s) <= max_dist for s in seen)


def dedupe_image_candidates(
    candidates: list[ImageCandidate],
    *,
    max_hamming: int = 6,
) -> list[ImageCandidate]:
    """Убирает визуальные дубликаты (один кадр с разных URL/размеров)."""
    kept: list[ImageCandidate] = []
    seen: list[int] = []
    for cand in candidates:
        try:
            h = _average_hash(cand.path)
        except Exception:
            kept.append(cand)
            continue
        if _is_near_duplicate(h, seen, max_dist=max_hamming):
            continue
        seen.append(h)
        kept.append(cand)
    return kept


def quality_ok(path: Path | str, template: str = "default") -> tuple[bool, str]:
    """Локальный quality-gate: размер под шаблон, аспект, резкость, тёмные пиксели."""
    settings = _imagery_settings()
    p = Path(path)
    try:
        im = _open_rgb(p)
    except Exception:
        return False, "unreadable"
    w, h = im.size
    tw, th = _template_size(template)
    max_up = float(getattr(settings, "imagery_max_upscale", 1.3) or 1.3)
    max_aspect = float(getattr(settings, "imagery_max_aspect_delta", 0.4) or 0.4)
    min_sharp = float(getattr(settings, "imagery_min_sharpness", 100) or 100)
    max_dark = float(getattr(settings, "imagery_max_dark_ratio", 0.55) or 0.55)

    crop_w, crop_h, upscale = crop_dims(w, h, tw, th)
    if crop_w < 1 or upscale > max_up + 1e-6:
        return False, f"upscale {upscale:.2f}x > {max_up}"

    src_ratio = w / max(1, h)
    tgt_ratio = tw / max(1, th)
    if abs(src_ratio - tgt_ratio) > max_aspect:
        return False, f"aspect delta {abs(src_ratio - tgt_ratio):.2f} > {max_aspect}"

    import numpy as np

    gray = _gray_array(im)
    dark_ratio = float((gray < 22).mean()) if gray.size else 1.0
    if dark_ratio > max_dark:
        return False, f"dark {dark_ratio:.2f} > {max_dark}"
    sharp = _laplacian_var(gray)
    if sharp < min_sharp:
        return False, f"blur {sharp:.1f} < {min_sharp}"
    _ = np
    return True, "ok"


def preview_jpeg(path: Path | str, *, max_side: int | None = None, quality: int = 70) -> bytes:
    if max_side is None:
        settings = _imagery_settings()
        max_side = int(getattr(settings, "imagery_preview_max_side", 512) or 512)
    im = _open_rgb(Path(path))
    w, h = im.size
    scale = min(1.0, max_side / max(w, h, 1))
    if scale < 1:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _entity_tokens(item: dict[str, Any]) -> list[str]:
    try:
        entities = json.loads(item.get("entities_json") or "{}")
    except Exception:
        entities = {}
    tokens: list[str] = []
    for p in (entities.get("players") or [])[:3]:
        tokens.append(str(p))
        tokens.append(_canon_player(str(p)))
    for t in (entities.get("teams") or [])[:3]:
        tokens.append(str(t))
        tokens.append(canonical_team(str(t)))
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        n = norm_name(tok)
        if n and n not in seen and len(n) >= 4:
            seen.add(n)
            out.append(n)
    return out


def _strong_text_match(url: str, item: dict[str, Any]) -> bool:
    blob = re.sub(r"[^a-z0-9а-яё]+", "", (url or "").lower())
    title = re.sub(r"[^a-z0-9а-яё]+", "", str(item.get("title") or "").lower())
    hay = blob + title
    players = []
    try:
        entities = json.loads(item.get("entities_json") or "{}")
        players = [norm_name(str(p)) for p in (entities.get("players") or []) if p]
    except Exception:
        players = []
    for p in players:
        compact = re.sub(r"[^a-z0-9а-яё]+", "", p)
        if len(compact) >= 5 and compact in hay:
            return True
    return False


def _needs_strict_attribution(item: dict[str, Any]) -> bool:
    try:
        entities = json.loads(item.get("entities_json") or "{}")
    except Exception:
        entities = {}
    if entities.get("players") or entities.get("teams"):
        return True
    et = str(item.get("event_type") or "")
    return et in {"transfer", "injury", "match_result", "lineup", "official_statement"}


def _relevance_prompt(item: dict[str, Any], n: int) -> str:
    try:
        entities = json.loads(item.get("entities_json") or "{}")
    except Exception:
        entities = {}
    players = ", ".join(str(p) for p in (entities.get("players") or [])[:4]) or "—"
    clubs = ", ".join(str(t) for t in (entities.get("teams") or [])[:4]) or "—"
    etype = str(item.get("event_type") or "other")
    title = str(item.get("title") or "")[:180]
    strict = _needs_strict_attribution(item)
    attr_rule = (
        "СТРОГАЯ атрибуция: клуб/лига на фото ДОЛЖНЫ совпадать с новостью. "
        "attribution_match=false → reject."
        if strict
        else "Мягкая атрибуция: generic-новость — attribution_match может быть unknown."
    )
    return (
        f"На вход {n} фото (idx 0..{n-1}) к футбольной новости.\n"
        f"Заголовок: {title}\n"
        f"Игрок(и): {players}\n"
        f"Клуб(ы)/сборная: {clubs}\n"
        f"Тип события: {etype}\n"
        f"{attr_rule}\n"
        "Иллюстрируй ЗАГОЛОВОК, не любой клуб из списка.\n"
        "Субъект = о ком новость (кто выиграл / кого назвали первым). "
        "Соперник, с которым играют, — не субъект, если заголовок не про него.\n"
        "Эмблема/логотип без игроков: relevant=false, кроме официального заявления без людей.\n"
        "Текст НА фото (плашка, цитата, титры, инфографика, чужая надпись, скрин соцсети): "
        "relevant=false и has_overlay_text=true.\n"
        "Исключения — не считать текстом: логотип/эмблема клуба, спонсор и номер на форме, "
        "фамилия на спине, Here we go / Here We Go (Романо). Остальной текст — мимо.\n"
        "На каждом фото — то, что относится к этой новости? Есть ли релевантный субъект "
        "(нужный игрок / форма клуба / сцена)? Нет ли постороннего (другая команда, "
        "неспорт, коллаж, чужой вотермарк, текст на кадре)?\n"
        "Верни JSON вида:\n"
        '{"results":[{"idx":0,"relevant":true,"subject_present":true,'
        '"club_on_photo":"Arsenal|Krasnodar|unknown|none",'
        '"league_on_photo":"EPL|RPL|unknown",'
        '"attribution_match":true,'
        '"who":"кто на фото, 1 фраза","wrong_subject":false,"has_overlay_text":false,'
        '"reason":"почему берём или режем","quality":"good|ok|poor","score":0.0}]}\n'
        "score от 0 до 1. relevant=false если другая команда/не та тема/текст на фото "
        "или attribution_match=false при строгой атрибуции. "
        "wrong_subject=true если человек или клуб не те."
    )


def _manual_query_match(url: str, query: str, item: dict[str, Any]) -> bool:
    q = norm_name(query)
    if len(q) >= 4:
        blob = re.sub(r"[^a-z0-9а-яё]+", "", (url or "").lower())
        if q in blob:
            return True
    return _strong_text_match(url, item)


_VIA_PRIORITY: dict[str, int] = {
    "article": 0,
    "og": 0,
    "wikimedia": 1,
    "wiki": 1,
    "yandex": 2,
    "bing": 2,
    "serpapi": 2,
}


def _via_rank(via: str) -> int:
    return _VIA_PRIORITY.get((via or "").strip().lower(), 3)


def _is_og_source(via: str) -> bool:
    return (via or "").strip().lower() in {"article", "og"}


def _is_search_source(via: str) -> bool:
    return (via or "").strip().lower() in {"yandex", "bing", "wikimedia", "wiki", "serpapi"}


def _sharpness_score(path: Path) -> float:
    try:
        return _laplacian_var(_gray_array(_open_rgb(path)))
    except Exception:
        return 0.0


def _script_pick_candidate(candidates: list[ImageCandidate]) -> ImageCandidate | None:
    """Скриптовый выбор лучшего: og/article > wiki > поиск; внутри — разрешение и резкость."""
    if not candidates:
        return None

    def key(c: ImageCandidate) -> tuple[int, int, float]:
        return (_via_rank(c.via), -(c.width * c.height), -_sharpness_score(c.path))

    return min(candidates, key=key)


def _should_call_vision(cand: ImageCandidate, item: dict[str, Any]) -> bool:
    settings = _imagery_settings()
    if bool(getattr(settings, "vision_skip_for_og", True)) and _is_og_source(cand.via):
        return False
    if _is_search_source(cand.via):
        return True
    if _needs_strict_attribution(item) and not _is_og_source(cand.via):
        return True
    return False


def _apply_vision_row(
    cand: ImageCandidate,
    row: dict[str, Any],
    item: dict[str, Any],
    *,
    min_rel: float,
) -> bool:
    relevant = bool(row.get("relevant"))
    subject = bool(row.get("subject_present"))
    overlay = bool(row.get("has_overlay_text"))
    attr_match = row.get("attribution_match")
    strict = _needs_strict_attribution(item)
    if strict and attr_match is False:
        relevant = False
    try:
        score = float(row.get("score") if row.get("score") is not None else 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if overlay:
        relevant = False
    cand.relevant = relevant
    cand.subject_present = subject
    cand.relevance = score
    cand.reason = str(row.get("reason") or row.get("who") or "")[:240]
    if overlay and "текст" not in cand.reason.lower():
        cand.reason = ("текст на фото. " + cand.reason).strip()[:240]
    cand.quality_label = str(row.get("quality") or "")
    cand.extras = {
        "who": str(row.get("who") or "")[:160],
        "wrong_subject": bool(row.get("wrong_subject")),
        "has_overlay_text": overlay,
        "club_on_photo": str(row.get("club_on_photo") or "")[:80],
        "league_on_photo": str(row.get("league_on_photo") or "")[:40],
        "attribution_match": attr_match,
    }
    return bool(relevant and subject and score >= min_rel)


def verify_single_photo(
    cand: ImageCandidate,
    item: dict[str, Any],
    *,
    trace: dict[str, Any] | None = None,
) -> ImageCandidate | None:
    """Финальная vision-проверка одной выбранной фотки (атрибуция)."""
    settings = _imagery_settings()
    min_rel = float(getattr(settings, "imagery_min_relevance", 0.55) or 0.55)
    model = (getattr(settings, "editorial_vision_model", None) or "gpt-5.6-luna").strip()
    prompt = _relevance_prompt(item, 1)
    try:
        from editorial.openai_client import get_client

        client = get_client()
        vision_ab = bool(getattr(settings, "vision_ab", False))
        ab_models = [
            ("gpt-4o-mini", "image_vision_ab_mini"),
            ((settings.editorial_text_model or "gpt-5.6-luna").strip(), "image_vision_ab_luna"),
        ]
        if vision_ab:
            for ab_model, ab_task in ab_models:
                try:
                    client.vision(
                        ab_model,
                        [preview_jpeg(cand.path)],
                        prompt,
                        json_mode=True,
                        max_tokens=400,
                        task=ab_task,
                    )
                except Exception as e:
                    print(f"[editorial] vision A/B {ab_task} fail: {e}", flush=True)
        data = client.vision(
            model,
            [preview_jpeg(cand.path)],
            prompt,
            json_mode=True,
            max_tokens=400,
            task="image_vision",
        )
        row: dict[str, Any] = {}
        if isinstance(data, dict):
            rows = data.get("results")
            if isinstance(rows, list) and rows:
                row = rows[0] if isinstance(rows[0], dict) else {}
            else:
                row = data
        kept = _apply_vision_row(cand, row, item, min_rel=min_rel)
        if trace is not None:
            trace["vision"] = {
                "model": model,
                "prompt": prompt,
                "min_relevance": min_rel,
                "single": True,
                "candidates": [
                    {
                        "idx": 0,
                        "url": cand.url,
                        "via": cand.via,
                        "width": cand.width,
                        "height": cand.height,
                        "path": str(cand.path),
                        "relevant": cand.relevant,
                        "subject_present": cand.subject_present,
                        "wrong_subject": cand.extras.get("wrong_subject"),
                        "has_overlay_text": cand.extras.get("has_overlay_text"),
                        "score": cand.relevance,
                        "reason": cand.reason,
                        "kept": kept,
                    }
                ],
                "error": None,
            }
        if kept:
            return cand
        print(
            f"[editorial] vision drop via={cand.via} score={cand.relevance:.2f} {cand.reason[:80]}",
            flush=True,
        )
        return None
    except Exception as e:
        print(f"[editorial] single vision fail, text fallback: {e}", flush=True)
        if _strong_text_match(cand.url, item):
            cand.relevant = True
            cand.subject_present = True
            cand.relevance = 0.6
            cand.reason = "text-match fallback"
            if trace is not None:
                trace["vision"] = {"model": model, "single": True, "error": str(e)[:400]}
            return cand
        if trace is not None:
            trace["vision"] = {"model": model, "single": True, "error": str(e)[:400]}
        return None


def _pick_with_optional_vision(
    pool: list[ImageCandidate],
    item: dict[str, Any],
    *,
    trace: dict[str, Any] | None = None,
) -> ImageCandidate | None:
    """Скрипт выбирает лучшего; vision — только на поисковых / строгой атрибуции."""
    if not pool:
        return None
    ordered = sorted(
        pool,
        key=lambda c: (_via_rank(c.via), -(c.width * c.height), -_sharpness_score(c.path)),
    )
    tried: set[str] = set()
    for cand in ordered:
        key = cand.url
        if key in tried:
            continue
        tried.add(key)
        if trace is not None and "script_pick" not in trace:
            trace["script_pick"] = {
                "url": cand.url,
                "via": cand.via,
                "width": cand.width,
                "height": cand.height,
            }
        if not _should_call_vision(cand, item):
            cand.relevant = True
            cand.subject_present = True
            cand.relevance = 0.85
            cand.reason = "og:image — доверие первоисточнику"
            if trace is not None:
                trace["vision_skipped"] = f"via={cand.via}"
            return cand
        verified = verify_single_photo(cand, item, trace=trace)
        if verified:
            return verified
    return None

def score_relevance(
    candidates: list[ImageCandidate],
    item: dict[str, Any],
    *,
    trace: dict[str, Any] | None = None,
    manual_relaxed: bool = False,
) -> list[ImageCandidate]:
    """Батч-vision. При ошибке — только сильное текстовое совпадение, иначе пусто."""
    if not candidates:
        return []
    settings = _imagery_settings()
    min_rel = float(getattr(settings, "imagery_min_relevance", 0.55) or 0.55)
    if manual_relaxed:
        min_rel = min(min_rel, 0.35)

    settings = _imagery_settings()
    use_single = bool(getattr(settings, "vision_single_candidate", True))
    if len(candidates) == 1 and not manual_relaxed and use_single:
        cand = candidates[0]
        if not _should_call_vision(cand, item):
            cand.relevant = True
            cand.subject_present = True
            cand.relevance = 0.85
            cand.reason = "og:image — доверие первоисточнику"
            if trace is not None:
                trace["vision_skipped"] = f"via={cand.via}"
            return [cand]
        verified = verify_single_photo(cand, item, trace=trace)
        return [verified] if verified else []

    model = (getattr(settings, "editorial_vision_model", None) or "gpt-5.6-luna").strip()
    prompt = _relevance_prompt(item, len(candidates))
    max_tokens = min(2200, 180 + len(candidates) * 140)
    try:
        from editorial.openai_client import get_client

        previews = [preview_jpeg(c.path) for c in candidates]
        client = get_client()
        vision_ab = bool(getattr(settings, "vision_ab", False))
        ab_models = [
            ("gpt-4o-mini", "image_vision_ab_mini"),
            ((settings.editorial_text_model or "gpt-5.6-luna").strip(), "image_vision_ab_luna"),
        ]
        if vision_ab:
            for ab_model, ab_task in ab_models:
                try:
                    client.vision(
                        ab_model,
                        previews,
                        prompt,
                        json_mode=True,
                        max_tokens=max_tokens,
                        task=ab_task,
                    )
                except Exception as e:
                    print(f"[editorial] vision A/B {ab_task} fail: {e}", flush=True)
        data = client.vision(
            model,
            previews,
            prompt,
            json_mode=True,
            max_tokens=max_tokens,
            task="image_vision",
        )
        rows = data.get("results") if isinstance(data, dict) else None
        by_idx: dict[int, dict[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    by_idx[int(row.get("idx"))] = row
                except (TypeError, ValueError):
                    continue
        kept: list[ImageCandidate] = []
        vision_rows: list[dict[str, Any]] = []
        for i, cand in enumerate(candidates):
            row = by_idx.get(i) or {}
            relevant = bool(row.get("relevant"))
            subject = bool(row.get("subject_present"))
            overlay = bool(row.get("has_overlay_text"))
            attr_match = row.get("attribution_match")
            strict = _needs_strict_attribution(item)
            if strict and attr_match is False:
                relevant = False
            try:
                score = float(row.get("score") if row.get("score") is not None else 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if overlay:
                relevant = False
            cand.relevant = relevant
            cand.subject_present = subject
            cand.relevance = score
            cand.reason = str(row.get("reason") or row.get("who") or "")[:240]
            if overlay and "текст" not in cand.reason.lower():
                cand.reason = ("текст на фото. " + cand.reason).strip()[:240]
            cand.quality_label = str(row.get("quality") or "")
            cand.extras = {
                "who": str(row.get("who") or "")[:160],
                "wrong_subject": bool(row.get("wrong_subject")),
                "has_overlay_text": overlay,
                "club_on_photo": str(row.get("club_on_photo") or "")[:80],
                "league_on_photo": str(row.get("league_on_photo") or "")[:40],
                "attribution_match": attr_match,
            }
            vision_rows.append(
                {
                    "idx": i,
                    "url": cand.url,
                    "via": cand.via,
                    "width": cand.width,
                    "height": cand.height,
                    "path": str(cand.path),
                    "relevant": relevant,
                    "subject_present": subject,
                    "wrong_subject": cand.extras["wrong_subject"],
                    "has_overlay_text": overlay,
                    "club_on_photo": cand.extras["club_on_photo"],
                    "league_on_photo": cand.extras["league_on_photo"],
                    "attribution_match": attr_match,
                    "who": cand.extras["who"],
                    "score": score,
                    "quality": cand.quality_label,
                    "reason": cand.reason,
                    "kept": bool(relevant and subject and score >= min_rel),
                }
            )
            if relevant and subject and score >= min_rel:
                kept.append(cand)
            else:
                print(
                    f"[editorial] vision drop idx={i} via={cand.via} "
                    f"rel={relevant} subj={subject} score={score:.2f} {cand.reason[:80]}",
                    flush=True,
                )
        kept.sort(key=lambda c: (c.relevance, c.width * c.height), reverse=True)
        if trace is not None:
            trace["vision"] = {
                "model": model,
                "prompt": prompt,
                "min_relevance": min_rel,
                "candidates": vision_rows,
                "error": None,
            }
        return kept
    except Exception as e:
        print(f"[editorial] vision fail, text fallback: {e}", flush=True)
        query = str((trace or {}).get("query") or "")
        if manual_relaxed:
            kept = list(candidates)
            for c in kept:
                c.relevant = True
                c.subject_present = True
                c.relevance = 0.55
                c.reason = "ручной запрос (vision недоступен)"
        else:
            kept = [c for c in candidates if _strong_text_match(c.url, item)]
            for c in kept:
                c.relevant = True
                c.subject_present = True
                c.relevance = 0.6
                c.reason = "text-match fallback"
        if manual_relaxed and not kept and query:
            kept = [c for c in candidates if _manual_query_match(c.url, query, item)]
            for c in kept:
                c.relevant = True
                c.subject_present = True
                c.relevance = 0.5
                c.reason = "ручной запрос (совпадение URL)"
        if trace is not None:
            trace["vision"] = {
                "model": model,
                "prompt": prompt,
                "min_relevance": min_rel,
                "candidates": [
                    {
                        "idx": i,
                        "url": c.url,
                        "via": c.via,
                        "kept": c in kept,
                        "reason": "text-match fallback" if c in kept else "vision error",
                    }
                    for i, c in enumerate(candidates)
                ],
                "error": str(e)[:400],
            }
        return kept


def _haar_cascade():
    import cv2

    path = getattr(cv2, "data", None)
    xml = ""
    if path is not None:
        xml = str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    if not xml or not Path(xml).is_file():
        return None
    return cv2.CascadeClassifier(xml)


def _dnn_net():
    try:
        import cv2
    except Exception:
        return None
    if not hasattr(getattr(cv2, "dnn", None), "readNetFromCaffe"):
        return None
    proto = FACE_DIR / "deploy.prototxt"
    model = FACE_DIR / "res10_300x300_ssd_iter_140000.caffemodel"
    if not proto.is_file() or not model.is_file():
        return None
    try:
        return cv2.dnn.readNetFromCaffe(str(proto), str(model))
    except Exception as e:
        print(f"[editorial] face dnn skip: {e}", flush=True)
        return None


def detect_faces(im: Image.Image, *, backend: str | None = None) -> list[tuple[int, int, int, int]]:
    """Список bbox (x, y, w, h) в координатах исходника."""
    settings = _imagery_settings()
    name = (backend or getattr(settings, "imagery_face_backend", None) or "opencv_dnn").strip().lower()
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    bgr = cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    boxes: list[tuple[int, int, int, int]] = []
    if name == "opencv_dnn":
        net = _dnn_net()
        if net is not None:
            blob = cv2.dnn.blobFromImage(
                cv2.resize(bgr, (300, 300)), 1.0, (300, 300), (104.0, 177.0, 123.0)
            )
            net.setInput(blob)
            det = net.forward()
            for i in range(det.shape[2]):
                conf = float(det[0, 0, i, 2])
                if conf < 0.5:
                    continue
                x1, y1, x2, y2 = det[0, 0, i, 3:7] * np.array([w, h, w, h])
                x, y = int(max(0, x1)), int(max(0, y1))
                bw, bh = int(min(w, x2) - x), int(min(h, y2) - y)
                if bw >= 24 and bh >= 24:
                    boxes.append((x, y, bw, bh))
    if not boxes:
        cascade = _haar_cascade()
        if cascade is not None:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            found = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
            for (x, y, fw, fh) in found:
                boxes.append((int(x), int(y), int(fw), int(fh)))
    return boxes


def compute_crop_box(
    src_w: int,
    src_h: int,
    target_w: int,
    target_h: int,
    faces: list[tuple[int, int, int, int]] | None = None,
    template: str = "default",
) -> tuple[int, int, int, int]:
    crop_w, crop_h, _ = crop_dims(src_w, src_h, target_w, target_h)
    if crop_w < 1:
        return 0, 0, src_w, src_h
    max_x = max(0, src_w - crop_w)
    max_y = max(0, src_h - crop_h)
    faces = list(faces or [])

    def clamp(x: int, y: int) -> tuple[int, int]:
        return max(0, min(max_x, x)), max(0, min(max_y, y))

    if not faces:
        x, y = clamp(max_x // 2, max_y // 2)
        return x, y, crop_w, crop_h

    cx = sum(fx + fw / 2 for fx, fy, fw, fh in faces) / len(faces)
    cy = sum(fy + fh / 2 for fx, fy, fw, fh in faces) / len(faces)
    # лицо в верхней трети — как у default (трансфер тоже квадрат с плашкой)
    x = int(round(cx - crop_w * 0.50))
    y = int(round(cy - crop_h * 0.33))
    x, y = clamp(x, y)

    def faces_inside(px: int, py: int) -> bool:
        for fx, fy, fw, fh in faces:
            if fx < px or fy < py or fx + fw > px + crop_w or fy + fh > py + crop_h:
                return False
        return True

    if not faces_inside(x, y):
        min_x = min(fx for fx, fy, fw, fh in faces)
        min_y = min(fy for fx, fy, fw, fh in faces)
        max_r = max(fx + fw for fx, fy, fw, fh in faces)
        max_b = max(fy + fh for fx, fy, fw, fh in faces)
        need_w = max_r - min_x
        need_h = max_b - min_y
        if need_w <= crop_w and need_h <= crop_h:
            x = min_x - (crop_w - need_w) // 2
            y = min_y - (crop_h - need_h) // 2
            x, y = clamp(x, y)
            if min_x < x:
                x = clamp(min_x, y)[0]
            if min_y < y:
                y = clamp(x, min_y)[1]
            if max_r > x + crop_w:
                x = clamp(max_r - crop_w, y)[0]
            if max_b > y + crop_h:
                y = clamp(x, max_b - crop_h)[1]
    return x, y, crop_w, crop_h


def smart_crop(
    path: Path | str,
    target_w: int,
    target_h: int,
    *,
    template: str = "default",
    faces: list[tuple[int, int, int, int]] | None = None,
    dest: Path | None = None,
) -> Path:
    src = Path(path)
    im = _open_rgb(src)
    src_w, src_h = im.size
    if faces is None:
        try:
            faces = detect_faces(im)
        except Exception as e:
            print(f"[editorial] face detect skip: {e}", flush=True)
            faces = []
    x, y, cw, ch = compute_crop_box(src_w, src_h, target_w, target_h, faces, template)
    cropped = im.crop((x, y, x + cw, y + ch))
    out = cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)
    target = dest or src.with_name(f"{src.stem}_{template}_{target_w}x{target_h}.jpg")
    out.save(target, format="JPEG", quality=90, optimize=True)
    return target


def _collect_candidate_urls(
    item: dict[str, Any],
    *,
    limit: int,
    trace: dict[str, Any] | None = None,
    query_override: str | None = None,
) -> list[tuple[str, str]]:
    """Статья (квадратные варианты Championat) + Яндекс по контексту новости."""
    query = (query_override or "").strip() or _query_for(item)
    print(f"[editorial] image query: {query}", flush=True)
    if trace is not None:
        trace["query"] = query
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: str, via: str) -> None:
        u = (url or "").strip()
        if not u or u in seen:
            return
        seen.add(u)
        ordered.append((u, via))

    og = article_image_urls(str(item.get("url") or ""))
    print(f"[editorial] article images={len(og)}", flush=True)
    for url in og:
        add(url, "article")
    if trace is not None:
        trace["article_n"] = len(og)

    player = next(iter(_players_in_title(item)), "")

    searches: list[tuple[str, tuple[str, ...]]] = [(query, ())]
    if player:
        searches.append((f"{player} фото", ("championat.com",)))

    for provider in _image_providers():
        for q, sites in searches:
            if provider.name != "yandex" and sites:
                continue
            try:
                if provider.name == "yandex":
                    urls = provider.search(q, limit=max(6, limit), sites=sites)
                else:
                    urls = provider.search(q, limit=max(6, limit))
            except Exception as e:
                print(f"[editorial] {provider.name} search fail: {e}", flush=True)
                if trace is not None:
                    trace.setdefault("searches", []).append(
                        {"provider": provider.name, "q": q, "sites": list(sites), "error": str(e)[:200]}
                    )
                continue
            print(
                f"[editorial] {provider.name} candidates={len(urls)} q={q[:80]}"
                + (f" sites={','.join(sites)}" if sites else ""),
                flush=True,
            )
            if trace is not None:
                trace.setdefault("searches", []).append(
                    {
                        "provider": provider.name,
                        "q": q,
                        "sites": list(sites),
                        "hits": len(urls),
                    }
                )
            for url in urls:
                via = provider.name
                add(url, via)
                for variant in publisher_image_variants(url):
                    add(variant, via)
            if len(ordered) >= limit * 2:
                break
        if len(ordered) >= limit * 2:
            break
    return ordered[: max(limit * 2, 8)]


def _download_named(url: str, news_id: Any) -> Path | None:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"
    digest = hashlib.sha1(url.encode()).hexdigest()[:10]
    dest = IMAGES_DIR / f"{news_id}_{digest}{ext}"
    return _download(url, dest)


def check_photo_matches_headline(
    cand: ImageCandidate,
    item: dict[str, Any],
) -> tuple[bool, float, str]:
    """Vision: фото соответствует заголовку/смыслу поста?"""
    settings = _imagery_settings()
    if not bool(getattr(settings, "photo_headline_check", True)):
        return True, 1.0, "disabled"
    model = (getattr(settings, "editorial_vision_model", None) or "gpt-5.6-luna").strip()
    headline = " ".join(
        str(item.get(k) or "") for k in ("headline", "caption_line1", "title") if item.get(k)
    ).strip()
    post = str(item.get("post_text") or item.get("body") or "")[:600]
    prompt = (
        f"Заголовок карточки: {headline[:300]}\n"
        f"Смысл поста: {post}\n"
        "Фото должно соответствовать смыслу (не празднование при поражении и т.п.).\n"
        'JSON: {"photo_matches_headline":true,"confidence":0.0,"reason":"..."}'
    )
    try:
        from editorial.openai_client import get_client

        data = get_client().vision(
            model,
            [preview_jpeg(cand.path)],
            prompt,
            json_mode=True,
            max_tokens=300,
            task="photo_headline_check",
        )
        if not isinstance(data, dict):
            return True, 0.5, "bad json"
        ok = bool(data.get("photo_matches_headline"))
        try:
            conf = float(data.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.5
        reason = str(data.get("reason") or "")[:200]
        return ok, conf, reason
    except Exception as e:
        print(f"[editorial] photo_headline_check fail: {e}", flush=True)
        return True, 0.5, str(e)[:120]


def find_photo(item: dict[str, Any], *, template_name: str = "default") -> str | None:
    """Кандидаты → quality → vision → smart-crop. Без годного — None (held)."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    settings = _imagery_settings()
    template = (template_name or "default").strip() or "default"
    cand_max = int(getattr(settings, "imagery_candidates_max", 8) or 8)
    news_id = item.get("id") or "x"
    tw, th = _template_size(template)
    trace = new_trace(item, template=template)

    try:
        urls = _collect_candidate_urls(item, limit=max(cand_max * 2, 8), trace=trace)
        pool: list[ImageCandidate] = []
        seen_hashes: list[int] = []
        for url, via in urls:
            path = _download_named(url, news_id)
            if not path:
                continue
            try:
                with Image.open(path) as im:
                    w, h = im.size
            except Exception:
                continue
            ok, why = quality_ok(path, template)
            if not ok:
                print(f"[editorial] quality drop via={via} {w}x{h}: {why}", flush=True)
                trace["quality_drops"].append(
                    {"via": via, "url": url, "width": w, "height": h, "why": why}
                )
                continue
            try:
                img_hash = _average_hash(path)
            except Exception:
                img_hash = None
            if img_hash is not None and _is_near_duplicate(img_hash, seen_hashes):
                trace.setdefault("dedupe_drops", []).append({"via": via, "url": url})
                continue
            if img_hash is not None:
                seen_hashes.append(img_hash)
            pool.append(ImageCandidate(path=path, url=url, via=via, width=w, height=h))
            trace["quality_ok"].append(
                {"via": via, "url": url, "width": w, "height": h, "path": str(path)}
            )
            print(f"[editorial] quality ok via={via} {w}x{h}", flush=True)
            if len(pool) >= cand_max:
                break

        if not pool:
            gen = _generate_cover_fallback(item, news_id=news_id)
            if gen:
                trace["outcome"] = "generated"
                trace["pick"] = {"path": gen, "via": "generate"}
                return gen
            print("[editorial] no usable image", flush=True)
            trace["outcome"] = "held_quality"
            return None

        use_script_pick = bool(getattr(settings, "vision_single_candidate", True))
        headline_check = bool(getattr(settings, "photo_headline_check", True))
        max_swap = int(getattr(settings, "photo_autoswap_max", 2) or 2)
        min_headline = float(getattr(settings, "photo_check_min", 0.6) or 0.6)
        best: ImageCandidate | None = None
        if use_script_pick:
            ordered = sorted(
                pool,
                key=lambda c: (_via_rank(c.via), -(c.width * c.height), -_sharpness_score(c.path)),
            )
            tried: set[str] = set()
            for swap_i in range(max(1, max_swap + 1)):
                pick = None
                for cand in ordered:
                    key = str(cand.path)
                    if key in tried:
                        continue
                    tried.add(key)
                    pick = verify_single_photo(cand, item, trace=trace) if cand.via != "article" else cand
                    if pick:
                        break
                if not pick:
                    break
                if headline_check:
                    ok_h, conf_h, _r = check_photo_matches_headline(pick, item)
                    if ok_h and conf_h >= min_headline:
                        best = pick
                        break
                    print(
                        f"[editorial] headline check fail swap={swap_i} conf={conf_h:.2f}",
                        flush=True,
                    )
                    if swap_i >= max_swap:
                        trace["outcome"] = "held_headline"
                        return None
                    continue
                best = pick
                break
            if not best:
                print("[editorial] no relevant image", flush=True)
                trace["outcome"] = "held_vision"
                return None
        else:
            ranked = score_relevance(pool, item, trace=trace)
            ranked = dedupe_image_candidates(ranked)
            if not ranked:
                print("[editorial] no relevant image", flush=True)
                trace["outcome"] = "held_vision"
                return None
            best = ranked[0]
        try:
            cropped = smart_crop(best.path, tw, th, template=template)
        except Exception as e:
            print(f"[editorial] smart_crop fail: {e}", flush=True)
            trace["outcome"] = "crop_fail"
            trace["pick"] = {
                "url": best.url,
                "via": best.via,
                "error": str(e)[:200],
            }
            return None
        print(
            f"[editorial] image pick via={best.via} score={best.relevance:.2f} "
            f"{best.width}x{best.height} → {tw}x{th} {best.reason[:80]}",
            flush=True,
        )
        trace["outcome"] = "picked"
        trace["pick"] = {
            "url": best.url,
            "via": best.via,
            "score": best.relevance,
            "reason": best.reason,
            "who": (best.extras or {}).get("who") or "",
            "path": str(best.path),
            "cropped": str(cropped),
            "width": best.width,
            "height": best.height,
        }
        return str(cropped)
    except Exception as e:
        trace["outcome"] = "error"
        trace["error"] = str(e)[:400]
        raise
    finally:
        try:
            _persist_imagery_meta(item, trace)
            append_trace(trace)
        except Exception as e:
            print(f"[editorial] imagery trace skip: {e}", flush=True)


def _persist_imagery_meta(item: dict[str, Any], trace: dict[str, Any]) -> None:
    news_id = item.get("id")
    if not news_id:
        return
    meta = {
        "query": trace.get("query") or "",
        "pick": trace.get("pick") or {},
        "vision": trace.get("vision"),
        "outcome": trace.get("outcome"),
    }
    try:
        from editorial.store import update_news

        update_news(int(news_id), imagery_meta_json=json.dumps(meta, ensure_ascii=False, default=str))
    except Exception as e:
        print(f"[editorial] imagery meta save fail: {e}", flush=True)


def build_photo_pool(
    item: dict[str, Any],
    query: str,
    *,
    template_name: str = "default",
    limit: int = 6,
) -> tuple[list[ImageCandidate], dict[str, Any]]:
    """Пул кандидатов для ручного выбора в TG-модерации."""
    settings = _imagery_settings()
    template = (template_name or "default").strip() or "default"
    cand_max = max(limit, int(getattr(settings, "imagery_candidates_max", 8) or 8))
    news_id = item.get("id") or "x"
    trace = new_trace(item, template=template)
    trace["query"] = query.strip()
    trace["manual_query"] = True
    url_limit = max(limit * 5, cand_max * 3, 30)
    urls = _collect_candidate_urls(
        item, limit=url_limit, trace=trace, query_override=query
    )
    pool: list[ImageCandidate] = []
    seen_hashes: list[int] = []
    for url, via in urls:
        path = _download_named(url, news_id)
        if not path:
            continue
        try:
            with Image.open(path) as im:
                w, h = im.size
        except Exception:
            continue
        ok, _why = quality_ok(path, template)
        if not ok:
            continue
        try:
            img_hash = _average_hash(path)
        except Exception:
            img_hash = None
        if img_hash is not None and _is_near_duplicate(img_hash, seen_hashes):
            trace.setdefault("dedupe_drops", []).append({"via": via, "url": url})
            continue
        if img_hash is not None:
            seen_hashes.append(img_hash)
        pool.append(ImageCandidate(path=path, url=url, via=via, width=w, height=h))
        if len(pool) >= limit:
            break
    ranked = (
        score_relevance(pool, item, trace=trace, manual_relaxed=True) if pool else []
    )
    if not ranked and pool:
        for c in pool:
            c.relevant = True
            c.subject_present = True
            c.relevance = 0.5
            c.reason = "ручной запрос (quality ok)"
        ranked = pool[:limit]
        if trace is not None:
            trace["manual_quality_fallback"] = True
    ranked = dedupe_image_candidates(ranked)
    return ranked[:limit], trace


def apply_photo_choice(
    item: dict[str, Any],
    candidate: ImageCandidate,
    *,
    template_name: str = "default",
) -> str | None:
    """Smart-crop выбранного кадра → путь JPEG."""
    template = (template_name or "default").strip() or "default"
    tw, th = _template_size(template)
    try:
        cropped = smart_crop(candidate.path, tw, th, template=template)
    except Exception as e:
        print(f"[editorial] manual crop fail: {e}", flush=True)
        return None
    return str(cropped)


def ensure_template_crop(
    path: str | Path,
    *,
    template_name: str = "default",
) -> str:
    """Smart-crop под размер шаблона, если кадр ещё не того размера."""
    src = Path(path)
    if not src.is_file():
        return str(path)
    template = (template_name or "default").strip() or "default"
    tw, th = _template_size(template)
    try:
        with Image.open(src) as im:
            w, h = im.size
    except Exception:
        return str(src)
    if w == tw and h == th:
        return str(src)
    try:
        return str(smart_crop(src, tw, th, template=template))
    except Exception as e:
        print(f"[editorial] ensure_template_crop fail: {e}", flush=True)
        return str(src)


def imagery_meta_of(item: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    try:
        raw = json.loads(item.get("imagery_meta_json") or "{}")
        if isinstance(raw, dict):
            meta = raw
    except Exception:
        meta = {}
    if meta.get("query") or meta.get("pick"):
        return meta
    from editorial.imagery_trace import load_trace_for_news

    trace = load_trace_for_news(item.get("id") or 0)
    if not trace:
        return meta
    return {
        "query": trace.get("query") or meta.get("query") or "",
        "pick": trace.get("pick") or meta.get("pick") or {},
        "vision": trace.get("vision") if trace.get("vision") is not None else meta.get("vision"),
        "outcome": trace.get("outcome") or meta.get("outcome") or "",
    }


def _generate_cover_fallback(item: dict[str, Any], *, news_id: Any) -> str | None:
    settings = get_settings()
    if not settings.editorial_image_gen_fallback:
        return None
    from editorial.openai_client import get_client

    model = (settings.editorial_image_model or "gpt-image-1-mini").strip()
    prompt = (
        "Abstract cinematic football stadium at night, floodlights, grass texture, "
        "motion blur, no faces, no real players, no names, no logos, no text. "
        f"Mood: {(item.get('title') or '')[:120]}"
    )
    try:
        raw = get_client().generate_image(model, prompt, size="1024x1536", task="image")
    except Exception as e:
        print(f"[editorial] image gen fail: {e}", flush=True)
        return None
    dest = IMAGES_DIR / f"{news_id}_gen.png"
    dest.write_bytes(raw)
    ok, why = quality_ok(dest, "breaking")
    if ok:
        print(f"[editorial] image ok via generate {model}", flush=True)
        return str(dest)
    print(f"[editorial] generated image quality drop: {why}", flush=True)
    return None
