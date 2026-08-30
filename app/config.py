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
    # прямой OpenAI API (перевод/SEO запасной путь + editorial горячий путь)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"
    openai_http_proxy: str = ""
    # editorial: только Platform API, не OpenClaw
    editorial_openai_base_url: str = "https://api.openai.com/v1"
    editorial_llm_transport: str = "openai"
    editorial_text_model: str = "gpt-5.6-luna"
    editorial_text_fallback: str = "gpt-5-mini"
    editorial_classify_model: str = "gpt-5.6-luna"
    editorial_classify_fallback: str = "gpt-5.6-luna"
    editorial_reasoning_model: str = "gpt-5.6-terra"
    editorial_reasoning_fallback: str = "gpt-5.6-luna"
    editorial_reasoning_effort: str = "low"
    editorial_search_model: str = "gpt-5-search-api"
    editorial_image_model: str = "gpt-image-1-mini"
    editorial_image_gen_fallback: bool = False
    editorial_allow_groq_fallback: bool = False
    editorial_llm_timeout: int = 60
    editorial_llm_max_retry: int = 4
    editorial_vision_model: str = "gpt-5.6-luna"
    vision_ab: bool = False
    vision_skip_for_og: bool = True
    vision_single_candidate: bool = True
    imagery_candidates_max: int = 4
    imagery_preview_max_side: int = 512
    imagery_min_relevance: float = 0.55
    imagery_max_upscale: float = 1.75
    imagery_min_sharpness: float = 100.0
    imagery_max_dark_ratio: float = 0.55
    imagery_max_aspect_delta: float = 1.0
    imagery_face_backend: str = "opencv_dnn"

    poll_interval_sec: int = 45
    sync_chats_interval_sec: int = 120
    publish_interval_sec: int = 20
    # Публиковать только посты с фото/видео (для всех источников)
    require_media_all: bool = True
    # Пауза перед публикацией: источник успевает удалить/переопубликовать правку
    publish_hold_sec: int = 180
    # Окно поиска похожих pending для отмены дублей при репосте
    republish_window_sec: int = 600

    # SEO match beacons
    football_data_token: str = ""
    football_data_base: str = "https://api.football-data.org/v4"
    seo_poll_interval_sec: int = 300
    seo_channels_dir: Path = ROOT / "seo" / "channels"

    # Yandex Wordstat — выбор матча по частотности запросов
    # OAuth: https://yandex.ru/support2/wordstat/en/content/api-wordstat
    wordstat_oauth_token: str = ""
    # Альтернатива: Yandex Cloud Search API (Api-Key + folderId)
    yandex_cloud_api_key: str = ""
    yandex_folder_id: str = ""
    wordstat_top_n: int = 5
    # Регионы Wordstat (225 = Россия). Через запятую, напр. 225 или 225,213
    wordstat_regions: str = ""  # пусто = весь мир; 225 = РФ

    db_path: Path = ROOT / "data" / "app.db"
    data_dir: Path = ROOT / "data"

    # Editorial pipeline (independent of repost / SEO workers)
    editorial_poll_interval_sec: int = 60
    editorial_freshness_sec: int = 900
    editorial_channels_dir: Path = ROOT / "editorial" / "channels"
    editorial_max_retry: int = 3
    editorial_min_gap_min: int = 30
    editorial_max_gap_min: int = 55
    editorial_item_ttl_sec: int = 10800
    clubs_file: Path = ROOT / "editorial" / "clubs.yaml"
    fifa_top100_file: Path = ROOT / "editorial" / "fifa_top100.yaml"
    fifa_ranking_backend: str = "fifa"
    fifa_ranking_refresh_sec: int = 86400
    factcheck_min_sources: int = 2
    factcheck_window_sec: int = 1800
    # search-api жрёт десятки тысяч токенов на пост — по умолчанию выкл.
    editorial_factcheck_enabled: bool = False
    openclaw_web_search: bool = True
    # story throttle (§ round-3)
    story_max_per_day: int = 3
    story_hard_cap: int = 4
    story_min_gap_posts: int = 3
    story_min_gap_min: int = 180
    story_incident_window_days: int = 3
    story_llm_relation_enabled: bool = True
    story_relation_hybrid: bool = True
    reasoning_escalate: float = 0.7
    # soccerblog multimodal gate (round-7)
    soccerblog_gate_enabled: bool = True
    soccerblog_auto_publish: bool = False  # true = уверенные мемы в канал без TG-модерации
    soccerblog_auto_confidence: float = 0.8
    soccerblog_gate_model: str = ""
    donor_gate_default: str = "template"
    ad_reject_strict: bool = True
    editorial_cost_benchmark: bool = False
    editorial_live_test: bool = False
    editorial_test_date: str = ""
    # полный лог запросов/ответов LLM (отдельная таблица, автоочистка)
    editorial_llm_full_log: bool = True
    editorial_llm_full_log_retention_days: int = 7
    editorial_llm_full_log_max_response_chars: int = 32_000
    # автоотклонение карточек в TG-модерации (дневные часы Екб)
    moderation_auto_reject_min: int = 60
    moderation_auto_reject_tz: str = "Asia/Yekaterinburg"
    moderation_quiet_start_hour: int = 22  # ночь с 22:00
    moderation_quiet_end_hour: int = 8  # до 08:00
    # meme / video source
    meme_source_enabled: bool = True
    meme_source_max_per_day: int = 5  # 0 = без лимита; дефолт дозирует мем-ленту
    meme_wrap_template: bool = False
    entertainment_floor_ratio: float = 0.20
    profanity_filter: str = "strict"
    profanity_mode: str = ""  # soften | strict; пусто = profanity_filter
    profanity_map: Path = ROOT / "editorial" / "profanity_map.yaml"
    # light_edit: детерминированная косметика вместо LLM rewrite
    editorial_text_mode: str = "light"  # light | llm
    light_edit_profanity: str = ""  # soften | strict; пусто = profanity_mode
    light_edit_strip: Path = ROOT / "editorial" / "light_edit_strip.yaml"
    sticker_pool: Path = ROOT / "editorial" / "sticker_pool.json"
    # шаблон результата матча (донорские табло)
    result_template_enabled: bool = True
    result_min_conf: float = 0.7
    result_logo_fallback: bool = False
    result_require_scorers: bool = True
    club_logos: Path = ROOT / "editorial" / "club_logos.json"
    # round-9 TG donors
    editorial_rss_enabled: bool = False
    tg_incremental: bool = True
    cross_donor_window_min: int = 180
    photo_headline_check: bool = True
    photo_check_min: float = 0.6
    photo_autoswap_max: int = 2
    moderation_queue_depth: int = 3
    video_max_mb: int = 250
    image_search_backend: str = "yandex"
    image_search_api_key: str = ""
    yandex_image_api_key: str = ""
    playwright_headless: bool = True

    fixtures_backend: str = "both"
    fixtures_live: bool = False
    fixtures_leagues_file: Path = ROOT / "editorial" / "fixtures_leagues.yaml"
    matchday_tz: str = "Europe/Moscow"
    matchday_hour_msk: int = 9
    matchday_grace_min: int = 15
    matchday_max_rows: int = 12
    results_enabled: bool = True
    results_poll_window_pre_min: int = 5
    results_poll_window_post_min: int = 30
    results_llm_caption: bool = False

    # Telegram MTProto (Telethon) — для видео «Media is too big»
    # По умолчанию — ключи Telegram Desktop; свой app: https://my.telegram.org
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session_path: str = ""  # default: data/tg_user.session

    # Telegram moderation bot (editorial → approve before MAX)
    api_telegram_bot_token: str = ""
    telegram_admin_id: int = 0
    editorial_tg_moderation: bool = True
    # Telegram content bot — публикация в канал после одобрения (зеркало MAX)
    telegram_content_bot_token: str = ""
    telegram_content_channel: str = ""
    moderation_photo_pool_size: int = 6
    moderation_feedback_dir: Path = ROOT / "data" / "editorial" / "feedback" / "moderation"
    moderation_blocks_file: Path = ROOT / "data" / "editorial" / "feedback" / "content_blocks.json"


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
