from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    max_bot_token: str
    max_api_base: str = "https://platform-api2.max.ru"

    admin_login: str
    admin_password: str
    admin_secret: str
    admin_host: str = "127.0.0.1"
    admin_port: int = 8790

    vk_access_token: str = ""

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_http_proxy: str = ""
    # общий прокси для Telegram/парсеров/OpenAI (тот же xray)
    scraper_http_proxy: str = ""

    # перевод: auto = OpenClaw → Groq fallback
    translate_backend: str = "auto"  # auto | openclaw | groq
    openclaw_base_url: str = "http://127.0.0.1:18789/v1"
    openclaw_api_key: str = ""
    openclaw_model: str = "openclaw/default"
    # опционально: форсировать бэкенд-модель агента, напр. openai/gpt-5.5
    openclaw_backend_model: str = ""
    # прямой OpenAI API (перевод / SEO)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"
    openai_http_proxy: str = ""

    poll_interval_sec: int = 45
    sync_chats_interval_sec: int = 45
    # Полный refresh title/icon/participants по всем MAX-каналам
    sync_meta_interval_sec: int = 600
    publish_interval_sec: int = 20
    # Публиковать только посты с фото/видео (для всех источников)
    require_media_all: bool = True
    # Пауза перед публикацией медиа: источник успевает удалить/переопубликовать правку.
    # Текстовые посты (без фото/видео) публикуются без этой паузы.
    publish_hold_sec: int = 180
    # Сколько держим sent-посты для зеркального удаления, если источник стёр пост
    source_delete_track_sec: int = 14400
    # Окно поиска похожих pending для отмены дублей при репосте
    republish_window_sec: int = 600

    # SEO match beacons
    football_data_token: str = ""
    football_data_base: str = "https://api.football-data.org/v4"
    seo_poll_interval_sec: int = 300
    seo_channels_dir: Path = ROOT / "seo" / "channels"

    # Свой контент (сериалы tvoe.live → MAX)
    content_chat_id: int = -76925672995546
    content_ref: str = "6615182816b6563e4d162a70"
    content_button_text: str = "СМОТРЕТЬ ОНЛАЙН"
    content_min_interval_sec: int = 7200
    content_max_interval_sec: int = 10800
    content_quiet_start_hour: int = 22
    content_quiet_end_hour: int = 8
    content_timezone: str = "Europe/Moscow"
    content_sync_interval_sec: int = 21600
    content_db_path: Path = ROOT / "data" / "content.db"
    content_api_base: str = "https://api.tvoe.live"
    content_site_base: str = "https://tvoe.live"
    content_cdn_base: str = "https://static.cdn.tvoe.live"
    content_catalog_limit: int = 100

    # Анонимные опросы (opros) → 3 канала MAX
    opros_openai_model: str = "gpt-6-luna"
    opros_min_interval_sec: int = 7200
    opros_max_interval_sec: int = 10800
    opros_quiet_start_hour: int = 22
    opros_quiet_end_hour: int = 10
    opros_timezone: str = "Asia/Yekaterinburg"
    opros_db_path: Path = ROOT / "data" / "opros.db"
    opros_chat_18plus: int = -79631137880794
    opros_chat_lichnoe: int = -79631097117402
    opros_chat_anon: int = -79631049734874
    opros_chat_football_or: int = -79631311551194  # max.ru/channel_oprosfootball
    opros_chat_football_who: int = -79631342811866  # max.ru/channel_ktokruche
    opros_chat_quiz_apl: int = -77925082381018  # max.ru/channel_aplmatch
    opros_chat_quiz_fifa: int = -77925134482138  # max.ru/channel_fifamatch
    opros_quiz_assets_dir: Path = ROOT / "opros" / "assets" / "quiz"

    # Yandex Wordstat — выбор матча по частотности запросов
    wordstat_oauth_token: str = ""
    yandex_cloud_api_key: str = ""
    yandex_folder_id: str = ""
    wordstat_top_n: int = 5
    wordstat_regions: str = ""

    db_path: Path = ROOT / "data" / "app.db"
    data_dir: Path = ROOT / "data"

    # Telegram MTProto (Telethon) — для видео «Media is too big»
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session_path: str = ""  # default: data/tg_user.session

    # Telegram Bot API — автопостинг в TG-каналы (через запятую)
    tg_bot_tokens: str = ""
    # ЛС для служебных алертов (тишина каналов). Тот же OWNER_CHAT_ID.
    owner_chat_id: int = 0
    silence_alert_hours: float = 24
    silence_check_interval_sec: int = 3600
    # Суточная сводка охватов в ЛС MAX-бота (Контент-мейкер). Несколько id через запятую.
    digest_owner_user_id: str = ""
    digest_timezone: str = "Asia/Yekaterinburg"
    # Обновление пользовательского ключа VK из сохранённой браузерной сессии
    vk_token_refresh_sec: int = 300

    def telegram_bot_tokens(self) -> list[str]:
        return [t.strip() for t in (self.tg_bot_tokens or "").split(",") if t.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_dotenv_manual() -> None:
    """Fallback for workers outside pydantic."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())
