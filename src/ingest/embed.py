"""Эмбеддинги (OpenAI). Дедуп — в ingest.dedup."""

from __future__ import annotations

import math

from src.config import get_openai_client
from src.ingest.sources import EMBEDDING_MODEL


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def embed_text(text: str) -> list[float]:
    client = get_openai_client()
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text[:8000])
    return list(resp.data[0].embedding)
