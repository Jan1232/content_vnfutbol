from __future__ import annotations

from functools import lru_cache

import httpx

from app.config import get_settings

SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"


@lru_cache
def scraper_proxy() -> str | None:
    s = get_settings()
    return (s.scraper_http_proxy or s.groq_http_proxy or "").strip() or None


@lru_cache
def openai_proxy() -> str | None:
    """Platform API режет РФ — ходим через xray (OPENAI_HTTP_PROXY / GROQ / scraper)."""
    s = get_settings()
    return (
        (s.openai_http_proxy or "").strip()
        or (s.groq_http_proxy or "").strip()
        or (s.scraper_http_proxy or "").strip()
        or None
    )


def http_client(**kwargs) -> httpx.Client:
    opts = {
        "timeout": 45.0,
        "follow_redirects": True,
        "headers": {"User-Agent": "Mozilla/5.0 (compatible; MaxRepost/1.0)"},
        "proxy": scraper_proxy(),
        "verify": SYSTEM_CA,
    }
    opts.update(kwargs)
    return httpx.Client(**opts)
