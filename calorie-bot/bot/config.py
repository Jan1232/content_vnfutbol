from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str
    allowed_chat_id: int
    telegram_http_proxy: str = ""

    user_name: str = "Ян"
    user_birthdate: date = date(2002, 8, 19)
    user_height_cm: float = 174
    user_sex: str = "male"
    activity_factor: float = 1.2

    timezone: str = "Asia/Yekaterinburg"

    openclaw_base_url: str = "http://127.0.0.1:18789/v1"
    openclaw_api_key: str = ""
    openclaw_model: str = "openclaw/default"

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_whisper_model: str = "whisper-large-v3"
    groq_http_proxy: str = ""

    # Picooc S1 Pro cloud sync (неофициальный API)
    picooc_email: str = ""
    picooc_password: str = ""
    picooc_role_name: str = ""
    picooc_proxy: str = ""
    picooc_sync_minutes: int = 5
    # mifflin | katch | picooc | auto
    bmr_method: str = "auto"

    reminder_morning_hour: int = 9
    reminder_evening_hour: int = 21

    database_path: Path = Field(default=ROOT / "data" / "calories.db")


@lru_cache
def get_settings() -> Settings:
    return Settings()
