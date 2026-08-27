"""Cross-source fact-check gate."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from app.config import get_settings
from editorial import llm
from editorial.models import NewsItem, Verdict
from editorial.store import cluster_domains, list_recent_corpus, record_domain
from editorial.topic_gate import cluster_id_for

HEALTH_EVENT_TYPES = {"injury", "health"}
WEB_SEARCH_EVENT_TYPES = {"injury", "health", "transfer", "official_statement"}
WEB_SEARCH_PICK_TAGS = {"sensation", "transfer_money"}
SKIP_SEARCH_PICK_TAGS = {"human_factor", "addition"}
_EVENT_QUERY = {
    "transfer": "transfer",
    "injury": "injury",
    "health": "injury",
    "official_statement": "official statement",
}
SENSATIONAL_HEALTH = (
    "диагностир",
    "diagnos",
    "рак",
    "cancer",
    "опухол",
    "tumor",
    "невролог",
    "neurolog",
    "инсульт",
    "stroke",
    "инфаркт",
    "heart attack",
    "смертельн",
    "terminal",
    "болезн",
    "disease",
    "заболеван",
)


def _domain(url: str, fallback: str = "") -> str:
    host = (urlparse(url or "").netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host:
        return host
    return (fallback or "").lower()


def _need_sources(event_type: str, min_sources: int) -> int:
    if event_type in HEALTH_EVENT_TYPES:
        return max(3, min_sources)
    return max(1, min_sources)


def _is_sensational_health(title: str, body: str, event_type: str) -> bool:
    blob = f"{title}\n{body}".lower()
    if event_type in HEALTH_EVENT_TYPES:
        return any(k in blob for k in SENSATIONAL_HEALTH) or True
    return any(k in blob for k in SENSATIONAL_HEALTH)


def _item_as_dict(item: NewsItem | dict[str, Any]) -> dict[str, Any]:
    if isinstance(item, NewsItem):
        return {
            "title": item.title,
            "body": item.body,
            "url": item.url,
            "source": item.source,
            "event_type": item.event_type,
            "entities": item.entities,
            "cluster_id": item.cluster_id,
        }
    entities = item.get("entities")
    if not entities:
        try:
            entities = json.loads(item.get("entities_json") or "{}")
        except Exception:
            entities = {}
    return {
        "title": item.get("title") or "",
        "body": item.get("body") or "",
        "url": item.get("url") or "",
        "source": item.get("source") or "",
        "event_type": item.get("event_type") or "",
        "entities": entities,
        "cluster_id": item.get("cluster_id") or "",
    }


def _pick_tag(entities: Any) -> str:
    if not isinstance(entities, dict):
        return ""
    pick = entities.get("pick")
    if isinstance(pick, dict):
        return str(pick.get("tag") or "")
    return ""


def needs_web_search(item: NewsItem | dict[str, Any]) -> bool:
    """Веб-поиск только там, где цена ошибки высокая. Остальное — RSS-консенсус."""
    data = _item_as_dict(item)
    event_type = (data.get("event_type") or "other").strip()
    tag = _pick_tag(data.get("entities"))
    if tag in SKIP_SEARCH_PICK_TAGS:
        return False
    if event_type in WEB_SEARCH_EVENT_TYPES:
        return True
    if _is_sensational_health(data.get("title") or "", data.get("body") or "", event_type):
        return True
    if tag in WEB_SEARCH_PICK_TAGS:
        return True
    return False


def search_query_for(item: NewsItem | dict[str, Any]) -> str:
    """Короткий запрос: игрок + клубы + тип. Без тела новости и длинного заголовка."""
    data = _item_as_dict(item)
    ents = data.get("entities") or {}
    bits: list[str] = []
    bits.extend(str(p).strip() for p in (ents.get("players") or [])[:2] if str(p).strip())
    bits.extend(str(t).strip() for t in (ents.get("teams") or [])[:2] if str(t).strip())
    event_type = (data.get("event_type") or "").strip()
    if event_type in _EVENT_QUERY:
        bits.append(_EVENT_QUERY[event_type])
    bits.append("football")
    q = " ".join(bits).strip()
    if len(q) < 16:
        title = " ".join((data.get("title") or "").split()[:8])
        q = f"{title} football".strip()
    return q[:160]


def _same_cluster(row: dict[str, Any], cluster_id: str) -> bool:
    if (row.get("cluster_id") or "") == cluster_id:
        return True
    try:
        other = cluster_id_for(row)
    except Exception:
        return False
    return other == cluster_id and bool(cluster_id)


def verify(
    item: NewsItem | dict[str, Any],
    *,
    min_sources: int | None = None,
    use_llm: bool = True,
    web_search: bool = True,
) -> Verdict:
    settings = get_settings()
    data = _item_as_dict(item)
    event_type = data.get("event_type") or "other"
    cluster_id = data.get("cluster_id") or cluster_id_for(item)
    need = _need_sources(event_type, int(min_sources or settings.factcheck_min_sources or 2))
    window = int(settings.factcheck_window_sec or 1800)

    corpus = list_recent_corpus(window)
    snippets: list[dict[str, Any]] = []
    domains: set[str] = set()

    own_domain = _domain(data.get("url") or "", data.get("source") or "")
    if own_domain:
        domains.add(own_domain)
        record_domain(cluster_id, own_domain)
        snippets.append(
            {
                "title": data.get("title"),
                "url": data.get("url"),
                "snippet": (data.get("body") or "")[:400],
                "domain": own_domain,
            }
        )

    for row in corpus:
        if not _same_cluster(row, cluster_id):
            continue
        dom = _domain(row.get("url") or "", row.get("source") or "")
        if not dom:
            continue
        domains.add(dom)
        record_domain(cluster_id, dom)
        snippets.append(
            {
                "title": row.get("title"),
                "url": row.get("url"),
                "snippet": (row.get("body") or "")[:300],
                "domain": dom,
            }
        )

    do_search = bool(web_search) and needs_web_search(data)
    if do_search:
        query = search_query_for(data)
        print(f"[editorial] factcheck search query={query!r}", flush=True)
        try:
            hits = llm.web_search(query)
        except Exception as e:
            print(f"[editorial] factcheck web_search: {e}", flush=True)
            return Verdict(
                status="UNCERTAIN",
                confidence=0.3,
                unique_domains=len(domains),
                reason=f"search-api fail: {e}"[:800],
                cluster_id=cluster_id,
            )
        for hit in hits:
            dom = _domain(str(hit.get("url") or ""), str(hit.get("domain") or ""))
            if not dom:
                continue
            domains.add(dom)
            record_domain(cluster_id, dom)
            snippets.append(
                {
                    "title": hit.get("title"),
                    "url": hit.get("url"),
                    "snippet": hit.get("snippet"),
                    "domain": dom,
                }
            )
    elif web_search:
        print(
            f"[editorial] factcheck skip search-api event={event_type} "
            f"tag={_pick_tag(data.get('entities'))}",
            flush=True,
        )

    domains |= cluster_domains(cluster_id)
    unique = len(domains)
    sensational = _is_sensational_health(data.get("title") or "", data.get("body") or "", event_type)

    if unique < need:
        if sensational and unique <= 1:
            return Verdict(
                status="REJECTED",
                confidence=0.2,
                unique_domains=unique,
                reason="единственный источник + сенсационная health/injury-формулировка",
                cluster_id=cluster_id,
            )
        return Verdict(
            status="UNCERTAIN",
            confidence=0.4,
            unique_domains=unique,
            reason=f"недостаточно независимых доменов: {unique} < {need}",
            cluster_id=cluster_id,
        )

    llm_result: dict[str, Any] = {}
    if use_llm:
        try:
            llm_result = llm.factcheck(data, snippets)
        except Exception as e:
            return Verdict(
                status="UNCERTAIN",
                confidence=0.4,
                unique_domains=unique,
                reason=f"llm factcheck fail: {e}",
                cluster_id=cluster_id,
            )

    contradiction = llm_result.get("contradiction")
    if contradiction not in {None, "", False}:
        return Verdict(
            status="REJECTED",
            confidence=float(llm_result.get("confidence") or 0.3),
            unique_domains=unique,
            reason=f"противоречие: {contradiction}",
            cluster_id=cluster_id,
            contradiction=str(contradiction),
            is_official=bool(llm_result.get("is_official")),
        )

    consistent = bool(llm_result.get("consistent", True)) if llm_result else True
    confidence = float(llm_result.get("confidence") or (0.8 if unique >= need else 0.5))
    is_official = bool(llm_result.get("is_official"))
    reason = str(llm_result.get("reason") or f"{unique} независимых доменов")

    if unique >= need and consistent and confidence >= 0.75:
        return Verdict(
            status="CONFIRMED",
            confidence=confidence,
            unique_domains=unique,
            reason=reason,
            cluster_id=cluster_id,
            is_official=is_official,
        )
    if sensational and unique <= 1:
        return Verdict(
            status="REJECTED",
            confidence=confidence,
            unique_domains=unique,
            reason="единственный источник + сенсационная health/injury-формулировка",
            cluster_id=cluster_id,
        )
    return Verdict(
        status="UNCERTAIN",
        confidence=confidence,
        unique_domains=unique,
        reason=reason,
        cluster_id=cluster_id,
        is_official=is_official,
    )
