"""Добор счёта и полного текста для новостей о результатах матчей."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

from app.http_util import http_client

_SCORE_PAIR = re.compile(r"(\d{1,2})\s*[:\-–]\s*(\d{1,2})")
_URL_SCORE = re.compile(
    r"(?:^|[/_-])(?:schet|score|resultat|rezultat)[-_](\d{1,2})[-_](\d{1,2})(?:[/_-]|$)",
    re.IGNORECASE,
)
_SCORE_CONTEXT = re.compile(
    r"(?:"
    r"со\s+сч[её]том"
    r"|сч[её]т(?:ом)?"
    r"|результат(?:ом)?"
    r"|финал"
    r"|побед(?:у|ил(?:и)?)?"
    r"|уступил(?:и)?"
    r"|обыграл(?:и)?"
    r"|разгромил(?:и)?"
    r"|ничья"
    r"|beat(?:s|en)?"
    r"|won"
    r"|finished"
    r"|full[- ]time"
    r"|ft"
    r")\W{0,12}(\d{1,2})\s*[:\-–]\s*(\d{1,2})",
    re.IGNORECASE,
)
_MATCH_HINT = re.compile(
    r"(?:"
    r"rezultat|resultat|result|schet|score|матч(?:а|е)?|"
    r"уступил|обыграл|победил|разгром|ничья|full[- ]time|"
    r"со\s+сч[её]том|сч[её]т\s*\d"
    r")",
    re.IGNORECASE,
)
_CHAMPIONAT_HOST = re.compile(r"(^|\.)championat\.com$", re.IGNORECASE)


def _valid_score(a: int, b: int) -> bool:
    return 0 <= a <= 15 and 0 <= b <= 15 and not (a == 0 and b == 0)


def format_score(home: int, away: int) -> str:
    return f"{home}:{away}"


def parse_score_from_url(url: str) -> tuple[int, int] | None:
    raw = unquote(str(url or "")).strip()
    if not raw:
        return None
    path = urlparse(raw).path.lower()
    m = _URL_SCORE.search(path.replace(".html", "/"))
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if _valid_score(a, b):
            return a, b
    return None


def parse_score_from_text(text: str) -> tuple[int, int] | None:
    blob = str(text or "")
    if not blob.strip():
        return None
    for rx in (_SCORE_CONTEXT, _SCORE_PAIR):
        m = rx.search(blob)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if _valid_score(a, b):
            return a, b
    return None


def body_has_score(text: str) -> bool:
    return parse_score_from_text(text) is not None


def looks_like_match_news(*, title: str = "", body: str = "", url: str = "", event_type: str = "") -> bool:
    if (event_type or "") == "match_result":
        return True
    blob = f"{title}\n{body}\n{url}"
    return bool(_MATCH_HINT.search(blob)) or parse_score_from_url(url) is not None


def _score_sentence(score: tuple[int, int]) -> str:
    return f"Счёт матча: {format_score(*score)}."


def _append_score_clause(body: str, score: tuple[int, int]) -> str:
    base = (body or "").strip()
    clause = _score_sentence(score)
    if clause in base:
        return base
    if not base:
        return clause
    if base.endswith((".", "!", "?", "…")):
        return f"{base} {clause}"
    return f"{base}. {clause}"


def _championat_article_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    root = soup.find(class_="article-content")
    if root:
        paras = [p.get_text(" ", strip=True) for p in root.find_all("p") if p.get_text(strip=True)]
        if paras:
            return "\n\n".join(paras)
    for sel in (("meta", {"property": "og:description"}), ("meta", {"name": "description"})):
        tag = soup.find(sel[0], sel[1])
        if tag and tag.get("content"):
            text = str(tag["content"]).strip()
            if text:
                return text
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    return ""


def fetch_article_body(url: str, *, timeout: float = 20.0) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    host = (urlparse(raw).hostname or "").lower()
    if not _CHAMPIONAT_HOST.search(host):
        return ""
    try:
        with http_client(timeout=timeout) as client:
            r = client.get(
                raw,
                headers={"User-Agent": "Mozilla/5.0 (compatible; vnf-editorial/1.0)"},
                follow_redirects=True,
            )
            r.raise_for_status()
            return _championat_article_text(r.text)
    except Exception as e:
        print(f"[editorial] match_enrich fetch fail {raw[:120]}: {e}", flush=True)
        return ""


def enrich_match_body(
    *,
    title: str = "",
    body: str = "",
    url: str = "",
    event_type: str = "",
    fetch_article: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Вернуть body со счётом и метаданные обогащения."""
    meta: dict[str, Any] = {"enriched": False}
    if not looks_like_match_news(title=title, body=body, url=url, event_type=event_type):
        return body or "", meta

    current = (body or "").strip()
    if body_has_score(current):
        score = parse_score_from_text(f"{title}\n{current}")
        if score:
            meta.update({"score": format_score(*score), "via": "existing"})
        return current, meta

    score = parse_score_from_url(url)
    if score:
        enriched = _append_score_clause(current, score)
        meta.update({"enriched": True, "score": format_score(*score), "via": "url"})
        return enriched, meta

    title_score = parse_score_from_text(title)
    if title_score:
        enriched = _append_score_clause(current, title_score)
        meta.update({"enriched": True, "score": format_score(*title_score), "via": "title"})
        return enriched, meta

    if not fetch_article:
        return current, meta

    article = fetch_article_body(url)
    if article:
        combined = f"{current}\n\n{article}".strip() if current else article.strip()
        score = parse_score_from_text(combined)
        meta.update({"enriched": True, "via": "article", "article_chars": len(article)})
        if score:
            meta["score"] = format_score(*score)
        return combined, meta

    return current, meta


def enrich_news_item(item: Any, *, fetch_article: bool = False) -> Any:
    body, meta = enrich_match_body(
        title=str(getattr(item, "title", "") or ""),
        body=str(getattr(item, "body", "") or ""),
        url=str(getattr(item, "url", "") or ""),
        event_type=str(getattr(item, "event_type", "") or ""),
        fetch_article=fetch_article,
    )
    item.body = body
    if meta.get("enriched"):
        entities = dict(getattr(item, "entities", None) or {})
        entities["match_enrich"] = meta
        item.entities = entities
    return item


def enrich_row(row: dict[str, Any], *, fetch_article: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    body, meta = enrich_match_body(
        title=str(row.get("title") or ""),
        body=str(row.get("body") or ""),
        url=str(row.get("url") or ""),
        event_type=str(row.get("event_type") or ""),
        fetch_article=fetch_article,
    )
    out = dict(row)
    out["body"] = body
    if meta.get("enriched") or meta.get("score"):
        try:
            import json

            entities = json.loads(out.get("entities_json") or "{}")
        except Exception:
            entities = {}
        entities["match_enrich"] = meta
        out["entities_json"] = json.dumps(entities, ensure_ascii=False)
    return out, meta
