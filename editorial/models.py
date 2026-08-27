from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class NewsItem:
    external_id: str
    source: str
    url: str
    title: str
    body: str
    lang: str
    published_at: datetime
    entities: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    event_type: str = "other"
    competition: str = ""
    is_national: bool = False
    cluster_id: str = ""


@dataclass
class Verdict:
    status: str  # CONFIRMED | REJECTED | UNCERTAIN
    confidence: float
    unique_domains: int
    reason: str
    cluster_id: str
    contradiction: str | None = None
    is_official: bool = False
