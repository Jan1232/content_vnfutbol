from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import httpx

from bot.config import Settings

log = logging.getLogger("calorie-bot.voice")


async def transcribe_voice(audio_bytes: bytes, settings: Settings, *, filename: str = "voice.ogg") -> str | None:
    if not settings.groq_api_key:
        return None
    try:
        async with httpx.AsyncClient(proxy=settings.groq_http_proxy or None, timeout=90) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                files={"file": (filename, audio_bytes, "audio/ogg")},
                data={
                    "model": settings.groq_whisper_model,
                    "language": "ru",
                    "response_format": "text",
                },
            )
            r.raise_for_status()
            text = r.text.strip()
            return text or None
    except Exception:
        log.exception("Groq whisper failed")
        return None


async def transcribe_telegram_voice(bot, file_id: str, settings: Settings) -> str | None:
    file = await bot.get_file(file_id)
    buf = await bot.download_file(file.file_path)
    data = buf.read()
    suffix = Path(file.file_path or "voice.ogg").suffix or ".ogg"
    return await transcribe_voice(data, settings, filename=f"voice{suffix}")
