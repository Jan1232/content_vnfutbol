"""Конфигурация системы генерации контента (отдельно от app/)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]

PRODUCER_MODEL = "gpt-5.6-luna"
CRITIC_MODEL = "gpt-5.6-luna"
FAN_MODEL = "gpt-5.6-terra"  # v2 веер: потолок творчества без критика

ARCHETYPES = (
    "transfer",
    "transfer_cancel",
    "news_opinion",
    "provocation",
    "result",
    "schedule",
    "lineup",
    "goal_live",
    "quote_hypocrisy",
    "quote_scandal",
    "achievement",
    "humor_list",
    "injury_list",
    "meme",
    "video",
)

VERACITY_LEVELS = ("verified", "rumored", "speculation")


def _load_env() -> None:
    # override=True: правки .env (OWNER_CHAT_ID и т.п.) подхватываются без вечного пустого значения из старта процесса
    load_dotenv(ROOT / ".env", override=True)


@lru_cache
def get_openai_client() -> OpenAI:
    _load_env()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не задан в .env")

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    proxy = (
        os.environ.get("OPENAI_HTTP_PROXY", "").strip()
        or os.environ.get("GROQ_HTTP_PROXY", "").strip()
        or os.environ.get("SCRAPER_HTTP_PROXY", "").strip()
    )

    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    if proxy:
        import httpx

        kwargs["http_client"] = httpx.Client(proxy=proxy, timeout=120.0)

    return OpenAI(**kwargs)
